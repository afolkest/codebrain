import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from codebrain import cli, db, ingest
from tests._helpers import write_jsonl


def _session(sid, ts="2026-01-01T00:00:00.000Z"):
    return {"type": "session", "id": sid, "timestamp": ts, "cwd": "/work"}


def _user(rid, parent, text, ts):
    return {"type": "message", "id": rid, "timestamp": ts, "parentId": parent,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _pi_root(root: Path, sid="P1", users=("hello pool",)) -> Path:
    d = root / "agent" / "sessions" / "proj"
    d.mkdir(parents=True, exist_ok=True)
    records = [_session(sid)]
    parent = None
    for i, text in enumerate(users, start=1):
        rid = f"u{i:07d}"[:8]
        records.append(_user(rid, parent, text, f"2026-01-01T00:0{i}:00.000Z"))
        parent = rid
    return write_jsonl(d, f"0_{sid}.jsonl", records)


def _pool_pi(pool: Path, machine: str, sid="P1", users=("hello pool",)) -> Path:
    return _pi_root(pool / "raw" / machine / "pi", sid=sid, users=users)


class TestPoolRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.pool = self.root / "pool"
        self.db_path = self.root / "codebrain.db"

    def conn(self):
        c = db.connect(self.db_path)
        self.addCleanup(c.close)
        return c

    def empty_live_roots(self):
        roots = {}
        for src in ingest.SOURCES:
            roots[src] = self.root / f"empty-{src}"
            roots[src].mkdir(parents=True, exist_ok=True)
        return roots

    def run_cli(self, *args, env=None, roots=None):
        out, err = io.StringIO(), io.StringIO()
        patches = []
        roots = roots or self.empty_live_roots()
        patches += [
            mock.patch("codebrain.ingest.DEFAULT_CLAUDE_ROOT", roots["claude"]),
            mock.patch("codebrain.ingest.DEFAULT_CODEX_ROOT", roots["codex"]),
            mock.patch("codebrain.ingest.DEFAULT_PI_ROOT", roots["pi"]),
            mock.patch("codebrain.ingest.DEFAULT_CURSOR_ROOT", roots["cursor"]),
            mock.patch("codebrain.ingest.DEFAULT_CURSOR_DB", self.root / "missing-cursor.db"),
            mock.patch("codebrain.cli.DEFAULT_POOL", self.pool),
            # Keep the read-path bmux overlay off the developer's real ~/.bmux
            # log — hermetic tests must not depend on (or ingest) local data —
            # and pin the freshness-gate window so an ambient
            # CODEBRAIN_MAX_STALENESS (a documented user knob) can't flip
            # gate-dependent assertions. Tests override via env=.
            mock.patch.dict(os.environ, {
                "CODEBRAIN_BMUX_LOG": str(self.root / "no-bmux.jsonl"),
                "CODEBRAIN_MAX_STALENESS": "600",
            }, clear=False),
        ]
        if env is not None:
            patches.append(mock.patch.dict(os.environ, env, clear=False))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                cli.main(["--db", str(self.db_path), *args])
        return out.getvalue(), err.getvalue()

    def test_refresh_pool_ingests_remote_and_preserves_origin_machine(self):
        _pool_pi(self.pool, "mini", sid="P1")
        conn = self.conn()

        stats = ingest.refresh_pool(conn, self.pool, sources=("pi",), local_machines={"local"})

        self.assertEqual(stats["pool_roots"], 1)
        self.assertEqual(stats["sessions"], 1)
        row = conn.execute("SELECT machine FROM sessions WHERE session_id='pi:P1'").fetchone()
        self.assertEqual(row["machine"], "mini")

    def test_refresh_pool_skips_configured_local_alias(self):
        _pool_pi(self.pool, "mini", sid="REMOTE")
        _pool_pi(self.pool, "alias", sid="LOCAL")
        conn = self.conn()

        stats = ingest.refresh_pool(conn, self.pool, sources=("pi",), local_machines={"alias"})

        self.assertEqual(stats["pool_roots"], 1)
        self.assertEqual(stats["skipped_local_roots"], 1)
        rows = conn.execute("SELECT session_id FROM sessions ORDER BY session_id").fetchall()
        self.assertEqual([r["session_id"] for r in rows], ["pi:REMOTE"])

    def test_read_command_auto_refreshes_remote_pool_but_no_refresh_skips_it(self):
        _pool_pi(self.pool, "mini", sid="REMOTE", users=("remote intent",))

        out, _ = self.run_cli("recent", "--json")
        rows = json.loads(out)
        self.assertEqual([(r["session_id"], r["last_user_text"]) for r in rows],
                         [("pi:REMOTE", "remote intent")])

        self.db_path.unlink()
        out, _ = self.run_cli("recent", "--json", "--no-refresh")
        self.assertEqual(json.loads(out), [])

    def test_auto_refresh_skips_stale_local_pool_after_fresh_live_home(self):
        live_pi = self.root / "live-pi"
        _pi_root(live_pi, sid="P1", users=("old", "fresh"))
        _pool_pi(self.pool, "alias", sid="P1", users=("old",))
        roots = self.empty_live_roots()
        roots["pi"] = live_pi

        with mock.patch("codebrain.ingest.DEFAULT_CLAUDE_ROOT", roots["claude"]), \
             mock.patch("codebrain.ingest.DEFAULT_CODEX_ROOT", roots["codex"]), \
             mock.patch("codebrain.ingest.DEFAULT_PI_ROOT", roots["pi"]), \
             mock.patch("codebrain.ingest.DEFAULT_CURSOR_ROOT", roots["cursor"]), \
             mock.patch("codebrain.ingest.DEFAULT_CURSOR_DB", self.root / "missing-cursor.db"), \
             mock.patch("codebrain.cli.DEFAULT_POOL", self.pool), \
             mock.patch.dict(os.environ, {"CODEBRAIN_LOCAL_MACHINES": "alias"}, clear=False), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["--db", str(self.db_path), "recent", "--json"])

        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        n = conn.execute("SELECT COUNT(*) AS c FROM transcript WHERE session_id='pi:P1'").fetchone()["c"]
        self.assertEqual(n, 2)

    def test_ingest_pool_filters_roots_and_does_not_pre_refresh_all_pool(self):
        _pool_pi(self.pool, "mini", sid="MINI")
        _pool_pi(self.pool, "air", sid="AIR")

        out, _ = self.run_cli("ingest-pool", "--pool", str(self.pool),
                              "--machine", "mini", "--source", "pi")
        self.assertIn("pool_roots=1", out)
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        rows = conn.execute("SELECT session_id FROM sessions ORDER BY session_id").fetchall()
        self.assertEqual([r["session_id"] for r in rows], ["pi:MINI"])

    def test_ingest_pool_include_local_is_explicit_and_reparses_live_homes(self):
        _pool_pi(self.pool, "alias", sid="LOCAL")
        live_pi = self.root / "live-pi"
        _pi_root(live_pi, sid="LIVE")
        roots = self.empty_live_roots()
        roots["pi"] = live_pi

        out, _ = self.run_cli(
            "ingest-pool", "--pool", str(self.pool), "--machine", "alias",
            "--source", "pi", "--include-local",
            env={"CODEBRAIN_LOCAL_MACHINES": "alias"}, roots=roots,
        )
        self.assertIn("reparsing local sources", out)
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        rows = conn.execute("SELECT session_id FROM sessions ORDER BY session_id").fetchall()
        self.assertEqual([r["session_id"] for r in rows], ["pi:LIVE", "pi:LOCAL"])

    def test_ingest_pool_include_local_reparses_even_without_alias_config(self):
        _pool_pi(self.pool, "mini", sid="LOCAL")
        live_pi = self.root / "live-pi"
        _pi_root(live_pi, sid="LIVE")
        roots = self.empty_live_roots()
        roots["pi"] = live_pi

        out, _ = self.run_cli(
            "ingest-pool", "--pool", str(self.pool), "--machine", "mini",
            "--source", "pi", "--include-local", roots=roots,
        )

        self.assertIn("reparsing local sources", out)
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        rows = conn.execute("SELECT session_id FROM sessions ORDER BY session_id").fetchall()
        self.assertEqual([r["session_id"] for r in rows], ["pi:LIVE", "pi:LOCAL"])

    def test_ingest_pool_rejects_invalid_machine_component(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_cli("ingest-pool", "--pool", str(self.pool), "--machine", "../bad")
        self.assertEqual(cm.exception.code, 2)

    def test_sweep_collects_and_refreshes_local_and_pool(self):
        live_pi = self.root / "live-pi"
        _pi_root(live_pi, sid="LIVE", users=("local intent",))
        _pool_pi(self.pool, "mini", sid="REMOTE", users=("remote intent",))
        roots = self.empty_live_roots()
        roots["pi"] = live_pi

        # collect reads its roots from collect.DEFAULT_ROOTS (captured at import),
        # so the sweep test must redirect those too, not only the ingest defaults.
        collect_roots = dict(roots)
        with mock.patch("codebrain.collect.DEFAULT_ROOTS", collect_roots):
            out, _ = self.run_cli("sweep", "--pool", str(self.pool),
                                  "--machine", "local", "--source", "pi",
                                  env={"CODEBRAIN_LOCAL_MACHINES": "local"},
                                  roots=roots)

        self.assertIn("collect:", out)
        self.assertIn("refresh: local", out)
        # Durability: the live session was mirrored into this machine's subtree.
        mirrored = self.pool / "raw" / "local" / "pi" / "agent" / "sessions" / "proj" / "0_LIVE.jsonl"
        self.assertTrue(mirrored.is_file())
        # Freshness: both the live session and the synced remote one are queryable.
        conn = self.conn()
        rows = conn.execute("SELECT session_id FROM sessions ORDER BY session_id").fetchall()
        self.assertEqual([r["session_id"] for r in rows], ["pi:LIVE", "pi:REMOTE"])

    def test_sweep_runs_all_three_provenance_overlays(self):
        # README/sweep contract: the pass covers "provenance overlays", not just
        # collect + refresh. Deleting those sync calls must fail a test.
        calls = []
        with mock.patch("codebrain.collect.DEFAULT_ROOTS", self.empty_live_roots()), \
             mock.patch("codebrain.bmux.sync",
                        side_effect=lambda conn, **kw: calls.append("bmux") or {}), \
             mock.patch("codebrain.codex_control.sync",
                        side_effect=lambda conn, **kw: calls.append("codex") or {}), \
             mock.patch("codebrain.cursor_provenance.sync",
                        side_effect=lambda conn, **kw: calls.append("cursor") or {}):
            self.run_cli("sweep", "--pool", str(self.pool), "--source", "pi",
                         "--machine", "local",
                         env={"CODEBRAIN_LOCAL_MACHINES": "local"})
        self.assertEqual(sorted(calls), ["bmux", "codex", "cursor"])

    def _marker(self):
        conn = self.conn()
        return conn.execute(
            "SELECT mtime, size FROM ingest_state WHERE path = ?",
            (cli.SWEEP_STATE_PATH,),
        ).fetchone()

    def _full_sweep(self, roots, collect_roots):
        with mock.patch("codebrain.collect.DEFAULT_ROOTS", collect_roots):
            return self.run_cli("sweep", "--pool", str(self.pool),
                                "--machine", "local",
                                env={"CODEBRAIN_LOCAL_MACHINES": "local"},
                                roots=roots)

    def _grown_live_pi(self):
        """A live pi root whose transcript gains a message after the sweep."""
        live_pi = self.root / "live-pi"
        path = _pi_root(live_pi, sid="LIVE", users=("first intent",))
        roots = self.empty_live_roots()
        roots["pi"] = live_pi
        return roots, path

    def _last_user(self, *args, roots=None, env=None):
        # Reads refresh the pool too; without the local-machine env the sweep's
        # own (possibly stale) pool mirror would be ingested as a remote's data.
        merged = {"CODEBRAIN_LOCAL_MACHINES": "local", **(env or {})}
        out, _ = self.run_cli("recent", "--json", *args, roots=roots, env=merged)
        rows = json.loads(out)
        return rows[0]["last_user_text"] if rows else None

    def test_fresh_sweep_marker_gates_reads_and_fresh_forces_refresh(self):
        roots, _ = self._grown_live_pi()
        before = time.time()
        self._full_sweep(roots, dict(roots))
        marker = self._marker()
        self.assertIsNotNone(marker)
        # Stamped with the refresh-phase start: files appearing mid-refresh may
        # be missed by that pass, so staleness counts from before the scan.
        self.assertGreaterEqual(marker["mtime"], before)
        self.assertLessEqual(marker["mtime"], time.time())

        _pi_root(self.root / "live-pi", sid="LIVE",
                 users=("first intent", "second intent"))
        # Gated read: the recent full sweep lets the read skip its own refresh,
        # so the post-sweep message is not yet visible.
        self.assertEqual(self._last_user(roots=roots), "first intent")
        # --fresh bypasses the gate and ingests the delta.
        self.assertEqual(self._last_user("--fresh", roots=roots), "second intent")
        # ...after which the gated read serves the now-current data.
        self.assertEqual(self._last_user(roots=roots), "second intent")

    def test_stale_marker_and_disabled_gate_let_reads_refresh(self):
        roots, _ = self._grown_live_pi()
        self._full_sweep(roots, dict(roots))
        _pi_root(self.root / "live-pi", sid="LIVE",
                 users=("first intent", "second intent"))

        with self.subTest("env zero disables the gate"):
            self.assertEqual(
                self._last_user(roots=roots, env={"CODEBRAIN_MAX_STALENESS": "0"}),
                "second intent")

        with self.subTest("an aged-out marker no longer gates"):
            _pi_root(self.root / "live-pi", sid="LIVE",
                     users=("first intent", "second intent", "third intent"))
            conn = self.conn()
            conn.execute("UPDATE ingest_state SET mtime = mtime - 100000 "
                         "WHERE path = ?", (cli.SWEEP_STATE_PATH,))
            conn.commit()
            self.assertEqual(self._last_user(roots=roots), "third intent")

    def test_event_insert_after_sweep_voids_the_gate(self):
        # The marker pins the events watermark it certified: any event row
        # inserted afterwards (manual ingest, --fresh read, an in-flight
        # sweep's per-file commits) must void the gate even while the marker
        # is young, or gated readers would see events whose provenance
        # overlays never ran (defaulting them to human).
        roots, _ = self._grown_live_pi()
        self._full_sweep(roots, dict(roots))
        _pi_root(self.root / "live-pi", sid="LIVE",
                 users=("first intent", "second intent"))
        conn = self.conn()
        conn.execute(
            "INSERT INTO events (event_id, origin_session_id, ts, actor, type,"
            " text, refs, raw) VALUES ('x:tip', NULL, '2026-01-01T00:00:00Z',"
            " 'user', 'message', 'concurrent', '{}', '{}')")
        conn.commit()
        self.assertEqual(self._last_user(roots=roots), "second intent")

    def test_future_marker_does_not_gate(self):
        roots, _ = self._grown_live_pi()
        self._full_sweep(roots, dict(roots))
        _pi_root(self.root / "live-pi", sid="LIVE",
                 users=("first intent", "second intent"))
        conn = self.conn()
        conn.execute("UPDATE ingest_state SET mtime = mtime + 100000 "
                     "WHERE path = ?", (cli.SWEEP_STATE_PATH,))
        conn.commit()
        self.assertEqual(self._last_user(roots=roots), "second intent")

    def test_marker_stamps_refresh_start_not_completion(self):
        # Stamping completion time would over-claim freshness by the whole
        # refresh duration: files appearing mid-refresh may be missed by the
        # pass, so staleness must be measured from before the scan began.
        roots, _ = self._grown_live_pi()
        real_refresh = cli.refresh

        def slow_refresh(conn, **kw):
            stats = real_refresh(conn, **kw)
            time.sleep(0.05)
            return stats

        with mock.patch("codebrain.cli.refresh", side_effect=slow_refresh):
            self._full_sweep(roots, dict(roots))
        finished = time.time()
        marker = self._marker()
        self.assertIsNotNone(marker)
        self.assertLessEqual(marker["mtime"], finished - 0.05)

    def test_manual_ingest_commands_void_the_marker_up_front(self):
        # ingest/ingest-pool mutate canonical data without overlay syncs, and
        # in-place event updates don't move the watermark — so they must drop
        # the marker before touching anything.
        roots, _ = self._grown_live_pi()
        for command in (("ingest", "--source", "pi"),
                        ("ingest-pool", "--pool", str(self.pool))):
            with self.subTest(command[0]):
                self._full_sweep(roots, dict(roots))
                self.assertIsNotNone(self._marker())
                self.run_cli(*command, roots=roots,
                             env={"CODEBRAIN_LOCAL_MACHINES": "local"})
                self.assertIsNone(self._marker())

    def test_incomplete_sweep_deletes_a_previous_marker(self):
        # A failing pass may have updated events in place beyond what the old
        # marker certified; the watermark can't catch updates, so the sweep
        # must not leave the stale marker gating reads.
        roots, _ = self._grown_live_pi()
        self._full_sweep(roots, dict(roots))
        self.assertIsNotNone(self._marker())
        with mock.patch("codebrain.collect.DEFAULT_ROOTS", dict(roots)), \
             mock.patch("codebrain.cursor_provenance.sync",
                        side_effect=RuntimeError("boom")):
            self.run_cli("sweep", "--pool", str(self.pool), "--machine", "local",
                         env={"CODEBRAIN_LOCAL_MACHINES": "local"}, roots=roots)
        self.assertIsNone(self._marker())

    def test_refresh_errors_block_the_stamp(self):
        roots, _ = self._grown_live_pi()
        errored = {"files": 1, "sessions": 0, "events": 0, "placements": 0,
                   "skipped": 0, "conflicts": 0, "errors": 1}
        with mock.patch("codebrain.collect.DEFAULT_ROOTS", dict(roots)), \
             mock.patch("codebrain.cli.refresh", return_value=errored):
            self.run_cli("sweep", "--pool", str(self.pool), "--machine", "local",
                         env={"CODEBRAIN_LOCAL_MACHINES": "local"}, roots=roots)
        self.assertIsNone(self._marker())

    def test_writer_racing_the_overlay_phase_is_not_certified(self):
        # An event committed after the overlays' snapshot (e.g. a manual
        # ingest racing the sweep) must not be folded into the stamped
        # watermark — the overlays never processed it. The sweep skips the
        # stamp; the gate must be off afterwards.
        roots, _ = self._grown_live_pi()

        def racing_sync(conn, **kw):
            conn.execute(
                "INSERT INTO events (event_id, origin_session_id, ts, actor,"
                " type, text, refs, raw) VALUES ('x:racer', NULL,"
                " '2026-01-01T00:00:00Z', 'user', 'message', 'raced', '{}',"
                " '{}')")
            conn.commit()
            return {}

        with mock.patch("codebrain.collect.DEFAULT_ROOTS", dict(roots)), \
             mock.patch("codebrain.cursor_provenance.sync",
                        side_effect=racing_sync):
            self.run_cli("sweep", "--pool", str(self.pool), "--machine", "local",
                         env={"CODEBRAIN_LOCAL_MACHINES": "local"}, roots=roots)
        conn = self.conn()
        with mock.patch.dict(os.environ, {"CODEBRAIN_MAX_STALENESS": "600"}):
            self.assertFalse(cli._sweep_is_fresh(conn))

    def test_cursor_head_advance_after_sweep_voids_the_gate(self):
        # Cursor rewrites events IN PLACE (no new rowids), but every accepted
        # Cursor mutation advances its session's head revision — the
        # generation's second component. A head advance after the stamp must
        # void the gate.
        roots, _ = self._grown_live_pi()
        self._full_sweep(roots, dict(roots))
        conn = self.conn()
        with mock.patch.dict(os.environ, {"CODEBRAIN_MAX_STALENESS": "600"}):
            self.assertTrue(cli._sweep_is_fresh(conn))
            conn.execute(
                "INSERT INTO cursor_session_heads (session_id, revision,"
                " digest) VALUES ('cursor:raced', 1, ?)", ("0" * 64,))
            conn.commit()
            self.assertFalse(cli._sweep_is_fresh(conn))

    def test_interrupt_during_overlays_deletes_a_previous_marker(self):
        # KeyboardInterrupt escapes the per-overlay except Exception; the
        # sweep's refresh may have committed in-place updates the generation
        # can't see, so the old marker must not survive the escape.
        roots, _ = self._grown_live_pi()
        self._full_sweep(roots, dict(roots))
        self.assertIsNotNone(self._marker())
        with mock.patch("codebrain.collect.DEFAULT_ROOTS", dict(roots)), \
             mock.patch("codebrain.cursor_provenance.sync",
                        side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_cli("sweep", "--pool", str(self.pool),
                             "--machine", "local",
                             env={"CODEBRAIN_LOCAL_MACHINES": "local"},
                             roots=roots)
        self.assertIsNone(self._marker())

    def test_refresh_exception_deletes_a_previous_marker(self):
        # An exception escaping refresh may leave partially committed
        # in-place updates the watermark can't see; the old marker must not
        # survive to certify them.
        roots, _ = self._grown_live_pi()
        self._full_sweep(roots, dict(roots))
        self.assertIsNotNone(self._marker())
        with mock.patch("codebrain.collect.DEFAULT_ROOTS", dict(roots)), \
             mock.patch("codebrain.cli.refresh",
                        side_effect=RuntimeError("mid-refresh crash")):
            with self.assertRaises(RuntimeError):
                self.run_cli("sweep", "--pool", str(self.pool),
                             "--machine", "local",
                             env={"CODEBRAIN_LOCAL_MACHINES": "local"},
                             roots=roots)
        self.assertIsNone(self._marker())

    def test_max_staleness_parsing_fails_toward_disabled(self):
        cases = {"": 600.0, "42.5": 42.5, "0": 0.0,
                 "off": 0.0, "inf": 0.0, "nan": 0.0}
        for raw, expected in cases.items():
            with self.subTest(repr(raw)):
                with mock.patch.dict(os.environ,
                                     {"CODEBRAIN_MAX_STALENESS": raw}):
                    self.assertEqual(cli._max_staleness_sec(), expected)

    def test_partial_or_failed_sweeps_do_not_stamp_the_marker(self):
        roots, _ = self._grown_live_pi()
        with self.subTest("partial --source sweep"):
            with mock.patch("codebrain.collect.DEFAULT_ROOTS", dict(roots)):
                self.run_cli("sweep", "--pool", str(self.pool), "--source", "pi",
                             "--machine", "local",
                             env={"CODEBRAIN_LOCAL_MACHINES": "local"}, roots=roots)
            self.assertIsNone(self._marker())

        with self.subTest("non-default pool sweep"):
            other = self.root / "other-pool"
            with mock.patch("codebrain.collect.DEFAULT_ROOTS", dict(roots)):
                self.run_cli("sweep", "--pool", str(other), "--machine", "local",
                             env={"CODEBRAIN_LOCAL_MACHINES": "local"}, roots=roots)
            self.assertIsNone(self._marker())

        with self.subTest("failing provenance overlay"):
            with mock.patch("codebrain.collect.DEFAULT_ROOTS", dict(roots)), \
                 mock.patch("codebrain.cursor_provenance.sync",
                            side_effect=RuntimeError("boom")):
                self.run_cli("sweep", "--pool", str(self.pool), "--machine", "local",
                             env={"CODEBRAIN_LOCAL_MACHINES": "local"}, roots=roots)
            self.assertIsNone(self._marker())

    def test_sweep_install_launchd_passes_the_sweep_command(self):
        # A silent regression here installs a collect-only agent while printing
        # "sweeps (collect + refresh)". Pin the kwarg through the CLI wiring.
        with mock.patch("codebrain.cli.install_launchd",
                        return_value=Path("/tmp/agent.plist")) as inst:
            out, _ = self.run_cli("sweep", "--install-launchd",
                                  "--pool", str(self.pool), "--interval", "300")
        self.assertEqual(inst.call_args.kwargs.get("command"), "sweep")
        self.assertIn("LaunchAgent loaded", out)
        with mock.patch("codebrain.cli.install_launchd",
                        return_value=Path("/tmp/agent.plist")) as inst:
            self.run_cli("collect", "--install-launchd", "--pool", str(self.pool))
        self.assertNotEqual(inst.call_args.kwargs.get("command"), "sweep")


if __name__ == "__main__":
    unittest.main()

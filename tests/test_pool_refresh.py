import contextlib
import io
import json
import os
import tempfile
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


if __name__ == "__main__":
    unittest.main()

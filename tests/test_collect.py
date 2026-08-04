"""collect — the append-only pool sweep (DESIGN.md collector): allowlist-only
capture (credentials never leave the home), stat-compare incrementality, the
shrink guard, and the pool-as-ingest-root round trip. Nothing ever deletes
from the pool."""
import contextlib
import io
import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from codebrain import collect, ingest
from tests._helpers import memory_db, write_jsonl


def _session(sid, ts):
    return {"type": "session", "id": sid, "timestamp": ts, "cwd": "/work"}


def _user(rid, parent, text, ts):
    return {"type": "message", "id": rid, "timestamp": ts, "parentId": parent,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


class CollectBase(unittest.TestCase):
    """A fake tool home + a pool, swept with machine='t'."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.pool = Path(tempfile.mkdtemp())

    def sweep(self, source="pi"):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            stats = collect.collect_source(source, raw_root=self.home,
                                           pool_root=self.pool, machine="t")
        self.last_out = out.getvalue()
        return stats

    def pooled(self, rel, source="pi"):
        return self.pool / "raw" / "t" / source / rel

    def pool_names(self):
        return {p.name for p in self.pool.rglob("*") if p.is_file()}

    def pi_home(self):
        d = self.home / "agent" / "sessions" / "proj"
        d.mkdir(parents=True)
        f = write_jsonl(d, "0_P.jsonl", [
            _session("P1", "2026-01-01T00:00:00.000Z"),
            _user("aaaa1111", None, "hello pool", "2026-01-01T00:01:00.000Z"),
        ])
        (self.home / "agent" / "run-history.jsonl").write_text('{"run": 1}\n', encoding="utf-8")
        # credentials live right next to the logs and must never leave the home
        (self.home / "agent" / "auth.json").write_text('{"apiKey": "SECRET"}', encoding="utf-8")
        return f


class TestSweep(CollectBase):
    def test_first_sweep_copies_allowlist_only(self):
        f = self.pi_home()
        stats = self.sweep()
        self.assertEqual((stats["files"], stats["new"], stats["errors"]), (2, 2, 0))
        dst = self.pooled("agent/sessions/proj/0_P.jsonl")
        self.assertEqual(dst.read_bytes(), f.read_bytes())   # layout + bytes preserved
        self.assertEqual(dst.stat().st_mtime, f.stat().st_mtime)  # copy2 → stat compare
        self.assertNotIn("auth.json", self.pool_names())      # secrets stay home

    def test_unchanged_sweep_is_a_noop(self):
        self.pi_home()
        self.sweep()
        stats = self.sweep()
        self.assertEqual((stats["new"], stats["updated"], stats["unchanged"]), (0, 0, 2))

    def test_collect_machine_env_alias(self):
        self.pi_home()
        with mock.patch.dict(os.environ, {"CODEBRAIN_MACHINE": "alias"}, clear=False):
            collect.collect_source("pi", raw_root=self.home, pool_root=self.pool)
        self.assertTrue((self.pool / "raw" / "alias" / "pi").is_dir())

    def test_collect_rejects_path_machine_names(self):
        self.pi_home()
        with self.assertRaises(ValueError):
            collect.collect_source("pi", raw_root=self.home, pool_root=self.pool,
                                   machine="../bad")
        with mock.patch.dict(os.environ, {"CODEBRAIN_MACHINE": "bad/name"}, clear=False):
            with self.assertRaises(ValueError):
                collect.collect_source("pi", raw_root=self.home, pool_root=self.pool)

    def test_grown_file_recopied(self):
        f = self.pi_home()
        self.sweep()
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(_user("bbbb2222", "aaaa1111", "more",
                                      "2026-01-01T00:02:00.000Z")) + "\n")
        stats = self.sweep()
        self.assertEqual((stats["updated"], stats["unchanged"]), (1, 1))
        self.assertEqual(self.pooled("agent/sessions/proj/0_P.jsonl").read_bytes(),
                         f.read_bytes())

    def test_shrink_guard_keeps_pool_copy_until_regrowth(self):
        f = self.pi_home()
        self.sweep()
        big = f.read_bytes()
        f.write_bytes(big[: len(big) // 2])   # truncated source: regression signal
        stats = self.sweep()
        self.assertEqual(stats["shrunk"], 1)
        self.assertIn("shrink-guard", self.last_out)
        dst = self.pooled("agent/sessions/proj/0_P.jsonl")
        self.assertEqual(dst.read_bytes(), big)              # archive copy wins
        # the guard is not a wedge: a source that grows past the pool copies again
        f.write_bytes(big + b'{"type": "x"}\n')
        stats = self.sweep()
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(dst.read_bytes(), big + b'{"type": "x"}\n')

    def test_source_deletion_never_deletes_pool(self):
        f = self.pi_home()
        self.sweep()
        f.unlink()   # upstream cleanup must never propagate to the archive
        stats = self.sweep()
        self.assertEqual(stats["files"], 1)   # only run-history.jsonl left to scan
        self.assertTrue(self.pooled("agent/sessions/proj/0_P.jsonl").exists())

    def test_symlink_not_followed(self):
        self.pi_home()
        outside = Path(tempfile.mkdtemp()) / "loot.jsonl"
        outside.write_text('{"secret": true}\n', encoding="utf-8")
        (self.home / "agent" / "sessions" / "proj" / "link.jsonl").symlink_to(outside)
        stats = self.sweep()
        self.assertEqual(stats["files"], 2)   # the link was never discovered
        self.assertNotIn("link.jsonl", self.pool_names())

    def test_stale_part_pruned_fresh_kept(self):
        """Crash leftovers (.part tmps) are swept up once they're old; a tmp young
        enough to belong to a concurrently running sweep is left alone."""
        self.pi_home()
        self.sweep()
        d = self.pool / "raw" / "t" / "pi" / "agent" / "sessions" / "proj"
        stale = d / ".dead.jsonl.123.part"
        stale.write_text("torn", encoding="utf-8")
        two_hours_ago = time.time() - 7200
        os.utime(stale, (two_hours_ago, two_hours_ago))
        unrelated = d / "victim.part"
        unrelated.write_text("not collector-owned", encoding="utf-8")
        os.utime(unrelated, (two_hours_ago, two_hours_ago))
        fresh = d / ".live.jsonl.456.part"
        fresh.write_text("inflight", encoding="utf-8")
        self.sweep()
        self.assertFalse(stale.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(fresh.exists())


class TestAllowlists(CollectBase):
    def test_claude_patterns(self):
        """projects/ (and the other session-data dirs) are taken WHOLE — the
        acc6608 review found extension lists dropping transcript-referenced
        sidecars (tool-results, *.meta.json). Env snapshots stay out: env vars
        hold tokens."""
        proj = self.home / "projects" / "-p-"
        for d in ("sid1/subagents", "sid1/tool-results", "memory", "session-memory"):
            (proj / d).mkdir(parents=True)
        (proj / "sid1.jsonl").write_text("{}\n", encoding="utf-8")
        (proj / "sid1" / "subagents" / "agent-x.jsonl").write_text("{}\n", encoding="utf-8")
        (proj / "sid1" / "subagents" / "agent-x.meta.json").write_text("{}", encoding="utf-8")
        (proj / "sid1" / "tool-results" / "r1.txt").write_text("big output", encoding="utf-8")
        (proj / "sessions-index.json").write_text("{}", encoding="utf-8")
        (proj / "memory" / "MEMORY.md").write_text("# m", encoding="utf-8")
        (proj / "session-memory" / "sm.md").write_text("# sm", encoding="utf-8")
        for d in ("tasks/s1", "file-history/s1", "teams/t1/inboxes"):
            (self.home / d).mkdir(parents=True)
        (self.home / "tasks" / "s1" / "1.json").write_text("{}", encoding="utf-8")
        (self.home / "file-history" / "s1" / "snap0").write_text("pre-edit", encoding="utf-8")
        (self.home / "teams" / "t1" / "inboxes" / "m.json").write_text("{}", encoding="utf-8")
        (self.home / "history.jsonl").write_text("{}\n", encoding="utf-8")
        (self.home / "session-env" / "s1").mkdir(parents=True)                     # excluded
        (self.home / "session-env" / "s1" / "env.json").write_text("TOKEN", encoding="utf-8")
        (self.home / "settings.json").write_text("{}", encoding="utf-8")           # excluded
        (self.home / ".credentials.json").write_text("SECRET", encoding="utf-8")   # excluded
        stats = self.sweep(source="claude")
        self.assertEqual((stats["files"], stats["new"]), (10, 10))
        names = self.pool_names()
        for kept in ("agent-x.meta.json", "r1.txt", "sm.md", "1.json", "m.json"):
            self.assertIn(kept, names)
        for excluded in ("snap0", "env.json", "settings.json", ".credentials.json"):
            self.assertNotIn(excluded, names)

    def test_codex_patterns(self):
        (self.home / "sessions" / "2026" / "01" / "02").mkdir(parents=True)
        (self.home / "archived_sessions").mkdir()
        (self.home / "sessions" / "2026" / "01" / "02" / "rollout-a.jsonl").write_text("{}\n", encoding="utf-8")
        (self.home / "archived_sessions" / "old.jsonl").write_text("{}\n", encoding="utf-8")
        (self.home / "session_index.jsonl").write_text("{}\n", encoding="utf-8")
        (self.home / "history.jsonl").write_text("{}\n", encoding="utf-8")
        (self.home / "auth.json").write_text("SECRET", encoding="utf-8")        # excluded
        (self.home / "logs_2.sqlite").write_text("not a log", encoding="utf-8")  # excluded
        stats = self.sweep(source="codex")
        self.assertEqual((stats["files"], stats["new"]), (4, 4))
        names = self.pool_names()
        self.assertIn("rollout-a.jsonl", names)
        self.assertNotIn("auth.json", names)
        self.assertNotIn("logs_2.sqlite", names)


class TestPoolAsIngestRoot(CollectBase):
    def test_pool_subtree_ingests_like_a_live_home(self):
        """DESIGN.md: ingest is agnostic to live-home vs pool — same layout."""
        self.pi_home()
        self.sweep()
        conn = memory_db()
        self.addCleanup(conn.close)
        stats = ingest.refresh(conn, sources=("pi",), machine="t",
                               roots={"pi": self.pool / "raw" / "t" / "pi"})
        self.assertEqual(stats["sessions"], 1)
        n = conn.execute("SELECT COUNT(*) AS c FROM sessions WHERE session_id='pi:P1'").fetchone()["c"]
        self.assertEqual(n, 1)

    def test_pool_round_trip_preserves_origin_machine(self):
        """SCHEMA.md: machine comes from the pool path raw/<machine>/<source>.
        Ingesting another machine's synced subtree — with no machine override —
        must keep its sessions labeled with the ORIGIN machine, not this host."""
        self.pi_home()
        with contextlib.redirect_stdout(io.StringIO()):
            collect.collect_source("pi", raw_root=self.home,
                                   pool_root=self.pool, machine="mini")
        conn = memory_db()
        self.addCleanup(conn.close)
        ingest.refresh(conn, sources=("pi",),
                       roots={"pi": self.pool / "raw" / "mini" / "pi"})
        row = conn.execute(
            "SELECT machine FROM sessions WHERE session_id='pi:P1'").fetchone()
        self.assertEqual(row["machine"], "mini")


class TestMachineForRoot(unittest.TestCase):
    def test_derivation_precedence(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            pool_pi = Path("/x/codebrain-pool/raw/mini/pi")
            self.assertEqual(ingest._machine_for_root("pi", pool_pi, None), "mini")
            self.assertEqual(ingest._machine_for_root("pi", pool_pi, "forced"), "forced")
            # a live tool home (or any non-pool-shaped root) is this machine
            self.assertEqual(ingest._machine_for_root("pi", Path("/home/u/.pi"), None),
                             socket.gethostname())
            self.assertEqual(ingest._machine_for_root("pi", None, None), socket.gethostname())
            # the trailing component must name the SAME source for path derivation
            self.assertEqual(ingest._machine_for_root("claude", pool_pi, None),
                             socket.gethostname())

    def test_live_home_uses_machine_env_alias(self):
        with mock.patch.dict(os.environ, {"CODEBRAIN_MACHINE": "alias"}, clear=True):
            self.assertEqual(ingest._machine_for_root("pi", Path("/home/u/.pi"), None),
                             "alias")


class TestLaunchdPlist(unittest.TestCase):
    def test_plist_rejects_path_machine_names(self):
        with self.assertRaises(ValueError):
            collect._plist_dict(machine="bad/name")
        with mock.patch.dict(os.environ, {"CODEBRAIN_MACHINE": "../bad"}, clear=False):
            with self.assertRaises(ValueError):
                collect._plist_dict()

    def test_plist_round_trips_weird_paths_and_flags(self):
        """plistlib must escape what an f-string template would not — a pool path
        with '&' or '<' previously produced unparseable XML and a hard
        `launchctl bootstrap` failure."""
        import plistlib
        weird = Path("/tmp/we&ird <pool>")
        spec = collect._plist_dict(interval=600, pool_root=weird,
                                   source="codex", machine="mini")
        back = plistlib.loads(plistlib.dumps(spec))   # parse what we'd write
        argv = back["ProgramArguments"]
        self.assertIn(str(weird), argv)
        self.assertEqual(argv[argv.index("--source") + 1], "codex")
        self.assertEqual(argv[argv.index("--machine") + 1], "mini")
        self.assertEqual(back["StartInterval"], 600)
        self.assertEqual(back["Label"], collect.LAUNCHD_LABEL)


if __name__ == "__main__":
    unittest.main()

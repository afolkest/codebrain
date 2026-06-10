"""collect — the append-only pool sweep (DESIGN.md collector): allowlist-only
capture (credentials never leave the home), stat-compare incrementality, the
shrink guard, and the pool-as-ingest-root round trip. Nothing ever deletes
from the pool."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

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


class TestAllowlists(CollectBase):
    def test_claude_patterns(self):
        proj = self.home / "projects" / "-p-"
        (proj / "sid1" / "subagents").mkdir(parents=True)
        (proj / "memory").mkdir()
        (proj / "sid1.jsonl").write_text("{}\n", encoding="utf-8")
        (proj / "sid1" / "subagents" / "agent.jsonl").write_text("{}\n", encoding="utf-8")
        (proj / "sessions-index.json").write_text("{}", encoding="utf-8")
        (proj / "memory" / "MEMORY.md").write_text("# m", encoding="utf-8")
        (self.home / "history.jsonl").write_text("{}\n", encoding="utf-8")
        (self.home / "settings.json").write_text("{}", encoding="utf-8")        # excluded
        (self.home / ".credentials.json").write_text("SECRET", encoding="utf-8")  # excluded
        stats = self.sweep(source="claude")
        self.assertEqual((stats["files"], stats["new"]), (5, 5))
        names = self.pool_names()
        self.assertIn("agent.jsonl", names)
        self.assertIn("MEMORY.md", names)
        self.assertNotIn("settings.json", names)
        self.assertNotIn(".credentials.json", names)

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


if __name__ == "__main__":
    unittest.main()

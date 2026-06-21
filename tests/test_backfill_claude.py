"""Claude historical backfill: sanitized restore -> pool-shaped root -> ingest."""
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from codebrain import ingest
from codebrain.backfill_claude import CopyPlan, MemberRef, _copy_member, backfill
from tests._helpers import memory_db


def _user(uuid, sid, text, ts, parent=None):
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": sid,
        "timestamp": ts,
        "cwd": "/work",
        "message": {"role": "user", "content": text},
    }


def _assistant(uuid, sid, text, ts, parent):
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": sid,
        "timestamp": ts,
        "cwd": "/work",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _jsonl(records):
    return "".join(json.dumps(r) + "\n" for r in records)


def _zip(path: Path, entries: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return path


class TestClaudeBackfill(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.restore = self.root / "restore"
        self.pool = self.root / "pool"
        self.restore.mkdir()

    def test_backfill_selects_latest_archives_sidecars_and_ingests(self):
        old_main = _jsonl([
            _user("u1", "S", "old prompt", "2026-01-01T00:01:00.000Z"),
        ])
        new_main = _jsonl([
            _user("u1", "S", "old prompt", "2026-01-01T00:01:00.000Z"),
            _assistant("a1", "S", "new answer", "2026-01-01T00:02:00.000Z", "u1"),
        ])
        base = "Macintosh HD/Users/example/.claude"
        _zip(self.restore / "old.zip", {
            f"{base}/projects/-proj/S.jsonl": old_main,
            f"{base}/settings.json": "{}",
            f"{base}/shell-snapshots/snapshot.sh": "TOKEN=secret",
            f"{base}/debug/noisy.txt": "debug",
            f"{base}/file-history/S/snap@v1": "old history",
        })
        _zip(self.restore / "new.zip", {
            f"{base}/projects/-proj/S.jsonl": new_main,
            f"{base}/projects/-proj/S/subagents/agent-x.jsonl": "{}\n",
            f"{base}/file-history/S/snap@v2": "new history",
            f"{base}/tasks/S/1.json": "{}",
        })

        manifest = backfill([self.restore], pool_root=self.pool, origin="restore")
        stats = manifest["stats"]
        self.assertEqual(stats["main_candidates"], 2)
        self.assertEqual(stats["selected_sessions"], 1)
        self.assertEqual(stats["duplicate_sessions"], 1)
        self.assertEqual(stats["project_sidecars_planned"], 1)

        dest = self.pool / "raw" / "restore" / "claude"
        self.assertIn("new answer", (dest / "projects" / "-proj" / "S.jsonl").read_text())
        self.assertTrue((dest / "projects" / "-proj" / "S" / "subagents" / "agent-x.jsonl").is_file())
        self.assertFalse((dest / "file-history").exists())
        self.assertFalse((dest / "settings.json").exists())
        self.assertFalse((dest / "shell-snapshots").exists())
        self.assertFalse((dest / "debug").exists())
        self.assertFalse((dest / "tasks").exists())

        loaded = json.loads(Path(manifest["manifest_path"]).read_text())
        self.assertEqual(loaded["sessions"][0]["decision"], "uuid-superset")

        conn = memory_db()
        self.addCleanup(conn.close)
        stats = ingest.refresh(conn, sources=("claude",), roots={"claude": dest})
        self.assertEqual(stats["sessions"], 1)
        row = conn.execute("SELECT machine FROM sessions WHERE session_id='claude:S'").fetchone()
        self.assertEqual(row["machine"], "restore")
        texts = [r["text"] for r in conn.execute("SELECT text FROM events ORDER BY event_id")]
        self.assertIn("new answer", texts)

    def test_dedup_groups_by_structured_session_id_not_filename(self):
        """The backfill groups duplicate candidates by Claude's sessionId field.

        The filenames differ here; using path text would produce two sessions.
        """
        base = "Macintosh HD/Users/example/.claude"
        _zip(self.restore / "a.zip", {
            f"{base}/projects/-old/filename-a.jsonl": _jsonl([
                _user("u1", "REAL", "first", "2026-01-01T00:01:00.000Z"),
            ]),
        })
        _zip(self.restore / "b.zip", {
            f"{base}/projects/-new/filename-b.jsonl": _jsonl([
                _user("u1", "REAL", "first", "2026-01-01T00:01:00.000Z"),
                _assistant("a1", "REAL", "second", "2026-01-01T00:02:00.000Z", "u1"),
            ]),
        })

        manifest = backfill([self.restore], pool_root=self.pool, origin="restore")

        self.assertEqual(manifest["stats"]["selected_sessions"], 1)
        self.assertEqual(manifest["stats"]["duplicate_sessions"], 1)
        selected = manifest["sessions"][0]["selected"]
        self.assertEqual(selected["session_id"], "REAL")
        self.assertEqual(selected["rel"], "projects/-new/filename-b.jsonl")
        dest = self.pool / "raw" / "restore" / "claude"
        self.assertTrue((dest / "projects" / "-new" / "filename-b.jsonl").is_file())
        self.assertFalse((dest / "projects" / "-old" / "filename-a.jsonl").exists())

    def test_legacy_top_level_agent_jsonl_is_retargeted_as_subagent_sidecar(self):
        base = "Macintosh HD/Users/example/.claude"
        _zip(self.restore / "one.zip", {
            f"{base}/projects/-proj/PARENT.jsonl": _jsonl([
                _user("u1", "PARENT", "prompt", "2026-01-01T00:01:00.000Z"),
            ]),
            # Old exports can store subagent transcripts at project root. The
            # structured fields identify them as sidechain records owned by the
            # parent session; they must not be copied as top-level main logs.
            f"{base}/projects/-proj/renamed-sidechain.jsonl": _jsonl([
                {**_user("su1", "PARENT", "subtask", "2026-01-01T00:02:00.000Z"),
                 "isSidechain": True},
            ]),
        })

        manifest = backfill([self.restore], pool_root=self.pool, origin="restore")

        self.assertEqual(manifest["stats"]["main_candidates"], 1)
        self.assertEqual(manifest["stats"]["legacy_subagents_seen"], 1)
        self.assertEqual(manifest["stats"]["legacy_subagents_planned"], 1)
        dest = self.pool / "raw" / "restore" / "claude"
        self.assertTrue(
            (dest / "projects" / "-proj" / "PARENT" / "subagents" / "renamed-sidechain.jsonl").is_file()
        )
        self.assertFalse((dest / "projects" / "-proj" / "renamed-sidechain.jsonl").exists())

    def test_inline_sidechain_does_not_drive_main_winner_selection(self):
        base = "Macintosh HD/Users/example/.claude"
        _zip(self.restore / "old-with-late-sidechain.zip", {
            f"{base}/projects/-proj/S.jsonl": _jsonl([
                _user("u1", "S", "old main", "2026-01-01T00:01:00.000Z"),
                {**_assistant("side", "S", "sidechain only", "2026-01-01T23:59:00.000Z", "u1"),
                 "isSidechain": True},
            ]),
        })
        _zip(self.restore / "new-main.zip", {
            f"{base}/projects/-proj/S.jsonl": _jsonl([
                _user("u1", "S", "old main", "2026-01-01T00:01:00.000Z"),
                _assistant("a1", "S", "new main", "2026-01-01T00:02:00.000Z", "u1"),
            ]),
        })

        manifest = backfill([self.restore], pool_root=self.pool, origin="restore")

        selected = manifest["sessions"][0]["selected"]
        self.assertEqual(Path(selected["archive"]).name, "new-main.zip")
        self.assertEqual(selected["main_max_ts"], "2026-01-01T00:02:00.000Z")
        dest = self.pool / "raw" / "restore" / "claude"
        self.assertIn("new main", (dest / "projects" / "-proj" / "S.jsonl").read_text())

    def test_exact_duplicate_transcript_unions_sidecars_from_other_archives(self):
        base = "Macintosh HD/Users/example/.claude"
        main = _jsonl([
            _user("u1", "S", "prompt", "2026-01-01T00:01:00.000Z"),
        ])
        _zip(self.restore / "a-with-sidecar.zip", {
            f"{base}/projects/-proj/S.jsonl": main,
            f"{base}/projects/-proj/S/subagents/agent-x.jsonl": "{}\n",
        })
        _zip(self.restore / "z-without-sidecar.zip", {
            f"{base}/projects/-proj/S.jsonl": main,
        })

        manifest = backfill([self.restore], pool_root=self.pool, origin="restore")

        self.assertEqual(manifest["sessions"][0]["decision"], "exact-duplicate")
        self.assertEqual(manifest["stats"]["project_sidecar_candidate_refs"], 1)
        self.assertEqual(manifest["stats"]["project_sidecars_planned"], 1)
        dest = self.pool / "raw" / "restore" / "claude"
        self.assertTrue((dest / "projects" / "-proj" / "S" / "subagents" / "agent-x.jsonl").is_file())

    def test_file_history_for_losing_candidate_path_id_is_skipped(self):
        base = "Macintosh HD/Users/example/.claude"
        _zip(self.restore / "old.zip", {
            f"{base}/projects/-old/path-a.jsonl": _jsonl([
                _user("u1", "REAL", "first", "2026-01-01T00:01:00.000Z"),
            ]),
            f"{base}/file-history/path-a/snap@v1": "old path history",
        })
        _zip(self.restore / "new.zip", {
            f"{base}/projects/-new/path-b.jsonl": _jsonl([
                _user("u1", "REAL", "first", "2026-01-01T00:01:00.000Z"),
                _assistant("a1", "REAL", "second", "2026-01-01T00:02:00.000Z", "u1"),
            ]),
        })

        manifest = backfill([self.restore], pool_root=self.pool, origin="restore")

        self.assertNotIn("file_history_planned", manifest["stats"])
        dest = self.pool / "raw" / "restore" / "claude"
        self.assertFalse((dest / "file-history").exists())

    def test_sidecar_destination_collision_is_preserved_outside_ingest_path(self):
        base = "Macintosh HD/Users/example/.claude"
        main = _jsonl([
            _user("u1", "S", "prompt", "2026-01-01T00:01:00.000Z"),
        ])
        _zip(self.restore / "a.zip", {
            f"{base}/projects/-proj/S.jsonl": main,
            f"{base}/projects/-proj/S/subagents/agent-x.jsonl": "older\n",
        })
        _zip(self.restore / "b.zip", {
            f"{base}/projects/-proj/S.jsonl": main,
            f"{base}/projects/-proj/S/subagents/agent-x.jsonl": "newer and larger\n",
        })

        manifest = backfill([self.restore], pool_root=self.pool, origin="restore")

        self.assertEqual(manifest["stats"]["project_sidecar_candidate_refs"], 2)
        self.assertEqual(manifest["stats"]["project_sidecars_planned"], 1)
        self.assertEqual(manifest["stats"]["collision_files_planned"], 1)
        dest = self.pool / "raw" / "restore" / "claude"
        self.assertEqual(
            (dest / "projects" / "-proj" / "S" / "subagents" / "agent-x.jsonl").read_text(),
            "newer and larger\n",
        )
        collisions = list((dest / "_codebrain_backfill_collisions").rglob("agent-x.jsonl.*"))
        self.assertEqual(len(collisions), 1)
        self.assertEqual(collisions[0].read_text(), "older\n")

    def test_file_directory_collision_moves_directory_aside(self):
        base = "Macintosh HD/Users/example/.claude"
        _zip(self.restore / "nested.zip", {
            f"{base}/projects/-proj/S.jsonl": _jsonl([
                _user("u1", "S", "prompt", "2026-01-01T00:01:00.000Z"),
            ]),
            f"{base}/projects/-proj/S/tool-results/r1/nested": "nested",
        })
        backfill([self.restore], pool_root=self.pool, origin="restore")

        (self.restore / "nested.zip").unlink()
        _zip(self.restore / "flat.zip", {
            f"{base}/projects/-proj/S.jsonl": _jsonl([
                _user("u1", "S", "prompt", "2026-01-01T00:01:00.000Z"),
            ]),
            f"{base}/projects/-proj/S/tool-results/r1": "flat",
        })
        manifest = backfill([self.restore], pool_root=self.pool, origin="restore")

        dest = self.pool / "raw" / "restore" / "claude"
        self.assertEqual((dest / "projects" / "-proj" / "S" / "tool-results" / "r1").read_text(), "flat")
        stale_nested = list((dest / "_codebrain_backfill_stale").rglob("r1/nested"))
        self.assertEqual(len(stale_nested), 1)
        self.assertGreaterEqual(manifest["stats"]["stale_moved"], 1)

    def test_rerun_moves_stale_prior_winner_out_of_ingest_path(self):
        base = "Macintosh HD/Users/example/.claude"
        _zip(self.restore / "old.zip", {
            f"{base}/projects/-old/filename-a.jsonl": _jsonl([
                _user("u1", "REAL", "old", "2026-01-01T00:01:00.000Z"),
            ]),
        })
        backfill([self.restore], pool_root=self.pool, origin="restore")

        (self.restore / "old.zip").unlink()
        _zip(self.restore / "new.zip", {
            f"{base}/projects/-new/filename-b.jsonl": _jsonl([
                _user("u1", "REAL", "old", "2026-01-01T00:01:00.000Z"),
                _assistant("a1", "REAL", "new", "2026-01-01T00:02:00.000Z", "u1"),
            ]),
        })
        manifest = backfill([self.restore], pool_root=self.pool, origin="restore")

        self.assertEqual(manifest["stats"]["stale"], 1)
        self.assertEqual(manifest["stats"]["stale_moved"], 1)
        dest = self.pool / "raw" / "restore" / "claude"
        self.assertFalse((dest / "projects" / "-old" / "filename-a.jsonl").exists())
        self.assertTrue((dest / "projects" / "-new" / "filename-b.jsonl").is_file())
        stale = list((dest / "_codebrain_backfill_stale").rglob("filename-a.jsonl"))
        self.assertEqual(len(stale), 1)

        conn = memory_db()
        self.addCleanup(conn.close)
        stats = ingest.refresh(conn, sources=("claude",), roots={"claude": dest})
        self.assertEqual(stats["sessions"], 1)
        texts = [r["text"] for r in conn.execute("SELECT text FROM events ORDER BY event_id")]
        self.assertIn("new", texts)

    def test_copy_failure_leaves_existing_destination_in_place(self):
        dest = self.pool / "raw" / "restore" / "claude"
        existing = dest / "projects" / "-proj" / "S.jsonl"
        existing.parent.mkdir(parents=True)
        existing.write_text("old good\n", encoding="utf-8")

        class BrokenZip:
            def open(self, name, mode="r"):
                raise OSError("boom")

        plan = CopyPlan(
            MemberRef(Path("broken.zip"), "missing", "projects/-proj/S.jsonl", 100, (2026, 1, 1, 0, 0, 0)),
            "main",
        )
        with self.assertRaises(OSError):
            _copy_member(BrokenZip(), plan, dest, False, "run")
        self.assertEqual(existing.read_text(encoding="utf-8"), "old good\n")

    def test_dry_run_does_not_write_pool(self):
        base = "Macintosh HD/Users/example/.claude"
        _zip(self.restore / "one.zip", {
            f"{base}/projects/-proj/S.jsonl": _jsonl([
                _user("u1", "S", "prompt", "2026-01-01T00:01:00.000Z"),
            ]),
        })
        manifest = backfill([self.restore], pool_root=self.pool, origin="restore", dry_run=True)
        self.assertTrue(manifest["dry_run"])
        self.assertEqual(manifest["stats"]["dry_run"], 1)
        self.assertFalse((self.pool / "raw" / "restore" / "claude").exists())


if __name__ == "__main__":
    unittest.main()

"""refresh() — delta ingest keyed on ingest_state, trigger-maintained FTS, and
the old-index migration. This is what makes the DB always-current at read time:
no 'on disk but not ingested yet' window, and never a full rebuild."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from codebrain import db, ingest
from tests._helpers import memory_db, write_jsonl


def _session(sid, ts):
    return {"type": "session", "id": sid, "timestamp": ts, "cwd": "/work"}


def _user(rid, parent, text, ts):
    return {"type": "message", "id": rid, "timestamp": ts, "parentId": parent,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _assistant(rid, parent, text, ts):
    return {"type": "message", "id": rid, "timestamp": ts, "parentId": parent,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class RefreshBase(unittest.TestCase):
    """A pi raw root (root/agent/sessions/<proj>/*.jsonl) + an in-memory DB."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.proj = self.root / "agent" / "sessions" / "proj"
        self.proj.mkdir(parents=True)
        self.conn = memory_db()
        self.addCleanup(self.conn.close)

    def refresh(self):
        return ingest.refresh(self.conn, sources=("pi",), machine="t",
                              roots={"pi": self.root})

    def count(self, table, where="1=1", params=()):
        return self.conn.execute(
            f"SELECT COUNT(*) AS c FROM {table} WHERE {where}", params).fetchone()["c"]


class TestDelta(RefreshBase):
    def test_initial_then_noop(self):
        write_jsonl(self.proj, "0_P1.jsonl", [
            _session("P1", "2026-01-01T00:00:00.000Z"),
            _user("aaaa1111", None, "hi", "2026-01-01T00:01:00.000Z"),
            _assistant("bbbb2222", "aaaa1111", "yo", "2026-01-01T00:02:00.000Z"),
        ])
        stats = self.refresh()
        self.assertEqual((stats["files"], stats["sessions"]), (1, 1))
        self.assertEqual(self.count("events"), 2)
        # unchanged disk -> the next refresh parses NOTHING
        stats = self.refresh()
        self.assertEqual(stats["files"], 0)
        self.assertEqual(self.count("events"), 2)

    def test_grown_file_reparse_flips_liveness(self):
        f = write_jsonl(self.proj, "0_P1.jsonl", [
            _session("P1", "2026-01-01T00:00:00.000Z"),
            _user("aaaa1111", None, "q", "2026-01-01T00:01:00.000Z"),
            _assistant("bbbb2222", "aaaa1111", "first answer", "2026-01-01T00:02:00.000Z"),
        ])
        self.refresh()
        a1 = "pi:bbbb2222:2026-01-01T00:02:00.000Z"
        self.assertEqual(self.count("session_events", "event_id=? AND live=1", (a1,)), 1)

        # the session continues: a rollback fork — a2 re-answers off the user turn
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                _assistant("cccc3333", "aaaa1111", "second answer",
                           "2026-01-01T00:03:00.000Z")) + "\n")
        stats = self.refresh()
        self.assertEqual(stats["files"], 1)   # just the grown file

        a2 = "pi:cccc3333:2026-01-01T00:03:00.000Z"
        # liveness flipped: a1 dead, a2 live; tip moved; no duplicate placements
        self.assertEqual(self.count("session_events", "event_id=? AND live=1", (a1,)), 0)
        self.assertEqual(self.count("session_events", "event_id=? AND live=1", (a2,)), 1)
        self.assertEqual(self.count("session_events", "session_id='pi:P1'"), 3)
        tip = self.conn.execute(
            "SELECT tip_event_id FROM sessions WHERE session_id='pi:P1'").fetchone()
        self.assertEqual(tip["tip_event_id"], a2)

    def test_new_file_parsed_alone(self):
        write_jsonl(self.proj, "0_P1.jsonl", [
            _session("P1", "2026-01-01T00:00:00.000Z"),
            _user("aaaa1111", None, "one", "2026-01-01T00:01:00.000Z"),
        ])
        self.refresh()
        write_jsonl(self.proj, "1_P2.jsonl", [
            _session("P2", "2026-01-02T00:00:00.000Z"),
            _user("dddd4444", None, "two", "2026-01-02T00:01:00.000Z"),
        ])
        stats = self.refresh()
        self.assertEqual((stats["files"], stats["sessions"]), (1, 1))
        self.assertEqual(self.count("sessions"), 2)

    def test_deleted_file_rows_survive(self):
        f = write_jsonl(self.proj, "0_P1.jsonl", [
            _session("P1", "2026-01-01T00:00:00.000Z"),
            _user("aaaa1111", None, "keep me", "2026-01-01T00:01:00.000Z"),
        ])
        self.refresh()
        f.unlink()   # upstream cleanup must never delete from the archive
        stats = self.refresh()
        self.assertEqual(stats["files"], 0)
        self.assertEqual(self.count("sessions", "session_id='pi:P1'"), 1)
        self.assertEqual(self.count("events"), 1)

    def test_contentless_file_not_retried(self):
        p = self.proj / "0_empty.jsonl"
        p.write_text("not json at all\n", encoding="utf-8")
        stats = self.refresh()
        self.assertEqual((stats["files"], stats["skipped"]), (1, 1))
        stats = self.refresh()    # unchanged -> not parsed again
        self.assertEqual(stats["files"], 0)


class TestFtsTriggers(RefreshBase):
    def test_index_stays_current_without_rebuild(self):
        if not db.has_fts5(self.conn):
            self.skipTest("sqlite built without FTS5")
        f = write_jsonl(self.proj, "0_P1.jsonl", [
            _session("P1", "2026-01-01T00:00:00.000Z"),
            _user("aaaa1111", None, "the zanzibar protocol", "2026-01-01T00:01:00.000Z"),
        ])
        self.refresh()

        def hits(q):
            return self.conn.execute(
                "SELECT e.event_id FROM events_fts f JOIN events e ON e.rowid=f.rowid "
                "WHERE events_fts MATCH ?", (q,)).fetchall()

        self.assertEqual(len(hits("zanzibar")), 1)   # indexed by the INSERT trigger
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                _assistant("bbbb2222", "aaaa1111", "engaging xylophone mode",
                           "2026-01-01T00:02:00.000Z")) + "\n")
        self.refresh()                                # no rebuild_fts anywhere
        self.assertEqual(len(hits("xylophone")), 1)

    def test_migration_from_old_standalone_index(self):
        if not db.has_fts5(self.conn):
            self.skipTest("sqlite built without FTS5")
        write_jsonl(self.proj, "0_P1.jsonl", [
            _session("P1", "2026-01-01T00:00:00.000Z"),
            _user("aaaa1111", None, "kumquat festival", "2026-01-01T00:01:00.000Z"),
        ])
        self.refresh()
        # devolve to the pre-trigger shape an existing DB would have
        self.conn.executescript(
            "DROP TRIGGER events_fts_ai; DROP TRIGGER events_fts_ad;"
            "DROP TRIGGER events_fts_au; DROP TABLE events_fts;")
        self.conn.execute("CREATE VIRTUAL TABLE events_fts USING fts5(text, event_id UNINDEXED)")

        with contextlib.redirect_stderr(io.StringIO()) as err:
            db._ensure_fts(self.conn)   # what connect() runs on an old DB
        self.assertIn("rebuilding FTS", err.getvalue())

        rows = self.conn.execute(
            "SELECT e.event_id FROM events_fts f JOIN events e ON e.rowid=f.rowid "
            "WHERE events_fts MATCH 'kumquat'").fetchall()
        self.assertEqual(len(rows), 1)   # old content re-indexed in the new shape


if __name__ == "__main__":
    unittest.main()

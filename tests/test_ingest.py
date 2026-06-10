"""Ingest + DB integration: idempotency, the conflict (F1) and malformed-record
(F4) hardening, the pi cross-file dedup the schema exists for, and FTS."""
import contextlib
import io
import tempfile
import unittest

from codebrain import db, ingest
from codebrain.adapters import codex, pi
from codebrain.adapters.base import EventRow, ParsedSession, PlacementRow, SessionRow
from tests._helpers import memory_db, write_jsonl


def _counts(conn):
    n = lambda t: conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
    return {"events": n("events"), "placements": n("session_events"), "sessions": n("sessions")}


# --- minimal pi records for the cross-file case ---
def _pi_session(sid, ts, parent=None):
    r = {"type": "session", "id": sid, "timestamp": ts, "cwd": "/work"}
    if parent:
        r["parentSession"] = parent
    return r


def _pi_user(rid, parent, text, ts):
    return {"type": "message", "id": rid, "timestamp": ts, "parentId": parent,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _pi_assistant(rid, parent, text, ts):
    return {"type": "message", "id": rid, "timestamp": ts, "parentId": parent,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class TestIdempotency(unittest.TestCase):
    def test_reingest_is_a_noop(self):
        d = tempfile.mkdtemp()
        f = write_jsonl(d, "0_P.jsonl", [
            _pi_session("P1", "2026-01-01T00:00:00.000Z"),
            _pi_user("aaaa1111", None, "hi", "2026-01-01T00:01:00.000Z"),
            _pi_assistant("bbbb2222", "aaaa1111", "yo", "2026-01-01T00:02:00.000Z"),
        ])
        conn = memory_db()
        self.addCleanup(conn.close)
        parse = lambda p: pi.parse_file(p, machine="t")
        ingest._ingest(conn, [f], parse)
        first = _counts(conn)
        ingest._ingest(conn, [f], parse)          # ingest the exact same file again
        self.assertEqual(_counts(conn), first)    # no new rows, no duplicates


class TestConflictSkip(unittest.TestCase):
    def _session(self, sid, eid, text):
        s = SessionRow(session_id=sid, source="test", tip_event_id=eid)
        e = EventRow(event_id=eid, origin_session_id=sid, ts="t", actor="user", type="message",
                     text=text, refs={"files": [], "commands": []}, raw={})
        p = [PlacementRow(session_id=sid, event_id=eid, seq=0, parent_event_id=None, live=1, inherited=0)]
        return ParsedSession(session=s, events=[e], placements=p)

    def test_conflict_skips_only_that_placement(self):
        # two sessions reuse one event_id with DIFFERENT content -> copy-consistency conflict
        from pathlib import Path
        d = Path(tempfile.mkdtemp())          # real paths: _ingest stats before parsing
        (d / "A").touch()
        (d / "B").touch()
        sess = {d / "A": self._session("test:A", "ev:1", "content A"),
                d / "B": self._session("test:B", "ev:1", "content B DIFFERENT")}
        conn = memory_db()
        self.addCleanup(conn.close)
        with contextlib.redirect_stdout(io.StringIO()):   # the conflict line is expected
            stats = ingest._ingest(conn, list(sess), lambda p: sess[p])

        self.assertEqual(stats["conflicts"], 1)
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["sessions"], 2)            # both sessions still commit
        # first content + origin kept
        row = conn.execute("SELECT text, origin_session_id FROM events WHERE event_id='ev:1'").fetchone()
        self.assertEqual(row["text"], "content A")
        self.assertEqual(row["origin_session_id"], "test:A")
        # B's conflicted placement is skipped; A's is present
        b = conn.execute("SELECT COUNT(*) AS c FROM session_events WHERE session_id='test:B'").fetchone()["c"]
        a = conn.execute("SELECT COUNT(*) AS c FROM session_events WHERE session_id='test:A'").fetchone()["c"]
        self.assertEqual(b, 0)
        self.assertEqual(a, 1)


class TestMalformedRecords(unittest.TestCase):
    def test_bad_payload_does_not_sink_the_file(self):
        d = tempfile.mkdtemp()
        f = write_jsonl(d, "rollout.jsonl", [
            {"type": "session_meta", "timestamp": "2026-01-01T00:00:00Z",
             "payload": {"id": "0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "cwd": "/w",
                         "timestamp": "2026-01-01T00:00:00Z"}},
            {"type": "event_msg", "timestamp": "2026-01-01T00:00:01Z", "payload": "BAD STRING"},
            {"type": "response_item", "timestamp": "2026-01-01T00:00:02Z", "payload": ["also", "bad"]},
            {"type": "event_msg", "timestamp": "2026-01-01T00:00:03Z",
             "payload": {"type": "user_message", "message": "hello world"}},
        ])
        conn = memory_db()
        self.addCleanup(conn.close)
        stats = ingest._ingest(conn, [f], lambda p: codex.parse_file(p, machine="t"))
        self.assertEqual(stats["errors"], 0)              # the file was NOT dropped
        self.assertEqual(stats["sessions"], 1)
        texts = [r["text"] for r in conn.execute("SELECT text FROM events")]
        self.assertIn("hello world", texts)               # the good record survived


class TestPiCrossFileDedup(unittest.TestCase):
    """The case the three-table schema exists for: a resume/branch copies the
    parent's live prefix verbatim, so a shared event is ONE row with N placements."""

    def _files(self):
        d = tempfile.mkdtemp()
        parent_name = "2026-01-01T00-00-00-000Z_PARENT.jsonl"
        parent = write_jsonl(d, parent_name, [
            _pi_session("PARENT", "2026-01-01T00:00:00.000Z"),
            _pi_user("aaaa1111", None, "shared", "2026-01-01T00:01:00.000Z"),
            _pi_assistant("bbbb2222", "aaaa1111", "shared answer", "2026-01-01T00:02:00.000Z"),
        ])
        child = write_jsonl(d, "2026-01-01T00-05-00-000Z_CHILD.jsonl", [
            _pi_session("CHILD", "2026-01-01T00:05:00.000Z", parent=str(parent)),
            _pi_user("aaaa1111", None, "shared", "2026-01-01T00:01:00.000Z"),            # copied
            _pi_assistant("bbbb2222", "aaaa1111", "shared answer", "2026-01-01T00:02:00.000Z"),  # copied
            _pi_user("dddd4444", "bbbb2222", "new", "2026-01-01T00:06:00.000Z"),         # authored
            _pi_assistant("eeee5555", "dddd4444", "new answer", "2026-01-01T00:07:00.000Z"),     # authored
        ])
        return parent, child

    def _check(self, conn):
        # 4 distinct events (the 2 shared rows are NOT duplicated)
        self.assertEqual(_counts(conn)["events"], 4)
        shared = "pi:aaaa1111:2026-01-01T00:01:00.000Z"
        places = conn.execute(
            "SELECT session_id, inherited FROM session_events WHERE event_id=? ORDER BY session_id",
            (shared,)).fetchall()
        self.assertEqual([(r["session_id"], r["inherited"]) for r in places],
                         [("pi:CHILD", 1), ("pi:PARENT", 0)])   # origin inherited=0, copy inherited=1
        # origin is the authoring (parent) session, regardless of ingest order
        origin = conn.execute("SELECT origin_session_id FROM events WHERE event_id=?", (shared,)).fetchone()
        self.assertEqual(origin["origin_session_id"], "pi:PARENT")

    def test_parent_then_child(self):
        parent, child = self._files()
        conn = memory_db()
        self.addCleanup(conn.close)
        ingest._ingest(conn, [parent, child], lambda p: pi.parse_file(p, machine="t"))
        self._check(conn)

    def test_child_then_parent_same_origin(self):
        parent, child = self._files()
        conn = memory_db()
        self.addCleanup(conn.close)
        ingest._ingest(conn, [child, parent], lambda p: pi.parse_file(p, machine="t"))
        self._check(conn)   # COALESCE(first non-null) makes origin order-independent


class TestFts(unittest.TestCase):
    def test_search_finds_event_text(self):
        d = tempfile.mkdtemp()
        f = write_jsonl(d, "0_P.jsonl", [
            _pi_session("P1", "2026-01-01T00:00:00.000Z"),
            _pi_user("aaaa1111", None, "the quicksilver fox", "2026-01-01T00:01:00.000Z"),
        ])
        conn = memory_db()
        self.addCleanup(conn.close)
        if not db.has_fts5(conn):
            self.skipTest("sqlite built without FTS5")
        ingest._ingest(conn, [f], lambda p: pi.parse_file(p, machine="t"))
        db.rebuild_fts(conn)   # repair path; the triggers already indexed it
        hits = conn.execute(
            "SELECT e.event_id FROM events_fts f JOIN events e ON e.rowid = f.rowid "
            "WHERE events_fts MATCH 'quicksilver'").fetchall()
        self.assertEqual(len(hits), 1)


if __name__ == "__main__":
    unittest.main()

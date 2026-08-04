"""Ingest + DB integration: idempotency, the conflict (F1) and malformed-record
(F4) hardening, the pi cross-file dedup the schema exists for, and FTS."""
import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace

from codebrain import db, ingest
from codebrain.adapters import codex, pi
from codebrain.adapters.base import (
    EventRow,
    ParsedSession,
    PlacementRow,
    SessionRow,
    SourceHead,
)
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


class TestFileRefsIndex(unittest.TestCase):
    """The files index (db.file_refs) is the structured signal `touched` reads
    instead of scanning events.refs JSON. Prove ingest/backfill populate it with
    the same basename normalization the query side uses."""

    def _event_with_files(self, conn, eid, files):
        return db.upsert_event(conn, EventRow(
            event_id=eid, origin_session_id="pi:S", ts="2026-01-01T00:00:00Z",
            actor="assistant", type="tool_call", text="edit",
            refs={"files": files, "commands": []}, raw={}))

    def test_upsert_event_unrolls_files_with_normalized_basename(self):
        conn = memory_db()
        self.addCleanup(conn.close)
        self._event_with_files(conn, "pi:e1",
                               ["docs/wip/plan.md", "/abs/repo/plan.md", "~/notes.md"])
        got = {r["file"]: r["basename"] for r in conn.execute(
            "SELECT file, basename FROM file_refs WHERE event_id='pi:e1'")}
        self.assertEqual(got["docs/wip/plan.md"], "plan.md")
        self.assertEqual(got["/abs/repo/plan.md"], "plan.md")
        self.assertEqual(got["~/notes.md"], "notes.md")  # ~ expands, basename stable

    def test_no_rows_for_events_without_files_and_dedup_is_idempotent(self):
        conn = memory_db()
        self.addCleanup(conn.close)
        db.upsert_event(conn, EventRow(
            event_id="pi:msg", origin_session_id="pi:S", ts="2026-01-01T00:00:00Z",
            actor="user", type="message", text="hi",
            refs={"files": [], "commands": []}, raw={}))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM file_refs").fetchone()[0], 0)
        self._event_with_files(conn, "pi:e1", ["a.md", "a.md"])  # dup within the event
        self._event_with_files(conn, "pi:e1", ["a.md"])          # re-upsert same event
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM file_refs WHERE event_id='pi:e1'").fetchone()[0],
            1)

    def test_file_refs_resync_when_an_existing_events_refs_change(self):
        # Codex enriches a tool_call's refs.files from a later patch_apply_end record;
        # on a refresh that re-parses the grown log, the same event_id is upserted with
        # MORE (or fewer) files. The files index must track that, not freeze on insert.
        conn = memory_db()
        self.addCleanup(conn.close)
        files_now = lambda: {r["file"] for r in conn.execute(
            "SELECT file FROM file_refs WHERE event_id='codex:e1'")}
        self._event_with_files(conn, "codex:e1", ["a.py"])
        self.assertEqual(files_now(), {"a.py"})
        self._event_with_files(conn, "codex:e1", ["a.py", "/abs/b.py"])  # enriched later
        self.assertEqual(files_now(), {"a.py", "/abs/b.py"})
        self._event_with_files(conn, "codex:e1", ["/abs/b.py"])          # and a drop
        self.assertEqual(files_now(), {"/abs/b.py"})

    def test_ensure_file_refs_backfills_a_preindex_cache(self):
        conn = memory_db()
        self.addCleanup(conn.close)
        # an event written the way a pre-files-index cache holds it (no file_refs row)
        conn.execute(
            "INSERT INTO events (event_id, ts, actor, type, text, refs, raw) "
            "VALUES (?,?,?,?,?,?,?)",
            ("pi:old", "2026-01-01T00:00:00Z", "assistant", "tool_call", "edit",
             json.dumps({"files": ["legacy/file.py"], "commands": []}), "{}"))
        conn.execute("DELETE FROM file_refs")
        db._ensure_file_refs(conn)                       # the one-time migration
        row = conn.execute(
            "SELECT file, basename FROM file_refs WHERE event_id='pi:old'").fetchone()
        self.assertEqual((row["file"], row["basename"]), ("legacy/file.py", "file.py"))

    def test_file_refs_keyed_on_event_not_placement(self):
        # pi cross-file dedup: one events row, N placements. file_refs is keyed on
        # event_id (content), so an inherited copy must NOT duplicate the file rows.
        conn = memory_db()
        self.addCleanup(conn.close)
        db.upsert_event(conn, EventRow(
            event_id="pi:shared", origin_session_id="pi:A", ts="2026-01-01T00:00:00Z",
            actor="assistant", type="tool_call", text="edit",
            refs={"files": ["shared.py"], "commands": []}, raw={}))
        db.upsert_placement(conn, PlacementRow(session_id="pi:A", event_id="pi:shared",
            seq=5, parent_event_id=None, live=1, inherited=0))
        db.upsert_placement(conn, PlacementRow(session_id="pi:B", event_id="pi:shared",
            seq=5, parent_event_id=None, live=1, inherited=1))  # resumed verbatim copy
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM file_refs WHERE event_id='pi:shared'").fetchone()[0], 1)

    def test_ensure_file_refs_backfill_dedups_multiple_files_and_skips_no_file_events(self):
        conn = memory_db()
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO events (event_id, ts, actor, type, text, refs, raw) "
            "VALUES (?,?,?,?,?,?,?)",
            ("pi:old", "2026-01-01T00:00:00Z", "assistant", "tool_call", "edit",
             json.dumps({"files": ["legacy/a.py", "/abs/b.py", "legacy/a.py"],
                         "commands": []}), "{}"))
        conn.execute(
            "INSERT INTO events (event_id, ts, actor, type, text, refs, raw) "
            "VALUES (?,?,?,?,?,?,?)",
            ("pi:msg", "2026-01-01T00:00:01Z", "user", "message", "hi",
             json.dumps({"files": [], "commands": []}), "{}"))
        conn.execute("DELETE FROM file_refs")
        db._ensure_file_refs(conn)
        got = {(r["event_id"], r["file"], r["basename"]) for r in conn.execute(
            "SELECT event_id, file, basename FROM file_refs")}
        self.assertEqual(got, {
            ("pi:old", "legacy/a.py", "a.py"),
            ("pi:old", "/abs/b.py", "b.py"),
        })

    def test_ensure_stats_skips_small_caches(self):
        conn = memory_db()
        self.addCleanup(conn.close)
        db.upsert_event(conn, EventRow(
            event_id="x:1", origin_session_id="x:S", ts="2026-01-01T00:00:00Z",
            actor="user", type="message", text="hi",
            refs={"files": [], "commands": []}, raw={}))
        db._ensure_stats(conn)  # below the 5000-event threshold: must not ANALYZE
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'").fetchone())


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


class TestCursorRevisionAuthority(unittest.TestCase):
    def _event(self, **changes):
        values = {
            "event_id": "cursor:bubble:1767225601000:call",
            "origin_session_id": "cursor:AUTHOR",
            "ts": "2026-01-01T00:00:01.000Z",
            "actor": "assistant",
            "type": "tool_call",
            "text": "read_file_v2: oldquartz.py",
            "refs": {"files": ["oldquartz.py"], "commands": []},
            "raw": {"version": "old", "params": {"targetFile": "oldquartz.py"}},
            "tool_call_event_id": None,
        }
        values.update(changes)
        return EventRow(**values)

    def _stored(self, conn, event_id="cursor:bubble:1767225601000:call"):
        row = conn.execute(
            "SELECT * FROM events WHERE event_id=?", (event_id,)
        ).fetchone()
        return {
            "origin": row["origin_session_id"],
            "ts": row["ts"],
            "actor": row["actor"],
            "type": row["type"],
            "text": row["text"],
            "refs": json.loads(row["refs"]),
            "call": row["tool_call_event_id"],
            "raw": json.loads(row["raw"]),
        }

    def test_cursor_head_rank_is_total_and_monotonic(self):
        conn = memory_db()
        self.addCleanup(conn.close)
        sid = "cursor:S"
        first = SourceHead(1, "a" * 64)
        equal = SourceHead(1, "a" * 64)
        tie_winner = SourceHead(1, "b" * 64)
        next_revision = SourceHead(2, "0" * 64)

        self.assertTrue(db.cursor_head_is_newer(conn, sid, first))
        self.assertTrue(db.record_cursor_head(conn, sid, first))
        self.assertFalse(db.cursor_head_is_newer(conn, sid, equal))
        self.assertFalse(db.record_cursor_head(conn, sid, SourceHead(1, "0" * 64)))
        self.assertTrue(db.cursor_head_is_newer(conn, sid, tie_winner))
        self.assertTrue(db.record_cursor_head(conn, sid, tie_winner))
        self.assertTrue(db.cursor_head_is_newer(conn, sid, next_revision))
        self.assertTrue(db.record_cursor_head(conn, sid, next_revision))
        row = conn.execute(
            "SELECT revision, digest FROM cursor_session_heads WHERE session_id=?",
            (sid,),
        ).fetchone()
        self.assertEqual((row["revision"], row["digest"]), (2, "0" * 64))
        for invalid in (
            SourceHead(0, "a" * 64),
            SourceHead(1, "short"),
            SourceHead(1, "A" * 64),
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    db.cursor_head_is_newer(conn, sid, invalid)

    def test_structural_mismatches_are_hard_conflicts(self):
        original = self._event()
        changes = {
            "ts": "2026-01-01T00:00:02.000Z",
            "actor": "user",
            "type": "message",
            "tool_call_event_id": "cursor:other:1767225601000:call",
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                conn = memory_db()
                try:
                    self.assertTrue(db.upsert_cursor_event(conn, original))
                    self.assertFalse(db.upsert_cursor_event(
                        conn, replace(original, **{field: value})
                    ))
                    stored_key = "call" if field == "tool_call_event_id" else field
                    self.assertEqual(
                        self._stored(conn)[stored_key], getattr(original, field)
                    )
                finally:
                    conn.close()

    def test_authored_replaces_inherited_and_inherited_cannot_regress_it(self):
        conn = memory_db()
        self.addCleanup(conn.close)
        inherited = self._event(origin_session_id=None, text="provisional",
                                refs={"files": ["provisional.py"], "commands": []},
                                raw={"version": "provisional"})
        authored = self._event(text="authoritative",
                               refs={"files": ["authoritative.py"], "commands": []},
                               raw={"version": "authoritative"})
        later_copy = self._event(origin_session_id=None, text="stale copy",
                                 refs={"files": ["stale.py"], "commands": []},
                                 raw={"version": "stale"})

        self.assertTrue(db.upsert_cursor_event(conn, inherited))
        self.assertTrue(db.upsert_cursor_event(conn, authored))
        self.assertTrue(db.upsert_cursor_event(conn, later_copy))
        stored = self._stored(conn)
        self.assertEqual(stored["origin"], "cursor:AUTHOR")
        self.assertEqual(stored["text"], "authoritative")
        self.assertEqual(stored["refs"]["files"], ["authoritative.py"])
        self.assertEqual(stored["raw"], {"version": "authoritative"})

    def test_newer_same_origin_updates_content_refs_raw_fts_and_pairing(self):
        conn = memory_db()
        self.addCleanup(conn.close)
        have_fts = db.has_fts5(conn)

        original = self._event()
        revised = self._event(
            text="read_file_v2: newzircon.py",
            refs={"files": ["newzircon.py"], "commands": []},
            raw={"version": "new", "params": {"targetFile": "newzircon.py"}},
        )
        call_id = original.event_id
        result_id = "cursor:bubble:1767225601000:result"
        old_result = self._event(
            event_id=result_id, actor="tool", type="tool_result",
            text="oldquartz result", refs={"files": [], "commands": []},
            raw={"result": "oldquartz result"}, tool_call_event_id=call_id,
        )
        new_result = replace(
            old_result, text="newzircon result", raw={"result": "newzircon result"}
        )
        message_id = "cursor:message:1767225600000:message"
        old_message = self._event(
            event_id=message_id, actor="user", type="message",
            text="oldquartz prompt", refs={"files": [], "commands": []},
            raw={"text": "oldquartz prompt"},
        )
        new_message = replace(
            old_message, text="newzircon prompt", raw={"text": "newzircon prompt"}
        )

        self.assertTrue(db.upsert_cursor_event(conn, original))
        self.assertTrue(db.upsert_cursor_event(conn, old_result))
        self.assertTrue(db.upsert_cursor_event(conn, old_message))
        self.assertTrue(db.upsert_cursor_event(conn, revised))
        self.assertTrue(db.upsert_cursor_event(conn, new_result))
        self.assertTrue(db.upsert_cursor_event(conn, new_message))

        self.assertEqual(self._stored(conn)["text"], "read_file_v2: newzircon.py")
        result = self._stored(conn, result_id)
        self.assertEqual(result["text"], "newzircon result")
        self.assertEqual(result["call"], call_id)
        self.assertEqual(self._stored(conn, message_id)["text"], "newzircon prompt")
        files = {row["file"] for row in conn.execute(
            "SELECT file FROM file_refs WHERE event_id=?", (call_id,)
        )}
        self.assertEqual(files, {"newzircon.py"})
        if have_fts:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'oldquartz'"
            ).fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'newzircon'"
            ).fetchone()[0], 3)

    def test_inherited_only_variants_converge_independently_of_order(self):
        variants = (
            self._event(origin_session_id=None, text="variant alpha",
                        refs={"files": ["alpha.py"], "commands": []},
                        raw={"variant": "alpha"}),
            self._event(origin_session_id=None, text="variant beta",
                        refs={"files": ["beta.py"], "commands": []},
                        raw={"variant": "beta"}),
        )
        outcomes = []
        for ordered in (variants, tuple(reversed(variants))):
            conn = memory_db()
            try:
                self.assertTrue(db.upsert_cursor_event(conn, ordered[0]))
                self.assertTrue(db.upsert_cursor_event(conn, ordered[1]))
                outcomes.append(self._stored(conn))
            finally:
                conn.close()
        self.assertEqual(outcomes[0], outcomes[1])

    def test_distinct_authored_origins_conflict_even_when_content_matches(self):
        for upsert in (db.upsert_event, db.upsert_cursor_event):
            with self.subTest(upsert=upsert.__name__):
                conn = memory_db()
                try:
                    original = self._event()
                    duplicate_author = replace(
                        original, origin_session_id="cursor:OTHER"
                    )
                    self.assertTrue(upsert(conn, original))
                    self.assertFalse(upsert(conn, duplicate_author))
                    self.assertEqual(self._stored(conn)["origin"], "cursor:AUTHOR")
                finally:
                    conn.close()


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

"""Physical no-op guarantees: re-presenting bytes the DB already holds must not
write. Refresh re-parses grown append-only logs in full, so almost every upsert
in a refresh is a byte-identical re-present; before these guards each one
rewrote the row and churned events_fts (~100x the cost of the comparison, and
the dominant term in multi-minute read-path refreshes on a large cache)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codebrain import db, ingest
from codebrain.adapters import pi
from codebrain.adapters.base import EventRow, ParsedSession, PlacementRow, SessionRow
from tests._helpers import memory_db, write_jsonl


class _TmpDirMixin:
    def _tmpdir(self) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name


def _event(**overrides) -> EventRow:
    base = dict(
        event_id="pi:e1", origin_session_id="pi:S", ts="2026-01-01T00:00:00Z",
        actor="user", type="message", text="hello world",
        refs={"files": [], "commands": []}, raw={"k": "v"},
    )
    base.update(overrides)
    return EventRow(**base)


def _session(**overrides) -> SessionRow:
    base = dict(session_id="pi:S", source="pi", machine="t", cwd="/w")
    base.update(overrides)
    return SessionRow(**base)


class TestEventNoopUpsert(unittest.TestCase):
    def test_identical_reupsert_writes_nothing(self):
        conn = memory_db()
        self.addCleanup(conn.close)
        self.assertTrue(db.upsert_event(conn, _event()))
        before = conn.total_changes
        self.assertTrue(db.upsert_event(conn, _event()))  # accepted, but no write
        self.assertEqual(conn.total_changes, before)

    def test_origin_promotion_still_writes(self):
        conn = memory_db()
        self.addCleanup(conn.close)
        db.upsert_event(conn, _event(origin_session_id=None))  # provisional copy
        before = conn.total_changes
        self.assertTrue(db.upsert_event(conn, _event(origin_session_id="pi:S")))
        self.assertGreater(conn.total_changes, before)  # promotion must be recorded
        row = conn.execute("SELECT origin_session_id FROM events WHERE event_id='pi:e1'").fetchone()
        self.assertEqual(row["origin_session_id"], "pi:S")

    def test_inherited_copy_after_authored_is_noop(self):
        conn = memory_db()
        self.addCleanup(conn.close)
        db.upsert_event(conn, _event(origin_session_id="pi:S"))
        before = conn.total_changes
        # A verbatim inherited copy (origin unknown to the copier) re-presents the
        # same bytes; the stored authored origin must survive without a write.
        self.assertTrue(db.upsert_event(conn, _event(origin_session_id=None)))
        self.assertEqual(conn.total_changes, before)
        row = conn.execute("SELECT origin_session_id FROM events WHERE event_id='pi:e1'").fetchone()
        self.assertEqual(row["origin_session_id"], "pi:S")

    def test_changed_refs_still_write_and_resync_file_refs(self):
        conn = memory_db()
        self.addCleanup(conn.close)
        db.upsert_event(conn, _event(actor="assistant", type="tool_call",
                                     refs={"files": ["a.py"], "commands": []}))
        self.assertTrue(db.upsert_event(conn, _event(
            actor="assistant", type="tool_call",
            refs={"files": ["a.py", "b.py"], "commands": []})))
        got = {r["file"] for r in conn.execute(
            "SELECT file FROM file_refs WHERE event_id='pi:e1'")}
        self.assertEqual(got, {"a.py", "b.py"})

    def test_each_mutable_event_field_change_is_written_alone(self):
        # Drift guard for the compare in _stored_event_matches: a field named in
        # the upsert's SET list but missing from the compare would make changes
        # to it silently skipped. One field changes per subtest, so a compare
        # that ignores any single mutable field fails here. (actor/type/text
        # changes are the conflict path, covered elsewhere.)
        import json as _json
        cases = [
            ("ts", "2027-06-01T00:00:00Z"),
            ("refs", {"files": ["z.py"], "commands": []}),
            ("raw", {"k": "v2"}),
            ("tool_call_event_id", "pi:tc9"),
        ]
        for field, new in cases:
            with self.subTest(field=field):
                conn = memory_db()
                db.upsert_event(conn, _event())
                self.assertTrue(db.upsert_event(conn, _event(**{field: new})))
                row = conn.execute(
                    "SELECT ts, refs, raw, tool_call_event_id FROM events "
                    "WHERE event_id='pi:e1'").fetchone()
                stored = row[field]
                if field in ("refs", "raw"):
                    stored = _json.loads(stored)
                self.assertEqual(stored, new)
                conn.close()


class TestSessionNoopUpsert(unittest.TestCase):
    def test_identical_reupsert_writes_nothing(self):
        conn = memory_db()
        self.addCleanup(conn.close)
        db.upsert_session(conn, _session())
        before = conn.total_changes
        db.upsert_session(conn, _session())
        self.assertEqual(conn.total_changes, before)

    def test_changed_field_still_writes(self):
        conn = memory_db()
        self.addCleanup(conn.close)
        db.upsert_session(conn, _session())
        db.upsert_session(conn, _session(title="named later"))
        row = conn.execute("SELECT title FROM sessions WHERE session_id='pi:S'").fetchone()
        self.assertEqual(row["title"], "named later")

    def test_skip_preserves_visibility_columns(self):
        conn = memory_db()
        self.addCleanup(conn.close)
        db.upsert_session(conn, _session())
        conn.execute("UPDATE sessions SET hidden_at='2026-01-01T00:00:00Z', "
                     "hidden_reason='noise' WHERE session_id='pi:S'")
        db.upsert_session(conn, _session())  # unchanged canonical fields: no write
        row = conn.execute("SELECT hidden_at, hidden_reason FROM sessions "
                           "WHERE session_id='pi:S'").fetchone()
        self.assertEqual((row["hidden_at"], row["hidden_reason"]),
                         ("2026-01-01T00:00:00Z", "noise"))

    def test_each_session_field_change_is_written_alone(self):
        # Drift guard: every non-key SessionRow field must be covered by the
        # compare in upsert_session — a field in the UPDATE's SET list but not
        # the compare would make its changes silently skipped. One field per
        # subtest, so a compare missing any single field fails here.
        cases = [
            ("source", "codex"), ("machine", "m2"), ("cwd", "/elsewhere"),
            ("repo", "some-repo"), ("created_at", "2027-01-01T00:00:00Z"),
            ("started_at", "2027-01-01T00:00:01Z"),
            ("ended_at", "2027-01-01T00:00:02Z"),
            ("parent_session_id", "pi:PARENT"), ("relation", "subagent"),
            ("spawn_event_id", "pi:spawn"), ("branch_point_event_id", "pi:bp"),
            ("tip_event_id", "pi:tip"), ("title", "renamed"),
        ]
        self.assertEqual(  # every non-key SessionRow field appears above
            {f for f, _ in cases},
            {f for f in SessionRow.__dataclass_fields__ if f != "session_id"})
        for field, new in cases:
            with self.subTest(field=field):
                conn = memory_db()
                db.upsert_session(conn, _session())
                db.upsert_session(conn, _session(**{field: new}))
                row = conn.execute(
                    f"SELECT {field} FROM sessions WHERE session_id='pi:S'"
                ).fetchone()
                self.assertEqual(row[0], new)
                conn.close()


class TestFtsTriggerScope(_TmpDirMixin, unittest.TestCase):
    def _integrity_ok(self, conn) -> bool:
        try:
            conn.execute("INSERT INTO events_fts(events_fts, rank) "
                         "VALUES ('integrity-check', 1)")
            return True
        except Exception:  # noqa: BLE001 — corruption signal, any form
            return False

    def test_raw_only_update_keeps_fts_consistent(self):
        conn = memory_db()
        self.addCleanup(conn.close)
        db.upsert_event(conn, _event())
        self.assertTrue(db.upsert_event(conn, _event(raw={"k": "v2"})))
        hits = conn.execute("SELECT rowid FROM events_fts WHERE events_fts MATCH 'hello'").fetchall()
        self.assertEqual(len(hits), 1)
        self.assertTrue(self._integrity_ok(conn))

    def test_text_update_reindexes(self):
        conn = memory_db()
        self.addCleanup(conn.close)
        db.upsert_event(conn, _event())
        conn.execute("UPDATE events SET text='goodbye moon' WHERE event_id='pi:e1'")
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'hello'").fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'goodbye'").fetchone()[0], 1)
        self.assertTrue(self._integrity_ok(conn))

    def test_unscoped_update_trigger_is_migrated(self):
        d = self._tmpdir()
        path = Path(d) / "cache.db"
        conn = db.connect(path)
        db.upsert_event(conn, _event())
        # Recreate the pre-scoping trigger shape an existing cache carries.
        conn.executescript("""
            DROP TRIGGER events_fts_au;
            CREATE TRIGGER events_fts_au AFTER UPDATE ON events BEGIN
              INSERT INTO events_fts(events_fts, rowid, text) VALUES('delete', old.rowid, old.text);
              INSERT INTO events_fts(rowid, text) VALUES (new.rowid, new.text);
            END;
        """)
        conn.commit()
        conn.close()
        conn = db.connect(path)  # reopen: migration must replace the trigger
        self.addCleanup(conn.close)
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger' "
                           "AND name='events_fts_au'").fetchone()["sql"]
        self.assertIn("UPDATE OF text", sql)
        self.assertIn("old.text IS NOT new.text", sql)
        conn.execute("UPDATE events SET text='migrated text' WHERE event_id='pi:e1'")
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'migrated'").fetchone()[0], 1)
        self.assertTrue(self._integrity_ok(conn))


class TestPlacementDiff(_TmpDirMixin, unittest.TestCase):
    def _pi_file(self, d, texts):
        records = [{"type": "session", "id": "P1",
                    "timestamp": "2026-01-01T00:00:00.000Z", "cwd": "/w"}]
        parent = None
        for i, t in enumerate(texts):
            rid = f"aaaa{i:04d}"
            records.append({"type": "message", "id": rid,
                            "timestamp": f"2026-01-01T00:0{i + 1}:00.000Z",
                            "parentId": parent,
                            "message": {"role": "user",
                                        "content": [{"type": "text", "text": t}]}})
            parent = rid
        return write_jsonl(d, "0_P.jsonl", records)

    def test_reparse_of_unchanged_file_is_a_physical_noop(self):
        d = self._tmpdir()
        f = self._pi_file(d, ["one", "two", "three"])
        conn = memory_db()
        self.addCleanup(conn.close)
        parse = lambda p: pi.parse_file(p, machine="t")
        ingest._ingest(conn, [f], parse)
        before = conn.total_changes
        os.utime(f)  # force refresh() semantics: stat changed, bytes identical
        ingest._ingest(conn, [f], parse)
        # Only the per-file ingest_state bookkeeping row may be written.
        self.assertLessEqual(conn.total_changes - before, 2)

    def test_stale_placements_are_dropped_on_shrunk_reparse(self):
        d = self._tmpdir()
        f = self._pi_file(d, ["one", "two", "three"])
        conn = memory_db()
        self.addCleanup(conn.close)
        parse = lambda p: pi.parse_file(p, machine="t")
        ingest._ingest(conn, [f], parse)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM session_events WHERE session_id='pi:P1'").fetchone()[0], 3)
        self._pi_file(d, ["one"])  # rewritten shorter in place
        ingest._ingest(conn, [f], parse)
        rows = conn.execute(
            "SELECT event_id FROM session_events WHERE session_id='pi:P1'").fetchall()
        self.assertEqual(len(rows), 1)

    def test_each_placement_field_change_is_written_alone(self):
        # The diff compares the full placement tuple; one field changes per
        # subtest, so a diff comparing only a subset of the tuple fails here.
        d = self._tmpdir()
        f = Path(d) / "raw.jsonl"
        f.write_text("{}\n")

        def parsed(placements):
            return ParsedSession(
                session=SessionRow(session_id="pi:S", source="pi", machine="t"),
                events=[
                    _event(event_id="pi:e1", origin_session_id="pi:S"),
                    _event(event_id="pi:e2", origin_session_id="pi:S"),
                ],
                placements=placements,
            )

        base = dict(session_id="pi:S", event_id="pi:e2", seq=1,
                    parent_event_id="pi:e1", live=1, inherited=0)
        for field, new in (("seq", 5), ("parent_event_id", None),
                           ("live", 0), ("inherited", 1)):
            with self.subTest(field=field):
                conn = memory_db()
                v1 = [PlacementRow("pi:S", "pi:e1", 0, None, 1, 0),
                      PlacementRow(**base)]
                v2 = [PlacementRow("pi:S", "pi:e1", 0, None, 1, 0),
                      PlacementRow(**{**base, field: new})]
                ingest._ingest(conn, [f], lambda p: parsed(v1))
                ingest._ingest(conn, [f], lambda p: parsed(v2))
                row = conn.execute(
                    "SELECT seq, parent_event_id, live, inherited "
                    "FROM session_events "
                    "WHERE session_id='pi:S' AND event_id='pi:e2'").fetchone()
                self.assertEqual(
                    tuple(row),
                    tuple({**base, field: new}[k] for k in
                          ("seq", "parent_event_id", "live", "inherited")))
                conn.close()

    def test_write_lock_precedes_decision_reads(self):
        # The compare-and-skip upserts turned the first statements of a file's
        # ingest into reads; two concurrent refreshers deciding against the
        # same stale snapshot must be impossible, so BEGIN IMMEDIATE has to
        # come before any decision-bearing read in the file transaction.
        d = self._tmpdir()
        f = self._pi_file(d, ["one", "two"])
        conn = memory_db()
        self.addCleanup(conn.close)
        statements: list = []
        conn.set_trace_callback(statements.append)
        ingest._ingest(conn, [f], lambda p: pi.parse_file(p, machine="t"))
        conn.set_trace_callback(None)
        first_read = next(i for i, s in enumerate(statements)
                          if s.lstrip().upper().startswith("SELECT"))
        first_lock = next(i for i, s in enumerate(statements)
                          if "BEGIN IMMEDIATE" in s.upper())
        self.assertLess(first_lock, first_read)

    def test_failed_write_after_placement_diff_rolls_back_whole_file(self):
        # Grown file: the only placement write is the appended row. A failure
        # there must roll back the file's whole transaction — including the
        # already-upserted new event — leaving the previous state intact.
        d = self._tmpdir()
        f = self._pi_file(d, ["one", "two", "three"])
        conn = memory_db()
        self.addCleanup(conn.close)
        parse = lambda p: pi.parse_file(p, machine="t")
        ingest._ingest(conn, [f], parse)
        self._pi_file(d, ["one", "two", "three", "four"])
        with mock.patch("codebrain.ingest.upsert_placement",
                        side_effect=RuntimeError("injected placement failure")):
            stats = ingest._ingest(conn, [f], parse)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM session_events WHERE session_id='pi:P1'").fetchone()[0], 3)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 3)

    def test_failed_write_after_stale_deletion_rolls_back_deletes(self):
        # Shrunk file: the diff issues stale-placement DELETEs with no upserts
        # after them. A later failure in the same transaction must restore the
        # deleted placements.
        d = self._tmpdir()
        f = self._pi_file(d, ["one", "two", "three"])
        conn = memory_db()
        self.addCleanup(conn.close)
        parse = lambda p: pi.parse_file(p, machine="t")
        ingest._ingest(conn, [f], parse)
        self._pi_file(d, ["one"])
        with mock.patch("codebrain.ingest._record_state",
                        side_effect=RuntimeError("injected state failure")):
            stats = ingest._ingest(conn, [f], parse)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM session_events WHERE session_id='pi:P1'").fetchone()[0], 3)


if __name__ == "__main__":
    unittest.main()

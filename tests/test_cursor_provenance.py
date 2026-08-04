"""Cursor origin evidence is driven only by structured source fields."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codebrain import cli, cursor_provenance, db, provenance
from codebrain.adapters.base import EventRow, PlacementRow, SessionRow
from tests._helpers import memory_db


def _session(conn, sid, *, relation=None, parent=None, source="cursor"):
    db.upsert_session(conn, SessionRow(
        session_id=sid, source=source, created_at="2026-01-01T00:00:00Z",
        relation=relation, parent_session_id=parent,
    ))


def _event(conn, sid, eid, text, raw=None, *, seq=0, inherited=0,
           live=1, actor="user", typ="message"):
    db.upsert_event(conn, EventRow(
        event_id=eid, origin_session_id=None if inherited else sid,
        ts=f"2026-01-01T00:00:{seq:02d}Z", actor=actor, type=typ,
        text=text, refs={"files": [], "commands": []}, raw=raw or {},
    ))
    db.upsert_placement(conn, PlacementRow(
        session_id=sid, event_id=eid, seq=seq,
        parent_event_id=None, live=live, inherited=inherited,
    ))


def _origins(conn):
    return {
        (row["session_id"], row["event_id"]): (row["origin"], row["evidence_kind"])
        for row in conn.execute(
            "SELECT session_id, event_id, origin, evidence_kind FROM event_origins"
        )
    }


class TestCursorStructuredProvenance(unittest.TestCase):
    def setUp(self):
        self.conn = memory_db()
        self.addCleanup(self.conn.close)

    def test_flags_not_wording_drive_simulated_plan_and_unknown_evidence(self):
        sid = "cursor:FLAGS"
        _session(self.conn, sid)
        cases = (
            ("cursor:sim", "same task", {"isSimulatedMsg": True}),
            ("cursor:human", "same task", {}),
            ("cursor:words", "background task completed", {}),
            ("cursor:future", "future reason", {
                "isSimulatedMsg": True, "simulatedMsgReason": 999,
            }),
            ("cursor:plan", "ordinary wording", {"isPlanExecution": True}),
            ("cursor:plan-words", "execute the plan", {}),
            ("cursor:contradiction", "ambiguous", {
                "isSimulatedMsg": False, "simulatedMsgReason": 3,
            }),
            ("cursor:string", "not a bool", {"isSimulatedMsg": "true"}),
            ("cursor:decision", "tool field is not provenance", {
                "toolFormerData": {"userDecision": "accepted"},
            }),
        )
        for seq, (eid, text, raw) in enumerate(cases):
            _event(self.conn, sid, eid, text, raw, seq=seq)
        _event(
            self.conn, sid, "cursor:assistant", "not user", {"isSimulatedMsg": True},
            seq=len(cases), actor="assistant",
        )
        self.conn.commit()

        stats = cursor_provenance.sync(self.conn, force=True)
        origins = _origins(self.conn)
        self.assertEqual(stats["master_control"], 3)
        self.assertEqual(stats["unknown"], 1)
        self.assertEqual(origins[(sid, "cursor:sim")], (
            "master_control", cursor_provenance.SIMULATED_KIND,
        ))
        self.assertEqual(origins[(sid, "cursor:future")][0], "master_control")
        self.assertEqual(origins[(sid, "cursor:plan")], (
            "master_control", cursor_provenance.PLAN_KIND,
        ))
        self.assertEqual(origins[(sid, "cursor:contradiction")][0], "unknown")
        for eid in (
            "cursor:human", "cursor:words", "cursor:plan-words",
            "cursor:string", "cursor:decision", "cursor:assistant",
        ):
            self.assertNotIn((sid, eid), origins)

    def test_explicit_kickoff_is_preferred_and_fallback_marks_only_first_input(self):
        fallback = "cursor:FALLBACK"
        explicit = "cursor:EXPLICIT"
        multiple = "cursor:MULTIPLE"
        top = "cursor:TOP"
        for sid in (fallback, explicit, multiple):
            _session(self.conn, sid, relation="subagent", parent=top)
        _session(self.conn, top)

        _event(self.conn, fallback, "cursor:f-first", "same task", seq=0)
        _event(self.conn, fallback, "cursor:f-followup", "later human", seq=1)
        _event(self.conn, explicit, "cursor:e-first", "earlier", seq=0)
        _event(self.conn, explicit, "cursor:e-marked", "kickoff", {
            "subagentSpawnTaskToolCallId": "call-1",
        }, seq=1)
        _event(self.conn, multiple, "cursor:a-one", "one", {
            "subagentSpawnTaskToolCallId": "call-1",
        }, seq=0)
        _event(self.conn, multiple, "cursor:a-two", "two", {
            "subagentSpawnTaskToolCallId": "call-2",
        }, seq=1)
        _event(self.conn, top, "cursor:top-human", "same task", seq=0)
        self.conn.commit()

        cursor_provenance.sync(self.conn, force=True)
        origins = _origins(self.conn)
        self.assertEqual(origins[(fallback, "cursor:f-first")][0], "master_control")
        self.assertNotIn((fallback, "cursor:f-followup"), origins)
        self.assertNotIn((explicit, "cursor:e-first"), origins)
        self.assertEqual(origins[(explicit, "cursor:e-marked")][0], "master_control")
        self.assertEqual(origins[(multiple, "cursor:a-one")][0], "master_control")
        self.assertEqual(origins[(multiple, "cursor:a-two")][0], "master_control")
        self.assertNotIn((top, "cursor:top-human"), origins)

    def test_empty_rolled_back_first_input_does_not_shift_fallback_to_followup(self):
        sid = "cursor:ROLLED"
        _session(self.conn, sid, relation="subagent", parent="cursor:P")
        _event(self.conn, sid, "cursor:empty-kickoff", "", seq=0, live=0)
        _event(self.conn, sid, "cursor:later-human", "later", seq=1)
        self.conn.commit()

        cursor_provenance.sync(self.conn, force=True)
        origins = _origins(self.conn)
        self.assertEqual(origins[(sid, "cursor:empty-kickoff")][0], "master_control")
        self.assertNotIn((sid, "cursor:later-human"), origins)

    def test_evidence_fans_out_to_inherited_placements(self):
        parent, child = "cursor:P", "cursor:C"
        _session(self.conn, parent)
        _session(self.conn, child, relation="subagent", parent=parent)
        _event(
            self.conn, parent, "cursor:shared", "generated",
            {"isSimulatedMsg": True}, seq=0,
        )
        _event(
            self.conn, child, "cursor:shared", "generated",
            {"isSimulatedMsg": True}, seq=0, inherited=1,
        )
        _event(self.conn, child, "cursor:kickoff", "work", seq=1)
        self.conn.commit()

        cursor_provenance.sync(self.conn, force=True)
        evidence = self.conn.execute(
            "SELECT session_id FROM event_origin_evidence "
            "WHERE event_id='cursor:shared' AND evidence_kind=? ORDER BY session_id",
            (cursor_provenance.SIMULATED_KIND,),
        ).fetchall()
        self.assertEqual([row["session_id"] for row in evidence], [child, parent])

    def test_rebuild_removes_stale_cursor_rows_preserves_other_derivers_and_skips(self):
        sid, eid = "cursor:S", "cursor:event"
        _session(self.conn, sid)
        _event(self.conn, sid, eid, "input", {"isSimulatedMsg": True})
        provenance.record_origin_evidence(self.conn, {
            "session_id": sid, "event_id": eid, "origin": "master_control",
            "evidence_kind": "other_deriver", "evidence_id": "external",
            "reason": "other structured evidence",
        })
        self.conn.commit()
        cursor_provenance.sync(self.conn, force=True)

        _event(self.conn, sid, eid, "input", {})
        self.conn.commit()
        cursor_provenance.sync(self.conn, changed_hint=True)
        kinds = {
            row["evidence_kind"] for row in self.conn.execute(
                "SELECT evidence_kind FROM event_origin_evidence WHERE event_id=?",
                (eid,),
            )
        }
        self.assertEqual(kinds, {"other_deriver"})
        self.assertEqual(
            cursor_provenance.sync(self.conn, changed_hint=False)["skipped"], 1
        )


class TestCursorProvenanceCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "codebrain.db"
        conn = db.connect(self.db_path)
        _session(conn, "cursor:CLI")
        _event(conn, "cursor:CLI", "cursor:human", "same text", seq=0)
        _event(conn, "cursor:CLI", "cursor:control", "same text", {
            "isSimulatedMsg": True,
        }, seq=1)
        conn.commit()
        conn.close()

    def run_cli(self, *args):
        out = io.StringIO()
        with mock.patch("codebrain.cli.bmux.sync"), \
             mock.patch("codebrain.cli.codex_control.sync"), \
             contextlib.redirect_stdout(out):
            cli.main(["--db", str(self.db_path), *args])
        return json.loads(out.getvalue())

    def test_read_path_sync_hides_control_message_even_without_refresh(self):
        human = self.run_cli("userlog", "--no-refresh", "--json")
        control = self.run_cli(
            "userlog", "--no-refresh", "--json", "--origin", "master-control"
        )
        search = self.run_cli(
            "search", "same", "--actor", "user", "--no-refresh", "--json"
        )
        recent = self.run_cli("recent", "--no-refresh", "--json")
        recent_control = self.run_cli(
            "recent", "--no-refresh", "--json", "--origin", "master-control"
        )
        self.assertEqual([row["event_id"] for row in human], ["cursor:human"])
        self.assertEqual([row["event_id"] for row in control], ["cursor:control"])
        self.assertEqual([row["event_id"] for row in search], ["cursor:human"])
        self.assertEqual(recent[0]["last_user_event_id"], "cursor:human")
        self.assertEqual(
            recent_control[0]["last_user_event_id"], "cursor:control"
        )

    def test_cursor_participates_in_source_prefix_resolution(self):
        self.assertIn("cursor:CLI%", cli._session_match_patterns("CLI"))
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(cli._resolve_unique_session(conn, "CLI")[0], "cursor:CLI")

    def test_cursor_source_filter_and_manual_repair_command(self):
        conn = db.connect(self.db_path)
        _session(conn, "pi:OTHER", source="pi")
        _event(conn, "pi:OTHER", "pi:other", "other", seq=0)
        conn.commit()
        conn.close()
        rows = self.run_cli(
            "userlog", "--source", "cursor", "--origin", "all",
            "--no-refresh", "--json",
        )
        self.assertEqual(
            {row["session_id"] for row in rows}, {"cursor:CLI"}
        )

        out = io.StringIO()
        with mock.patch("codebrain.cli.bmux.sync"), \
             mock.patch("codebrain.cli.codex_control.sync"), \
             contextlib.redirect_stdout(out):
            cli.main([
                "--db", str(self.db_path), "cursor-provenance-sync", "--no-refresh",
            ])
        self.assertIn("Cursor provenance:", out.getvalue())


if __name__ == "__main__":
    unittest.main()

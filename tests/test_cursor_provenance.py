"""Cursor origin evidence is driven only by structured source fields."""
from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codebrain import cli, cursor_archive, cursor_provenance, db, ingest, provenance
from codebrain.adapters.base import EventRow, PlacementRow, SessionRow, SourceHead
from tests._helpers import memory_db
from tests.test_cursor_integration import _snapshot


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


def _advance_head(conn, sid, revision):
    # Mirrors the ingest contract sync gates on: every accepted canonical
    # Cursor write commits together with a head advance (ingest._ingest).
    db.record_cursor_head(conn, sid, SourceHead(revision, "0" * 64))


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
        _advance_head(self.conn, sid, 1)
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


class TestCursorProvenanceHeadGating(unittest.TestCase):
    """sync's change detection is driven by cursor_session_heads, not hints."""

    def setUp(self):
        self.conn = memory_db()
        self.addCleanup(self.conn.close)
        self.sid = "cursor:GATE"
        _session(self.conn, self.sid)
        _event(self.conn, self.sid, "cursor:flagged", "generated",
               {"isSimulatedMsg": True}, seq=0)
        _advance_head(self.conn, self.sid, 1)
        self.conn.commit()

    def test_unchanged_heads_skip_without_writes_despite_changed_hint(self):
        self.assertEqual(cursor_provenance.sync(self.conn)["skipped"], 0)
        before = self.conn.total_changes
        stats = cursor_provenance.sync(self.conn, changed_hint=True)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["events"], 0)
        self.assertEqual(self.conn.total_changes, before)

    def test_head_advance_invalidates_skip_and_rebuild_reclassifies(self):
        cursor_provenance.sync(self.conn)
        _event(self.conn, self.sid, "cursor:flagged", "generated", {}, seq=0)
        _advance_head(self.conn, self.sid, 2)
        self.conn.commit()
        stats = cursor_provenance.sync(self.conn, changed_hint=False)
        self.assertEqual(stats["skipped"], 0)
        self.assertNotIn((self.sid, "cursor:flagged"), _origins(self.conn))

    def test_force_rebuilds_even_when_heads_unchanged(self):
        self.assertEqual(cursor_provenance.sync(self.conn)["skipped"], 0)
        stats = cursor_provenance.sync(self.conn, force=True)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(stats["events"], 1)
        self.assertEqual(stats["master_control"], 1)

    def test_skip_never_runs_the_source_rows_join(self):
        # The whole point of the head gate: the events.raw join must stay off
        # the read path when nothing changed, not merely produce a no-op.
        cursor_provenance.sync(self.conn)
        with mock.patch.object(cursor_provenance, "_source_rows",
                               side_effect=AssertionError("join ran on skip")):
            stats = cursor_provenance.sync(self.conn, changed_hint=True)
        self.assertEqual(stats["skipped"], 1)

    def test_algo_version_bump_forces_one_rebuild(self):
        cursor_provenance.sync(self.conn)
        with mock.patch.object(cursor_provenance, "ALGO_VERSION",
                               cursor_provenance.ALGO_VERSION + 1):
            stats = cursor_provenance.sync(self.conn, changed_hint=False)
            self.assertEqual(stats["skipped"], 0)
            self.assertEqual(stats["master_control"], 1)
            self.assertEqual(
                cursor_provenance.sync(self.conn, changed_hint=False)["skipped"], 1
            )

    def test_legacy_fingerprint_states_rebuild_once_then_skip(self):
        # Earlier syncs stored sha256 hex fingerprints at STATE_PATH (first of
        # _source_rows, later of the heads table). Either legacy shape must be
        # treated as "not the per-session algo marker": one full rebuild, then
        # the per-session diff takes over and skips.
        for label in ("source-rows fingerprint", "head fingerprint"):
            with self.subTest(label):
                self.conn.execute(
                    "INSERT OR REPLACE INTO ingest_state "
                    "(path, mtime, size, session_id) VALUES (?, 0, ?, ?)",
                    (cursor_provenance.STATE_PATH, 1, "ab" * 32),
                )
                self.conn.execute("DELETE FROM cursor_provenance_state")
                self.conn.commit()
                first = cursor_provenance.sync(self.conn, changed_hint=False)
                self.assertEqual(first["skipped"], 0)
                self.assertEqual(first["master_control"], 1)
                self.assertEqual(
                    cursor_provenance.sync(self.conn, changed_hint=False)["skipped"],
                    1,
                )


class TestCursorProvenanceIncremental(unittest.TestCase):
    """A head advance rebuilds that session's branch family, nothing else."""

    def setUp(self):
        self.conn = memory_db()
        self.addCleanup(self.conn.close)

    def _flagged_session(self, sid, eid, revision=1):
        _session(self.conn, sid)
        _event(self.conn, sid, eid, "generated", {"isSimulatedMsg": True}, seq=0)
        _advance_head(self.conn, sid, revision)

    def test_incremental_rebuild_scopes_to_the_changed_session(self):
        self._flagged_session("cursor:A", "cursor:a-ev")
        self._flagged_session("cursor:B", "cursor:b-ev")
        self.conn.commit()
        self.assertEqual(cursor_provenance.sync(self.conn)["rebuilt_sessions"], 2)

        # Deliberately violate the ingest contract for B: change its event
        # without advancing its head. A correctly scoped incremental sync must
        # NOT notice (B is never rescanned) — that stale evidence is exactly
        # what proves the rebuild did not touch B.
        _event(self.conn, "cursor:B", "cursor:b-ev", "generated", {}, seq=0)
        _event(self.conn, "cursor:A", "cursor:a-ev", "generated", {}, seq=0)
        _advance_head(self.conn, "cursor:A", 2)
        self.conn.commit()

        stats = cursor_provenance.sync(self.conn, changed_hint=False)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(stats["rebuilt_sessions"], 1)
        origins = _origins(self.conn)
        self.assertNotIn(("cursor:A", "cursor:a-ev"), origins)
        self.assertIn(("cursor:B", "cursor:b-ev"), origins)
        # No temp scaffolding leaks out of sync().
        leftover = self.conn.execute(
            "SELECT name FROM sqlite_temp_master WHERE type='table'"
        ).fetchall()
        self.assertEqual(leftover, [])

    def test_family_closure_recomputes_sessions_sharing_events(self):
        parent, child, other = "cursor:P", "cursor:C", "cursor:Z"
        _session(self.conn, parent)
        _session(self.conn, child, relation="subagent", parent=parent)
        _event(self.conn, parent, "cursor:shared", "generated",
               {"isSimulatedMsg": True}, seq=0)
        _event(self.conn, child, "cursor:shared", "generated",
               {"isSimulatedMsg": True}, seq=0, inherited=1)
        _event(self.conn, child, "cursor:kickoff", "work", seq=1)
        _advance_head(self.conn, parent, 1)
        _advance_head(self.conn, child, 1)
        self._flagged_session(other, "cursor:z-ev")
        self.conn.commit()
        cursor_provenance.sync(self.conn)
        origins = _origins(self.conn)
        self.assertIn((parent, "cursor:shared"), origins)
        self.assertIn((child, "cursor:shared"), origins)

        # The flag disappears from the shared event; only the PARENT's head
        # advances. The child holds an inherited placement of the same event,
        # so its evidence row must be recomputed too — the closure has to pull
        # the child in — while the unrelated session stays untouched.
        _event(self.conn, parent, "cursor:shared", "generated", {}, seq=0)
        _advance_head(self.conn, parent, 2)
        self.conn.commit()

        stats = cursor_provenance.sync(self.conn, changed_hint=False)
        self.assertEqual(stats["rebuilt_sessions"], 2)
        origins = _origins(self.conn)
        self.assertNotIn((parent, "cursor:shared"), origins)
        self.assertNotIn((child, "cursor:shared"), origins)
        self.assertIn((other, "cursor:z-ev"), origins)
        # The child's own subagent-kickoff fallback evidence was recomputed,
        # not lost, by the family rebuild.
        self.assertEqual(origins[(child, "cursor:kickoff")][0], "master_control")

    def test_revision_removing_shared_fallback_event_cleans_inherited_evidence(self):
        # Subagent A's first authored event `old` is inherited by B. A's next
        # revision REMOVES `old` (making `new` its first authored input) and
        # only A's head advances. The post-change placement graph no longer
        # connects A to B, so a placement-only closure would strand B's stale
        # kickoff evidence forever (watermarks match afterwards, so it would
        # never be revisited). The evidence-seed edge — sessions holding
        # our-kind evidence on events AUTHORED by a changed session — must pull
        # B in, and the incremental result must equal a full rebuild's.
        top, suba, branch = "cursor:TOP", "cursor:A", "cursor:B"
        _session(self.conn, top)
        _session(self.conn, suba, relation="subagent", parent=top)
        _session(self.conn, branch)
        _event(self.conn, suba, "cursor:old", "kick", seq=0)
        _event(self.conn, branch, "cursor:old", "kick", seq=0, inherited=1)
        _advance_head(self.conn, suba, 1)
        _advance_head(self.conn, branch, 1)
        self.conn.commit()
        cursor_provenance.sync(self.conn)
        origins = _origins(self.conn)
        self.assertIn((suba, "cursor:old"), origins)
        self.assertIn((branch, "cursor:old"), origins)

        self.conn.execute(
            "DELETE FROM session_events WHERE session_id=? AND event_id=?",
            (suba, "cursor:old"))
        _event(self.conn, suba, "cursor:new", "kick2", seq=0)
        _advance_head(self.conn, suba, 2)
        self.conn.commit()

        stats = cursor_provenance.sync(self.conn, changed_hint=False)
        self.assertEqual(stats["skipped"], 0)
        incremental = _origins(self.conn)
        cursor_provenance.sync(self.conn, force=True)
        self.assertEqual(incremental, _origins(self.conn))
        self.assertNotIn((branch, "cursor:old"), incremental)
        self.assertIn((suba, "cursor:new"), incremental)

    def test_flag_gained_on_shared_event_reaches_unchanged_inherited_holder(self):
        # The load-bearing case for the PLACEMENT closure (the evidence-seed
        # edge cannot help here — no prior evidence row exists to follow): a
        # shared event GAINS a control flag via the authoring session's
        # revision, and the unchanged session holding an inherited copy must be
        # recomputed too, or its copy keeps reading as human.
        parent, child = "cursor:GP", "cursor:GC"
        _session(self.conn, parent)
        _session(self.conn, child)
        _event(self.conn, parent, "cursor:g-shared", "generated", {}, seq=0)
        _event(self.conn, child, "cursor:g-shared", "generated", {}, seq=0,
               inherited=1)
        _advance_head(self.conn, parent, 1)
        _advance_head(self.conn, child, 1)
        self.conn.commit()
        cursor_provenance.sync(self.conn)
        self.assertEqual(_origins(self.conn), {})

        _event(self.conn, parent, "cursor:g-shared", "generated",
               {"isSimulatedMsg": True}, seq=0)
        _advance_head(self.conn, parent, 2)
        self.conn.commit()
        cursor_provenance.sync(self.conn, changed_hint=False)
        incremental = _origins(self.conn)
        cursor_provenance.sync(self.conn, force=True)
        self.assertEqual(incremental, _origins(self.conn))
        self.assertIn((child, "cursor:g-shared"), incremental)

    def test_closure_fixpoint_reaches_two_hop_family_members(self):
        # S shares e1 with M; M holds an inherited copy of subagent N's
        # kickoff event e2. Rebuilding M (pulled in at hop 1) deletes M's
        # kickoff row for e2, and recreating it requires re-running N's
        # lineage rule — N is only reachable at hop 2, so a closure that stops
        # after one round loses (M, e2) relative to a full rebuild.
        s, m, n, top = "cursor:S2", "cursor:M2", "cursor:N2", "cursor:TOP2"
        _session(self.conn, top)
        _session(self.conn, s)
        _session(self.conn, m)
        _session(self.conn, n, relation="subagent", parent=top)
        _event(self.conn, n, "cursor:e2", "kick", seq=0)
        _event(self.conn, m, "cursor:e2", "kick", seq=0, inherited=1)
        _event(self.conn, m, "cursor:e1", "hello", seq=1)
        _event(self.conn, s, "cursor:e1", "hello", seq=0, inherited=1)
        for sid in (s, m, n, top):
            _advance_head(self.conn, sid, 1)
        self.conn.commit()
        cursor_provenance.sync(self.conn)
        self.assertIn((m, "cursor:e2"), _origins(self.conn))

        _event(self.conn, s, "cursor:s-new", "more", seq=1)
        _advance_head(self.conn, s, 2)
        self.conn.commit()
        stats = cursor_provenance.sync(self.conn, changed_hint=False)
        self.assertEqual(stats["skipped"], 0)
        incremental = _origins(self.conn)
        cursor_provenance.sync(self.conn, force=True)
        self.assertEqual(incremental, _origins(self.conn))
        self.assertIn((m, "cursor:e2"), incremental)

    def test_vanished_head_drops_state_and_rearms_skip(self):
        self._flagged_session("cursor:GONE", "cursor:g-ev")
        self.conn.commit()
        cursor_provenance.sync(self.conn)
        self.assertEqual(
            cursor_provenance.sync(self.conn, changed_hint=True)["skipped"], 1)

        self.conn.execute(
            "DELETE FROM cursor_session_heads WHERE session_id='cursor:GONE'")
        self.conn.commit()
        stats = cursor_provenance.sync(self.conn, changed_hint=False)
        self.assertEqual(stats["skipped"], 0)
        rows = self.conn.execute(
            "SELECT session_id FROM cursor_provenance_state").fetchall()
        self.assertEqual(rows, [])
        self.assertEqual(
            cursor_provenance.sync(self.conn, changed_hint=False)["skipped"], 1)


class TestCursorProvenanceIngestInvalidation(unittest.TestCase):
    """A real archive ingest advances cursor_session_heads and re-arms sync."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = Path(self.tmp.name) / "archive"
        self.conn = memory_db()
        self.addCleanup(self.conn.close)

    def _refresh(self):
        return ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )

    def test_new_revision_reingest_rebuilds_classification(self):
        cursor_archive.publish_snapshot(_snapshot("PROV"), self.archive)
        self._refresh()
        self.assertEqual(cursor_provenance.sync(self.conn)["skipped"], 0)
        self.assertEqual(
            cursor_provenance.sync(self.conn, changed_hint=True)["skipped"], 1
        )

        flagged = _snapshot("PROV")
        flagged["order"][0]["payload"]["isSimulatedMsg"] = True
        cursor_archive.publish_snapshot(flagged, self.archive)
        self.assertEqual(self._refresh()["sessions"], 1)

        stats = cursor_provenance.sync(self.conn, changed_hint=False)
        self.assertEqual((stats["skipped"], stats["master_control"]), (0, 1))
        self.assertEqual(
            _origins(self.conn)[
                ("cursor:PROV", "cursor:b1:1767225601000:message")
            ],
            ("master_control", cursor_provenance.SIMULATED_KIND),
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

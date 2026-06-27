"""bmux provenance overlay (codebrain/bmux.py).

Structured provenance only: matches native user messages to bmux control
submissions by resolved session + exact UTF-8 SHA-256, fail-closed on ambiguity.
The five cases the plan calls out are pinned here.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codebrain import bmux, cli, db
from codebrain.adapters.base import EventRow, PlacementRow, SessionRow
from tests._helpers import memory_db, write_jsonl


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _add_user(conn, *, sid, eid, seq, ts, text, source=None, live=1):
    source = source or sid.split(":", 1)[0]
    db.upsert_session(conn, SessionRow(
        session_id=sid, source=source, cwd="/work",
        started_at="2026-01-01T00:00:00Z", ended_at=ts))
    db.upsert_event(conn, EventRow(
        event_id=eid, origin_session_id=sid, ts=ts, actor="user", type="message",
        text=text, refs={"files": [], "commands": []}, raw={}))
    db.upsert_placement(conn, PlacementRow(
        session_id=sid, event_id=eid, seq=seq, parent_event_id=None,
        live=live, inherited=0))


def _send_submitted(*, eid, sid, text, submitted_at, send_id="bmux_send_1",
                    master_id="bmux_master_x"):
    body = text.encode("utf-8")
    return {
        "kind": "bmux.send_submitted", "event_id": eid,
        "actor": {"master_id": master_id},
        "data": {
            "send_id": send_id, "codebrain_session_id": sid,
            "transcript_origin": "master_control", "submitted_at": submitted_at,
            "payload": {"sha256": _sha(text), "byte_count": len(body), "line_count": 1},
        },
    }


def _launch_prompt_submitted(*, eid, launch_id, text, submitted_at,
                             send_id="bmux_send_L", master_id="m"):
    body = text.encode("utf-8")
    return {
        "kind": "bmux.launch_prompt_submitted", "event_id": eid,
        "actor": {"master_id": master_id},
        "data": {
            "send_id": send_id, "launch_id": launch_id, "submitted_at": submitted_at,
            "transcript_origin": "master_control",
            "payload": {"sha256": _sha(text), "byte_count": len(body), "line_count": 1},
        },
    }


def _pane_discovered(*, eid, sid, launch_id=None, previous_launch_id=None):
    data = {"codebrain_session_id": sid, "link_state": "linked"}
    if launch_id is not None:
        data["launch_id"] = launch_id
    if previous_launch_id is not None:
        data["previous_bmux"] = {"launch_id": previous_launch_id}
    return {"kind": "bmux.pane_discovered", "event_id": eid, "data": data}


def _send_no_timestamp(*, eid, sid, text):
    """A submission missing submitted_at/attempted_at — cannot satisfy the
    required time/order condition, so it must never match (fail closed)."""
    body = text.encode("utf-8")
    return {
        "kind": "bmux.send_submitted", "event_id": eid, "actor": {"master_id": "m"},
        "data": {"send_id": "s", "codebrain_session_id": sid,
                 "payload": {"sha256": _sha(text), "byte_count": len(body), "line_count": 1}},
    }


def _add_inherited_copy(conn, *, child_sid, eid, seq, ts, source=None):
    """A pi-style inherited placement of an already-authored event in a child
    session (same event_id, inherited=1) — the copy-invariance case."""
    source = source or child_sid.split(":", 1)[0]
    db.upsert_session(conn, SessionRow(
        session_id=child_sid, source=source, cwd="/work",
        started_at="2026-01-01T00:00:00Z", ended_at=ts, relation="resume"))
    db.upsert_placement(conn, PlacementRow(
        session_id=child_sid, event_id=eid, seq=seq, parent_event_id=None,
        live=1, inherited=1))


class TestBmuxProvenance(unittest.TestCase):
    def setUp(self):
        self.conn = memory_db()
        self.addCleanup(self.conn.close)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _sync(self, records, window_sec=bmux.DEFAULT_WINDOW_SEC):
        self.conn.commit()  # mirror the read path: refresh() commits before sync()
        path = write_jsonl(self.tmp.name, "bmux.jsonl", records)
        return bmux.sync(self.conn, path, window_sec=window_sec)

    def _origin(self, sid, eid):
        row = self.conn.execute(
            "SELECT origin FROM event_origins WHERE session_id=? AND event_id=?",
            (sid, eid)).fetchone()
        return row["origin"] if row else None  # None == clean human (no row)

    # 1) a clean human message in an unrelated session stays human
    def test_clean_human_message_stays_human(self):
        _add_user(self.conn, sid="claude:H", eid="claude:h1", seq=0,
                  ts="2026-01-01T00:00:01Z", text="how do I fix the build?")
        # bmux activity exists, but for a different session + payload
        stats = self._sync([_send_submitted(
            eid="e1", sid="codex:OTHER", text="please continue",
            submitted_at="2026-01-01T00:00:01.000Z")])
        self.assertIsNone(self._origin("claude:H", "claude:h1"))
        self.assertEqual(stats["master_control"], 0)

    # 2) a bmux send becomes master_control
    def test_bmux_send_becomes_master_control(self):
        _add_user(self.conn, sid="codex:S", eid="codex:S:6", seq=0,
                  ts="2026-01-01T00:00:01.050Z", text="please continue")
        stats = self._sync([_send_submitted(
            eid="e1", sid="codex:S", text="please continue",
            submitted_at="2026-01-01T00:00:01.000Z")])
        self.assertEqual(self._origin("codex:S", "codex:S:6"), "master_control")
        self.assertEqual(stats["master_control"], 1)

    # 3) a human typing different text near a bmux send stays human
    def test_manual_different_text_near_bmux_send_stays_human(self):
        _add_user(self.conn, sid="codex:S", eid="codex:S:6", seq=0,
                  ts="2026-01-01T00:00:01.050Z", text="please continue")  # the bmux one
        _add_user(self.conn, sid="codex:S", eid="codex:S:7", seq=1,
                  ts="2026-01-01T00:00:02.000Z", text="actually, stop and explain")
        self._sync([_send_submitted(
            eid="e1", sid="codex:S", text="please continue",
            submitted_at="2026-01-01T00:00:01.000Z")])
        self.assertEqual(self._origin("codex:S", "codex:S:6"), "master_control")
        self.assertIsNone(self._origin("codex:S", "codex:S:7"))  # human

    # 4) a repeated identical ambiguous payload becomes unknown (fail closed)
    def test_repeated_identical_payload_becomes_unknown(self):
        # two byte-identical user messages in the same session, one bmux send:
        # cannot uniquely pair -> both unknown, neither claimed as clean human.
        _add_user(self.conn, sid="codex:S", eid="codex:S:6", seq=0,
                  ts="2026-01-01T00:00:01.050Z", text="please continue")
        _add_user(self.conn, sid="codex:S", eid="codex:S:9", seq=1,
                  ts="2026-01-01T00:00:03.000Z", text="please continue")
        stats = self._sync([_send_submitted(
            eid="e1", sid="codex:S", text="please continue",
            submitted_at="2026-01-01T00:00:01.000Z")])
        self.assertEqual(self._origin("codex:S", "codex:S:6"), "unknown")
        self.assertEqual(self._origin("codex:S", "codex:S:9"), "unknown")
        self.assertEqual(stats["master_control"], 0)
        self.assertEqual(stats["unknown"], 2)

    # 5) a launch prompt resolves through a later pane_discovered (current shape)
    def test_launch_prompt_resolves_via_pane_discovered(self):
        _add_user(self.conn, sid="claude:T", eid="claude:t1", seq=0,
                  ts="2026-01-01T00:00:05.010Z", text="Review the diff")
        stats = self._sync([
            _launch_prompt_submitted(eid="e1", launch_id="L1", text="Review the diff",
                                     submitted_at="2026-01-01T00:00:05.000Z"),
            _pane_discovered(eid="e2", sid="claude:T", launch_id="L1"),
        ])
        self.assertEqual(self._origin("claude:T", "claude:t1"), "master_control")
        self.assertEqual(stats["resolved"], 1)
        self.assertEqual(stats["master_control"], 1)

    # 5b) same, but launch_id only recoverable from previous_bmux (historical shape)
    def test_launch_prompt_resolves_via_previous_bmux_launch_id(self):
        _add_user(self.conn, sid="codex:U", eid="codex:U:3", seq=0,
                  ts="2026-01-01T00:00:05.010Z", text="bootstrap prompt")
        self._sync([
            _launch_prompt_submitted(eid="e1", launch_id="L9", text="bootstrap prompt",
                                     submitted_at="2026-01-01T00:00:05.000Z"),
            _pane_discovered(eid="e2", sid="codex:U", previous_launch_id="L9"),
        ])
        self.assertEqual(self._origin("codex:U", "codex:U:3"), "master_control")

    # an unresolved launch prompt is left unmatched (fail closed), pollutes nothing
    def test_unresolved_launch_prompt_left_unmatched(self):
        _add_user(self.conn, sid="codex:U", eid="codex:U:3", seq=0,
                  ts="2026-01-01T00:00:05.010Z", text="bootstrap prompt")
        stats = self._sync([_launch_prompt_submitted(
            eid="e1", launch_id="L-missing", text="bootstrap prompt",
            submitted_at="2026-01-01T00:00:05.000Z")])  # no pane_discovered
        self.assertIsNone(self._origin("codex:U", "codex:U:3"))
        self.assertEqual(stats["unresolved"], 1)
        self.assertEqual(stats["master_control"], 0)

    # identical payload to two different sessions separates cleanly by launch_id
    # (the real-log ambiguity case from the resolver probe)
    def test_identical_payload_two_sessions_separate_by_launch_id(self):
        _add_user(self.conn, sid="codex:A", eid="codex:A:6", seq=0,
                  ts="2026-01-01T00:00:05.010Z", text="Review the diff")
        _add_user(self.conn, sid="claude:B", eid="claude:b1", seq=0,
                  ts="2026-01-01T00:00:06.010Z", text="Review the diff")
        self._sync([
            _launch_prompt_submitted(eid="e1", launch_id="LA", text="Review the diff",
                                     submitted_at="2026-01-01T00:00:05.000Z", send_id="s1"),
            _pane_discovered(eid="e2", sid="codex:A", launch_id="LA"),
            _launch_prompt_submitted(eid="e3", launch_id="LB", text="Review the diff",
                                     submitted_at="2026-01-01T00:00:06.000Z", send_id="s2"),
            _pane_discovered(eid="e4", sid="claude:B", launch_id="LB"),
        ])
        self.assertEqual(self._origin("codex:A", "codex:A:6"), "master_control")
        self.assertEqual(self._origin("claude:B", "claude:b1"), "master_control")

    # the window bounds the blast radius: same hash, but far outside the window
    def test_match_outside_window_is_not_claimed(self):
        _add_user(self.conn, sid="codex:S", eid="codex:S:6", seq=0,
                  ts="2026-01-01T03:00:00Z", text="please continue")  # hours later
        self._sync([_send_submitted(
            eid="e1", sid="codex:S", text="please continue",
            submitted_at="2026-01-01T00:00:01.000Z")], window_sec=600)
        self.assertIsNone(self._origin("codex:S", "codex:S:6"))

    # no bmux log -> clean no-op
    def test_missing_log_is_noop(self):
        stats = bmux.sync(self.conn, Path(self.tmp.name) / "does-not-exist.jsonl")
        self.assertEqual(stats["submissions"], 0)

    # --- hardening (review findings) ------------------------------------

    # ORDERED window: a human message typed BEFORE the bmux send (same text)
    # stays human; only the message at/after the submission is master_control.
    def test_human_message_before_send_stays_human(self):
        _add_user(self.conn, sid="codex:S", eid="codex:S:1", seq=0,
                  ts="2026-01-01T00:00:00.000Z", text="please continue")   # human, earlier
        _add_user(self.conn, sid="codex:S", eid="codex:S:6", seq=1,
                  ts="2026-01-01T00:00:30.050Z", text="please continue")   # the bmux one
        self._sync([_send_submitted(
            eid="e1", sid="codex:S", text="please continue",
            submitted_at="2026-01-01T00:00:30.000Z")])
        self.assertIsNone(self._origin("codex:S", "codex:S:1"))            # before window
        self.assertEqual(self._origin("codex:S", "codex:S:6"), "master_control")

    # FAIL CLOSED: a submission with no submitted_at is dropped, never matched.
    def test_missing_submitted_at_never_matches(self):
        _add_user(self.conn, sid="codex:S", eid="codex:S:6", seq=0,
                  ts="2026-01-01T00:00:01Z", text="please continue")
        stats = self._sync([_send_no_timestamp(
            eid="e1", sid="codex:S", text="please continue")])
        self.assertIsNone(self._origin("codex:S", "codex:S:6"))
        self.assertEqual(stats["submissions"], 0)  # dropped before storage/match

    # DEGRADED EVIDENCE: a same-session/same-hash match whose transcript ts is
    # unparseable is unknown (inside the blast radius, unverifiable) — never
    # promoted to master_control, and never left as clean human.
    def test_unparseable_transcript_ts_is_unknown_not_human(self):
        _add_user(self.conn, sid="codex:S", eid="codex:S:6", seq=0,
                  ts="not-a-timestamp", text="please continue")
        stats = self._sync([_send_submitted(
            eid="e1", sid="codex:S", text="please continue",
            submitted_at="2026-01-01T00:00:01.000Z")])
        self.assertEqual(self._origin("codex:S", "codex:S:6"), "unknown")
        self.assertEqual(stats["master_control"], 0)
        self.assertEqual(stats["unknown"], 1)

    # A readable-but-far timestamp stays human (the window says "unrelated"); only
    # the *unverifiable* case degrades to unknown — the two must not be conflated.
    def test_readable_far_timestamp_stays_human(self):
        _add_user(self.conn, sid="codex:S", eid="codex:S:6", seq=0,
                  ts="2026-01-01T05:00:00Z", text="please continue")  # hours later, readable
        self._sync([_send_submitted(
            eid="e1", sid="codex:S", text="please continue",
            submitted_at="2026-01-01T00:00:01.000Z")], window_sec=600)
        self.assertIsNone(self._origin("codex:S", "codex:S:6"))

    # FAIL CLOSED: a launch_id seen with two different sessions resolves to
    # neither (no wrong-session attribution).
    def test_conflicting_launch_id_resolves_to_neither(self):
        _add_user(self.conn, sid="codex:A", eid="codex:A:6", seq=0,
                  ts="2026-01-01T00:00:05.010Z", text="ambiguous prompt")
        stats = self._sync([
            _launch_prompt_submitted(eid="e1", launch_id="LX", text="ambiguous prompt",
                                     submitted_at="2026-01-01T00:00:05.000Z"),
            _pane_discovered(eid="e2", sid="codex:A", launch_id="LX"),
            _pane_discovered(eid="e3", sid="claude:B", launch_id="LX"),  # same id, other session
        ])
        self.assertIsNone(self._origin("codex:A", "codex:A:6"))
        self.assertEqual(stats["unresolved"], 1)
        self.assertEqual(stats["master_control"], 0)

    # The verdict propagates to inherited (resumed) copies of the same event.
    def test_origin_propagates_to_inherited_copies(self):
        _add_user(self.conn, sid="codex:P", eid="codex:P:6", seq=0,
                  ts="2026-01-01T00:00:01.050Z", text="Review the diff")     # authored in parent
        _add_inherited_copy(self.conn, child_sid="codex:C", eid="codex:P:6", seq=0,
                            ts="2026-01-01T00:00:01.050Z")                    # copied into child
        self._sync([_send_submitted(
            eid="e1", sid="codex:P", text="Review the diff",
            submitted_at="2026-01-01T00:00:01.000Z")])
        self.assertEqual(self._origin("codex:P", "codex:P:6"), "master_control")
        self.assertEqual(self._origin("codex:C", "codex:P:6"), "master_control")

    # Steady state: an unchanged log + no transcript change is a no-op.
    def test_unchanged_log_is_skipped(self):
        _add_user(self.conn, sid="codex:S", eid="codex:S:6", seq=0,
                  ts="2026-01-01T00:00:01.050Z", text="please continue")
        self.conn.commit()
        path = write_jsonl(self.tmp.name, "bmux.jsonl", [_send_submitted(
            eid="e1", sid="codex:S", text="please continue",
            submitted_at="2026-01-01T00:00:01.000Z")])
        first = bmux.sync(self.conn, path)
        self.assertEqual(first["master_control"], 1)
        self.assertEqual(first["skipped"], 0)
        # same path, unchanged, caller signals no transcript change -> skip
        second = bmux.sync(self.conn, path, changed_hint=False)
        self.assertEqual(second["skipped"], 1)
        self.assertEqual(second["master_control"], 0)
        self.assertEqual(self._origin("codex:S", "codex:S:6"), "master_control")  # preserved


class TestBmuxProvenanceCLI(unittest.TestCase):
    """The user-facing contract (plan steps 4-6): default queries hide
    master_control; --origin flips it. AGENTS.md requires a test that proves the
    structured signal drives the behavior."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "codebrain.db"
        conn = db.connect(self.db_path)
        # one human message, one bmux-submitted message, same session
        _add_user(conn, sid="codex:S", eid="codex:S:1", seq=0,
                  ts="2026-01-01T00:00:00.000Z", text="my own question")
        _add_user(conn, sid="codex:S", eid="codex:S:6", seq=1,
                  ts="2026-01-01T00:00:30.050Z", text="please continue")
        conn.commit()
        conn.close()
        self.log = write_jsonl(self.tmp.name, "bmux.jsonl", [_send_submitted(
            eid="e1", sid="codex:S", text="please continue",
            submitted_at="2026-01-01T00:00:30.000Z")])
        self.env = mock.patch.dict(os.environ, {"CODEBRAIN_BMUX_LOG": str(self.log)})
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--db", str(self.db_path), *args])
        return out.getvalue()

    def _texts(self, rows):
        return {r.get("text") or r.get("last_user_text") for r in rows}

    def test_default_queries_hide_master_control_until_overridden(self):
        # populate the overlay via the explicit command (env-redirected log)
        self.run_cli("bmux-sync", "--no-refresh")

        # userlog default: human only
        human = json.loads(self.run_cli("userlog", "--no-refresh", "--json"))
        self.assertEqual(self._texts(human), {"my own question"})

        # --origin master-control: only the bmux message, labeled
        mc = json.loads(self.run_cli("userlog", "--no-refresh", "--json",
                                     "--origin", "master-control"))
        self.assertEqual(self._texts(mc), {"please continue"})
        self.assertEqual(mc[0]["origin"], "master_control")

        # --origin all: both
        allrows = json.loads(self.run_cli("userlog", "--no-refresh", "--json",
                                          "--origin", "all"))
        self.assertEqual(self._texts(allrows), {"my own question", "please continue"})

        # search --actor user defaults to human too
        hits = json.loads(self.run_cli("search", "continue", "--no-refresh", "--json",
                                       "--actor", "user"))
        self.assertEqual(hits, [])
        hits_all = json.loads(self.run_cli("search", "continue", "--no-refresh", "--json",
                                           "--actor", "user", "--origin", "all"))
        self.assertEqual(self._texts(hits_all), {"please continue"})

    def test_no_refresh_still_syncs_provenance(self):
        # No explicit bmux-sync: the --no-refresh read path itself must build the
        # overlay (option B), so the bmux send is hidden from default userlog
        # rather than leaking as human just because ingest was skipped.
        human = json.loads(self.run_cli("userlog", "--no-refresh", "--json"))
        self.assertEqual(self._texts(human), {"my own question"})
        mc = json.loads(self.run_cli("userlog", "--no-refresh", "--json",
                                     "--origin", "master-control"))
        self.assertEqual(self._texts(mc), {"please continue"})


if __name__ == "__main__":
    unittest.main()

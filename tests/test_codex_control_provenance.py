"""Codex control-message provenance overlay.

Structured sender-side Codex tool calls can explain receiver-side native
``user_message`` rows. These tests prove the structured target+hash evidence
drives classification, not the message text itself.
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from codebrain import cli, codex_control, db, provenance
from codebrain.adapters.base import EventRow, PlacementRow, SessionRow
from tests._helpers import memory_db


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _session(conn, sid):
    db.upsert_session(conn, SessionRow(
        session_id=sid, source="codex", cwd="/work",
        started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:01:00Z"))


def _add_user(conn, *, sid, eid, seq, ts, text, live=1):
    _session(conn, sid)
    db.upsert_event(conn, EventRow(
        event_id=eid, origin_session_id=sid, ts=ts, actor="user", type="message",
        text=text, refs={"files": [], "commands": []},
        raw={"type": "event_msg", "timestamp": ts,
             "payload": {"type": "user_message", "message": text}}))
    db.upsert_placement(conn, PlacementRow(
        session_id=sid, event_id=eid, seq=seq, parent_event_id=None,
        live=live, inherited=0))


def _add_raw_event(conn, *, sid, eid, seq, ts, actor, typ, text, raw):
    _session(conn, sid)
    db.upsert_event(conn, EventRow(
        event_id=eid, origin_session_id=sid, ts=ts, actor=actor, type=typ,
        text=text, refs={"files": [], "commands": []}, raw=raw))
    db.upsert_placement(conn, PlacementRow(
        session_id=sid, event_id=eid, seq=seq, parent_event_id=None,
        live=1, inherited=0))


def _mcp_reply_raw(*, ts, call_id, target, prompt, duration_secs=1, server="codex"):
    return {
        "type": "event_msg",
        "timestamp": ts,
        "payload": {
            "type": "mcp_tool_call_end",
            "call_id": call_id,
            "invocation": {
                "server": server,
                "tool": "codex-reply",
                "arguments": {"threadId": target, "prompt": prompt},
            },
            "duration": {"secs": duration_secs, "nanos": 0},
            "result": {"Ok": {"content": [{"type": "text", "text": "ok"}]}},
        },
    }


def _mcp_start_raw(*, ts, call_id, target, prompt, duration_secs=1, server="codex"):
    return {
        "type": "event_msg",
        "timestamp": ts,
        "payload": {
            "type": "mcp_tool_call_end",
            "call_id": call_id,
            "invocation": {
                "server": server,
                "tool": "codex",
                "arguments": {"prompt": prompt},
            },
            "duration": {"secs": duration_secs, "nanos": 0},
            "result": {"Ok": {"structuredContent": {"threadId": target}}},
        },
    }


def _send_input_raw(*, ts, call_id, target, message=None, items=None,
                    namespace=codex_control.MULTI_AGENT_V1_NAMESPACE):
    args = {"target": target}
    if message is not None:
        args["message"] = message
    if items is not None:
        args["items"] = items
    return {
        "type": "response_item",
        "timestamp": ts,
        "payload": {
            "type": "function_call",
            "name": "send_input",
            "namespace": namespace,
            "arguments": json.dumps(args),
            "call_id": call_id,
        },
    }


class TestCodexControlProvenance(unittest.TestCase):
    def setUp(self):
        self.conn = memory_db()
        self.addCleanup(self.conn.close)

    def _sync(self, window_sec=codex_control.DEFAULT_WINDOW_SEC):
        self.conn.commit()
        return codex_control.sync(self.conn, window_sec=window_sec, force=True)

    def _origin(self, sid, eid):
        row = self.conn.execute(
            "SELECT origin, evidence_kind, evidence_id FROM event_origins "
            "WHERE session_id=? AND event_id=?",
            (sid, eid)).fetchone()
        return dict(row) if row else None

    def test_mcp_codex_reply_marks_receiver_user_message_non_human(self):
        _add_user(self.conn, sid="codex:T", eid="codex:T:1", seq=0,
                  ts="2026-01-01T00:00:01Z", text="agent instruction")
        _add_raw_event(self.conn, sid="codex:S", eid="codex:S:9", seq=0,
                       ts="2026-01-01T00:00:02Z", actor="tool", typ="tool_result",
                       text="[mcp codex.codex-reply]",
                       raw=_mcp_reply_raw(ts="2026-01-01T00:00:02Z",
                                          call_id="call_reply", target="T",
                                          prompt="agent instruction"))

        stats = self._sync()

        self.assertEqual(stats["master_control"], 1)
        self.assertEqual(self._origin("codex:T", "codex:T:1"), {
            "origin": "master_control",
            "evidence_kind": codex_control.EVIDENCE_KIND,
            "evidence_id": "codex:S:9:call_reply",
        })
        sub = self.conn.execute(
            "SELECT target_session_id, payload_sha256 FROM codex_control_submissions "
            "WHERE evidence_id='codex:S:9:call_reply'"
        ).fetchone()
        self.assertEqual(sub["target_session_id"], "codex:T")
        self.assertEqual(sub["payload_sha256"], _sha("agent instruction"))

    def test_mcp_codex_start_uses_result_thread_id(self):
        _add_user(self.conn, sid="codex:NEW", eid="codex:NEW:1", seq=0,
                  ts="2026-01-01T00:00:01Z", text="start child")
        _add_raw_event(self.conn, sid="codex:S", eid="codex:S:9", seq=0,
                       ts="2026-01-01T00:00:02Z", actor="tool", typ="tool_result",
                       text="[mcp codex.codex]",
                       raw=_mcp_start_raw(ts="2026-01-01T00:00:02Z",
                                          call_id="call_start", target="NEW",
                                          prompt="start child"))

        self._sync()

        self.assertEqual(self._origin("codex:NEW", "codex:NEW:1")["origin"],
                         "master_control")

    def test_mcp_codex_tool_allows_configured_server_name(self):
        _add_user(self.conn, sid="codex:T", eid="codex:T:1", seq=0,
                  ts="2026-01-01T00:00:01Z", text="agent instruction")
        _add_raw_event(self.conn, sid="codex:S", eid="codex:S:9", seq=0,
                       ts="2026-01-01T00:00:02Z", actor="tool", typ="tool_result",
                       text="[mcp codex-prod.codex-reply]",
                       raw=_mcp_reply_raw(ts="2026-01-01T00:00:02Z",
                                          call_id="call_reply", target="T",
                                          prompt="agent instruction",
                                          server="codex-prod"))

        stats = self._sync()

        self.assertEqual(self._origin("codex:T", "codex:T:1")["origin"],
                         "master_control")
        self.assertEqual(stats["master_control"], 1)

    def test_send_input_function_call_marks_receiver_user_message_non_human(self):
        _add_user(self.conn, sid="codex:AGENT", eid="codex:AGENT:1", seq=0,
                  ts="2026-01-01T00:00:03Z", text="please review")
        _add_raw_event(self.conn, sid="codex:S", eid="codex:S:9", seq=0,
                       ts="2026-01-01T00:00:02Z", actor="assistant", typ="tool_call",
                       text='send_input: {"target":"AGENT"}',
                       raw=_send_input_raw(ts="2026-01-01T00:00:02Z",
                                           call_id="call_send", target="AGENT",
                                           message="please review"))

        self._sync()

        self.assertEqual(self._origin("codex:AGENT", "codex:AGENT:1")["origin"],
                         "master_control")

    def test_send_input_items_match_codex_receiver_text_concatenation(self):
        _add_user(self.conn, sid="codex:AGENT", eid="codex:AGENT:1", seq=0,
                  ts="2026-01-01T00:00:03Z", text="part Apart B")
        _add_raw_event(self.conn, sid="codex:S", eid="codex:S:9", seq=0,
                       ts="2026-01-01T00:00:02Z", actor="assistant", typ="tool_call",
                       text='send_input: {"target":"AGENT"}',
                       raw=_send_input_raw(
                           ts="2026-01-01T00:00:02Z",
                           call_id="call_send", target="AGENT",
                           items=[
                               {"type": "text", "text": "part A"},
                               {"type": "text", "text": "part B"},
                               {"type": "image_url", "image_url": "https://example.test/i.png"},
                           ]))

        self._sync()

        self.assertEqual(self._origin("codex:AGENT", "codex:AGENT:1")["origin"],
                         "master_control")

    def test_send_input_same_name_wrong_namespace_is_ignored(self):
        _add_user(self.conn, sid="codex:AGENT", eid="codex:AGENT:1", seq=0,
                  ts="2026-01-01T00:00:03Z", text="please review")
        _add_raw_event(self.conn, sid="codex:S", eid="codex:S:9", seq=0,
                       ts="2026-01-01T00:00:02Z", actor="assistant", typ="tool_call",
                       text='send_input: {"target":"AGENT"}',
                       raw=_send_input_raw(ts="2026-01-01T00:00:02Z",
                                           call_id="call_send", target="AGENT",
                                           message="please review",
                                           namespace="other"))

        stats = self._sync()

        self.assertIsNone(self._origin("codex:AGENT", "codex:AGENT:1"))
        self.assertEqual(stats["master_control"], 0)

    def test_legacy_send_input_without_namespace_is_still_evidence(self):
        _add_user(self.conn, sid="codex:AGENT", eid="codex:AGENT:1", seq=0,
                  ts="2026-01-01T00:00:03Z", text="please review")
        _add_raw_event(self.conn, sid="codex:S", eid="codex:S:9", seq=0,
                       ts="2026-01-01T00:00:02Z", actor="assistant", typ="tool_call",
                       text='send_input: {"target":"AGENT"}',
                       raw=_send_input_raw(ts="2026-01-01T00:00:02Z",
                                           call_id="call_send", target="AGENT",
                                           message="please review",
                                           namespace=None))

        self._sync()

        self.assertEqual(self._origin("codex:AGENT", "codex:AGENT:1")["origin"],
                         "master_control")

    def test_same_prompt_without_structured_tool_call_stays_human(self):
        _add_user(self.conn, sid="codex:T", eid="codex:T:1", seq=0,
                  ts="2026-01-01T00:00:01Z", text="agent instruction")

        stats = self._sync()

        self.assertIsNone(self._origin("codex:T", "codex:T:1"))
        self.assertEqual(stats["master_control"], 0)

    def test_repeated_identical_payload_becomes_unknown(self):
        _add_user(self.conn, sid="codex:T", eid="codex:T:1", seq=0,
                  ts="2026-01-01T00:00:01Z", text="continue")
        _add_user(self.conn, sid="codex:T", eid="codex:T:2", seq=1,
                  ts="2026-01-01T00:00:02Z", text="continue")
        _add_raw_event(self.conn, sid="codex:S", eid="codex:S:9", seq=0,
                       ts="2026-01-01T00:00:03Z", actor="tool", typ="tool_result",
                       text="[mcp codex.codex-reply]",
                       raw=_mcp_reply_raw(ts="2026-01-01T00:00:03Z",
                                          call_id="call_reply", target="T",
                                          prompt="continue"))

        stats = self._sync()

        self.assertEqual(self._origin("codex:T", "codex:T:1")["origin"], "unknown")
        self.assertEqual(self._origin("codex:T", "codex:T:2")["origin"], "unknown")
        self.assertEqual(stats["master_control"], 0)
        self.assertEqual(stats["unknown"], 2)

    def test_rebuilding_one_evidence_kind_preserves_other_effective_origin(self):
        _add_user(self.conn, sid="codex:T", eid="codex:T:1", seq=0,
                  ts="2026-01-01T00:00:01Z", text="agent instruction")
        provenance.replace_evidence_kind(self.conn, "other", [{
            "session_id": "codex:T",
            "event_id": "codex:T:1",
            "origin": "master_control",
            "evidence_kind": "other",
            "evidence_id": "other-1",
            "reason": "test evidence",
        }])
        self.conn.commit()

        self._sync()

        self.assertEqual(self._origin("codex:T", "codex:T:1"), {
            "origin": "master_control",
            "evidence_kind": "other",
            "evidence_id": "other-1",
        })

    def test_duplicate_codex_call_ids_keep_distinct_evidence_rows(self):
        _add_user(self.conn, sid="codex:A", eid="codex:A:1", seq=0,
                  ts="2026-01-01T00:00:01Z", text="prompt A")
        _add_user(self.conn, sid="codex:B", eid="codex:B:1", seq=0,
                  ts="2026-01-01T00:00:02Z", text="prompt B")
        _add_raw_event(self.conn, sid="codex:S1", eid="codex:S1:9", seq=0,
                       ts="2026-01-01T00:00:03Z", actor="tool", typ="tool_result",
                       text="[mcp codex.codex-reply]",
                       raw=_mcp_reply_raw(ts="2026-01-01T00:00:03Z",
                                          call_id="call_dup", target="A",
                                          prompt="prompt A"))
        _add_raw_event(self.conn, sid="codex:S2", eid="codex:S2:9", seq=0,
                       ts="2026-01-01T00:00:04Z", actor="tool", typ="tool_result",
                       text="[mcp codex.codex-reply]",
                       raw=_mcp_reply_raw(ts="2026-01-01T00:00:04Z",
                                          call_id="call_dup", target="B",
                                          prompt="prompt B"))

        self._sync()

        rows = self.conn.execute(
            "SELECT evidence_id, target_session_id FROM codex_control_submissions "
            "WHERE evidence_id LIKE '%call_dup' ORDER BY evidence_id"
        ).fetchall()
        self.assertEqual([(r["evidence_id"], r["target_session_id"]) for r in rows], [
            ("codex:S1:9:call_dup", "codex:A"),
            ("codex:S2:9:call_dup", "codex:B"),
        ])
        self.assertEqual(self._origin("codex:A", "codex:A:1")["evidence_id"],
                         "codex:S1:9:call_dup")
        self.assertEqual(self._origin("codex:B", "codex:B:1")["evidence_id"],
                         "codex:S2:9:call_dup")


class TestCodexControlCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "codebrain.db"
        conn = db.connect(self.db_path)
        _add_user(conn, sid="codex:T", eid="codex:T:1", seq=0,
                  ts="2026-01-01T00:00:01Z", text="my real question")
        _add_user(conn, sid="codex:T", eid="codex:T:2", seq=1,
                  ts="2026-01-01T00:00:03Z", text="agent instruction")
        _add_raw_event(conn, sid="codex:S", eid="codex:S:9", seq=0,
                       ts="2026-01-01T00:00:04Z", actor="tool", typ="tool_result",
                       text="[mcp codex.codex-reply]",
                       raw=_mcp_reply_raw(ts="2026-01-01T00:00:04Z",
                                          call_id="call_reply", target="T",
                                          prompt="agent instruction"))
        conn.commit()
        conn.close()

    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--db", str(self.db_path), *args])
        return out.getvalue()

    def _texts(self, rows):
        return {r.get("text") or r.get("last_user_text") for r in rows}

    def test_default_queries_hide_codex_control_until_overridden(self):
        human = json.loads(self.run_cli("userlog", "--no-refresh", "--json"))
        self.assertEqual(self._texts(human), {"my real question"})

        mc = json.loads(self.run_cli("userlog", "--no-refresh", "--json",
                                     "--origin", "master-control"))
        self.assertEqual(self._texts(mc), {"agent instruction"})
        self.assertEqual(mc[0]["origin"], "master_control")

        recent = json.loads(self.run_cli("recent", "--no-refresh", "--json"))
        self.assertEqual(self._texts(recent), {"my real question"})

        hits = json.loads(self.run_cli("search", "instruction", "--no-refresh",
                                       "--json", "--actor", "user"))
        self.assertEqual(hits, [])

        hits_default = json.loads(self.run_cli("search", "instruction",
                                               "--no-refresh", "--json"))
        self.assertEqual(hits_default, [])

        hits_human = json.loads(self.run_cli("search", "real",
                                             "--no-refresh", "--json"))
        self.assertEqual(self._texts(hits_human), {"my real question"})

        hits_mc = json.loads(self.run_cli("search", "instruction", "--no-refresh",
                                          "--json", "--origin", "master-control"))
        self.assertEqual(self._texts(hits_mc), {"agent instruction"})
        self.assertEqual(hits_mc[0]["origin"], "master_control")

        hits_contradictory = json.loads(self.run_cli(
            "search", "instruction", "--no-refresh", "--json",
            "--actor", "assistant", "--origin", "master-control"))
        self.assertEqual(hits_contradictory, [])


if __name__ == "__main__":
    unittest.main()

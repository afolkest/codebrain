"""Canonical Cursor snapshot adapter and structured lineage behavior."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from codebrain import cursor_archive, cursor_export
from codebrain.adapters import cursor
from tests._helpers import assert_session_invariants, by_id
from tests.test_cursor_export import _header, _modern_composer, _put, _state_db


BASE_MS = 1767225600000


def _item(bubble_id, bubble_type, text=None, created_at=None, **payload_fields):
    payload = {"bubbleId": bubble_id, "type": bubble_type}
    if text is not None:
        payload["text"] = text
    if created_at is not None:
        payload["createdAt"] = created_at
    payload.update(payload_fields)
    return {
        "bubbleId": bubble_id, "type": bubble_type,
        "createdAt": created_at, "payload": payload,
    }


def _snapshot(sid="S", order=(), created_at=BASE_MS, **session_fields):
    session = {"composerId": sid, "createdAt": created_at, "name": "Cursor title"}
    session.update(session_fields)
    return {
        "projectionVersion": 1, "composerId": sid, "sourceVersion": 17,
        "sourceCapability": "separate-bubbles", "session": session,
        "order": list(order),
    }


class TestCursorAdapter(unittest.TestCase):
    def test_messages_tool_pair_refs_and_session_metadata(self):
        user = _item(
            "user", 1, "hello", "2026-01-01T00:00:01Z",
            isSimulatedMsg=True,
        )
        assistant = _item(
            "assistant", 2, "I will run it", "2026-01-01T00:00:02Z",
            toolFormerData={
                "name": "run_terminal_command_v2", "toolCallId": "call-1",
                "status": "completed",
                "params": '{"command":"pwd","cwd":"/work"}',
                "result": '{"output":"/work\\n"}',
            },
        )
        parsed = cursor.parse_snapshot(_snapshot(
            order=(user, assistant),
            workspaceIdentifier={"uri": {"fsPath": "/work"}},
            trackedGitRepos=[{"repoPath": "/work"}],
        ), machine="mini")
        assert_session_invariants(self, parsed, "cursor")

        ids = [event.event_id for event in parsed.events]
        self.assertEqual(ids, [
            "cursor:user:1767225601000:message",
            "cursor:assistant:1767225602000:message",
            "cursor:assistant:1767225602000:call",
            "cursor:assistant:1767225602000:result",
        ])
        events = by_id(parsed)
        call = events[ids[2]]
        result = events[ids[3]]
        self.assertEqual(call.refs, {"files": [], "commands": ["pwd"]})
        self.assertEqual(call.text, "run_terminal_command_v2: pwd")
        self.assertEqual(result.text, "/work\n")
        self.assertEqual(result.tool_call_event_id, call.event_id)
        self.assertTrue(events[ids[0]].raw["isSimulatedMsg"])
        self.assertEqual(
            [placement.parent_event_id for placement in parsed.placements],
            [None, ids[0], ids[1], ids[2]],
        )
        self.assertEqual((parsed.session.cwd, parsed.session.repo), ("/work", "/work"))
        self.assertEqual(parsed.session.machine, "mini")
        self.assertEqual(parsed.session.tip_event_id, ids[-1])

    def test_terminal_statuses_and_result_rendering(self):
        tools = (
            ("done", {"status": "completed"}),
            ("failed", {
                "status": "error", "error": "source error",
                "result": '{"output":"ignored"}',
            }),
            ("cancelled", {"status": "cancelled"}),
            ("loading", {"status": "loading"}),
            ("payload", {
                "status": "loading", "result": {"contents": ["a", "b"]},
            }),
        )
        order = []
        for index, (bubble_id, fields) in enumerate(tools, 1):
            order.append(_item(
                bubble_id, 2, "", f"2026-01-01T00:00:0{index}Z",
                toolFormerData={
                    "name": "unknown_tool", "toolCallId": f"call-{index}",
                    **fields,
                },
            ))
        parsed = cursor.parse_snapshot(_snapshot(order=order))
        assert_session_invariants(self, parsed, "cursor")
        results = [event for event in parsed.events if event.type == "tool_result"]
        self.assertEqual([event.text for event in results], [
            "", "source error", "", '["a","b"]',
        ])
        self.assertEqual(
            len([event for event in parsed.events if event.type == "tool_call"]), 5
        )
        self.assertNotIn("cursor:loading:1767225604000:result", by_id(parsed))

    def test_exact_structured_ref_mappings_do_not_guess_unknown_paths(self):
        cases = (
            ("read_file_v2", {
                "targetFile": "/work/a.py", "effectiveUri": "file:///work/a.py",
            }),
            ("read_lints", {"paths": ["a.py", "b.py", "a.py"]}),
            ("ripgrep_raw_search", {"path": "src", "pattern": "needle"}),
            ("mcp-private-tool", {"path": "/must/not/infer", "command": "nope"}),
        )
        order = []
        for index, (name, args) in enumerate(cases, 1):
            order.append(_item(
                f"b{index}", 2, "", f"2026-01-01T00:00:0{index}Z",
                toolFormerData={
                    "name": name, "toolCallId": f"c{index}", "status": "loading",
                    "params": args,
                },
            ))
        parsed = cursor.parse_snapshot(_snapshot(order=order))
        calls = [event for event in parsed.events if event.type == "tool_call"]
        self.assertEqual(calls[0].refs["files"], [
            "/work/a.py", "file:///work/a.py",
        ])
        self.assertEqual(calls[1].refs["files"], ["a.py", "b.py"])
        self.assertEqual(calls[2].refs["files"], ["src"])
        self.assertEqual(calls[3].refs, {"files": [], "commands": []})
        self.assertIn('"path":"/must/not/infer"', calls[3].text)

        fallback = _item(
            "shell", 2, "", "2026-01-01T00:00:05Z",
            toolFormerData={
                "name": "run_terminal_cmd", "toolCallId": "shell-call",
                "status": "loading", "params": {},
                "rawArgs": '{"command":"git status"}',
            },
        )
        fallback_call = cursor.parse_snapshot(
            _snapshot(order=(fallback,))
        ).events[0]
        self.assertEqual(fallback_call.refs["commands"], ["git status"])

    def test_copied_prefix_ids_inheritance_equality_and_branch_point(self):
        copied_parent = _item("same", 1, "shared", "2026-01-01T00:00:01Z")
        copied_child = _item(
            "same", 1, "shared", "2025-12-31T16:00:01-08:00"
        )
        native = _item("native", 1, "new", "2026-01-01T00:00:02Z")
        parent = cursor.parse_snapshot(_snapshot("P", (copied_parent,)))
        child = cursor.parse_snapshot(_snapshot(
            "C", (copied_child, native), created_at=BASE_MS + 2000
        ))
        assert_session_invariants(self, parent, "cursor")
        assert_session_invariants(self, child, "cursor")
        self.assertEqual(parent.events[0].event_id, child.events[0].event_id)
        self.assertEqual(child.placements[0].inherited, 1)
        self.assertIsNone(child.events[0].origin_session_id)
        self.assertEqual(child.placements[1].inherited, 0)
        self.assertEqual(child.events[1].origin_session_id, "cursor:C")
        self.assertEqual(child.session.branch_point_event_id, child.events[0].event_id)

    def test_reused_bubble_id_with_different_time_is_distinct(self):
        first = cursor.parse_snapshot(_snapshot(
            "A", (_item("reused", 1, "same", "2026-01-01T00:00:01Z"),)
        ))
        second = cursor.parse_snapshot(_snapshot(
            "B", (_item("reused", 1, "same", "2026-01-01T00:00:02Z"),)
        ))
        self.assertNotEqual(first.events[0].event_id, second.events[0].event_id)

    def test_non_prefix_inheritance_fails_closed(self):
        native = _item("native", 1, "new", "2026-01-01T00:00:03Z")
        stale = _item("stale", 1, "old", "2026-01-01T00:00:01Z")
        with self.assertRaises(cursor.CursorAdapterError):
            cursor.parse_snapshot(_snapshot(
                "C", (native, stale), created_at=BASE_MS + 2000
            ))

    def test_untimed_fallback_is_session_scoped_encoded_and_not_inherited(self):
        item = _item("old:bubble", 1, "legacy")
        first = cursor.parse_snapshot(_snapshot("odd\ncomposer", (item,)))
        second = cursor.parse_snapshot(_snapshot("other", (item,)))
        with_prefix = cursor.parse_snapshot(_snapshot(
            "odd\ncomposer", (_item("earlier", 1, "x"), item)
        ))
        assert_session_invariants(self, first, "cursor")
        self.assertNotEqual(first.events[0].event_id, second.events[0].event_id)
        self.assertEqual(
            first.events[0].event_id,
            "cursor:odd%0Acomposer:old%3Abubble:untimed:message",
        )
        self.assertEqual(first.events[0].event_id, with_prefix.events[1].event_id)
        self.assertNotIn("\n", first.events[0].event_id)
        self.assertEqual(first.placements[0].inherited, 0)
        self.assertEqual(first.events[0].origin_session_id, "cursor:odd\ncomposer")
        self.assertEqual(first.events[0].ts, "2026-01-01T00:00:00.000Z")

    def test_thought_summary_and_empty_snapshot_emit_no_events(self):
        order = (
            _item(
                "thought", 2, "secret", None, isThought=True,
                toolFormerData={
                    "name": "read_file_v2", "toolCallId": "hidden-call",
                    "status": "completed", "params": {"targetFile": "/secret"},
                    "result": "secret result",
                },
            ),
            _item("summary", 2, "secret", None, isSummarization=True),
        )
        parsed = cursor.parse_snapshot(_snapshot(order=order))
        assert_session_invariants(self, parsed, "cursor")
        self.assertEqual((parsed.events, parsed.placements), ([], []))
        self.assertIsNone(parsed.session.tip_event_id)

    def test_embedded_projection_uses_the_same_canonical_contract(self):
        snapshot = _snapshot(
            "legacy", (_item("embedded-0", 1, "old"),), created_at=BASE_MS
        )
        snapshot["sourceVersion"] = 1
        snapshot["sourceCapability"] = "embedded"
        parsed = cursor.parse_snapshot(snapshot)
        assert_session_invariants(self, parsed, "cursor")
        self.assertEqual(parsed.events[0].text, "old")
        self.assertIn(":untimed:message", parsed.events[0].event_id)

    def test_parse_file_reads_reconstructed_archive_head(self):
        root = Path(tempfile.mkdtemp())
        head = cursor_archive.publish_snapshot(
            _snapshot(order=(_item(
                "u", 1, "from archive", "2026-01-01T00:00:01Z"
            ),)),
            root,
        )
        parsed = cursor.parse_file(head, machine="pool-host")
        assert_session_invariants(self, parsed, "cursor")
        self.assertEqual(parsed.events[0].text, "from archive")
        self.assertEqual(parsed.session.machine, "pool-host")

    def test_invalid_projection_fails_closed(self):
        with self.assertRaises(cursor.CursorAdapterError):
            cursor.parse_snapshot({"projectionVersion": 2})
        bad = _snapshot(order=({"bubbleId": "bad", "type": 3, "payload": {}},))
        with self.assertRaises(cursor.CursorAdapterError):
            cursor.parse_snapshot(bad)
        boolean_type = _item("bool", True, "not a numeric type")
        with self.assertRaises(cursor.CursorAdapterError):
            cursor.parse_snapshot(_snapshot(order=(boolean_type,)))
        duplicate = _item("same", 1, "x", "2026-01-01T00:00:01Z")
        with self.assertRaises(cursor.CursorAdapterError):
            cursor.parse_snapshot(_snapshot(order=(duplicate, duplicate)))
        empty = _item("", 1, "x", "2026-01-01T00:00:01Z")
        with self.assertRaises(cursor.CursorAdapterError):
            cursor.parse_snapshot(_snapshot(order=(empty,)))


class TestCursorSpawnResolution(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.path = self.root / "state.vscdb"
        self.writer = _state_db(self.path)
        self.addCleanup(self.writer.close)

    def test_structured_parent_call_resolves_child_spawn_event(self):
        _header(self.writer, "parent")
        _header(self.writer, "child", isSubagent=1)
        parent = _modern_composer("parent", ("spawn",))
        parent["fullConversationHeadersOnly"][0]["type"] = 2
        child = _modern_composer("child", ("input",), subagentInfo={
            "parentComposerId": "parent", "toolCallId": "task-call",
        })
        _put(self.writer, "composerData:parent", parent)
        _put(self.writer, "composerData:child", child)
        _put(self.writer, "bubbleId:parent:spawn", {
            "bubbleId": "spawn", "type": 2, "text": "",
            "createdAt": "2026-01-01T00:00:01Z",
            "toolFormerData": {
                "name": "task_v2", "toolCallId": "task-call",
                "status": "completed", "params": '{"prompt":"work"}',
                "additionalData": {"subagentComposerId": "child"},
            },
        })
        _put(self.writer, "bubbleId:child:input", {
            "bubbleId": "input", "type": 1, "text": "work",
            "createdAt": "2026-01-01T00:00:02Z",
        })
        self.writer.commit()

        snapshot = cursor_export.read_session_snapshot(self.path, "child")
        info = snapshot["session"]["subagentInfo"]
        self.assertEqual((info["spawnBubbleId"], info["spawnCreatedAt"]), (
            "spawn", "2026-01-01T00:00:01Z",
        ))
        parsed = cursor.parse_snapshot(snapshot)
        assert_session_invariants(self, parsed, "cursor")
        self.assertEqual(parsed.session.parent_session_id, "cursor:parent")
        self.assertEqual(parsed.session.relation, "subagent")
        self.assertEqual(
            parsed.session.spawn_event_id,
            "cursor:spawn:1767225601000:call",
        )

    def test_parent_identity_survives_when_spawn_is_unresolved(self):
        snapshot = _snapshot(
            "child", (), subagentInfo={
                "parentComposerId": "parent", "toolCallId": "missing",
            },
        )
        parsed = cursor.parse_snapshot(snapshot)
        self.assertEqual(parsed.session.parent_session_id, "cursor:parent")
        self.assertEqual(parsed.session.relation, "subagent")
        self.assertIsNone(parsed.session.spawn_event_id)

    def test_parent_tool_without_emittable_name_does_not_resolve(self):
        _header(self.writer, "parent")
        _header(self.writer, "child", isSubagent=1)
        parent = _modern_composer("parent", ("spawn",))
        parent["fullConversationHeadersOnly"][0]["type"] = 2
        child = _modern_composer("child", (), subagentInfo={
            "parentComposerId": "parent", "toolCallId": "task-call",
        })
        _put(self.writer, "composerData:parent", parent)
        _put(self.writer, "composerData:child", child)
        _put(self.writer, "bubbleId:parent:spawn", {
            "bubbleId": "spawn", "type": 2,
            "createdAt": "2026-01-01T00:00:01Z",
            "toolFormerData": {
                "toolCallId": "task-call", "status": "completed",
                "additionalData": {"subagentComposerId": "child"},
            },
        })
        self.writer.commit()
        snapshot = cursor_export.read_session_snapshot(self.path, "child")
        self.assertNotIn("spawnBubbleId", snapshot["session"]["subagentInfo"])
        self.assertIsNone(cursor.parse_snapshot(snapshot).session.spawn_event_id)

        _put(self.writer, "bubbleId:parent:spawn", {
            "bubbleId": "spawn", "type": 2, "isThought": True,
            "createdAt": "2026-01-01T00:00:01Z",
            "toolFormerData": {
                "name": "task_v2", "toolCallId": "task-call",
                "status": "completed",
                "additionalData": {"subagentComposerId": "child"},
            },
        })
        self.writer.commit()
        hidden = cursor_export.read_session_snapshot(self.path, "child")
        self.assertNotIn("spawnBubbleId", hidden["session"]["subagentInfo"])

        _put(self.writer, "bubbleId:parent:spawn", {
            "bubbleId": "spawn", "type": 3,
            "createdAt": "2026-01-01T00:00:01Z",
            "toolFormerData": {
                "name": "task_v2", "toolCallId": "task-call",
                "status": "completed",
                "additionalData": {"subagentComposerId": "child"},
            },
        })
        self.writer.commit()
        malformed = cursor_export.read_session_snapshot(self.path, "child")
        self.assertNotIn("spawnBubbleId", malformed["session"]["subagentInfo"])


if __name__ == "__main__":
    unittest.main()

"""Codex adapter — synthesized turn forest, thread_rolled_back popping (incl. the
C1 full-rollback null-tip case), MCP result capture/dedup, apply_patch refs."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codebrain import codex_control, ingest
from codebrain.adapters import codex
from tests._helpers import (
    assert_session_invariants,
    by_id,
    live_ids,
    memory_db,
    write_jsonl,
)

UUID = "0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
FRAGMENT = "0199ffff-1111-2222-3333-444444444444"
PARENT_FRAGMENT = "01998888-1111-2222-3333-444444444444"


def _meta(ts="2026-01-01T00:00:00Z", history_base=None, ordinal=None):
    record = {"type": "session_meta", "timestamp": ts,
              "payload": {"id": UUID, "cwd": "/work", "timestamp": ts}}
    if history_base is not None:
        record["payload"]["history_base"] = history_base
    if ordinal is not None:
        record["ordinal"] = ordinal
    return record


def _with_ordinals(records, start):
    return [{**record, "ordinal": start + index}
            for index, record in enumerate(records)]


def _user(text, ts):
    return {"type": "event_msg", "timestamp": ts,
            "payload": {"type": "user_message", "message": text}}


def _completed_user(text, ts):
    return {"type": "event_msg", "timestamp": ts,
            "payload": {"type": "item_completed", "turn_id": "turn-1",
                        "item": {"type": "UserMessage", "id": "user-item-1",
                                 "content": [{"type": "text", "text": text,
                                              "text_elements": []}]}}}


def _assistant(text, ts):
    return {"type": "response_item", "timestamp": ts,
            "payload": {"type": "message", "role": "assistant",
                        "content": [{"type": "text", "text": text}]}}


def _call(name, args, call_id, ts):
    return {"type": "response_item", "timestamp": ts,
            "payload": {"type": "function_call", "name": name, "arguments": args, "call_id": call_id}}


def _output(call_id, out, ts):
    return {"type": "response_item", "timestamp": ts,
            "payload": {"type": "function_call_output", "call_id": call_id, "output": out}}


def _rollback(n, ts):
    return {"type": "event_msg", "timestamp": ts,
            "payload": {"type": "thread_rolled_back", "num_turns": n}}


def _inter_agent_meta(trigger_turn=True, ts="2026-01-01T00:00:02Z"):
    return {"type": "inter_agent_communication_metadata", "timestamp": ts,
            "payload": {"trigger_turn": trigger_turn}}


def _inter_agent_message(text, ts):
    return {"type": "response_item", "timestamp": ts,
            "payload": {"type": "agent_message", "author": "/root/A",
                        "recipient": "/root/B",
                        "content": [{"type": "input_text", "text": text}]}}


def _event_agent_message(text, ts):
    return {"type": "event_msg", "timestamp": ts,
            "payload": {"type": "agent_message", "message": text, "phase": "commentary"}}


def _legacy_inter_agent_communication(text, ts, trigger_turn=False):
    return {"type": "inter_agent_communication", "timestamp": ts,
            "payload": {"author": "/root/A", "recipient": "/root/B",
                        "other_recipients": [], "content": text,
                        "trigger_turn": trigger_turn}}


def eid(line):  # event_id = codex:<uuid>:<0-based line>
    return f"codex:{UUID}:{line}"


def _write_rollover_root(codex_root: Path) -> Path:
    session_dir = codex_root / "sessions" / "2026" / "01" / "01"
    session_dir.mkdir(parents=True, exist_ok=True)
    root_history = {
        "thread_id": PARENT_FRAGMENT,
        "end_ordinal_exclusive": 100,
        "end_byte_offset": 999,
    }
    records = [
        {**_meta(history_base=root_history), "ordinal": 100},
        {**_completed_user("first", "2026-01-01T00:00:01Z"), "ordinal": 101},
        {**_call("apply_patch", '{"input":"*** Update File: old.py"}', "c1",
                 "2026-01-01T00:00:02Z"), "ordinal": 102},
        {"type": "event_msg", "timestamp": "2026-01-01T00:00:03Z",
         "ordinal": 103, "payload": {"type": "token_count"}},
        {"type": "event_msg", "timestamp": "2026-01-01T00:00:04Z",
         "ordinal": 103, "payload": {"type": "thread_settings_applied"}},
        {**_completed_user("superseded", "2026-01-01T00:00:05Z"),
         "ordinal": 104},
        {**_assistant("aborted answer", "2026-01-01T00:00:06Z"),
         "ordinal": 105},
    ]
    return write_jsonl(
        session_dir, f"rollout-2026-01-01T00-00-00-{UUID}.jsonl", records,
    )


def _write_rollover_successor(codex_root: Path, first: Path,
                              *, byte_delta: int = 0) -> Path:
    with first.open("rb") as handle:
        committed_bytes = sum(len(line) for line in handle.readlines()[:5])
    history = {
        "thread_id": UUID,
        "end_ordinal_exclusive": 104,
        "end_byte_offset": committed_bytes + byte_delta,
    }
    records = [
        {**_meta("2026-01-01T00:00:07Z", history_base=history), "ordinal": 104},
        {**_output("c1", "patched", "2026-01-01T00:00:08Z"), "ordinal": 105},
        {"type": "event_msg", "timestamp": "2026-01-01T00:00:09Z",
         "ordinal": 106,
         "payload": {"type": "patch_apply_end", "call_id": "c1",
                     "changes": {"/work/new.py": {"kind": "update"}}}},
        {**_assistant("done", "2026-01-01T00:00:10Z"), "ordinal": 107},
    ]
    return write_jsonl(
        first.parent,
        f"rollout-2026-01-01T00-00-07-{UUID}_{FRAGMENT}.jsonl",
        records,
    )


class TestCodex(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def parse(self, records):
        p = write_jsonl(self.dir, "rollout.jsonl", records)
        parsed = codex.parse_file(p, machine="m1")
        assert parsed is not None, "adapter unexpectedly returned None"
        return parsed

    def test_linear_turn(self):
        parsed = self.parse([
            _meta(),                                                              # line 0
            _user("do X", "2026-01-01T00:00:01Z"),                                # line 1
            _assistant("ok", "2026-01-01T00:00:02Z"),                             # line 2
            _call("shell", '{"command":["ls"]}', "c1", "2026-01-01T00:00:03Z"),   # line 3
            _output("c1", "file.txt", "2026-01-01T00:00:04Z"),                    # line 4
        ])
        assert_session_invariants(self, parsed, "codex")
        self.assertEqual(parsed.session.session_id, f"codex:{UUID}")
        self.assertEqual(parsed.session.cwd, "/work")
        self.assertEqual([(e.actor, e.type) for e in parsed.events],
                         [("user", "message"), ("assistant", "message"),
                          ("assistant", "tool_call"), ("tool", "tool_result")])
        ev = by_id(parsed)
        self.assertEqual(ev[eid(4)].tool_call_event_id, eid(3))   # paired by call_id
        self.assertEqual(ev[eid(3)].refs["commands"], ["ls"])
        self.assertEqual(live_ids(parsed), {eid(1), eid(2), eid(3), eid(4)})
        self.assertEqual(parsed.session.tip_event_id, eid(4))

    def test_item_completed_user_message_is_the_structured_canonical_prompt(self):
        parsed = self.parse([
            _meta(),                                                          # line 0
            # Model-facing user records can contain source-generated context or a
            # duplicate of the prompt. Neither is transcript truth for retrieval.
            {"type": "response_item", "timestamp": "2026-01-01T00:00:01Z",
             "payload": {"type": "message", "role": "user",
                         "internal_chat_message_metadata_passthrough": {
                             "content_item_kinds": ["agents_md.instructions"]},
                         "content": [{"type": "input_text",
                                      "text": "injected context"}]}},         # line 1
            {"type": "response_item", "timestamp": "2026-01-01T00:00:02Z",
             "payload": {"type": "message", "role": "user",
                         "internal_chat_message_metadata_passthrough": {
                             "content_item_kinds": ["user.text"]},
                         "content": [{"type": "input_text",
                                      "text": "do the new thing"}]}},         # line 2
            {"type": "event_msg", "timestamp": "2026-01-01T00:00:02Z",
             "payload": {"type": "item_completed", "turn_id": "turn-1",
                         "item": {"type": "AgentMessage", "id": "agent-item-1",
                                  "content": [{"type": "Text",
                                               "text": "not user input"}]}}},  # line 3
            _completed_user("do the new thing", "2026-01-01T00:00:02Z"),    # line 4
            _assistant("ok", "2026-01-01T00:00:03Z"),                       # line 5
        ])
        assert_session_invariants(self, parsed, "codex")
        self.assertEqual([(e.actor, e.text) for e in parsed.events],
                         [("user", "do the new thing"), ("assistant", "ok")])
        self.assertEqual(parsed.events[0].event_id, eid(4))
        self.assertEqual(parsed.events[0].raw["payload"]["item"]["type"],
                         "UserMessage")
        self.assertEqual(live_ids(parsed), {eid(4), eid(5)})

    def test_paginated_rollouts_use_structured_cuts_and_session_line_ids(self):
        # This logical session itself forked from another thread, so its source
        # ordinals begin at 100 rather than zero. The first physical file still
        # owns local event ids 1/2; a same-session continuation must normalize
        # later source ordinals relative to that root without changing them.
        root_dir = Path(self.dir) / "sessions" / "2026" / "01" / "01"
        root_dir.mkdir(parents=True)
        root_history = {
            "thread_id": PARENT_FRAGMENT,
            "end_ordinal_exclusive": 100,
            "end_byte_offset": 999,
        }
        first_records = _with_ordinals([
            _meta(history_base=root_history),                              # local 0
            _completed_user("first", "2026-01-01T00:00:01Z"),            # local 1
            _assistant("first answer", "2026-01-01T00:00:02Z"),          # local 2
            {"type": "event_msg", "timestamp": "2026-01-01T00:00:03Z",
             "payload": {"type": "task_started"}},                      # cut begins
            _completed_user("superseded", "2026-01-01T00:00:04Z"),       # excluded
            _assistant("aborted answer", "2026-01-01T00:00:05Z"),        # excluded
        ], 100)
        first = write_jsonl(
            root_dir,
            f"rollout-2026-01-01T00-00-00-{UUID}.jsonl",
            first_records,
        )
        with first.open("rb") as handle:
            committed_bytes = sum(len(line) for line in handle.readlines()[:3])

        second_history = {
            "thread_id": UUID,
            "end_ordinal_exclusive": 103,
            "end_byte_offset": committed_bytes,
        }
        second_records = _with_ordinals([
            _meta("2026-01-01T00:00:06Z", history_base=second_history),    # logical 3
            _completed_user("second", "2026-01-01T00:00:07Z"),           # logical 4
            _assistant("second answer", "2026-01-01T00:00:08Z"),         # logical 5
            _rollback(1, "2026-01-01T00:00:09Z"),                        # pops second
            _completed_user("third", "2026-01-01T00:00:10Z"),            # logical 7
            _assistant("third answer", "2026-01-01T00:00:11Z"),          # logical 8
        ], 103)
        second = write_jsonl(
            root_dir,
            f"rollout-2026-01-01T00-00-06-{UUID}_{FRAGMENT}.jsonl",
            second_records,
        )

        groups = codex.group_rollout_files([second, first])
        self.assertEqual(len(groups), 1)
        parsed = codex.parse_rollout_group(groups[0], machine="m1")
        assert parsed is not None
        assert_session_invariants(self, parsed, "codex")
        ev = by_id(parsed)

        self.assertEqual([e.text for e in parsed.events],
                         ["first", "first answer", "second", "second answer",
                          "third", "third answer"])
        self.assertNotIn("superseded", [e.text for e in parsed.events])
        self.assertEqual(set(ev), {eid(1), eid(2), eid(4), eid(5), eid(7), eid(8)})
        self.assertEqual(live_ids(parsed), {eid(1), eid(2), eid(7), eid(8)})
        parent = {p.event_id: p.parent_event_id for p in parsed.placements}
        self.assertEqual(parent[eid(4)], eid(2))
        self.assertEqual(parent[eid(7)], eid(2))
        self.assertEqual(parsed.session.tip_event_id, eid(8))

    def test_paginated_duplicate_control_ordinal_and_cross_fragment_tool_pair(self):
        codex_root = Path(self.dir) / ".codex"
        first = _write_rollover_root(codex_root)
        second = _write_rollover_successor(codex_root, first)

        parsed = codex.parse_rollout_group(
            codex.group_rollout_files([second, first])[0], machine="m1",
        )
        assert parsed is not None
        assert_session_invariants(self, parsed, "codex")
        events = by_id(parsed)
        self.assertEqual(set(events), {eid(1), eid(2), eid(6), eid(8)})
        self.assertNotIn("superseded", [event.text for event in parsed.events])
        self.assertEqual(events[eid(6)].tool_call_event_id, eid(2))
        self.assertEqual(events[eid(2)].refs["files"], ["old.py", "/work/new.py"])

    def test_paginated_chain_fails_closed_on_invalid_byte_cut(self):
        codex_root = Path(self.dir) / ".codex"
        first = _write_rollover_root(codex_root)
        second = _write_rollover_successor(codex_root, first, byte_delta=1)
        group = codex.group_rollout_files([first, second])[0]
        with self.assertRaisesRegex(ValueError, "byte boundary mismatch"):
            codex.parse_rollout_group(group, machine="m1")

    def test_paginated_chain_requires_root_fragment(self):
        codex_root = Path(self.dir) / ".codex"
        first = _write_rollover_root(codex_root)
        second = _write_rollover_successor(codex_root, first)
        group = codex.group_rollout_files([second])[0]
        with self.assertRaisesRegex(ValueError, "missing its root fragment"):
            codex.parse_rollout_group(group, machine="m1")

    def test_paginated_divergent_duplicate_fragment_fails_closed(self):
        codex_root = Path(self.dir) / ".codex"
        first = _write_rollover_root(codex_root)
        second = _write_rollover_successor(codex_root, first)
        duplicate_dir = Path(self.dir) / "archive"
        duplicate_dir.mkdir()
        duplicate = write_jsonl(
            duplicate_dir, second.name,
            [
                {**_meta("2026-01-01T00:00:07Z", history_base={
                    "thread_id": UUID,
                    "end_ordinal_exclusive": 104,
                    "end_byte_offset": codex._fragment_header(
                        second).previous_end_byte_offset,
                }), "ordinal": 104},
                {**_output("c1", "DIFFER", "2026-01-01T00:00:08Z"),
                 "ordinal": 105},
            ],
        )
        group = codex.group_rollout_files([first, second, duplicate])[0]
        with self.assertRaisesRegex(ValueError, "divergent physical copies"):
            codex.parse_rollout_group(group, machine="m1")

    def test_paginated_exact_prefix_duplicate_uses_append_authoritative_copy(self):
        codex_root = Path(self.dir) / ".codex"
        first = _write_rollover_root(codex_root)
        second = _write_rollover_successor(codex_root, first)
        duplicate_dir = Path(self.dir) / "archive"
        duplicate_dir.mkdir()
        with second.open(encoding="utf-8") as handle:
            prefix_records = [json.loads(line) for line in handle.readlines()[:2]]
        duplicate = write_jsonl(duplicate_dir, second.name, prefix_records)

        parsed = codex.parse_rollout_group(
            codex.group_rollout_files([duplicate, first, second])[0], machine="m1",
        )
        assert parsed is not None
        self.assertEqual([event.text for event in parsed.events],
                         ["first", "apply_patch: old.py", "patched", "done"])

    def test_paginated_duplicate_ordinal_may_not_emit_two_events(self):
        codex_root = Path(self.dir) / ".codex"
        session_dir = codex_root / "sessions" / "2026" / "01" / "01"
        session_dir.mkdir(parents=True)
        root = write_jsonl(session_dir,
            f"rollout-2026-01-01T00-00-00-{UUID}.jsonl", [
                {**_meta(), "ordinal": 100},
                {**_completed_user("one", "2026-01-01T00:00:01Z"),
                 "ordinal": 101},
                {**_completed_user("two", "2026-01-01T00:00:02Z"),
                 "ordinal": 101},
            ])
        with root.open("rb") as handle:
            boundary = sum(len(line) for line in handle)
        successor = write_jsonl(session_dir,
            f"rollout-2026-01-01T00-00-03-{UUID}_{FRAGMENT}.jsonl", [
                {**_meta("2026-01-01T00:00:03Z", history_base={
                    "thread_id": UUID,
                    "end_ordinal_exclusive": 102,
                    "end_byte_offset": boundary,
                }), "ordinal": 102},
                {**_assistant("answer", "2026-01-01T00:00:04Z"),
                 "ordinal": 103},
            ])
        group = codex.group_rollout_files([root, successor])[0]
        with self.assertRaisesRegex(ValueError, "emits multiple events"):
            codex.parse_rollout_group(group, machine="m1")


    def test_full_rollback_yields_null_tip(self):
        # C1 regression: a session rolled all the way back has NO live tip. The dead
        # tail must NOT be resurrected as live (that produced a live orphan w/ dead parent).
        parsed = self.parse([
            _meta(),                                          # line 0
            _user("first", "2026-01-01T00:00:01Z"),           # line 1
            _assistant("a1", "2026-01-01T00:00:02Z"),         # line 2
            _rollback(1, "2026-01-01T00:00:03Z"),             # line 3 -> pops the only turn
        ])
        assert_session_invariants(self, parsed, "codex")
        self.assertIsNone(parsed.session.tip_event_id)
        self.assertEqual(live_ids(parsed), set())             # nothing live
        self.assertEqual(len(parsed.events), 2)               # but events are retained

    def test_partial_rollback_reverts_tip(self):
        parsed = self.parse([
            _meta(),                                          # 0
            _user("first", "2026-01-01T00:00:01Z"),           # 1  turn A
            _assistant("a1", "2026-01-01T00:00:02Z"),         # 2
            _user("second", "2026-01-01T00:00:03Z"),          # 3  turn B
            _assistant("a2", "2026-01-01T00:00:04Z"),         # 4
            _rollback(1, "2026-01-01T00:00:05Z"),             # 5  pops turn B
            _user("third", "2026-01-01T00:00:06Z"),           # 6  turn C, parents to reverted tip
            _assistant("a3", "2026-01-01T00:00:07Z"),         # 7
        ])
        assert_session_invariants(self, parsed, "codex")
        live, dead = live_ids(parsed), {eid(3), eid(4)}
        self.assertEqual(live, {eid(1), eid(2), eid(6), eid(7)})
        self.assertTrue(dead.isdisjoint(live))               # turn B is dead
        # the new turn re-anchors on the reverted tip (a1 @ line 2), not on dead a2
        parent = {p.event_id: p.parent_event_id for p in parsed.placements}
        self.assertEqual(parent[eid(6)], eid(2))
        self.assertEqual(parsed.session.tip_event_id, eid(7))

    def test_mcp_result_captured_and_deduped(self):
        parsed = self.parse([
            _meta(),                                          # 0
            _user("go", "2026-01-01T00:00:01Z"),              # 1
            # m1: standalone MCP end with no function_output -> captured as a tool_result
            {"type": "event_msg", "timestamp": "2026-01-01T00:00:02Z",
             "payload": {"type": "mcp_tool_call_end", "call_id": "m1",
                         "invocation": {"server": "srv", "tool": "do"},
                         "result": {"Ok": {"content": [{"type": "text", "text": "RESULT"}]}}}},  # 2
            # m2: MCP end that ALSO has a *_output below -> must be deduped (not captured here)
            {"type": "event_msg", "timestamp": "2026-01-01T00:00:03Z",
             "payload": {"type": "mcp_tool_call_end", "call_id": "m2",
                         "invocation": {"server": "srv", "tool": "two"},
                         "result": {"Ok": {"content": [{"type": "text", "text": "X"}]}}}},        # 3
            {"type": "response_item", "timestamp": "2026-01-01T00:00:04Z",
             "payload": {"type": "custom_tool_call_output", "call_id": "m2", "output": "done"}},   # 4
        ])
        assert_session_invariants(self, parsed, "codex")
        ev = by_id(parsed)
        # m1 captured with the [mcp server.tool] prefix and body
        self.assertIn(eid(2), ev)
        self.assertTrue(ev[eid(2)].text.startswith("[mcp srv.do]"))
        self.assertIn("RESULT", ev[eid(2)].text)
        # m2's mcp_end (line 3) was deduped away; only the output (line 4) became an event
        self.assertNotIn(eid(3), ev)
        self.assertEqual(ev[eid(4)].text, "done")
        # exactly two tool_results: the m1 MCP capture + the m2 output
        self.assertEqual(sum(1 for e in parsed.events if e.type == "tool_result"), 2)

    def test_apply_patch_refs_enriched(self):
        parsed = self.parse([
            _meta(),                                          # 0
            _user("patch it", "2026-01-01T00:00:01Z"),        # 1
            {"type": "response_item", "timestamp": "2026-01-01T00:00:02Z",
             "payload": {"type": "custom_tool_call", "name": "apply_patch",
                         "input": "*** Add File: foo.py\n+x\n", "call_id": "p1"}},   # 2
            {"type": "event_msg", "timestamp": "2026-01-01T00:00:03Z",
             "payload": {"type": "patch_apply_end", "call_id": "p1",
                         "changes": {"/abs/foo.py": {"add": 1}}}},                   # 3
        ])
        assert_session_invariants(self, parsed, "codex")
        ev = by_id(parsed)
        self.assertTrue(ev[eid(2)].text.startswith("apply_patch: foo.py"))
        # the marker path AND the patch_apply_end absolute path both land in refs
        self.assertEqual(ev[eid(2)].refs["files"], ["foo.py", "/abs/foo.py"])

    def test_inter_agent_message_is_not_user_input(self):
        parsed = self.parse([
            _meta(),                                                  # 0
            _user("human prompt", "2026-01-01T00:00:01Z"),            # 1
            _inter_agent_meta(trigger_turn=True),                     # 2
            _inter_agent_message("agent handoff", "2026-01-01T00:00:03Z"),  # 3
        ])
        assert_session_invariants(self, parsed, "codex")
        ev = by_id(parsed)
        self.assertEqual(ev[eid(3)].actor, "assistant")
        self.assertEqual(ev[eid(3)].type, "message")
        self.assertEqual(ev[eid(3)].text, "agent handoff")
        self.assertEqual(ev[eid(3)].refs["inter_agent"]["author"], "/root/A")
        self.assertEqual(sum(1 for e in parsed.events if e.actor == "user"), 1)
        parent = {p.event_id: p.parent_event_id for p in parsed.placements}
        self.assertEqual(parent[eid(3)], eid(1))

    def test_inter_agent_trigger_turn_rolls_back_without_dropping_human_turn(self):
        parsed = self.parse([
            _meta(),                                               # 0
            _user("human prompt", "2026-01-01T00:00:01Z"),         # 1
            _assistant("ok", "2026-01-01T00:00:02Z"),              # 2
            _inter_agent_meta(trigger_turn=True, ts="2026-01-01T00:00:03Z"),  # 3
            _inter_agent_message("agent task", "2026-01-01T00:00:04Z"),      # 4
            _assistant("agent answer", "2026-01-01T00:00:05Z"),    # 5
            _rollback(1, "2026-01-01T00:00:06Z"),                  # 6
        ])
        assert_session_invariants(self, parsed, "codex")
        self.assertEqual(live_ids(parsed), {eid(1), eid(2)})

    def test_inter_agent_non_trigger_turn_rolls_back_without_dropping_human_turn(self):
        parsed = self.parse([
            _meta(),                                               # 0
            _user("human prompt", "2026-01-01T00:00:01Z"),         # 1
            _assistant("ok", "2026-01-01T00:00:02Z"),              # 2
            _inter_agent_meta(trigger_turn=False, ts="2026-01-01T00:00:03Z"),  # 3
            _inter_agent_message("agent note", "2026-01-01T00:00:04Z"),       # 4
            _assistant("agent answer", "2026-01-01T00:00:05Z"),    # 5
            _rollback(1, "2026-01-01T00:00:06Z"),                  # 6
        ])
        assert_session_invariants(self, parsed, "codex")
        self.assertEqual(live_ids(parsed), {eid(1), eid(2)})

    def test_legacy_inter_agent_communication_is_non_user_rollback_boundary(self):
        parsed = self.parse([
            _meta(),                                               # 0
            _user("human prompt", "2026-01-01T00:00:01Z"),         # 1
            _assistant("ok", "2026-01-01T00:00:02Z"),              # 2
            _legacy_inter_agent_communication(
                "legacy agent note", "2026-01-01T00:00:03Z"),      # 3
            _assistant("agent answer", "2026-01-01T00:00:04Z"),    # 4
            _rollback(1, "2026-01-01T00:00:05Z"),                  # 5
        ])
        assert_session_invariants(self, parsed, "codex")
        ev = by_id(parsed)
        self.assertEqual(ev[eid(3)].actor, "assistant")
        self.assertEqual(ev[eid(3)].refs["inter_agent"]["trigger_turn"], False)
        self.assertEqual(sum(1 for e in parsed.events if e.actor == "user"), 1)
        self.assertEqual(live_ids(parsed), {eid(1), eid(2)})

    def test_unstructured_event_agent_message_still_ignored(self):
        parsed = self.parse([
            _meta(),                                               # 0
            _user("go", "2026-01-01T00:00:01Z"),                   # 1
            _assistant("canonical assistant", "2026-01-01T00:00:02Z"),  # 2
            _event_agent_message("streamed duplicate", "2026-01-01T00:00:03Z"),  # 3
        ])
        assert_session_invariants(self, parsed, "codex")
        self.assertEqual([e.text for e in parsed.events],
                         ["go", "canonical assistant"])


class TestCodexPaginatedIngest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / ".codex"
        self.first = _write_rollover_root(self.root)
        self.conn = memory_db()
        self.addCleanup(self.conn.close)

    def refresh(self):
        return ingest.refresh(
            self.conn, sources=("codex",), machine="mini",
            roots={"codex": self.root},
        )

    def test_successor_refresh_replaces_legacy_tail_and_tracks_every_file(self):
        first = self.refresh()
        self.assertEqual((first["errors"], first["conflicts"]), (0, 0))
        self.assertEqual(
            self.conn.execute(
                "SELECT text FROM events WHERE event_id=?", (eid(6),)
            ).fetchone()["text"],
            "aborted answer",
        )
        self.conn.execute(
            "UPDATE sessions SET hidden_at='2026-01-02', hidden_reason='keep me' "
            "WHERE session_id=?", (f"codex:{UUID}",),
        )
        self.conn.execute(
            "INSERT INTO event_origins "
            "(session_id,event_id,origin,evidence_kind) VALUES (?,?,?,?)",
            (f"codex:{UUID}", eid(6), "unknown", "bmux"),
        )
        self.conn.execute(
            "INSERT INTO event_origin_evidence "
            "(session_id,event_id,origin,evidence_kind) VALUES (?,?,?,?)",
            (f"codex:{UUID}", eid(6), "unknown", "bmux"),
        )
        bmux_log = Path(self.root.parent) / "bmux.jsonl"
        self.conn.execute(
            "INSERT INTO ingest_state(path,mtime,size,session_id) VALUES (?,?,?,NULL)",
            (str(bmux_log), 1, 1),
        )
        self.conn.execute(
            "INSERT INTO ingest_state(path,mtime,size,session_id) VALUES (?,?,?,NULL)",
            (codex_control.STATE_PATH, 1, -1),
        )
        self.conn.commit()

        second_path = _write_rollover_successor(self.root, self.first)
        with mock.patch.dict(os.environ, {"CODEBRAIN_BMUX_LOG": str(bmux_log)}):
            second = self.refresh()

        self.assertEqual((second["errors"], second["conflicts"]), (0, 0))
        row = self.conn.execute(
            "SELECT actor,type,text,tool_call_event_id FROM events WHERE event_id=?",
            (eid(6),),
        ).fetchone()
        self.assertEqual(
            tuple(row), ("tool", "tool_result", "patched", eid(2)),
        )
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM events WHERE event_id=?", (eid(5),)
        ).fetchone())
        texts = [row["text"] for row in self.conn.execute(
            "SELECT e.text FROM session_events se JOIN events e USING(event_id) "
            "WHERE se.session_id=? ORDER BY se.seq", (f"codex:{UUID}",)
        )]
        self.assertEqual(texts, ["first", "apply_patch: old.py", "patched", "done"])
        states = self.conn.execute(
            "SELECT path,session_id FROM ingest_state WHERE path IN (?,?)",
            (str(self.first), str(second_path)),
        ).fetchall()
        self.assertEqual({row["path"] for row in states},
                         {str(self.first), str(second_path)})
        self.assertEqual({row["session_id"] for row in states}, {f"codex:{UUID}"})
        self.assertEqual(self.conn.execute(
            "SELECT hidden_at,hidden_reason FROM sessions WHERE session_id=?",
            (f"codex:{UUID}",),
        ).fetchone()[:], ("2026-01-02", "keep me"))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM event_origin_evidence WHERE event_id=?", (eid(6),)
        ).fetchone()[0], 0)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM ingest_state WHERE path=?", (str(bmux_log),)
        ).fetchone())
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM ingest_state WHERE path=?", (codex_control.STATE_PATH,)
        ).fetchone())

        unchanged = self.refresh()
        self.assertEqual(unchanged["files"], 0)

    def test_successor_refresh_rejects_foreign_reference_to_reused_id(self):
        self.refresh()
        self.conn.execute(
            "INSERT INTO sessions(session_id,source,tip_event_id) VALUES (?,?,?)",
            ("codex:foreign", "codex", eid(6)),
        )
        self.conn.execute(
            "INSERT INTO session_events "
            "(session_id,event_id,seq,parent_event_id,live,inherited) "
            "VALUES (?,?,0,NULL,1,1)", ("codex:foreign", eid(6)),
        )
        self.conn.commit()
        second_path = _write_rollover_successor(self.root, self.first)

        with mock.patch("builtins.print"):
            stats = self.refresh()

        self.assertEqual(stats["errors"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT text FROM events WHERE event_id=?", (eid(6),)
        ).fetchone()["text"], "aborted answer")
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM ingest_state WHERE path=?", (str(second_path),)
        ).fetchone())

    def test_upgrade_marker_repairs_unchanged_legacy_fragment_ingest(self):
        second_path = _write_rollover_successor(self.root, self.first)
        with mock.patch("builtins.print"):
            legacy = ingest._ingest(
                self.conn, [self.first, second_path],
                lambda path: codex.parse_file(path, machine="mini"),
            )
        self.assertGreater(legacy["conflicts"], 0)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM ingest_state WHERE path=?",
            (ingest._codex_pagination_state_path(self.root),),
        ).fetchone())

        repaired = self.refresh()

        self.assertEqual((repaired["errors"], repaired["conflicts"]), (0, 0))
        self.assertGreater(repaired["events"], 0)
        row = self.conn.execute(
            "SELECT actor,type,text,tool_call_event_id FROM events WHERE event_id=?",
            (eid(6),),
        ).fetchone()
        self.assertEqual(tuple(row), ("tool", "tool_result", "patched", eid(2)))
        marker = self.conn.execute(
            "SELECT mtime,size FROM ingest_state WHERE path=?",
            (ingest._codex_pagination_state_path(self.root),),
        ).fetchone()
        self.assertEqual(tuple(marker), (0.0, ingest.CODEX_PAGINATION_VERSION))

    def test_placement_only_refresh_invalidates_provenance(self):
        second_path = _write_rollover_successor(self.root, self.first)
        self.refresh()
        before = {
            row["event_id"]: tuple(row)
            for row in self.conn.execute(
                "SELECT event_id,actor,type,text,refs,tool_call_event_id,raw "
                "FROM events WHERE origin_session_id=?", (f"codex:{UUID}",)
            )
        }
        bmux_log = Path(self.root.parent) / "bmux.jsonl"
        self.conn.execute(
            "INSERT INTO ingest_state(path,mtime,size,session_id) VALUES (?,?,?,NULL)",
            (str(bmux_log), 1, 1),
        )
        self.conn.execute(
            "INSERT INTO ingest_state(path,mtime,size,session_id) VALUES (?,?,?,NULL)",
            (codex_control.STATE_PATH, 1, -1),
        )
        with second_path.open(encoding="utf-8") as handle:
            records = [json.loads(line) for line in handle]
        records.append({**_rollback(1, "2026-01-01T00:00:11Z"), "ordinal": 108})
        write_jsonl(second_path.parent, second_path.name, records)

        with mock.patch.dict(os.environ, {"CODEBRAIN_BMUX_LOG": str(bmux_log)}):
            stats = self.refresh()

        self.assertEqual(stats["errors"], 0)
        after = {
            row["event_id"]: tuple(row)
            for row in self.conn.execute(
                "SELECT event_id,actor,type,text,refs,tool_call_event_id,raw "
                "FROM events WHERE origin_session_id=?", (f"codex:{UUID}",)
            )
        }
        self.assertEqual(after, before)
        self.assertEqual(self.conn.execute(
            "SELECT SUM(live) FROM session_events WHERE session_id=?",
            (f"codex:{UUID}",),
        ).fetchone()[0], 0)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM ingest_state WHERE path=?", (str(bmux_log),)
        ).fetchone())
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM ingest_state WHERE path=?", (codex_control.STATE_PATH,)
        ).fetchone())


if __name__ == "__main__":
    unittest.main()

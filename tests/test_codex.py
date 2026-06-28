"""Codex adapter — synthesized turn forest, thread_rolled_back popping (incl. the
C1 full-rollback null-tip case), MCP result capture/dedup, apply_patch refs."""
import tempfile
import unittest

from codebrain.adapters import codex
from tests._helpers import assert_session_invariants, by_id, live_ids, write_jsonl

UUID = "0199aaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _meta(ts="2026-01-01T00:00:00Z"):
    return {"type": "session_meta", "timestamp": ts,
            "payload": {"id": UUID, "cwd": "/work", "timestamp": ts}}


def _user(text, ts):
    return {"type": "event_msg", "timestamp": ts,
            "payload": {"type": "user_message", "message": text}}


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


def eid(line):  # event_id = codex:<uuid>:<0-based line>
    return f"codex:{UUID}:{line}"


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


if __name__ == "__main__":
    unittest.main()

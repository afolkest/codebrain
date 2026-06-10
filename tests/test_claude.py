"""Claude adapter — parentUuid tree, bridged parents, parallel-tool liveness,
sidechain skipping, resume dedup."""
import tempfile
import unittest

from codebrain.adapters import claude
from tests._helpers import assert_session_invariants, by_id, live_ids, write_jsonl


def _user(uuid, parent, text, ts):
    return {"type": "user", "uuid": uuid, "parentUuid": parent, "sessionId": "S1",
            "cwd": "/work", "timestamp": ts, "message": {"role": "user", "content": text}}


def _assistant_text(uuid, parent, text, ts):
    return {"type": "assistant", "uuid": uuid, "parentUuid": parent, "sessionId": "S1",
            "cwd": "/work", "timestamp": ts,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _assistant_tool(uuid, parent, tool_id, name, inp, ts):
    return {"type": "assistant", "uuid": uuid, "parentUuid": parent, "sessionId": "S1",
            "cwd": "/work", "timestamp": ts,
            "message": {"role": "assistant",
                        "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": inp}]}}


def _tool_result(uuid, parent, tool_id, text, ts):
    return {"type": "user", "uuid": uuid, "parentUuid": parent, "sessionId": "S1",
            "cwd": "/work", "timestamp": ts,
            "message": {"role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                     "content": [{"type": "text", "text": text}]}]}}


class TestClaudeLinear(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def parse(self, records):
        p = write_jsonl(self.dir, "S1.jsonl", records)
        parsed = claude.parse_file(p, machine="m1")
        assert parsed is not None, "adapter unexpectedly returned None"
        return parsed

    def test_linear_session(self):
        parsed = self.parse([
            _user("u1", None, "do the thing", "2026-01-01T00:00:01Z"),
            _assistant_text("a1", "u1", "on it", "2026-01-01T00:00:02Z"),
            _assistant_tool("a2", "a1", "tu1", "Bash", {"command": "ls"}, "2026-01-01T00:00:03Z"),
            _tool_result("r1", "a2", "tu1", "file.txt", "2026-01-01T00:00:04Z"),
        ])
        assert_session_invariants(self, parsed, "claude")

        self.assertEqual(parsed.session.session_id, "claude:S1")
        self.assertEqual(parsed.session.cwd, "/work")
        self.assertEqual(parsed.session.machine, "m1")
        self.assertEqual([(e.actor, e.type) for e in parsed.events],
                         [("user", "message"), ("assistant", "message"),
                          ("assistant", "tool_call"), ("tool", "tool_result")])
        # tool_result pairs back to its tool_call
        ev = by_id(parsed)
        self.assertEqual(ev["claude:r1"].tool_call_event_id, "claude:a2")
        # the Bash command is captured as a ref
        self.assertEqual(ev["claude:a2"].refs["commands"], ["ls"])
        # everything is live; tip is the last event
        self.assertEqual(live_ids(parsed), {"claude:u1", "claude:a1", "claude:a2", "claude:r1"})
        self.assertEqual(parsed.session.tip_event_id, "claude:r1")

    def test_rollback_branch_is_dead(self):
        # user asks, gets a1; then rewinds to u1 and asks again -> a2 branch.
        parsed = self.parse([
            _user("u1", None, "question", "2026-01-01T00:00:01Z"),
            _assistant_text("a1", "u1", "first (abandoned) answer", "2026-01-01T00:00:02Z"),
            _user("u2", "u1", "actually, redo", "2026-01-01T00:00:03Z"),
            _assistant_text("a2", "u2", "second answer", "2026-01-01T00:00:04Z"),
        ])
        assert_session_invariants(self, parsed, "claude")
        # the abandoned branch (a1) is dead; the live path is u1 -> u2 -> a2
        self.assertEqual(live_ids(parsed), {"claude:u1", "claude:u2", "claude:a2"})
        self.assertNotIn("claude:a1", live_ids(parsed))
        self.assertEqual(parsed.session.tip_event_id, "claude:a2")

    def test_parallel_tool_results_both_live(self):
        # one user turn -> two tool calls -> two results. The parallel-tool fix must
        # keep BOTH results live even though only one is on the tip's direct chain.
        parsed = self.parse([
            _user("u1", None, "run both", "2026-01-01T00:00:01Z"),
            _assistant_tool("a1", "u1", "tu1", "Bash", {"command": "echo one"}, "2026-01-01T00:00:02Z"),
            _assistant_tool("a2", "a1", "tu2", "Bash", {"command": "echo two"}, "2026-01-01T00:00:03Z"),
            _tool_result("r1", "a2", "tu1", "one", "2026-01-01T00:00:04Z"),
            _tool_result("r2", "a2", "tu2", "two", "2026-01-01T00:00:05Z"),
        ])
        assert_session_invariants(self, parsed, "claude")
        live = live_ids(parsed)
        self.assertIn("claude:r1", live)
        self.assertIn("claude:r2", live)
        self.assertIn("claude:a1", live)
        self.assertIn("claude:a2", live)

    def test_sidechain_skipped_and_resume_deduped(self):
        records = [
            _user("u1", None, "prompt", "2026-01-01T00:00:01Z"),
            _assistant_text("a1", "u1", "answer", "2026-01-01T00:00:02Z"),
            # a sub-agent inline copy — must NOT become an event here
            {"type": "assistant", "uuid": "side1", "parentUuid": "u1", "sessionId": "S1",
             "isSidechain": True, "timestamp": "2026-01-01T00:00:02Z",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "sidechain"}]}},
            # a resume re-emission of u1 (same uuid, same content) — kept once
            _user("u1", None, "prompt", "2026-01-01T00:00:01Z"),
        ]
        parsed = self.parse(records)
        assert_session_invariants(self, parsed, "claude")
        ids = [e.event_id for e in parsed.events]
        self.assertEqual(ids, ["claude:u1", "claude:a1"])  # no side1, no duplicate u1
        self.assertTrue(all("side1" not in e.event_id for e in parsed.events))


if __name__ == "__main__":
    unittest.main()

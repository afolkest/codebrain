"""pi adapter — parentId tree, multi-block assistant records, rollback forks,
synthetic ids for id-less records, and branch-file inherited/authored split."""
import tempfile
import unittest

from codebrain.adapters import pi
from tests._helpers import assert_session_invariants, by_id, live_ids, write_jsonl


def _session(sid, ts, parent=None, cwd="/work"):
    r = {"type": "session", "id": sid, "timestamp": ts, "cwd": cwd}
    if parent:
        r["parentSession"] = parent
    return r


def _user(rid, parent, text, ts):
    return {"type": "message", "id": rid, "timestamp": ts, "parentId": parent,
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def _assistant_text(rid, parent, text, ts):
    return {"type": "message", "id": rid, "timestamp": ts, "parentId": parent,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


def _assistant_text_tool(rid, parent, text, tool_id, name, args, ts):
    return {"type": "message", "id": rid, "timestamp": ts, "parentId": parent,
            "message": {"role": "assistant",
                        "content": [{"type": "text", "text": text},
                                    {"type": "toolCall", "id": tool_id, "name": name, "arguments": args}]}}


def _tool_result(rid, parent, tool_id, text, ts):
    return {"type": "message", "id": rid, "timestamp": ts, "parentId": parent,
            "message": {"role": "toolResult", "toolCallId": tool_id,
                        "content": [{"type": "text", "text": text}]}}


class TestPi(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def parse(self, records, name="0_P.jsonl"):
        p = write_jsonl(self.dir, name, records)
        parsed = pi.parse_file(p, machine="m1")
        assert parsed is not None, "adapter unexpectedly returned None"
        return parsed

    def test_linear_multiblock(self):
        parsed = self.parse([
            _session("P1", "2026-01-01T00:00:00.000Z"),
            _user("aaaa1111", None, "hi", "2026-01-01T00:01:00.000Z"),
            _assistant_text_tool("bbbb2222", "aaaa1111", "sure", "tc1", "bash",
                                 {"command": "ls"}, "2026-01-01T00:02:00.000Z"),
            _tool_result("cccc3333", "bbbb2222", "tc1", "out", "2026-01-01T00:03:00.000Z"),
        ])
        assert_session_invariants(self, parsed, "pi")
        self.assertEqual(parsed.session.session_id, "pi:P1")
        self.assertEqual([(e.actor, e.type) for e in parsed.events],
                         [("user", "message"), ("assistant", "message"),
                          ("assistant", "tool_call"), ("tool", "tool_result")])
        ev = by_id(parsed)
        # the assistant text+toolCall is one record but two events; the tool_call
        # carries the record id + the toolCall id
        self.assertIn("pi:bbbb2222:2026-01-01T00:02:00.000Z:tc1", ev)
        # tool_result pairs back to that tool_call
        self.assertEqual(ev["pi:cccc3333:2026-01-01T00:03:00.000Z"].tool_call_event_id,
                         "pi:bbbb2222:2026-01-01T00:02:00.000Z:tc1")
        self.assertEqual(ev["pi:bbbb2222:2026-01-01T00:02:00.000Z:tc1"].refs["commands"], ["ls"])
        # all authored here, all live
        self.assertTrue(all(e.origin_session_id == "pi:P1" for e in parsed.events))
        self.assertEqual(len(live_ids(parsed)), 4)
        self.assertTrue(all(p.inherited == 0 for p in parsed.placements))

    def test_rollback_fork(self):
        # u1 -> a1, then rewind to u1 and answer again -> a2. a1 is the dead branch.
        parsed = self.parse([
            _session("P2", "2026-01-01T00:00:00.000Z"),
            _user("aaaa1111", None, "q", "2026-01-01T00:01:00.000Z"),
            _assistant_text("bbbb2222", "aaaa1111", "first", "2026-01-01T00:02:00.000Z"),
            _assistant_text("cccc3333", "aaaa1111", "second", "2026-01-01T00:03:00.000Z"),
        ])
        assert_session_invariants(self, parsed, "pi")
        live = live_ids(parsed)
        self.assertEqual(live, {"pi:aaaa1111:2026-01-01T00:01:00.000Z",
                                "pi:cccc3333:2026-01-01T00:03:00.000Z"})
        self.assertNotIn("pi:bbbb2222:2026-01-01T00:02:00.000Z", live)
        self.assertEqual(parsed.session.tip_event_id, "pi:cccc3333:2026-01-01T00:03:00.000Z")

    def test_missing_id_gets_synthetic_line_id(self):
        # a message with no `id` must not collide on pi:None:<ts>; it gets pi:L<line>:<ts>
        parsed = self.parse([
            _session("P3", "2026-01-01T00:00:00.000Z"),
            {"type": "message", "timestamp": "2026-01-01T00:01:00.000Z", "parentId": None,
             "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}},
        ])
        assert_session_invariants(self, parsed, "pi")
        self.assertEqual(len(parsed.events), 1)
        self.assertEqual(parsed.events[0].event_id, "pi:L1:2026-01-01T00:01:00.000Z")

    def test_branch_file_inherited_and_authored(self):
        # a resume/branch file: the copied prefix (ts < created_at) is inherited;
        # the tail authored here. Lineage + branch point come from that split.
        parent_path = "/some/dir/2026-01-01T00-00-00-000Z_PARENT.jsonl"
        parsed = self.parse([
            _session("CHILD", "2026-01-01T00:05:00.000Z", parent=parent_path),
            _user("aaaa1111", None, "shared", "2026-01-01T00:01:00.000Z"),            # inherited
            _assistant_text("bbbb2222", "aaaa1111", "shared answer", "2026-01-01T00:02:00.000Z"),  # inherited
            _user("dddd4444", "bbbb2222", "new", "2026-01-01T00:06:00.000Z"),         # authored
            _assistant_text("eeee5555", "dddd4444", "new answer", "2026-01-01T00:07:00.000Z"),     # authored
        ], name="0_CHILD.jsonl")
        assert_session_invariants(self, parsed, "pi")

        self.assertEqual(parsed.session.relation, "branch")
        self.assertEqual(parsed.session.parent_session_id, "pi:PARENT")
        inh = {p.event_id: p.inherited for p in parsed.placements}
        self.assertEqual(inh["pi:aaaa1111:2026-01-01T00:01:00.000Z"], 1)
        self.assertEqual(inh["pi:bbbb2222:2026-01-01T00:02:00.000Z"], 1)
        self.assertEqual(inh["pi:dddd4444:2026-01-01T00:06:00.000Z"], 0)
        self.assertEqual(inh["pi:eeee5555:2026-01-01T00:07:00.000Z"], 0)
        # origin is stamped only on events authored in THIS session
        ev = by_id(parsed)
        self.assertIsNone(ev["pi:aaaa1111:2026-01-01T00:01:00.000Z"].origin_session_id)
        self.assertEqual(ev["pi:dddd4444:2026-01-01T00:06:00.000Z"].origin_session_id, "pi:CHILD")
        # branch point = the last live inherited event
        self.assertEqual(parsed.session.branch_point_event_id, "pi:bbbb2222:2026-01-01T00:02:00.000Z")


if __name__ == "__main__":
    unittest.main()

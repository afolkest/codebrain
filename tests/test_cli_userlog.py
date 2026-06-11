import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from codebrain import cli, db
from codebrain.adapters.base import EventRow, PlacementRow, SessionRow


def _add(conn, *, sid="pi:S", source="pi", cwd="/work", eid, seq, ts, text,
         actor="user", typ="message", live=1, inherited=0, raw=None,
         relation=None, parent_session_id=None, branch_point_event_id=None):
    db.upsert_session(conn, SessionRow(
        session_id=sid, source=source, cwd=cwd, started_at="2026-01-01T00:00:00Z",
        ended_at=ts, relation=relation, parent_session_id=parent_session_id,
        branch_point_event_id=branch_point_event_id,
    ))
    db.upsert_event(conn, EventRow(
        event_id=eid, origin_session_id=sid if not inherited else None, ts=ts,
        actor=actor, type=typ, text=text, refs={"files": [], "commands": []}, raw=raw or {},
    ))
    db.upsert_placement(conn, PlacementRow(
        session_id=sid, event_id=eid, seq=seq, parent_event_id=None,
        live=live, inherited=inherited,
    ))


def _subagent_spawn_raw(rid="spawn111", ts="2026-01-01T00:02:00Z", cid="tc-sub"):
    return {
        "type": "message", "id": rid, "timestamp": ts,
        "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": cid, "name": "subagent", "arguments": {"agent": "oracle"}}
        ]},
    }


class TestUserlogCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "codebrain.db"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)

    def run_cli(self, *args):
        self.conn.commit()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--db", str(self.db_path), *args])
        return out.getvalue()

    def test_userlog_defaults_to_live_authored_user_messages(self):
        _add(self.conn, eid="pi:u-old", seq=0, ts="2026-01-01T00:01:00Z", text="old intent")
        _add(self.conn, eid="pi:a", seq=1, ts="2026-01-01T00:02:00Z", text="assistant noise",
             actor="assistant")
        _add(self.conn, eid="pi:u-dead", seq=2, ts="2026-01-01T00:03:00Z", text="dead branch",
             live=0)
        _add(self.conn, eid="pi:u-copy", seq=3, ts="2026-01-01T00:04:00Z", text="copied intent",
             inherited=1)
        _add(self.conn, eid="pi:u-caveat", seq=4, ts="2026-01-01T00:04:30Z",
             text="<local-command-caveat>generated command transcript</local-command-caveat>")
        _add(self.conn, eid="pi:u-command", seq=5, ts="2026-01-01T00:04:45Z",
             text="<command-name>/exit</command-name> <command-message>exit</command-message>")
        _add(self.conn, eid="pi:u-task-note", seq=6, ts="2026-01-01T00:04:48Z",
             text="<task-notification><task-id>abc</task-id></task-notification>")
        _add(self.conn, eid="pi:u-new", seq=7, ts="2026-01-01T00:05:00Z", text="new intent")

        out = self.run_cli("userlog", "--no-refresh", "--limit", "10")

        self.assertIn("new intent", out)
        self.assertIn("old intent", out)
        self.assertLess(out.index("new intent"), out.index("old intent"))
        self.assertNotIn("assistant noise", out)
        self.assertNotIn("dead branch", out)
        self.assertNotIn("copied intent", out)
        self.assertNotIn("generated command transcript", out)
        self.assertNotIn("command-message", out)
        self.assertNotIn("task-notification", out)
        self.assertIn("session: pi:S", out)
        self.assertIn("seq: 7", out)
        self.assertIn("expand: sessdb turns pi:S --around-seq 7", out)

    def test_userlog_excludes_structured_subagent_sessions_by_default(self):
        raw = _subagent_spawn_raw()
        spawn_eid = "pi:spawn111:2026-01-01T00:02:00Z:tc-sub"
        _add(self.conn, sid="pi:SUB", eid=spawn_eid, seq=0,
             ts="2026-01-01T00:02:00Z", text="subagent: {agent: oracle}",
             actor="assistant", typ="tool_call", inherited=1, raw=raw,
             parent_session_id="pi:PARENT", relation="branch", branch_point_event_id=spawn_eid)
        _add(self.conn, sid="pi:SUB", eid="pi:sub-u", seq=1,
             ts="2026-01-01T00:03:00Z",
             text="Task: You are a delegated subagent running from a fork.",
             parent_session_id="pi:PARENT", relation="branch", branch_point_event_id=spawn_eid)
        _add(self.conn, sid="pi:HUMAN", eid="pi:human-u", seq=0,
             ts="2026-01-01T00:01:00Z", text="human intent")

        out = self.run_cli("userlog", "--no-refresh", "--limit", "10")
        self.assertIn("human intent", out)
        self.assertNotIn("delegated subagent", out)

        out = self.run_cli("userlog", "--no-refresh", "--include-subagents", "--limit", "10")
        self.assertIn("delegated subagent", out)

    def test_userlog_filters_and_json_output(self):
        _add(self.conn, sid="pi:S", source="pi", cwd="/repo/codebrain", eid="pi:u1", seq=0,
             ts="2026-01-01T00:01:00Z", text="target but too early")
        _add(self.conn, sid="codex:C", source="codex", cwd="/repo/codebrain", eid="codex:u1", seq=0,
             ts="2026-01-03T00:01:00Z", text="target wrong source")
        _add(self.conn, sid="pi:S", source="pi", cwd="/repo/codebrain", eid="pi:u2", seq=1,
             ts="2026-01-03T00:02:00Z", text="target right size")
        _add(self.conn, sid="pi:S", source="pi", cwd="/repo/codebrain", eid="pi:u3", seq=2,
             ts="2026-01-03T00:03:00Z", text="target message that is deliberately too long")

        out = self.run_cli(
            "userlog", "--no-refresh", "--json", "--source", "pi", "--cwd", "brain",
            "--since", "2026-01-02", "--query", "TARGET", "--min-chars", "10",
            "--max-chars", "20",
        )
        rows = json.loads(out)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], "pi:S")
        self.assertEqual(rows[0]["seq"], 1)
        self.assertEqual(rows[0]["text"], "target right size")


class TestRecentCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "codebrain.db"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)

    def run_cli(self, *args):
        self.conn.commit()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--db", str(self.db_path), *args])
        return out.getvalue()

    def test_recent_sorts_by_latest_user_message_not_agent_event(self):
        _add(self.conn, sid="pi:S1", eid="pi:s1-u", seq=0,
             ts="2026-01-01T00:01:00Z", text="older user request")
        _add(self.conn, sid="pi:S1", eid="pi:s1-a", seq=1,
             ts="2026-01-01T00:05:00Z", text="newer assistant event",
             actor="assistant")
        _add(self.conn, sid="pi:S2", eid="pi:s2-u", seq=0,
             ts="2026-01-01T00:03:00Z", text="newer user request")

        out = self.run_cli("recent", "--no-refresh", "--json", "--limit", "10")
        rows = json.loads(out)

        self.assertEqual([r["session_id"] for r in rows], ["pi:S2", "pi:S1"])
        self.assertEqual(rows[0]["last_user_text"], "newer user request")
        self.assertEqual(rows[1]["live_event_count"], 2)

        text_out = self.run_cli("recent", "--no-refresh", "--limit", "1")
        self.assertIn("session: pi:S2", text_out)
        self.assertIn("seq: 0", text_out)
        self.assertIn("expand: sessdb turns pi:S2 --around-seq 0", text_out)

    def test_recent_excludes_structured_subagent_sessions_by_default(self):
        raw = _subagent_spawn_raw()
        spawn_eid = "pi:spawn111:2026-01-01T00:02:00Z:tc-sub"
        _add(self.conn, sid="pi:SUB", eid=spawn_eid, seq=0,
             ts="2026-01-01T00:02:00Z", text="subagent: {agent: oracle}",
             actor="assistant", typ="tool_call", inherited=1, raw=raw,
             parent_session_id="pi:PARENT", relation="branch", branch_point_event_id=spawn_eid)
        _add(self.conn, sid="pi:SUB", eid="pi:sub-u", seq=1,
             ts="2026-01-01T00:04:00Z", text="subagent task prompt",
             parent_session_id="pi:PARENT", relation="branch", branch_point_event_id=spawn_eid)
        _add(self.conn, sid="pi:HUMAN", eid="pi:human-u", seq=0,
             ts="2026-01-01T00:03:00Z", text="human intent")

        rows = json.loads(self.run_cli("recent", "--no-refresh", "--json", "--limit", "10"))
        self.assertEqual([r["session_id"] for r in rows], ["pi:HUMAN"])

        rows = json.loads(self.run_cli(
            "recent", "--no-refresh", "--json", "--include-subagents", "--limit", "10"
        ))
        self.assertEqual([r["session_id"] for r in rows], ["pi:SUB", "pi:HUMAN"])

    def test_recent_filters_to_matching_user_activity(self):
        _add(self.conn, sid="pi:S1", source="pi", cwd="/repo/codebrain", eid="pi:s1-u", seq=0,
             ts="2026-01-01T00:01:00Z", text="target but too early")
        _add(self.conn, sid="codex:C", source="codex", cwd="/repo/codebrain", eid="codex:c-u", seq=0,
             ts="2026-01-03T00:01:00Z", text="target wrong source")
        _add(self.conn, sid="pi:S2", source="pi", cwd="/repo/codebrain", eid="pi:s2-u", seq=0,
             ts="2026-01-03T00:02:00Z", text="target right size")
        _add(self.conn, sid="pi:S3", source="pi", cwd="/repo/codebrain", eid="pi:s3-u", seq=0,
             ts="2026-01-03T00:03:00Z", text="target message that is deliberately too long")

        out = self.run_cli(
            "recent", "--no-refresh", "--json", "--source", "pi", "--cwd", "brain",
            "--since", "2026-01-02", "--min-chars", "10", "--max-chars", "20",
        )
        rows = json.loads(out)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], "pi:S2")
        self.assertEqual(rows[0]["last_user_seq"], 0)
        self.assertEqual(rows[0]["last_user_text"], "target right size")


if __name__ == "__main__":
    unittest.main()

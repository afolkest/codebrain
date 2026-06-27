import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from codebrain import cli, db
from codebrain.adapters.base import EventRow, PlacementRow, SessionRow


def _add(conn, *, sid="pi:S", source="pi", cwd="/work", eid, seq, ts, text,
         actor="user", typ="message", live=1, inherited=0):
    db.upsert_session(conn, SessionRow(
        session_id=sid, source=source, cwd=cwd, started_at="2026-01-01T00:00:00Z",
        ended_at=ts,
    ))
    db.upsert_event(conn, EventRow(
        event_id=eid, origin_session_id=sid if not inherited else None, ts=ts,
        actor=actor, type=typ, text=text, refs={"files": [], "commands": []}, raw={},
    ))
    db.upsert_placement(conn, PlacementRow(
        session_id=sid, event_id=eid, seq=seq, parent_event_id=None,
        live=live, inherited=inherited,
    ))


class TestTurnsCli(unittest.TestCase):
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

    def run_cli_capture(self, *args):
        """Run the CLI capturing stdout, stderr, and the exit code (None if no
        SystemExit was raised)."""
        self.conn.commit()
        out, err, code = io.StringIO(), io.StringIO(), None
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli.main(["--db", str(self.db_path), *args])
            except SystemExit as e:
                code = e.code
        return out.getvalue(), err.getvalue(), code

    def seed(self):
        _add(self.conn, eid="pi:u0", seq=0, ts="2026-01-01T00:01:00Z",
             text="first user request")
        _add(self.conn, eid="pi:a1", seq=1, ts="2026-01-01T00:02:00Z",
             text="first assistant answer " + "x" * 80, actor="assistant")
        _add(self.conn, eid="pi:tc2", seq=2, ts="2026-01-01T00:03:00Z",
             text="bash: secret command details", actor="assistant", typ="tool_call")
        _add(self.conn, eid="pi:tr3", seq=3, ts="2026-01-01T00:04:00Z",
             text="tool result noise", actor="tool", typ="tool_result")
        _add(self.conn, eid="pi:u4", seq=4, ts="2026-01-01T00:05:00Z",
             text="second user request")
        _add(self.conn, eid="pi:a5", seq=5, ts="2026-01-01T00:06:00Z",
             text="second assistant answer", actor="assistant")
        _add(self.conn, eid="pi:u6", seq=6, ts="2026-01-01T00:07:00Z",
             text="third user request")
        _add(self.conn, eid="pi:a7", seq=7, ts="2026-01-01T00:08:00Z",
             text="third assistant answer", actor="assistant")
        _add(self.conn, eid="pi:u8-dead", seq=8, ts="2026-01-01T00:09:00Z",
             text="dead user request", live=0)

    def test_turns_groups_by_user_and_hides_tools_by_default(self):
        self.seed()
        out = self.run_cli("turns", "pi:S", "--no-refresh", "--limit", "2", "--agent-chars", "30")

        self.assertIn("turn 0  seq 0..3", out)
        self.assertIn("user[0]: first user request", out)
        self.assertIn("assistant/message[1]: first assistant answer", out)
        self.assertIn("tools: 2 hidden", out)
        self.assertIn("turn 1  seq 4..5", out)
        self.assertNotIn("secret command details", out)
        self.assertNotIn("third user request", out)
        self.assertNotIn("dead user request", out)

    def test_turns_around_seq_and_show_tools_json(self):
        self.seed()
        out = self.run_cli(
            "turns", "pi:S", "--no-refresh", "--json", "--around-seq", "4",
            "--context-turns", "1", "--show-tools", "--tool-chars", "12",
        )
        rows = json.loads(out)

        self.assertEqual([r["user_seq"] for r in rows], [0, 4, 6])
        self.assertEqual(rows[1]["user_live"], 1)
        self.assertEqual(rows[1]["user_inherited"], 0)
        self.assertEqual(rows[0]["hidden_tool_events"], 0)
        tool_events = [e for e in rows[0]["events"] if e["type"] == "tool_call"]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0]["inherited"], 0)
        self.assertTrue(tool_events[0]["preview"].startswith("bash:"))

    def test_turns_all_includes_rolled_back_turns(self):
        self.seed()
        out = self.run_cli("turns", "pi:S", "--no-refresh", "--all", "--limit", "10")
        self.assertIn("user[8] (rolled-back): dead user request", out)

    def test_turn_negative_one_returns_latest_live_turn(self):
        self.seed()
        out = self.run_cli("turns", "pi:S", "--no-refresh", "--turn", "-1")
        self.assertIn("turn 2  seq 6..7", out)
        self.assertIn("user[6]: third user request", out)
        self.assertNotIn("second user request", out)
        self.assertNotIn("first user request", out)
        # default excludes rolled-back, so the dead turn is never "latest"
        self.assertNotIn("dead user request", out)

    def test_turn_negative_two_returns_previous_turn(self):
        self.seed()
        out = self.run_cli("turns", "pi:S", "--no-refresh", "--turn", "-2")
        self.assertIn("turn 1  seq 4..5", out)
        self.assertIn("user[4]: second user request", out)
        self.assertNotIn("third user request", out)
        self.assertNotIn("first user request", out)

    def test_turn_zero_returns_first_turn(self):
        self.seed()
        out = self.run_cli("turns", "pi:S", "--no-refresh", "--turn", "0")
        self.assertIn("turn 0  seq 0..3", out)
        self.assertIn("user[0]: first user request", out)
        self.assertNotIn("second user request", out)
        self.assertNotIn("third user request", out)

    def test_turn_out_of_range_positive_text_errors(self):
        self.seed()
        out, err, code = self.run_cli_capture("turns", "pi:S", "--no-refresh", "--turn", "3")
        self.assertEqual(code, 1)
        self.assertIn("out of range", err)
        self.assertNotIn("turn ", out)

    def test_turn_out_of_range_negative_text_errors(self):
        self.seed()
        out, err, code = self.run_cli_capture("turns", "pi:S", "--no-refresh", "--turn", "-4")
        self.assertEqual(code, 1)
        self.assertIn("out of range", err)
        self.assertNotIn("turn ", out)

    def test_turn_json_returns_single_turn(self):
        self.seed()
        out = self.run_cli("turns", "pi:S", "--no-refresh", "--turn", "-1", "--json")
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["turn_index"], 2)
        self.assertEqual(rows[0]["user_seq"], 6)

    def test_turn_json_out_of_range_is_empty_array(self):
        self.seed()
        out = self.run_cli("turns", "pi:S", "--no-refresh", "--turn", "9", "--json")
        self.assertEqual(json.loads(out), [])

    def test_turn_rejects_around_seq(self):
        self.seed()
        _, err, code = self.run_cli_capture(
            "turns", "pi:S", "--no-refresh", "--turn", "0", "--around-seq", "4")
        self.assertEqual(code, 2)
        self.assertIn("not allowed with", err)


if __name__ == "__main__":
    unittest.main()

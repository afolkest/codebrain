import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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


def _iso(dt):
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class TestSearchCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "codebrain.db"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        if not db.has_fts5(self.conn):
            self.skipTest("sqlite built without FTS5")

    def run_cli(self, *args):
        self.conn.commit()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--db", str(self.db_path), *args])
        return out.getvalue()

    def test_search_filters_and_json_output(self):
        _add(self.conn, sid="pi:S1", source="pi", cwd="/repo/codebrain", eid="pi:s1-u", seq=0,
             ts="2026-01-01T00:01:00Z", text="needle user preference")
        _add(self.conn, sid="pi:S1", source="pi", cwd="/repo/codebrain", eid="pi:s1-a", seq=1,
             ts="2026-01-01T00:02:00Z", text="needle assistant explanation",
             actor="assistant")
        _add(self.conn, sid="codex:C", source="codex", cwd="/repo/codebrain", eid="codex:c-u", seq=0,
             ts="2026-01-01T00:03:00Z", text="needle wrong source")
        _add(self.conn, sid="pi:S2", source="pi", cwd="/repo/example-project", eid="pi:s2-u", seq=0,
             ts="2026-01-01T00:04:00Z", text="needle wrong cwd")

        out = self.run_cli(
            "search", "needle", "--no-refresh", "--json", "--actor", "user",
            "--type", "message", "--source", "pi", "--cwd", "brain",
            "--after", "2026-01-01", "--before", "2026-01-02",
        )
        rows = json.loads(out)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ts"], "2026-01-01T00:01:00Z")
        self.assertEqual(rows[0]["source"], "pi")
        self.assertEqual(rows[0]["session_id"], "pi:S1")
        self.assertEqual(rows[0]["seq"], 0)
        self.assertEqual(rows[0]["event_id"], "pi:s1-u")
        self.assertEqual(rows[0]["cwd"], "/repo/codebrain")
        self.assertEqual(rows[0]["actor"], "user")
        self.assertEqual(rows[0]["type"], "message")
        self.assertEqual(rows[0]["inherited"], 0)
        self.assertIn("[needle]", rows[0]["snippet"])
        self.assertEqual(rows[0]["text"], "needle user preference")
        self.assertEqual(rows[0]["expand_command"], "sessdb turns pi:S1 --around-seq 0")

    def test_search_timestamp_filters_handle_fractional_boundaries(self):
        _add(self.conn, sid="pi:S", eid="pi:exact", seq=0,
             ts="2026-01-01T00:02:00.000Z", text="boundneedle exact boundary")
        _add(self.conn, sid="pi:S", eid="pi:later", seq=1,
             ts="2026-01-01T00:02:00.001Z", text="boundneedle just later")

        rows = json.loads(self.run_cli(
            "search", "boundneedle", "--no-refresh", "--json", "--after", "2026-01-01T00:02:00Z"
        ))
        self.assertEqual({r["event_id"] for r in rows}, {"pi:exact", "pi:later"})

        rows = json.loads(self.run_cli(
            "search", "boundneedle", "--no-refresh", "--json", "--before", "2026-01-01T00:02:00Z"
        ))
        self.assertEqual(rows, [])

        rows = json.loads(self.run_cli(
            "search", "boundneedle", "--no-refresh", "--json", "--before", "2026-01-01T00:02:00.001Z"
        ))
        self.assertEqual([r["event_id"] for r in rows], ["pi:exact"])

    def test_search_session_and_recent_filters_are_explicit(self):
        now = datetime.now(timezone.utc)
        _add(self.conn, sid="pi:OLD", eid="pi:old-u", seq=0,
             ts=_iso(now - timedelta(hours=2)), text="freshneedle older intent")
        _add(self.conn, sid="pi:NOW", eid="pi:now-u", seq=0,
             ts=_iso(now), text="freshneedle current session echo")
        _add(self.conn, sid="pi:NOW", eid="pi:now-copy", seq=1,
             ts=_iso(now), text="copiedneedle inherited current context", inherited=1)

        rows = json.loads(self.run_cli("search", "freshneedle", "--no-refresh", "--json"))
        self.assertEqual({r["session_id"] for r in rows}, {"pi:OLD", "pi:NOW"})

        rows = json.loads(self.run_cli(
            "search", "freshneedle", "--no-refresh", "--json", "--exclude-session", "pi:N"
        ))
        self.assertEqual([r["session_id"] for r in rows], ["pi:OLD"])

        rows = json.loads(self.run_cli(
            "search", "freshneedle", "--no-refresh", "--json", "--only-session", "pi:N"
        ))
        self.assertEqual([r["session_id"] for r in rows], ["pi:NOW"])

        rows = json.loads(self.run_cli("search", "copiedneedle", "--no-refresh", "--json"))
        self.assertEqual(rows, [])

        rows = json.loads(self.run_cli(
            "search", "copiedneedle", "--no-refresh", "--json", "--only-session", "pi:N"
        ))
        self.assertEqual([(r["session_id"], r["inherited"]) for r in rows], [("pi:NOW", 1)])

        rows = json.loads(self.run_cli(
            "search", "freshneedle", "--no-refresh", "--json", "--exclude-recent", "1h"
        ))
        self.assertEqual([r["session_id"] for r in rows], ["pi:OLD"])

    def test_search_subagent_filter_uses_structured_signal_not_prompt_text(self):
        raw = _subagent_spawn_raw()
        spawn_eid = "pi:spawn111:2026-01-01T00:02:00Z:tc-sub"
        _add(self.conn, sid="pi:SUB", eid=spawn_eid, seq=0,
             ts="2026-01-01T00:02:00Z", text="subagent call",
             actor="assistant", typ="tool_call", inherited=1, raw=raw,
             parent_session_id="pi:PARENT", relation="branch", branch_point_event_id=spawn_eid)
        _add(self.conn, sid="pi:SUB", eid="pi:sub-u", seq=1,
             ts="2026-01-01T00:03:00Z", text="structneedle neutral child instruction",
             parent_session_id="pi:PARENT", relation="branch", branch_point_event_id=spawn_eid)
        human_text = "Task: You are a delegated subagent running from a fork. structneedle"
        _add(self.conn, sid="pi:HUMAN", eid="pi:human-u", seq=0,
             ts="2026-01-01T00:04:00Z", text=human_text)

        rows = json.loads(self.run_cli("search", "structneedle", "--no-refresh", "--json"))
        self.assertEqual([r["session_id"] for r in rows], ["pi:HUMAN"])
        self.assertEqual(rows[0]["text"], human_text)

        rows = json.loads(self.run_cli(
            "search", "structneedle", "--no-refresh", "--json", "--include-subagents"
        ))
        self.assertEqual({r["session_id"] for r in rows}, {"pi:HUMAN", "pi:SUB"})


if __name__ == "__main__":
    unittest.main()

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from codebrain import cli, db
from codebrain.adapters.base import EventRow, PlacementRow, SessionRow


def _session(conn, sid, *, source="pi", cwd="/repo/example-project", started="2026-01-01T00:00:00Z",
             parent=None, relation=None, branch=None, spawn=None):
    db.upsert_session(conn, SessionRow(
        session_id=sid, source=source, cwd=cwd, started_at=started, ended_at=started,
        parent_session_id=parent, relation=relation, branch_point_event_id=branch,
        spawn_event_id=spawn,
    ))


def _event(conn, sid, *, eid, seq, ts, text, actor="user", typ="message", inherited=0, raw=None):
    db.upsert_event(conn, EventRow(
        event_id=eid, origin_session_id=sid if not inherited else None, ts=ts,
        actor=actor, type=typ, text=text, refs={"files": [], "commands": []}, raw=raw or {},
    ))
    db.upsert_placement(conn, PlacementRow(
        session_id=sid, event_id=eid, seq=seq, parent_event_id=None, live=1,
        inherited=inherited,
    ))


def _subagent_spawn_raw(rid="spawn111", ts="2026-01-01T00:02:00Z", cid="tc-sub"):
    return {
        "type": "message", "id": rid, "timestamp": ts,
        "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": cid, "name": "subagent", "arguments": {"agent": "oracle"}}
        ]},
    }


class TestLineageCli(unittest.TestCase):
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

    def seed_tree(self):
        _session(self.conn, "pi:ROOT", started="2026-01-01T00:00:00Z")
        _session(self.conn, "pi:CHILD", started="2026-01-01T01:00:00Z",
                 parent="pi:ROOT", relation="branch", branch="pi:root-event")
        _session(self.conn, "pi:SIB", started="2026-01-01T01:30:00Z",
                 parent="pi:ROOT", relation="branch", branch="pi:root-event")
        _session(self.conn, "pi:GRAND", started="2026-01-01T02:00:00Z",
                 parent="pi:CHILD", relation="subagent", branch="pi:spawn", spawn="pi:spawn")
        _event(self.conn, "pi:ROOT", eid="pi:root-u", seq=0,
               ts="2026-01-01T00:10:00Z", text="root intent")
        _event(self.conn, "pi:CHILD", eid="pi:child-u", seq=2,
               ts="2026-01-01T01:10:00Z", text="child intent")
        _event(self.conn, "pi:SIB", eid="pi:sib-u", seq=4,
               ts="2026-01-01T01:40:00Z", text="sibling intent")
        _event(self.conn, "pi:GRAND", eid="pi:grand-u", seq=6,
               ts="2026-01-01T02:10:00Z", text="grandchild task")

    def test_lineage_text_shows_root_parent_children_and_siblings(self):
        self.seed_tree()

        out = self.run_cli("lineage", "pi:CHILD", "--no-refresh")

        self.assertIn("lineage: pi:ROOT -> pi:CHILD", out)
        self.assertIn("root:", out)
        self.assertIn("pi:ROOT", out)
        self.assertIn("parent:", out)
        self.assertIn("current:", out)
        self.assertIn("children:", out)
        self.assertIn("pi:GRAND  pi", out)
        self.assertIn("relation=subagent", out)
        self.assertIn("spawn: pi:spawn", out)
        self.assertIn("siblings:", out)
        self.assertIn("pi:SIB", out)
        self.assertIn("last_user[2]: child intent", out)
        self.assertIn("expand: sessdb turns pi:CHILD --around-seq 2", out)

    def test_lineage_uses_structured_subagent_fallback_for_existing_dbs(self):
        spawn = "pi:spawn111:2026-01-01T00:02:00Z:tc-sub"
        _session(self.conn, "pi:PARENT")
        _session(self.conn, "pi:SUB", parent="pi:PARENT", relation="branch", branch=spawn)
        _event(self.conn, "pi:SUB", eid=spawn, seq=0, ts="2026-01-01T00:02:00Z",
               text="subagent: {agent: oracle}", actor="assistant", typ="tool_call",
               inherited=1, raw=_subagent_spawn_raw())
        _event(self.conn, "pi:SUB", eid="pi:sub-u", seq=1, ts="2026-01-01T00:03:00Z",
               text="neutral child instruction")

        model = json.loads(self.run_cli("lineage", "pi:SUB", "--no-refresh", "--json"))

        self.assertEqual(model["current"]["stored_relation"], "branch")
        self.assertEqual(model["current"]["relation"], "subagent")
        self.assertEqual(model["current"]["spawn_event_id"], spawn)

    def test_lineage_json_is_structured(self):
        self.seed_tree()

        model = json.loads(self.run_cli("lineage", "GRAND", "--no-refresh", "--json"))

        self.assertEqual(model["root"]["session_id"], "pi:ROOT")
        self.assertEqual(model["parent"]["session_id"], "pi:CHILD")
        self.assertEqual(model["current"]["session_id"], "pi:GRAND")
        self.assertEqual(model["current"]["relation"], "subagent")
        self.assertEqual([s["session_id"] for s in model["ancestors"]],
                         ["pi:ROOT", "pi:CHILD", "pi:GRAND"])
        self.assertEqual(model["current"]["last_user"]["seq"], 6)
        self.assertEqual(model["current"]["last_user"]["expand_command"],
                         "sessdb turns pi:GRAND --around-seq 6")
        self.assertEqual(model["siblings_total"], 0)
        self.assertEqual(model["children"], [])


if __name__ == "__main__":
    unittest.main()

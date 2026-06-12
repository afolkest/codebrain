import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from codebrain import cli, db
from codebrain.adapters.base import EventRow, PlacementRow, SessionRow


def _session(conn, sid="pi:S", *, source="pi", cwd="/repo/example-project"):
    db.upsert_session(conn, SessionRow(
        session_id=sid, source=source, cwd=cwd,
        started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:10:00Z",
    ))


def _event(conn, *, sid="pi:S", eid, seq, ts, text, actor="user", typ="message",
           refs=None, live=1):
    _session(conn, sid=sid)
    db.upsert_event(conn, EventRow(
        event_id=eid, origin_session_id=sid, ts=ts, actor=actor, type=typ, text=text,
        refs=refs or {"files": [], "commands": []}, raw={},
    ))
    db.upsert_placement(conn, PlacementRow(
        session_id=sid, event_id=eid, seq=seq, parent_event_id=None, live=live,
        inherited=0,
    ))


class TestRefsCli(unittest.TestCase):
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

    def test_refs_groups_files_commands_and_commits(self):
        _event(self.conn, eid="pi:u0", seq=0, ts="2026-01-01T00:00:00Z",
               text="why did we decide this")
        _event(self.conn, eid="pi:a1", seq=1, ts="2026-01-01T00:01:00Z",
               text="git show 6ec541b:docs/wip/pipeline-redesign.md",
               actor="assistant", typ="tool_call", refs={
                   "files": ["docs/wip/pipeline-redesign.md"],
                   "commands": ["git show 6ec541b:docs/wip/pipeline-redesign.md"],
               })
        _event(self.conn, eid="pi:t2", seq=2, ts="2026-01-01T00:02:00Z",
               text="commit deadbeef also touched docs/wip/pipeline-redesign.md",
               actor="tool", typ="tool_result", refs={
                   "files": ["docs/wip/pipeline-redesign.md"],
                   "commands": [],
               })
        _event(self.conn, eid="pi:a3", seq=3, ts="2026-01-01T00:03:00Z",
               text="unrelated file plus bogus abc1234g and numeric 1234567 tokens",
               actor="assistant", refs={"files": ["unrelated.md"], "commands": []})

        out = self.run_cli("refs", "pi:S", "--no-refresh")

        self.assertIn("session: pi:S", out)
        self.assertIn("files:", out)
        self.assertIn("docs/wip/pipeline-redesign.md", out)
        self.assertIn("commands:", out)
        self.assertIn("git show 6ec541b:docs/wip/pipeline-redesign.md", out)
        self.assertIn("commits:", out)
        self.assertIn("git show 6ec541b", out)
        self.assertIn("git show 6ec541b:docs/wip/pipeline-redesign.md", out)
        self.assertIn("git show deadbeef", out)
        self.assertNotIn("git show 6ec541b:unrelated.md", out)
        self.assertNotIn("git show deadbeef:unrelated.md", out)
        self.assertNotIn("git show abc1234", out)
        self.assertNotIn("git show 1234567", out)
        self.assertIn("expand: sessdb turns pi:S --around-seq 1", out)
        self.assertIn("expand: sessdb turns pi:S --around-seq 2", out)

        model = json.loads(self.run_cli("refs", "pi:S", "--no-refresh", "--json"))
        self.assertEqual(model["session"]["session_id"], "pi:S")
        self.assertEqual(model["scope"]["all"], False)
        self.assertEqual([x["value"] for x in model["files"]],
                         ["docs/wip/pipeline-redesign.md", "unrelated.md"])
        self.assertEqual(model["files"][0]["events"][0]["seq"], 1)
        self.assertEqual([x["value"] for x in model["commands"]],
                         ["git show 6ec541b:docs/wip/pipeline-redesign.md"])
        self.assertEqual({x["value"] for x in model["commits"]}, {"6ec541b", "deadbeef"})
        self.assertEqual(model["commits"][0]["events"][0]["expand_command"],
                         "sessdb turns pi:S --around-seq 1")

    def test_refs_ignores_malformed_unstructured_and_text_only_file_mentions(self):
        _event(self.conn, eid="pi:bad", seq=0, ts="2026-01-01T00:00:00Z",
               text="mentions docs/free-text.md and commit abc1234g but has bad refs",
               actor="assistant", refs={"files": None, "commands": "git show feedabc"})
        self.conn.execute("UPDATE events SET refs=? WHERE event_id=?", ("{not json", "pi:bad"))
        _event(self.conn, eid="pi:null", seq=1, ts="2026-01-01T00:01:00Z",
               text="another docs/only-text.md mention with numeric 1234567",
               actor="assistant", refs={"files": "docs/not-a-list.md", "commands": None})

        model = json.loads(self.run_cli("refs", "pi:S", "--no-refresh", "--json"))

        self.assertEqual(model["files"], [])
        self.assertEqual(model["commands"], [])
        self.assertEqual(model["commits"], [])

    def test_refs_defaults_to_live_events_and_all_includes_rolled_back_refs(self):
        _event(self.conn, eid="pi:live", seq=0, ts="2026-01-01T00:00:00Z",
               text="live ref", actor="assistant",
               refs={"files": ["live.md"], "commands": []})
        _event(self.conn, eid="pi:dead", seq=1, ts="2026-01-01T00:01:00Z",
               text="dead ref", actor="assistant", live=0,
               refs={"files": ["dead.md"], "commands": []})

        model = json.loads(self.run_cli("refs", "pi:S", "--no-refresh", "--json"))
        self.assertEqual([x["value"] for x in model["files"]], ["live.md"])

        model = json.loads(self.run_cli("refs", "pi:S", "--no-refresh", "--json", "--all"))
        self.assertEqual([x["value"] for x in model["files"]], ["dead.md", "live.md"])

    def test_refs_can_scope_to_turn_window(self):
        _event(self.conn, eid="pi:u0", seq=0, ts="2026-01-01T00:00:00Z",
               text="before user")
        _event(self.conn, eid="pi:a1", seq=1, ts="2026-01-01T00:01:00Z",
               text="before ref", actor="assistant",
               refs={"files": ["before.md"], "commands": []})
        _event(self.conn, eid="pi:u2", seq=2, ts="2026-01-01T00:02:00Z",
               text="target user")
        _event(self.conn, eid="pi:a3", seq=3, ts="2026-01-01T00:03:00Z",
               text="target ref", actor="assistant",
               refs={"files": ["target.md"], "commands": []})
        _event(self.conn, eid="pi:u4", seq=4, ts="2026-01-01T00:04:00Z",
               text="after user")
        _event(self.conn, eid="pi:a5", seq=5, ts="2026-01-01T00:05:00Z",
               text="after ref", actor="assistant",
               refs={"files": ["after.md"], "commands": []})

        model = json.loads(self.run_cli(
            "refs", "pi:S", "--no-refresh", "--json", "--around-seq", "3",
            "--context-turns", "0",
        ))

        self.assertEqual(model["scope"]["seq_start"], 2)
        self.assertEqual(model["scope"]["seq_end"], 3)
        self.assertEqual([x["value"] for x in model["files"]], ["target.md"])


if __name__ == "__main__":
    unittest.main()

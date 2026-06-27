import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from codebrain import cli, db
from codebrain.adapters.base import EventRow, PlacementRow, SessionRow


def _session(conn, sid="pi:S", *, source="pi", cwd="/repo/example-project",
             relation=None, parent_session_id=None, branch_point_event_id=None):
    db.upsert_session(conn, SessionRow(
        session_id=sid, source=source, cwd=cwd,
        started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:10:00Z",
        relation=relation, parent_session_id=parent_session_id,
        branch_point_event_id=branch_point_event_id,
    ))


def _event(conn, *, sid="pi:S", source="pi", cwd="/repo/example-project", eid, seq, ts,
           text, actor="assistant", typ="message", refs=None, live=1, inherited=0,
           relation=None, parent_session_id=None, branch_point_event_id=None, raw=None):
    _session(conn, sid=sid, source=source, cwd=cwd, relation=relation,
             parent_session_id=parent_session_id,
             branch_point_event_id=branch_point_event_id)
    db.upsert_event(conn, EventRow(
        event_id=eid, origin_session_id=None if inherited else sid, ts=ts,
        actor=actor, type=typ, text=text,
        refs=refs or {"files": [], "commands": []}, raw=raw or {},
    ))
    db.upsert_placement(conn, PlacementRow(
        session_id=sid, event_id=eid, seq=seq, parent_event_id=None, live=live,
        inherited=inherited,
    ))


class TestTouchedCli(unittest.TestCase):
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

    def test_touched_finds_structured_file_refs_with_evidence(self):
        _event(self.conn, eid="pi:u0", seq=0, ts="2026-01-01T00:00:00Z",
               text="why did this file change?", actor="user")
        _event(self.conn, eid="pi:a1", seq=1, ts="2026-01-01T00:01:00Z",
               text="Read docs/wip/pipeline-redesign.md", typ="tool_call",
               refs={"files": ["docs/wip/pipeline-redesign.md"], "commands": []})
        _event(self.conn, eid="pi:a2", seq=2, ts="2026-01-01T00:02:00Z",
               text="free-text mention docs/wip/pipeline-redesign.md only",
               refs={"files": [], "commands": []})
        _event(self.conn, sid="claude:A", source="claude", cwd="/repo/example-project",
               eid="claude:a3", seq=0, ts="2026-01-01T00:03:00Z",
               text="Edit absolute path", typ="tool_call",
               refs={"files": ["/repo/example-project/docs/wip/pipeline-redesign.md"], "commands": []})

        model = json.loads(self.run_cli(
            "touched", "docs/wip/pipeline-redesign.md", "--no-refresh", "--json"
        ))

        self.assertEqual(model["query"]["mode"], "path")
        self.assertEqual([m["file"] for m in model["matches"]], [
            "/repo/example-project/docs/wip/pipeline-redesign.md",
            "docs/wip/pipeline-redesign.md",
        ])
        self.assertEqual(model["matches"][1]["nearest_user"]["seq"], 0)
        self.assertEqual(model["matches"][1]["expand_command"],
                         "sessdb turns pi:S --around-seq 1")
        self.assertEqual(model["matches"][1]["refs_command"],
                         "sessdb refs pi:S --around-seq 1 --context-turns 0")

        model = json.loads(self.run_cli(
            "touched", "/repo/example-project/docs/wip/pipeline-redesign.md", "--no-refresh", "--json"
        ))
        self.assertEqual([m["file"] for m in model["matches"]], [
            "/repo/example-project/docs/wip/pipeline-redesign.md",
            "docs/wip/pipeline-redesign.md",
        ])

        out = self.run_cli("touched", "docs/wip/pipeline-redesign.md", "--no-refresh")
        self.assertIn("path: docs/wip/pipeline-redesign.md  mode: path", out)
        self.assertIn("file: docs/wip/pipeline-redesign.md", out)
        self.assertIn("nearest_user[0]: why did this file change?", out)
        self.assertIn("expand: sessdb turns pi:S --around-seq 1", out)
        self.assertIn("refs: sessdb refs pi:S --around-seq 1 --context-turns 0", out)
        self.assertNotIn("free-text mention", out)

    def test_touched_basename_prefix_and_filters(self):
        _event(self.conn, eid="pi:docs", seq=0, ts="2026-01-02T00:00:00Z",
               text="docs file", refs={"files": ["docs/wip/foo.md"], "commands": []})
        _event(self.conn, sid="pi:OTHER", cwd="/repo/other", eid="pi:other", seq=0,
               ts="2026-01-02T00:01:00Z", text="other file",
               refs={"files": ["notes/foo.md"], "commands": []})
        _event(self.conn, sid="codex:C", source="codex", cwd="/repo/example-project",
               eid="codex:c", seq=0, ts="2026-01-02T00:02:00Z", text="codex file",
               refs={"files": ["docs/wip/bar.md"], "commands": []})

        model = json.loads(self.run_cli(
            "touched", "foo.md", "--basename", "--no-refresh", "--json"
        ))
        self.assertEqual({m["file"] for m in model["matches"]}, {"docs/wip/foo.md", "notes/foo.md"})

        _event(self.conn, sid="pi:ARCH", cwd="/repo/example-project", eid="pi:arch", seq=0,
               ts="2026-01-02T00:00:30Z", text="nested archive file",
               refs={"files": ["archive/docs/wip/foo.md"], "commands": []})

        model = json.loads(self.run_cli(
            "touched", "docs/wip/", "--prefix", "--source", "pi", "--cwd", "example-project",
            "--after", "2026-01-01", "--before", "2026-01-03", "--no-refresh", "--json"
        ))
        self.assertEqual([m["file"] for m in model["matches"]], ["docs/wip/foo.md"])

    def test_touched_prefix_reconciles_relative_ref_against_session_cwd(self):
        # An ABSOLUTE prefix query must still match a RELATIVE ref via the session's
        # cwd. The files-index candidate cannot see that reconciliation, so prefix
        # mode uses the full file-bearing superset and lets the Python matcher decide.
        # (Regression guard: a basename/raw-file prefilter silently dropped this.)
        _event(self.conn, sid="pi:R", cwd="/repo/example-project", eid="pi:r1", seq=0,
               ts="2026-01-05T00:00:00Z", text="edit", typ="tool_call",
               refs={"files": ["src/app.py"], "commands": []})
        model = json.loads(self.run_cli(
            "touched", "/repo/example-project/src", "--prefix", "--no-refresh", "--json"))
        self.assertEqual([m["file"] for m in model["matches"]], ["src/app.py"])

    def test_touched_live_inherited_and_all_scoping(self):
        _event(self.conn, eid="pi:live", seq=0, ts="2026-01-01T00:00:00Z",
               text="live file", refs={"files": ["live.md"], "commands": []})
        _event(self.conn, sid="pi:CHILD", eid="pi:copy", seq=0,
               ts="2026-01-01T00:01:00Z", text="inherited file", inherited=1,
               refs={"files": ["copy.md"], "commands": []})
        _event(self.conn, eid="pi:dead", seq=2, ts="2026-01-01T00:02:00Z",
               text="dead file", live=0, refs={"files": ["dead.md"], "commands": []})

        model = json.loads(self.run_cli("touched", "copy.md", "--no-refresh", "--json"))
        self.assertEqual(model["matches"], [])

        model = json.loads(self.run_cli(
            "touched", "copy.md", "--include-inherited", "--no-refresh", "--json"
        ))
        self.assertEqual([m["file"] for m in model["matches"]], ["copy.md"])

        model = json.loads(self.run_cli(
            "touched", "copy.md", "--only-session", "pi:CH", "--no-refresh", "--json"
        ))
        self.assertEqual([m["session_id"] for m in model["matches"]], ["pi:CHILD"])

        model = json.loads(self.run_cli("touched", "dead.md", "--no-refresh", "--json"))
        self.assertEqual(model["matches"], [])

        model = json.loads(self.run_cli("touched", "dead.md", "--all", "--no-refresh", "--json"))
        self.assertEqual(model["matches"][0]["file"], "dead.md")
        self.assertEqual(model["matches"][0]["expand_command"],
                         "sessdb turns pi:S --around-seq 2 --all")
        self.assertEqual(model["matches"][0]["refs_command"],
                         "sessdb refs pi:S --around-seq 2 --context-turns 0 --all")


if __name__ == "__main__":
    unittest.main()

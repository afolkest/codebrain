import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codebrain import cli, db
from codebrain.adapters.base import EventRow, PlacementRow, SessionRow


def _add(conn, *, sid="pi:S", source="pi", cwd="/repo/codebrain", eid, seq, ts,
         text, refs=None, actor="user", typ="message"):
    db.upsert_session(conn, SessionRow(
        session_id=sid, source=source, cwd=cwd,
        started_at="2026-01-01T00:00:00Z", ended_at=ts,
    ))
    db.upsert_event(conn, EventRow(
        event_id=eid, origin_session_id=sid, ts=ts, actor=actor, type=typ,
        text=text, refs=refs or {"files": [], "commands": []}, raw={},
    ))
    db.upsert_placement(conn, PlacementRow(
        session_id=sid, event_id=eid, seq=seq, parent_event_id=None,
        live=1, inherited=0,
    ))


class TestVisibilityCli(unittest.TestCase):
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
        self.conn.commit()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as cm:
                cli.main(["--db", str(self.db_path), *args])
        return cm.exception.code, out.getvalue(), err.getvalue()

    def test_connect_migrates_existing_sessions_table(self):
        old_path = Path(self.tmp.name) / "old.db"
        raw = sqlite3.connect(old_path)
        raw.execute("""
            CREATE TABLE sessions (
              session_id TEXT PRIMARY KEY,
              source TEXT NOT NULL,
              machine TEXT,
              cwd TEXT,
              repo TEXT,
              created_at TEXT,
              started_at TEXT,
              ended_at TEXT,
              parent_session_id TEXT,
              relation TEXT,
              spawn_event_id TEXT,
              branch_point_event_id TEXT,
              tip_event_id TEXT,
              title TEXT
            )
        """)
        raw.commit()
        raw.close()

        conn = db.connect(old_path)
        self.addCleanup(conn.close)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}

        self.assertIn("hidden_at", cols)
        self.assertIn("hidden_reason", cols)

    def test_hide_unhide_and_hidden_list_are_structured_session_visibility(self):
        _add(self.conn, sid="pi:VISIBLE", eid="pi:v-u", seq=0,
             ts="2026-01-01T00:01:00Z", text="visible needle")
        _add(self.conn, sid="pi:HIDDEN", eid="pi:h-u", seq=0,
             ts="2026-01-01T00:02:00Z", text="hidden needle")

        out = self.run_cli("hide", "pi:HIDDEN", "--reason", "benchmark leakage", "--no-refresh")
        self.assertIn("hidden: pi:HIDDEN", out)

        rows = json.loads(self.run_cli("hidden", "--no-refresh", "--json"))
        self.assertEqual([r["session_id"] for r in rows], ["pi:HIDDEN"])
        self.assertEqual(rows[0]["hidden_reason"], "benchmark leakage")
        self.assertTrue(rows[0]["hidden_at"])

        rows = json.loads(self.run_cli("recent", "--no-refresh", "--json", "--limit", "10"))
        self.assertEqual([r["session_id"] for r in rows], ["pi:VISIBLE"])

        rows = json.loads(self.run_cli(
            "recent", "--no-refresh", "--json", "--include-hidden", "--limit", "10"
        ))
        self.assertEqual([r["session_id"] for r in rows], ["pi:HIDDEN", "pi:VISIBLE"])
        self.assertEqual(rows[0]["hidden_reason"], "benchmark leakage")
        self.assertIsNone(rows[1]["hidden_reason"])

        rows = json.loads(self.run_cli(
            "recent", "--no-refresh", "--json", "--only-hidden", "--limit", "10"
        ))
        self.assertEqual([r["session_id"] for r in rows], ["pi:HIDDEN"])

        out = self.run_cli("unhide", "pi:HIDDEN", "--no-refresh")
        self.assertIn("unhidden: pi:HIDDEN", out)
        rows = json.loads(self.run_cli("hidden", "--no-refresh", "--json"))
        self.assertEqual(rows, [])

    def test_hide_resolves_batch_before_printing_or_committing(self):
        _add(self.conn, sid="pi:ONE", eid="pi:one-u", seq=0,
             ts="2026-01-01T00:01:00Z", text="one")

        code, out, err = self.run_cli_capture(
            "hide", "pi:ONE", "pi:MISSING", "--reason", "noise", "--no-refresh"
        )

        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("no session matching 'pi:MISSING'", err)
        row = self.conn.execute(
            "SELECT hidden_at FROM sessions WHERE session_id='pi:ONE'"
        ).fetchone()
        self.assertIsNone(row["hidden_at"])

    def test_search_userlog_list_and_touched_exclude_hidden_by_default(self):
        if not db.has_fts5(self.conn):
            self.skipTest("sqlite built without FTS5")
        _add(self.conn, sid="pi:VISIBLE", eid="pi:v-u", seq=0,
             ts="2026-01-01T00:01:00Z", text="visneedle user",
             refs={"files": ["visible.py"], "commands": []})
        _add(self.conn, sid="pi:HIDDEN", eid="pi:h-u", seq=0,
             ts="2026-01-01T00:02:00Z", text="visneedle hidden user",
             refs={"files": ["hidden.py"], "commands": []})
        self.run_cli("hide", "pi:HIDDEN", "--reason", "noise", "--no-refresh")

        search = json.loads(self.run_cli("search", "visneedle", "--no-refresh", "--json"))
        self.assertEqual([r["session_id"] for r in search], ["pi:VISIBLE"])
        search = json.loads(self.run_cli(
            "search", "visneedle", "--no-refresh", "--json", "--include-hidden"
        ))
        self.assertEqual({r["session_id"] for r in search}, {"pi:VISIBLE", "pi:HIDDEN"})
        hidden_search = [r for r in search if r["session_id"] == "pi:HIDDEN"][0]
        self.assertEqual(hidden_search["hidden_reason"], "noise")

        userlog = json.loads(self.run_cli(
            "userlog", "--query", "visneedle", "--no-refresh", "--json", "--limit", "10"
        ))
        self.assertEqual([r["session_id"] for r in userlog], ["pi:VISIBLE"])

        touched = json.loads(self.run_cli("touched", "hidden.py", "--no-refresh", "--json"))
        self.assertEqual(touched["matches"], [])
        touched = json.loads(self.run_cli(
            "touched", "hidden.py", "--no-refresh", "--json", "--include-hidden"
        ))
        self.assertEqual([m["session_id"] for m in touched["matches"]], ["pi:HIDDEN"])
        self.assertEqual(touched["matches"][0]["hidden_reason"], "noise")

        listing = self.run_cli("list", "--no-refresh", "--limit", "10")
        self.assertIn("pi:VISIBLE", listing)
        self.assertNotIn("pi:HIDDEN", listing)
        listing = self.run_cli("list", "--no-refresh", "--include-hidden", "--limit", "10")
        self.assertIn("pi:HIDDEN", listing)

        text = self.run_cli(
            "search", "visneedle", "--no-refresh", "--include-hidden", "--limit", "10"
        )
        self.assertIn("hidden: noise", text)

    def test_only_session_discovery_still_requires_include_hidden(self):
        if not db.has_fts5(self.conn):
            self.skipTest("sqlite built without FTS5")
        _add(self.conn, sid="pi:HIDDEN", eid="pi:h-u", seq=0,
             ts="2026-01-01T00:02:00Z", text="privneedle hidden user",
             refs={"files": ["hidden.py"], "commands": []})
        self.run_cli("hide", "pi:HIDDEN", "--reason", "noise", "--no-refresh")

        search = json.loads(self.run_cli(
            "search", "privneedle", "--only-session", "pi:HIDDEN", "--no-refresh", "--json"
        ))
        self.assertEqual(search, [])
        search = json.loads(self.run_cli(
            "search", "privneedle", "--only-session", "pi:HIDDEN",
            "--include-hidden", "--no-refresh", "--json"
        ))
        self.assertEqual([r["session_id"] for r in search], ["pi:HIDDEN"])

        touched = json.loads(self.run_cli(
            "touched", "hidden.py", "--only-session", "pi:HIDDEN", "--no-refresh", "--json"
        ))
        self.assertEqual(touched["matches"], [])
        touched = json.loads(self.run_cli(
            "touched", "hidden.py", "--only-session", "pi:HIDDEN",
            "--include-hidden", "--no-refresh", "--json"
        ))
        self.assertEqual([m["session_id"] for m in touched["matches"]], ["pi:HIDDEN"])


if __name__ == "__main__":
    unittest.main()

"""Cursor archive integration across local refresh, collection, and pool roots."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from codebrain import collect, cursor_archive, db, ingest
from codebrain.adapters import cursor
from tests._helpers import memory_db
from tests.test_cursor_export import _header, _modern_composer, _put, _state_db


BASE_MS = 1767225600000


def _snapshot(sid="S", texts=("one",)):
    order = []
    for index, text in enumerate(texts, 1):
        bubble_id = f"b{index}"
        created_at = BASE_MS + index * 1000
        payload = {
            "bubbleId": bubble_id, "type": 1 if index == 1 else 2,
            "text": text, "createdAt": created_at,
        }
        order.append({
            "bubbleId": bubble_id, "type": payload["type"],
            "createdAt": created_at, "payload": payload,
        })
    return {
        "projectionVersion": 1, "composerId": sid, "sourceVersion": 17,
        "sourceCapability": "separate-bubbles",
        "session": {
            "composerId": sid, "createdAt": BASE_MS,
            "name": f"Cursor {sid}",
        },
        "order": order,
    }


def _tool_snapshot(sid, session_created_at):
    created_at = BASE_MS + 1000
    payload = {
        "bubbleId": "shared-tool", "type": 2, "text": "",
        "createdAt": created_at,
        "toolFormerData": {
            "name": "read_file_v2", "toolCallId": "reused-source-call",
            "status": "completed", "params": {"targetFile": "shared.py"},
            "result": {"contents": "same result"},
        },
    }
    return {
        "projectionVersion": 1, "composerId": sid, "sourceVersion": 17,
        "sourceCapability": "separate-bubbles",
        "session": {"composerId": sid, "createdAt": session_created_at},
        "order": [{
            "bubbleId": "shared-tool", "type": 2,
            "createdAt": created_at, "payload": payload,
        }],
    }


def _mutable_tool_snapshot(sid, session_created_at, *, target, result):
    snapshot = _tool_snapshot(sid, session_created_at)
    tool = snapshot["order"][0]["payload"]["toolFormerData"]
    tool["params"] = {"targetFile": target}
    tool["result"] = {"contents": result}
    return snapshot


def _live_cursor_db(path: Path):
    writer = _state_db(path)
    _header(writer, "LIVE", createdAt=BASE_MS)
    _put(writer, "composerData:LIVE", _modern_composer(
        "LIVE", ("u1",), createdAt=BASE_MS,
    ))
    _put(writer, "bubbleId:LIVE:u1", {
        "bubbleId": "u1", "type": 1, "text": "from live Cursor",
        "createdAt": BASE_MS + 1000,
    })
    writer.commit()
    return writer


class TestCursorRefreshIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / "archive"
        self.conn = memory_db()
        self.addCleanup(self.conn.close)

    def test_default_refresh_exports_before_ingest_then_is_noop(self):
        writer = _live_cursor_db(self.root / "state.vscdb")
        self.addCleanup(writer.close)
        with mock.patch.object(ingest, "DEFAULT_CURSOR_DB", self.root / "state.vscdb"), \
             mock.patch.object(ingest, "DEFAULT_CURSOR_ROOT", self.archive):
            first = ingest.refresh(self.conn, sources=("cursor",), machine="local")
            second = ingest.refresh(self.conn, sources=("cursor",), machine="local")

        self.assertEqual((first["files"], first["sessions"], first["errors"]), (1, 1, 0))
        self.assertEqual(second["files"], 0)
        self.assertEqual(len(cursor_archive.discover_heads(self.archive)), 1)
        row = self.conn.execute(
            "SELECT machine FROM sessions WHERE session_id='cursor:LIVE'"
        ).fetchone()
        self.assertEqual(row["machine"], "local")

    def test_refresh_export_is_opportunistic_and_collect_is_authoritative(self):
        cursor_archive.publish_snapshot(_snapshot(), self.archive)
        fake_db = self.root / "state.vscdb"
        fake_db.write_bytes(b"present")
        stats_stub = {"candidates": 0, "published": 0, "unchanged": 0,
                      "skipped": 0, "errors": 0, "busy": 0}
        with mock.patch.object(ingest, "DEFAULT_CURSOR_DB", fake_db), \
             mock.patch.object(ingest, "DEFAULT_CURSOR_ROOT", self.archive), \
             mock.patch("codebrain.ingest.cursor_archive.export_cursor",
                        return_value=dict(stats_stub)) as export:
            ingest.refresh(self.conn, sources=("cursor",))
        export.assert_called_once()
        self.assertFalse(export.call_args.kwargs["authoritative"])

        with mock.patch.object(ingest, "DEFAULT_CURSOR_DB", fake_db), \
             mock.patch.object(ingest, "DEFAULT_CURSOR_ROOT", self.archive), \
             mock.patch("codebrain.ingest.cursor_archive.export_cursor",
                        return_value=dict(stats_stub)) as export:
            collect.collect_source("cursor", pool_root=self.root / "pool")
        export.assert_called_once()
        self.assertTrue(export.call_args.kwargs["authoritative"])

    def test_explicit_archive_never_exports(self):
        cursor_archive.publish_snapshot(_snapshot(), self.archive)
        with mock.patch("codebrain.ingest.cursor_archive.export_cursor") as export:
            stats = ingest.refresh(
                self.conn, sources=("cursor",),
                roots={"cursor": self.archive},
            )
        export.assert_not_called()
        self.assertEqual((stats["sessions"], stats["errors"]), (1, 0))

    def test_failed_export_retains_last_good_archive(self):
        cursor_archive.publish_snapshot(_snapshot(), self.archive)
        fake_db = self.root / "state.vscdb"
        fake_db.write_bytes(b"present")
        with mock.patch.object(ingest, "DEFAULT_CURSOR_DB", fake_db), \
             mock.patch.object(ingest, "DEFAULT_CURSOR_ROOT", self.archive), \
             mock.patch(
                 "codebrain.ingest.cursor_archive.export_cursor",
                 side_effect=cursor_archive.CursorArchiveError("locked"),
             ):
            stats = ingest.refresh(self.conn, sources=("cursor",))

        self.assertEqual((stats["sessions"], stats["errors"]), (1, 1))
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM sessions WHERE session_id='cursor:S'"
        ).fetchone())

    def test_new_rollback_revision_authoritatively_replaces_placements(self):
        cursor_archive.publish_snapshot(_snapshot(texts=("one", "two")), self.archive)
        ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )
        cursor_archive.publish_snapshot(_snapshot(texts=("one",)), self.archive)
        stats = ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )

        self.assertEqual((stats["files"], stats["sessions"]), (1, 1))
        placements = self.conn.execute(
            "SELECT event_id FROM session_events WHERE session_id='cursor:S'"
        ).fetchall()
        events = self.conn.execute("SELECT event_id FROM events").fetchall()
        tip = self.conn.execute(
            "SELECT tip_event_id FROM sessions WHERE session_id='cursor:S'"
        ).fetchone()["tip_event_id"]
        self.assertEqual(len(placements), 1)
        self.assertEqual(len(events), 2)
        self.assertEqual(tip, "cursor:b1:1767225601000:message")

    def test_bad_session_head_isolated_from_valid_session(self):
        cursor_archive.publish_snapshot(_snapshot("GOOD"), self.archive)
        bad = _snapshot("BAD")
        bad["projectionVersion"] = 2
        cursor_archive.publish_snapshot(bad, self.archive)
        stats = ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )
        rows = self.conn.execute("SELECT session_id FROM sessions").fetchall()
        self.assertEqual([row["session_id"] for row in rows], ["cursor:GOOD"])
        self.assertEqual((stats["sessions"], stats["errors"]), (1, 1))

    def test_copied_tool_call_and_result_pair_without_event_conflicts(self):
        cursor_archive.publish_snapshot(
            _tool_snapshot("PARENT", BASE_MS), self.archive
        )
        cursor_archive.publish_snapshot(
            _tool_snapshot("CHILD", BASE_MS + 2000), self.archive
        )
        stats = ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )
        events = self.conn.execute(
            "SELECT event_id, type, tool_call_event_id FROM events ORDER BY event_id"
        ).fetchall()
        placements = self.conn.execute(
            "SELECT session_id, event_id, inherited FROM session_events "
            "ORDER BY session_id, event_id"
        ).fetchall()
        call_id = "cursor:shared-tool:1767225601000:call"
        result = next(row for row in events if row["type"] == "tool_result")
        self.assertEqual((stats["conflicts"], len(events), len(placements)), (0, 2, 4))
        self.assertEqual(result["tool_call_event_id"], call_id)
        self.assertEqual(
            {(row["session_id"], row["inherited"]) for row in placements},
            {("cursor:PARENT", 0), ("cursor:CHILD", 1)},
        )

    def test_later_revision_updates_stable_tool_events_and_indexes(self):
        old = _mutable_tool_snapshot(
            "AUTHOR", BASE_MS, target="oldquartz.py", result="oldquartz result",
        )
        new = _mutable_tool_snapshot(
            "AUTHOR", BASE_MS, target="newzircon.py", result="newzircon result",
        )
        cursor_archive.publish_snapshot(old, self.archive)
        ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )
        cursor_archive.publish_snapshot(new, self.archive)
        stats = ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )

        call_id = "cursor:shared-tool:1767225601000:call"
        result_id = "cursor:shared-tool:1767225601000:result"
        call = self.conn.execute(
            "SELECT text, refs, origin_session_id FROM events WHERE event_id=?",
            (call_id,),
        ).fetchone()
        result = self.conn.execute(
            "SELECT text, tool_call_event_id FROM events WHERE event_id=?",
            (result_id,),
        ).fetchone()
        files = self.conn.execute(
            "SELECT file FROM file_refs WHERE event_id=?", (call_id,)
        ).fetchall()
        head = self.conn.execute(
            "SELECT revision FROM cursor_session_heads "
            "WHERE session_id='cursor:AUTHOR'"
        ).fetchone()
        self.assertEqual((stats["sessions"], stats["conflicts"]), (1, 0))
        self.assertIn("newzircon.py", call["text"])
        self.assertEqual(call["origin_session_id"], "cursor:AUTHOR")
        self.assertEqual(json.loads(call["refs"])["files"], ["newzircon.py"])
        self.assertEqual([row["file"] for row in files], ["newzircon.py"])
        self.assertIn("newzircon result", result["text"])
        self.assertEqual(result["tool_call_event_id"], call_id)
        self.assertEqual(head["revision"], 2)

    def test_later_revision_updates_message_under_stable_identity(self):
        cursor_archive.publish_snapshot(
            _snapshot("AUTHOR", texts=("old message",)), self.archive
        )
        ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )
        cursor_archive.publish_snapshot(
            _snapshot("AUTHOR", texts=("revised message",)), self.archive
        )
        stats = ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )

        rows = self.conn.execute(
            "SELECT event_id, text, origin_session_id FROM events"
        ).fetchall()
        self.assertEqual((stats["sessions"], stats["conflicts"]), (1, 0))
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            (rows[0]["event_id"], rows[0]["text"], rows[0]["origin_session_id"]),
            (
                "cursor:b1:1767225601000:message",
                "revised message",
                "cursor:AUTHOR",
            ),
        )

    def test_authored_and_inherited_copies_converge_in_either_order(self):
        outcomes = []
        for child_first in (True, False):
            with self.subTest(child_first=child_first):
                conn = memory_db()
                self.addCleanup(conn.close)
                child_root = self.root / f"child-{child_first}"
                parent_root = self.root / f"parent-{child_first}"
                cursor_archive.publish_snapshot(
                    _mutable_tool_snapshot(
                        "CHILD", BASE_MS + 2000,
                        target="stale.py", result="stale result",
                    ),
                    child_root,
                )
                cursor_archive.publish_snapshot(
                    _mutable_tool_snapshot(
                        "PARENT", BASE_MS,
                        target="authoritative.py", result="authoritative result",
                    ),
                    parent_root,
                )
                roots = (child_root, parent_root) if child_first \
                    else (parent_root, child_root)
                for root in roots:
                    ingest.ingest_source(conn, "cursor", raw_root=root)
                call_id = "cursor:shared-tool:1767225601000:call"
                row = conn.execute(
                    "SELECT text, refs, origin_session_id FROM events WHERE event_id=?",
                    (call_id,),
                ).fetchone()
                placements = conn.execute(
                    "SELECT session_id, inherited FROM session_events "
                    "WHERE event_id=? ORDER BY session_id", (call_id,),
                ).fetchall()
                outcomes.append((
                    dict(row),
                    [(p["session_id"], p["inherited"]) for p in placements],
                ))
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0][0]["origin_session_id"], "cursor:PARENT")
        self.assertIn("authoritative.py", outcomes[0][0]["text"])
        self.assertEqual(
            outcomes[0][1], [("cursor:CHILD", 1), ("cursor:PARENT", 0)]
        )

    def test_lower_rank_from_another_root_cannot_regress_session(self):
        newer_root = self.root / "newer"
        stale_root = self.root / "stale"
        old = _mutable_tool_snapshot(
            "AUTHOR", BASE_MS, target="old.py", result="old result",
        )
        new = _mutable_tool_snapshot(
            "AUTHOR", BASE_MS, target="new.py", result="new result",
        )
        cursor_archive.publish_snapshot(old, stale_root)
        cursor_archive.publish_snapshot(old, newer_root)
        cursor_archive.publish_snapshot(new, newer_root)

        ingest.ingest_source(self.conn, "cursor", raw_root=newer_root)
        stats = ingest.ingest_source(self.conn, "cursor", raw_root=stale_root)

        row = self.conn.execute(
            "SELECT text FROM events "
            "WHERE event_id='cursor:shared-tool:1767225601000:call'"
        ).fetchone()
        head = self.conn.execute(
            "SELECT revision FROM cursor_session_heads "
            "WHERE session_id='cursor:AUTHOR'"
        ).fetchone()
        self.assertEqual((stats["sessions"], stats["skipped"]), (0, 1))
        self.assertIn("new.py", row["text"])
        self.assertEqual(head["revision"], 2)

    def test_failed_revision_write_rolls_back_content_and_head(self):
        old = _mutable_tool_snapshot(
            "AUTHOR", BASE_MS, target="old.py", result="old result",
        )
        new = _mutable_tool_snapshot(
            "AUTHOR", BASE_MS, target="new.py", result="new result",
        )
        cursor_archive.publish_snapshot(old, self.archive)
        ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )
        cursor_archive.publish_snapshot(new, self.archive)
        # Fail at the head-recording step: it always runs for a cursor session,
        # unlike placement writes, which a content-only revision no longer touches
        # (unchanged placements are skipped, not rewritten).
        with mock.patch(
                "codebrain.ingest.record_cursor_head",
                side_effect=RuntimeError("injected head failure")):
            failed = ingest.refresh(
                self.conn, sources=("cursor",), roots={"cursor": self.archive}
            )

        call_id = "cursor:shared-tool:1767225601000:call"
        text_after_failure = self.conn.execute(
            "SELECT text FROM events WHERE event_id=?", (call_id,)
        ).fetchone()["text"]
        head_after_failure = self.conn.execute(
            "SELECT revision FROM cursor_session_heads "
            "WHERE session_id='cursor:AUTHOR'"
        ).fetchone()["revision"]
        retried = ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )
        text_after_retry = self.conn.execute(
            "SELECT text FROM events WHERE event_id=?", (call_id,)
        ).fetchone()["text"]
        self.assertEqual(failed["errors"], 1)
        self.assertIn("old.py", text_after_failure)
        self.assertEqual(head_after_failure, 1)
        self.assertEqual(retried["sessions"], 1)
        self.assertIn("new.py", text_after_retry)

    def test_lost_head_race_is_a_rolled_back_skip_not_an_error(self):
        path = cursor_archive.publish_snapshot(_snapshot("RACE"), self.archive)
        processed = set()
        with mock.patch(
                "codebrain.ingest.record_cursor_head", return_value=False):
            stats = ingest._ingest(
                self.conn, [path], cursor.parse_file,
                processed_paths=processed,
            )
        self.assertEqual((stats["skipped"], stats["errors"]), (1, 0))
        self.assertEqual(processed, {path})
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM sessions WHERE session_id='cursor:RACE'"
        ).fetchone())

    def test_warm_cache_survives_database_reopen_without_json_validation(self):
        cursor_archive.publish_snapshot(_snapshot("WARM"), self.archive)
        db_path = self.root / "cache.db"
        first_conn = db.connect(db_path)
        ingest.refresh(
            first_conn, sources=("cursor",), roots={"cursor": self.archive}
        )
        first_conn.close()

        reopened = db.connect(db_path)
        self.addCleanup(reopened.close)
        with mock.patch(
                "codebrain.cursor_archive.select_session_head",
                side_effect=AssertionError("warm cache revalidated JSON")), \
             mock.patch(
                "codebrain.cursor_archive._read_private_text",
                side_effect=AssertionError("warm cache opened JSON")):
            stats = ingest.refresh(
                reopened, sources=("cursor",), roots={"cursor": self.archive}
            )
        self.assertEqual((stats["files"], stats["errors"]), (0, 0))

    def test_one_changed_session_validates_only_its_chain(self):
        cursor_archive.publish_snapshot(_snapshot("ONE"), self.archive)
        cursor_archive.publish_snapshot(_snapshot("TWO"), self.archive)
        ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )
        cursor_archive.publish_snapshot(
            _snapshot("ONE", texts=("one", "changed")), self.archive
        )

        original = cursor_archive.select_session_head
        with mock.patch(
                "codebrain.cursor_archive.select_session_head",
                wraps=original) as select:
            stats = ingest.refresh(
                self.conn, sources=("cursor",), roots={"cursor": self.archive}
            )
        self.assertEqual((stats["sessions"], select.call_count), (1, 1))
        self.assertEqual(
            select.call_args.args[0].session_key,
            cursor_archive.session_directory(self.archive, "ONE").parent.name,
        )

    def test_out_of_order_predecessor_arrival_unlocks_latest_head(self):
        source = self.root / "ordered-source"
        target = self.root / "ordered-target"
        cursor_archive.publish_snapshot(_snapshot("ORDER"), source)
        cursor_archive.publish_snapshot(
            _snapshot("ORDER", texts=("one", "latest")), source
        )
        revisions = cursor_archive.discover_revisions(source)
        destination_dir = cursor_archive.session_directory(target, "ORDER")
        destination_dir.mkdir(parents=True)
        (destination_dir / revisions[1].name).write_bytes(
            cursor_archive.read_revision_bytes(revisions[1])
        )

        first = ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": target}
        )
        (destination_dir / revisions[0].name).write_bytes(
            cursor_archive.read_revision_bytes(revisions[0])
        )
        second = ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": target}
        )

        tip = self.conn.execute(
            "SELECT text FROM transcript WHERE session_id='cursor:ORDER' "
            "ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        head = self.conn.execute(
            "SELECT revision FROM cursor_session_heads "
            "WHERE session_id='cursor:ORDER'"
        ).fetchone()
        self.assertEqual((first["files"], second["sessions"]), (0, 1))
        self.assertEqual((tip["text"], head["revision"]), ("latest", 2))

    def test_cache_write_failure_fails_open_and_revalidates(self):
        cursor_archive.publish_snapshot(_snapshot("RETRY"), self.archive)
        self.conn.execute(
            "CREATE TRIGGER fail_cursor_cache BEFORE INSERT ON cursor_archive_heads "
            "BEGIN SELECT RAISE(FAIL, 'injected cache failure'); END"
        )
        self.conn.commit()
        first = ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )
        self.conn.execute("DROP TRIGGER fail_cursor_cache")
        self.conn.commit()

        original = cursor_archive.select_session_head
        with mock.patch(
                "codebrain.cursor_archive.select_session_head",
                wraps=original) as select:
            second = ingest.refresh(
                self.conn, sources=("cursor",), roots={"cursor": self.archive}
            )
        self.assertEqual(first["errors"], 1)
        self.assertEqual((second["skipped"], select.call_count), (1, 1))

    def test_malformed_cache_row_and_validator_bump_fail_open(self):
        cursor_archive.publish_snapshot(_snapshot("CACHE"), self.archive)
        ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )
        original = cursor_archive.select_session_head
        mutations = (
            "selected_digest='not-a-digest'",
            "selected_revision=selected_revision+1",
            "selected_session_id='cursor:OTHER'",
            "selected_session_id='CACHE'",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.conn.execute(
                    f"UPDATE cursor_archive_heads SET {mutation}"
                )
                self.conn.commit()
                with mock.patch(
                        "codebrain.cursor_archive.select_session_head",
                        wraps=original) as malformed_select:
                    malformed = ingest.refresh(
                        self.conn, sources=("cursor",),
                        roots={"cursor": self.archive},
                    )
                self.assertEqual(
                    (malformed["skipped"], malformed_select.call_count), (1, 1)
                )

        with mock.patch.object(
                ingest, "CURSOR_ARCHIVE_VALIDATOR_VERSION", 2), \
             mock.patch(
                "codebrain.cursor_archive.select_session_head",
                wraps=original) as bumped_select:
            bumped = ingest.refresh(
                self.conn, sources=("cursor",), roots={"cursor": self.archive}
            )
        self.assertEqual((bumped["skipped"], bumped_select.call_count), (1, 1))

    def test_missing_or_unsafe_root_retains_cache_for_recovery(self):
        cursor_archive.publish_snapshot(_snapshot("ROOT"), self.archive)
        ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )
        detached = self.root / "detached-archive"
        self.archive.rename(detached)
        missing = ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )
        cached_while_missing = self.conn.execute(
            "SELECT COUNT(*) FROM cursor_archive_heads"
        ).fetchone()[0]
        self.archive.symlink_to(detached, target_is_directory=True)
        unsafe = ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )
        self.archive.unlink()
        detached.rename(self.archive)
        with mock.patch(
                "codebrain.cursor_archive.select_session_head",
                side_effect=AssertionError("recovered unchanged root revalidated")):
            recovered = ingest.refresh(
                self.conn, sources=("cursor",), roots={"cursor": self.archive}
            )
        self.assertEqual((missing["errors"], cached_while_missing), (0, 1))
        self.assertEqual(unsafe["errors"], 1)
        self.assertEqual((recovered["files"], recovered["errors"]), (0, 0))

    def test_archive_cache_isolated_by_root_for_same_session_hash(self):
        one = self.root / "root-one"
        two = self.root / "root-two"
        cursor_archive.publish_snapshot(_snapshot("SAME", texts=("one",)), one)
        cursor_archive.publish_snapshot(_snapshot("SAME", texts=("two",)), two)
        ingest.refresh(self.conn, sources=("cursor",), roots={"cursor": one})
        ingest.refresh(self.conn, sources=("cursor",), roots={"cursor": two})

        roots = self.conn.execute(
            "SELECT root, COUNT(*) AS n FROM cursor_archive_heads GROUP BY root"
        ).fetchall()
        self.assertEqual(len(roots), 2)
        self.assertEqual({row["n"] for row in roots}, {1})

    def test_equal_revision_divergence_converges_in_both_root_orders(self):
        alpha_root = self.root / "equal-alpha"
        beta_root = self.root / "equal-beta"
        alpha_path = cursor_archive.publish_snapshot(
            _snapshot("EQUAL", texts=("alpha",)), alpha_root
        )
        beta_path = cursor_archive.publish_snapshot(
            _snapshot("EQUAL", texts=("beta",)), beta_root
        )
        expected_digest, expected_text = max(
            (alpha_path.name.split("-", 1)[1][:-5], "alpha"),
            (beta_path.name.split("-", 1)[1][:-5], "beta"),
        )
        outcomes = []
        for roots in ((alpha_root, beta_root), (beta_root, alpha_root)):
            conn = memory_db()
            try:
                for root in roots:
                    ingest.ingest_source(conn, "cursor", raw_root=root)
                event = conn.execute(
                    "SELECT text, origin_session_id FROM events"
                ).fetchone()
                head = conn.execute(
                    "SELECT revision, digest FROM cursor_session_heads "
                    "WHERE session_id='cursor:EQUAL'"
                ).fetchone()
                placements = conn.execute(
                    "SELECT COUNT(*) FROM session_events "
                    "WHERE session_id='cursor:EQUAL'"
                ).fetchone()[0]
                outcomes.append((dict(event), tuple(head), placements))
            finally:
                conn.close()
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertEqual(outcomes[0], (
            {"text": expected_text, "origin_session_id": "cursor:EQUAL"},
            (1, expected_digest),
            1,
        ))

    def test_corrupt_fallback_is_handled_once_without_canonical_regression(self):
        first = cursor_archive.publish_snapshot(
            _snapshot("FALLBACK", texts=("old",)), self.archive
        )
        second = cursor_archive.publish_snapshot(
            _snapshot("FALLBACK", texts=("old", "new")), self.archive
        )
        ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )
        second_bytes = second.read_bytes()
        second.write_text("{}", encoding="utf-8")
        fallback = ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )
        with mock.patch(
                "codebrain.cursor_archive.select_session_head",
                side_effect=AssertionError("handled fallback revalidated")):
            noop = ingest.refresh(
                self.conn, sources=("cursor",), roots={"cursor": self.archive}
            )

        second.write_bytes(second_bytes)
        repaired = ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )
        placements = self.conn.execute(
            "SELECT COUNT(*) AS n FROM session_events "
            "WHERE session_id='cursor:FALLBACK'"
        ).fetchone()["n"]
        head = self.conn.execute(
            "SELECT revision FROM cursor_session_heads "
            "WHERE session_id='cursor:FALLBACK'"
        ).fetchone()["revision"]
        self.assertIsNotNone(first)
        self.assertEqual((fallback["skipped"], noop["files"]), (1, 0))
        self.assertEqual((repaired["skipped"], placements, head), (1, 2, 2))

    def test_scaled_warm_refresh_opens_no_revision_json(self):
        for index in range(200):
            snapshot = _snapshot(f"SCALE-{index}")
            bubble_id = f"scale-bubble-{index}"
            snapshot["order"][0]["bubbleId"] = bubble_id
            snapshot["order"][0]["payload"]["bubbleId"] = bubble_id
            cursor_archive.publish_snapshot(snapshot, self.archive)
        ingest.refresh(
            self.conn, sources=("cursor",), roots={"cursor": self.archive}
        )

        with mock.patch(
                "codebrain.cursor_archive._read_private_text",
                side_effect=AssertionError("warm scale refresh opened JSON")):
            stats = ingest.refresh(
                self.conn, sources=("cursor",), roots={"cursor": self.archive}
            )
        self.assertEqual((stats["files"], stats["errors"]), (0, 0))


class TestCursorCollectionIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / "archive"
        self.pool = self.root / "pool"

    def test_discovery_and_collection_copy_only_valid_complete_revisions(self):
        one = cursor_archive.publish_snapshot(_snapshot(texts=("one",)), self.archive)
        two = cursor_archive.publish_snapshot(
            _snapshot(texts=("one", "two")), self.archive
        )
        (self.archive / "exporter-state.json").write_text("{}", encoding="utf-8")
        (self.archive / "fake.vscdb").write_text("private", encoding="utf-8")
        (one.parent / "malformed.json").write_text("{}", encoding="utf-8")
        (one.parent / ".future.json.part").write_text("partial", encoding="utf-8")
        outside = self.root / "outside.json"
        outside.write_text(json.dumps({"secret": True}), encoding="utf-8")
        (one.parent / "linked.json").symlink_to(outside)

        self.assertEqual(collect.discover("cursor", self.archive), [one, two])
        first = collect.collect_source(
            "cursor", raw_root=self.archive, pool_root=self.pool, machine="mini"
        )
        second = collect.collect_source(
            "cursor", raw_root=self.archive, pool_root=self.pool, machine="mini"
        )
        names = {p.name for p in self.pool.rglob("*") if p.is_file()}
        self.assertEqual((first["new"], second["unchanged"]), (2, 2))
        self.assertEqual(names, {one.name, two.name})

    def test_existing_revision_is_never_overwritten(self):
        one = cursor_archive.publish_snapshot(_snapshot(), self.archive)
        collect.collect_source(
            "cursor", raw_root=self.archive, pool_root=self.pool, machine="mini"
        )
        pooled = self.pool / "raw" / "mini" / "cursor" / one.relative_to(self.archive)
        pooled.write_text("foreign", encoding="utf-8")
        stats = collect.collect_source(
            "cursor", raw_root=self.archive, pool_root=self.pool, machine="mini"
        )
        self.assertEqual((stats["updated"], stats["errors"]), (0, 1))
        self.assertEqual(pooled.read_text(encoding="utf-8"), "foreign")

    def test_existing_revision_symlink_is_not_read_or_replaced(self):
        one = cursor_archive.publish_snapshot(_snapshot(), self.archive)
        collect.collect_source(
            "cursor", raw_root=self.archive, pool_root=self.pool, machine="mini"
        )
        pooled = self.pool / "raw" / "mini" / "cursor" / one.relative_to(self.archive)
        pooled.unlink()
        outside = self.root / "outside-revision.json"
        outside.write_text("foreign", encoding="utf-8")
        pooled.symlink_to(outside)

        stats = collect.collect_source(
            "cursor", raw_root=self.archive, pool_root=self.pool, machine="mini"
        )

        self.assertEqual((stats["unchanged"], stats["errors"]), (0, 1))
        self.assertTrue(pooled.is_symlink())
        self.assertEqual(outside.read_text(encoding="utf-8"), "foreign")

    def test_concurrent_revision_arrival_is_never_overwritten(self):
        one = cursor_archive.publish_snapshot(_snapshot(), self.archive)
        destination = (
            self.pool / "raw" / "mini" / "cursor" / one.relative_to(self.archive)
        )
        revision_bytes = cursor_archive.read_revision_bytes(one)

        def arrive(_source, _target, **_kwargs):
            destination.write_bytes(revision_bytes)
            raise FileExistsError("synced concurrently")

        with mock.patch("codebrain.collect.os.link", side_effect=arrive):
            stats = collect.collect_source(
                "cursor", raw_root=self.archive,
                pool_root=self.pool, machine="mini",
            )
        self.assertEqual((stats["new"], stats["unchanged"], stats["errors"]), (0, 1, 0))
        self.assertEqual(destination.read_bytes(), revision_bytes)

    def test_new_revision_fsyncs_file_and_destination_directory(self):
        cursor_archive.publish_snapshot(_snapshot(), self.archive)
        real_fsync = collect.os.fsync
        modes = []

        def record(fd):
            modes.append(collect.os.fstat(fd).st_mode)
            return real_fsync(fd)

        with mock.patch("codebrain.collect.os.fsync", side_effect=record):
            stats = collect.collect_source(
                "cursor", raw_root=self.archive,
                pool_root=self.pool, machine="mini",
            )
        self.assertEqual(stats["new"], 1)
        self.assertTrue(any(collect.stat.S_ISREG(mode) for mode in modes))
        # raw, machine, source, sessions, session, and revisions each fsync the
        # parent edge that made them durable; publication fsyncs revisions again.
        self.assertGreaterEqual(
            sum(collect.stat.S_ISDIR(mode) for mode in modes), 7,
        )

    def test_destination_symlinks_cannot_escape_pool(self):
        cursor_archive.publish_snapshot(_snapshot(), self.archive)
        revision = cursor_archive.discover_revisions(self.archive)[0]
        session_hash = revision.parent.parent.name
        for component in (
                "parent", "pool", "raw", "machine", "source",
                "sessions", "session", "revisions"):
            with self.subTest(component=component):
                case = self.root / f"symlink-{component}"
                case.mkdir()
                pool = case / "pool"
                outside = case / "outside"
                outside.mkdir()
                victim = outside / "victim.part"
                victim.write_text("do not delete", encoding="utf-8")
                owned_victim = outside / f".{revision.name}.4242.part"
                owned_victim.write_text("also do not delete", encoding="utf-8")
                old = time.time() - 7200
                os.utime(victim, (old, old))
                os.utime(owned_victim, (old, old))

                if component == "parent":
                    parent = case / "pool-parent"
                    parent.symlink_to(outside, target_is_directory=True)
                    pool = parent / "pool"
                elif component == "pool":
                    pool.symlink_to(outside, target_is_directory=True)
                elif component == "raw":
                    pool.mkdir()
                    (pool / "raw").symlink_to(outside, target_is_directory=True)
                elif component == "machine":
                    (pool / "raw").mkdir(parents=True)
                    (pool / "raw" / "mini").symlink_to(
                        outside, target_is_directory=True,
                    )
                elif component == "source":
                    (pool / "raw" / "mini").mkdir(parents=True)
                    (pool / "raw" / "mini" / "cursor").symlink_to(
                        outside, target_is_directory=True,
                    )
                elif component == "sessions":
                    (pool / "raw" / "mini" / "cursor").mkdir(parents=True)
                    (pool / "raw" / "mini" / "cursor" / "sessions").symlink_to(
                        outside, target_is_directory=True,
                    )
                elif component == "session":
                    sessions = pool / "raw" / "mini" / "cursor" / "sessions"
                    sessions.mkdir(parents=True)
                    (sessions / session_hash).symlink_to(
                        outside, target_is_directory=True,
                    )
                else:
                    session = (
                        pool / "raw" / "mini" / "cursor" / "sessions"
                        / session_hash
                    )
                    session.mkdir(parents=True)
                    (session / "revisions").symlink_to(
                        outside, target_is_directory=True,
                    )

                stats = collect.collect_source(
                    "cursor", raw_root=self.archive,
                    pool_root=pool, machine="mini",
                )
                self.assertEqual(stats["errors"], 1)
                self.assertEqual(victim.read_text(encoding="utf-8"), "do not delete")
                self.assertEqual(
                    owned_victim.read_text(encoding="utf-8"), "also do not delete",
                )
                self.assertEqual(set(outside.iterdir()), {victim, owned_victim})

    def test_stale_prune_removes_only_owned_cursor_temp_names(self):
        revision = cursor_archive.publish_snapshot(_snapshot(), self.archive)
        collect.collect_source(
            "cursor", raw_root=self.archive, pool_root=self.pool, machine="mini"
        )
        pooled = (
            self.pool / "raw" / "mini" / "cursor"
            / revision.relative_to(self.archive)
        )
        unrelated = pooled.parent / "victim.part"
        unrelated.write_text("not collector-owned", encoding="utf-8")
        owned = pooled.parent / f".{pooled.name}.999999.part"
        owned.write_text("torn", encoding="utf-8")
        old = time.time() - 7200
        os.utime(unrelated, (old, old))
        os.utime(owned, (old, old))

        stats = collect.collect_source(
            "cursor", raw_root=self.archive, pool_root=self.pool, machine="mini"
        )

        self.assertEqual((stats["unchanged"], stats["errors"]), (1, 0))
        self.assertTrue(unrelated.exists())
        self.assertFalse(owned.exists())

    def test_default_collection_exports_but_explicit_root_does_not(self):
        writer = _live_cursor_db(self.root / "state.vscdb")
        self.addCleanup(writer.close)
        with mock.patch.object(ingest, "DEFAULT_CURSOR_DB", self.root / "state.vscdb"), \
             mock.patch.object(ingest, "DEFAULT_CURSOR_ROOT", self.archive):
            stats = collect.collect_source(
                "cursor", pool_root=self.pool, machine="mini"
            )
        self.assertEqual((stats["new"], stats["errors"]), (1, 0))

        with mock.patch("codebrain.ingest.cursor_archive.export_cursor") as export:
            collect.collect_source(
                "cursor", raw_root=self.archive,
                pool_root=self.root / "other-pool", machine="mini",
            )
        export.assert_not_called()

    def test_remote_pool_round_trip_preserves_chain_and_machine(self):
        cursor_archive.publish_snapshot(_snapshot(texts=("one",)), self.archive)
        cursor_archive.publish_snapshot(_snapshot(texts=("one", "latest")), self.archive)
        collect.collect_source(
            "cursor", raw_root=self.archive, pool_root=self.pool, machine="mini"
        )
        conn = memory_db()
        self.addCleanup(conn.close)
        with mock.patch("codebrain.ingest.cursor_archive.export_cursor") as export:
            stats = ingest.refresh_pool(
                conn, self.pool, sources=("cursor",), local_machines={"local"}
            )
        export.assert_not_called()
        row = conn.execute(
            "SELECT machine, tip_event_id FROM sessions WHERE session_id='cursor:S'"
        ).fetchone()
        text = conn.execute(
            "SELECT text FROM transcript WHERE session_id='cursor:S' ORDER BY seq DESC LIMIT 1"
        ).fetchone()["text"]
        self.assertEqual((stats["sessions"], row["machine"], text), (1, "mini", "latest"))


if __name__ == "__main__":
    unittest.main()

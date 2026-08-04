"""Cursor archive integration across local refresh, collection, and pool roots."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from codebrain import collect, cursor_archive, ingest
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

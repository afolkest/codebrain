"""Cursor archive integration across local refresh, collection, and pool roots."""
from __future__ import annotations

import json
import tempfile
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

    def test_concurrent_revision_arrival_is_never_overwritten(self):
        one = cursor_archive.publish_snapshot(_snapshot(), self.archive)
        destination = (
            self.pool / "raw" / "mini" / "cursor" / one.relative_to(self.archive)
        )
        revision_bytes = cursor_archive.read_revision_bytes(one)

        def arrive(_source, target, **_kwargs):
            Path(target).write_bytes(revision_bytes)
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
        self.assertTrue(any(collect.stat.S_ISDIR(mode) for mode in modes))

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

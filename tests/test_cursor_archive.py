"""Immutable, reconstructible archive for safe Cursor projections."""
from __future__ import annotations

import json
import multiprocessing
import os
import queue
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from codebrain import cursor_archive, cursor_export
from tests.test_cursor_export import _header, _modern_composer, _put, _state_db


def _snapshot(sid="S", texts=("one",), title="title"):
    return {
        "projectionVersion": 1, "composerId": sid, "sourceVersion": 17,
        "sourceCapability": "separate-bubbles",
        "session": {"composerId": sid, "createdAt": 1000, "name": title},
        "order": [
            {"bubbleId": f"b{i}", "type": 1 if i == 0 else 2,
             "createdAt": f"2026-01-01T00:00:0{i}Z",
             "payload": {"bubbleId": f"b{i}", "type": 1 if i == 0 else 2,
                         "text": text}}
            for i, text in enumerate(texts)
        ],
    }


def _lock_probe(root, ready, acquired):
    ready.put(True)
    with cursor_archive.archive_lock(Path(root)):
        acquired.put(True)


class TestRevisionArchive(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_first_publish_is_private_deterministic_and_noop_on_repeat(self):
        snapshot = _snapshot()
        path = cursor_archive.publish_snapshot(snapshot, self.root)
        self.assertIsNotNone(path)
        self.assertEqual(path.parent.parent.name,
                         cursor_archive.session_directory(self.root, "S").parent.name)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertIsNone(cursor_archive.publish_snapshot(snapshot, self.root))
        self.assertEqual(cursor_archive.read_latest_snapshot(path), snapshot)
        self.assertEqual(cursor_archive.discover_heads(self.root), [path])

    def test_archive_lock_serializes_separate_processes(self):
        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        acquired = context.Queue()
        with cursor_archive.archive_lock(self.root):
            process = context.Process(
                target=_lock_probe, args=(self.root, ready, acquired)
            )
            process.start()
            self.addCleanup(process.join, 1)
            self.addCleanup(lambda: process.is_alive() and process.terminate())
            self.assertTrue(ready.get(timeout=3))
            with self.assertRaises(queue.Empty):
                acquired.get(timeout=0.1)
        self.assertTrue(acquired.get(timeout=3))
        process.join(timeout=3)
        self.assertEqual(process.exitcode, 0)

    def test_changed_revision_stores_only_new_payloads_and_rolls_back_order(self):
        first = cursor_archive.publish_snapshot(_snapshot(texts=("one", "two")), self.root)
        second = cursor_archive.publish_snapshot(
            _snapshot(texts=("one", "two", "three")), self.root
        )
        third_snapshot = _snapshot(texts=("one",), title="rolled back")
        third = cursor_archive.publish_snapshot(third_snapshot, self.root)

        first_data = json.loads(first.read_text())
        second_data = json.loads(second.read_text())
        third_data = json.loads(third.read_text())
        self.assertEqual(len(first_data["payloads"]), 2)
        self.assertEqual(len(second_data["payloads"]), 1)
        self.assertEqual(len(third_data["payloads"]), 0)
        self.assertEqual(cursor_archive.read_latest_snapshot(third), third_snapshot)

    def test_exact_snapshot_cycles_remain_reconstructible(self):
        snapshots = [
            _snapshot(texts=(text,)) for text in ("A", "B", "A", "B")
        ]
        paths = [cursor_archive.publish_snapshot(value, self.root) for value in snapshots]
        self.assertEqual(
            cursor_archive.read_latest_snapshot(paths[-1]), snapshots[-1]
        )
        self.assertEqual(cursor_archive.discover_heads(self.root), [paths[-1]])
        self.assertEqual(cursor_archive.discover_revisions(self.root), paths)

    def test_out_of_order_arrival_selects_latest_reconstructible_chain(self):
        one = cursor_archive.publish_snapshot(_snapshot(texts=("one",)), self.root)
        two = cursor_archive.publish_snapshot(_snapshot(texts=("one", "two")), self.root)
        three = cursor_archive.publish_snapshot(
            _snapshot(texts=("one", "two", "three")), self.root
        )
        remote = Path(tempfile.mkdtemp())
        dest = cursor_archive.session_directory(remote, "S")
        dest.mkdir(parents=True)
        shutil.copy2(one, dest / one.name)
        shutil.copy2(three, dest / three.name)
        self.assertEqual(cursor_archive.discover_heads(remote), [dest / one.name])
        shutil.copy2(two, dest / two.name)
        self.assertEqual(cursor_archive.discover_heads(remote), [dest / three.name])

    def test_corrupt_revision_and_part_file_do_not_displace_prior_head(self):
        one = cursor_archive.publish_snapshot(_snapshot(), self.root)
        revision_dir = one.parent
        (revision_dir / "00000000000000000002-deadbeef.json").write_text("{}")
        (revision_dir / ".future.json.part").write_text("{")
        self.assertEqual(cursor_archive.discover_heads(self.root), [one])

    def test_revision_symlinks_are_not_archive_evidence(self):
        one = cursor_archive.publish_snapshot(_snapshot(), self.root)
        external = self.root / "outside.json"
        one.rename(external)
        one.symlink_to(external)
        self.assertEqual(cursor_archive.discover_heads(self.root), [])

    def test_archive_owned_directory_symlinks_are_rejected_without_side_effects(self):
        for component in ("root", "sessions", "session", "revisions"):
            with self.subTest(component=component):
                base = Path(tempfile.mkdtemp())
                root = base / "archive"
                outside = base / "outside"
                outside.mkdir(mode=0o755)
                revision_dir = cursor_archive.session_directory(root, "S")
                if component == "root":
                    root.symlink_to(outside, target_is_directory=True)
                else:
                    root.mkdir()
                    if component == "sessions":
                        revision_dir.parent.parent.symlink_to(
                            outside, target_is_directory=True
                        )
                    elif component == "session":
                        revision_dir.parent.parent.mkdir()
                        revision_dir.parent.symlink_to(
                            outside, target_is_directory=True
                        )
                    else:
                        revision_dir.parent.mkdir(parents=True)
                        revision_dir.symlink_to(outside, target_is_directory=True)

                with self.assertRaises(cursor_archive.CursorArchiveError):
                    cursor_archive.publish_snapshot(_snapshot(), root)
                self.assertEqual(outside.stat().st_mode & 0o777, 0o755)
                self.assertEqual(list(outside.iterdir()), [])
                self.assertEqual(cursor_archive.discover_heads(root), [])
                with self.assertRaises(cursor_archive.CursorArchiveError):
                    cursor_archive.latest_complete_revision(revision_dir)

    def test_lock_symlink_is_rejected_without_chmodding_target(self):
        self.root.mkdir(exist_ok=True)
        outside = self.root.parent / "outside-lock"
        outside.write_text("foreign")
        outside.chmod(0o644)
        (self.root / ".export.lock").symlink_to(outside)
        descriptor_count = len(os.listdir("/dev/fd")) \
            if Path("/dev/fd").is_dir() else None
        for _ in range(10):
            with self.assertRaises(cursor_archive.CursorArchiveError):
                cursor_archive.publish_snapshot(_snapshot(), self.root)
        self.assertEqual(outside.stat().st_mode & 0o777, 0o644)
        if descriptor_count is not None:
            self.assertEqual(len(os.listdir("/dev/fd")), descriptor_count)

    def test_malformed_archive_json_never_displaces_a_valid_head(self):
        one = cursor_archive.publish_snapshot(_snapshot(), self.root)
        revision_dir = one.parent
        (revision_dir / "deep.json").write_text("[" * 1500 + "0" + "]" * 1500)
        (revision_dir / "surrogate.json").write_text(
            '{"archiveVersion":1,"snapshot":{"bad":"\\ud800"}}'
        )
        self.assertEqual(cursor_archive.discover_heads(self.root), [one])

    def test_atomic_failure_publishes_no_revision(self):
        with mock.patch("codebrain.cursor_archive.os.link", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                cursor_archive.publish_snapshot(_snapshot(), self.root)
        self.assertEqual(cursor_archive.discover_heads(self.root), [])
        self.assertEqual(list(self.root.rglob("*.part")), [])

    def test_existing_revision_path_is_never_overwritten(self):
        cursor_archive.publish_snapshot(_snapshot(), self.root)
        changed = _snapshot(texts=("changed",))
        logical, _ = cursor_archive._deflate_snapshot(changed)
        revision_dir = cursor_archive.session_directory(self.root, "S")
        collision = revision_dir / (
            f"{2:020d}-{cursor_archive.digest(logical)}.json"
        )
        collision.write_text("foreign")

        with self.assertRaises(FileExistsError):
            cursor_archive.publish_snapshot(changed, self.root)
        self.assertEqual(collision.read_text(), "foreign")

    def test_strict_json_rejects_nonfinite_payload(self):
        snapshot = _snapshot()
        snapshot["order"][0]["payload"]["bad"] = float("nan")
        with self.assertRaises(cursor_archive.CursorArchiveError):
            cursor_archive.publish_snapshot(snapshot, self.root)

    def test_metadata_scan_invalidates_only_changed_session_without_json_reads(self):
        cursor_archive.publish_snapshot(_snapshot("S"), self.root)
        cursor_archive.publish_snapshot(_snapshot("T"), self.root)
        with mock.patch(
            "codebrain.cursor_archive._read_segment",
            side_effect=AssertionError("metadata scan parsed JSON"),
        ):
            before = cursor_archive.scan_archive_metadata(self.root)
        rows_before = {row.session_key: row for row in before.sessions}

        cursor_archive.publish_snapshot(
            _snapshot("S", texts=("one", "two")), self.root
        )
        after = cursor_archive.scan_archive_metadata(self.root)
        rows_after = {row.session_key: row for row in after.sessions}
        s_key = cursor_archive.session_directory(self.root, "S").parent.name
        t_key = cursor_archive.session_directory(self.root, "T").parent.name
        self.assertEqual(before.root_signature, after.root_signature)
        self.assertNotEqual(rows_before[s_key].signature, rows_after[s_key].signature)
        self.assertEqual(rows_before[t_key].signature, rows_after[t_key].signature)

        selected = cursor_archive.select_session_head(rows_after[s_key])
        self.assertEqual((selected.composer_id, selected.revision), ("S", 2))
        self.assertEqual(selected.snapshot["order"][1]["payload"]["text"], "two")
        self.assertEqual(
            selected.snapshot_digest,
            selected.path.name.split("-", 1)[1].removesuffix(".json"),
        )

    def test_session_signature_tracks_out_of_order_and_in_place_corrections(self):
        one = cursor_archive.publish_snapshot(_snapshot(texts=("one",)), self.root)
        two = cursor_archive.publish_snapshot(_snapshot(texts=("one", "two")), self.root)
        three = cursor_archive.publish_snapshot(
            _snapshot(texts=("one", "two", "three")), self.root
        )
        remote = Path(tempfile.mkdtemp())
        dest = cursor_archive.session_directory(remote, "S")
        dest.mkdir(parents=True)
        shutil.copy2(one, dest / one.name)
        shutil.copy2(three, dest / three.name)

        first_row = cursor_archive.scan_archive_metadata(remote).sessions[0]
        self.assertEqual(cursor_archive.select_session_head(first_row).revision, 1)
        shutil.copy2(two, dest / two.name)
        ordered_row = cursor_archive.scan_archive_metadata(remote).sessions[0]
        self.assertNotEqual(first_row.signature, ordered_row.signature)
        self.assertEqual(cursor_archive.select_session_head(ordered_row).revision, 3)

        (dest / three.name).write_bytes(b"{" + b" " * (three.stat().st_size - 1))
        corrected_row = cursor_archive.scan_archive_metadata(remote).sessions[0]
        self.assertNotEqual(ordered_row.signature, corrected_row.signature)
        self.assertEqual(cursor_archive.select_session_head(corrected_row).revision, 2)

        (dest / two.name).unlink()
        deleted_row = cursor_archive.scan_archive_metadata(remote).sessions[0]
        self.assertNotEqual(corrected_row.signature, deleted_row.signature)
        self.assertEqual(cursor_archive.select_session_head(deleted_row).revision, 1)

    def test_metadata_scan_tracks_symlink_type_without_following_it(self):
        revision = cursor_archive.publish_snapshot(_snapshot(), self.root)
        before = cursor_archive.scan_archive_metadata(self.root).sessions[0]
        outside = self.root / "outside"
        outside.write_text("not archive evidence")
        revision.unlink()
        revision.symlink_to(outside)
        after = cursor_archive.scan_archive_metadata(self.root).sessions[0]
        self.assertNotEqual(before.signature, after.signature)
        self.assertIsNone(cursor_archive.select_session_head(after))

    def test_session_head_rejects_row_for_a_different_session_key(self):
        cursor_archive.publish_snapshot(_snapshot("S"), self.root)
        row = cursor_archive.scan_archive_metadata(self.root).sessions[0]
        forged = cursor_archive.CursorSessionSignature(
            session_key="0" * 64,
            revision_dir=row.revision_dir,
            signature=row.signature,
        )
        with self.assertRaises(cursor_archive.CursorArchiveError):
            cursor_archive.select_session_head(forged)


class TestIncrementalExport(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.db_path = self.root / "state.vscdb"
        self.archive = self.root / "archive"
        self.writer = _state_db(self.db_path, wal=True)
        self.addCleanup(self.writer.close)
        _header(self.writer, "S")
        _put(self.writer, "composerData:S", _modern_composer("S", ("u1",)))
        _put(self.writer, "bubbleId:S:u1", {
            "_v": 3, "bubbleId": "u1", "type": 1, "text": "first",
            "createdAt": "2026-01-01T00:00:01Z",
        })
        self.writer.commit()

    def test_full_then_noop_then_changed_header_exports_new_revision(self):
        first = cursor_archive.export_cursor(self.db_path, self.archive, now=1000)
        second = cursor_archive.export_cursor(self.db_path, self.archive, now=1001)
        self.assertEqual((first["published"], second["candidates"]), (1, 0))

        _put(self.writer, "bubbleId:S:u1", {
            "_v": 3, "bubbleId": "u1", "type": 1, "text": "changed",
            "createdAt": "2026-01-01T00:00:01Z",
        })
        self.writer.execute(
            "UPDATE composerHeaders SET checkpointAt=checkpointAt+1 WHERE composerId='S'"
        )
        self.writer.commit()
        third = cursor_archive.export_cursor(self.db_path, self.archive, now=1002)
        self.assertEqual(third["published"], 1)
        head = cursor_archive.discover_heads(self.archive)[0]
        self.assertEqual(
            cursor_archive.read_latest_snapshot(head)["order"][0]["payload"]["text"],
            "changed",
        )

    def test_noop_skips_part_scan_and_unchanged_state_write(self):
        cursor_archive.export_cursor(self.db_path, self.archive, now=1000)
        with mock.patch(
                "codebrain.cursor_archive._prune_stale_parts",
                side_effect=AssertionError("part scan ran before interval")), \
             mock.patch(
                "codebrain.cursor_archive._write_exporter_state",
                side_effect=AssertionError("unchanged state was rewritten")):
            stats = cursor_archive.export_cursor(
                self.db_path, self.archive, now=1001,
            )
        self.assertEqual((stats["candidates"], stats["errors"]), (0, 0))

    def test_part_scan_runs_again_at_interval(self):
        cursor_archive.export_cursor(self.db_path, self.archive, now=1000)
        with mock.patch(
                "codebrain.cursor_archive._prune_stale_parts") as prune:
            cursor_archive.export_cursor(
                self.db_path, self.archive,
                now=1000 + cursor_archive.PART_PRUNE_SECONDS,
            )
        prune.assert_called_once_with(self.archive)

    def test_unsettled_update_retains_head_and_is_retried(self):
        cursor_archive.export_cursor(self.db_path, self.archive, now=1000)
        original = cursor_archive.discover_heads(self.archive)[0]
        value = _modern_composer("S", ("u1",), generatingBubbleIds=["u1"])
        _put(self.writer, "composerData:S", value)
        self.writer.execute(
            "UPDATE composerHeaders SET checkpointAt=checkpointAt+1 WHERE composerId='S'"
        )
        self.writer.commit()
        skipped = cursor_archive.export_cursor(self.db_path, self.archive, now=1001)
        early = cursor_archive.export_cursor(self.db_path, self.archive, now=1060)
        retried = cursor_archive.export_cursor(self.db_path, self.archive, now=1061)
        self.assertEqual(
            (skipped["skipped"], early["candidates"], retried["candidates"]),
            (1, 0, 1),
        )
        state = json.loads((self.archive / "exporter-state.json").read_text())
        self.assertEqual(state["retryRecords"]["S"], {
            "category": "active", "attempts": 2, "nextAttemptAt": 1181,
        })
        self.assertEqual(cursor_archive.discover_heads(self.archive), [original])

    def test_changed_valid_header_token_bypasses_retry_delay(self):
        cursor_archive.export_cursor(self.db_path, self.archive, now=1000)
        _put(self.writer, "composerData:S", _modern_composer(
            "S", ("u1",), generatingBubbleIds=["u1"],
        ))
        self.writer.execute(
            "UPDATE composerHeaders SET checkpointAt=checkpointAt+1 "
            "WHERE composerId='S'"
        )
        self.writer.commit()
        self.assertEqual(
            cursor_archive.export_cursor(
                self.db_path, self.archive, now=1001,
            )["skipped"],
            1,
        )

        _put(self.writer, "composerData:S", _modern_composer("S", ("u1",)))
        _put(self.writer, "bubbleId:S:u1", {
            "_v": 3, "bubbleId": "u1", "type": 1, "text": "settled",
            "createdAt": "2026-01-01T00:00:01Z",
        })
        self.writer.execute(
            "UPDATE composerHeaders SET checkpointAt=checkpointAt+1 "
            "WHERE composerId='S'"
        )
        self.writer.commit()
        bypassed = cursor_archive.export_cursor(
            self.db_path, self.archive, now=1002,
        )
        self.assertEqual((bypassed["candidates"], bypassed["published"]), (1, 1))

    def test_full_reconcile_bypasses_active_retry_delay(self):
        _put(self.writer, "composerData:S", _modern_composer(
            "S", ("u1",), generatingBubbleIds=["u1"],
        ))
        self.writer.commit()
        first = cursor_archive.export_cursor(self.db_path, self.archive, now=1000)
        forced = cursor_archive.export_cursor(
            self.db_path, self.archive, full_reconcile=True, now=1001,
        )
        self.assertEqual((first["skipped"], forced["candidates"]), (1, 1))

    def test_draft_retry_uses_daily_backoff(self):
        _put(self.writer, "composerData:S", _modern_composer("S", status="none"))
        self.writer.commit()
        first = cursor_archive.export_cursor(self.db_path, self.archive, now=1000)
        early = cursor_archive.export_cursor(
            self.db_path,
            self.archive,
            now=1000 + cursor_archive.LONG_RETRY_SECONDS - 1,
        )
        due = cursor_archive.export_cursor(
            self.db_path,
            self.archive,
            now=1000 + cursor_archive.LONG_RETRY_SECONDS,
        )
        state = json.loads((self.archive / "exporter-state.json").read_text())
        self.assertEqual((first["skipped"], early["candidates"], due["candidates"]),
                         (1, 0, 1))
        self.assertEqual(state["retryRecords"]["S"]["category"], "draft")

    def test_incomplete_retry_uses_short_structured_backoff(self):
        with mock.patch(
                "codebrain.cursor_export.project_session",
                side_effect=cursor_export.CursorSnapshotIncomplete("incomplete")):
            first = cursor_archive.export_cursor(
                self.db_path, self.archive, now=1000,
            )
            early = cursor_archive.export_cursor(
                self.db_path, self.archive, now=1059,
            )
            due = cursor_archive.export_cursor(
                self.db_path, self.archive, now=1060,
            )
        state = json.loads((self.archive / "exporter-state.json").read_text())
        record = state["retryRecords"]["S"]
        self.assertEqual((first["skipped"], early["candidates"], due["skipped"]),
                         (1, 0, 1))
        self.assertEqual(
            (record["category"], record["attempts"], record["nextAttemptAt"]),
            ("incomplete", 2, 1180),
        )

    def test_absent_retry_uses_daily_structured_backoff(self):
        with mock.patch(
                "codebrain.cursor_export.project_session", return_value=None):
            first = cursor_archive.export_cursor(
                self.db_path, self.archive, now=1000,
            )
            early = cursor_archive.export_cursor(
                self.db_path, self.archive,
                now=1000 + cursor_archive.LONG_RETRY_SECONDS - 1,
            )
        state = json.loads((self.archive / "exporter-state.json").read_text())
        record = state["retryRecords"]["S"]
        self.assertEqual((first["skipped"], early["candidates"]), (1, 0))
        self.assertEqual(
            (record["category"], record["attempts"], record["nextAttemptAt"]),
            ("absent", 1, 1000 + cursor_archive.LONG_RETRY_SECONDS),
        )

    def test_legacy_pending_ids_migrate_to_typed_retry_records(self):
        cursor_archive.export_cursor(self.db_path, self.archive, now=1000)
        _put(self.writer, "composerData:S", _modern_composer(
            "S", ("u1",), generatingBubbleIds=["u1"],
        ))
        self.writer.commit()
        state_path = self.archive / "exporter-state.json"
        state = json.loads(state_path.read_text())
        state["version"] = 1
        state["pendingComposerIds"] = ["S"]
        state.pop("retryRecords", None)
        state_path.write_text(json.dumps(state))

        stats = cursor_archive.export_cursor(self.db_path, self.archive, now=1001)
        migrated = json.loads(state_path.read_text())
        self.assertEqual((stats["candidates"], stats["skipped"]), (1, 1))
        self.assertEqual(migrated["version"], cursor_archive.EXPORTER_STATE_VERSION)
        self.assertEqual(migrated["retryRecords"]["S"]["attempts"], 1)
        self.assertNotIn("pendingComposerIds", migrated)

    def test_bad_or_far_future_retry_state_rebuilds_immediately(self):
        cursor_archive.export_cursor(self.db_path, self.archive, now=1000)
        state_path = self.archive / "exporter-state.json"
        base = json.loads(state_path.read_text())
        bad_records = (
            {"S": {"category": "active", "attempts": "1",
                   "nextAttemptAt": 1060}},
            {"S": {"category": "active", "attempts": 1,
                   "nextAttemptAt": 1001
                   + cursor_archive.ACTIVE_RETRY_MAX_SECONDS + 1}},
            {"S": {"category": "active", "attempts": 1,
                   "nextAttemptAt": 1001 + cursor_archive.LONG_RETRY_SECONDS + 1}},
        )
        for retry_records in bad_records:
            with self.subTest(retry_records=retry_records):
                state = dict(base)
                state["retryRecords"] = retry_records
                state_path.write_text(json.dumps(state))
                stats = cursor_archive.export_cursor(
                    self.db_path, self.archive, now=1001,
                )
                self.assertEqual((stats["candidates"], stats["unchanged"]), (1, 1))

    def test_state_and_lock_are_private_and_outside_revision_discovery(self):
        cursor_archive.export_cursor(self.db_path, self.archive, now=1000)
        for name in ("exporter-state.json", ".export.lock"):
            path = self.archive / name
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_data_only_session_is_found_on_full_reconcile(self):
        _put(self.writer, "composerData:data-only", {
            "_v": 1, "createdAt": 1, "conversation": [
                {"bubbleId": "old", "type": 1, "text": "historical"},
            ],
        })
        self.writer.commit()
        stats = cursor_archive.export_cursor(
            self.db_path, self.archive, full_reconcile=True, now=1000
        )
        self.assertEqual(stats["published"], 2)
        self.assertEqual(len(cursor_archive.discover_heads(self.archive)), 2)

    def test_unsettled_data_only_session_is_pending_until_it_settles(self):
        _put(self.writer, "composerData:data-only", {
            "_v": 3, "createdAt": 1, "fullConversationHeadersOnly": [],
            "generatingBubbleIds": ["future"],
        })
        self.writer.commit()
        first = cursor_archive.export_cursor(
            self.db_path, self.archive, full_reconcile=True, now=1000
        )
        second = cursor_archive.export_cursor(self.db_path, self.archive, now=1001)
        self.assertEqual((first["skipped"], second["candidates"]), (1, 0))

        _put(self.writer, "composerData:data-only", {
            "_v": 1, "createdAt": 1, "conversation": [
                {"bubbleId": "old", "type": 1, "text": "settled"},
            ],
        })
        self.writer.commit()
        third = cursor_archive.export_cursor(self.db_path, self.archive, now=1059)
        fourth = cursor_archive.export_cursor(self.db_path, self.archive, now=1060)
        fifth = cursor_archive.export_cursor(self.db_path, self.archive, now=1061)
        self.assertEqual(
            (third["candidates"], fourth["published"], fifth["candidates"]),
            (0, 1, 0),
        )

    def test_malformed_reconcile_timestamp_fails_open_to_full_scan(self):
        cursor_archive.export_cursor(self.db_path, self.archive, now=1000)
        state_path = self.archive / "exporter-state.json"
        state = json.loads(state_path.read_text())
        state["lastFullReconcileAt"] = "not-a-number"
        state_path.write_text(json.dumps(state))

        stats = cursor_archive.export_cursor(self.db_path, self.archive, now=1001)
        self.assertEqual((stats["candidates"], stats["unchanged"]), (1, 1))

    def test_future_reconcile_timestamp_fails_open_to_full_scan(self):
        cursor_archive.export_cursor(self.db_path, self.archive, now=1000)
        state_path = self.archive / "exporter-state.json"
        state = json.loads(state_path.read_text())
        state["lastFullReconcileAt"] = 2000
        state_path.write_text(json.dumps(state))
        stats = cursor_archive.export_cursor(self.db_path, self.archive, now=1001)
        self.assertEqual((stats["candidates"], stats["unchanged"]), (1, 1))

    def test_periodic_reconcile_discovers_headerless_session_at_threshold(self):
        cursor_archive.export_cursor(self.db_path, self.archive, now=1000)
        _put(self.writer, "composerData:data-only", {
            "_v": 1, "conversation": [
                {"bubbleId": "old", "type": 1, "text": "periodic"},
            ],
        })
        self.writer.commit()
        before = cursor_archive.export_cursor(
            self.db_path, self.archive,
            now=1000 + cursor_archive.FULL_RECONCILE_SECONDS - 1,
        )
        due = cursor_archive.export_cursor(
            self.db_path, self.archive,
            now=1000 + cursor_archive.FULL_RECONCILE_SECONDS,
        )
        self.assertEqual((before["candidates"], due["published"]), (0, 1))

    def test_invalid_header_token_does_not_block_valid_session(self):
        _header(self.writer, "bad", checkpointAt=b"not-json")
        _put(self.writer, "composerData:bad", {
            "_v": 1, "conversation": [
                {"bubbleId": "bad", "type": 1, "text": "bad"},
            ],
        })
        self.writer.commit()
        stats = cursor_archive.export_cursor(
            self.db_path, self.archive, full_reconcile=True, now=1000
        )
        self.assertEqual((stats["published"], stats["errors"]), (1, 1))
        self.assertEqual(len(cursor_archive.discover_heads(self.archive)), 1)

    def test_invalid_header_backs_off_and_exact_restoration_bypasses(self):
        cursor_archive.export_cursor(self.db_path, self.archive, now=1000)
        valid_checkpoint = self.writer.execute(
            "SELECT checkpointAt FROM composerHeaders WHERE composerId='S'"
        ).fetchone()[0]
        self.writer.execute(
            "UPDATE composerHeaders SET checkpointAt=? WHERE composerId='S'",
            (b"not-canonical-json",),
        )
        self.writer.commit()
        invalid = cursor_archive.export_cursor(
            self.db_path, self.archive, now=1001,
        )
        backed_off = cursor_archive.export_cursor(
            self.db_path, self.archive, now=1002,
        )
        self.writer.execute(
            "UPDATE composerHeaders SET checkpointAt=? WHERE composerId='S'",
            (valid_checkpoint,),
        )
        self.writer.commit()
        restored = cursor_archive.export_cursor(
            self.db_path, self.archive, now=1003,
        )
        self.assertEqual(
            (
                invalid["errors"], invalid["candidates"],
                backed_off["candidates"], restored["candidates"],
                restored["unchanged"],
            ),
            (1, 1, 0, 1, 1),
        )

    def test_pathological_source_json_is_isolated_per_session(self):
        deep = '{"_v":1,"conversation":' + "[" * 1500 + "0" \
            + "]" * 1500 + "}"
        self.writer.execute(
            "INSERT INTO cursorDiskKV(key,value) VALUES (?,?)",
            ("composerData:deep", deep),
        )
        self.writer.commit()
        stats = cursor_archive.export_cursor(
            self.db_path, self.archive, full_reconcile=True, now=1000
        )
        self.assertEqual((stats["published"], stats["errors"]), (1, 1))

    def test_invalid_exporter_state_is_rebuilt(self):
        cursor_archive.export_cursor(self.db_path, self.archive, now=1000)
        state_path = self.archive / "exporter-state.json"
        for invalid in (
            '{"version":1,"bad":"\\ud800"}',
            '{"version":1,"bad":' + "[" * 1500 + "0" + "]" * 1500 + "}",
        ):
            with self.subTest(kind=invalid[:30]):
                state_path.write_text(invalid)
                stats = cursor_archive.export_cursor(
                    self.db_path, self.archive, now=1001
                )
                self.assertEqual((stats["candidates"], stats["unchanged"]), (1, 1))

    def test_state_write_failure_leaves_published_revision_recoverable(self):
        with mock.patch(
            "codebrain.cursor_archive._write_exporter_state",
            side_effect=OSError("state failed"),
        ):
            stats = cursor_archive.export_cursor(
                self.db_path, self.archive, now=1000
            )
        self.assertEqual((stats["published"], stats["errors"]), (1, 1))
        self.assertEqual(len(cursor_archive.discover_heads(self.archive)), 1)
        retry = cursor_archive.export_cursor(self.db_path, self.archive, now=1001)
        self.assertEqual(retry["unchanged"], 1)

    def test_cleanup_removes_only_exporter_owned_part_names(self):
        self.archive.mkdir(parents=True)
        owned = self.archive / ".exporter-state.json.123.part"
        unrelated = self.archive / "foreign.part"
        owned.write_text("old")
        unrelated.write_text("keep")
        old = cursor_archive.time.time() - 7200
        os.utime(owned, (old, old))
        os.utime(unrelated, (old, old))
        cursor_archive.export_cursor(self.db_path, self.archive, now=1000)
        self.assertFalse(owned.exists())
        self.assertTrue(unrelated.exists())

    def test_full_reconcile_forgets_deleted_header_token(self):
        cursor_archive.export_cursor(self.db_path, self.archive, now=1000)
        self.writer.execute("DELETE FROM composerHeaders WHERE composerId='S'")
        self.writer.execute("DELETE FROM cursorDiskKV WHERE key='composerData:S'")
        self.writer.commit()
        cursor_archive.export_cursor(
            self.db_path, self.archive, full_reconcile=True, now=1001
        )

        _header(self.writer, "S")
        _put(self.writer, "composerData:S", _modern_composer("S", ("u1",)))
        self.writer.commit()
        stats = cursor_archive.export_cursor(self.db_path, self.archive, now=1002)
        self.assertEqual((stats["candidates"], stats["unchanged"]), (1, 1))

    def test_missing_source_database_is_isolated(self):
        stats = cursor_archive.export_cursor(
            self.root / "missing.vscdb", self.archive, now=1000
        )
        self.assertEqual(stats["errors"], 1)

    def test_nonfinite_export_time_is_rejected(self):
        with self.assertRaises(cursor_archive.CursorArchiveError):
            cursor_archive.export_cursor(self.db_path, self.archive, now=float("nan"))


if __name__ == "__main__":
    unittest.main()

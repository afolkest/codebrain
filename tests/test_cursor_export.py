"""Cursor's live SQLite state -> safe logical transcript projection."""
from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from codebrain import cursor_export


def _state_db(path: Path, wal: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.executescript("""
        CREATE TABLE composerHeaders (
          composerId TEXT PRIMARY KEY, workspaceId TEXT, createdAt INTEGER,
          lastUpdatedAt INTEGER, isArchived INTEGER, isSubagent INTEGER,
          recency INTEGER, checkpointAt INTEGER, value TEXT
        );
        CREATE TABLE cursorDiskKV (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);
        CREATE TABLE ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB);
    """)
    conn.commit()
    return conn


def _put(conn, key: str, value) -> None:
    encoded = None if value is None else json.dumps(value)
    conn.execute("INSERT OR REPLACE INTO cursorDiskKV(key,value) VALUES (?,?)",
                 (key, encoded))


def _header(conn, sid: str, **overrides) -> None:
    value = {
        "composerId": sid,
        "name": "Safe title",
        "workspaceIdentifier": {
            "id": "workspace", "uri": {
                "fsPath": "/work", "scheme": "file",
                "external": "https://user:SECRET_URI_PASSWORD@example.test/work",
            },
        },
        "trackedGitRepos": [{
            "repoPath": "/work", "remoteUrl": "https://u:SECRET_REMOTE@example.test/r",
        }],
        "agentLocation": {"worktreePath": "/work/tree", "secret": "NOPE"},
    }
    row = {
        "composerId": sid, "workspaceId": "workspace", "createdAt": 1000,
        "lastUpdatedAt": 2000, "isArchived": 0, "isSubagent": 0,
        "recency": 2000, "checkpointAt": 2000, "value": json.dumps(value),
    }
    row.update(overrides)
    conn.execute("""
        INSERT INTO composerHeaders VALUES
          (:composerId,:workspaceId,:createdAt,:lastUpdatedAt,:isArchived,
           :isSubagent,:recency,:checkpointAt,:value)
    """, row)


def _modern_composer(sid: str, bubble_ids=("u1", "a1"), **overrides):
    value = {
        "_v": 17, "composerId": sid, "createdAt": 1000,
        "lastUpdatedAt": 2000, "status": "completed",
        "fullConversationHeadersOnly": [
            {"bubbleId": bid, "type": 1 if i == 0 else 2}
            for i, bid in enumerate(bubble_ids)
        ],
        "blobEncryptionKey": "SECRET_BLOB_KEY",
        "speculativeSummarizationEncryptionKey": "SECRET_SUMMARY_KEY",
        "conversationState": "SECRET_CONVERSATION_STATE",
        "subagentComposerIds": [],
    }
    value.update(overrides)
    return value


class CursorProjectionBase(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.path = self.root / "state.vscdb"
        self.writer = _state_db(self.path)
        self.addCleanup(self.writer.close)

    def put_modern(self, sid="S", composer=None):
        _header(self.writer, sid)
        _put(self.writer, f"composerData:{sid}", composer or _modern_composer(sid))
        _put(self.writer, f"bubbleId:{sid}:u1", {
            "_v": 3, "bubbleId": "u1", "type": 1, "text": "hello",
            "createdAt": "2026-01-01T00:00:01.000Z", "requestId": "r1",
            "isSimulatedMsg": True, "simulatedMsgReason": 3,
            "simulatedMessageMetadata": {"taskId": "t", "title": "done",
                                         "unknownSecret": "NOPE"},
            "conversationState": "SECRET_BUBBLE_STATE",
            "thinking": {"text": "SECRET_THINKING"},
            "unknownFutureSecret": "SECRET_FUTURE",
            "context": {"selectedImages": [{
                "uuid": "attachment", "path": "/tmp/image.png",
                "dimension": {"width": 2, "height": 3, "secret": "NOPE"},
                "unknown": "NOPE",
            }], "codebaseContextChunks": ["SECRET_CODEBASE"]},
        })
        _put(self.writer, f"bubbleId:{sid}:a1", {
            "_v": 3, "bubbleId": "a1", "type": 2, "text": "",
            "createdAt": "2026-01-01T00:00:02.000Z",
            "toolFormerData": {
                "name": "run_terminal_command_v2", "toolCallId": "call-1",
                "modelCallId": "model-1", "tool": 15, "toolIndex": 0,
                "status": "completed", "params": {"command": "pwd"},
                "result": {"output": "/work", "exitCode": 0},
                "toolCallBinary": "SECRET_BINARY",
                "additionalData": {"isPruned": False, "subagentComposerId": "C",
                                   "instructions": "SECRET_INSTRUCTIONS",
                                   "topFiles": [{"secret": "SECRET_TOP_FILES"}]},
            },
            "allThinkingBlocks": ["SECRET_MORE_THINKING"],
        })
        self.writer.commit()


class TestCursorProjection(CursorProjectionBase):
    def test_modern_projection_is_ordered_and_allowlisted(self):
        self.put_modern()
        snapshot = cursor_export.read_session_snapshot(self.path, "S")

        self.assertEqual([r["bubbleId"] for r in snapshot["order"]], ["u1", "a1"])
        self.assertEqual(snapshot["session"]["workspaceIdentifier"]["uri"]["fsPath"],
                         "/work")
        user = snapshot["order"][0]["payload"]
        self.assertTrue(user["isSimulatedMsg"])
        self.assertEqual(user["attachments"][0]["dimension"], {"width": 2, "height": 3})
        tool = snapshot["order"][1]["payload"]["toolFormerData"]
        self.assertEqual(tool["params"]["command"], "pwd")
        self.assertEqual(tool["additionalData"]["subagentComposerId"], "C")

        encoded = json.dumps(snapshot, sort_keys=True)
        for secret in (
            "SECRET_BLOB_KEY", "SECRET_SUMMARY_KEY", "SECRET_CONVERSATION_STATE",
            "SECRET_BUBBLE_STATE", "SECRET_THINKING", "SECRET_MORE_THINKING",
            "SECRET_BINARY", "SECRET_FUTURE", "SECRET_CODEBASE",
            "SECRET_INSTRUCTIONS", "SECRET_TOP_FILES", "SECRET_URI_PASSWORD",
            "SECRET_REMOTE", "NOPE",
        ):
            self.assertNotIn(secret, encoded)

    def test_allowlist_rejects_wrong_typed_nested_metadata(self):
        self.put_modern()
        header = self.writer.execute(
            "SELECT value FROM composerHeaders WHERE composerId='S'"
        ).fetchone()[0]
        value = json.loads(header)
        value["agentLocation"]["environment"] = {
            "id": "local", "uri": {"fsPath": "/safe",
                                    "external": "SECRET_NESTED"},
        }
        value["name"] = {"futureSecret": "SECRET_TOP_LEVEL"}
        self.writer.execute(
            "UPDATE composerHeaders SET value=? WHERE composerId='S'",
            (json.dumps(value),),
        )
        self.writer.commit()
        snapshot = cursor_export.read_session_snapshot(self.path, "S")
        self.assertEqual(snapshot["session"]["agentLocation"]["environment"]
                         ["uri"]["fsPath"], "/safe")
        self.assertNotIn("SECRET_NESTED", json.dumps(snapshot))
        self.assertNotIn("SECRET_TOP_LEVEL", json.dumps(snapshot))

    def test_embedded_v1_uses_array_order_and_timing_anchor(self):
        _put(self.writer, "composerData:old", {
            "_v": 1, "composerId": "old", "createdAt": 1000,
            "conversation": [
                {"_v": 1, "bubbleId": "u", "type": 1, "text": "old user"},
                {"_v": 1, "bubbleId": "a", "type": 2, "text": "old answer",
                 "timingInfo": {"clientRpcSendTime": 1750000000000,
                                "clientStartTime": 12345}},
            ],
        })
        self.writer.commit()

        snapshot = cursor_export.read_session_snapshot(self.path, "old")
        self.assertEqual(snapshot["sourceCapability"], "embedded")
        self.assertEqual([r["bubbleId"] for r in snapshot["order"]], ["u", "a"])
        self.assertIsNone(snapshot["order"][0]["createdAt"])
        self.assertEqual(snapshot["order"][1]["createdAt"], 1750000000000)

    def test_renderer_relative_timing_is_not_an_event_time(self):
        _put(self.writer, "composerData:old", {
            "_v": 1, "composerId": "old", "createdAt": 1000,
            "conversation": [{
                "bubbleId": "a", "type": 2, "text": "old answer",
                "timingInfo": {"clientRpcSendTime": 12345,
                               "clientStartTime": 1750000000000},
            }],
        })
        self.writer.commit()
        snapshot = cursor_export.read_session_snapshot(self.path, "old")
        self.assertIsNone(snapshot["order"][0]["createdAt"])

    def test_legacy_thought_and_summary_text_are_excluded_by_structured_flag(self):
        _put(self.writer, "composerData:old", {
            "_v": 1, "composerId": "old", "createdAt": 1000,
            "conversation": [
                {"bubbleId": "thought", "type": 2, "text": "SECRET_THOUGHT",
                 "isThought": True, "usageUuid": "usage"},
                {"bubbleId": "summary", "type": 2, "text": "SECRET_SUMMARY",
                 "isSummarization": True, "serverBubbleId": "server"},
                {"bubbleId": "visible", "type": 2, "text": "visible"},
            ],
        })
        self.writer.commit()
        snapshot = cursor_export.read_session_snapshot(self.path, "old")
        thought, summary, visible = [r["payload"] for r in snapshot["order"]]
        self.assertEqual(thought, {
            "type": 2, "usageUuid": "usage", "isThought": True,
            "bubbleId": "thought",
        })
        self.assertNotIn("text", summary)
        self.assertEqual(summary["serverBubbleId"], "server")
        self.assertEqual(visible["text"], "visible")
        self.assertNotIn("SECRET_THOUGHT", json.dumps(snapshot))
        self.assertNotIn("SECRET_SUMMARY", json.dumps(snapshot))

    def test_data_only_composer_is_discovered(self):
        _put(self.writer, "composerData:data-only", {
            "_v": 1, "createdAt": 1,
            "conversation": [{"bubbleId": "u", "type": 1, "text": "x"}],
        })
        self.writer.commit()
        conn = cursor_export.connect_cursor(self.path)
        self.addCleanup(conn.close)
        self.assertIn("data-only", cursor_export.composer_ids(conn))

    def test_missing_target_bubble_uses_unique_timestamped_copy(self):
        _header(self.writer, "child")
        composer = _modern_composer("child", ("copied",))
        composer["fullConversationHeadersOnly"][0]["createdAt"] = \
            "2026-01-01T00:00:01.000Z"
        _put(self.writer, "composerData:child", composer)
        _put(self.writer, "bubbleId:parent:copied", {
            "_v": 3, "bubbleId": "copied", "type": 1, "text": "copy",
            "createdAt": "2026-01-01T00:00:01.000Z",
        })
        self.writer.commit()

        snapshot = cursor_export.read_session_snapshot(self.path, "child")
        self.assertEqual(snapshot["order"][0]["payload"]["text"], "copy")

    def test_ambiguous_copy_fails_closed(self):
        _header(self.writer, "child")
        composer = _modern_composer("child", ("copied",))
        composer["fullConversationHeadersOnly"][0]["createdAt"] = "2026-01-01T00:00:01Z"
        _put(self.writer, "composerData:child", composer)
        for owner in ("p1", "p2"):
            _put(self.writer, f"bubbleId:{owner}:copied", {
                "bubbleId": "copied", "type": 1, "text": owner,
                "createdAt": "2026-01-01T00:00:01Z",
            })
        self.writer.commit()
        with self.assertRaises(cursor_export.CursorSnapshotIncomplete):
            cursor_export.read_session_snapshot(self.path, "child")

    def test_copy_lookup_escapes_like_metacharacters(self):
        weird_id = "copy%_id"
        _header(self.writer, "child")
        composer = _modern_composer("child", (weird_id,))
        composer["fullConversationHeadersOnly"][0]["createdAt"] = "2026-01-01T00:00:01Z"
        _put(self.writer, "composerData:child", composer)
        _put(self.writer, "bubbleId:parent:copyXXid", {
            "bubbleId": "copyXXid", "type": 1, "text": "wrong",
            "createdAt": "2026-01-01T00:00:01Z",
        })
        self.writer.commit()
        with self.assertRaises(cursor_export.CursorSnapshotIncomplete):
            cursor_export.read_session_snapshot(self.path, "child")

    def test_malformed_source_json_is_not_treated_as_absent(self):
        self.writer.execute(
            "INSERT INTO cursorDiskKV(key,value) VALUES ('composerData:bad','{')"
        )
        self.writer.commit()
        with self.assertRaises(cursor_export.CursorSnapshotError):
            cursor_export.read_session_snapshot(self.path, "bad")

    def test_deep_and_non_utf8_json_strings_are_typed_source_errors(self):
        values = (
            '{"_v":1,"conversation":' + "[" * 1500 + "0" + "]" * 1500 + "}",
            '{"_v":1,"conversation":[],"name":"\\ud800"}',
        )
        for index, value in enumerate(values):
            sid = f"bad-{index}"
            with self.subTest(sid=sid):
                self.writer.execute(
                    "INSERT INTO cursorDiskKV(key,value) VALUES (?,?)",
                    (f"composerData:{sid}", value),
                )
                self.writer.commit()
                with self.assertRaises(cursor_export.CursorSnapshotError):
                    cursor_export.read_session_snapshot(self.path, sid)

    def test_null_header_and_exact_bubble_fail_closed(self):
        _header(self.writer, "S", value=None)
        _put(self.writer, "composerData:S", _modern_composer("S", ("u1",)))
        _put(self.writer, "bubbleId:S:u1", {
            "bubbleId": "u1", "type": 1, "text": "x",
            "createdAt": "2026-01-01T00:00:00Z",
        })
        self.writer.commit()
        with self.assertRaises(cursor_export.CursorSnapshotError):
            cursor_export.read_session_snapshot(self.path, "S")

        self.writer.execute("DELETE FROM composerHeaders WHERE composerId='S'")
        _header(self.writer, "S")
        self.writer.execute(
            "UPDATE cursorDiskKV SET value=NULL WHERE key='bubbleId:S:u1'"
        )
        self.writer.commit()
        with self.assertRaises(cursor_export.CursorSnapshotError):
            cursor_export.read_session_snapshot(self.path, "S")

        self.writer.execute("DELETE FROM cursorDiskKV WHERE key='composerData:bad'")
        _header(self.writer, "bad")
        self.writer.execute(
            "UPDATE composerHeaders SET value='{' WHERE composerId='bad'"
        )
        _put(self.writer, "composerData:bad", _modern_composer("bad", ("b",)))
        _put(self.writer, "bubbleId:bad:b", {
            "bubbleId": "b", "type": 1, "text": "x",
            "createdAt": "2026-01-01T00:00:00Z",
        })
        self.writer.commit()
        with self.assertRaises(cursor_export.CursorSnapshotError):
            cursor_export.read_session_snapshot(self.path, "bad")

        self.writer.execute("DELETE FROM cursorDiskKV WHERE key='composerData:bad'")
        self.writer.execute("DELETE FROM composerHeaders WHERE composerId='bad'")
        _header(self.writer, "bad")
        _put(self.writer, "composerData:bad", _modern_composer("bad", ("b",)))
        self.writer.execute(
            "INSERT INTO cursorDiskKV(key,value) VALUES ('bubbleId:bad:b','{')"
        )
        self.writer.commit()
        with self.assertRaises(cursor_export.CursorSnapshotError):
            cursor_export.read_session_snapshot(self.path, "bad")

    def test_duplicate_placement_and_active_session_fail_closed(self):
        self.put_modern(composer=_modern_composer("S", ("u1", "u1")))
        with self.assertRaises(cursor_export.CursorSnapshotIncomplete):
            cursor_export.read_session_snapshot(self.path, "S")

        self.writer.execute("DELETE FROM cursorDiskKV WHERE key='composerData:S'")
        _put(self.writer, "composerData:S", _modern_composer(
            "S", generatingBubbleIds=["a1"]
        ))
        self.writer.commit()
        with self.assertRaises(cursor_export.CursorSessionUnsettled):
            cursor_export.read_session_snapshot(self.path, "S")

    def test_all_structured_unsettled_signals_fail_closed(self):
        _header(self.writer, "S")
        cases = (
            {"status": "none"},
            {"status": "SECRET_STATUS"},
            {"queueItems": [{"text": "queued"}]},
            {"isContinuationInProgress": True},
            {"status": None},
        )
        for overrides in cases:
            with self.subTest(overrides=sorted(overrides)):
                value = _modern_composer("S")
                value.update(overrides)
                _put(self.writer, "composerData:S", value)
                self.writer.commit()
                with self.assertRaises(cursor_export.CursorSessionUnsettled) as cm:
                    cursor_export.read_session_snapshot(self.path, "S")
                self.assertNotIn("SECRET_STATUS", str(cm.exception))

    def test_dual_capability_and_embedded_duplicate_fail_closed(self):
        value = {
            "_v": 1, "createdAt": 1, "status": "completed",
            "conversation": [
                {"bubbleId": "same", "type": 1, "text": "a"},
                {"bubbleId": "same", "type": 2, "text": "b"},
            ],
        }
        _put(self.writer, "composerData:S", value)
        self.writer.commit()
        with self.assertRaises(cursor_export.CursorSnapshotIncomplete):
            cursor_export.read_session_snapshot(self.path, "S")

        value["fullConversationHeadersOnly"] = []
        _put(self.writer, "composerData:S", value)
        self.writer.commit()
        with self.assertRaises(cursor_export.CursorSnapshotError):
            cursor_export.read_session_snapshot(self.path, "S")

    def test_settled_empty_transcript_is_a_complete_snapshot(self):
        _put(self.writer, "composerData:empty", {
            "_v": 1, "createdAt": 1, "status": "completed",
            "conversation": [],
        })
        self.writer.commit()
        snapshot = cursor_export.read_session_snapshot(self.path, "empty")
        self.assertEqual(snapshot["order"], [])

    def test_header_and_settle_timestamp_fallbacks(self):
        _header(self.writer, "S")
        value = _modern_composer("S", ("u1", "a1"))
        value["fullConversationHeadersOnly"][0]["createdAt"] = \
            "2026-01-01T00:00:01Z"
        _put(self.writer, "composerData:S", value)
        _put(self.writer, "bubbleId:S:u1", {
            "bubbleId": "u1", "type": 1, "text": "u",
        })
        _put(self.writer, "bubbleId:S:a1", {
            "bubbleId": "a1", "type": 2, "text": "a",
            "timingInfo": {"clientSettleTime": 1750000000000},
        })
        self.writer.commit()
        snapshot = cursor_export.read_session_snapshot(self.path, "S")
        self.assertEqual(snapshot["order"][0]["createdAt"], "2026-01-01T00:00:01Z")
        self.assertEqual(snapshot["order"][1]["createdAt"], 1750000000000)
        self.assertEqual(snapshot["session"]["checkpointAt"], 2000)

    def test_non_finite_header_timestamp_is_not_exported(self):
        _header(self.writer, "old", recency=float("inf"), checkpointAt=float("-inf"))
        _put(self.writer, "composerData:old", {
            "_v": 1, "conversation": [
                {"bubbleId": "u", "type": 1, "text": "x"},
            ],
        })
        self.writer.commit()
        snapshot = cursor_export.read_session_snapshot(self.path, "old")
        self.assertNotIn("recency", snapshot["session"])
        self.assertNotIn("checkpointAt", snapshot["session"])
        json.dumps(snapshot, allow_nan=False)


class TestSchemaValidation(unittest.TestCase):
    def test_missing_required_column_is_typed_schema_error(self):
        root = Path(tempfile.mkdtemp())
        path = root / "bad.vscdb"
        conn = sqlite3.connect(path)
        conn.executescript("""
            CREATE TABLE composerHeaders (composerId TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE cursorDiskKV (key TEXT UNIQUE, value BLOB);
        """)
        conn.close()
        with self.assertRaises(cursor_export.CursorSnapshotError):
            cursor_export.connect_cursor(path)


class TestReadOnlyWAL(unittest.TestCase):
    def test_read_only_connection_sees_committed_wal_and_rejects_writes(self):
        root = Path(tempfile.mkdtemp())
        path = root / "state.vscdb"
        writer = _state_db(path, wal=True)
        self.addCleanup(writer.close)
        _header(writer, "S")
        _put(writer, "composerData:S", {
            "_v": 1, "createdAt": 1,
            "conversation": [{"bubbleId": "u", "type": 1, "text": "wal"}],
        })
        writer.commit()

        reader = cursor_export.connect_cursor(path)
        self.addCleanup(reader.close)
        with cursor_export.read_transaction(reader):
            snapshot = cursor_export.project_session(reader, "S")
            self.assertEqual(snapshot["order"][0]["payload"]["text"], "wal")
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute("DELETE FROM composerHeaders")

    def test_read_transaction_remains_pinned_across_writer_commit(self):
        root = Path(tempfile.mkdtemp())
        path = root / "state.vscdb"
        writer = _state_db(path, wal=True)
        self.addCleanup(writer.close)
        _header(writer, "S")
        _put(writer, "composerData:S", {
            "_v": 1, "createdAt": 1,
            "conversation": [{"bubbleId": "u", "type": 1, "text": "before"}],
        })
        writer.commit()
        reader = cursor_export.connect_cursor(path)
        self.addCleanup(reader.close)

        with cursor_export.read_transaction(reader):
            writer.execute(
                "UPDATE cursorDiskKV SET value=? WHERE key='composerData:S'",
                (json.dumps({
                    "_v": 1, "createdAt": 1,
                    "conversation": [{"bubbleId": "u", "type": 1,
                                      "text": "after"}],
                }),),
            )
            writer.commit()
            pinned = cursor_export.project_session(reader, "S")
            self.assertEqual(pinned["order"][0]["payload"]["text"], "before")
        fresh = cursor_export.read_session_snapshot(path, "S")
        self.assertEqual(fresh["order"][0]["payload"]["text"], "after")


if __name__ == "__main__":
    unittest.main()

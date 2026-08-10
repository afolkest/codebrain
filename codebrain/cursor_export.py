"""Safe, read-only projection of Cursor IDE chat state.

Cursor stores local chat history in a large live SQLite database.  That database
also contains encryption keys, opaque state, editor context, and unrelated
configuration, so it is never a collection root.  This module is the firewall:
it reads one consistent SQLite snapshot and returns a narrow, deterministic
transcript projection suitable for codebrain's own raw archive.

Revision publication lives below this projection layer (added separately); the
functions here do not write to either Cursor or codebrain storage.
"""
from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Iterator, Optional


DEFAULT_CURSOR_DB = (
    Path.home() / "Library" / "Application Support" / "Cursor" / "User"
    / "globalStorage" / "state.vscdb"
)
DEFAULT_CURSOR_ROOT = Path.home() / ".codebrain" / "cursor-raw"


class CursorSnapshotError(RuntimeError):
    """A source schema/session cannot be projected safely."""


class CursorSnapshotIncomplete(CursorSnapshotError):
    """The ordered transcript references missing or ambiguous source data."""


class CursorRetryCategory(str, Enum):
    """Structured reason an unsettled session should be revisited."""

    ACTIVE = "active"
    DRAFT = "draft"


class CursorSessionUnsettled(CursorSnapshotError):
    """Cursor is still mutating this session; retain the last settled export."""

    def __init__(self, message: str, retry_category: CursorRetryCategory):
        super().__init__(message)
        self.retry_category = retry_category


def connect_cursor(path: Path = DEFAULT_CURSOR_DB) -> sqlite3.Connection:
    """Open Cursor's live SQLite database without creating or modifying it.

    ``immutable=1`` is deliberately absent: committed chat state may exist only
    in the WAL, and immutable readers ignore normal live-database coordination.
    """
    path = Path(path).resolve()
    uri = path.as_uri() + "?mode=ro&cache=private"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        _require_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Pin one coherent view of headers, composer data, and bubble KVs."""
    conn.execute("BEGIN")
    try:
        # A read pins the WAL snapshot now instead of at an arbitrary later query.
        conn.execute("SELECT COUNT(*) FROM composerHeaders").fetchone()
        yield
    finally:
        conn.execute("ROLLBACK")


def composer_ids(conn: sqlite3.Connection, include_data_only: bool = True) -> list[str]:
    """Return structured session ids, including historical data-only composers."""
    ids = {r[0] for r in conn.execute("SELECT composerId FROM composerHeaders")
           if isinstance(r[0], str) and r[0]}
    if include_data_only:
        for row in conn.execute(
            "SELECT key FROM cursorDiskKV WHERE key LIKE 'composerData:%'"
        ):
            key = row[0]
            if isinstance(key, str) and len(key) > len("composerData:"):
                ids.add(key[len("composerData:"):])
    return sorted(ids)


def read_session_snapshot(path: Path, composer_id: str) -> Optional[dict]:
    """Read one session in a short, internally consistent transaction."""
    conn = connect_cursor(path)
    try:
        with read_transaction(conn):
            return project_session(conn, composer_id)
    finally:
        conn.close()


def project_session(conn: sqlite3.Connection, composer_id: str) -> Optional[dict]:
    """Project one composer using structured capabilities, not version guesses.

    Returns ``None`` for absent/empty drafts.  An unsettled or internally
    inconsistent session raises a typed exception so an exporter can retain the
    last known-good revision rather than publishing a destructive partial view.
    """
    if not isinstance(composer_id, str) or not composer_id:
        raise CursorSnapshotError("composer id must be a non-empty string")

    header_row = conn.execute(
        "SELECT * FROM composerHeaders WHERE composerId=?", (composer_id,)
    ).fetchone()
    header_value = _json_object(
        header_row["value"], f"composerHeaders:{composer_id}"
    ) if header_row else {}
    composer = _kv_object(conn, "composerData:" + composer_id, null_is_missing=False)
    if composer is None:
        return None

    capability = _capability(composer)
    _require_settled(composer)
    session = _project_session_metadata(composer_id, composer, header_row, header_value)
    spawn = _resolve_subagent_spawn(conn, composer_id, composer.get("subagentInfo"))
    if spawn and isinstance(session.get("subagentInfo"), dict):
        session["subagentInfo"].update(spawn)

    if capability == "embedded":
        source_order = composer["conversation"]
        ordered = []
        seen = set()
        for index, bubble in enumerate(source_order):
            if not isinstance(bubble, dict):
                raise CursorSnapshotIncomplete(
                    f"{composer_id}: embedded bubble {index} is not an object"
                )
            bubble_id = _bubble_id(bubble, index)
            if bubble_id in seen:
                raise CursorSnapshotIncomplete(
                    f"{composer_id}: duplicate embedded bubble id"
                )
            seen.add(bubble_id)
            bubble_type = _bubble_type(bubble, composer_id, bubble_id)
            ordered.append({
                "bubbleId": bubble_id,
                "type": bubble_type,
                "createdAt": _source_created_at(bubble, None),
                "payload": _project_bubble(bubble, bubble_id),
            })
    else:
        ordered = _project_separate_order(conn, composer_id, composer)

    result = {
        "projectionVersion": 1,
        "composerId": composer_id,
        "sourceCapability": capability,
        "session": session,
        "order": ordered,
    }
    if isinstance(composer.get("_v"), int) and not isinstance(composer.get("_v"), bool):
        result["sourceVersion"] = composer["_v"]
    return result


def _require_schema(conn: sqlite3.Connection) -> None:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    missing = {"composerHeaders", "cursorDiskKV"} - tables
    if missing:
        raise CursorSnapshotError(
            "unsupported Cursor database: missing " + ", ".join(sorted(missing))
        )
    columns = {r[1] for r in conn.execute("PRAGMA table_info(composerHeaders)")}
    required = {
        "composerId", "createdAt", "lastUpdatedAt", "isArchived",
        "isSubagent", "recency", "checkpointAt", "value",
    }
    missing_columns = required - columns
    if missing_columns:
        raise CursorSnapshotError(
            "unsupported Cursor composerHeaders columns: missing "
            + ", ".join(sorted(missing_columns))
        )
    kv_columns = {r[1] for r in conn.execute("PRAGMA table_info(cursorDiskKV)")}
    if not {"key", "value"}.issubset(kv_columns):
        raise CursorSnapshotError("unsupported Cursor cursorDiskKV columns")


def _capability(composer: dict) -> str:
    if "_v" in composer and composer["_v"] is not None \
            and (not isinstance(composer["_v"], int)
                 or isinstance(composer["_v"], bool)):
        raise CursorSnapshotError("composer has an invalid source version")
    embedded = isinstance(composer.get("conversation"), list)
    separate = isinstance(composer.get("fullConversationHeadersOnly"), list)
    if embedded and separate:
        raise CursorSnapshotError("composer has ambiguous conversation capabilities")
    if embedded:
        return "embedded"
    if separate:
        return "separate-bubbles"
    raise CursorSnapshotError("composer has no supported conversation capability")


def _require_settled(composer: dict) -> None:
    for field in ("generatingBubbleIds", "queueItems"):
        if field in composer and not isinstance(composer[field], list):
            raise CursorSnapshotError(
                f"composer has an invalid structured list {field}"
            )
        if composer.get(field):
            raise CursorSessionUnsettled(
                "composer has active or queued bubbles", CursorRetryCategory.ACTIVE,
            )
    _require_exact_booleans(
        composer, "composer", ("isContinuationInProgress",)
    )
    if composer.get("isContinuationInProgress") is True:
        raise CursorSessionUnsettled(
            "composer continuation is in progress", CursorRetryCategory.ACTIVE,
        )
    status = composer.get("status")
    if status == "none":
        raise CursorSessionUnsettled(
            "composer is a draft", CursorRetryCategory.DRAFT,
        )
    if status not in (None, "completed", "aborted"):
        raise CursorSessionUnsettled(
            "composer status is not terminal", CursorRetryCategory.ACTIVE,
        )
    # Missing status is an observed historical convention, not accepted for the
    # modern encrypted/stateful generations where it could mean partial data.
    version = composer.get("_v")
    if status is None and isinstance(version, int) and version >= 14:
        raise CursorSessionUnsettled(
            "modern composer has no terminal status", CursorRetryCategory.ACTIVE,
        )


def _project_separate_order(conn: sqlite3.Connection, composer_id: str,
                            composer: dict) -> list[dict]:
    ordered = []
    seen = set()
    copied_key_index = None
    for index, summary in enumerate(composer["fullConversationHeadersOnly"]):
        if not isinstance(summary, dict) or not isinstance(summary.get("bubbleId"), str) \
                or not summary.get("bubbleId"):
            raise CursorSnapshotIncomplete(
                f"{composer_id}: ordered header {index} has no bubble id"
            )
        bubble_id = summary["bubbleId"]
        if bubble_id in seen:
            raise CursorSnapshotIncomplete(
                f"{composer_id}: duplicate ordered bubble {bubble_id}"
            )
        seen.add(bubble_id)
        bubble = _kv_object(
            conn, f"bubbleId:{composer_id}:{bubble_id}", null_is_missing=False
        )
        if bubble is None:
            if copied_key_index is None:
                copied_key_index = _bubble_key_index(conn)
            bubble = _resolve_copied_bubble(conn, bubble_id, summary, copied_key_index)
        if bubble is None:
            raise CursorSnapshotIncomplete(
                f"{composer_id}: missing ordered bubble {bubble_id}"
            )
        stored_id = bubble.get("bubbleId")
        if stored_id is not None and stored_id != bubble_id:
            raise CursorSnapshotIncomplete(
                f"{composer_id}: bubble id mismatch for {bubble_id}"
            )
        if summary.get("type") is not None and bubble.get("type") != summary.get("type"):
            raise CursorSnapshotIncomplete(
                f"{composer_id}: bubble type mismatch for {bubble_id}"
            )
        bubble_type = _bubble_type(bubble, composer_id, bubble_id)
        ordered.append({
            "bubbleId": bubble_id,
            "type": bubble_type,
            "createdAt": _source_created_at(bubble, summary),
            "payload": _project_bubble(bubble, bubble_id),
        })
    return ordered


def _bubble_key_index(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """All bubble KV keys grouped by the id after their last colon.

    Built at most once per projected session, inside that session's pinned read
    transaction. The range predicate stays on the covering key index (';' is
    the code point after ':'), so this never touches value blobs — unlike the
    per-bubble ``LIKE ... ESCAPE`` fallback it replaces, whose escape clause
    forced a full table scan of the multi-GB live database for every copied
    bubble.
    """
    index: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT key FROM cursorDiskKV WHERE key >= 'bubbleId:' AND key < 'bubbleId;'"
    ):
        key = row[0]
        if isinstance(key, str):
            index.setdefault(key.rsplit(":", 1)[-1], []).append(key)
    return index


def _resolve_copied_bubble(conn: sqlite3.Connection, bubble_id: str,
                           summary: dict,
                           key_index: dict[str, list[str]]) -> Optional[dict]:
    """Resolve copied v17 placements whose target composer has no KV payload.

    A global fallback is safe only with an exact structured timestamp and one
    matching candidate.  A bare bubble UUID has observed collisions.
    """
    wanted_ts = summary.get("createdAt")
    if not isinstance(wanted_ts, str) or not wanted_ts:
        return None
    suffix = ":" + bubble_id
    if ":" in bubble_id:
        keys = (k for group in key_index.values() for k in group)
    else:
        keys = iter(key_index.get(bubble_id, ()))
    matches = []
    for key in keys:
        # Match the shape 'bubbleId:' + owner + ':' + bubble_id, exactly as the
        # replaced LIKE pattern did: the length guard rejects a key whose only
        # colon is the prefix's own.
        if not key.endswith(suffix) \
                or len(key) < len("bubbleId:") + len(suffix):
            continue
        row = conn.execute(
            "SELECT value FROM cursorDiskKV WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            continue
        candidate = _json_object(row[0], key)
        if candidate and candidate.get("createdAt") == wanted_ts \
                and candidate.get("bubbleId") == bubble_id \
                and (summary.get("type") is None
                     or candidate.get("type") == summary.get("type")):
            matches.append(candidate)
    return matches[0] if len(matches) == 1 else None


def _project_session_metadata(composer_id: str, composer: dict, header_row,
                              header_value: dict) -> dict:
    workspace = composer.get("workspaceIdentifier") or header_value.get("workspaceIdentifier")
    repos = composer.get("trackedGitRepos") or header_value.get("trackedGitRepos")
    out = {
        "composerId": composer_id,
        "createdAt": _first_typed(
            (composer.get("createdAt"), header_row["createdAt"] if header_row else None),
            (int, float),
        ),
        "lastUpdatedAt": _first_typed(
            (composer.get("lastUpdatedAt"),
             header_row["lastUpdatedAt"] if header_row else None),
            (int, float),
        ),
        "status": _first_typed((composer.get("status"),), str),
        "name": _first_typed((composer.get("name"), header_value.get("name")), str),
        "subtitle": _first_typed(
            (composer.get("subtitle"), header_value.get("subtitle")), str
        ),
        "recency": _first_typed(
            (header_row["recency"] if header_row else None,), (int, float)
        ),
        "checkpointAt": _first_typed(
            (header_row["checkpointAt"] if header_row else None,), (int, float)
        ),
        "isArchived": _sqlite_bool(header_row["isArchived"], "isArchived")
        if header_row else False,
        "isSubagent": _sqlite_bool(header_row["isSubagent"], "isSubagent")
        if header_row else isinstance(composer.get("subagentInfo"), dict),
        "workspaceIdentifier": _project_workspace(workspace),
        "trackedGitRepos": _project_repos(repos),
        "agentLocation": _project_agent_location(header_value.get("agentLocation")),
        "subagentInfo": _project_subagent_info(
            composer.get("subagentInfo"),
        ),
        "subagentComposerIds": _string_list(composer.get("subagentComposerIds")),
    }
    return {k: v for k, v in out.items() if v not in (None, [], {})}


def _project_bubble(bubble: dict, bubble_id: str) -> dict:
    _require_exact_booleans(bubble, bubble_id, ("isThought", "isSummarization"))
    out = _pick_scalars(bubble, {
        "_v": int, "type": int, "text": str,
        "createdAt": (str, int, float), "requestId": str,
        "usageUuid": str, "serverBubbleId": str,
        "isSimulatedMsg": bool, "simulatedMsgReason": int,
        "isPlanExecution": bool, "planId": str, "planTitle": str,
        "subagentSpawnTaskToolCallId": str,
        "isThought": bool, "isSummarization": bool,
    })
    out["bubbleId"] = bubble_id
    if out.get("isThought") is True or out.get("isSummarization") is True:
        # Cursor's older generations can store hidden thought/summary material in
        # the ordinary text field.  Keep the source control flag, never the text.
        out.pop("text", None)

    metadata = _pick_scalars(
        bubble.get("simulatedMessageMetadata"), {"taskId": str, "title": str}
    )
    if metadata:
        out["simulatedMessageMetadata"] = metadata
    timing = _pick_scalars(
        bubble.get("timingInfo"),
        {
            "clientRpcSendTime": (int, float), "clientEndTime": (int, float),
            "clientSettleTime": (int, float), "clientStartTime": (int, float),
        },
    )
    if timing:
        out["timingInfo"] = timing
    tool = _project_tool(bubble.get("toolFormerData"))
    if tool:
        out["toolFormerData"] = tool
    attachments = _project_attachments(bubble.get("context"))
    if attachments:
        out["attachments"] = attachments
    return out


def _require_exact_booleans(value: dict, label: str,
                            fields: tuple[str, ...]) -> None:
    """Fail closed when privacy-sensitive source flags are malformed."""
    for field in fields:
        if field in value and type(value[field]) is not bool:
            raise CursorSnapshotError(
                f"{label}: invalid structured boolean {field}"
            )


def _project_tool(value) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("name"), str) \
            or not isinstance(value.get("toolCallId"), str):
        return {}
    out = _pick_scalars(value, {
        "modelCallId": str, "name": str, "status": str, "tool": int,
        "toolCallId": str, "toolIndex": int, "error": str,
        "userDecision": str,
    })
    # These fields are intentionally lossless transcript evidence.  Unlike all
    # metadata projections, their arbitrary JSON shape is part of the contract.
    for key in ("params", "rawArgs", "result"):
        if key in value and _is_json_tree(value[key]):
            out[key] = value[key]
    extra = _pick_scalars(
        value.get("additionalData"),
        {
            "subagentComposerId": str, "startedAtMs": (int, float),
            "status": str, "terminationReason": str, "isPruned": bool,
            "totalFiles": int, "totalMatches": int, "path": str,
            "pattern": str, "outputMode": str, "backgroundShellId": str,
            "taskId": str,
        },
    )
    if extra:
        out["additionalData"] = extra
    return out


def _project_attachments(context) -> list[dict]:
    if not isinstance(context, dict) or not isinstance(context.get("selectedImages"), list):
        return []
    out = []
    for item in context["selectedImages"]:
        projected = _pick_scalars(
            item, {
                "uuid": str, "path": str, "loadedAt": (str, int, float),
                "addedWithoutMention": bool,
            }
        )
        if isinstance(item, dict):
            dimension = _pick_scalars(
                item.get("dimension"), {"width": (int, float), "height": (int, float)}
            )
            if dimension:
                projected["dimension"] = dimension
        if projected:
            out.append(projected)
    return out


def _project_workspace(value) -> dict:
    if not isinstance(value, dict):
        return {}
    out = _pick_scalars(value, {"id": str, "type": str})
    uri = _pick_scalars(value.get("uri"), {
        "$mid": int, "fsPath": str, "path": str, "scheme": str,
    })
    if uri:
        out["uri"] = uri
    return out


def _project_repos(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    for repo in value:
        projected = _pick_scalars(repo, {"repoPath": str, "repoName": str})
        if isinstance(repo, dict) and isinstance(repo.get("branches"), list):
            branches = [
                _pick_scalars(b, {"branchName": str,
                                  "lastInteractionAt": (int, float)})
                for b in repo["branches"] if isinstance(b, dict)
            ]
            branches = [b for b in branches if b]
            if branches:
                projected["branches"] = branches
        if projected:
            out.append(projected)
    return out


def _project_agent_location(value) -> dict:
    out = _pick_scalars(value, {
        "type": str, "status": str, "worktreeId": str, "worktreePath": str,
        "sourceRepoRootPath": str, "ownership": str, "retentionPolicy": str,
    })
    if not isinstance(value, dict):
        return out
    for key in ("environment", "sourceEnvironment"):
        environment = value.get(key)
        if isinstance(environment, str):
            out[key] = environment
        else:
            projected = _project_workspace(environment)
            if projected:
                out[key] = projected
    return out


def _source_created_at(bubble: dict, summary: Optional[dict]):
    if _valid_source_time(bubble.get("createdAt")):
        return bubble.get("createdAt")
    if summary and _valid_source_time(summary.get("createdAt")):
        return summary.get("createdAt")
    timing = bubble.get("timingInfo")
    if isinstance(timing, dict):
        for key in ("clientRpcSendTime", "clientEndTime", "clientSettleTime"):
            if _valid_source_time(timing.get(key)):
                return timing[key]
    return None


def _valid_source_time(value) -> bool:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Reject renderer/process-relative counters while tolerating the full
        # observed Cursor history and a generous future window.
        return 946684800000 <= value <= 4102444800000
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return 2000 <= parsed.year <= 2100
    return False


def _bubble_id(bubble: dict, index: int) -> str:
    for key in ("bubbleId", "id"):
        if isinstance(bubble.get(key), str) and bubble[key]:
            return bubble[key]
    return f"embedded-{index}"


def _bubble_type(bubble: dict, composer_id: str, bubble_id: str) -> int:
    value = bubble.get("type")
    if isinstance(value, int) and not isinstance(value, bool) and value in (1, 2):
        return value
    raise CursorSnapshotIncomplete(f"{composer_id}: invalid bubble type for {bubble_id}")


def _kv_object(conn: sqlite3.Connection, key: str,
               null_is_missing: bool = True) -> Optional[dict]:
    row = conn.execute("SELECT value FROM cursorDiskKV WHERE key=?", (key,)).fetchone()
    if row is None:
        return None
    if row[0] is None and not null_is_missing:
        raise CursorSnapshotError(f"{key}: null JSON value")
    return _json_object(row[0], key)


def _json_object(value, label: str) -> Optional[dict]:
    if value is None:
        raise CursorSnapshotError(f"{label}: null JSON value")
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise CursorSnapshotError(f"{label}: invalid UTF-8 JSON")
    if not isinstance(value, str):
        raise CursorSnapshotError(f"{label}: JSON value is not text")
    try:
        decoded = json.loads(value)
    except (ValueError, RecursionError):
        raise CursorSnapshotError(f"{label}: invalid JSON")
    if not isinstance(decoded, dict):
        raise CursorSnapshotError(f"{label}: JSON value is not an object")
    if not _is_json_tree(decoded):
        raise CursorSnapshotError(f"{label}: JSON contains non-finite or invalid values")
    return decoded


def _pick_scalars(value, schema: dict) -> dict:
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, expected in schema.items():
        field = value.get(key)
        if field is None:
            continue
        # bool is an int subclass; admit it only for explicitly boolean fields.
        accepts_bool = expected is bool or (
            isinstance(expected, tuple) and bool in expected
        )
        if isinstance(field, bool) and not accepts_bool:
            continue
        if isinstance(field, expected):
            out[key] = field
    return out


def _first_typed(values, expected):
    for value in values:
        accepts_bool = expected is bool or (
            isinstance(expected, tuple) and bool in expected
        )
        if isinstance(value, bool) and not accepts_bool:
            continue
        if isinstance(value, float) and not math.isfinite(value):
            continue
        if isinstance(value, expected):
            return value
    return None


def _project_subagent_info(value) -> dict:
    out = _pick_scalars(value, {
        "parentComposerId": str, "rootParentConversationId": str,
        "parentRequestId": str, "rootParentRequestId": str,
        "conversationLengthAtSpawn": int, "subagentType": int,
        "subagentTypeName": str, "toolCallId": str,
    })
    if isinstance(value, dict):
        history = _string_list(value.get("toolCallIdHistory"))
        if history:
            out["toolCallIdHistory"] = history
        starts = value.get("toolCallConversationStartIndexById")
        if isinstance(starts, dict):
            projected = {
                k: v for k, v in starts.items()
                if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool)
            }
            if projected:
                out["toolCallConversationStartIndexById"] = projected
    return out


def _resolve_subagent_spawn(conn: sqlite3.Connection, child_id: str, value) -> dict:
    """Resolve a child to one current parent call using structured identities."""
    if not isinstance(value, dict):
        return {}
    parent_id = value.get("parentComposerId")
    call_id = value.get("toolCallId")
    if not isinstance(parent_id, str) or not parent_id \
            or not isinstance(call_id, str) or not call_id:
        return {}
    try:
        parent = _kv_object(
            conn, "composerData:" + parent_id, null_is_missing=False
        )
        if parent is None:
            return {}
        capability = _capability(parent)
        candidates = []
        if capability == "embedded":
            source = ((bubble, None) for bubble in parent["conversation"])
        else:
            entries = []
            for summary in parent["fullConversationHeadersOnly"]:
                if not isinstance(summary, dict):
                    continue
                bubble_id = summary.get("bubbleId")
                if not isinstance(bubble_id, str) or not bubble_id:
                    continue
                bubble = _kv_object(conn, f"bubbleId:{parent_id}:{bubble_id}")
                if bubble is not None:
                    entries.append((bubble, summary))
            source = iter(entries)
        for bubble, summary in source:
            if not isinstance(bubble, dict):
                continue
            summary_id = summary.get("bubbleId") if isinstance(summary, dict) else None
            stored_id = bubble.get("bubbleId")
            if isinstance(summary_id, str) and stored_id is not None \
                    and stored_id != summary_id:
                continue
            bubble_type = bubble.get("type")
            if not isinstance(bubble_type, int) or isinstance(bubble_type, bool) \
                    or bubble_type not in (1, 2) \
                    or (isinstance(summary, dict) and summary.get("type") is not None
                        and summary.get("type") != bubble_type):
                continue
            if bubble.get("isThought") is True \
                    or bubble.get("isSummarization") is True:
                continue
            tool = bubble.get("toolFormerData")
            additional = tool.get("additionalData") if isinstance(tool, dict) else None
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) \
                    or not tool["name"] or tool.get("toolCallId") != call_id \
                    or not isinstance(additional, dict) \
                    or additional.get("subagentComposerId") != child_id:
                continue
            bubble_id = summary_id or stored_id or bubble.get("id")
            created_at = _source_created_at(
                bubble, summary if isinstance(summary, dict) else None
            )
            if isinstance(bubble_id, str) and bubble_id \
                    and _valid_source_time(created_at):
                candidates.append((bubble_id, created_at))
        if len(candidates) == 1:
            return {
                "spawnBubbleId": candidates[0][0],
                "spawnCreatedAt": candidates[0][1],
            }
    except (sqlite3.Error, CursorSnapshotError):
        return {}
    return {}


def _string_list(value) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _sqlite_bool(value, label: str) -> bool:
    if value in (0, False, None):
        return False
    if value in (1, True):
        return True
    raise CursorSnapshotError(f"invalid structured boolean column {label}")


def _is_json_tree(value, max_depth: int = 256, max_nodes: int = 1_000_000) -> bool:
    """Validate strict, UTF-8 JSON without recursive Python traversal."""
    stack = [(value, 0)]
    seen = 0
    while stack:
        current, depth = stack.pop()
        seen += 1
        if seen > max_nodes:
            return False
        if current is None or isinstance(current, (bool, int)):
            continue
        if isinstance(current, str):
            try:
                current.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                return False
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                return False
            continue
        if isinstance(current, (list, dict)):
            if depth >= max_depth:
                return False
            if isinstance(current, dict):
                for key in current:
                    if not isinstance(key, str):
                        return False
                    try:
                        key.encode("utf-8", errors="strict")
                    except UnicodeEncodeError:
                        return False
                values = current.values()
            else:
                values = current
            stack.extend((item, depth + 1) for item in values)
            continue
        return False
    return True

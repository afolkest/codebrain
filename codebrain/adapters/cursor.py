"""Cursor adapter for reconstructed safe revision snapshots.

Cursor's current ordered composer view is linear.  A bubble may emit a visible
message, a tool call, and a terminal tool result; those components are chained
in that order.  Copied prefixes retain bubble identity and authoring time, so
timed IDs deduplicate across sessions without a lineage pre-scan.  Untimed
legacy bubbles deliberately use session-scoped IDs and are never classified as
inherited.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from codebrain import cursor_archive
from codebrain.adapters.base import EventRow, ParsedSession, PlacementRow, SessionRow


SOURCE = "cursor"
TERMINAL_TOOL_STATUSES = {"completed", "error", "cancelled"}
COMMAND_FIELDS = {
    "run_terminal_command_v2": "command",
    "run_terminal_cmd": "command",
}
FILE_FIELDS = {
    "read_file_v2": ("targetFile", "effectiveUri"),
    "read_file": ("relativeWorkspacePath", "targetFile"),
    "read": ("relativeWorkspacePath", "targetFile"),
    "edit_file_v2": ("relativeWorkspacePath",),
    "edit_file": ("relativeWorkspacePath",),
    "search_replace": ("relativeWorkspacePath",),
    "delete_file": ("relativeWorkspacePath",),
    "apply_patch": ("relativeWorkspacePath",),
    "reapply": ("relativeWorkspacePath",),
    "write": ("relativeWorkspacePath",),
    "ripgrep_raw_search": ("path",),
    "grep": ("path",),
    "glob_file_search": ("targetDirectory",),
    "list_dir": ("directoryPath",),
}
RESULT_TEXT_FIELDS = (
    "output", "contents", "markdown", "content", "resultForModel",
)


class CursorAdapterError(RuntimeError):
    pass


def parse_file(path: Path, machine: Optional[str] = None) -> ParsedSession:
    return parse_snapshot(cursor_archive.read_latest_snapshot(path), machine=machine)


def parse_snapshot(snapshot: dict, machine: Optional[str] = None) -> ParsedSession:
    if not isinstance(snapshot, dict) \
            or isinstance(snapshot.get("projectionVersion"), bool) \
            or snapshot.get("projectionVersion") != 1:
        raise CursorAdapterError("unsupported Cursor projection")
    composer_id = snapshot.get("composerId")
    session_data = snapshot.get("session")
    ordered = snapshot.get("order")
    if not isinstance(composer_id, str) or not composer_id \
            or not isinstance(session_data, dict) or not isinstance(ordered, list):
        raise CursorAdapterError("invalid Cursor projection envelope")
    if session_data.get("composerId") != composer_id:
        raise CursorAdapterError("Cursor session identity mismatch")

    sid = f"{SOURCE}:{composer_id}"
    created_ms = _epoch_ms(session_data.get("createdAt"))
    created_at = _iso(created_ms) if created_ms is not None else None
    events: list[EventRow] = []
    inherited_by_id: dict[str, int] = {}
    seen_bubbles = set()
    authored_started = False

    def emit(event_id: str, ts: str, actor: str, typ: str, text: Optional[str],
             refs: dict, raw: dict, inherited: int,
             tool_call_event_id: Optional[str] = None) -> None:
        events.append(EventRow(
            event_id=event_id, ts=ts, actor=actor, type=typ, text=text,
            refs=refs, raw=raw, tool_call_event_id=tool_call_event_id,
            origin_session_id=None if inherited else sid,
        ))
        inherited_by_id[event_id] = inherited

    for item in ordered:
        item_type = item.get("type") if isinstance(item, dict) else None
        if not isinstance(item, dict) or not isinstance(item.get("payload"), dict) \
                or not isinstance(item.get("bubbleId"), str) \
                or not isinstance(item_type, int) or isinstance(item_type, bool) \
                or item_type not in (1, 2):
            raise CursorAdapterError("invalid Cursor ordered bubble")
        payload = item["payload"]
        bubble_id = item["bubbleId"]
        if not bubble_id or bubble_id in seen_bubbles:
            raise CursorAdapterError("duplicate or empty Cursor bubble identity")
        seen_bubbles.add(bubble_id)
        if payload.get("bubbleId") != bubble_id or payload.get("type") != item_type:
            raise CursorAdapterError("Cursor bubble identity mismatch")

        hidden = payload.get("isThought") is True \
            or payload.get("isSummarization") is True
        if hidden:
            continue
        text_value = payload.get("text")
        has_message = isinstance(text_value, str) and (
            item_type == 1 or bool(text_value.strip())
        )
        tool = payload.get("toolFormerData")
        has_tool = isinstance(tool, dict) and isinstance(tool.get("name"), str) \
            and bool(tool["name"]) and isinstance(tool.get("toolCallId"), str) \
            and bool(tool["toolCallId"])

        event_ms = _epoch_ms(item.get("createdAt"))
        if event_ms is None:
            identity = (
                f"{SOURCE}:{_component(composer_id)}:{_component(bubble_id)}:"
                "untimed"
            )
            ts = created_at or _iso(0)
            inherited = 0
        else:
            identity = f"{SOURCE}:{_component(bubble_id)}:{event_ms}"
            ts = _iso(event_ms)
            inherited = int(created_ms is not None and event_ms < created_ms)
        if (has_message or has_tool) and inherited and authored_started:
            raise CursorAdapterError("Cursor inherited events are not a prefix")
        if (has_message or has_tool) and not inherited:
            authored_started = True

        if has_message:
            emit(
                identity + ":message", ts,
                "user" if item_type == 1 else "assistant", "message",
                text_value, _empty_refs(), payload, inherited,
            )

        if has_tool:
            call_id = identity + ":call"
            call_text, refs = _render_tool_call(tool)
            emit(
                call_id, ts, "assistant", "tool_call", call_text, refs,
                payload, inherited,
            )
            if _has_tool_result(tool):
                emit(
                    identity + ":result", ts, "tool", "tool_result",
                    _render_tool_result(tool), _empty_refs(), payload,
                    inherited, tool_call_event_id=call_id,
                )

    placements = []
    previous = None
    branch_point = None
    for sequence, event in enumerate(events):
        inherited = inherited_by_id[event.event_id]
        placements.append(PlacementRow(
            session_id=sid, event_id=event.event_id, seq=sequence,
            parent_event_id=previous, live=1, inherited=inherited,
        ))
        previous = event.event_id
        if inherited:
            branch_point = event.event_id

    info = session_data.get("subagentInfo")
    parent_session_id = relation = spawn_event_id = None
    if isinstance(info, dict) and isinstance(info.get("parentComposerId"), str) \
            and info["parentComposerId"]:
        parent_session_id = f"{SOURCE}:{info['parentComposerId']}"
        relation = "subagent"
        spawn_ms = _epoch_ms(info.get("spawnCreatedAt"))
        spawn_bubble = info.get("spawnBubbleId")
        if spawn_ms is not None and isinstance(spawn_bubble, str) and spawn_bubble:
            spawn_event_id = (
                f"{SOURCE}:{_component(spawn_bubble)}:{spawn_ms}:call"
            )

    timestamps = [event.ts for event in events]
    session = SessionRow(
        session_id=sid, source=SOURCE, machine=machine,
        cwd=_cwd(session_data), repo=_repo(session_data),
        created_at=created_at,
        started_at=min(timestamps) if timestamps else None,
        ended_at=max(timestamps) if timestamps else None,
        parent_session_id=parent_session_id, relation=relation,
        spawn_event_id=spawn_event_id,
        branch_point_event_id=branch_point,
        tip_event_id=events[-1].event_id if events else None,
        title=session_data.get("name")
        if isinstance(session_data.get("name"), str) else None,
    )
    return ParsedSession(session=session, events=events, placements=placements)


def _component(value: str) -> str:
    return quote(value, safe="")


def _epoch_ms(value) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value < 946684800000 \
                or value > 4102444800000:
            return None
        return int(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    milliseconds = int(parsed.astimezone(timezone.utc).timestamp() * 1000)
    return milliseconds if 946684800000 <= milliseconds <= 4102444800000 else None


def _iso(milliseconds: int) -> str:
    return datetime.fromtimestamp(
        milliseconds / 1000, tz=timezone.utc
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            return value
    return value


def _tool_args(tool: dict) -> dict:
    empty = {}
    for key in ("params", "rawArgs"):
        value = _json_value(tool.get(key))
        if isinstance(value, dict):
            if value:
                return value
            empty = value
    return empty


def _render_tool_call(tool: dict) -> tuple[str, dict]:
    name = tool["name"]
    args = _tool_args(tool)
    files = []
    for field in FILE_FIELDS.get(name, ()):
        value = args.get(field)
        if isinstance(value, str) and value and value not in files:
            files.append(value)
    if name == "read_lints" and isinstance(args.get("paths"), list):
        for value in args["paths"]:
            if isinstance(value, str) and value and value not in files:
                files.append(value)
    commands = []
    command_field = COMMAND_FIELDS.get(name)
    command = args.get(command_field) if command_field else None
    if isinstance(command, str) and command:
        commands.append(command)
    refs = {"files": files, "commands": commands}
    if commands:
        return f"{name}: {commands[0]}", refs
    if files:
        return f"{name}: {files[0]}", refs
    body = json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":")) \
        if args else ""
    return (f"{name}: {body[:160]}" if body else name), refs


def _has_tool_result(tool: dict) -> bool:
    return "result" in tool or isinstance(tool.get("error"), str) \
        or tool.get("status") in TERMINAL_TOOL_STATUSES


def _render_tool_result(tool: dict) -> str:
    if isinstance(tool.get("error"), str):
        return tool["error"]
    if "result" not in tool:
        return ""
    original = tool.get("result")
    result = _json_value(original)
    if isinstance(result, dict):
        for key in RESULT_TEXT_FIELDS:
            if key in result:
                return _render_json_value(result[key])
    if isinstance(result, str):
        return result
    if result is original and isinstance(original, str):
        return original
    return _render_json_value(result)


def _render_json_value(value) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _empty_refs() -> dict:
    return {"files": [], "commands": []}


def _cwd(session: dict) -> Optional[str]:
    workspace = session.get("workspaceIdentifier")
    uri = workspace.get("uri") if isinstance(workspace, dict) else None
    if isinstance(uri, dict) and isinstance(uri.get("fsPath"), str):
        return uri["fsPath"]
    location = session.get("agentLocation")
    if isinstance(location, dict) and isinstance(location.get("worktreePath"), str):
        return location["worktreePath"]
    return None


def _repo(session: dict) -> Optional[str]:
    repos = session.get("trackedGitRepos")
    if isinstance(repos, list):
        for repo in repos:
            if isinstance(repo, dict) and isinstance(repo.get("repoPath"), str):
                return repo["repoPath"]
    return None

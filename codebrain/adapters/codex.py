"""Codex adapter — flat append-only rollout log (see formats/codex.md, SCHEMA.md).

Codex has no native parent tree; we **synthesize** one (SCHEMA.md "Linearization"):

  - Turns are anchored on Codex's clean structured user item:
    ``event_msg.user_message`` or ``event_msg.item_completed`` with
    ``item.type=UserMessage``. Which signal appears depends on the rollout mode,
    not just the cli version. We deliberately do NOT anchor on ``task_started`` —
    the oldest logs (cli 0.39, 2025-09) have no
    ``task_started``/``task_complete`` at all. A turn = a user prompt + the emitted
    events that follow it.
    Structured inter-agent messages also start userless rollback turns; they are
    non-human input, never actor=user.
  - Within a turn, emitted events chain linearly (each parents the previous one).
  - ``thread_rolled_back{num_turns:n}`` pops the last ``n`` *live* user-turns; they
    survive as dead side branches (live=0) and the next turn parents to the reverted
    live tip — the linear-log equivalent of excluding Claude's abandoned subtree.

Two parallel in-file views (formats/codex.md "Dedup"): the clean human prompt comes
from the structured ``event_msg`` user item; assistant text + tool calls/results
come from the ``response_item`` backbone. Never both — no double count. Dropped
(kept in raw only): reasoning (encrypted in current versions), injected
``developer``/bloated-``user`` ``response_item`` messages, and pure telemetry
(``token_count``, streamed ``event_msg.agent_message``…).

Resume re-emits ``session_meta`` with the same id in the same file → one session,
no dedup. Compaction appends a ``compacted`` marker and continues linearly → handled
for free (the marker is skipped; the next turn continues the live chain).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from codebrain.adapters.base import EventRow, ParsedSession, PlacementRow, SessionRow, read_records

SOURCE = "codex"

_UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_ROLLOUT_IDS_RE = re.compile(
    rf"(?P<session>{_UUID_RE})(?:_(?P<fragment>{_UUID_RE}))?$"
)


@dataclass(frozen=True)
class RolloutFragment:
    """One physical Codex journal segment.

    Newer Codex Desktop sessions paginate one logical thread across several
    rollout files. ``session_uuid`` is the stable user-visible thread id from
    ``session_meta.id``; ``fragment_uuid`` is the source-generated journal id
    carried by the rollout filename. The JSON header names only the predecessor
    fragment, so the filename is the sole positive signal for the current one.
    """

    path: Path
    session_uuid: str
    fragment_uuid: str
    previous_fragment_uuid: Optional[str]
    start_ordinal: int
    previous_end_byte_offset: Optional[int]
    header_error: Optional[str] = None


@dataclass(frozen=True)
class RolloutGroup:
    """Every discovered physical file for one logical Codex session."""

    session_uuid: str
    fragments: tuple[RolloutFragment, ...]

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(fragment.path for fragment in self.fragments)

    @property
    def name(self) -> str:
        return f"codex:{self.session_uuid} ({len(self.fragments)} rollout files)"

    @property
    def is_paginated(self) -> bool:
        """Whether this logical session spans distinct physical journals."""
        return len({fragment.fragment_uuid for fragment in self.fragments}) > 1

# Command-execution tools across versions (formats/codex.md "Tools & commands").
CMD_TOOLS = {"shell", "shell_command", "exec_command", "local_shell", "container.exec"}


def _uuid_from_filename(stem: str) -> str:
    # rollout-<localTs>-<uuid>; the uuid is the trailing 8-4-4-4-12 groups.
    parts = stem.split("-")
    return "-".join(parts[-5:]) if len(parts) >= 5 else stem


def _rollout_filename_ids(path: Path) -> tuple[str, str]:
    """Return ``(logical session, physical fragment)`` from a rollout name.

    Paginated Desktop names end in ``<session_uuid>_<fragment_uuid>``. Older and
    unpaginated names end in just ``<session_uuid>``. The structured JSON header
    remains authoritative for the logical session; this parser supplies the
    current fragment id because the header exposes only its predecessor.
    """
    match = _ROLLOUT_IDS_RE.search(path.stem)
    if match is None:
        fallback = _uuid_from_filename(path.stem)
        return fallback, fallback
    session_uuid = match.group("session").lower()
    fragment_uuid = (match.group("fragment") or session_uuid).lower()
    return session_uuid, fragment_uuid


def _fragment_header(path: Path) -> RolloutFragment:
    """Read only enough source JSON to identify a physical rollout fragment."""
    filename_session, fragment_uuid = _rollout_filename_ids(path)
    payload = None
    header_ordinal = None
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") != "session_meta":
                continue
            candidate = record.get("payload")
            if isinstance(candidate, dict):
                payload = candidate
                header_ordinal = record.get("ordinal")
                break
    payload = payload or {}
    session_uuid = payload.get("id")
    if not isinstance(session_uuid, str) or not session_uuid:
        session_uuid = filename_session
    else:
        session_uuid = session_uuid.lower()
    if filename_session != session_uuid:
        raise ValueError(
            f"rollout filename session {filename_session} disagrees with "
            f"session_meta.id {session_uuid}"
        )

    history_base = payload.get("history_base")
    same_session_continuation = fragment_uuid != session_uuid
    if not same_session_continuation:
        # A single-UUID rollout can itself be a fork whose history_base points
        # into another canonical session. It is the logical root of *this*
        # session; retain its source ordinal so a later same-session pagination
        # can be normalized relative to it, but do not stitch it to that parent.
        previous = None
        start = header_ordinal if isinstance(header_ordinal, int) \
            and not isinstance(header_ordinal, bool) else 0
        previous_bytes = None
    elif isinstance(history_base, dict):
        previous = history_base.get("thread_id")
        start = history_base.get("end_ordinal_exclusive")
        previous_bytes = history_base.get("end_byte_offset")
        if not isinstance(previous, str) or not previous:
            raise ValueError(f"invalid history_base.thread_id in {path}")
        previous = previous.lower()
        if not isinstance(start, int) or isinstance(start, bool) or start <= 0:
            raise ValueError(f"invalid history_base.end_ordinal_exclusive in {path}")
        if (not isinstance(previous_bytes, int) or isinstance(previous_bytes, bool)
                or previous_bytes <= 0):
            raise ValueError(f"invalid history_base.end_byte_offset in {path}")
        if header_ordinal != start:
            raise ValueError(
                f"session_meta.ordinal {header_ordinal!r} disagrees with "
                f"history_base.end_ordinal_exclusive {start} in {path}"
            )
    else:
        raise ValueError(
            f"paginated rollout {path} has no structured session_meta.history_base"
        )

    return RolloutFragment(
        path=path,
        session_uuid=session_uuid,
        fragment_uuid=fragment_uuid,
        previous_fragment_uuid=previous,
        start_ordinal=start,
        previous_end_byte_offset=previous_bytes,
    )


def group_rollout_files(paths) -> list[RolloutGroup]:
    """Group physical rollout files by source-generated logical identity.

    The common case is one traditional rollout file. Keep discovery stat-only
    there so refresh-on-read does not reopen thousands of cold transcripts just
    to learn what their filenames already state. Composite names and duplicate
    physical copies take the slower structured-header validation path.
    """
    named: dict[str, list[tuple[Path, str]]] = {}
    for path in paths:
        path = Path(path)
        session_uuid, fragment_uuid = _rollout_filename_ids(path)
        named.setdefault(session_uuid, []).append((path, fragment_uuid))

    groups = []
    for session_uuid, members in sorted(named.items()):
        if len(members) == 1 and members[0][1] == session_uuid:
            path, fragment_uuid = members[0]
            fragments = [RolloutFragment(
                path=path,
                session_uuid=session_uuid,
                fragment_uuid=fragment_uuid,
                previous_fragment_uuid=None,
                start_ordinal=0,
                previous_end_byte_offset=None,
            )]
        else:
            fragments = []
            for path, fragment_uuid in members:
                try:
                    fragment = _fragment_header(path)
                except Exception as exc:  # isolated in parse_rollout_group
                    fragment = RolloutFragment(
                        path=path,
                        session_uuid=session_uuid,
                        fragment_uuid=fragment_uuid,
                        previous_fragment_uuid=None,
                        start_ordinal=0,
                        previous_end_byte_offset=None,
                        header_error=str(exc),
                    )
                fragments.append(fragment)
        groups.append(RolloutGroup(
            session_uuid=session_uuid, fragments=tuple(fragments),
        ))
    return groups


def rollout_group_needs_upgrade(group: RolloutGroup) -> bool:
    """Whether a pre-grouping/pre-UserMessage cache must reparse this unit.

    Pagination is identified by source-generated fragment ids. Ordinary files
    are candidates only when their JSON carries the exact structured user-item
    shape the older adapter skipped. This one-time scanner deliberately does not
    classify from prompt text.
    """
    if group.is_paginated:
        return True
    for path in group.paths:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("type") != "event_msg":
                    continue
                payload = record.get("payload")
                if not isinstance(payload, dict) \
                        or payload.get("type") != "item_completed":
                    continue
                item = payload.get("item")
                if isinstance(item, dict) and item.get("type") == "UserMessage":
                    return True
    return False


def _payload(rec) -> dict:
    """Envelope payloads are always objects for the types we read; guard against
    drift so one valid-JSON record with a non-dict payload can't sink the whole file."""
    pl = rec.get("payload")
    return pl if isinstance(pl, dict) else {}


def _parse_args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _join_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("text"))


_PATCH_MARKERS = ("*** Add File: ", "*** Update File: ", "*** Delete File: ")


def _patch_files(patch_text: str) -> list:
    files = []
    for line in patch_text.splitlines():
        for m in _PATCH_MARKERS:
            if line.startswith(m):
                files.append(line[len(m):].strip())
    return files


def _render_tool_call(ri_type: str, name: str, pl: dict):
    """(text, refs) for a function_call / custom_tool_call response_item."""
    files, commands = [], []
    if ri_type == "custom_tool_call":
        inp = pl.get("input")
        if name == "apply_patch" and isinstance(inp, str):
            files = _patch_files(inp)
            text = "apply_patch: " + (", ".join(files[:3]) if files else inp[:120])
        else:
            text = f"{name}: {str(inp)[:160]}"
        return text, {"files": files, "commands": commands}

    args = _parse_args(pl.get("arguments"))
    if name in CMD_TOOLS:
        cmd = args.get("cmd")
        if cmd is None:
            c = args.get("command")
            if isinstance(c, list):
                cmd = " ".join(str(x) for x in c)
            elif isinstance(c, str):
                cmd = c
        if isinstance(cmd, str) and cmd:
            commands.append(cmd)
            return f"{name}: {cmd}", {"files": files, "commands": commands}
    if name == "apply_patch" and isinstance(args.get("input"), str):
        files = _patch_files(args["input"])
        return "apply_patch: " + (", ".join(files[:3]) or args["input"][:120]), {"files": files, "commands": commands}
    # path-ish arg (read_file / update_plan / spawn_agent / write_stdin / …)
    for k in ("path", "file_path", "abs_path"):
        if isinstance(args.get(k), str):
            files.append(args[k])
    body = json.dumps(args, ensure_ascii=False)[:160] if args else ""
    return (f"{name}: {body}" if body else name), {"files": files, "commands": commands}


def _result_text(pl: dict) -> str:
    out = pl.get("output")
    if isinstance(out, str):
        return out
    if out is not None:
        return json.dumps(out, ensure_ascii=False)
    r = pl.get("result")
    return json.dumps(r, ensure_ascii=False) if r is not None else ""


def _mcp_result_text(result) -> str:
    """mcp_tool_call_end result is `{Ok:{content:[{type:text,text}]}}` or `{Err:…}`."""
    if isinstance(result, dict):
        ok = result.get("Ok")
        if isinstance(ok, dict) and isinstance(ok.get("content"), list):
            parts = [b.get("text", "") for b in ok["content"] if isinstance(b, dict) and b.get("text")]
            joined = "\n".join(p for p in parts if p)
            if joined:
                return joined
        return json.dumps(result, ensure_ascii=False)
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False) if result is not None else ""


def _agent_message_text(pl: dict) -> str:
    text = _join_text(pl.get("content"))
    if text.strip():
        return text
    author = pl.get("author")
    recipient = pl.get("recipient")
    route = " -> ".join(x for x in (author, recipient) if isinstance(x, str) and x)
    return f"[inter-agent message{': ' + route if route else ''}]"


def _file_is_prefix(prefix: Path, complete: Path) -> bool:
    """Whether ``prefix`` is a byte-for-byte prefix of ``complete``."""
    if prefix.stat().st_size > complete.stat().st_size:
        return False
    with prefix.open("rb") as left, complete.open("rb") as right:
        while True:
            chunk = left.read(1024 * 1024)
            if not chunk:
                return True
            if right.read(len(chunk)) != chunk:
                return False


def _ordered_fragment_copies(group: RolloutGroup) -> list[RolloutFragment]:
    """Select one append-authoritative copy per fragment and validate its chain."""
    bad = [fragment for fragment in group.fragments if fragment.header_error]
    if bad:
        raise ValueError(bad[0].header_error)
    copies: dict[str, list[RolloutFragment]] = {}
    for fragment in group.fragments:
        copies.setdefault(fragment.fragment_uuid, []).append(fragment)

    selected = []
    for fragment_uuid, candidates in copies.items():
        candidates = sorted(
            candidates,
            key=lambda fragment: (-fragment.path.stat().st_size, str(fragment.path)),
        )
        winner = candidates[0]
        winner_identity = (
            winner.session_uuid, winner.previous_fragment_uuid,
            winner.start_ordinal, winner.previous_end_byte_offset,
        )
        for candidate in candidates[1:]:
            candidate_identity = (
                candidate.session_uuid, candidate.previous_fragment_uuid,
                candidate.start_ordinal, candidate.previous_end_byte_offset,
            )
            if candidate_identity != winner_identity:
                raise ValueError(
                    f"fragment {fragment_uuid} has conflicting structured headers"
                )
            if not _file_is_prefix(candidate.path, winner.path):
                raise ValueError(
                    f"fragment {fragment_uuid} has divergent physical copies"
                )
        selected.append(winner)

    ordered = sorted(selected, key=lambda fragment: fragment.start_ordinal)
    if not ordered:
        return []
    if (ordered[0].fragment_uuid != group.session_uuid
            or ordered[0].previous_fragment_uuid is not None):
        raise ValueError(f"session {group.session_uuid} is missing its root fragment")
    for previous, current in zip(ordered, ordered[1:]):
        if current.previous_fragment_uuid != previous.fragment_uuid:
            raise ValueError(
                f"session {group.session_uuid} has a broken fragment chain: "
                f"{current.fragment_uuid} points to {current.previous_fragment_uuid}, "
                f"expected {previous.fragment_uuid}"
            )
        if current.start_ordinal <= previous.start_ordinal:
            raise ValueError(
                f"session {group.session_uuid} has non-increasing history ordinals"
            )
    return ordered


def parse_rollout_group(group: RolloutGroup, machine: Optional[str] = None,
                        title: Optional[str] = None) -> Optional[ParsedSession]:
    """Parse a paginated Codex journal as one logical append-only transcript.

    The next fragment's structured ``history_base`` is an exact committed-prefix
    watermark for the preceding fragment. Records beyond that boundary are a
    superseded handoff attempt (typically an aborted replay) and remain available
    in raw evidence but are not part of the canonical logical transcript.
    """
    ordered = _ordered_fragment_copies(group)
    if not ordered:
        return None
    if len(ordered) == 1:
        # Preserve the historical local-line ID scheme for ordinary files (and
        # for forks whose source ordinals begin inside another thread's history).
        return parse_file(ordered[0].path, machine=machine, title=title)

    combined = []
    synthetic_base = 0
    for index, fragment in enumerate(ordered):
        with fragment.path.open("rb") as handle:
            raw_lines = handle.readlines()
        records = read_records(fragment.path)
        if index + 1 < len(ordered):
            following = ordered[index + 1]
            boundary = following.previous_end_byte_offset
            committed_bytes = 0
            local_end = None
            for line_index, raw_line in enumerate(raw_lines):
                committed_bytes += len(raw_line)
                if committed_bytes == boundary:
                    local_end = line_index + 1
                    break
                if committed_bytes > boundary:
                    break
            if local_end is None:
                raise ValueError(
                    f"fragment {fragment.fragment_uuid} byte boundary mismatch: "
                    f"{boundary} is not a complete-line prefix"
                )
        else:
            local_end = len(raw_lines)

        committed_records = [
            record for record in records
            if isinstance(record.get("_line"), int)
            and record["_line"] < local_end
        ]
        previous_ordinal = None
        for record in committed_records:
            local_line = record.get("_line")
            source_ordinal = record.get("ordinal")
            if not isinstance(source_ordinal, int) or isinstance(source_ordinal, bool):
                raise ValueError(
                    f"fragment {fragment.fragment_uuid} record at local line "
                    f"{local_line} has invalid ordinal {source_ordinal!r}"
                )
            if previous_ordinal is None:
                if source_ordinal != fragment.start_ordinal:
                    raise ValueError(
                        f"fragment {fragment.fragment_uuid} starts at ordinal "
                        f"{source_ordinal}, expected {fragment.start_ordinal}"
                    )
            elif source_ordinal < previous_ordinal \
                    or source_ordinal > previous_ordinal + 1:
                raise ValueError(
                    f"fragment {fragment.fragment_uuid} has non-contiguous "
                    f"ordinals {previous_ordinal}, {source_ordinal}"
                )
            previous_ordinal = source_ordinal
            record = dict(record)
            # Event ids remain rooted in the original file's physical line
            # coordinates. Each committed prefix advances the next fragment's
            # synthetic base by its physical line count. This both preserves
            # ids assigned before pagination and remains unique when Codex emits
            # the same source ordinal twice for adjacent control records.
            record["_line"] = synthetic_base + local_line
            combined.append(record)

        if index + 1 < len(ordered):
            following = ordered[index + 1]
            if previous_ordinal is None or previous_ordinal + 1 != following.start_ordinal:
                raise ValueError(
                    f"fragment {fragment.fragment_uuid} committed ordinal ends at "
                    f"{previous_ordinal!r}; successor starts at "
                    f"{following.start_ordinal}"
                )
        synthetic_base += local_end

    combined.sort(key=lambda record: record["_line"])
    parsed = parse_file(
        ordered[0].path, machine=machine, title=title, _records=combined,
    )
    if parsed is not None:
        emitted_ordinals: dict[int, str] = {}
        for event in parsed.events:
            source_ordinal = event.raw.get("ordinal")
            if not isinstance(source_ordinal, int) or isinstance(source_ordinal, bool):
                continue
            previous = emitted_ordinals.setdefault(source_ordinal, event.event_id)
            if previous != event.event_id:
                raise ValueError(
                    f"session {group.session_uuid} emits multiple events at "
                    f"source ordinal {source_ordinal}: {previous}, {event.event_id}"
                )
    return parsed


def parse_file(path: Path, machine: Optional[str] = None, title: Optional[str] = None,
               _records=None) -> Optional[ParsedSession]:
    records = read_records(path) if _records is None else _records
    if not records:
        return None

    # ---- session metadata (first session_meta; resume re-emits the same id) ----
    session_uuid = cwd = repo = created_at = parent_uuid = relation = None
    for r in records:
        pl = _payload(r)
        if r.get("type") == "session_meta" and session_uuid is None:
            session_uuid = pl.get("id")
            cwd = pl.get("cwd")
            created_at = pl.get("timestamp") or r.get("timestamp")
            git = pl.get("git")
            if isinstance(git, dict):
                repo = git.get("repository_url") or git.get("branch")
            src = pl.get("source")
            if isinstance(src, dict) and isinstance(src.get("subagent"), dict):
                spawn = src["subagent"].get("thread_spawn") or {}
                parent_uuid, relation = spawn.get("parent_thread_id"), "subagent"
            if parent_uuid is None and pl.get("parent_thread_id"):
                parent_uuid, relation = pl["parent_thread_id"], "subagent"
            if parent_uuid is None and pl.get("forked_from_id"):
                # fork_context sub-agents carry agent_role; bare forks (risk-assessment) don't.
                parent_uuid = pl["forked_from_id"]
                relation = "subagent" if pl.get("agent_role") else "branch"
        elif r.get("type") == "turn_context" and cwd is None:
            cwd = pl.get("cwd")
    if session_uuid is None:
        session_uuid = _uuid_from_filename(path.stem)
    sid = f"{SOURCE}:{session_uuid}"

    # MCP results arrive only as event_msg.mcp_tool_call_end; capture those whose
    # call_id has no *_output (otherwise the output already carries the result).
    output_call_ids = {
        _payload(r).get("call_id")
        for r in records
        if r.get("type") == "response_item"
        and _payload(r).get("type") in ("function_call_output", "custom_tool_call_output")
    }
    output_call_ids.discard(None)

    # ---- single pass in line order: emit events + synthesize the turn forest ----
    events: list[EventRow] = []
    meta: dict[str, dict] = {}
    parent_of: dict[str, Optional[str]] = {}
    call_to_eid: dict[str, str] = {}
    patch_changes: dict[str, dict] = {}
    by_eid: dict[str, EventRow] = {}

    turns: list[dict] = []          # {events: [eid], parent_tip: eid|None, live: bool}
    stack: list[int] = []           # indices into `turns` (live turns, push order)
    state: dict[str, Optional[str]] = {"tip": None}   # current live tip eid

    def emit(rec, actor, typ, text, refs, is_user, tool_call_event_id=None,
             starts_turn=None):
        if starts_turn is None:
            starts_turn = is_user
        eid = f"{sid}:{rec['_line']}"
        e = EventRow(
            event_id=eid, ts=rec.get("timestamp", ""), actor=actor, type=typ,
            text=text, refs=refs, raw={k: v for k, v in rec.items() if k != "_line"},
            tool_call_event_id=tool_call_event_id, origin_session_id=sid,
        )
        events.append(e)
        by_eid[eid] = e
        meta[eid] = {"line": rec["_line"], "ts": rec.get("timestamp", "")}
        parent_of[eid] = state["tip"]
        if starts_turn:
            turns.append({"events": [eid], "parent_tip": state["tip"], "live": True})
            stack.append(len(turns) - 1)
        else:
            if not stack:
                # An emitted event before any user prompt (resume preamble / old logs)
                # or right after a rollback emptied the stack → give it its own turn.
                turns.append({"events": [], "parent_tip": state["tip"], "live": True})
                stack.append(len(turns) - 1)
            turns[stack[-1]]["events"].append(eid)
        state["tip"] = eid
        return eid

    def rollback(n):
        n = min(n, len(stack))
        if n <= 0:
            return
        popped = stack[-n:]
        del stack[-n:]
        revert = turns[popped[0]]["parent_tip"]
        for ti in popped:
            turns[ti]["live"] = False
        state["tip"] = revert

    pending_inter_agent_metadata = None
    for rec in records:
        t = rec.get("type")
        pl = _payload(rec)
        pt = pl.get("type")
        if t == "inter_agent_communication_metadata":
            pending_inter_agent_metadata = pl
            continue
        if t == "event_msg":
            if pt == "user_message":
                emit(rec, "user", "message", pl.get("message") or "", {"files": [], "commands": []}, is_user=True)
            elif pt == "item_completed":
                item = pl.get("item")
                if isinstance(item, dict) and item.get("type") == "UserMessage":
                    emit(rec, "user", "message", _join_text(item.get("content")),
                         {"files": [], "commands": []}, is_user=True)
            elif pt == "thread_rolled_back":
                try:
                    n = int(pl.get("num_turns") or 1)
                except (TypeError, ValueError):
                    n = 1
                rollback(n)
            elif pt == "patch_apply_end":
                cid, ch = pl.get("call_id"), pl.get("changes")
                if cid and isinstance(ch, dict):
                    patch_changes[cid] = ch
            elif pt == "mcp_tool_call_end":
                cid = pl.get("call_id")
                if not (isinstance(cid, str) and cid in output_call_ids):
                    inv = pl.get("invocation")
                    inv = inv if isinstance(inv, dict) else {}
                    label = f"{inv.get('server', 'mcp')}.{inv.get('tool', '?')}"
                    body = _mcp_result_text(pl.get("result"))
                    text = f"[mcp {label}]" + (f"\n{body}" if body else "")
                    emit(rec, "tool", "tool_result", text, {"files": [], "commands": []},
                         is_user=False, tool_call_event_id=call_to_eid.get(cid) if isinstance(cid, str) else None)
        elif t == "response_item":
            if pt == "message":
                if pl.get("role") == "assistant":
                    text = _join_text(pl.get("content"))
                    if text.strip():
                        emit(rec, "assistant", "message", text, {"files": [], "commands": []}, is_user=False)
                # developer / user (bloated) messages: dropped
            elif pt == "agent_message":
                metadata = (
                    pending_inter_agent_metadata
                    if isinstance(pending_inter_agent_metadata, dict) else {}
                )
                refs = {
                    "files": [],
                    "commands": [],
                    "inter_agent": {
                        "author": pl.get("author"),
                        "recipient": pl.get("recipient"),
                        "trigger_turn": metadata.get("trigger_turn"),
                    },
                }
                emit(rec, "assistant", "message", _agent_message_text(pl), refs,
                     is_user=False, starts_turn=True)
                pending_inter_agent_metadata = None
            elif pt in ("function_call", "custom_tool_call"):
                text, refs = _render_tool_call(pt, pl.get("name") or "?", pl)
                eid = emit(rec, "assistant", "tool_call", text, refs, is_user=False)
                cid = pl.get("call_id")
                if isinstance(cid, str):
                    call_to_eid[cid] = eid
            elif pt in ("function_call_output", "custom_tool_call_output"):
                cid = pl.get("call_id")
                paired = call_to_eid.get(cid) if isinstance(cid, str) else None
                emit(rec, "tool", "tool_result", _result_text(pl), {"files": [], "commands": []},
                     is_user=False, tool_call_event_id=paired)
            # reasoning / web_search_* / tool_search_*: dropped (kept in raw pool)
        elif t == "inter_agent_communication":
            refs = {
                "files": [],
                "commands": [],
                "inter_agent": {
                    "author": pl.get("author"),
                    "recipient": pl.get("recipient"),
                    "trigger_turn": pl.get("trigger_turn"),
                },
            }
            text = pl.get("content") if isinstance(pl.get("content"), str) else ""
            if not text.strip():
                text = _agent_message_text({
                    "author": pl.get("author"),
                    "recipient": pl.get("recipient"),
                    "content": [],
                })
            emit(rec, "assistant", "message", text, refs,
                 is_user=False, starts_turn=True)
        # session_meta / turn_context / compacted: not events

    if not events:
        return None

    # Enrich tool_call refs with patch_apply_end's absolute paths (optional, by call_id).
    for cid, changes in patch_changes.items():
        e = by_eid.get(call_to_eid.get(cid, ""))
        if e is not None:
            have = set(e.refs.get("files", []))
            for fp in changes:
                if fp not in have:
                    e.refs.setdefault("files", []).append(fp)

    live = {eid for tn in turns if tn["live"] for eid in tn["events"]}
    order = sorted(events, key=lambda e: meta[e.event_id]["line"])
    # tip = the live tip after replay. None ⇒ the whole session was rolled back: a
    # legitimately empty live branch (SCHEMA allows a NULL tip). Do NOT fall back to
    # the last physical line — that event is dead, and marking it live would create a
    # live orphan whose parent is live=0.
    tip = state["tip"]
    if tip is not None:
        live.add(tip)

    placements = [
        PlacementRow(session_id=sid, event_id=e.event_id, seq=seq,
                     parent_event_id=parent_of[e.event_id],
                     live=1 if e.event_id in live else 0, inherited=0)
        for seq, e in enumerate(order)
    ]

    all_ts = [m["ts"] for m in meta.values() if m["ts"]]
    session = SessionRow(
        session_id=sid, source=SOURCE, machine=machine, cwd=cwd, repo=repo,
        created_at=created_at, started_at=min(all_ts) if all_ts else created_at,
        ended_at=max(all_ts) if all_ts else None,
        parent_session_id=f"{SOURCE}:{parent_uuid}" if parent_uuid else None,
        relation=relation, tip_event_id=tip, title=title,
    )
    return ParsedSession(session=session, events=events, placements=placements)

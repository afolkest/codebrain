"""Codex adapter — flat append-only rollout log (see formats/codex.md, SCHEMA.md).

Codex has no native parent tree; we **synthesize** one (SCHEMA.md "Linearization"):

  - Turns are anchored on the clean ``event_msg.user_message``. We deliberately do
    NOT anchor on ``task_started`` — the oldest logs (cli 0.39, 2025-09) have no
    ``task_started``/``task_complete`` at all, while every version has the clean
    user prompt. A turn = a user prompt + the emitted events that follow it.
    Structured inter-agent messages can also start a userless turn when Codex
    marks them trigger_turn; they are non-human input, never actor=user.
  - Within a turn, emitted events chain linearly (each parents the previous one).
  - ``thread_rolled_back{num_turns:n}`` pops the last ``n`` *live* user-turns; they
    survive as dead side branches (live=0) and the next turn parents to the reverted
    live tip — the linear-log equivalent of excluding Claude's abandoned subtree.

Two parallel in-file views (formats/codex.md "Dedup"): the clean human prompt comes
from ``event_msg.user_message``; assistant text + tool calls/results come from the
``response_item`` backbone. Never both — no double count. Dropped (kept in raw only):
reasoning (encrypted in current versions), injected ``developer``/bloated-``user``
``response_item`` messages, and pure telemetry (``token_count``, streamed
``event_msg.agent_message``…).

Resume re-emits ``session_meta`` with the same id in the same file → one session,
no dedup. Compaction appends a ``compacted`` marker and continues linearly → handled
for free (the marker is skipped; the next turn continues the live chain).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from codebrain.adapters.base import EventRow, ParsedSession, PlacementRow, SessionRow, read_records

SOURCE = "codex"

# Command-execution tools across versions (formats/codex.md "Tools & commands").
CMD_TOOLS = {"shell", "shell_command", "exec_command", "local_shell", "container.exec"}


def _uuid_from_filename(stem: str) -> str:
    # rollout-<localTs>-<uuid>; the uuid is the trailing 8-4-4-4-12 groups.
    parts = stem.split("-")
    return "-".join(parts[-5:]) if len(parts) >= 5 else stem


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


def parse_file(path: Path, machine: Optional[str] = None, title: Optional[str] = None) -> Optional[ParsedSession]:
    records = read_records(path)
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
                     is_user=False, starts_turn=bool(metadata.get("trigger_turn")))
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

"""Claude Code adapter — main transcript path (see formats/claude.md, SCHEMA.md).

Sub-agents (`<sessionId>/subagents/*.jsonl`) are deferred to a later slice;
their inline `isSidechain:true` copies in the parent are ignored here.

Linearization (SCHEMA.md):
  - Emit user(prompt) / user(tool_result) / assistant(text) / assistant(tool_use).
    Drop thinking (reasoning), attachments, system markers, sidecars.
  - parent_event_id = nearest *emitted* ancestor via bridged parentUuid
    (skips dropped nodes; dangling -> root).
  - tip = childless emitted event with max (ts, line). live = ancestors of tip,
    plus the paired tool_result of any live tool_call (parallel-tool fix).
  - tool_result pairing recorded on events.tool_call_event_id (not parent).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from codebrain.adapters.base import EventRow, ParsedSession, PlacementRow, SessionRow

SOURCE = "claude"


def _eid(uuid: str) -> str:
    return f"{SOURCE}:{uuid}"


def _content_blocks(message: dict):
    c = message.get("content")
    if isinstance(c, str):
        return [{"type": "_string", "text": c}]
    if isinstance(c, list):
        return c
    return []


def _render_tool_call(name: str, inp: dict):
    """(text, refs) for an assistant tool_use block."""
    files, commands = [], []
    for k in ("file_path", "notebook_path", "path"):
        v = inp.get(k)
        if isinstance(v, str):
            files.append(v)
    if name == "Bash" and isinstance(inp.get("command"), str):
        commands.append(inp["command"])
    if commands:
        text = f"{name}: {commands[0]}"
    elif files:
        text = f"{name}: {files[0]}"
    else:
        text = f"{name}: {json.dumps(inp, ensure_ascii=False)[:200]}"
    return text, {"files": files, "commands": commands}


def _render_tool_result(block: dict, rec: dict):
    """(text, refs) for a user tool_result block."""
    content = block.get("content")
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        text = "\n".join(p for p in parts if p)
    else:
        text = content if isinstance(content, str) else ""
    files = []
    tur = rec.get("toolUseResult")
    if isinstance(tur, dict) and isinstance(tur.get("filePath"), str):
        files.append(tur["filePath"])
    return text, {"files": files, "commands": []}


def parse_file(path: Path, machine: Optional[str] = None) -> Optional[ParsedSession]:
    records = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # tolerate a partial trailing line in an active session
            if isinstance(rec, dict):
                rec["_line"] = i
                records.append(rec)
    if not records:
        return None

    by_uuid = {r["uuid"]: r for r in records if isinstance(r.get("uuid"), str)}

    session_id = None
    cwd = None
    title = None
    title_ts = ""
    for r in records:
        if session_id is None and isinstance(r.get("sessionId"), str):
            session_id = r["sessionId"]
        if cwd is None and isinstance(r.get("cwd"), str):
            cwd = r["cwd"]
        if r.get("type") == "ai-title":
            t = r.get("aiTitle") or r.get("title")
            ts = r.get("timestamp", "")
            if isinstance(t, str) and ts >= title_ts:
                title, title_ts = t, ts
    if session_id is None:
        session_id = path.stem
    sid = f"{SOURCE}:{session_id}"

    # ---- emit events; track per-record emitted ids (block order) ----
    events: list[EventRow] = []
    meta: dict[str, dict] = {}          # event_id -> {uuid, parent_uuid, line, idx, ts}
    record_emitted: dict[str, list[str]] = {}   # uuid -> [event_id, ...]
    tooluse_by_callid: dict[str, str] = {}       # tool_use_id -> tool_call event_id
    result_by_callid: dict[str, str] = {}        # tool_use_id -> tool_result event_id

    def emit(rec, eid, actor, typ, text, refs, idx, tool_call_event_id=None):
        events.append(EventRow(
            event_id=eid, ts=rec.get("timestamp", ""), actor=actor, type=typ,
            text=text, refs=refs, raw={k: v for k, v in rec.items() if k != "_line"},
            tool_call_event_id=tool_call_event_id, origin_session_id=sid,
        ))
        meta[eid] = {"uuid": rec.get("uuid"), "parent_uuid": rec.get("parentUuid"),
                     "line": rec["_line"], "idx": idx, "ts": rec.get("timestamp", "")}
        record_emitted.setdefault(rec.get("uuid"), []).append(eid)

    seen_uuids: set[str] = set()
    for rec in records:
        # Skip sub-agent inline copies — their canonical home is the sub-agent file.
        if rec.get("isSidechain"):
            continue
        uuid = rec.get("uuid")
        if not isinstance(uuid, str):
            continue
        # Dedup re-emitted records (resume rewrites prior history with identical
        # uuid+content; verified by the copy-consistency check). Keep the first.
        if uuid in seen_uuids:
            continue
        seen_uuids.add(uuid)
        rtype = rec.get("type")
        msg = rec.get("message") or {}
        blocks = _content_blocks(msg)
        emitted_here = [b for b in blocks if _is_emitted_block(rtype, b)]
        multi = len(emitted_here) > 1
        idx = 0
        for b in blocks:
            btype = b.get("type")
            if rtype == "assistant" and btype == "text":
                eid = _eid(uuid) if not multi else f"{_eid(uuid)}:b{idx}"
                emit(rec, eid, "assistant", "message", b.get("text", ""), {"files": [], "commands": []}, idx)
                idx += 1
            elif rtype == "assistant" and btype == "tool_use":
                disc = b.get("id") or f"b{idx}"
                eid = _eid(uuid) if not multi else f"{_eid(uuid)}:{disc}"
                inp = b.get("input")
                text, refs = _render_tool_call(b.get("name", "?"), inp if isinstance(inp, dict) else {})
                emit(rec, eid, "assistant", "tool_call", text, refs, idx)
                if isinstance(b.get("id"), str):
                    tooluse_by_callid[b["id"]] = eid
                idx += 1
            elif rtype == "user" and btype == "_string":
                eid = _eid(uuid)
                emit(rec, eid, "user", "message", b.get("text", ""), {"files": [], "commands": []}, idx)
                idx += 1
            elif rtype == "user" and btype == "tool_result":
                tuid = b.get("tool_use_id", "")
                eid = _eid(uuid) if not multi else f"{_eid(uuid)}:{tuid}"
                text, refs = _render_tool_result(b, rec)
                emit(rec, eid, "tool", "tool_result", text, refs, idx,
                     tool_call_event_id=_eid(tuid) if tuid else None)
                if tuid:
                    result_by_callid[tuid] = eid
                idx += 1
            # thinking / images / other → dropped

    if not events:
        return None

    # Resolve tool_result -> tool_call to the actual (possibly discriminated) event id.
    for e in events:
        if e.type != "tool_result":
            continue
        for b in _content_blocks(e.raw.get("message", {})):
            if b.get("type") == "tool_result" and b.get("tool_use_id"):
                e.tool_call_event_id = tooluse_by_callid.get(b["tool_use_id"], e.tool_call_event_id)
                break

    # ---- bridged parent_event_id ----
    def bridged_parent(uuid: str) -> Optional[str]:
        p = by_uuid.get(uuid, {}).get("parentUuid")
        seen = set()
        while isinstance(p, str) and p not in seen:
            seen.add(p)
            if p in record_emitted and record_emitted[p]:
                return record_emitted[p][-1]
            p = by_uuid.get(p, {}).get("parentUuid")
        return None

    # Transcript order + each event's line-order predecessor. Used to reconnect
    # compaction components: post-compaction records parent off the dropped
    # `compact_boundary` (a null-parent 2nd root), so a bridged-None event that
    # has a prior emitted event links to it — stitching pre/post-compaction into
    # one live timeline (the very first prompt has no predecessor → stays a root).
    order = sorted(events, key=lambda e: (meta[e.event_id]["line"], meta[e.event_id]["idx"]))
    prev_in_order: dict[str, Optional[str]] = {}
    for i, e in enumerate(order):
        prev_in_order[e.event_id] = order[i - 1].event_id if i > 0 else None

    parent_of: dict[str, Optional[str]] = {}
    for e in events:
        m = meta[e.event_id]
        if m["idx"] > 0:
            siblings = record_emitted[m["uuid"]]
            parent_of[e.event_id] = siblings[siblings.index(e.event_id) - 1]
        else:
            bp = bridged_parent(m["uuid"])
            parent_of[e.event_id] = bp if bp is not None else prev_in_order[e.event_id]

    # ---- tip = childless emitted event, max (ts, line, idx) ----
    has_child = set(p for p in parent_of.values() if p)
    leaves = [e.event_id for e in events if e.event_id not in has_child]
    tip = max(leaves, key=lambda eid: (meta[eid]["ts"], meta[eid]["line"], meta[eid]["idx"]))

    # ---- live set: ancestors of tip + paired results of live tool_calls ----
    live = set()
    cur: Optional[str] = tip
    seen = set()
    while cur and cur not in seen:
        seen.add(cur)
        live.add(cur)
        cur = parent_of.get(cur)
    # attach paired tool_result for every live tool_call (parallel-tool fix)
    callid_by_eid = {v: k for k, v in tooluse_by_callid.items()}
    for eid in list(live):
        callid = callid_by_eid.get(eid)
        if callid and callid in result_by_callid:
            live.add(result_by_callid[callid])

    # ---- seq by transcript order; placements ----
    placements = [
        PlacementRow(
            session_id=sid, event_id=e.event_id, seq=seq,
            parent_event_id=parent_of[e.event_id],
            live=1 if e.event_id in live else 0, inherited=0,
        )
        for seq, e in enumerate(order)
    ]

    all_ts = [m["ts"] for m in meta.values() if m["ts"]]
    started = min(all_ts) if all_ts else None
    ended = max(all_ts) if all_ts else None
    session = SessionRow(
        session_id=sid, source=SOURCE, machine=machine, cwd=cwd,
        created_at=started, started_at=started, ended_at=ended,
        tip_event_id=tip, title=title,
    )
    return ParsedSession(session=session, events=events, placements=placements)


def _is_emitted_block(rtype: str, b: dict) -> bool:
    bt = b.get("type")
    if rtype == "assistant":
        return bt in ("text", "tool_use")
    if rtype == "user":
        return bt in ("_string", "tool_result")
    return False

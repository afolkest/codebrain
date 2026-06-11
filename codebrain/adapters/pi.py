"""pi adapter — parentId tree with cross-file inherited prefixes (see formats/pi.md, SCHEMA.md).

Structurally close to the Claude adapter (bridged-parent linearization + tip-ancestry
liveness), with pi's own shapes and the one genuinely new mechanism — resume/branch:

  - Tree via ``parentId`` (8-hex short ids). Control records (``model_change``,
    ``thinking_level_change``, ``compaction``, ``custom_message``) sit in the same
    chain but emit nothing → we bridge over them, exactly like dropped reasoning.
  - One assistant *record* can be multi-block (``text`` + N ``toolCall``); each block
    becomes its own event, chained in block order. Reasoning (``thinking``) and inline
    ``image`` blocks are dropped.
  - Forks are rollback-only (parallel tool calls chain, never fork), so liveness is
    simply: walk parents from the latest-ts childless event; its ancestors are live.

Resume/branch (the hard part, formats/pi.md): pi copies the parent's then-live prefix
**verbatim** into a NEW file — same 8-hex ids, same timestamps — then continues. So:

  - ``event_id = pi:<8hex>:<ts>`` is **copy-invariant** (the copied prefix dedups to the
    parent's ``events`` rows; no lineage pre-scan needed — see SCHEMA.md "ID scheme").
  - A placement is ``inherited`` iff ``event.ts < session.created_at`` (the copied prefix
    was authored before this session began); the origin is the one session that authored
    it (``inherited=0``), so we stamp ``origin_session_id`` only on authored events.
  - ``parent_session_id`` comes from the ``parentSession`` **filename uuid** (the local
    path won't survive Syncthing); ``branch_point`` = the highest-seq inherited event.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from codebrain.adapters.base import EventRow, ParsedSession, PlacementRow, SessionRow, read_records

SOURCE = "pi"


def _uuid_from_filename(name: str) -> str:
    # "<localTs>_<uuid>(.jsonl)"; the uuid is the last underscore-separated token.
    stem = name[:-6] if name.endswith(".jsonl") else name
    return stem.split("_")[-1]


def _render_tool_call(name: str, args):
    """(text, refs) for an assistant toolCall block (pi args are already a dict)."""
    args = args if isinstance(args, dict) else {}
    files, commands = [], []
    if isinstance(args.get("path"), str):
        files.append(args["path"])
    if name == "bash" and isinstance(args.get("command"), str):
        commands.append(args["command"])
    if commands:
        text = f"{name}: {commands[0]}"
    elif files:
        text = f"{name}: {files[0]}"
    else:
        text = f"{name}: {json.dumps(args, ensure_ascii=False)[:160]}"
    return text, {"files": files, "commands": commands}


def _render_tool_result(msg: dict):
    content = msg.get("content")
    parts, has_image = [], False
    if isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                parts.append(b.get("text", ""))
            elif b.get("type") == "image":
                has_image = True
    elif isinstance(content, str):
        parts.append(content)
    text = "\n".join(p for p in parts if p)
    if not text and has_image:
        text = "[image]"
    return text, {"files": [], "commands": []}


def parse_file(path: Path, machine: Optional[str] = None) -> Optional[ParsedSession]:
    records = read_records(path)
    if not records:
        return None

    sess = next((r for r in records if r.get("type") == "session"), None)
    session_uuid = (sess or {}).get("id") or _uuid_from_filename(path.name)
    sid = f"{SOURCE}:{session_uuid}"
    created_at = (sess or {}).get("timestamp") or ""
    cwd = (sess or {}).get("cwd")
    parent_session = (sess or {}).get("parentSession")
    parent_sid = relation = None
    if isinstance(parent_session, str) and parent_session:
        parent_sid = f"{SOURCE}:{_uuid_from_filename(Path(parent_session).name)}"
        relation = "branch"

    by_id = {r["id"]: r for r in records if isinstance(r.get("id"), str)}

    # ---- emit events (skip control records; bridge over them later) ----
    events: list[EventRow] = []
    meta: dict[str, dict] = {}                  # eid -> {rid, line, idx, ts}
    record_emitted: dict[str, list] = {}        # 8hex record id -> [eid, ...] (block order)
    call_to_eid: dict[str, str] = {}            # toolCall.id -> tool_call eid
    tool_name_by_eid: dict[str, str] = {}       # tool_call eid -> structured tool name
    result_tcid: dict[str, str] = {}            # tool_result eid -> toolCallId (resolve later)

    def emit(rec, eid, rid, actor, typ, text, refs, idx, tool_call_event_id=None):
        ts = rec.get("timestamp", "")
        # Authored here unless copied from a parent (ts before this session began).
        # No session record (created_at == "") ⇒ no inherited prefix ⇒ all authored.
        authored = (not created_at) or (ts >= created_at)
        events.append(EventRow(
            event_id=eid, ts=ts, actor=actor, type=typ, text=text, refs=refs,
            raw={k: v for k, v in rec.items() if k != "_line"},
            tool_call_event_id=tool_call_event_id,
            origin_session_id=sid if authored else None,
        ))
        meta[eid] = {"rid": rid, "line": rec["_line"], "idx": idx, "ts": ts}
        record_emitted.setdefault(rid, []).append(eid)

    for rec in records:
        if rec.get("type") != "message":
            continue
        m = rec.get("message")
        m = m if isinstance(m, dict) else {}   # tolerate a non-dict message (format drift)
        role = m.get("role")
        rid = rec.get("id")
        if not isinstance(rid, str):
            rid = f"L{rec['_line']}"   # synthetic, file-local: keeps event_ids unique
        ts = rec.get("timestamp", "")
        content = m.get("content")
        blocks = content if isinstance(content, list) else []
        idx = 0
        if role == "user":
            texts = [b for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
            multi = len(texts) > 1
            for j, b in enumerate(texts):
                eid = f"{SOURCE}:{rid}:{ts}" + (f":t{j}" if multi else "")
                emit(rec, eid, rid, "user", "message", b.get("text", ""), {"files": [], "commands": []}, idx)
                idx += 1
        elif role == "assistant":
            ntext = sum(1 for b in blocks if isinstance(b, dict) and b.get("type") == "text")
            tj = 0
            for b in blocks:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    eid = f"{SOURCE}:{rid}:{ts}" + (f":t{tj}" if ntext > 1 else "")
                    tj += 1
                    emit(rec, eid, rid, "assistant", "message", b.get("text", ""), {"files": [], "commands": []}, idx)
                    idx += 1
                elif bt == "toolCall":
                    cid = b.get("id") or f"b{idx}"
                    eid = f"{SOURCE}:{rid}:{ts}:{cid}"
                    name = b.get("name") or "?"
                    text, refs = _render_tool_call(name, b.get("arguments"))
                    emit(rec, eid, rid, "assistant", "tool_call", text, refs, idx)
                    tool_name_by_eid[eid] = name
                    if isinstance(b.get("id"), str):
                        call_to_eid[b["id"]] = eid
                    idx += 1
                # thinking / image: dropped
        elif role == "toolResult":
            eid = f"{SOURCE}:{rid}:{ts}"
            text, refs = _render_tool_result(m)
            emit(rec, eid, rid, "tool", "tool_result", text, refs, idx)
            if isinstance(m.get("toolCallId"), str):
                result_tcid[eid] = m["toolCallId"]
            idx += 1

    if not events:
        return None

    for eid, tcid in result_tcid.items():           # resolve tool_result -> tool_call
        by = next((e for e in events if e.event_id == eid), None)
        if by is not None:
            by.tool_call_event_id = call_to_eid.get(tcid)

    # ---- bridged parent (skip non-emitting control/reasoning records) ----
    def bridged_parent(rid: str) -> Optional[str]:
        p = by_id.get(rid, {}).get("parentId")
        seen = set()
        while isinstance(p, str) and p not in seen:
            seen.add(p)
            if record_emitted.get(p):
                return record_emitted[p][-1]
            p = by_id.get(p, {}).get("parentId")
        return None

    # parent = previous emitted block within the record, else the nearest emitted
    # ancestor record (bridged_parent). A dangling parent → None (root): unlike
    # Claude, pi compaction is parented INTO the tree, so there is no dropped null
    # root to stitch across — no line-predecessor fallback is needed here.
    parent_of: dict[str, Optional[str]] = {}
    for e in events:
        m = meta[e.event_id]
        siblings = record_emitted[m["rid"]]
        i = siblings.index(e.event_id)
        parent_of[e.event_id] = siblings[i - 1] if i > 0 else bridged_parent(m["rid"])

    # ---- tip = latest-ts childless emitted event; live = its ancestors ----
    has_child = {p for p in parent_of.values() if p}
    leaves = [e.event_id for e in events if e.event_id not in has_child] or [e.event_id for e in events]
    tip = max(leaves, key=lambda eid: (meta[eid]["ts"], meta[eid]["line"], meta[eid]["idx"]))
    live, cur, seen = set(), tip, set()
    while cur and cur not in seen:
        seen.add(cur)
        live.add(cur)
        cur = parent_of.get(cur)

    # ---- seq by transcript order; placements (inherited = authored before this session) ----
    order = sorted(events, key=lambda e: (meta[e.event_id]["line"], meta[e.event_id]["idx"]))
    placements = []
    branch_seq, branch_eid = -1, None
    for seq, e in enumerate(order):
        inh = 1 if (created_at and meta[e.event_id]["ts"] < created_at) else 0
        lv = 1 if e.event_id in live else 0
        placements.append(PlacementRow(
            session_id=sid, event_id=e.event_id, seq=seq,
            parent_event_id=parent_of[e.event_id], live=lv, inherited=inh,
        ))
        if inh and lv and seq > branch_seq:
            branch_seq, branch_eid = seq, e.event_id

    branch_point_event_id = branch_eid if parent_sid else None
    spawn_event_id = None
    if branch_point_event_id and tool_name_by_eid.get(branch_point_event_id) == "subagent":
        relation = "subagent"
        spawn_event_id = branch_point_event_id

    all_ts = [m["ts"] for m in meta.values() if m["ts"]]
    session = SessionRow(
        session_id=sid, source=SOURCE, machine=machine, cwd=cwd,
        created_at=created_at or None, started_at=min(all_ts) if all_ts else None,
        ended_at=max(all_ts) if all_ts else None,
        parent_session_id=parent_sid, relation=relation,
        spawn_event_id=spawn_event_id,
        branch_point_event_id=branch_point_event_id,
        tip_event_id=tip, title=None,
    )
    return ParsedSession(session=session, events=events, placements=placements)

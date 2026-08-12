"""Codex control-message provenance overlay.

Some Codex control surfaces deliver text into another Codex thread as ordinary
native ``user_message`` transcript rows. This module reconstructs those sends
from structured sender-side tool calls and marks the receiver-side user message
as non-human when the target thread, exact prompt hash, and timestamp ordering
line up. It never classifies from prompt wording.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from codebrain import provenance

EVIDENCE_KIND = "codex_control"

# Bump whenever ANY part of the derivation changes — submission extraction
# (tool sets, payload/target parsing, evidence ids) OR matching/verdict logic
# (windows, ambiguity rules, classification). This INCLUDES adapters/codex.py
# changes that alter the stored raw shape of existing events: a re-parse
# rewrites those rows in place below the rowid watermark, so only a version
# bump (or force) makes extraction revisit them. A stored-version mismatch
# forces one full mirror rebuild + rematch under the new logic.
DERIVATION_VERSION = 1

# Codex control sends can be queued behind an active turn, unlike bmux terminal
# paste. A wider window is acceptable because matching still requires target
# thread + exact prompt hash, and duplicate candidates degrade to unknown.
DEFAULT_WINDOW_SEC = 6 * 60 * 60
NEG_SKEW_SEC = 5
STATE_PATH = "__codebrain_codex_control_provenance__"

MCP_REPLY_TOOLS = {"codex-reply", "codex_reply"}
MCP_START_TOOLS = {"codex"}
FUNCTION_SEND_TOOLS = {"send_input"}
MULTI_AGENT_V1_NAMESPACE = "multi_agent_v1"


def _empty_stats(**extra) -> dict:
    base = {"submissions": 0, "stored": 0, "resolved": 0, "unresolved": 0,
            "master_control": 0, "unknown": 0, "skipped": 0}
    base.update(extra)
    return base


def _watermark(conn: sqlite3.Connection) -> int:
    """Highest events rowid; O(1) via the implicit rowid.

    Sound as an incremental cursor because this codebase never deletes events
    (auto rowids therefore stay monotonic) and codex event ``raw`` is immutable
    after insert: rollout files are append-only, so re-parsing a grown file
    re-upserts identical raw in place without changing the rowid. A VACUUM can
    renumber implicit rowids and break both properties; ``sessdb
    codex-control-sync`` (force) rebuilds from scratch and recovers.
    """
    return int(conn.execute(
        "SELECT COALESCE(MAX(rowid), 0) FROM events"
    ).fetchone()[0])


def _state(conn: sqlite3.Connection):
    # Stored as (mtime=events watermark, size=-DERIVATION_VERSION). The size is
    # NEGATED as the format discriminator: the previous format stored (max
    # codex rowid, codex event count) in the same row, and a count CAN equal a
    # small positive version (a one-event DB stored (1.0, 1)) — but it can
    # never be negative, so legacy rows always fail the version check in
    # sync() and force one full rebuild. No migration.
    row = conn.execute(
        "SELECT mtime, size FROM ingest_state WHERE path = ?", (STATE_PATH,)
    ).fetchone()
    return (row["mtime"], row["size"]) if row else None


def _parse_json(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _normalize_codex_session_id(value):
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    return value if value.startswith("codex:") else f"codex:{value}"


def _evidence_id(row, call_id) -> str:
    """Globally unique evidence id; Codex call_id is only per sender thread."""
    return f"{row['event_id']}:{call_id}" if isinstance(call_id, str) and call_id else row["event_id"]


def _payload_text(args: dict) -> str:
    message = args.get("prompt")
    if isinstance(message, str):
        return message
    message = args.get("message")
    if isinstance(message, str):
        return message
    parts = []
    items = args.get("items")
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
    return "".join(parts)


def _result_thread_id(result) -> str | None:
    result = _parse_json(result)
    ok = result.get("Ok")
    if isinstance(ok, dict):
        sc = ok.get("structuredContent")
        if isinstance(sc, dict):
            tid = sc.get("threadId") or sc.get("conversationId")
            if isinstance(tid, str):
                return tid
        for block in ok.get("content") or []:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if not isinstance(text, str):
                continue
            parsed = _parse_json(text)
            tid = parsed.get("threadId") or parsed.get("conversationId")
            if isinstance(tid, str):
                return tid
    return None


def _duration(payload: dict) -> timedelta:
    d = payload.get("duration")
    if not isinstance(d, dict):
        return timedelta()
    secs = d.get("secs") or 0
    nanos = d.get("nanos") or 0
    try:
        return timedelta(seconds=float(secs), microseconds=float(nanos) / 1000)
    except (TypeError, ValueError):
        return timedelta()


def _submitted_at(rec: dict, payload: dict, *, subtract_duration: bool) -> str:
    ts = rec.get("timestamp") or ""
    if not subtract_duration:
        return ts
    dt = provenance.to_dt(ts)
    if dt is None:
        return ts
    return (dt - _duration(payload)).isoformat().replace("+00:00", "Z")


def _submission_from_mcp(row) -> dict | None:
    raw = _parse_json(row["raw"])
    if raw.get("type") != "event_msg":
        return None
    payload = raw.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "mcp_tool_call_end":
        return None
    inv = payload.get("invocation")
    if not isinstance(inv, dict):
        return None
    tool = inv.get("tool")
    args = inv.get("arguments")
    if not isinstance(tool, str) or not isinstance(args, dict):
        return None

    if tool in MCP_REPLY_TOOLS:
        target = args.get("threadId") or args.get("conversationId")
        kind = "codex.mcp.codex-reply"
        resolved_via = "invocation.arguments.threadId"
    elif tool in MCP_START_TOOLS:
        target = _result_thread_id(payload.get("result"))
        kind = "codex.mcp.codex"
        resolved_via = "result.Ok.structuredContent.threadId"
    else:
        return None

    text = _payload_text(args)
    if not text:
        return None
    body = text.encode("utf-8")
    call_id = payload.get("call_id")
    return {
        "evidence_id": _evidence_id(row, call_id),
        "kind": kind,
        "submitted_at": _submitted_at(raw, payload, subtract_duration=True),
        "sender_session_id": row["origin_session_id"],
        "target_session_id": _normalize_codex_session_id(target),
        "payload_sha256": provenance.sha256_text(text),
        "payload_byte_count": len(body),
        "payload_line_count": text.count("\n") + 1,
        "resolved_via": resolved_via,
        "raw_event": json.dumps(raw, ensure_ascii=False),
    }


def _submission_from_function(row) -> dict | None:
    raw = _parse_json(row["raw"])
    if raw.get("type") != "response_item":
        return None
    payload = raw.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "function_call":
        return None
    name = payload.get("name")
    namespace = payload.get("namespace")
    if name not in FUNCTION_SEND_TOOLS:
        return None
    # Current Codex writes namespace="multi_agent_v1"; older transcripts predate
    # that field. Use the structured namespace when present, but do not lose
    # legacy sender-side evidence solely because the field is absent.
    if namespace is not None and namespace != MULTI_AGENT_V1_NAMESPACE:
        return None
    args = _parse_json(payload.get("arguments"))
    target = args.get("target")
    text = _payload_text(args)
    if not text:
        return None
    body = text.encode("utf-8")
    call_id = payload.get("call_id")
    return {
        "evidence_id": _evidence_id(row, call_id),
        "kind": f"codex.function.{name}",
        "submitted_at": _submitted_at(raw, payload, subtract_duration=False),
        "sender_session_id": row["origin_session_id"],
        "target_session_id": _normalize_codex_session_id(target),
        "payload_sha256": provenance.sha256_text(text),
        "payload_byte_count": len(body),
        "payload_line_count": text.count("\n") + 1,
        "resolved_via": "function_call.arguments.target",
        "raw_event": json.dumps(raw, ensure_ascii=False),
    }


def read_submissions(conn: sqlite3.Connection, min_rowid: int = 0) -> list[dict]:
    """Extract control submissions from codex events with rowid > min_rowid."""
    # The unary + hints keep the non-rowid terms off indexes: the planner
    # otherwise picks a MULTI-INDEX OR over ix_ev_actor_type_ts, which visits
    # every tool row in the DB and defeats the incremental rowid bound.
    rows = conn.execute(
        """
        SELECT event_id, origin_session_id, ts, raw
        FROM events
        WHERE rowid > ?
          AND +origin_session_id LIKE 'codex:%'
          AND ((+actor = 'tool' AND type = 'tool_result')
               OR (+actor = 'assistant' AND type = 'tool_call'))
          AND json_valid(raw)
          AND json_extract(raw, '$.type') IN ('event_msg', 'response_item')
        """,
        (min_rowid,),
    ).fetchall()
    out = []
    for row in rows:
        sub = _submission_from_mcp(row) or _submission_from_function(row)
        if sub is not None and sub["payload_sha256"] and sub["submitted_at"]:
            out.append(sub)
    return out


def _store_submissions(conn: sqlite3.Connection, subs: list[dict], *,
                       full: bool) -> int:
    if full:
        conn.execute("DELETE FROM codex_control_submissions")
    for sub in subs:
        conn.execute(
            """
            INSERT OR REPLACE INTO codex_control_submissions
              (evidence_id, kind, submitted_at, sender_session_id,
               target_session_id, payload_sha256, payload_byte_count,
               payload_line_count, resolved_via, raw_event)
            VALUES (:evidence_id, :kind, :submitted_at, :sender_session_id,
                    :target_session_id, :payload_sha256, :payload_byte_count,
                    :payload_line_count, :resolved_via, :raw_event)
            """,
            sub,
        )
    return len(subs)


def _load_submissions(conn: sqlite3.Connection) -> list:
    """Every mirrored submission, in deterministic order.

    Matching must run over the full mirror, never just newly extracted rows: a
    new receiver message can match an old submission, and a new duplicate
    submission must degrade a previously unique master_control verdict to
    unknown.
    """
    return conn.execute(
        """
        SELECT evidence_id, kind, submitted_at, target_session_id,
               payload_sha256
        FROM codex_control_submissions ORDER BY evidence_id
        """
    ).fetchall()


def sync(conn: sqlite3.Connection, window_sec: int = DEFAULT_WINDOW_SEC,
         changed_hint: bool = True, force: bool = False) -> dict:
    """Maintain the overlay: incremental extraction, full re-match.

    Submission extraction scans only events past the stored rowid watermark;
    verdicts always rebuild from the whole (tiny) submissions mirror.
    """
    # Watermark before extraction: rows landing between this read and the scan
    # are extracted now AND rescanned next run, which INSERT OR REPLACE keyed
    # on evidence_id makes idempotent.
    watermark = _watermark(conn)
    cur_state = (float(watermark), -DERIVATION_VERSION)
    stored = _state(conn)
    # The watermark comparison is authoritative, so changed_hint is
    # deliberately ignored: the hint fires on any transcript growth anywhere,
    # while the watermark detects exactly the event growth extraction depends
    # on. The parameter stays for API compatibility with the overlay hooks.
    # A non-default window changes verdict semantics without changing state,
    # so it must bypass the skip; rerun with the default window (or force) to
    # restore standard verdicts afterwards.
    if not force and window_sec == DEFAULT_WINDOW_SEC and stored == cur_state:
        return _empty_stats(skipped=1)

    full = force or stored is None or stored[1] != -DERIVATION_VERSION
    stats = _empty_stats()
    try:
        new_subs = read_submissions(conn, 0 if full else int(stored[0]))
        stats["submissions"] = len(new_subs)
        stats["stored"] = _store_submissions(conn, new_subs, full=full)
        subs = _load_submissions(conn)

        msg_cache: dict[str, list] = {}
        pairs = []
        degraded = set()
        for i, sub in enumerate(subs):
            sid = sub["target_session_id"]
            if not sid:
                stats["unresolved"] += 1
                continue
            stats["resolved"] += 1
            sub_dt = provenance.to_dt(sub["submitted_at"])
            if sid not in msg_cache:
                msg_cache[sid] = provenance.candidate_user_messages(conn, sid)
            for msg in msg_cache[sid]:
                if provenance.sha256_text(msg["text"]) != sub["payload_sha256"]:
                    continue
                msg_dt = provenance.to_dt(msg["ts"])
                if sub_dt is None or msg_dt is None:
                    degraded.add(msg["event_id"])
                    continue
                delta = (msg_dt - sub_dt).total_seconds()
                if delta < -NEG_SKEW_SEC or delta > window_sec:
                    continue
                pairs.append((i, msg["event_id"]))

        ev_subs: dict[str, set[int]] = {}
        sub_evs: dict[int, set[str]] = {}
        for sub_idx, eid in pairs:
            ev_subs.setdefault(eid, set()).add(sub_idx)
            sub_evs.setdefault(sub_idx, set()).add(eid)

        verdicts: dict[str, tuple[str, str | None, str]] = {}
        for eid, sub_idxs in ev_subs.items():
            unique = len(sub_idxs) == 1 and len(sub_evs[next(iter(sub_idxs))]) == 1
            if unique:
                sub = subs[next(iter(sub_idxs))]
                verdicts[eid] = (
                    "master_control",
                    sub["evidence_id"],
                    f"unique {sub['kind']} structured control-submission match",
                )
            else:
                verdicts[eid] = (
                    "unknown",
                    None,
                    "ambiguous Codex structured control-submission candidates",
                )
        for eid in degraded:
            verdicts[eid] = (
                "unknown",
                None,
                "Codex control payload hash match with unverifiable timestamp",
            )

        evidence_rows = []
        for eid, (origin, evidence_id, reason) in verdicts.items():
            stats["master_control" if origin == "master_control" else "unknown"] += 1
            for p in conn.execute(
                "SELECT session_id FROM session_events WHERE event_id = ?", (eid,)
            ).fetchall():
                evidence_rows.append({
                    "session_id": p["session_id"],
                    "event_id": eid,
                    "origin": origin,
                    "evidence_kind": EVIDENCE_KIND,
                    "evidence_id": evidence_id,
                    "reason": reason,
                })

        provenance.replace_evidence_kind(conn, EVIDENCE_KIND, evidence_rows)
        if window_sec == DEFAULT_WINDOW_SEC:
            conn.execute(
                "INSERT OR REPLACE INTO ingest_state (path, mtime, size, session_id) "
                "VALUES (?, ?, ?, NULL)",
                (STATE_PATH, float(watermark), -DERIVATION_VERSION),
            )
        else:
            # A custom window produced non-standard verdicts. Writing the normal
            # marker would let the next default-window sync skip right over
            # them; drop the marker instead so that sync must rematch.
            conn.execute("DELETE FROM ingest_state WHERE path = ?", (STATE_PATH,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return stats

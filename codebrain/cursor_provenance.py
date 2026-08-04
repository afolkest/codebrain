"""Structured origin evidence for Cursor user-message events.

Cursor records control-plane input as ordinary user bubbles.  This overlay uses
only source booleans, source IDs, canonical session lineage, and placement order;
message wording never participates in classification.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from codebrain import provenance


SIMULATED_KIND = "cursor_simulated"
PLAN_KIND = "cursor_plan_execution"
KICKOFF_KIND = "cursor_subagent_kickoff"
EVIDENCE_KINDS = (SIMULATED_KIND, PLAN_KIND, KICKOFF_KIND)
STATE_PATH = "__codebrain_cursor_provenance__"


def _empty_stats(**extra) -> dict:
    base = {
        "events": 0, "master_control": 0, "unknown": 0,
        "evidence": 0, "skipped": 0,
    }
    base.update(extra)
    return base


def _source_rows(conn: sqlite3.Connection) -> list:
    return conn.execute(
        """
        SELECT s.session_id, s.relation, s.parent_session_id,
               se.event_id, se.seq, se.live, se.inherited, e.raw
        FROM sessions s
        JOIN session_events se ON se.session_id = s.session_id
        JOIN events e ON e.event_id = se.event_id
        WHERE s.source = 'cursor' AND e.actor = 'user' AND e.type = 'message'
        ORDER BY s.session_id, se.seq, se.event_id
        """
    ).fetchall()


def _fingerprint(rows: list) -> str:
    digest = hashlib.sha256()
    for row in rows:
        value = tuple(row)
        encoded = json.dumps(
            value, ensure_ascii=True, separators=(",", ":")
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _state(conn: sqlite3.Connection):
    row = conn.execute(
        "SELECT session_id FROM ingest_state WHERE path = ?", (STATE_PATH,)
    ).fetchone()
    return row["session_id"] if row else None


def _parse_raw(value) -> dict:
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, RecursionError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def sync(conn: sqlite3.Connection, changed_hint: bool = True,
         force: bool = False) -> dict:
    rows = _source_rows(conn)
    fingerprint = _fingerprint(rows)
    if not force and not changed_hint and _state(conn) == fingerprint:
        return _empty_stats(skipped=1)

    stats = _empty_stats()
    try:
        raw_by_event = {}
        placements: dict[str, list[str]] = {}
        subagents: dict[str, list] = {}
        for row in rows:
            raw_by_event.setdefault(row["event_id"], _parse_raw(row["raw"]))
            placements.setdefault(row["event_id"], []).append(row["session_id"])
            if row["relation"] == "subagent" and row["parent_session_id"] \
                    and row["inherited"] == 0:
                subagents.setdefault(row["session_id"], []).append(row)
        stats["events"] = len(raw_by_event)

        evidence: dict[tuple[str, str, str], dict] = {}

        def mark(kind: str, event_id: str, origin: str, reason: str) -> None:
            if origin == "master_control":
                stats["master_control"] += 1
            else:
                stats["unknown"] += 1
            for session_id in placements.get(event_id, ()):
                evidence[(session_id, event_id, kind)] = {
                    "session_id": session_id,
                    "event_id": event_id,
                    "origin": origin,
                    "evidence_kind": kind,
                    "evidence_id": event_id if origin == "master_control" else None,
                    "reason": reason,
                }

        for event_id, raw in raw_by_event.items():
            simulated = raw.get("isSimulatedMsg")
            reason_code = raw.get("simulatedMsgReason")
            if simulated is True:
                mark(
                    SIMULATED_KIND, event_id, "master_control",
                    "Cursor structured isSimulatedMsg flag",
                )
            elif simulated is False and isinstance(reason_code, int) \
                    and not isinstance(reason_code, bool) and reason_code != 0:
                mark(
                    SIMULATED_KIND, event_id, "unknown",
                    "contradictory Cursor simulated-message fields",
                )
            if raw.get("isPlanExecution") is True:
                mark(
                    PLAN_KIND, event_id, "master_control",
                    "Cursor structured isPlanExecution flag",
                )
            kickoff_id = raw.get("subagentSpawnTaskToolCallId")
            if isinstance(kickoff_id, str) and kickoff_id.strip():
                mark(
                    KICKOFF_KIND, event_id, "master_control",
                    "Cursor structured subagent kickoff field",
                )

        for candidates in subagents.values():
            has_explicit = any(
                isinstance(
                    raw_by_event[row["event_id"]].get(
                        "subagentSpawnTaskToolCallId"
                    ), str,
                ) and raw_by_event[row["event_id"]][
                    "subagentSpawnTaskToolCallId"
                ].strip()
                for row in candidates
            )
            if not has_explicit:
                mark(
                    KICKOFF_KIND, candidates[0]["event_id"], "master_control",
                    "first authored input in structured Cursor subagent lineage",
                )

        for kind in EVIDENCE_KINDS:
            conn.execute(
                "DELETE FROM event_origin_evidence WHERE evidence_kind = ?", (kind,)
            )
        for row in evidence.values():
            provenance.record_origin_evidence(conn, row)
        provenance.rebuild_effective_origins(conn)
        stats["evidence"] = len(evidence)
        conn.execute(
            "INSERT OR REPLACE INTO ingest_state (path, mtime, size, session_id) "
            "VALUES (?, 0, ?, ?)",
            (STATE_PATH, len(rows), fingerprint),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return stats

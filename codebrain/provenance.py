"""Shared provenance helpers for non-human user-message overlays.

The canonical transcript tables stay source-native. Provenance derivers write
structured evidence here; the effective event_origins table is rebuilt from that
evidence so CLI joins still see at most one row per transcript placement.
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from typing import Optional

NON_HUMAN_ORIGINS = ("master_control", "unknown")

# A known control-plane match is stronger than an ambiguity marker. Ties are
# deterministic but otherwise not semantically meaningful.
_ORIGIN_PRECEDENCE = {"unknown": 0, "master_control": 1}


def to_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sha256_text(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def candidate_user_messages(conn: sqlite3.Connection, session_id: str) -> list:
    """Live, authored native user messages for one session."""
    return conn.execute(
        """
        SELECT event_id, ts, text FROM transcript
        WHERE session_id = ? AND actor = 'user' AND type = 'message'
              AND live = 1 AND inherited = 0 AND COALESCE(text, '') <> ''
        """,
        (session_id,),
    ).fetchall()


def record_origin_evidence(conn: sqlite3.Connection, row: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO event_origin_evidence
          (session_id, event_id, origin, evidence_kind, evidence_id, reason)
        VALUES (:session_id, :event_id, :origin, :evidence_kind, :evidence_id,
                :reason)
        """,
        row,
    )


def rebuild_effective_origins(conn: sqlite3.Connection) -> None:
    """Rebuild one effective origin row per placement from all evidence rows."""
    rows = conn.execute(
        """
        SELECT session_id, event_id, origin, evidence_kind, evidence_id, reason
        FROM event_origin_evidence
        WHERE origin IN ('master_control', 'unknown')
        """
    ).fetchall()
    best: dict[tuple[str, str], sqlite3.Row] = {}
    for row in rows:
        key = (row["session_id"], row["event_id"])
        old = best.get(key)
        if old is None or _rank(row) > _rank(old):
            best[key] = row

    conn.execute("DELETE FROM event_origins")
    conn.executemany(
        """
        INSERT INTO event_origins
          (session_id, event_id, origin, evidence_kind, evidence_id, reason)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            (r["session_id"], r["event_id"], r["origin"], r["evidence_kind"],
             r["evidence_id"], r["reason"])
            for r in best.values()
        ),
    )


def replace_evidence_kind(conn: sqlite3.Connection, evidence_kind: str,
                          rows: list[dict]) -> None:
    """Replace one deriver's evidence and refresh effective verdicts."""
    conn.execute("DELETE FROM event_origin_evidence WHERE evidence_kind = ?",
                 (evidence_kind,))
    for row in rows:
        record_origin_evidence(conn, row)
    rebuild_effective_origins(conn)


def _rank(row) -> tuple:
    return (
        _ORIGIN_PRECEDENCE.get(row["origin"], -1),
        row["evidence_kind"] or "",
        row["evidence_id"] or "",
        row["reason"] or "",
    )

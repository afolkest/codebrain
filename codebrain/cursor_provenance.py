"""Structured origin evidence for Cursor user-message events.

Cursor records control-plane input as ordinary user bubbles.  This overlay uses
only source booleans, source IDs, canonical session lineage, and placement order;
message wording never participates in classification.
"""
from __future__ import annotations

import json
import sqlite3

from codebrain import provenance


SIMULATED_KIND = "cursor_simulated"
PLAN_KIND = "cursor_plan_execution"
KICKOFF_KIND = "cursor_subagent_kickoff"
EVIDENCE_KINDS = (SIMULATED_KIND, PLAN_KIND, KICKOFF_KIND)
STATE_PATH = "__codebrain_cursor_provenance__"
# Participates in the stored algo marker: bump whenever classification logic
# changes so unchanged source data still gets exactly one full rebuild.
ALGO_VERSION = 1

_AFFECTED_TABLE = "temp._cursor_prov_affected"


def _algo_marker() -> str:
    # Computed per call (not at import) so a patched ALGO_VERSION — tests, hot
    # reload — is honoured. Can never collide with the fingerprint hex digests
    # earlier versions stored at STATE_PATH, so upgraded databases rebuild once.
    return f"per-session:{ALGO_VERSION}"


def _empty_stats(**extra) -> dict:
    base = {
        "events": 0, "master_control": 0, "unknown": 0,
        "evidence": 0, "skipped": 0, "rebuilt_sessions": 0,
    }
    base.update(extra)
    return base


def _source_rows(conn: sqlite3.Connection, restricted: bool = False) -> list:
    # CROSS JOIN pins the join order to sessions -> placements -> events (PK).
    # Left to itself the planner drives off ix_ev_actor_type_ts and visits every
    # user message of every source before filtering to cursor sessions — 2-3x
    # slower on a large multi-source cache (measured 12s vs 5s on 1.4M events).
    # When restricted, the scan drives off the (tiny) affected temp table.
    source = f"FROM {_AFFECTED_TABLE} a " \
             "CROSS JOIN sessions s ON s.session_id = a.session_id" \
        if restricted else "FROM sessions s"
    return conn.execute(
        f"""
        SELECT s.session_id, s.relation, s.parent_session_id,
               se.event_id, se.seq, se.live, se.inherited, e.raw
        {source}
        CROSS JOIN session_events se ON se.session_id = s.session_id
        CROSS JOIN events e ON e.event_id = se.event_id
        WHERE s.source = 'cursor' AND e.actor = 'user' AND e.type = 'message'
        ORDER BY s.session_id, se.seq, se.event_id
        """
    ).fetchall()


def _fill_affected(conn: sqlite3.Connection, changed: set) -> None:
    """Seed the temp table with the changed sessions, then close it over
    shared-user-message reach.  Evidence rows may target any session holding a
    placement of an affected event (inherited branch copies), and the subagent
    lineage rule of those sessions can reach further sessions in turn, so the
    rebuild scope is the transitive family, computed to a fixpoint.  Families
    are branch/subagent trees — a handful of sessions, not the whole corpus."""
    conn.execute(f"DROP TABLE IF EXISTS {_AFFECTED_TABLE}")
    conn.execute(
        f"CREATE TEMP TABLE {_AFFECTED_TABLE.split('.')[1]} "
        "(session_id TEXT PRIMARY KEY)"
    )
    conn.executemany(
        f"INSERT OR IGNORE INTO {_AFFECTED_TABLE} VALUES (?)",
        ((sid,) for sid in changed),
    )
    # Historical fan-out edge: a changed session's revision may have REMOVED
    # the event that justified evidence in other sessions (e.g. the subagent
    # kickoff fallback event, inherited into a branch), so the current
    # placement graph no longer connects them. mark() always fans out to the
    # session holding the marked event itself, and evidence rows are only ever
    # deleted by this function — so at this point a changed session still
    # holds its own copy of every our-kind row it justified. Pulling in every
    # session sharing an our-kind-evidenced EVENT with a changed session
    # therefore reaches all stale holders, with no reliance on
    # events.origin_session_id being populated. Runs once, over exactly the
    # changed seed, before the placement fixpoint.
    kinds = ", ".join("?" * len(EVIDENCE_KINDS))
    conn.execute(
        f"""
        INSERT OR IGNORE INTO {_AFFECTED_TABLE}
        SELECT DISTINCT other.session_id
        FROM {_AFFECTED_TABLE} a
        CROSS JOIN event_origin_evidence mine ON mine.session_id = a.session_id
             AND mine.evidence_kind IN ({kinds})
        CROSS JOIN event_origin_evidence other ON other.event_id = mine.event_id
             AND other.evidence_kind IN ({kinds})
        """,
        EVIDENCE_KINDS * 2,
    )
    while True:
        # CROSS JOIN pins the drive order to the (tiny) affected table; left to
        # itself the planner starts from ix_ev_actor_type_ts and walks every
        # user message in the cache (measured 13s vs 9ms on a 1.4M-event DB).
        # rowcount (sqlite3_changes) is the exact local sentinel — OR IGNOREd
        # duplicates don't count, and unlike total_changes it can't be
        # perturbed by unrelated writes on the connection.
        inserted = conn.execute(
            f"""
            INSERT OR IGNORE INTO {_AFFECTED_TABLE}
            SELECT DISTINCT se2.session_id
            FROM {_AFFECTED_TABLE} a
            CROSS JOIN session_events se1 ON se1.session_id = a.session_id
            CROSS JOIN events e ON e.event_id = se1.event_id
                 AND e.actor = 'user' AND e.type = 'message'
            CROSS JOIN session_events se2 ON se2.event_id = se1.event_id
            CROSS JOIN sessions s2 ON s2.session_id = se2.session_id
                 AND s2.source = 'cursor'
            """
        ).rowcount
        if inserted <= 0:
            break


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
    # Every accepted write of Cursor sessions/events/placements commits in the
    # same per-file transaction that advances that session's row in
    # cursor_session_heads (ingest._ingest / db.record_cursor_head), so a head
    # equal to its cursor_provenance_state row implies that session's
    # _source_rows() slice is unchanged.  Diffing the two tables keeps the
    # events.raw join off the read path and scopes rebuilds to the sessions
    # that actually moved (plus their shared-event family).
    # changed_hint is deliberately not consulted: the head watermark subsumes
    # it; the parameter remains for call-site compatibility (cli._open).
    # Heads are captured BEFORE _source_rows() and the captured values are what
    # gets recorded, so recorded state is never newer than the data the rebuild
    # read; a concurrent head advance can only cause one extra rebuild, never
    # a stale skip.
    heads = {
        row["session_id"]: (row["revision"], row["digest"])
        for row in conn.execute(
            "SELECT session_id, revision, digest FROM cursor_session_heads"
        )
    }
    full = force or _state(conn) != _algo_marker()
    changed: set = set()
    if not full:
        state = {
            row["session_id"]: (row["revision"], row["digest"])
            for row in conn.execute(
                "SELECT session_id, revision, digest FROM cursor_provenance_state"
            )
        }
        changed = {sid for sid, head in heads.items() if state.get(sid) != head}
        changed |= set(state) - set(heads)
        if not changed:
            return _empty_stats(skipped=1)

    stats = _empty_stats()
    try:
        if full:
            rows = _source_rows(conn)
        else:
            _fill_affected(conn, changed)
            rows = _source_rows(conn, restricted=True)
        raw_by_event = {}
        placements: dict[str, list[str]] = {}
        subagents: dict[str, list] = {}
        for row in rows:
            raw_by_event.setdefault(row["event_id"], _parse_raw(row["raw"]))
            placements.setdefault(row["event_id"], []).append(row["session_id"])
            if row["relation"] == "subagent" and row["parent_session_id"] \
                    and row["inherited"] == 0:
                subagents.setdefault(row["session_id"], []).append(row)
        # Per-run deltas over the rebuilt scope, not corpus totals (a forced or
        # migration rebuild covers everything, so cursor-provenance-sync still
        # prints totals).
        stats["events"] = len(raw_by_event)
        stats["rebuilt_sessions"] = len({row["session_id"] for row in rows})

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

        if full:
            for kind in EVIDENCE_KINDS:
                conn.execute(
                    "DELETE FROM event_origin_evidence WHERE evidence_kind = ?",
                    (kind,),
                )
            conn.execute("DELETE FROM cursor_provenance_state")
        else:
            # Every recomputed evidence row targets a session in the affected
            # closure, and every existing our-kind row of an affected session
            # was recomputed above — so delete exactly the closure's rows.
            for kind in EVIDENCE_KINDS:
                conn.execute(
                    "DELETE FROM event_origin_evidence WHERE evidence_kind = ? "
                    f"AND session_id IN (SELECT session_id FROM {_AFFECTED_TABLE})",
                    (kind,),
                )
            conn.executemany(
                "DELETE FROM cursor_provenance_state WHERE session_id = ?",
                [(sid,) for sid in changed - set(heads)],
            )
        for row in evidence.values():
            provenance.record_origin_evidence(conn, row)
        provenance.rebuild_effective_origins(conn)
        stats["evidence"] = len(evidence)
        recorded = heads if full else \
            {sid: heads[sid] for sid in changed if sid in heads}
        conn.executemany(
            "INSERT OR REPLACE INTO cursor_provenance_state "
            "(session_id, revision, digest) VALUES (?, ?, ?)",
            [(sid, rev, dig) for sid, (rev, dig) in recorded.items()],
        )
        conn.execute(
            "INSERT OR REPLACE INTO ingest_state (path, mtime, size, session_id) "
            "VALUES (?, 0, ?, ?)",
            (STATE_PATH, len(rows), _algo_marker()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.execute(f"DROP TABLE IF EXISTS {_AFFECTED_TABLE}")
        except sqlite3.Error:
            pass  # a dead/locked connection must not mask the real exception
    return stats

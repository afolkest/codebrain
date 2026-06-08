"""SQLite layer: schema (SCHEMA.md), connection, idempotent upserts.

The DB is a rebuildable cache (DESIGN.md) — drop it and re-ingest from raw.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from codebrain.adapters.base import EventRow, PlacementRow, SessionRow

DEFAULT_DB = Path(os.environ.get("CODEBRAIN_DB", Path.home() / ".codebrain" / "codebrain.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id            TEXT PRIMARY KEY,
  source                TEXT NOT NULL,
  machine               TEXT,
  cwd                   TEXT,
  repo                  TEXT,
  created_at            TEXT,
  started_at            TEXT,
  ended_at              TEXT,
  parent_session_id     TEXT,
  relation              TEXT,
  spawn_event_id        TEXT,
  branch_point_event_id TEXT,
  tip_event_id          TEXT,
  title                 TEXT
);

CREATE TABLE IF NOT EXISTS events (
  event_id            TEXT PRIMARY KEY,
  origin_session_id   TEXT,
  ts                  TEXT NOT NULL,
  actor               TEXT NOT NULL,
  type                TEXT NOT NULL,
  text                TEXT,
  refs                TEXT,          -- JSON {"files":[],"commands":[]}
  tool_call_event_id  TEXT,
  raw                 TEXT NOT NULL  -- JSON source record
);

CREATE TABLE IF NOT EXISTS session_events (
  session_id       TEXT NOT NULL,
  event_id         TEXT NOT NULL,
  seq              INTEGER NOT NULL,
  parent_event_id  TEXT,
  live             INTEGER NOT NULL,
  inherited        INTEGER NOT NULL,
  PRIMARY KEY (session_id, event_id)
);

CREATE INDEX IF NOT EXISTS ix_se_session_live_seq ON session_events(session_id, live, seq);
CREATE INDEX IF NOT EXISTS ix_se_event           ON session_events(event_id);
CREATE INDEX IF NOT EXISTS ix_se_parent          ON session_events(parent_event_id);
CREATE INDEX IF NOT EXISTS ix_ev_origin          ON events(origin_session_id);

CREATE VIEW IF NOT EXISTS transcript AS
  SELECT se.session_id, se.seq, e.event_id, e.ts, e.actor, e.type,
         e.text, e.refs, e.tool_call_event_id, se.parent_event_id,
         se.live, se.inherited
  FROM session_events se JOIN events e USING (event_id);
"""


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _ensure_fts(conn)
    return conn


def has_fts5(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _ensure_fts(conn: sqlite3.Connection) -> None:
    if has_fts5(conn):
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS events_fts "
            "USING fts5(text, event_id UNINDEXED)"
        )


# ---- idempotent upserts (re-ingest is a no-op; see SCHEMA.md Ingest contract) ----

def upsert_session(conn: sqlite3.Connection, s: SessionRow) -> None:
    conn.execute(
        """
        INSERT INTO sessions (session_id, source, machine, cwd, repo, created_at,
            started_at, ended_at, parent_session_id, relation, spawn_event_id,
            branch_point_event_id, tip_event_id, title)
        VALUES (:session_id, :source, :machine, :cwd, :repo, :created_at,
            :started_at, :ended_at, :parent_session_id, :relation, :spawn_event_id,
            :branch_point_event_id, :tip_event_id, :title)
        ON CONFLICT(session_id) DO UPDATE SET
            source=excluded.source, machine=excluded.machine, cwd=excluded.cwd,
            repo=excluded.repo, created_at=excluded.created_at,
            started_at=excluded.started_at, ended_at=excluded.ended_at,
            parent_session_id=excluded.parent_session_id, relation=excluded.relation,
            spawn_event_id=excluded.spawn_event_id,
            branch_point_event_id=excluded.branch_point_event_id,
            tip_event_id=excluded.tip_event_id, title=excluded.title
        """,
        s.__dict__,
    )


def upsert_event(conn: sqlite3.Connection, e: EventRow) -> bool:
    """Insert/refresh one event. Returns True if written, False if SKIPPED due to a
    copy-consistency conflict — an existing row with the same event_id but different
    actor/type/text (an id collision or a non-verbatim copy). On conflict we keep the
    first row untouched and flag it (SCHEMA.md "flag, don't merge"); the caller counts
    it and the rest of the session still commits. Skipping one event is strictly safer
    than aborting the whole file, and conflicts are near-impossible (ids are globally
    unique for claude/codex, copy-invariant for pi)."""
    row = conn.execute(
        "SELECT actor, type, text FROM events WHERE event_id=?", (e.event_id,)
    ).fetchone()
    if row is not None and (row["actor"], row["type"], row["text"]) != (e.actor, e.type, e.text):
        return False
    conn.execute(
        """
        INSERT INTO events (event_id, origin_session_id, ts, actor, type, text,
            refs, tool_call_event_id, raw)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(event_id) DO UPDATE SET
            -- keep the FIRST non-null origin: an event is authored by exactly one
            -- session, so a later (inherited) copy must not overwrite it.
            origin_session_id=COALESCE(origin_session_id, excluded.origin_session_id),
            ts=excluded.ts, actor=excluded.actor, type=excluded.type,
            text=excluded.text, refs=excluded.refs,
            tool_call_event_id=excluded.tool_call_event_id, raw=excluded.raw
        """,
        (e.event_id, e.origin_session_id, e.ts, e.actor, e.type, e.text,
         json.dumps(e.refs), e.tool_call_event_id, json.dumps(e.raw)),
    )
    return True


def upsert_placement(conn: sqlite3.Connection, p: PlacementRow) -> None:
    conn.execute(
        """
        INSERT INTO session_events (session_id, event_id, seq, parent_event_id, live, inherited)
        VALUES (:session_id, :event_id, :seq, :parent_event_id, :live, :inherited)
        ON CONFLICT(session_id, event_id) DO UPDATE SET
            seq=excluded.seq, parent_event_id=excluded.parent_event_id,
            live=excluded.live, inherited=excluded.inherited
        """,
        p.__dict__,
    )


def rebuild_fts(conn: sqlite3.Connection) -> None:
    if not has_fts5(conn):
        return
    conn.execute("DELETE FROM events_fts")
    conn.execute(
        "INSERT INTO events_fts (event_id, text) "
        "SELECT event_id, text FROM events WHERE text IS NOT NULL AND text <> ''"
    )

"""SQLite layer: schema (SCHEMA.md), connection, idempotent upserts.

The DB is a rebuildable cache (DESIGN.md) — drop it and re-ingest from raw.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

from codebrain.adapters.base import EventRow, PlacementRow, SessionRow
from codebrain.paths import path_basename

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
  title                 TEXT,
  hidden_at             TEXT,
  hidden_reason         TEXT
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
-- The intent-browsing commands (userlog/recent) filter user messages and sort by
-- time; without this they full-scan ~1M events. (actor, type, ts) lets the planner
-- read just the user/message slice in time order.
CREATE INDEX IF NOT EXISTS ix_ev_actor_type_ts   ON events(actor, type, ts);

-- Files index (a rebuildable derivation, NOT part of the canonical model in
-- SCHEMA.md): events.refs.files unrolled to one row per (event, file) so `touched`
-- is an indexed lookup instead of a json_array_length scan over every event. Keyed
-- on event_id (content, like events) — placement/visibility filters come from the
-- session_events join. basename is normalized identically to the query side
-- (codebrain.paths) so an indexed basename lookup never misses a real match.
CREATE TABLE IF NOT EXISTS file_refs (
  event_id  TEXT NOT NULL,
  file      TEXT NOT NULL,
  basename  TEXT NOT NULL,
  PRIMARY KEY (event_id, file)
);
-- (basename, event_id) covers the touched IN-subquery: the candidate event_ids
-- are read straight from the index without touching the table.
CREATE INDEX IF NOT EXISTS ix_file_refs_basename ON file_refs(basename, event_id);
CREATE INDEX IF NOT EXISTS ix_file_refs_file     ON file_refs(file);

-- Internal bookkeeping, NOT part of the canonical model (SCHEMA.md): the
-- (mtime, size) each raw file had when last parsed, so refresh() re-parses only
-- what changed. session_id is a debugging convenience (raw file <-> session).
CREATE TABLE IF NOT EXISTS ingest_state (
  path       TEXT PRIMARY KEY,
  mtime      REAL NOT NULL,
  size       INTEGER NOT NULL,
  session_id TEXT
);

-- bmux provenance overlay. NOT part of the canonical
-- transcript model: a rebuildable derivation of bmux's own event log, joined to
-- transcripts by resolved session + exact payload SHA-256. Drop and rebuild from
-- ~/.bmux/events/bmux.jsonl; never mutates events/session_events.
CREATE TABLE IF NOT EXISTS bmux_control_submissions (
  send_id              TEXT,
  launch_id            TEXT,
  kind                 TEXT NOT NULL,   -- bmux.send_submitted | bmux.launch_prompt_submitted
  submitted_at         TEXT NOT NULL,
  codebrain_session_id TEXT,            -- resolved (NULL if launch_id unresolved)
  payload_sha256       TEXT NOT NULL,
  payload_byte_count   INTEGER,
  payload_line_count   INTEGER,
  master_id            TEXT,
  resolved_via         TEXT,            -- which field produced the launch_id/session
  raw_event            TEXT NOT NULL,
  PRIMARY KEY (kind, send_id, launch_id, submitted_at)
);

-- Per (session, event) provenance verdict — single verdict per placement (PK is
-- (session_id, event_id)). Absence of a row == clean human input (the default
-- bucket); a row records the effective non-human classification selected from
-- event_origin_evidence by the provenance coordinator.
CREATE TABLE IF NOT EXISTS event_origins (
  session_id    TEXT NOT NULL,
  event_id      TEXT NOT NULL,
  origin        TEXT NOT NULL,          -- master_control | unknown | human
  evidence_kind TEXT,                   -- deriver that produced the winning row
  evidence_id   TEXT,                   -- send_id / launch_id of the matched event
  reason        TEXT,
  PRIMARY KEY (session_id, event_id)
);

-- Rebuildable provenance evidence. Multiple derivers can independently mark a
-- placement; event_origins is then rebuilt as the single effective verdict so
-- read-path joins never duplicate rows. Derivers own only their evidence_kind.
CREATE TABLE IF NOT EXISTS event_origin_evidence (
  session_id    TEXT NOT NULL,
  event_id      TEXT NOT NULL,
  origin        TEXT NOT NULL,          -- master_control | unknown
  evidence_kind TEXT NOT NULL,
  evidence_id   TEXT,
  reason        TEXT,
  PRIMARY KEY (session_id, event_id, evidence_kind)
);

-- Codex provenance overlay. Rebuildable mirror of structured Codex control
-- submissions (MCP codex/codex-reply and older send_input tool calls), joined to
-- receiver transcripts by target thread + exact payload SHA-256.
CREATE TABLE IF NOT EXISTS codex_control_submissions (
  evidence_id          TEXT PRIMARY KEY,
  kind                 TEXT NOT NULL,
  submitted_at         TEXT NOT NULL,
  sender_session_id    TEXT,
  target_session_id    TEXT,
  payload_sha256       TEXT NOT NULL,
  payload_byte_count   INTEGER,
  payload_line_count   INTEGER,
  resolved_via         TEXT,
  raw_event            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_bcs_session_sha
  ON bmux_control_submissions(codebrain_session_id, payload_sha256);
CREATE INDEX IF NOT EXISTS ix_eo_origin ON event_origins(origin);
CREATE INDEX IF NOT EXISTS ix_eoe_event ON event_origin_evidence(session_id, event_id);
CREATE INDEX IF NOT EXISTS ix_eoe_kind ON event_origin_evidence(evidence_kind);
CREATE INDEX IF NOT EXISTS ix_ccs_target_sha
  ON codex_control_submissions(target_session_id, payload_sha256);

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
    # WAL + busy timeout: a refresh-on-read may overlap a scheduled ingest or a
    # second query; readers must not block on the writer (no-ops on :memory:).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    _ensure_visibility_columns(conn)
    _ensure_fts(conn)
    _ensure_file_refs(conn)
    _ensure_origin_evidence(conn)
    _ensure_stats(conn)
    conn.commit()
    return conn


def _ensure_stats(conn: sqlite3.Connection) -> None:
    """Compute table statistics once on a large cache that lacks them.

    Without sqlite_stat1 the planner over-prefers the new (actor,type,ts) and
    file_refs indexes and full-scans where a session/event seek is far cheaper
    (it made `recent`/`touched` ~20-40x slower in testing). A bounded one-time
    ANALYZE fixes selectivity; it persists, so later opens just probe and skip.
    Skipped on small/test caches, where the planner is fine without stats."""
    have = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_stat1'"
    ).fetchone()
    if have and conn.execute("SELECT 1 FROM sqlite_stat1 LIMIT 1").fetchone():
        return
    if conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] < 5000:
        return
    print("codebrain: analyzing indexes (one-time)…", file=sys.stderr)
    try:
        conn.execute("PRAGMA analysis_limit=1000")  # sample per index — seconds, not minutes
        conn.execute("ANALYZE")
    except sqlite3.OperationalError:
        # stats are an optimization, not correctness; if a concurrent writer holds the
        # lock (busy_timeout elapsed), skip — the next read retries the one-time build.
        pass


def _ensure_visibility_columns(conn: sqlite3.Connection) -> None:
    """Migrate existing caches to the session visibility schema.

    The raw logs remain authoritative; visibility is a reversible retrieval
    policy stored in the rebuildable SQLite cache.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "hidden_at" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN hidden_at TEXT")
    if "hidden_reason" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN hidden_reason TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_sessions_hidden ON sessions(hidden_at)")


def _ensure_file_refs(conn: sqlite3.Connection) -> None:
    """Backfill the files index once for caches built before it existed.

    The table is created by SCHEMA; new events populate it via upsert_event. An
    existing cache has events but no file_refs rows, so derive them one time from
    events.refs (a one-time scan, like the FTS rebuild). Guarded on emptiness, so
    every later open is a cheap probe. basename is computed in Python with the same
    normalizer the query side uses (so the index can't silently miss a match)."""
    if conn.execute("SELECT 1 FROM file_refs LIMIT 1").fetchone() is not None:
        return
    rows = conn.execute(
        "SELECT event_id, refs FROM events "
        "WHERE json_valid(refs) AND json_array_length(refs, '$.files') > 0"
    ).fetchall()
    if not rows:
        return
    print(f"codebrain: building file index over {len(rows)} events (one-time)…",
          file=sys.stderr)
    conn.executemany(
        "INSERT OR IGNORE INTO file_refs (event_id, file, basename) VALUES (?,?,?)",
        _file_ref_tuples((r["event_id"], r["refs"]) for r in rows),
    )


def _ensure_origin_evidence(conn: sqlite3.Connection) -> None:
    """Seed the multi-deriver evidence table from legacy effective rows once."""
    have_evidence = conn.execute(
        "SELECT 1 FROM event_origin_evidence LIMIT 1"
    ).fetchone()
    if have_evidence is not None:
        return
    have_origins = conn.execute("SELECT 1 FROM event_origins LIMIT 1").fetchone()
    if have_origins is None:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO event_origin_evidence
          (session_id, event_id, origin, evidence_kind, evidence_id, reason)
        SELECT session_id, event_id, origin, COALESCE(evidence_kind, 'legacy'),
               evidence_id, reason
        FROM event_origins
        WHERE origin IN ('master_control', 'unknown')
        """
    )


def _file_ref_tuples(items):
    """(event_id, refs_json) -> (event_id, file, basename) rows, deduped per event."""
    for event_id, refs in items:
        try:
            files = (json.loads(refs) or {}).get("files") or []
        except (json.JSONDecodeError, TypeError):
            continue
        seen = set()
        for f in files:
            if not isinstance(f, str) or not f or f in seen:
                continue
            seen.add(f)
            yield (event_id, f, path_basename(f))


def _record_file_refs(conn: sqlite3.Connection, e: EventRow) -> None:
    """(Re)build one event's rows in the files index to match its current refs.
    Called on first insert and whenever refs change (see upsert_event); the leading
    delete makes it a replace, so a resync can also drop a ref, not only add one."""
    conn.execute("DELETE FROM file_refs WHERE event_id=?", (e.event_id,))
    files = (e.refs or {}).get("files") or []
    seen = set()
    for f in files:
        if not isinstance(f, str) or not f or f in seen:
            continue
        seen.add(f)
        conn.execute(
            "INSERT OR IGNORE INTO file_refs (event_id, file, basename) VALUES (?,?,?)",
            (e.event_id, f, path_basename(f)),
        )


def has_fts5(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x)")
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return True
    except sqlite3.OperationalError:
        return False


def _ensure_fts(conn: sqlite3.Connection) -> None:
    """events_fts is an external-content FTS5 index over events.text, kept current
    by triggers — so incremental ingest never needs a full index rebuild. Migrates
    the pre-trigger standalone table in place (the index is derived data)."""
    if not has_fts5(conn):
        return
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='events_fts'"
    ).fetchone()
    fresh = row is None
    if row is not None and "content='events'" not in (row["sql"] or ""):
        conn.execute("DROP TABLE events_fts")   # old standalone shape -> migrate
        fresh = True
    if fresh:
        conn.execute(
            "CREATE VIRTUAL TABLE events_fts USING fts5("
            "text, content='events', content_rowid='rowid')"
        )
    # Triggers cover every row (even NULL text) so the 'delete' commands always
    # match what was inserted — the requirement for external-content integrity.
    conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS events_fts_ai AFTER INSERT ON events BEGIN
          INSERT INTO events_fts(rowid, text) VALUES (new.rowid, new.text);
        END;
        CREATE TRIGGER IF NOT EXISTS events_fts_ad AFTER DELETE ON events BEGIN
          INSERT INTO events_fts(events_fts, rowid, text) VALUES('delete', old.rowid, old.text);
        END;
        CREATE TRIGGER IF NOT EXISTS events_fts_au AFTER UPDATE ON events BEGIN
          INSERT INTO events_fts(events_fts, rowid, text) VALUES('delete', old.rowid, old.text);
          INSERT INTO events_fts(rowid, text) VALUES (new.rowid, new.text);
        END;
    """)
    if fresh:
        n = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        if n:
            print(f"codebrain: rebuilding FTS index over {n} events (one-time)…",
                  file=sys.stderr)
            conn.execute("INSERT INTO events_fts(events_fts) VALUES('rebuild')")


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
        "SELECT actor, type, text, refs FROM events WHERE event_id=?", (e.event_id,)
    ).fetchone()
    if row is not None and (row["actor"], row["type"], row["text"]) != (e.actor, e.type, e.text):
        return False
    refs_json = json.dumps(e.refs)
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
         refs_json, e.tool_call_event_id, json.dumps(e.raw)),
    )
    if row is None:
        # Fresh insert: only touch the index if there are files (the ~80% no-file
        # events skip it entirely, so a full ingest doesn't eat 1M no-op deletes).
        if (e.refs or {}).get("files"):
            _record_file_refs(conn, e)
    elif row["refs"] != refs_json:
        # refs changed for an existing event — happens when a later refresh re-parses
        # a grown log: e.g. the Codex adapter enriches a tool_call's refs.files with
        # paths from a `patch_apply_end` record that only appears later. Resync (which
        # also clears, in case a ref was dropped) so `touched` can't go stale.
        _record_file_refs(conn, e)
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
    """Repair-only: the triggers keep events_fts current; this re-derives the whole
    index from events (FTS5 'rebuild' command on the external-content table)."""
    if not has_fts5(conn):
        return
    conn.execute("INSERT INTO events_fts(events_fts) VALUES('rebuild')")

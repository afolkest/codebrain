"""Ingest raw logs into the local SQLite cache (idempotent, multi-source).

This reads each tool's raw home directly (read-only) and is agnostic to whether
the root is a live tool dir or a pool subtree (collect.py mirrors homes into
`<pool>/raw/<machine>/<source>` with relpaths preserved, so either works as a
root). All upserts key on copy-invariant ids, so re-running is a no-op and the
three sources share one deduped `events` table.
"""
from __future__ import annotations

import json
import socket
import sqlite3
from pathlib import Path
from typing import Callable, Optional

from codebrain.adapters import claude, codex, pi
from codebrain.db import upsert_event, upsert_placement, upsert_session

DEFAULT_CLAUDE_ROOT = Path.home() / ".claude"
DEFAULT_CODEX_ROOT = Path.home() / ".codex"
DEFAULT_PI_ROOT = Path.home() / ".pi"

SOURCES = ("claude", "codex", "pi")


# ---- discovery (top-level transcripts only; sub-agent files live deeper) ----

def discover_claude_files(raw_root: Path):
    projects = Path(raw_root) / "projects"
    return sorted(projects.glob("*/*.jsonl")) if projects.is_dir() else []


def discover_codex_files(raw_root: Path):
    root = Path(raw_root)
    files = sorted((root / "sessions").glob("*/*/*/rollout-*.jsonl"))
    files += sorted((root / "archived_sessions").glob("*.jsonl"))
    return files


def discover_pi_files(raw_root: Path):
    base = Path(raw_root) / "agent" / "sessions"
    return sorted(base.glob("*/*.jsonl")) if base.is_dir() else []


def _load_codex_titles(raw_root: Path) -> dict:
    """{sessionUuid: thread_name} from session_index.jsonl (Codex has no in-file title)."""
    f = Path(raw_root) / "session_index.jsonl"
    titles: dict = {}
    if not f.is_file():
        return titles
    with open(f, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            i, t = r.get("id"), r.get("thread_name")
            if isinstance(i, str) and isinstance(t, str) and t:
                titles[i] = t
    return titles


def _record_state(conn, path: Path, st, session_id: Optional[str]) -> None:
    """Remember the stat a file had when parsed, so refresh() can skip it until it
    changes. Committed with the file's data (same transaction)."""
    conn.execute(
        "INSERT OR REPLACE INTO ingest_state (path, mtime, size, session_id) VALUES (?,?,?,?)",
        (str(path), st.st_mtime, st.st_size, session_id),
    )


def _ingest(conn, files, parse_fn: Callable, enrich: Optional[Callable] = None) -> dict:
    stats = {"files": 0, "sessions": 0, "events": 0, "placements": 0,
             "skipped": 0, "conflicts": 0, "errors": 0}
    for path in files:
        stats["files"] += 1
        try:
            # Stat BEFORE parsing: if the file grows mid-parse, the recorded state
            # is already stale and the next refresh re-parses it. Never the reverse.
            st = path.stat()
        except OSError:
            continue  # vanished between discovery and now; retried next run
        try:
            parsed = parse_fn(path)
        except Exception as exc:  # noqa: BLE001 — one bad file shouldn't sink the run
            print(f"  ! parse error {path.name}: {exc}")
            stats["errors"] += 1
            _record_state(conn, path, st, None)  # don't re-error every refresh; a
            conn.commit()                        # changed file is retried anyway
            continue
        if parsed is None:
            stats["skipped"] += 1  # contentless file (empty / no emittable events)
            _record_state(conn, path, st, None)
            conn.commit()
            continue
        conflicts = 0
        try:
            if enrich is not None:
                enrich(parsed)
            upsert_session(conn, parsed.session)
            skipped: set = set()
            for e in parsed.events:
                if not upsert_event(conn, e):  # copy-consistency conflict (SCHEMA.md)
                    conflicts += 1
                    skipped.add(e.event_id)
                    print(f"  ~ conflict {path.name}: kept first content for {e.event_id}")
            # A re-parse is authoritative for this session's placements: replace,
            # don't merge, so no stale placement survives a rewritten/shrunk file.
            conn.execute("DELETE FROM session_events WHERE session_id=?",
                         (parsed.session.session_id,))
            for p in parsed.placements:
                if p.event_id in skipped:
                    continue  # the event row we'd point at holds another session's content
                upsert_placement(conn, p)
            _record_state(conn, path, st, parsed.session.session_id)
        except Exception as exc:  # noqa: BLE001 — a genuine DB error isolates one file
            print(f"  ! write error {path.name}: {exc}")
            stats["errors"] += 1
            conn.rollback()
            continue
        stats["sessions"] += 1
        stats["events"] += len(parsed.events)
        stats["placements"] += len(parsed.placements)
        stats["conflicts"] += conflicts
        conn.commit()
    return stats


def _machine_for_root(source: str, raw_root: Optional[Path], machine: Optional[str]) -> str:
    """Which machine produced the sessions under this root. A pool subtree carries
    its origin in the path — raw/<machine>/<source> (SCHEMA.md) — which is the only
    record of it: ingesting macmini's synced subtree on the macbook must NOT stamp
    those sessions 'macbook'. A live tool home is always this machine. An explicit
    machine= wins over both."""
    if machine:
        return machine
    if raw_root is not None:
        root = Path(raw_root)
        if root.name == source and root.parent.parent.name == "raw":
            return root.parent.name
    return socket.gethostname()


def _source_jobs(source: str, machine: Optional[str], raw_root: Optional[Path] = None):
    """(files, parse_fn, enrich) for one source — shared by full ingest and refresh."""
    machine = _machine_for_root(source, raw_root, machine)
    if source == "claude":
        root = raw_root or DEFAULT_CLAUDE_ROOT
        return (discover_claude_files(root),
                lambda p: claude.parse_file(p, machine=machine), None)
    if source == "codex":
        root = raw_root or DEFAULT_CODEX_ROOT
        titles = _load_codex_titles(root)

        def enrich(parsed):
            uuid = parsed.session.session_id.split(":", 1)[-1]
            if uuid in titles:
                parsed.session.title = titles[uuid]

        return (discover_codex_files(root),
                lambda p: codex.parse_file(p, machine=machine), enrich)
    if source == "pi":
        root = raw_root or DEFAULT_PI_ROOT
        return (discover_pi_files(root),
                lambda p: pi.parse_file(p, machine=machine), None)
    raise ValueError(f"unknown source {source!r}")


def ingest_source(conn: sqlite3.Connection, source: str,
                  raw_root: Optional[Path] = None, machine: Optional[str] = None) -> dict:
    files, parse_fn, enrich = _source_jobs(source, machine, raw_root)
    return _ingest(conn, files, parse_fn, enrich)


def refresh(conn: sqlite3.Connection, sources=SOURCES, machine: Optional[str] = None,
            roots: Optional[dict] = None) -> dict:
    """Delta ingest: re-parse only files that are new or whose (mtime, size) changed
    since ingest_state last saw them. Cheap enough to run before every read —
    tens of ms when nothing changed — which makes the DB effectively always
    current for this machine: there is no 'not ingested yet' window.
    `roots` optionally overrides a source's raw root ({"pi": Path(...)})."""
    state = {r["path"]: (r["mtime"], r["size"])
             for r in conn.execute("SELECT path, mtime, size FROM ingest_state")}
    total = {"files": 0, "sessions": 0, "events": 0, "placements": 0,
             "skipped": 0, "conflicts": 0, "errors": 0}
    for src in sources:
        files, parse_fn, enrich = _source_jobs(src, machine, (roots or {}).get(src))
        changed = []
        for f in files:
            try:
                st = f.stat()
            except OSError:
                continue
            if state.get(str(f)) != (st.st_mtime, st.st_size):
                changed.append(f)
        if changed:
            stats = _ingest(conn, changed, parse_fn, enrich)
            for k, v in stats.items():
                total[k] += v
    return total


def ingest_all(conn: sqlite3.Connection, sources=SOURCES, machine: Optional[str] = None) -> dict:
    """Full pass: parse every discovered file (first build / disaster recovery).
    Also primes ingest_state, so the next refresh() is a delta. FTS stays current
    via the events triggers — no rebuild step."""
    total = {"files": 0, "sessions": 0, "events": 0, "placements": 0,
             "skipped": 0, "conflicts": 0, "errors": 0}
    for src in sources:
        stats = ingest_source(conn, src, machine=machine)
        print(f"  {src}: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
        for k, v in stats.items():
            total[k] += v
    conn.commit()
    return total


# Back-compat helper (the spine's original entry point).
def ingest_claude(conn: sqlite3.Connection, raw_root: Path = DEFAULT_CLAUDE_ROOT,
                  machine: Optional[str] = None) -> dict:
    stats = ingest_source(conn, "claude", raw_root=raw_root, machine=machine)
    conn.commit()
    return stats

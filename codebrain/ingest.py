"""Ingest raw logs into the local SQLite cache (idempotent, multi-source).

For the spine this reads each tool's raw home directly (read-only). The
collector -> pool step (DESIGN.md) comes later; ingest is agnostic to whether the
root is a live tool dir or a synced pool. All upserts key on copy-invariant ids,
so re-running is a no-op and the three sources share one deduped `events` table.
"""
from __future__ import annotations

import json
import socket
import sqlite3
from pathlib import Path
from typing import Callable, Optional

from codebrain.adapters import claude, codex, pi
from codebrain.db import rebuild_fts, upsert_event, upsert_placement, upsert_session

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


def _ingest(conn, files, parse_fn: Callable, enrich: Optional[Callable] = None) -> dict:
    stats = {"files": 0, "sessions": 0, "events": 0, "placements": 0,
             "skipped": 0, "conflicts": 0, "errors": 0}
    for path in files:
        stats["files"] += 1
        try:
            parsed = parse_fn(path)
        except Exception as exc:  # noqa: BLE001 — one bad file shouldn't sink the run
            print(f"  ! parse error {path.name}: {exc}")
            stats["errors"] += 1
            continue
        if parsed is None:
            stats["skipped"] += 1  # contentless file (empty / no emittable events)
            continue
        conflicts = 0
        try:
            if enrich is not None:
                enrich(parsed)
            upsert_session(conn, parsed.session)
            for e in parsed.events:
                if not upsert_event(conn, e):  # copy-consistency conflict (SCHEMA.md)
                    conflicts += 1
                    print(f"  ~ conflict {path.name}: kept first content for {e.event_id}")
            for p in parsed.placements:
                upsert_placement(conn, p)
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


def ingest_source(conn: sqlite3.Connection, source: str,
                  raw_root: Optional[Path] = None, machine: Optional[str] = None) -> dict:
    machine = machine or socket.gethostname()
    if source == "claude":
        root = raw_root or DEFAULT_CLAUDE_ROOT
        return _ingest(conn, discover_claude_files(root),
                       lambda p: claude.parse_file(p, machine=machine))
    if source == "codex":
        root = raw_root or DEFAULT_CODEX_ROOT
        titles = _load_codex_titles(root)

        def enrich(parsed):
            uuid = parsed.session.session_id.split(":", 1)[-1]
            if uuid in titles:
                parsed.session.title = titles[uuid]

        return _ingest(conn, discover_codex_files(root),
                       lambda p: codex.parse_file(p, machine=machine), enrich)
    if source == "pi":
        root = raw_root or DEFAULT_PI_ROOT
        return _ingest(conn, discover_pi_files(root),
                       lambda p: pi.parse_file(p, machine=machine))
    raise ValueError(f"unknown source {source!r}")


def ingest_all(conn: sqlite3.Connection, sources=SOURCES, machine: Optional[str] = None) -> dict:
    total = {"files": 0, "sessions": 0, "events": 0, "placements": 0,
             "skipped": 0, "conflicts": 0, "errors": 0}
    for src in sources:
        stats = ingest_source(conn, src, machine=machine)
        print(f"  {src}: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
        for k, v in stats.items():
            total[k] += v
    rebuild_fts(conn)
    conn.commit()
    return total


# Back-compat helper (the spine's original entry point).
def ingest_claude(conn: sqlite3.Connection, raw_root: Path = DEFAULT_CLAUDE_ROOT,
                  machine: Optional[str] = None) -> dict:
    stats = ingest_source(conn, "claude", raw_root=raw_root, machine=machine)
    rebuild_fts(conn)
    conn.commit()
    return stats

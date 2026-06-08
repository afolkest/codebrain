"""Ingest raw logs into the local SQLite cache (idempotent).

For the spine this reads a raw root directly (default ~/.claude, read-only).
The collector -> pool step (DESIGN.md) comes later; ingest is agnostic to
whether the root is a live tool dir or a synced pool.
"""
from __future__ import annotations

import socket
import sqlite3
from pathlib import Path
from typing import Optional

from codebrain.adapters import claude
from codebrain.db import rebuild_fts, upsert_event, upsert_placement, upsert_session

DEFAULT_CLAUDE_ROOT = Path.home() / ".claude"


def discover_claude_files(raw_root: Path):
    """Top-level session transcripts only (sub-agent files live one level deeper)."""
    projects = Path(raw_root) / "projects"
    if not projects.is_dir():
        return []
    return sorted(projects.glob("*/*.jsonl"))


def ingest_claude(conn: sqlite3.Connection, raw_root: Path = DEFAULT_CLAUDE_ROOT,
                  machine: Optional[str] = None) -> dict:
    machine = machine or socket.gethostname()
    files = discover_claude_files(raw_root)
    stats = {"files": 0, "sessions": 0, "events": 0, "placements": 0, "errors": 0}
    for path in files:
        stats["files"] += 1
        try:
            parsed = claude.parse_file(path, machine=machine)
        except Exception as exc:  # noqa: BLE001 — one bad file shouldn't sink the run
            print(f"  ! parse error {path.name}: {exc}")
            stats["errors"] += 1
            continue
        if parsed is None:
            continue
        try:
            upsert_session(conn, parsed.session)
            for e in parsed.events:
                upsert_event(conn, e)
            for p in parsed.placements:
                upsert_placement(conn, p)
        except ValueError as exc:  # copy-consistency / origin invariant
            print(f"  ! invariant {path.name}: {exc}")
            stats["errors"] += 1
            conn.rollback()
            continue
        stats["sessions"] += 1
        stats["events"] += len(parsed.events)
        stats["placements"] += len(parsed.placements)
        conn.commit()
    rebuild_fts(conn)
    conn.commit()
    return stats

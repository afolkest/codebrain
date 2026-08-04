"""Canonical row model — the contract adapters emit (see SCHEMA.md).

An adapter turns one raw session file into a `ParsedSession`:
  - one `SessionRow`
  - N `EventRow` (deduped content)
  - N `PlacementRow` (per-session placement = the forest)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def read_records(path: Path) -> list:
    """Tolerant JSONL read shared by every adapter: skip blank/garbled lines and a
    partial trailing line in an actively-written session; tag each dict with its
    0-based source line as ``_line`` (used for ordering / event ids)."""
    records = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                rec["_line"] = i
                records.append(rec)
    return records


@dataclass
class EventRow:
    """Deduped content. `event_id` is `<source>:…`, copy-invariant."""
    event_id: str
    ts: str
    actor: str            # user | assistant | tool
    type: str             # message | tool_call | tool_result
    text: Optional[str]
    refs: dict            # {"files": [...], "commands": [...]}
    raw: dict             # canonical raw evidence (original record or safe projection)
    tool_call_event_id: Optional[str] = None   # tool_result → its paired tool_call
    origin_session_id: Optional[str] = None


@dataclass
class PlacementRow:
    """Placement of an event within one session's transcript (the forest)."""
    session_id: str
    event_id: str
    seq: int
    parent_event_id: Optional[str]
    live: int             # 1 on this session's live branch, else 0
    inherited: int        # 1 if copied in from a parent session (pi/Cursor), else 0


@dataclass
class SessionRow:
    session_id: str
    source: str
    machine: Optional[str] = None
    cwd: Optional[str] = None
    repo: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    parent_session_id: Optional[str] = None
    relation: Optional[str] = None
    spawn_event_id: Optional[str] = None
    branch_point_event_id: Optional[str] = None
    tip_event_id: Optional[str] = None
    title: Optional[str] = None


@dataclass(frozen=True)
class SourceHead:
    """Validated source revision rank attached to an archive-backed parse."""
    revision: int
    digest: str


@dataclass
class ParsedSession:
    session: SessionRow
    events: list = field(default_factory=list)
    placements: list = field(default_factory=list)
    source_head: Optional[SourceHead] = None

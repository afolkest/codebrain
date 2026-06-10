"""Shared test helpers: fixture writing, an in-memory DB, and the schema
invariants every adapter's output must satisfy (SCHEMA.md)."""
from __future__ import annotations

import json
from pathlib import Path

from codebrain import db


def write_jsonl(directory, name: str, records: list) -> Path:
    """Write `records` as one JSON object per line; return the file path."""
    p = Path(directory) / name
    with open(p, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return p


def memory_db():
    """A fresh in-memory database with the real schema + FTS."""
    return db.connect(Path(":memory:"))


def assert_session_invariants(tc, parsed, source: str) -> None:
    """Invariants that must hold for ANY ParsedSession, on every source.

    These are the properties we keep hand-checking against the live DB; pinning
    them here means a regression in liveness/parent/seq logic fails a test
    instead of silently corrupting a transcript.
    """
    events = parsed.events
    placements = parsed.placements
    eids = [e.event_id for e in events]
    eset = set(eids)

    # event ids: unique within a session, and source-prefixed (copy-invariant scheme)
    tc.assertEqual(len(eids), len(eset), "duplicate event_id within one session")
    for eid in eids:
        tc.assertTrue(eid.startswith(source + ":"), f"event_id not source-prefixed: {eid!r}")

    # exactly one placement per event; seq is a 0..n-1 permutation
    tc.assertEqual(len(placements), len(events), "placement/event count mismatch")
    pl_by_eid = {p.event_id: p for p in placements}
    tc.assertEqual(set(pl_by_eid), eset, "placement event_ids != event ids")
    tc.assertEqual(sorted(p.seq for p in placements), list(range(len(placements))),
                   "seq is not a contiguous 0..n-1 range")

    # every parent resolves to an event in THIS session (or is a root)
    for p in placements:
        if p.parent_event_id is not None:
            tc.assertIn(p.parent_event_id, eset,
                        f"dangling parent {p.parent_event_id!r} for {p.event_id!r}")

    # the core liveness invariant: a live event never hangs off a dead parent
    for p in placements:
        if p.live and p.parent_event_id is not None:
            tc.assertEqual(pl_by_eid[p.parent_event_id].live, 1,
                           f"live {p.event_id!r} has dead parent {p.parent_event_id!r}")

    # tip is either NULL (fully rolled back) or a live event of the session
    tip = parsed.session.tip_event_id
    if tip is not None:
        tc.assertIn(tip, eset, f"tip {tip!r} is not an event of the session")
        tc.assertEqual(pl_by_eid[tip].live, 1, "tip is not live")

    # parent chains terminate (no cycles) — guards the cycle/self-loop crash class
    for start in eids:
        seen = set()
        cur = pl_by_eid[start].parent_event_id
        while cur is not None:
            tc.assertNotIn(cur, seen, f"cycle in parent chain from {start!r}")
            seen.add(cur)
            cur = pl_by_eid[cur].parent_event_id


def live_ids(parsed) -> set:
    return {p.event_id for p in parsed.placements if p.live}


def by_id(parsed) -> dict:
    return {e.event_id: e for e in parsed.events}

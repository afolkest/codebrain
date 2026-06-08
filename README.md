# codebrain

A searchable store of my coding-agent sessions (Claude, Codex, pi). See
[DESIGN.md](DESIGN.md) for architecture, [SCHEMA.md](SCHEMA.md) for the canonical
schema, and [`formats/`](formats/) for the reverse-engineered log formats.

**Status:** all three sources land on one schema —
`raw logs → per-source adapter → SQLite (3 tables) → CLI + FTS + grep + raw SQL`.
Proven on real logs: **~1,970 sessions / ~380k events** from Claude, Codex, and pi,
ingested with zero errors (claude 90% live, codex 98%, pi 94%).

## Quickstart

```bash
pip install -e .            # or run as: python3 -m codebrain <cmd>

sessdb ingest               # build the DB from ~/.claude + ~/.codex + ~/.pi (read-only)
sessdb ingest --source pi   # …or just one source
sessdb list                 # recent sessions (any source)
sessdb show <session>       # a session's live transcript (--all includes rolled-back)
sessdb search <query>       # FTS5 over event text (ranked, cross-source)
sessdb grep <pattern>       # ripgrep over the raw logs (all sources)
sessdb schema               # print the DDL
```

The DB is a **rebuildable cache** (DESIGN.md golden rule): delete `~/.codebrain/codebrain.db`
and re-ingest anytime. It is never synced. Point any sqlite3 client at it for
arbitrary joins — the schema is the interface (`sessdb schema`).

## Model (SCHEMA.md)

- **`events`** — deduped content (`<source>:`-prefixed, copy-invariant ids).
- **`session_events`** — per-session placement (`seq`, `parent_event_id`, `live`,
  `inherited`); the forest lives here, so an event can be live in one session and
  rolled-back in another.
- **`sessions`** — metadata + lineage/sub-agent links + tip.

Read a transcript: `SELECT * FROM transcript WHERE session_id=? AND live=1 ORDER BY seq`.

## What each adapter handles

- **Claude** (`parentUuid` tree) — bridged-parent linearization, parallel-tool
  results, compaction reconnection, resume re-emission dedup.
- **Codex** (flat append-only log, no native tree) — **synthesized** turn forest:
  turns anchored on the clean `user_message` (works on 0.39→0.137, incl. the old
  logs that lack `task_started`); `thread_rolled_back{n}` pops live user-turns as
  dead side branches; `apply_patch`/`patch_apply_end` files; sub-agent/fork lineage.
- **pi** (`parentId` tree) — the cross-file case the schema exists for: resume/branch
  copies the parent's live prefix **verbatim** into a new file, so a shared event is
  **one `events` row** with N placements (origin `inherited=0` + copies `inherited=1`),
  keyed on the copy-invariant `pi:<8hex>:<ts>`.

## Current limitations

- **Main transcript path only** — sub-agents are deferred: Claude `<sessionId>/subagents/`
  (inline `isSidechain` copies are ignored), pi `<session>/<runId>/run-<i>/`. Codex
  sub-agent rollouts *are* ingested (they're standalone files) with a parent-session
  link; the spawn-event link (`spawn_event_id`) is a later cross-file slice.
- Ingest re-reads all files each run (idempotent upserts); incremental ingest,
  the collector→pool step, and embeddings/sqlite-vec are later slices.
- `bash`-side file mutations aren't tracked in `refs` (known gap across all sources);
  Codex reasoning is encrypted (≥2026-04) and excluded everywhere.

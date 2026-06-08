# codebrain

A searchable store of my coding-agent sessions (Claude, Codex, pi). See
[DESIGN.md](DESIGN.md) for architecture, [SCHEMA.md](SCHEMA.md) for the canonical
schema, and [`formats/`](formats/) for the reverse-engineered log formats.

**Status:** spine proven end-to-end for the Claude main-transcript path —
`raw logs → adapter → SQLite (3 tables) → CLI + FTS + grep + raw SQL`.

## Quickstart

```bash
pip install -e .            # or run as: python3 -m codebrain <cmd>

sessdb ingest               # build the DB from ~/.claude (read-only) -> ~/.codebrain/codebrain.db
sessdb list                 # recent sessions
sessdb show <session>       # a session's live transcript (--all includes rolled-back)
sessdb search <query>       # FTS5 over event text (ranked)
sessdb grep <pattern>       # ripgrep over the raw logs
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

## Current limitations

- **Claude only**, **main transcript path only** — sub-agents (`<sessionId>/subagents/`),
  Codex, and pi adapters are not built yet.
- Ingest re-reads all files each run (idempotent upserts); incremental ingest,
  the collector→pool step, and embeddings/sqlite-vec are later slices.
- `bash`-side file mutations aren't tracked in `refs` (known gap across all sources).

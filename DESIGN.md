# codebrain — Design

## What this is

A centralized, searchable store of all my coding-agent sessions (Claude, Codex,
Cursor, pi, …) across all my machines, accessible to any agent. Lets an agent read
a specific session's messages, search across sessions (keyword + semantic), and
filter on structured facts (files touched, repo, time, machine).

## Golden rule

**Raw evidence is the source of truth. The database is a disposable, rebuildable cache.**
For ordinary append-only sources, raw evidence is the original log. Cursor is the
explicit safety exception: its live SQLite store mixes transcript data with
secrets and unrelated application state, so the raw evidence codebrain owns and
syncs is a deterministic, allowlisted transcript projection. Anything derived
(normalized events, embeddings, summaries, indexes) can be deleted and
regenerated from the corresponding raw evidence. This removes migration fear and
makes parser bugs non-catastrophic.

## Architecture

```
Sources    .claude / .codex / .pi + Cursor SQLite (per machine)
   │
Projector  Cursor SQLite → private immutable safe archive
   │
Collector  mirror allowlisted evidence → sync pool ← writes only own subtree
   │
Syncthing  replicate pool across machines         ← P2P, encrypted, offline-tolerant
   │
Adapters   per-source parsers → canonical events  ← the only agent-specific code
   │
Ingester   build local SQLite from live homes + synced pool ← incremental, idempotent
   │
Index      FTS + vectors + files-touched + summaries
   │
Access     CLI + raw SQL + grep                   ← agents write scripts
```

## Core decisions

**Storage: SQLite (one file per machine).**
- Holds relational metadata + FTS5 (keyword) + sqlite-vec (semantic) in one file.
- Zero-ops, openable from any script in any language — fits the agents-write-scripts model.
- Built locally on each machine; **never synced** (binary + WAL corrupts when synced).
- Personal scale; if we ever outgrow it, the rebuildable-cache rule makes migrating to Postgres an afternoon.

**Multi-machine: sync the inputs, not the index.**
- Sync cheap append-only source evidence (original logs or immutable Cursor
  revision segments) plus the derivation cache; regenerate the heavy binary DB
  locally.
- Each machine queries its own local DB → fast, works offline (laptop away from home with the mini asleep).
- Normal reads refresh local live tool homes directly and project settled Cursor
  sessions for immediate current-session freshness, then refresh synced remote
  pool subtrees for cross-machine history. Cursor archive discovery uses
  rebuildable, root-scoped per-session metadata signatures, so the unchanged
  corpus path opens no revision JSON and only changed chains are reconstructed.
- Tradeoff accepted: remote sessions are eventually consistent (collector interval + Syncthing latency + next local read command).

**Sync transport: Syncthing.**
- Peer-to-peer, encrypted, continuous, no cloud (keeps secret-laden transcripts on our own devices).
- **Single writer per path:** each machine writes only `raw/<hostname>/…`, which
  avoids ordinary conflicts. A conflicting immutable Cursor revision is never
  overwritten and is reported.
- Pool is separate from live tool dirs; a collector mirrors evidence in (firewall
  between live tool state and the synced archive). Never point Syncthing at a
  live tool home or Cursor's application-support directory.

**Pool layout** (built by `sessdb collect`, default `~/codebrain-pool`)
```
codebrain-pool/
  raw/
    macbook/  claude/…  codex/…  cursor/…  pi/… ← only macbook writes here
    macmini/  …                               ← only the mini writes here
  derivations/
    <content-hash>.json                       ← later: embeddings + summaries, content-addressed
```
Claude/Codex/pi subtrees preserve the tool home's internal layout (allowlisted
files only). Cursor instead preserves codebrain's immutable revision layout from
`~/.codebrain/cursor-raw`; Cursor's database, WAL, exporter state, credentials,
and other application state never enter the pool. Ingest can use any pool source
subtree as a raw root. Automatic read-time refresh skips this machine's own pool
subtree by default because live sources are fresher and should remain
authoritative.

**Format: sync source evidence, normalize on ingest.**
- The pool holds original tool logs for Claude/Codex/pi. For Cursor it holds the
  positively projected transcript envelope and its immutable revision chain;
  reviewed tool arguments/results remain lossless transcript evidence and can
  still contain secrets. Every machine runs the adapter as code.
- Ingest into a **thin canonical envelope**, keeping the raw blob attached:
  `event(session_id, seq, ts, actor, type[message|tool_call|tool_result], text, refs[files,commands], raw)`
- Normalization is what makes intelligent access cheap (e.g. "redact tool calls" = filter by `type`).

**Access: no MCP — CLI + raw SQL + grep.**
- Agents are great at writing scripts; a fixed tool surface is too rigid.
- Thin CLI for common ops, **read-only raw-SQL escape hatch** for arbitrary joins,
  `sessdb grep` shelling to ripgrep over local source and remote pool roots for
  literal/regex forensics. Cursor is limited to its safe archive; default grep
  never searches Cursor's live database or home.
- Schema documented (e.g. `--schema` / CLAUDE.md) in place of MCP discoverability.

**Query modalities → structures**
| Need | Mechanism |
|---|---|
| "last 3 messages of session X" | ordered events by id |
| "sessions that edited auth.py" | files-touched index |
| keyword search | FTS5 (ranked) |
| semantic search | sqlite-vec embeddings |
| literal/regex forensics | ripgrep over raw |

**Derivations: content-addressed, computed once.**
- Embeddings/summaries keyed by content hash; whichever machine sees content first computes it, result propagates via the synced `derivations/` cache. No double work.

## Where the work is

1. **Adapters (~half).** Reverse-engineering N undocumented, drifting log formats; mapping their event zoos onto the envelope; per-adapter files-touched extraction; sub-agent/nested transcripts. Ongoing maintenance, not one-time.
2. **Ingest pipeline + daemons (~20%).** Incremental/idempotent ingest; collectors + ingesters as reliable background services on two Macs.
3. **Retrieval quality (~20%, unbounded ceiling).** Chunking, what to embed, summary granularity. Minimum is cheap; "good" can absorb infinite time.
4. **Schema + CLI + grep + sync config (~10–15%).** The visible "interface" — the cheap, bounded part.

## Build sequence

1. One adapter (Claude) end-to-end: collector → pool → ingest → SQLite → CLI → grep. Prove the spine.
2. Add adapters one at a time as plugins against the working system.
3. Add embeddings/summaries once real data flows, so retrieval is tuned on the actual corpus.

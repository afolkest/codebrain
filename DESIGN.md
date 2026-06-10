# codebrain — Design

## What this is

A centralized, searchable store of all my coding-agent sessions (Claude, Codex, pi, …) across all my machines, accessible to any agent. Lets an agent read a specific session's messages, search across sessions (keyword + semantic), and filter on structured facts (files touched, repo, time, machine).

## Golden rule

**Raw logs are the source of truth. The database is a disposable, rebuildable cache.**
Anything derived (normalized events, embeddings, summaries, indexes) can be deleted and regenerated from raw logs. This removes migration fear and makes parser bugs non-catastrophic.

## Architecture

```
Sources    .claude / .codex / .pi (per machine)   ← raw logs, immutable
   │
Collector  mirror local logs → sync pool          ← writes only own subtree
   │
Syncthing  replicate pool across machines         ← P2P, encrypted, offline-tolerant
   │
Adapters   per-source parsers → canonical events  ← the only agent-specific code
   │
Ingester   build local SQLite from pool           ← incremental, idempotent
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
- Sync cheap append-only text (raw logs + derivation cache); regenerate the heavy binary DB locally.
- Each machine queries its own local DB → fast, works offline (laptop away from home with the mini asleep).
- Tradeoff accepted: eventual consistency between machines.

**Sync transport: Syncthing.**
- Peer-to-peer, encrypted, continuous, no cloud (keeps secret-laden transcripts on our own devices).
- **Single writer per path:** each machine writes only `raw/<hostname>/…`, so conflicts can't happen.
- Pool is separate from live tool dirs; a collector mirrors logs in (firewall between live tool state and the synced archive). Never point Syncthing at `~/.claude` directly.

**Pool layout** (built by `sessdb collect`, default `~/codebrain-pool`)
```
codebrain-pool/
  raw/
    macbook/  claude/…  codex/…  pi/…         ← only macbook writes here
    macmini/  …                               ← only the mini writes here
  derivations/
    <content-hash>.json                       ← later: embeddings + summaries, content-addressed
```
Each `<source>` subtree preserves the tool home's internal layout (allowlisted
files only — credentials and tool-internal databases never enter the pool), so
ingest can use a pool subtree as a raw root exactly like a live home.

**Format: sync raw, normalize on ingest.**
- Pool holds original tool format (truest source of truth); every machine runs every adapter as code.
- Ingest into a **thin canonical envelope**, keeping the raw blob attached:
  `event(session_id, seq, ts, actor, type[message|tool_call|tool_result], text, refs[files,commands], raw)`
- Normalization is what makes intelligent access cheap (e.g. "redact tool calls" = filter by `type`).

**Access: no MCP — CLI + raw SQL + grep.**
- Agents are great at writing scripts; a fixed tool surface is too rigid.
- Thin CLI for common ops, **read-only raw-SQL escape hatch** for arbitrary joins, `sessdb grep` shelling to ripgrep over `raw/` for literal/regex forensics.
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

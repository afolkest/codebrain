# Cursor ingestion execution checklist

Goal: add Cursor IDE session ingestion to codebrain through a deterministic,
safe projection of Cursor's local SQLite history. Cursor's database remains the
upstream source of truth; codebrain archives only an allowlisted transcript
projection and keeps its normalized SQLite cache rebuildable.

## Slice 1 — Safe Cursor snapshot projection

- [ ] Add a read-only Cursor SQLite reader and capability-gated logical snapshot
  projection for embedded and separate-bubble composer formats.
- [ ] Preserve structured ordering, session metadata, tools, simulated/control
  flags, lineage, and timestamp evidence.
- [ ] Positively allowlist exported fields; exclude encryption keys, opaque
  conversation state, thinking, tool binaries, and unrelated context.
- [ ] Cover WAL visibility, format generations, timestamp fallbacks, missing
  bubbles, active/draft sessions, and security canaries.

Scope: logical in-memory snapshots and source-database safety.

Non-goals: publishing archive revisions, canonical event normalization, CLI
integration, CLI/ACP/background-agent stores.

## Slice 2 — Immutable revision archive

- [ ] Publish deterministic immutable per-session revision segments with
  snapshot/payload hashes and atomic private-file semantics.
- [ ] Store complete current order plus only new/changed safe payloads.
- [ ] Reconstruct the latest complete revision under out-of-order arrival.
- [ ] Add incremental header tokens, periodic/full reconciliation, locking, and
  failure isolation.

Scope: codebrain-owned raw Cursor archive and its discovery/reconstruction API.

Non-goals: normalized database ingestion or direct collection from `~/.cursor`.

## Slice 3 — Canonical Cursor adapter

- [ ] Map Cursor sessions, messages, tool calls/results, refs, timestamps, and
  placements into the canonical envelope.
- [ ] Implement copy-invariant timestamped event IDs and safe session-scoped
  fallback IDs for untimed historical bubbles.
- [ ] Implement inherited-prefix detection, branch points, structured subagent
  lineage, and spawn-call linkage.
- [ ] Exclude thinking and unstructured classifications; exercise all adapter
  invariants and version/tool edge cases.

Scope: sanitized revision snapshot to `ParsedSession`.

Non-goals: live database access, source collection, or provenance overlays.

## Slice 4 — Ingest, refresh, collection, and pool integration

- [ ] Add `cursor` to source discovery, defaults, refresh, full ingest, and pool
  machine handling.
- [ ] Export before local refresh/collection while never exporting for custom or
  pool roots.
- [ ] Collect only immutable safe revision files and preserve the append-only
  pool contract.
- [ ] Verify authoritative placement replacement, no-op refresh, rollback
  shrink, remote pool round trip, and failure isolation.

Scope: ingestion spine and sync durability.

Non-goals: syncing Cursor databases, application support, or project sidecars.

## Slice 5 — Provenance and CLI integration

- [ ] Add structured Cursor origin evidence for simulated messages, plan
  execution, and subagent kickoff input.
- [ ] Add Cursor source/root handling across CLI filters, refresh paths, prefix
  resolution, help text, and safe raw grep.
- [ ] Prove structured fields—not text—drive human-intent filtering and lineage.
- [ ] Harden event copy-consistency pairing if required by Cursor result IDs.

Scope: retrieval behavior and user-facing commands.

Non-goals: automatic decisions/preferences, prompt classifiers, or source-text
heuristics.

## Slice 6 — Documentation, real-corpus smoke, and final verification

- [ ] Document the Cursor format, safe-projection exception, source mapping,
  sync boundary, privacy exclusions, and deferred surfaces.
- [ ] Add a non-mutating real-corpus smoke test with clean skipping when Cursor
  is unavailable.
- [ ] Run the full test suite, diff checks, security scans, and independent final
  review; fix all must-fix and should-fix findings.
- [ ] Confirm the repository is clean and every completed slice is committed.

Scope: governing docs and end-to-end acceptance.

Non-goals: Cursor CLI/ACP protobuf decoding, remote Background Agents,
historical unordered orphan bubbles, or ancillary binary artifacts.


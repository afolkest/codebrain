# Cursor ingestion execution checklist

Goal: add Cursor IDE session ingestion to codebrain through a deterministic,
safe projection of Cursor's local SQLite history. Cursor's database remains the
upstream source of truth; codebrain archives only an allowlisted transcript
projection and keeps its normalized SQLite cache rebuildable.

## Slice 1 — Safe Cursor snapshot projection

- [x] Add a read-only Cursor SQLite reader and capability-gated logical snapshot
  projection for embedded and separate-bubble composer formats.
- [x] Preserve structured ordering, session metadata, tools, simulated/control
  flags, lineage, and timestamp evidence.
- [x] Positively allowlist exported fields; exclude encryption keys, opaque
  conversation state, thinking, tool binaries, and unrelated context.
- [x] Cover WAL visibility, format generations, timestamp fallbacks, missing
  bubbles, active/draft sessions, and security canaries.

Scope: logical in-memory snapshots and source-database safety.

Non-goals: publishing archive revisions, canonical event normalization, CLI
integration, CLI/ACP/background-agent stores.

## Slice 2 — Immutable revision archive

- [x] Publish deterministic immutable per-session revision segments with
  snapshot/payload hashes and atomic private-file semantics.
- [x] Store complete current order plus only new/changed safe payloads.
- [x] Reconstruct the latest complete revision under out-of-order arrival.
- [x] Add incremental header tokens, periodic/full reconciliation, locking, and
  failure isolation.

Scope: codebrain-owned raw Cursor archive and its discovery/reconstruction API.

Non-goals: normalized database ingestion or direct collection from `~/.cursor`.

## Slice 3 — Canonical Cursor adapter

- [x] Map Cursor sessions, messages, tool calls/results, refs, timestamps, and
  placements into the canonical envelope.
- [x] Implement copy-invariant timestamped event IDs and safe session-scoped
  fallback IDs for untimed historical bubbles.
- [x] Implement inherited-prefix detection, branch points, structured subagent
  lineage, and spawn-call linkage.
- [x] Exclude thinking and unstructured classifications; exercise all adapter
  invariants and version/tool edge cases.

Scope: sanitized revision snapshot to `ParsedSession`.

Non-goals: live database access, source collection, or provenance overlays.

## Slice 4 — Ingest, refresh, collection, and pool integration

- [x] Add `cursor` to source discovery, defaults, refresh, full ingest, and pool
  machine handling.
- [x] Export before local refresh/collection while never exporting for custom or
  pool roots.
- [x] Collect only immutable safe revision files and preserve the append-only
  pool contract.
- [x] Verify authoritative placement replacement, no-op refresh, rollback
  shrink, remote pool round trip, and failure isolation.

Scope: ingestion spine and sync durability.

Non-goals: syncing Cursor databases, application support, or project sidecars.

## Slice 5 — Provenance and CLI integration

- [x] Add structured Cursor origin evidence for simulated messages, plan
  execution, and subagent kickoff input.
- [x] Add Cursor source/root handling across CLI filters, refresh paths, prefix
  resolution, help text, and safe raw grep.
- [x] Prove structured fields—not text—drive human-intent filtering and lineage.
- [x] Harden event copy-consistency pairing if required by Cursor result IDs.

Scope: retrieval behavior and user-facing commands.

Non-goals: automatic decisions/preferences, prompt classifiers, or source-text
heuristics.

## Slice 6 — Privacy and durability hardening

- [x] Fail closed on malformed hidden/settled controls and harden Cursor
  collection/default grep against destination/root symlinks.
- [x] Restrict stale cleanup to exact collector-owned temporary names and fsync
  every newly created Cursor pool directory edge.
- [x] Cover every codebrain-owned symlink boundary, hidden-content leakage, and
  malformed settled-state signals with destructive canaries.
- [x] Run the full suite and focused security checks, then commit the completed
  privacy/durability slice.

Scope: source firewall and codebrain-owned destination/privacy boundaries.

Non-goals: Cursor CLI/ACP protobuf decoding, remote Background Agents,
historical unordered orphan bubbles, ancillary binary artifacts, or
corpus-scale refresh/revision-authority changes.

## Slice 7 — Documentation and real-corpus smoke

- [x] Document the Cursor format, safe-projection exception, source mapping,
  sync boundary, privacy exclusions, and deferred surfaces.
- [x] Add a strictly non-mutating real-corpus smoke over the safe archive, with
  clean skipping when no archive is available.
- [x] Audit governing docs/help against the safe-evidence contract and exact
  structured mapping conventions.
- [x] Run the full suite and diff checks, then commit the completed docs/smoke
  slice.

Scope: governing docs, user-visible help, and real safe-archive acceptance.

Non-goals: live-database smoke, feature semantics changes, or deferred Cursor
surfaces.

## Slice 8 — Corpus-scale refresh and revision authority

- [ ] Make unchanged Cursor head discovery tens-of-milliseconds at corpus scale
  while preserving latest-reconstructible semantics after out-of-order arrival.
- [ ] Back off permanent draft/source-error retries without delaying changed or
  genuinely active/incomplete sessions indefinitely.
- [ ] Apply later authored Cursor payload revisions authoritatively without
  allowing inherited/stale copies to overwrite origin content or break pairing.
- [ ] Add scale, retry, message/tool mutation, copied-session, and order-
  independence tests; benchmark the real archive and no-change refresh.
- [ ] Run the full suite, diff/security scans, and three-reviewer final
  acceptance; fix every must-fix and should-fix finding.
- [ ] Confirm the repository is clean and every completed slice is committed.

Scope: rebuildable discovery bookkeeping and revision-aware canonical updates.

Non-goals: content-hash event identity, mutable pool revisions, prompt-text
classification, or the deferred Cursor surfaces above.

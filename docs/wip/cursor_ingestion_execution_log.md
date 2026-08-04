# Cursor ingestion execution log

Record only significant decisions discovered while executing the active
checklist. The pre-implementation research decisions are captured in the
checklist and will be documented in `formats/cursor.md`; routine implementation
choices do not belong here.

## 2026-08-04 — Slice 1: Safe Cursor snapshot projection

- Decision: The export firewall validates both field names and recursively
  constrained value shapes; arbitrary JSON is permitted only for the explicit
  transcript-evidence fields `params`, `rawArgs`, and `result`.
- Context: Independent security review found that a key-only allowlist could
  still pass future opaque data through nested metadata objects. Legacy Cursor
  bubbles also store hidden thought/summary text in ordinary `text` fields with
  separate structured flags.
- Alternatives considered: top-level allowlisting with a denylist for known
  nested secrets; copying all source JSON and deleting known unsafe fields.
- Rationale: A recursive positive schema fails closed under format drift and
  keeps the synced archive boundary auditable. `isThought` and
  `isSummarization` are retained while their text is discarded.
- Product/architecture impact: Cursor's database remains the upstream truth,
  while codebrain explicitly archives only a safe transcript projection.
- Reversibility: Easy to add newly reviewed fields; intentionally difficult for
  unknown fields to enter the archive accidentally.
- Follow-up: Immutable revision publication in Slice 2 must serialize with
  strict canonical JSON and retain the last settled snapshot on projection
  errors.

## 2026-08-04 — Slice 2: Immutable revision archive

- Decision: Archive publication is anchored to no-follow directory descriptors,
  while malformed header rows and sessions are tracked as independent pending
  work instead of invalidating an export sweep.
- Context: Independent review demonstrated that path-based creation could follow
  archive-internal symlinks outside the requested root, and that aggregate token
  validation let one malformed SQLite scalar suppress every valid session.
  Deep or non-UTF-8 JSON also crossed the intended typed failure boundary.
- Alternatives considered: path-level symlink checks under the process lock;
  rejecting the entire header scan; accepting Python decoder failures as fatal.
- Rationale: Descriptor-relative creation, create-only revision links, recursive
  directory fsyncs, bounded JSON validation, and per-session pending state keep
  the safe archive private, crash-reconstructible, and fail-open for unaffected
  sessions.
- Product/architecture impact: Only reconstructible immutable heads are source
  evidence; exporter bookkeeping remains local and rebuildable, and one damaged
  source record cannot erase or delay unrelated history.
- Reversibility: Moderate. The archive version is explicit, while stricter
  validation can be relaxed later without changing valid revision contents.
- Follow-up: The Slice 3 adapter must consume reconstructed snapshots rather
  than parsing delta segments directly.

## 2026-08-04 — Slice 3: Canonical Cursor adapter

- Decision: Treat inherited emitted bubbles as a contiguous prefix and fail
  closed if later structured timestamps cross back before the session creation
  boundary; resolve child spawn links only from a uniquely matching, emittable
  parent tool bubble identified by both call and child IDs.
- Context: Cursor reuses tool-call IDs, while copied bubbles preserve their
  bubble identity and authored timestamp. Independent review also found that a
  looser parent lookup could mint a spawn event ID for a bubble the adapter
  itself would reject or suppress.
- Alternatives considered: classify each bubble independently; link by tool
  call ID alone; retain a dangling spawn ID when parent evidence is malformed.
- Rationale: Timestamped bubble-component IDs deduplicate copied history, the
  prefix invariant avoids inventing non-linear placement semantics from corrupt
  ordering, and the exact structured predicate guarantees every resolved spawn
  can correspond to the canonical parent call event.
- Product/architecture impact: Cursor lineage remains evidence-first and uses
  structured identities only; ambiguous or malformed evidence is preserved as
  parent relation without a fabricated spawn link.
- Reversibility: Moderate. Additional structured lineage formats can be added
  later, but accepted event and spawn identities intentionally remain stable.
- Follow-up: Slice 5 will expose Cursor's existing structured origin evidence
  to provenance filtering without adding text classification.

## 2026-08-04 — Slice 4: Ingest, refresh, collection, and pool integration

- Decision: Collect every reconstructible Cursor revision segment, while
  ingesting only the latest reconstructible head; publish pool copies with a
  create-only link and retain either identical or conflicting concurrent
  arrivals without replacement.
- Context: Cursor archive heads contain payload deltas and cannot reconstruct on
  another machine without their predecessors. The generic collector's mutable
  log replacement path could also overwrite an immutable revision that arrived
  concurrently from sync.
- Alternatives considered: collect only heads; filename-glob every JSON file;
  reuse the generic `os.replace` and shrink-guard behavior.
- Rationale: Archive validation excludes databases, exporter state, locks,
  partial files, malformed segments, and symlinks. Separating all-segment
  replication from head-only ingestion preserves both remote reconstruction and
  authoritative placement replacement. File and directory fsyncs make a
  reported new revision crash-durable.
- Product/architecture impact: Live Cursor export occurs only when no raw-root
  override is supplied; custom and pool roots are pure consumers of sanitized,
  immutable evidence, and missing Cursor installations create no archive state.
- Reversibility: Easy at the integration layer; the append-only no-overwrite
  behavior is deliberately part of the pool durability contract.
- Follow-up: A broader dirfd/no-follow refactor for all collector destination
  trees remains optional inherited hardening, not a Cursor-specific blocker.

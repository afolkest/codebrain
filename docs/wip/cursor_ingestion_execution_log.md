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

## 2026-08-04 — Slice 5: Provenance and CLI integration

- Decision: Treat exact Cursor simulated/plan booleans and nonblank subagent
  kickoff IDs as direct structured origin evidence. For older subagents without
  an explicit kickoff field, classify the first authored placement by structured
  parent relation and sequence, regardless of text content or liveness.
- Context: Cursor represents control-plane input as ordinary user bubbles.
  Review found that a nonempty-text/live-only fallback could skip an empty or
  rolled-back kickoff and incorrectly move the verdict to a later human
  follow-up. The corpus also contains sessions with multiple explicit kickoff
  markers, each of which is direct event-level evidence.
- Alternatives considered: infer from prompt wording; classify every subagent
  user message; require the fallback event to be live/nonempty; degrade multiple
  explicit markers to unknown.
- Rationale: Source flags, canonical lineage, authored placement, and sequence
  are stable structured signals. Evidence fans out to inherited placements and
  remains a rebuildable overlay, while identical text and tool approval fields
  have no semantic effect.
- Product/architecture impact: Human-intent queries exclude Cursor control input
  by default without turning derived classifications into canonical transcript
  truth. Default raw grep searches only the sanitized Cursor archive, never the
  Cursor database or live home.
- Reversibility: Easy. Evidence kinds are independently replaceable and the
  force-sync CLI rebuilds them from canonical raw fields.
- Follow-up: None. Aggregate copy-consistency review found no Cursor raw/ref/
  tool-result pairing conflicts requiring a source-specific database rule.

## 2026-08-04 — Slice 6: Privacy and durability hardening

- Decision: Hidden and settled control fields fail closed on malformed types,
  and Cursor pool/grep roots use no-follow path validation. A live-database
  smoke cannot be called strictly non-mutating; documentation and a safe-archive
  smoke moved to Slice 7, while performance and revision-authority acceptance
  moved to Slice 8.
- Context: Independent review reproduced SQLite creating a `-shm` sidecar from
  a nominally read-only WAL connection, hidden text leaking when a thought flag
  changed type, and a symlinked pool destination redirecting writes and stale
  temp deletion outside the pool. The same review cycle found corpus-scale and
  payload-mutation issues that would make the documentation slice exceed a
  reviewable boundary.
- Alternatives considered: call the live-database smoke non-mutating because it
  issues no SQL writes; retain path-based symlink checks; finish every acceptance
  fix in one oversized slice.
- Rationale: Security-sensitive source controls and codebrain-owned path
  boundaries must fail closed. Safe archive heads can exercise real evidence in
  Slice 7 without touching live SQLite. Splitting the remaining documentation
  and correctness/performance work keeps each commit independently reviewable.
- Product/architecture impact: “Safe projection” means a constrained private
  evidence boundary, not secret-free transcript content; the pool remains
  private, and no default operation can escape into Cursor application state.
- Reversibility: Easy for smoke selection and validation strictness; moderate
  for descriptor-relative collection because it intentionally strengthens the
  durability contract.
- Follow-up: Slice 7 documents and smokes the final privacy boundary; Slice 8
  owns cheap head invalidation, retry backoff, and authored payload revision
  authority before final acceptance.

## 2026-08-04 — Slice 8: Corpus-scale refresh and revision authority

- Decision: Cache no-follow archive metadata independently per root and hashed
  session directory, keeping the validated selected head separate from the last
  selection deliberately handled by ingest. Advance a canonical Cursor session
  only when its validated `(revision, snapshotDigest)` rank exceeds the accepted
  watermark, in the same transaction as canonical and derived rows.
- Context: The real safe archive held about 1,746 sessions and 404 MiB. Full head
  reconstruction took roughly 6–8 seconds, while equivalent metadata scanning
  took 50–90 ms. Export also retried 431 unchanged failures on every read, mostly
  structured drafts. A real multi-revision tool bubble changed from loading to
  completed under the same bubble/time/tool identity; first-writer event merging
  lost that later result and could drop the authored placement when an inherited
  copy arrived first.
- Alternatives considered: hash payload content into event IDs; trust only a
  global archive-tree signature; use mutable pool heads; retain flat pending IDs
  and unconditional retries; let later inherited copies overwrite the event row.
- Rationale: Stable source identity preserves copy deduplication, origins,
  lineage, and call/result pairing. A root-scoped per-session cache preserves the
  existing latest-reconstructible rule after out-of-order arrival while limiting
  validation to changed chains. Typed structured retry categories avoid prompt
  heuristics and let header changes/full reconciliation bypass backoff.
- Product/architecture impact: Later authored Cursor revisions replace mutable
  content and refresh FTS/file references without splitting event identity.
  Inherited copies keep placements but cannot overwrite authored evidence;
  equal/lower remote heads cannot regress canonical state. Normal unchanged
  reads open no archive JSON. Cache and retry state remain rebuildable local
  bookkeeping, not transcript truth and not synced evidence.
- Acceptance evidence: The final real archive contained 1,747 sessions and 1,756
  revision files. Seven hot metadata scans had an 88 ms median (85–91 ms range),
  and seven warm explicit-root refreshes had a 116 ms median (109–120 ms range),
  processed zero files, and opened zero revision JSON. Full validation of all
  1,747 heads took 5.9 seconds; a separate cold canonical ingest populated
  134,828 events/placements without errors. Synthetic scale and failure tests
  cover warm restart, one-session invalidation, out-of-order predecessor arrival,
  in-place repair/fallback, root isolation, malformed/versioned cache state,
  cache/write rollback, concurrent watermark races, typed retry branches, and
  both equal-rank and authored/inherited ingest orders.
- Reversibility: The discovery cache, accepted-head table, and exporter retry
  state can be dropped and rebuilt from immutable revisions. The authority rule
  is source-specific but preserves all historical revision evidence in the raw
  archive.
- Follow-up: Revisit the validator version only when archive-chain validation
  semantics change. Deferred Cursor CLI/ACP, Background Agent, orphan-history,
  and ancillary-binary surfaces remain outside this execution cycle.

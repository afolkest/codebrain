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

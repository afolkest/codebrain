# Format notes — Cursor

This reference is reverse-engineered from Cursor state observed on local macOS
installations and from the behavior enforced by codebrain's exporter, archive,
adapter, and provenance overlay. It is **empirical, not an official Cursor
specification**. Cursor can change these private stores without notice, so the
implementation detects structured capabilities and fails closed on ambiguous or
incomplete data instead of guessing from prompts or version numbers.

Cursor differs from Claude, Codex, and pi in one important way: its canonical
local conversation store is a live SQLite database that also contains unrelated
application state. That database is never a codebrain raw root and is never
synced. A narrow, codebrain-owned JSON projection is the raw evidence boundary
for ingest, collection, and grep.

## Source location and read contract

The supported macOS database is:

```text
~/Library/Application Support/Cursor/User/globalStorage/state.vscdb
```

The exporter opens it through a SQLite URI with `mode=ro&cache=private`, enables
`PRAGMA query_only=ON`, and reads each projection in a transaction. It does not
use `immutable=1`: a running Cursor may have committed conversation data in the
WAL, and an immutable reader would ignore normal live-database coordination.
The exporter issues no mutations and writes transcript output only to
codebrain's safe archive; SQLite remains responsible for live-reader locking and
WAL coordination.

A supported database has these structured surfaces:

- `composerHeaders`, keyed by `composerId`, with at least `createdAt`,
  `lastUpdatedAt`, `isArchived`, `isSubagent`, `recency`, `checkpointAt`, and a
  JSON `value` column.
- `cursorDiskKV(key, value)`, whose relevant UTF-8 JSON-object keys are
  `composerData:<composerId>` and, for the modern layout,
  `bubbleId:<composerId>:<bubbleId>`.

Header rows are the incremental change signal, while `composerData:*` also
finds historical data-only composers during a periodic full reconciliation.
Missing tables/columns, invalid JSON, non-finite numbers, invalid required
header booleans, and unsupported or ambiguous conversation capabilities are
errors. Ill-typed optional allowlist fields are omitted.
The source `_v` integer is retained as evidence when present; it is not the
primary format dispatch mechanism.

## Two observed conversation capabilities

The projector recognizes the shape of `composerData:<composerId>` rather than
assuming a Cursor release range.

### Embedded conversations

An embedded composer has a `conversation` array. Each entry is the complete
bubble object and the array is the logical order. A bubble identity comes from
`bubbleId`, then legacy `id`; if neither exists, the projection assigns the
deterministic position-local identity `embedded-<index>`. Bubble `type` must be
the integer `1` (user) or `2` (assistant), and identities must be unique within
the composer.

### Separate-bubble conversations

A separate-bubble composer has a `fullConversationHeadersOnly` array. Its entries
provide ordered `bubbleId` values and may carry `type` and `createdAt`; the full
payload is read from `bubbleId:<composerId>:<bubbleId>`. Every ordered entry must
resolve, and summary identity/type must agree with the stored payload.

Some observed copied placements refer to a bubble whose payload remains under a
different composer key. The fallback is deliberately narrow: it requires an
exact structured timestamp, compatible type, matching stored bubble identity,
and exactly one candidate across `bubbleId:*:<bubbleId>`. A bare bubble UUID is
not globally unique enough. Zero or multiple candidates makes the snapshot
incomplete.

A composer exposing both arrays is ambiguous and rejected. A composer exposing
neither is unsupported. Unordered `bubbleId:*` records that do not participate
in a supported ordered composer are not reconstructed.

## Ordering, timestamps, and settled-state gating

The ordered array is authoritative. The exporter does not sort bubbles by text,
timestamp, KV key, or database row order. Each projected order entry contains
`bubbleId`, `type`, the best valid `createdAt`, and a safe bubble `payload`.
Timestamp preference is bubble `createdAt`, ordered-summary `createdAt`, then
`timingInfo.clientRpcSendTime`, `clientEndTime`, or `clientSettleTime`. Numeric
times must look like Unix milliseconds in 2000–2100; ISO timestamps must parse
to a year in that range. Invalid or absent times remain untimed rather than
being invented from ordering.

Publishing is allowed only for settled composers. The projector defers a
session when it has active `generatingBubbleIds`, queued `queueItems`,
`isContinuationInProgress`, a draft `status="none"`, or any nonterminal status.
Supported terminal statuses are `completed` and `aborted`; missing status is an
observed historical convention but is rejected for source version 14 or later.
When present, `generatingBubbleIds` and `queueItems` must be arrays and
`isContinuationInProgress` must be an exact boolean. Malformed controls fail the
session closed rather than being treated as falsey.
An incomplete or unsettled read preserves the last known-good archive revision
and remains pending for a later retry.

## Safe projection version 1

The live database contains far more than transcript evidence. The projection is
a positive recursive allowlist, not a denylist or a dump of rows. Its envelope
is:

```text
projectionVersion: 1
composerId: <source composer identity>
sourceCapability: embedded | separate-bubbles
sourceVersion: <optional integer _v>
session: <allowlisted session metadata>
order: [{bubbleId, type, createdAt?, payload}, ...]
```

Allowlisted session metadata includes source timestamps/status/title fields;
archived/subagent header flags; a bounded workspace URI shape; tracked repo and
branch metadata; agent/worktree location metadata; child composer IDs; and the
structured `subagentInfo` identities described below. Absent values, empty
containers, and most ill-typed optional fields are omitted; false booleans and
other valid scalar values remain evidence. Security-sensitive hidden-content
flags and settled-state controls are exact-typed and fail the session closed.

Allowlisted bubble scalars are the bubble identity/type/version, visible `text`,
source timestamps and request IDs, plan/simulation control fields, hidden-content
flags, and `subagentSpawnTaskToolCallId`. The projection may also retain:

- `simulatedMessageMetadata`: `taskId`, `title`.
- `timingInfo`: the four allowlisted client timing fields.
- `context.selectedImages`: image identity/path/load time, dimensions, and the
  structured `addedWithoutMention` flag. Image bytes and the rest of context
  are not copied.
- `toolFormerData`: tool identity/status/index/error/user decision; `params`,
  `rawArgs`, and `result` as lossless strict JSON transcript evidence; and a
  small allowlist of `additionalData` such as subagent composer identity,
  lifecycle status/timing, file-match counts/path/pattern, background shell ID,
  or task ID.

`params`, `rawArgs`, and `result` are the deliberate exception to scalar-only
metadata because their arbitrary JSON shape is the actual tool call/result.
All retained JSON must be finite, strictly UTF-8 encodable, bounded in nesting
and node count, and serializable as canonical JSON.

If `isThought` or `isSummarization` is true, the bubble's text is removed at the
projection firewall. The control flag remains so the adapter can suppress the
bubble. Hidden thought/summary text is therefore neither archived nor emitted
as a canonical event.

The projection excludes all other database tables and KV keys, credentials,
encryption material, settings, extension/global application state, arbitrary
editor context, binary attachments, and unknown nested fields. A newly observed
field stays excluded until explicitly reviewed and allowlisted.

This boundary prevents unrelated application secrets from being swept up, but
it cannot make transcript content non-sensitive: visible messages and lossless
tool arguments/results can themselves contain paths, source code, command
output, or secrets supplied during a session. Treat the safe archive and pool as
private transcript storage.

## Structured tools and references

A canonical tool event requires nonempty structured `name` and `toolCallId` fields.
One visible bubble can emit, in order, its message, a tool call, and a tool
result. A result is present when `result` exists, `error` is a string, or status
is terminal (`completed`, `error`, or `cancelled`). The result points directly
to the call event created from the same bubble; pairing never depends on nearest
text or position.

Tool call text and refs are deterministic views of projected arguments:

- `run_terminal_command_v2` and `run_terminal_cmd` extract only `command` into
  `refs.commands`.
- `read_file_v2` extracts `targetFile`/`effectiveUri`; `read_file` and `read`
  extract `relativeWorkspacePath`/`targetFile`.
- `edit_file_v2`, `edit_file`, `search_replace`, `delete_file`, `apply_patch`,
  `reapply`, and `write` extract `relativeWorkspacePath`.
- `ripgrep_raw_search` and `grep` extract `path`; `glob_file_search` extracts
  `targetDirectory`; `list_dir` extracts `directoryPath`; `read_lints` extracts
  each string in `paths`.
- Other tools retain their name and a bounded rendering of structured args but
  do not acquire inferred file/command refs. Unknown `path` or `command` keys do
  not become refs merely because of their names.

The adapter checks `params` before `rawArgs`, parsing a JSON-encoded string when
possible. Result rendering similarly prefers known result text fields
(`output`, `contents`, `markdown`, `content`, `resultForModel`) and otherwise
uses deterministic JSON. Tool names and argument values are never used to
classify session lineage or user intent.

## Lineage and user-message provenance

`subagentInfo` may retain `parentComposerId`, root-parent and request IDs,
conversation length at spawn, subagent type/name, `toolCallId`, tool-call history,
and structured conversation-start indexes. A child becomes a canonical
`relation="subagent"` only when it has a nonempty structured parent composer ID;
the parent session is `cursor:<parentComposerId>`.

The exporter attempts to resolve a spawn event only when the child's structured
parent composer ID and tool call ID identify exactly one visible parent bubble
whose tool `additionalData.subagentComposerId` equals the child composer ID. A
valid bubble timestamp is also required. If the join is absent or ambiguous,
the parent relation remains evidence but `spawn_event_id` is unset. No prompt
wording, title, header flag, or naming convention is used to infer lineage.

Cursor also stores control-plane input as ordinary user bubbles. The provenance
overlay records separate evidence from structured fields only:

- `isSimulatedMsg is true` → `cursor_simulated`, `master_control`.
- `isSimulatedMsg is false` plus a nonzero integer `simulatedMsgReason` → the
  same evidence kind with `unknown`, because the fields contradict.
- `isPlanExecution is true` → `cursor_plan_execution`, `master_control`.
- A nonblank `subagentSpawnTaskToolCallId` → `cursor_subagent_kickoff`,
  `master_control`.
- For a structurally linked subagent with no explicit kickoff field on any
  authored user message, the first authored user-message placement by `seq`
  supplies legacy `cursor_subagent_kickoff` evidence, regardless of text content
  or liveness.

Evidence fans out to every session placement of a deduplicated event. Message
text is not read for any of these classifications.

## Immutable revision archive

The default safe root is:

```text
~/.codebrain/cursor-raw/
  sessions/<sha256(composerId)>/revisions/
    <20-digit-revision>-<snapshotDigest>.json
```

Archive JSON is strict UTF-8 canonical JSON with sorted keys. Each segment has
`archiveVersion`, `composerId`, a monotonic `revision`,
`previousSnapshotDigest`, `snapshotDigest`, a logical `snapshot`, and
content-addressed `payloads`. The logical order holds `payloadHash` references;
a new revision stores only payload hashes not already available through its
predecessor chain. The snapshot still describes the complete current logical
order, so truncation or deletion is an explicit new state rather than an
append-only assumption.

Revision files are create-only, private regular files published atomically
under a process lock and fsynced directory tree. A revision is discoverable only
when its filename, composer-directory hash, canonical digests, payload hashes,
revision number, and predecessor chain validate. Out-of-order remote arrivals
remain invisible until their chain is reconstructible. Ingest reads only the
newest reconstructible head per composer; collection copies every
reconstructible segment needed to reproduce each chain.

`exporter-state.json`, `.export.lock`, and temporary `.part` files are local
export machinery, not transcript evidence and not collection inputs. Header
tokens drive cheap incremental exports, while a full composer reconciliation
becomes due once the previous full pass is 24 hours old. Identical logical
snapshots publish no revision. Exporter state version 2 stores only typed retry
bookkeeping: `active` and `incomplete` sessions use exponential 60-second to
one-hour delays; `draft`, `absent`, and `source-error` sessions retry daily.
A changed valid header token or full reconciliation bypasses the delay. Legacy
pending IDs migrate due immediately, malformed or far-future retry state fails
open, stale-part scans are hourly, and an unchanged state file is not rewritten.

Normal refresh does not reconstruct every chain. It computes no-follow metadata
signatures independently for each 64-hex session directory and persists the
validated selection plus the last selection deliberately handled by ingest in
the rebuildable local SQLite cache. An unchanged session opens no revision JSON;
a new, deleted, repaired, or out-of-order segment invalidates only that session.
The uncached discovery path remains the verifier used by full ingest and
collection. Cache rows are scoped by absolute archive root, so local and remote
roots with the same session hash cannot collide.

## Mapping to the canonical schema

| canonical field | Cursor mapping |
|---|---|
| source/session | `source="cursor"`; `session_id="cursor:<composerId>"` |
| title | allowlisted session `name` |
| cwd | workspace URI `fsPath`, falling back to agent `worktreePath` |
| repo | first structured tracked-repo `repoPath` |
| actor | bubble type 1 → user; type 2 → assistant; result → tool |
| event type | visible text → `message`; structured tool → `tool_call`; terminal tool evidence → `tool_result` |
| raw | the safe projected bubble payload, never a live database row |
| ordering | projected composer order; emitted message/call/result are chained in that order |
| tool pairing | result `tool_call_event_id` is the call emitted from the same bubble |
| lineage | structured parent composer ID and, when uniquely resolved, parent spawn call |

Timed event IDs are copy-invariant:

```text
cursor:<percent-encoded-bubbleId>:<epoch-ms>:message
cursor:<percent-encoded-bubbleId>:<epoch-ms>:call
cursor:<percent-encoded-bubbleId>:<epoch-ms>:result
```

Copied prefixes retain bubble identity and authoring time, so these IDs dedupe
the same source event across sessions. A timed event is inherited exactly when
its event time precedes the current composer's `createdAt`; inherited events
must form one contiguous prefix. Their `origin_session_id` is left unset, while
authored events point to the current session. The final inherited event is the
session branch point.

Payload content can legitimately change under one stable identity as Cursor
settles a tool result or revises a bubble. Each parsed archive head therefore
carries `(revision, snapshotDigest)`, and canonical ingestion accepts only a
greater total rank for that Cursor session. Later authored content from the same
origin replaces the event row and refreshes FTS/file references while preserving
call/result IDs and pairing. Inherited copies contribute placements but never
overwrite authored content; if they arrive first, they remain provisional until
the authored session arrives. Equal or lower heads from another pool root cannot
regress the session.

Untimed legacy events cannot safely dedupe across composers and use a
session-scoped identity instead:

```text
cursor:<percent-encoded-composerId>:<percent-encoded-bubbleId>:untimed:<kind>
```

They use the session creation time for display when available (otherwise the
Unix epoch), but are never marked inherited. Every canonical placement is
linear and live; the adapter does not infer abandoned branches from text.

User bubbles with string text emit a message even when empty. Assistant text
emits only when nonblank. A visible tool can emit independently of message text.
Thought and summarization bubbles emit nothing.

## Collection, sync, and grep boundary

Normal local refresh, full ingest, and collection first refresh the safe archive
when the default Cursor database exists. Supplying an explicit Cursor raw root
(including a pool root) bypasses live export. If Cursor is absent, codebrain does
not create a safe archive merely to represent an empty source.

Only immutable revision JSON may enter the pool, under:

```text
<pool>/raw/<machine>/cursor/sessions/<hash>/revisions/<revision>.json
```

Cursor revisions are validated and canonicalized before collection. Pool writes
are create-only: an identical existing file is unchanged, while a byte conflict
is preserved in place and reported as an error. In particular, never sync
`state.vscdb`, its WAL/SHM files, Cursor's application-support directory, archive
locks, exporter state, or temporary files.

Default raw grep roots include the safe `~/.codebrain/cursor-raw` archive and
remote pool roots. They never include the live database or `~/.cursor`. Explicit
paths remain an operator choice, but the default privacy boundary is the safe
projection.

## Deferred and unsupported surfaces

These surfaces are intentionally outside the current canonical adapter:

- Cursor CLI/ACP protobuf logs or protocol traffic. They require a separately
  verified decoder and identity/ordering model before they can be evidence.
- Remote Background Agent storage and synchronization. Local composer metadata
  may describe a background task, but codebrain does not fetch remote history.
- Historical unordered/orphan `bubbleId:*` records with no complete supported
  composer order. They are not silently timestamp-sorted or attached by text.
- Ancillary `~/.cursor/projects` material such as derived agent transcripts,
  tool-output files, terminal state, MCP state, logs, and indexes. These can be
  lossy, model-view, duplicated, or credential-bearing and are not canonical
  conversation truth.
- Other operating-system database locations and future private schemas until
  they are observed, documented, and covered by tests.

Schema drift should produce an explicit skip/error while retaining the last
good immutable revision. Extending support means adding structured capability
handling and an allowlist test; free-text prompt patterns are not a fallback.

# Format notes — pi

Reverse-engineered from real logs on this machine (`~/.pi`, 49 top-level sessions + 18 sub-agent transcripts, ~Apr–Jun 2026). Treat as empirical, not spec — it drifts, and pi is explicitly multi-provider so provider-dependent fields vary.

## Headline: how pi differs from Claude & Codex

- **Tree like Claude, but `parentId` (8-hex short ids), not `parentUuid`.** Whole assistant messages are **one record** (multi-block `content` array), not one-block-per-line like Claude.
- **Three roles:** `user`, `assistant`, **`toolResult`** (a first-class role, not a user-record flavor).
- **Reasoning is readable plaintext** (`thinking` blocks) — the opposite of Codex's encrypted reasoning.
- **Rich per-message metadata** no other source has: `usage` (tokens **+ dollar cost**), `model`, `provider`, `stopReason`, `responseId`.
- **Forks are rollback-only** (corpus: 66/66 fork-children are `user`). Parallel tool calls do **not** fork. Simpler than Claude's two-fork-types.
- **Resume/branch copies history into a NEW file** (`parentSession` link + copied prefix, original ids retained) → **heavy cross-file duplication; dedup is mandatory** (per-lineage). This is the hard part, and it's unlike both Claude (0 cross-file dup) and Codex (resume appends to same file).
- **Sub-agents are separate `session.jsonl` files** under `<session>/<runId>/run-<i>/`, with **no raw-record duplication** in the parent (like Codex, unlike Claude's inline `isSidechain` copies).

## Location & layout

- Transcripts: `~/.pi/agent/sessions/<encoded-cwd>/<ts>_<sessionUuid>.jsonl`
  - Filename = `<localtime-ts>_<sessionUuid>` where `sessionUuid` is a **UUIDv7** (sortable, embeds creation time). The `<ts>` prefix is **local** time; record `timestamp`s are UTC `Z`.
  - `<encoded-cwd>` = cwd bracketed and separated by `-`, e.g. `/Users/example/tmp` → `--Users-example-tmp--`. **Lossy** (a real `-` is indistinguishable). Don't decode it — read the `cwd` field in the `session` record instead.
- A session **may also have a sibling directory** `<ts>_<sessionUuid>/` next to the flat file, holding sub-agent transcripts: `<sessionUuid>/<runId>/run-<i>/session.jsonl` (see Sub-agents). A "session" is therefore potentially the flat file **plus** that tree. The collector must capture both.
- Per-cwd sibling: `subagent-artifacts/` (shared scratch for sub-agent output; holds a `.last-cleanup` marker).
- Top-level sidecars (not transcripts): `~/.pi/agent/run-history.jsonl` (global run log, `{agent,duration,status,task,ts}`), `settings.json`, `auth.json` (**secrets — do not read**).

## Schema version — pi stamps it

The `session` record carries `version` (integer schema version). Entire corpus is **`version: 3`**. Unlike Codex (9 months of silent drift), pi gives you an explicit version number — **branch the adapter on `session.version`**, and treat unknown versions as a hard signal to re-inspect.

## Record types (whole corpus)

| type | count | in tree? | role |
|---|---|---|---|
| `message` | 5207 | yes (`parentId`) | conversation (user/assistant/toolResult) |
| `thinking_level_change` | 87 | yes | UI state: reasoning effort changed |
| `model_change` | 50 | yes | model/provider switched mid-session |
| `session` | 49 | root | session meta (one per file) |
| `compaction` | 5 | yes | compaction boundary marker |
| `custom_message` | 1 | yes | injected UI notice (e.g. `subagent-notify`) |

**Everything is in the `parentId` tree** — including control records. `model_change`/`thinking_level_change` chain into the same `parentId` line as messages (a message's `parentId` often points at a `thinking_level_change`). This is *unlike* Claude, where sidecar control records are keyed only by sessionId. To reconstruct the conversation, walk `parentId` and **skip/segregate non-`message` records** as state-change events.

## Envelope

Top-level keys: `type`, `id`, `parentId`, `timestamp`, and (for `message`) `message`.

- **`id` is an 8-hex short id (32-bit)** — unique only *within a session lineage*, NOT global. (Contrast: `session.id` is a full UUIDv7.) This matters for dedup (below).
- `parentId: null` = a root. Files normally have a few roots (`session`, the first `model_change`) before the chain joins up.

### The `message` record

`message: { role, content[], timestamp, … }`. Note the **nested `message.timestamp`** in addition to the top-level one.

- **role `user`** — `content` is an array of blocks (typically one `text`). Human prompts.
- **role `assistant`** — rich metadata:
  `model`, `provider`, `api`, `responseId`, `stopReason`, `usage`, sometimes `errorMessage`, `diagnostics`.
  - `usage`: `{input, output, cacheRead, cacheWrite, totalTokens, cost:{input,output,cacheRead,cacheWrite,total}}` — per-message **token counts and dollar cost**. Nothing else in the corpus offers this.
  - `stopReason` ∈ `stop` (477), `toolUse` (1809), `aborted` (38), `error` (3).
- **role `toolResult`** — `content[]`, plus `toolCallId`, `toolName`, `isError`. Carries tool output. `isError` is an explicit flag (110 true / 2248 false).

### Content blocks

| block | fields | where |
|---|---|---|
| `text` | `{type, text}` | any role |
| `thinking` | `{type, thinking, thinkingSignature?}` | assistant |
| `toolCall` | `{type, id, name, arguments}` | assistant only |
| `image` | `{type, data, mimeType}` | user / toolResult (base64 inline, e.g. `image/png`) |

- **Reasoning is readable**: the `thinking` field holds plaintext reasoning; `thinkingSignature` is an opaque signature (present on 1209/1221 blocks). Big contrast with Codex (encrypted ≥2026-04).
- **Images are inline base64** (`data` + `mimeType`) → these records can be large and bloat files.
- `toolResult` content can be **multimodal** (`text` + `image`).

## Tool pairing

- `toolCall.id` ↔ `toolResult.message.toolCallId`; `toolName` echoes the call's `name`.
- **`toolCall.id` is a compound, provider-shaped string**: here always `call_<openai-call-id>|fc_<function-call-item-id>` (OpenAI Responses API, `|`-joined). An Anthropic-backed session would carry a different shape — treat the id as opaque, match it verbatim.
- **Parallel tool calls** = multiple `toolCall` blocks in **one** assistant message; their `toolResult`s are **separate records chained linearly** (each parents the next), **not** forked. So parallel tools never create a `parentId` fork.

## Forks — rollback only (simpler than Claude)

A `parentId` with >1 child is a fork. **Corpus-wide, every fork child is a `user` message (66/66).** The only fork cause in pi is **rollback**: the user rewound/edited and resent, diverging from a shared `assistant` parent. There is **no parallel-tool fork** (parallel tools chain; see above).

Reconstruction:
1. **Current tip** = the live leaf (latest-timestamp childless `message`, or the conversation tip you’re rendering).
2. A rollback fork's losing subtree (not containing the tip) is **abandoned** — exclude for "final conversation," keep tagged for "full history incl. rolled-back attempts."
3. Walk `parentId` from the tip; group control records as state events, not turns.

## Compaction — append-only, full history retained

A `compaction` record: `{id, parentId, timestamp, firstKeptEntryId, summary, tokensBefore, details, fromHook}`.
- Parented into the existing tree (append-only); **all pre-compaction messages remain** at full fidelity.
- `firstKeptEntryId` = id of the first message kept across the boundary (everything before it is summarized by `summary` but physically retained).
- `tokensBefore` = context size that triggered it; `fromHook` = hook-triggered vs auto/manual.
- Tag `summary` as synthetic (not a human turn).

## Resume / branch — cross-file copy (the hard part)

Resume or branch does **not** append to the same file (unlike Codex) and does **not** avoid duplication (unlike Claude). Instead it **creates a NEW file** (new `sessionUuid`) and:

1. Sets `session.parentSession` = **absolute path to the source `.jsonl`** (present on the 3 branched sessions in the corpus).
2. **Copies the parent's then-live conversation as a contiguous prefix**, preserving the **original 8-hex `id`s**, then continues with fresh records.

Verified: one child had 290 messages = **273 inherited (contiguous block, shared ids with parent) + 14 new (tail)**. If the parent was compacted, only the post-compaction *kept* messages are inherited (the child lacked the parent's pre-compaction head). One parent can spawn **multiple children** (e.g. two children stamped the same second = a branch-into-two).

**Consequences for ingest:**
- **Cross-file dedup is mandatory.** ~273 message ids were shared across one parent+children group.
- **Dedup must be lineage-scoped**, not global: the 8-hex `id` is only unique *within* a lineage, so key on **(lineage-root sessionUuid, message id)**. Build the lineage by following `parentSession` to the root.
- `parentSession` is a **local absolute path** → won't resolve after Syncthing replication to another machine. **Extract the parent `sessionUuid` from the filename** (`…_<uuid>.jsonl`) rather than trusting the path.

## Sub-agents

- Spawned via a `subagent` **tool call** (`name:"subagent"`) — a multi-task dispatcher. Args seen: `action`, `tasks`, `concurrency`, `context`, `cwd`, `clarify`, `async`, `agentScope`. (`action`-only calls query the agent registry.)
- Stored as `<session>/<runId>/run-<i>/session.jsonl`:
  - `runId` (8-hex) = `details.runId` in the `subagent` **toolResult** (`details.mode` = `parallel|…`). This is the parent→sub link.
  - `run-<i>` indexes the **i-th parallel task** in that one dispatch (5 tasks → `run-0..run-4`), **not** retries.
  - Each `session.jsonl` is a **full independent transcript** with its **own** `sessionUuid`, **no `parentSession`**, and **zero id-overlap with the parent** (no raw-record duplication — the parent only stores the sub-agent's *summary* in the toolResult).
- Sub-agents frequently **write output to repo files**; the toolResult lists the paths (e.g. `…/review-latest-commit/correctness-regressions.md`).
- `custom_message` `{customType:"subagent-notify", display:true, content}` = async/background sub-agent completion notice injected into the parent tree (synthetic).
- Builtin agent roles observed: `context-builder`, `delegate`, `oracle`, `planner`, `researcher`, `reviewer`.

Modeling choice: treat each `run-<i>/session.jsonl` as a child session, linked to the parent via `(runId, i)` and the spawning `subagent` toolCall id. No dedup needed against the parent.

## Files touched — clean

Extract directly from `toolCall.arguments` (assistant `message` content):
- `read` → `{path, limit?, offset?}`
- `write` → `{path, content}`
- `edit` → `{path, edits[]}`
- `bash` → `{command, timeout}` — **opaque** to file tracking (same gap as Claude/Codex; no structured file record for shell-side mutations).
Sub-agent file writes surface as paths in the `subagent` toolResult text.

## Interruptions & errors — explicit

Unlike Claude/Codex (mostly inferred), pi records these directly:
- assistant `stopReason:"aborted"` + `errorMessage:"Operation aborted"` (user interrupt).
- assistant `stopReason:"error"` + `errorMessage` (e.g. `"WebSocket closed 1006"`, `"terminated"`).
- toolResult `isError:true` with the error text in `content`.

## Provider drift — the main adapter risk

This corpus is **100% `provider:"openai-codex"` / `api:"openai-codex-responses"`**. pi is explicitly multi-provider (`model_change.provider`, per-message `provider`). A different backend would change:
- `toolCall.id` shape (the `call_…|fc_…` form is OpenAI-Responses-specific).
- possibly `thinking` block fields and `usage` shape.

So: **branch on `session.version` for schema drift, and on `message.provider` for provider-shaped fields.** Don't hardcode the `|`-split id form.

## Mapping → canonical envelope

| canonical event | from pi |
|---|---|
| `actor` | `message.role` → user/assistant; `toolResult` → actor=tool |
| `type` message/tool_call/tool_result | block `type`: `thinking`→message (flagged), `text`→message, `toolCall`→tool_call, role `toolResult`→tool_result |
| `seq` | live-branch order via `parentId` walk, tie-break `timestamp` |
| `ts` | top-level `timestamp` (UTC) |
| `refs.files` | `toolCall.arguments.path` for read/write/edit |
| `refs.commands` | `toolCall.arguments.command` for bash |
| session meta | `session.id` (UUIDv7), `cwd`, `version`; lineage via `parentSession` (resolve to root uuid) |
| metrics (extension) | `message.usage` (tokens + cost), `model`, `provider`, `stopReason` |
| `raw` | the whole original record |

## Resolved / still open

Resolved by the corpus sweep:
- ✅ **Forks** are rollback-only (assistant→multiple-user); parallel tools chain, never fork.
- ✅ **Compaction** is append-only; full pre-compaction history retained (`firstKeptEntryId`+`summary`).
- ✅ **Resume/branch** = new file + `parentSession` + copied-prefix with original ids ⇒ **lineage-scoped dedup mandatory** (key on root-uuid + 8-hex id; get parent uuid from the filename, not the local path).
- ✅ **Sub-agents** = separate `run-<i>/session.jsonl` under `<session>/<runId>/`, no raw dup, linked via `details.runId` + spawning toolCall.
- ✅ **Files touched** extractable from `toolCall.arguments.path`; bash is the known blind spot.
- ✅ **Interruptions/errors** explicit via `stopReason`/`errorMessage`/`isError`.

Still to confirm (minor / out-of-corpus):
- **Non-OpenAI providers**: `toolCall.id` / `thinking` / `usage` shapes under an Anthropic (or other) backend — none present here.
- **Higher schema versions** (`version > 3`): unseen; the version field is the tripwire.
- **`custom_message` customTypes** beyond `subagent-notify`.
- **`details` payload** of `compaction` and of the `subagent` toolResult (beyond `runId`/`mode`) — not fully enumerated.
- **`diagnostics`** field on assistant messages (rare; contents not inspected).

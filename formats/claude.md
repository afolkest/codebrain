# Format notes — Claude Code

Reverse-engineered from real logs on this machine (`~/.claude`, Claude Code `version` per-record). Treat as empirical, not spec — it drifts.

## Location & layout

- Transcripts: `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`
  - Main transcript = one flat JSONL file; filename (minus `.jsonl`) = `sessionId`.
  - A session **may also have a sibling directory** `<sessionId>/` next to the flat file, containing `subagents/` (see Sub-agents). So a "session" is potentially `<sessionId>.jsonl` **plus** `<sessionId>/subagents/*.jsonl`. The collector must capture both.
  - `<encoded-cwd>` = the cwd with `/` → `-`. **Lossy** (a real `-` in a path is indistinguishable from a separator). Don't decode it — read the `cwd` field inside records instead.
- Other relevant top-level dirs/files (later sources, not transcripts):
  - `~/.claude/history.jsonl` — global prompt history.
  - `~/.claude/file-history/` — actual file backups behind the checkpoint/undo system; codebrain does not collect these file bodies.
  - `~/.claude/sessions/`, `tasks/`, `plans/`, `todos`-like state.

### Record types seen across the whole corpus (160 files)
`assistant`, `user`, `progress`, `attachment`, `file-history-snapshot`, `last-prompt`, `system`, `permission-mode`, `ai-title`, `mode`, `queue-operation`, `custom-title`, `agent-name`, `pr-link`. Of these only `assistant`/`user`/`attachment`/`system` carry the `uuid`/`parentUuid` tree; the rest are sidecar state.

## The file is an interleaved event log, not a message list

Each line is one JSON record. `type` partitions them into three groups:

**1. Conversation records** (have `uuid` + `parentUuid` → form a tree):
- `user` — two flavors:
  - human prompt: `message.content` is a **string**, has `promptId`.
  - tool result: `message.content` is `[{type:"tool_result", tool_use_id, content, is_error?}]`, has top-level `toolUseResult` (structured), `sourceToolAssistantUUID`. No `requestId`.
- `assistant` — `message.content` is a **single-element** array; one block per record (`thinking` | `text` | `tool_use`). All records from one model response **share `requestId`**.
- `attachment` — pasted/attached content tied to a user turn; shares the user record's timestamp.

Common fields on these: `uuid`, `parentUuid`, `sessionId`, `timestamp` (ISO-8601 UTC `Z`), `cwd`, `gitBranch`, `version`, `userType`, `isSidechain`, `entrypoint`, `type`, `message`.

**2. Sidecar control records** (keyed only by `sessionId` + `type`, NOT in the tree):
- `ai-title` (`aiTitle`) — auto title, re-emitted as it evolves.
- `mode` (`mode`), `permission-mode` (`permissionMode`) — UI state.
- `last-prompt` (`lastPrompt`, `leafUuid`) — snapshot of the conversation **leaf** at each prompt. `leafUuid` is the tree tip; it moves on every prompt and on rollback.
- `system` (`subtype:"turn_duration"`, `durationMs`, `messageCount`, `isMeta`) — end-of-turn timing marker. (Has uuid/parentUuid and sits in the tree as a turn delimiter.)

**3. File tracking:**
- `file-history-snapshot` (`messageId`, `snapshot.trackedFileBackups`, `isSnapshotUpdate`) — checkpoint metadata; backups live in `~/.claude/file-history/`.

## Tree model & turn structure

Records link child→parent via `parentUuid` (root = `parentUuid: null`). A logical turn looks like:

```
user(string) [→ attachment…] → assistant(thinking) → assistant(text)
   → [assistant(tool_use) → user(tool_result)]*  → system(turn_duration)
```

- **A model response is split across records** (thinking, text, each tool_use = separate lines). **Regroup by `requestId`** to reassemble one assistant message.
- **Pair tool_use ↔ tool_result by id**, NOT by tree position: `tool_use.id` (`toolu_…`) == `tool_result.tool_use_id`.

## Forks — the hard part. Two causes, opposite meaning.

A `parentUuid` with >1 child is a fork. **Distinguish before reconstructing:**

| | Rollback fork | Parallel-tool fork |
|---|---|---|
| children | ≥2 `user` records with **string** content + distinct `promptId` | share one assistant **`requestId`**; a `tool_use` sibling + its `tool_result` |
| meaning | user rewound/edited & resent — earlier branch **abandoned** | one response with N tool calls; both sides **really happened** |
| signature | fresh human prompts diverging from same parent | off-path `tool_result` becomes a dead leaf |

Observed examples (this session):
- Rollback: parent `d3effced` → `144f6e98` (live? no) vs `a5a48a56`. Earlier branch `144f6e98→…→d05a033a` is a dead leaf; later branch is live.
- Parallel: parent `90dfddde` (tool_use, `req_011CbdNz`) → sibling tool_use `5791145f` (same req) + tool_result `d4d97282` (dead leaf).

### Reconstruction algorithm
1. Find the **current tip** = the leaf reachable as the last `last-prompt.leafUuid`, or the latest-timestamp childless conversation node.
2. **Live branch membership:** a rollback fork's losing subtree (the one not containing the tip) is *abandoned* — exclude for "final conversation," keep tagged for "full history incl. rolled-back attempts."
3. **Do not** reconstruct content by naive parent-walk from the tip — in parallel-tool regions it drops off-path sibling `tool_result`s. Instead: collect live `tool_use` blocks, then attach **every** `tool_result` whose `tool_use_id` matches, regardless of tree position.
4. Assemble assistant messages by grouping live records on `requestId`.

## Compaction — append-only, full history retained

Verified across 81 sessions. Compaction does **not** rewrite or truncate the file; it **appends** two things and continues:

1. A `system` record, `subtype:"compact_boundary"`, `parentUuid:null` (a *new root*), with `compactMetadata`:
   `trigger` (`auto`|`manual`), `preTokens`, `postTokens`, `durationMs`, `preCompactDiscoveredTools`, and (esp. manual) `preservedSegment`/`preservedMessages` listing the `uuid`s kept verbatim across the boundary.
2. A `user` record flagged `isCompactSummary:true` whose content is the injected summary, parented back into the **pre-compaction** tree.

Consequences:
- **All pre-compaction messages remain** at full fidelity — you are never reduced to just the summary.
- The tree can now have **multiple roots** (original root + each `compact_boundary`). Reconstruction by parent-walk from the tip still bridges old→new *through the summary record's* `parentUuid`; the `compact_boundary` record itself sits off to the side as a marker.
- Tag the `isCompactSummary` message as synthetic (not a real human turn).

## Resume & cross-file duplication — not a problem at top level

Checked all 78 top-level session files: **0 `uuid`s are shared between any two of them.** So `--resume` appends to the *same* `sessionId.jsonl` rather than copying history into a new file. No top-level dedup needed.

The only real duplication is **sub-agent records, which appear twice**: inline in the parent flat file (`isSidechain:true`) *and* in the dedicated `subagents/*.jsonl`. So the dedup rule is simply: **dedup by `uuid` globally** when ingesting a session + its sub-agent files.

(Caution learned the hard way: raw-text `grep` for uuids/markers gives false positives, because a session logs *its own* analysis output — uuids and field names printed into tool results match themselves. Always classify via parsed JSON fields, not substring search.)

## Sub-agents

- Stored as `<sessionId>/subagents/agent-<hash>.jsonl`, each with a tiny sibling `agent-<hash>.meta.json` = `{"agentType": "...", "description": "..."}` (e.g. `general-purpose`, `Explore`). That's clean sub-agent identity.
- A sub-agent file is its own transcript (`user`/`assistant`/`progress` records) whose records carry **the parent's `sessionId`** and `isSidechain:true`; root is a `user` record (the task prompt).
- Launched from the parent via a `Task` `tool_use` (`input.subagent_type`, `input.description`).
- `progress` records live inside sub-agent transcripts (streaming status).
- Modeling choice: treat each sub-agent as a child session linked to the parent via the `Task` tool_use id, and dedup its records against the inline `isSidechain` copies in the parent.

## Files touched

- Primary: on `user`/tool_result records, `toolUseResult` for edit tools carries `filePath`, `structuredPatch` (the diff), `originalFile`, `userModified`. Confirmed: captured the `DESIGN.md` Write this session.
- Edit tool names to scan: `Write`, `Edit`, `MultiEdit`, `NotebookEdit` (`tool_use.name`).
- **Limitation:** files changed via `Bash` (e.g. `git`, `sed`, redirects) are **invisible** here — no structured record. Heuristic Bash-command parsing would be unreliable; flag as a known gap.
- Secondary/corroborating: `file-history-snapshot.trackedFileBackups`. The actual backup bodies under `~/.claude/file-history/` are intentionally not collected.

## Mapping → canonical envelope

| canonical event | from Claude |
|---|---|
| `actor` | `message.role` (user/assistant); tool_result → actor=tool |
| `type` message/tool_call/tool_result | from block `type` (thinking→message w/ flag, text→message, tool_use→tool_call, tool_result→tool_result) |
| `seq` | tree order along the live branch (parent-walk), tie-break `timestamp` |
| `ts` | `timestamp` |
| `refs.files` | `toolUseResult.filePath` / structuredPatch targets |
| `refs.commands` | `tool_use.input.command` for Bash |
| session meta | `sessionId`, `cwd`, `gitBranch`, `version`, final `ai-title` |
| `raw` | the whole original record |

## Resolved / still open

Resolved by the corpus sweep:
- ✅ **Rollbacks** are append-only; abandoned branches retained (0 dangling parents).
- ✅ **Compaction** is append-only; full pre-compaction history retained (see above).
- ✅ **Resume** doesn't duplicate across top-level files; dedup by `uuid` globally for sub-agents.
- ✅ **Sub-agents** layout decoded (see above).

Still to confirm (minor):
- **Images / binary attachments:** `attachment` shape for images; `isImage` flag seen on tool results.
- **MCP tools:** tool_use `name` namespacing (`mcp__server__tool`).
- **Interrupted turns / errors:** `interrupted`, `is_error` fields.
- **Minor record types** not yet opened: `queue-operation`, `custom-title`, `agent-name`, `pr-link`.

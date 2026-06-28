# Format notes — Codex

Reverse-engineered from real logs on this machine (`~/.codex`), spanning **2025-09 → 2026-06** (cli `0.x`, latest observed `0.137.0-alpha.4`). Empirical, not spec — and it **drifts hard across versions** (see the timeline; it is the headline of this doc). Read alongside `claude.md`: the two formats are structurally opposite — Claude is a `uuid` tree, Codex is a flat append-only log with explicit event markers.

## Location & layout

- Transcripts: `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<localTs>-<sessionId>.jsonl`
  - One flat JSONL file per session. **No sibling dirs, no tree** — a session is exactly one file.
  - `<localTs>` in the filename is **local time** (e.g. `16-33-14`); the `timestamp` inside records is **UTC** (`…Z`). The date-tree dirs are local-date. Don't derive UTC from the filename.
  - `<sessionId>` is a **UUIDv7** (sortable, embeds creation time). Equals `session_meta.id`; the join key into `history.jsonl` / `session_index.jsonl`.
- `~/.codex/archived_sessions/` — flat dir of the **same** `rollout-*.jsonl` format (archived/aged-out threads). The collector must capture this too.
- Sidecar catalogs (later sources, not transcripts):
  - `~/.codex/history.jsonl` — global human-prompt history: `{session_id, ts (unix s), text}`, one line per user prompt across all sessions.
  - `~/.codex/session_index.jsonl` — thread catalog: `{id, thread_name, updated_at}` (`thread_name` = human title, the analog of Claude's `ai-title`).
- **Ignore the SQLite files** (`logs_2.sqlite` ~800MB, `state_5`, `goals_1`, `memories_1`): Codex's own internal stores, not transcript truth; never sync or read them as sources. The JSONL rollout files are the source of truth.

## Format version timeline (the thing that will break a naive adapter)

A *single tool* changed its log schema repeatedly over 9 months. The adapter **must branch on version** — detect by field presence (robust) and/or `session_meta.cli_version`. Observed transitions (single-file monthly samples, so month boundaries are approximate):

| concern | 2025-09 → 2025-10 | 2025-11 → 2026-03 | 2026-04 → 2026-06 (current) |
|---|---|---|---|
| **reasoning text** | **readable** (`agent_reasoning` events + `reasoning.summary[].text`) | **readable** (same) | **encrypted only** — `summary:[]`, `content:null`, opaque `encrypted_content` |
| **shell/exec tool** | `shell` | `shell` → `shell_command` (≈2025-12) → `exec_command` (≈2026-02) | `exec_command` |
| **structured edits** | none — edits go through `shell` | `apply_patch` custom_tool_call (from 2025-11) | `apply_patch` |
| **`patch_apply_end`** | absent | absent | present (from ≈2026-04), but **not 1:1** with apply_patch |
| **`session_meta`** | `instructions`, no lineage fields | (transitional) | `base_instructions` + `forked_from_id`/`agent_role`/`agent_nickname`/`dynamic_tools`/`memory_mode` |
| **multi-agent** | — | — | `spawn_agent`/`wait_agent`/`close_agent` + `agent_role` sessions |

Stable across all versions: the `{type,timestamp,payload}` envelope; `session_meta`/`turn_context`/`response_item`/`event_msg`/`compacted` partition; `function_call`↔`function_call_output` pairing by `call_id`; `turn_aborted`; `compacted`; `update_plan`.

**Correction (verified against 0.39, 2025-09):** `task_started`/`task_complete` are **not** universal — the earliest logs have neither (and no `thread_rolled_back` either). So an adapter must **not** anchor turns on `task_started`. The codebrain adapter instead anchors a turn on the clean `event_msg.user_message`, which is present in every version; `thread_rolled_back{n}` (when present) pops the last `n` of those user-turns.

## The file is a flat append-only event log (no uuid tree)

Each line is `{"type": <envelope>, "timestamp": <UTC>, "payload": {…}}`. There is **no `uuid`/`parentUuid`** — ordering is purely **file/line order**. Five envelope `type`s:

| envelope `type` | role |
|---|---|
| `session_meta` | session header (re-emitted on every resume — see Resume) |
| `turn_context` | per-turn config snapshot (model, effort, mode, sandbox, cwd) |
| `response_item` | the **model-facing** conversation (OpenAI Responses API items) |
| `event_msg` | the **UI/telemetry** stream (clean prompts, streamed text, usage, tool/patch/turn events) |
| `compacted` | compaction record (see Compaction) |

`response_item` and `event_msg` are **two parallel views of the same conversation**. This in-file duplication is the *only* duplication in Codex — resolve it by picking a canonical source per field (see Canonical mapping), don't ingest both as separate events.

### `session_meta.payload`
Current (2026-04+): `id`, `timestamp` (UTC), `cwd`, `originator` (`codex-tui`), `cli_version`, `source`, `thread_source`, `model_provider`, `git` (`{commit_hash, branch, repository_url}` or null), **`base_instructions.text`** (full system prompt), and the lineage/identity fields **`forked_from_id`**, **`agent_role`**, **`agent_nickname`**, plus `dynamic_tools`, `memory_mode`.
Old (2025): thinner — `instructions` (not `base_instructions`), `id`, `cwd`, `cli_version`, `originator`, `timestamp`, sometimes `git`; **no lineage fields**.

### `turn_context.payload` (per turn / per config change)
`turn_id`, `cwd`, `workspace_roots[]`, `current_date`, `timezone`, `approval_policy`, `sandbox_policy`, `permission_profile`, **`model`** (e.g. `gpt-5.5`), `personality`, **`collaboration_mode`** (`{mode: default|plan, settings:{model, reasoning_effort, developer_instructions}}`), `effort`, `summary`. This is where per-turn model/effort/mode lives (Codex doesn't stamp it on every record like Claude does).

## `response_item` payload types (the model-facing backbone)

| `payload.type` | shape / notes |
|---|---|
| `message` | `role ∈ {developer, user, assistant}`, `content[]` of `{type: input_text\|output_text, text}`; assistant also has `phase`. **`developer` = injected instructions; `user` response_items are bloated** with AGENTS.md + `<environment_context>` + `<turn_aborted>` markers — not the clean prompt. |
| `reasoning` | `summary`, `content`, `encrypted_content`. **Version-dependent readability** — see Reasoning. |
| `function_call` | `{name, arguments (JSON-encoded **string**), call_id}`. `name` evolves: `shell`/`shell_command`/`exec_command` (+ `update_plan`, `spawn_agent`, `write_stdin`, …). |
| `function_call_output` | `{call_id, name (usually null), output (string)}`. Pair to the call by **`call_id`**. |
| `custom_tool_call` | `{status, call_id, name, input (raw **string**)}`. `apply_patch` is the main one; `input` is the literal `*** Begin Patch … *** End Patch` text. |
| `custom_tool_call_output` | `{call_id, output (string)}`. |
| `web_search_call` / `web_search_end` | native web search; `action:{type:"search", query, queries[]}`. |
| `tool_search_call` / `tool_search_output` | dynamic/deferred tool discovery (loads tool namespaces on demand); metadata, not conversation. |
| `agent_message` | Native inter-agent message: `{author, recipient, content, internal_chat_message_metadata_passthrough}`. Often preceded by top-level `inter_agent_communication_metadata {trigger_turn}`. This is non-human input and must not be mapped to `actor=user`. |

### Inter-agent / control-message provenance

Current Codex can send instructions to another thread through multiple structured
surfaces:

- Native multi-agent v2 records `inter_agent_communication_metadata` followed by
  `response_item.agent_message` in the receiver transcript. codebrain emits this
  as an assistant-side message, with `trigger_turn` starting a userless turn when
  present.
- MCP `codex-reply` records a sender-side `event_msg.mcp_tool_call_end` with
  `invocation.tool="codex-reply"`, `arguments.threadId`, and `arguments.prompt`.
- MCP `codex` starts a new thread; the sender-side result carries
  `structuredContent.threadId`, and the arguments carry the initial `prompt`.
- Older multi-agent `send_input` records a sender-side `response_item.function_call`
  with structured `arguments.target` and `arguments.message` or text `items`.

For the MCP/function-call cases, the receiver transcript still looks like an
ordinary `event_msg.user_message`. The provenance overlay classifies it only by
structured target thread id + exact UTF-8 payload hash + timestamp ordering, with
ambiguous matches marked `unknown`.

Tool pairing is by **`call_id`** (Codex's analog of Claude's `tool_use_id`) — never by position.

## `event_msg` payload types (the UI/telemetry stream)

| `payload.type` | shape / use |
|---|---|
| `user_message` | **the clean human prompt**: `{message, images, local_images, text_elements}`. Use this, not the bloated `response_item` user message. |
| `agent_message` | streamed assistant text: `{message, phase: commentary\|final, memory_citation}`. `commentary`=interim, `final`=answer. |
| `agent_reasoning` | **(old only, ≤2026-03)** readable reasoning: `{text}` (markdown summary headers). Gone in current versions. |
| `task_started` | turn start: `{turn_id, started_at (unix), model_context_window, collaboration_mode_kind}`. |
| `task_complete` | turn end: `{turn_id, last_agent_message (final text), completed_at, duration_ms, time_to_first_token_ms}`. |
| `turn_aborted` | interrupt: `{turn_id, reason:"interrupted", completed_at, duration_ms}`. Also injected as a `response_item` user message wrapping `<turn_aborted>…`. |
| `patch_apply_end` | structured edit result — see Files touched. |
| `token_count` | usage + rate limits: `{info:{total_token_usage, last_token_usage, model_context_window}, rate_limits:{…, plan_type}}`. |
| `thread_goal_updated` | goal-tracking state (feeds Codex's `goals` DB). |
| `thread_rolled_back` | **in-file rewind marker** — see Rollback. |
| `context_compacted` | UI marker (`{type}` only); real payload is the top-level `compacted` record. |
| `mcp_tool_call_end` | MCP tool result: `{call_id, duration, invocation, result}`. |

## Turn structure

A turn is bracketed by `task_started{turn_id}` … `task_complete{turn_id}` (or `turn_aborted{turn_id}`). The same `turn_id` tags `turn_context` and `patch_apply_end`. Human turns have exactly one clean `user_message`; native inter-agent `trigger_turn` messages can start a non-human/userless turn. Within a turn, the `response_item`s are the model-facing sequence; the `event_msg`s interleave the clean prompt, streamed text, usage, and tool/patch/turn events.

## Rollback — in-file, append-only, explicit marker (semantics verified)

No tree; a rewind is an `event_msg` `thread_rolled_back {num_turns:N}` appended to the **same file**. Distribution across the corpus (162 files): **878×`n=1`, 54×`n=2`, 4×`n=3`, 4×`n=4`, 1×`n=13`**.

**Verified semantics** (traced a 20-rewind file line by line). The dominant pattern: user interrupts a turn (`turn_aborted`) → `thread_rolled_back{n=1}` discards that turn → user re-submits the (often edited) prompt, which now completes. Clean example: `task_started`→`user:"Send two agents to review…"`→`turn_aborted`→`thread_rolled_back n=1`→`task_started`→`user:"Send two agents to review…"` (same text)→`task_complete`.

So **`num_turns` counts live turns popped from the current tip** (usually human-prompt turns; native inter-agent `trigger_turn` records can also start a turn), and **rollbacks stack** (an `n=2` operates on the tip left by an earlier rollback, not on physical line position). The popped turns remain physically above the marker — abandoned, but retained.

**Reconstruction:** maintain a stack of turns while replaying in file order; each clean `event_msg.user_message` pushes a human turn, and each `response_item.agent_message` with preceding `inter_agent_communication_metadata.trigger_turn` pushes a non-human/userless turn; each `thread_rolled_back{n}` pops the last `n`. Final stack = canonical conversation; popped turns = full-history-only. (Linear-log equivalent of excluding Claude's abandoned rollback subtree.)

## Resume — same file, same id, re-emitted header

A resumed thread **appends to the same `sessionId.jsonl`** and re-emits `session_meta` (identical `id` + original `timestamp`) plus the developer/environment preamble. Observed: one file with **24 identical `session_meta`** = 23 resumes. One file = one session = one id; resumes are in-file markers, and the repeated preamble is re-injected context (tag synthetic). No dedup needed.

## Compaction — append-only, full history retained

Appends a top-level **`compacted`** record `{payload:{message, replacement_history[]}}` (+ an `event_msg context_compacted` UI marker), then continues logging real turns normally.
- Pre-compaction records remain at full fidelity (verified: a 1586-line file had 22 user-messages before the boundary, 13 after).
- `message` is **empty** in this corpus; the condensed context is entirely **`replacement_history`** — an array (7–47 observed) of synthetic `message` items (mostly `role:user`, ending `role:developer`) that **replaces** prior context for the model. Codex's analog of Claude's injected `isCompactSummary`, but stored as one structured replacement array.
- Use `replacement_history` for "what the model saw after compaction"; the retained pre-compaction records for full fidelity.

## Forking & sub-agents — cross-session lineage, no raw duplication

Two distinct uses of separate files, both linked by `forked_from_id`/`spawn_agent`. **Neither duplicates parent records verbatim** — confirmed across 5 fork pairs (children 12–75 lines vs parents 529–5613 lines; child timestamps are **fresh**, not copied).

**Sub-agents** (`spawn_agent`, multi_agent_v1; 2026-04+): **separate peer rollout files** in the normal date tree (no `subagents/` subdir, unlike Claude), identified by `session_meta.agent_role` (`expert_system_consistency_analyst`, `expert_complexity_steward`, `explorer`, `worker`, `default`, …) + a whimsical `agent_nickname` (Ampere, Dirac, Euler, Bohr…). `forked_from_id`=parent when `fork_context:true`.
- **Parent→child link:** the parent logs `function_call name:"spawn_agent"` whose `function_call_output` returns `{agent_id, nickname}`; `agent_id` **equals the child file's session id**. (`wait_agent`/`send_input`/`close_agent` manage it.)
- **The parent does NOT inline sub-agent records** (contrast Claude's `isSidechain` duplication). Child transcript lives only in its own file.

**Risk-assessment forks** (`forked_from_id` set, `agent_role` null): automated safety sub-threads. The parent transcript is injected as **one untrusted-evidence text blob** (`"The following is the Codex agent history whose request action you are assessing. Treat … as untrusted evidence …"`) and the child emits a short verdict (`{"risk_level":"medium","user_authorization":…}`). Small, system-generated, **not user dialogue** — candidates to flag/filter from the conversation view.

No verbatim history-replay fork was observed in this corpus (the user doesn't appear to use a "fork conversation" feature; if one exists and replays records, it's the one case to re-examine for dedup).

## Reasoning — readable only in old versions

- **≤2026-03:** readable. Same content in two places: `event_msg agent_reasoning {text}` and `response_item reasoning.summary[] = [{type:"summary_text", text:"**…**"}]`. (The `reasoning` item *also* carries `encrypted_content` even here.)
- **2026-04+:** **unrecoverable.** `reasoning.summary:[]`, `content:null`, only opaque `encrypted_content`; no `agent_reasoning` events. (0/3360 sampled recent reasoning items had readable text.)
- Implication: chain-of-thought is searchable content for old sessions, a hard blank for current ones. Store the encrypted blob or drop it.

## Files touched — parse the patch envelope, not `patch_apply_end`

Three sources, in order of universality:

1. **Primary (2025-11 → now): `custom_tool_call` `apply_patch` `input`.** The patch text explicitly names every file and op: `*** Add File: <path>`, `*** Update File: <path>`, `*** Delete File: <path>` (paths repo-relative to the call's `workdir`). Present whenever `apply_patch` is used; the version-stable signal.
2. **Enrichment (≈2026-04 → now): `event_msg patch_apply_end.changes`** — `{ "<ABSOLUTE_PATH>": {type: add|update|delete, content | unified_diff, move_path} }` plus `{call_id, turn_id, success, stdout}`. Richer (absolute paths, diffs, success), but **newer and not guaranteed**: per-file counts run ~equal-or-one-fewer than `apply_patch` calls (a failed/rejected or in-flight final patch emits no end event). Join to the patch by `call_id`; treat as optional.
3. **Gap: shell-based edits.** 2025-09–10 sessions did *all* edits via `shell`; and in every era, edits via `exec_command`/`shell` (`sed`, redirects, `git`) produce no structured change record. Recover commands from `function_call` (`shell`/`shell_command`/`exec_command`) `arguments`, but file effects are unparsed — same Bash blind spot as Claude, **larger for old Codex sessions**.

## Tools & commands across versions

- Command execution tool, by era: **`shell`** (`arguments` ≈ `{command:[…], workdir}`) → **`shell_command`** → **`exec_command`** (`arguments` ≈ `{cmd, workdir, yield_time_ms, max_output_tokens}`). `refs.commands` extraction must handle all three names and their differing arg schemas.
- **`update_plan`** (function_call, all eras): the plan/checklist tool — carries structured plan/TODO state (analog of Claude's todos). Worth capturing as plan-state metadata.
- **Multi-agent** (2026-04+): `spawn_agent`, `wait_agent`, `send_input`, `close_agent`, `resume_agent`, `write_stdin`.
- **MCP / web search**: `mcp_tool_call_end`; `web_search_call`/`web_search_end`.

## Dedup & canonical-source rules

- **No raw-record dedup needed anywhere** (cleaner than Claude): sub-agents are separate files (not inlined), forks inject summaries (not records), resume is in-file (one file, one id).
- **The one duplication is in-file**: `response_item` (model truth) vs `event_msg` (UI truth). Rule: backbone = `response_item` sequence; clean human prompt = `event_msg.user_message.message`; assistant text = `response_item.message` with `role=assistant`; final answer also appears in streamed `event_msg.agent_message`/`task_complete.last_agent_message` and must not be double-counted.

## Mapping → canonical envelope

| canonical event | from Codex |
|---|---|
| session id | filename id / `session_meta.id` |
| session meta | `cwd`, `git{commit_hash,branch,repository_url}`, `model` (from `turn_context`), `cli_version`, `originator`, `thread_name` (from `session_index.jsonl`) |
| lineage | `forked_from_id`; `agent_role`+`agent_nickname` (sub-agent); parent's `spawn_agent` output `agent_id` |
| `actor` | `message.role` (developer→system/synthetic, user, assistant); tool_result→tool |
| `type` message/tool_call/tool_result | response_item: message→message, function_call/custom_tool_call→tool_call, *_output→tool_result, reasoning→message(synthetic; text only if old) |
| message text (human) | `event_msg.user_message.message` (clean) — not the bloated `response_item` user message |
| message text (assistant) | `response_item.message` with `role=assistant`; streamed `event_msg.agent_message` and `task_complete.last_agent_message` are duplicate UI/final-answer views |
| reasoning text | old: `agent_reasoning.text` / `reasoning.summary[].text`; current: none (encrypted) |
| `seq` | file/line order, after applying rollback popping + compaction handling |
| `ts` | record `timestamp` (UTC) |
| `refs.files` | parse `apply_patch` `input` (`*** Add/Update/Delete File:`); enrich via `patch_apply_end.changes` |
| `refs.commands` | `function_call` `shell`/`shell_command`/`exec_command` → `arguments.command`/`.cmd` |
| `raw` | the whole original record |

## Limitations / watch-list

- **Version drift is the dominant risk** — branch the adapter on field presence + `cli_version`; don't assume current schema for old files.
- **Reasoning unreadable in current versions** (encrypted); readable only ≤2026-03.
- **Files-touched is weakest for old/shell-heavy sessions**; `patch_apply_end` is newer and not guaranteed — prefer parsing the `apply_patch` envelope.
- **Two parallel views in-file** — canonical-source-per-field or double-count.
- Classify by parsed JSON fields, **never raw substring grep** (same self-contamination caution as `claude.md`; `rg -l` is fine for *locating* candidate files, but confirm structurally).

## Resolved / still open

Resolved by the corpus sweep:
- ✅ **Rollback** = in-file `thread_rolled_back{num_turns}`; semantics verified (pop N live turns from tip, stacks); abandoned records retained.
- ✅ **Resume** = same file, re-emitted identical `session_meta`; no dedup.
- ✅ **Compaction** = append-only top-level `compacted{replacement_history}`; full history retained.
- ✅ **Sub-agents / forks** = separate files, `forked_from_id`/`spawn_agent` linkage; no inlining, no raw duplication; risk-assessment forks identified.
- ✅ **Files touched** = `apply_patch` envelope primary, `patch_apply_end` enrichment, shell-edit gap mapped.
- ✅ **Version drift** = reasoning/tool-name/edit-recording transitions mapped (see timeline).

Still to confirm (minor):
- Exact `shell`/`shell_command` argument schemas (old eras) for clean command extraction.
- `mcp_tool_call_end.invocation`/`result` shape; whether MCP calls also appear as `response_item function_call`.
- Image/`local_images` attachment shape in `user_message`.
- Whether a true user-initiated "fork conversation" (verbatim history replay) exists and would need dedup — none seen here.
- `update_plan` argument shape (plan/TODO state capture).
- `plan` vs `default` collaboration-mode behavioral differences.

# Intent Archaeology Roadmap

This roadmap captures what `codebrain` should optimize next after real-world use
recovering an old design decision from agent-session history.

It complements `AGENT_RETRIEVAL_PLAN.md`: the first browsing primitives
(`recent`, `userlog`, `turns`) now exist. The next work should make the successful
archaeology loop smoother without turning `codebrain` into a magic memory oracle.

## Progress

- [x] Search filters/noise exclusion: `--actor`, `--type`, `--source`, `--cwd`,
  `--before`, `--after`, `--exclude-session`, `--only-session`,
  `--exclude-recent`, `--include-inherited`, `--include-subagents`, `--json`.
- [x] Turn-centered search expansion: `search --around N` inlines nearby
  user-centered turns, with hidden/truncated tool context and structured JSON.
- [x] `lineage <session>` primitive: factual root/parent/current/children/siblings
  from structured session parent links, with latest-user previews and JSON.
- [x] `refs <session>` primitive: grouped files, commands, and commit hashes from
  structured event refs plus conservative commit-token extraction, with seq evidence
  and JSON.
- [x] `touched <path>` primitive: file-first archaeology over structured
  `events.refs.files`, with exact/suffix, basename, prefix, filters, expand hints,
  and JSON.
- [ ] JSON/docs/cheatsheet consistency pass.
- [ ] Polish/hardening/simplification/testing/analysis mode; no more feature-add
  mode unless empirical use clearly justifies it.

## Core lesson

`codebrain` is valuable when the question is:

> What did we decide, prefer, reject, or reason through in a past agent discussion?

Git is better when the question is:

> What code or committed artifact existed at a specific point?

The strongest workflow combines them:

```text
codebrain finds the relevant discussion / turn / session
  -> codebrain exposes lineage and referenced artifacts
    -> git recovers committed docs, patches, and code snapshots
```

So `codebrain` should not compete with git. It should be the fast path from vague
intent/rationale memory to the concrete session, turn, file, or commit that git can
then inspect.

## Product philosophy

Prefer lean composable primitives over opinionated one-shot commands.

Good primitives:

- expose evidence
- filter noise
- preserve session/turn/file ids
- provide JSON for scripts and future agents
- compose naturally with `git`, `sqlite3`, `rg`, and existing `turns`

Do not build:

- automatic canonical-decision extraction
- `find-discussion` classifiers
- LLM-generated memory as source of truth
- regex classification of intent/subagents/decisions from prompt text
- automatic discussion-heavy ranking/classifiers
- automatic preference extraction
- semantic/vector search as an oracle or replacement for evidence traversal

A future agent should be able to reconstruct a sophisticated workflow by repeatedly
calling simple commands.

## Target archaeology loop

A good future session should look like:

```bash
sessdb search "ExamplePriority" --cwd example-project --actor user --before 2026-06-11
sessdb turns <session> --around-seq <seq> --context-turns 2
sessdb lineage <session>
sessdb refs <session>
sessdb touched <path>
git show <commit>:<path>
```

The command set should make this easy, not hide it behind a single high-judgment
command.

## Slice 1 — Search filters and noise exclusion

Extend `sessdb search` first. It is currently the obvious entry point when an agent
remembers a phrase but not the session.

Add composable filters:

```bash
sessdb search "query" --actor user
sessdb search "query" --type message
sessdb search "query" --source pi
sessdb search "query" --cwd example-project
sessdb search "query" --before 2026-06-11
sessdb search "query" --after 2026-06-01
sessdb search "query" --exclude-session <session_id>
sessdb search "query" --only-session <session_id>
sessdb search "query" --exclude-recent 1h
sessdb search "query" --include-inherited
sessdb search "query" --include-subagents
sessdb search "query" --json
```

Default behavior should remain understandable and should include all sessions,
including the active/current one. Do **not** guess or silently exclude the current
session: long sessions and pre-compact recovery often need self-search. Make this an
explicit filter instead. `--before`, `--exclude-session`, `--only-session`, and
`--exclude-recent` are the highest-value filters from the real use case.

Implementation notes:

- Reuse shared filter semantics from `recent` / `userlog` where possible.
- Do not classify sessions from prompt text.
- Subagent exclusion should continue to use structured relation/tool-call lineage.
- Date filters should operate on event timestamps for search hits.
- `--cwd` can stay substring-based initially; no repo abstraction required.
- JSON rows should include enough to compose: `session_id`, `seq`, `event_id`,
  `ts`, `source`, `cwd`, `actor`, `type`, `text`, and an expand command or fields
  needed to construct one.

Acceptance criteria:

- Searching a phrase includes current/active sessions by default.
- Searching a phrase with `--exclude-session` removes hits from that session.
- Searching with `--only-session` supports deliberate self-search/pre-compact recovery,
  including inherited live context in that one session.
- Searching with `--before` excludes later/current self-referential hits.
- Searching with `--actor user` can isolate user confirmations/preferences.
- `--json` output is stable and scriptable.

## Slice 2 — Turn-centered search output

Current FTS hits are event-centered. For intent archaeology, the useful unit is often
one user turn plus nearby assistant/tool context.

Add a small expansion option rather than a new high-level classifier:

```bash
sessdb search "ExamplePriority" --around 2
```

or, if cleaner:

```bash
sessdb search "ExamplePriority" --unit turn --context-turns 2
```

The output should show the matching event plus nearby user-centered turns, using the
same truncation style as `sessdb turns`.

Minimal version:

- keep default search compact
- always print `expand: sessdb turns <session> --around-seq <seq>`
- add `--around/--context-turns` to inline that expansion when requested

Acceptance criteria:

- A phrase hit can be understood without immediately running a second command.
- Large tool output stays hidden/truncated by default.
- JSON output preserves match metadata and the expanded turn block separately.

## Slice 3 — `lineage <session>` primitive

Duplicate branch/resume sessions were a real source of confusion. Add a primitive
that exposes lineage without guessing a canonical session.

Example:

```bash
sessdb lineage pi:...
sessdb lineage pi:... --json
```

Human output should show:

```text
root:      <session>
parent:    <session> relation=<branch|subagent|resume|...> branch_point=<event/seq>
current:   <session>
children:
  - <session> relation=branch started=<time> last_user=<time>
  - <session> relation=subagent started=<time> last_user=<time>
siblings:
  - ...
```

Keep this factual. Do not auto-label "the canonical decision session". If a root or
original candidate is obvious from parent pointers, show it as lineage, not truth.

Implementation notes:

- Use `sessions.parent_session_id`, `sessions.relation`,
  `sessions.branch_point_event_id`, and `sessions.spawn_event_id`.
- Join to latest live user message for useful previews.
- Include expand commands for parent/current/children.
- Make subagent relation visible but keep subagents excluded from intent views by
  default elsewhere.

Acceptance criteria:

- Given a branch copy, the user can find its parent/root.
- Given an original session, the user can see branch/subagent children.
- JSON contains enough fields for agents to choose their own traversal.

## Slice 4 — `refs <session>` primitive

After `codebrain` finds the conversation, agents need a low-friction handoff to git
and files. Add a command that extracts structured references for one session.

Example:

```bash
sessdb refs <session>
sessdb refs <session> --around-seq 142 --context-turns 2
sessdb refs <session> --json
```

Output categories:

- files from `events.refs.files`
- commands from `events.refs.commands`
- commit hashes mentioned in event text or structured command output
- cwd/source/session metadata
- event ids and seqs where each ref appeared

This should be an evidence index, not an artifact classifier. It is okay if commit
hash extraction is a conservative text pattern, because a commit hash is itself a
structured token to hand to git; do not infer product semantics from it.

Possible output:

```text
session: pi:...
cwd: ~/code/example-project

files:
  docs/wip/example-priority.md
    seq 120 assistant tool_result
    expand: sessdb turns <session> --around-seq 120

commits:
  6ec541b
    seq 133 assistant message
    git show 6ec541b
```

Acceptance criteria:

- A session that mentions a commit and file yields copy-pastable `git show` hints.
- File refs are grouped/deduplicated but retain seq/event evidence.
- JSON output is suitable for scripts.

## Slice 5 — `touched <path>` primitive

After `refs <session>` gives conversation → artifacts, add the inverse primitive:
artifact → conversations. Keep it structured: match only `events.refs.files`, not
free-text path-looking mentions.

Example:

```bash
sessdb touched docs/wip/pipeline-redesign.md
sessdb touched pipeline-redesign.md --basename
sessdb touched docs/wip/ --prefix --cwd example-project
sessdb touched docs/wip/pipeline-redesign.md --json
```

Output should include source/cwd/session metadata, seq/event evidence, nearest user
context, and copy-pastable `turns` / `refs` expansion commands. Default behavior can
match a relative path against an absolute structured ref by path-boundary suffix;
`--basename` and `--prefix` make broader matching explicit.

Acceptance criteria:

- A known file can lead back to sessions/turns that structurally referenced it.
- Free-text path mentions do not become evidence unless the adapter put the path in
  `events.refs.files`.
- JSON output is suitable for scripts.

## Slice 6 — JSON and CLI consistency pass

Once the archaeology primitives exist, make the interface regular.

Checklist:

- all read commands support `--json`
- all result rows include `session_id`, `seq` where applicable, and stable ids
- human output consistently includes `expand:` commands
- filters use the same names across commands where possible
- docs and `CHEATSHEET.txt` include the archaeology workflow

## Explicit non-goals / probably-never features

The following should not be treated as deferred backlog. They are contrary to the
shape of the tool unless the user explicitly reverses this product direction:

- automatic decision summaries
- `decision-pack`
- discussion-heavy ranking/classifiers
- canonical-intent extraction
- automatic preference extraction
- LLM-authored memory as source of truth
- any command that claims to know the user's "real" decision without showing the
  underlying transcript/file/commit evidence

These can be reconstructed by composing search filters, `turns`, `lineage`, `refs`,
`touched`, and git. That is the point.

Semantic/vector search is also suspect. If it is ever added, it should be a low-trust
recall aid only: opt-in, evidence-preserving, and always returning concrete sessions,
turns, and event ids to inspect. It must not become an intent oracle, ranker of
canonical decisions, or replacement for exact search plus transcript traversal.

Materialized turn tables are different: they are an implementation/cache detail, not
a product feature. Add them only if live SQL becomes too slow or awkward, and keep the
observable CLI behavior primitive and evidence-first.

## Recommended implementation order

1. Extend `search` with filters/noise exclusion and JSON.
2. Add turn-centered search expansion.
3. Add `lineage <session>`.
4. Add `refs <session>`.
5. Add `touched <path>` as the inverse artifact-to-session primitive.
6. Do a JSON/docs/cheatsheet consistency pass.
7. Shift to polish, hardening, simplification, testing, and empirical analysis.

Keep each slice small and reviewable. Prefer shipping a narrow primitive that works
well over a broad command with hidden judgment. After `touched`, default to improving
and measuring the existing loop rather than adding new commands.

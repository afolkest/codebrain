# Intent Archaeology Roadmap

This roadmap captures what `codebrain` should optimize next after real-world use
recovering an old design decision from agent-session history.

It complements `AGENT_RETRIEVAL_PLAN.md`: the first browsing primitives
(`recent`, `userlog`, `turns`) now exist. The next work should make the successful
archaeology loop smoother without turning `codebrain` into a magic memory oracle.

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

Avoid for now:

- automatic canonical-decision extraction
- `find-discussion` classifiers
- LLM-generated memory as source of truth
- regex classification of intent/subagents/decisions from prompt text
- semantic/vector search until the basic loop is clearly insufficient

A future agent should be able to reconstruct a sophisticated workflow by repeatedly
calling simple commands.

## Target archaeology loop

A good future session should look like:

```bash
sessdb search "ExamplePriority" --cwd example-project --actor user --before 2026-06-11
sessdb turns <session> --around-seq <seq> --context-turns 2
sessdb lineage <session>
sessdb refs <session>
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
sessdb search "query" --exclude-recent 1h
sessdb search "query" --include-inherited
sessdb search "query" --include-subagents
sessdb search "query" --json
```

Default behavior should remain understandable, but archaeology usually wants to
remove current/live noise. `--before`, `--exclude-session`, and `--exclude-recent`
are the highest-value filters from the real use case.

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

- Searching a phrase with `--exclude-session` removes hits from that session.
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
- branches if present in structured commands/output
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

## Slice 5 — JSON and CLI consistency pass

Once the above primitives exist, make the interface regular.

Checklist:

- all read commands support `--json`
- all result rows include `session_id`, `seq` where applicable, and stable ids
- human output consistently includes `expand:` commands
- filters use the same names across commands where possible
- docs and `CHEATSHEET.txt` include the archaeology workflow

## Deferred ideas

Do not implement these until repeated use proves the primitives are insufficient:

- semantic/vector search
- automatic decision summaries
- `decision-pack`
- discussion-heavy ranking/classifiers
- canonical-intent or preference extraction
- materialized turn tables unless live SQL becomes slow

These can be reconstructed by composing search filters, `turns`, `lineage`, `refs`,
and git. That is the point.

## Recommended implementation order

1. Extend `search` with filters/noise exclusion and JSON.
2. Add turn-centered search expansion.
3. Add `lineage <session>`.
4. Add `refs <session>`.
5. Do a JSON/docs/cheatsheet consistency pass.

Keep each slice small and reviewable. Prefer shipping a narrow primitive that works
well over a broad command with hidden judgment.

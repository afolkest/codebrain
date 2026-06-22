# Simplification Audit

Date: 2026-06-22

## Scope

Audit after the archaeology primitives and cross-machine pool refresh landed. The
question is not "what feature next?" but "what can be made easier to understand,
trust, and maintain without reducing evidence-first capability?"

Current command count: 15

- `ingest`
- `collect`
- `ingest-pool`
- `backfill-claude`
- `list`
- `recent`
- `userlog`
- `turns`
- `show`
- `search`
- `lineage`
- `refs`
- `touched`
- `grep`
- `schema`

Current main implementation pressure point: `codebrain/cli.py` is ~1,500 lines and
mixes ops commands, browsing commands, rendering, parser setup, and helper logic.
This is still workable, but it is where future confusion will accumulate.

## Keep the product shape

The core shape is good and should not be simplified away:

```text
recent / userlog / search
  -> turns
  -> lineage
  -> refs / touched
  -> git / files / raw SQL / grep
```

This is evidence-first and composable. Do not replace it with a classifier,
canonical decision command, summary oracle, or LLM-authored memory.

## Command taxonomy

The flat command list is the biggest user-facing complexity. Most commands are not
part of the daily archaeology loop.

### Daily archaeology commands

These are the commands agents/users should reach for most often:

- `recent` — session starting points by latest live user activity
- `userlog` — raw stream of recent user messages
- `search` — exact/FTS phrase entry point, with filters and `--around`
- `turns` — expand a session around a sequence
- `lineage` — disambiguate branches/resumes/subagents
- `refs` — conversation -> files/commands/commits
- `touched` — file/artifact -> conversations

### Setup / sync / repair commands

These should be documented as ops, not normal retrieval:

- `ingest` — first build / full rebuild
- `collect` — local live homes -> syncable pool
- `ingest-pool` — debug/repair explicit pool ingest; normal reads already refresh
  remote pool subtrees
- `backfill-claude` — one-shot historical backup import

### Escape hatches / low-level commands

These are useful but should not be presented as part of the normal loop:

- `show` — raw transcript view when `turns` is not enough
- `grep` — raw-file literal/regex forensics
- `schema` — SQL interface docs
- `list` — older/session-metadata listing; mostly superseded by `recent`

## Highest-value simplifications

### 1. Reorganize README and CHEATSHEET around the taxonomy

Status: recommended first slice.

Problem:

- README Quickstart is a flat list of nearly every command.
- CHEATSHEET begins with sync ops, then daily commands, without headings.
- `ingest-pool` and `backfill-claude` compete visually with daily retrieval.

Suggested change:

- README Quickstart should show:
  1. install / first build
  2. daily archaeology loop
  3. sync setup pointer
  4. ops/escape hatch list
- CHEATSHEET should have headings:
  - Setup/sync
  - Daily archaeology
  - Artifact handoff
  - Escape hatches
- Top-level CLI docstring can also group commands in prose, even if argparse's
  generated command list remains flat.

Why first:

- Lowest risk.
- Makes existing behavior easier without changing semantics.
- Directly addresses user confusion around pool/sync/ingest.

### 2. Remove exact test counts from docs

Status: recommended first slice.

Problem:

README says the exact number of tests. It has already required repeated updates and
creates doc churn unrelated to user value.

Suggested change:

```text
python3 -m unittest discover        # full test suite, a second or two
```

If exact counts are needed, CI or test output provides them.

### 3. Decide whether `list` should be demoted

Status: recommended docs-only demotion, not removal.

Problem:

`list` and `recent` both sound like "recent sessions." For intent archaeology,
`recent` is usually better because it sorts by latest live user activity.

Suggested change:

- Keep `list` for metadata/debug compatibility.
- Move it out of the daily loop in docs/help prose.
- Describe it as "session metadata by start time" or similar.

Do not remove now; scripts may use it.

### 4. Clarify `grep` scope after pool sync

Status: recommended docs/help clarification.

Problem:

`grep` defaults to local live tool homes (`~/.claude`, `~/.codex`, `~/.pi`). After
cross-machine pool refresh, users may expect `grep` to search synced remote pool raw
logs too. It currently does not unless paths are supplied.

Options:

1. Document it clearly: default grep is local live homes; pass `~/codebrain-pool/raw`
   for synced archive forensics.
2. Or change default grep roots to include `~/codebrain-pool/raw` when it exists.

Recommendation:

Start with documentation. Changing default grep may duplicate local+pool hits and
make output noisier.

### 5. Archive or status-tag old plan documents

Status: recommended docs cleanup.

Problem:

Root-level planning docs are useful history but add cognitive load:

- `AGENT_RETRIEVAL_PLAN.md`
- `INTENT_ARCHAEOLOGY_ROADMAP.md`
- `POOL_SYNC_REFRESH_PLAN.md`

They overlap with current docs and include old ordering/backlog notes.

Suggested change:

- Keep them, but add clear status banners:
  - implemented / historical design record
  - remaining checklist, if any
- Consider moving later to `docs/plans/` once root docs feel crowded.

Do not delete; they capture product decisions and non-goals.

## Code simplification opportunities

### 6. Split `codebrain/cli.py` by concern

Status: useful, but not first unless editing `cli.py` becomes painful.

Current `cli.py` combines:

- refresh/open handling
- command implementations
- rendering helpers
- SQL builders
- path/ref helpers
- parser construction

Low-risk split candidates:

```text
codebrain/cli.py              # parser + dispatch only
codebrain/cli_intent.py       # recent/userlog/search/turns/lineage/refs/touched
codebrain/cli_ops.py          # ingest/collect/ingest-pool/backfill/grep/schema
codebrain/render.py           # _wrapped, _oneline, placement suffix, output helpers
```

Recommendation:

Do not do a big split all at once. If touched/refs/search are edited again, extract
one cohesive group then. Tests already cover CLI behavior well enough to support it.

### 7. Share repeated filter/parser definitions

Status: good medium-small cleanup.

Repeated option families appear in `search` and `touched`:

- `--source`
- `--cwd`
- `--after`
- `--before`
- `--exclude-session`
- `--only-session`
- `--exclude-recent`
- `--include-inherited`
- `--include-subagents`
- `--json`
- `--no-refresh`

Suggested helpers:

```python
def _add_common_event_filters(sp, *, only_session_help: str): ...
def _apply_common_event_filters(where, params, args, *, alias: str): ...
```

Caution:

Do not over-generalize user-message filters (`recent`/`userlog`) with event filters
unless the SQL remains easy to read. Structured subagent exclusion must stay explicit
and test-backed.

### 8. Keep pool refresh isolated; avoid config layer for now

Status: deliberate non-action.

The env hooks are enough for the current setup:

- `CODEBRAIN_MACHINE`
- `CODEBRAIN_LOCAL_MACHINES`

A config command/file would be another user-facing surface. Add it only if dogfood
shows env vars are too awkward.

### 9. Leave `backfill_claude.py` isolated

Status: no immediate simplification.

It is large, but it is a one-shot ops module with focused tests. Pulling pieces out
now would mostly move complexity around. Keep it out of the daily path and out of
Quickstart prominence.

## Hardening items discovered during the audit

These are not simplifications, but they are good regression targets if touched:

- `grep` default-scope tests if docs or behavior changes.
- `ingest-pool --machine typo` should remain visibly diagnostic (`pool_roots=0` plus
  no-matching-roots message).
- `collect --machine` and `CODEBRAIN_MACHINE` path-component validation is now
  important because pool discovery rejects invalid components.
- Any refactor of common filters must preserve structured subagent exclusion and
  inherited-context semantics.

## Recommended next slice

Status: implemented in the docs pass following this audit.

Small docs-only simplification:

1. Rewrite README Quickstart into grouped sections.
2. Add headings to `CHEATSHEET.txt`.
3. Remove exact test count from README.
4. Clarify `grep` default scope.
5. Demote `list`, `show`, `ingest-pool`, and `backfill-claude` in docs without
   removing commands.

Acceptance criteria:

- A new agent can identify the daily archaeology loop in under a minute.
- Sync/setup commands do not look required for every query.
- Docs no longer need test-count churn.
- No code behavior changes.

## Not recommended right now

- Removing commands.
- Adding a config subsystem.
- Adding benchmark scaffolding before a small docs simplification pass.
- Moving all planning docs in the same slice as CLI/docs edits.
- Splitting `cli.py` before there is an active reason to edit those command groups.

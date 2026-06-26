# bmux Provenance Plan

Status: planning note.

## Problem

codebrain indexes native coding-agent transcripts from Claude, Codex, and pi.
Those transcripts usually record every prompt submitted to an agent as a native
`user` message. bmux can also submit prompts into worker panes on behalf of a
master agent, and those prompts will appear in the worker transcript with the
same native `user` actor.

For intent archaeology, that distinction matters. Human-authored words should
remain cleanly separable from master-agent control messages, without relying on
style guesses, typo detection, or prompt text regexes.

## Design Principle

Do not change the meaning of native transcript `actor`.

`actor` remains the source role:

```text
user | assistant | tool
```

Add a second provenance dimension for native `user` messages:

```text
origin = human | master_control | unknown
```

This keeps the model honest:

- `actor=user, origin=human` means a human-authored prompt.
- `actor=user, origin=master_control` means bmux submitted the prompt.
- `actor=user, origin=unknown` means codebrain cannot safely classify the prompt
  as clean human intent or verified bmux control.
- assistant/tool events do not need an input origin.

The exact column/view name is still open. Candidate names:

```text
input_origin
transcript_origin
message_origin
```

`transcript_origin` matches the current bmux event field.

## bmux Event Source

bmux records successful control submissions in:

```text
~/.bmux/events/bmux.jsonl
```

The relevant event kinds are:

```text
bmux.send_submitted
bmux.launch_prompt_submitted
```

Both use:

```text
transcript_origin = master_control
visible_provenance_strategy = none
```

`bmux.send_submitted` includes the target `codebrain_session_id` directly.

`bmux.launch_prompt_submitted` may not yet know the `codebrain_session_id`, so it
must be resolved by `launch_id` through a later `bmux.pane_discovered` or
`bmux.pane_linked` event that resolved the same launch. See Resolution Decision
for how `launch_id` is extracted across current and historical event shapes.

The bmux event log does not store the raw submitted message body. It stores
payload metadata such as SHA-256, byte counts, line counts, and timestamps.

## Resolution Decision

Settled after probing a real `~/.bmux/events/bmux.jsonl`: the docs implied a
single top-level `launch_id` on `pane_discovered`/`pane_linked`, but real logs
carry it in several places depending on bmux version. The decision:

**Launch-prompt provenance resolution is launch-id-first.** For a
`bmux.launch_prompt_submitted` event, extract `launch_id` from the first of
these that is present:

```text
data.launch_id
data.previous_bmux.launch_id
data.launch_correlation.launch_id
the attempt event referenced by data.attempt_event_id (its launch_correlation.launch_id / previous_bmux.launch_id)
```

Build a `launch_id -> codebrain_session_id` index from `bmux.pane_discovered` and
`bmux.pane_linked` events using the same extraction order, then look the
submission's `launch_id` up in it. `bmux.send_submitted` skips all of this and
uses `data.codebrain_session_id` directly.

Pane/time is **not** the primary key — `pane_id` is recyclable. It is allowed
only as a best-effort fallback for old/malformed logs and must fail closed.
A launch prompt whose `launch_id` resolves to no session is left **unmatched**
(never guessed, never attributed to an unrelated session).

This dissolves the identical-payload-hash concern: two launches can share a
payload SHA-256, but each resolves via its own `launch_id` to a distinct
`codebrain_session_id`, so the `session + hash + time/order` key separates them.

## Classification Rule

For each native transcript message where:

```text
actor = user
type = message
```

classify as `master_control` only when there is a one-to-one structured match to
a bmux control-submission event.

A valid v0 match should require:

- same resolved `codebrain_session_id`
- same exact UTF-8 SHA-256 payload hash
- plausible timestamp/order near `submitted_at`
- one bmux event matching one transcript message

If the match is unique, set:

```text
origin = master_control
```

If there is no plausible bmux control event for that message, set:

```text
origin = human
```

If a specific bmux control event plausibly matches but cannot be uniquely paired,
set:

```text
origin = unknown
```

## Ambiguity Scope

Ambiguity must be narrow.

Do not downgrade unrelated native user messages just because bmux exists. A
message should become `unknown` only inside the blast radius of a specific bmux
control event for the same resolved `codebrain_session_id`.

Examples:

- If a human manually types a different message into a bmux-controlled pane near
  a bmux send, it should remain `origin=human`.
- If bmux submits `please continue` and a human submits a different message in
  the same pane nearby, only the bmux payload should match `master_control`.
- If bmux and a human submit byte-identical text into the same session around the
  same time and codebrain cannot pair them uniquely, those candidate messages may
  become `origin=unknown`.

`unknown` should be rare. It means "not clean enough for automatic human-intent
retrieval," not "discard this text."

## Query Behavior

Intent-oriented commands should protect the human-intent bucket by default.

Suggested behavior:

```sh
codebrain userlog
codebrain recent
```

Default to clean human-authored messages only:

```text
actor = user
origin = human
```

For explicit inspection, expose origin filters:

```sh
codebrain userlog --origin all
codebrain userlog --origin human
codebrain userlog --origin master-control
codebrain userlog --origin unknown

codebrain search "query" --actor user
codebrain search "query" --actor user --origin all
codebrain search "query" --actor user --origin master-control
```

`turns` and `show` should display the full transcript by default, including
master-control and unknown messages, but label user-message origin in text and
JSON output.

This keeps transcript inspection complete while keeping intent archaeology clean.

## Non-Goals

- No style-based authorship classifier.
- No typo/polish heuristic.
- No confidence ladder.
- No in-band `[bmux/...]` marker for now.
- No mutation of native transcript files.
- No second transcript store.
- No redefinition of native `actor`.

## Likely Implementation Shape

Keep raw transcripts as the source of truth and add a rebuildable overlay inside
codebrain's SQLite cache.

Possible schema:

```sql
CREATE TABLE bmux_control_submissions (
  send_id TEXT,
  launch_id TEXT,
  kind TEXT NOT NULL,
  submitted_at TEXT NOT NULL,
  codebrain_session_id TEXT,
  payload_sha256 TEXT NOT NULL,
  payload_byte_count INTEGER,
  payload_line_count INTEGER,
  master_id TEXT,
  raw_event TEXT NOT NULL,
  PRIMARY KEY (kind, send_id, launch_id, submitted_at)
);

CREATE TABLE event_origins (
  session_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  origin TEXT NOT NULL,
  evidence_kind TEXT,
  evidence_id TEXT,
  reason TEXT,
  PRIMARY KEY (session_id, event_id)
);
```

Then expose a view or query helper that joins `transcript` to `event_origins` and
returns `origin='human'` for unmatched native user messages.

The exact schema can change, but the important boundary is:

- `events` and `session_events` continue representing native transcript facts.
- bmux provenance is an overlay derived from bmux's event log.
- the overlay is rebuildable.

## Minimal Slice

1. Add a bmux event-log reader that extracts submitted control events and resolves
   bootstrap `launch_id` events to `codebrain_session_id` when possible.
2. Add an origin overlay table or view.
3. Match transcript user messages to bmux events by session id + payload hash +
   narrow time/order window.
4. Update `userlog`, `recent`, and `search --actor user` defaults to use clean
   human intent.
5. Add `--origin` filters.
6. Label origins in `turns` / `show` JSON and text output.
7. Add tests for:
   - clean human message remains human in an unrelated session
   - bmux send becomes master_control
   - manual different text near bmux send remains human
   - repeated identical ambiguous payload becomes unknown
   - launch prompt resolves through later pane discovery/link event


## Implementation Handoff

A fresh implementation agent should start by reading:

1. `AGENTS.md` in this repo. The important rule is structured provenance first:
   do not classify user intent from prompt style, typo patterns, or arbitrary text
   regexes.
2. `BMUX_PROVENANCE_PLAN.md` for the intended codebrain-side model.
3. `~/code/bmux/docs/V0_CODEBRAIN_PROVENANCE.md` for the bmux event contract.

Likely code touch points:

- `codebrain/db.py`
  - add the rebuildable provenance overlay schema
  - consider a transcript-with-origin view or helper query shape
- `codebrain/cli.py`
  - read/apply bmux provenance during refresh/open or through an explicit helper
  - add `--origin` filters where appropriate
  - keep `actor` as the native transcript role
  - label origin in `turns` and `show`
- `pyproject.toml`
  - optionally add `codebrain = "codebrain.cli:main"` while keeping `sessdb` as an alias
- tests
  - existing relevant files include `tests/test_cli_userlog.py`,
    `tests/test_cli_search.py`, and `tests/test_cli_turns.py`
  - add a focused provenance test file if that keeps fixtures clearer

Preserve these boundaries:

- Do not mutate native Claude/Codex/pi transcript files.
- Do not store bmux-submitted message bodies in codebrain unless there is a
  separate explicit decision to do so.
- Do not redefine `actor` to include bmux concepts.
- Do not add style-based authorship confidence.
- Do not make unrelated historical user messages ambiguous just because bmux is
  configured.
- If a bmux event cannot be resolved to a session, leave it unmatched rather than
  polluting unrelated sessions.

The matching-window policy is intentionally not fully nailed down. The first
implementation should choose a simple conservative window, document it in tests,
and keep ambiguity local to the matching candidates for the same resolved
`codebrain_session_id` and payload hash.

Suggested test command:

```sh
python3 -m unittest discover
```

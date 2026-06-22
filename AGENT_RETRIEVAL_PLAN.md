# Agent Retrieval UX Plan

## Core purpose

`codebrain` is not mainly an audit log of what past agents did. Its core job is to
help future agents reconstruct the user's brain: goals, taste, constraints,
corrections, preferences, decisions, and intent over time.

Past agent activity is still useful evidence, but it is secondary. The primary
corpus is the user's own messages.

## Context

`codebrain` already has the storage spine:

```text
raw logs → per-source adapters → SQLite → CLI / FTS / grep / raw SQL
```

The next useful slice is a thin, SQLite-first retrieval layer that helps an agent
quickly answer:

- What has the user cared about recently?
- What did the user ask for, correct, reject, or prefer?
- Which sessions contain relevant user intent for this task?
- What nearby agent/tool context is needed to disambiguate that intent?

## Design principle: compositional primitives

Do **not** build a large preference ontology or one-shot "reconstruct user brain"
command. Prefer a small set of flexible primitives that reasoning agents can chain:

1. **List** recent user/session activity.
2. **Filter** by source, cwd/repo, time, length, actor, live branch.
3. **Search** primarily over user text.
4. **Expand** from a hit to surrounding turns/transcript context.
5. **Join** file/activity evidence back to the nearest user request.
6. **Export JSON** so agents can script combinations.

Compositionality beats over-engineered special-purpose commands.

## Guiding idea

Optimize first for **user intent**.

User messages are usually higher-signal than agent outputs: they contain goals,
preferences, corrections, constraints, taste, and decisions. Assistant/tool outputs
can be enormous and low-density. So the browsing layer should make user messages
first-class and aggressively truncate agent output by default.

A useful retrieval unit is a **turn**:

```text
user message + truncated assistant/tool aftermath until the next user message
```

## Non-goals for this slice

- No semantic/vector search yet.
- No embeddings, summaries, or `sqlite-vec` yet.
- No MCP server or heavyweight tool-access system.
- No sub-agent ingestion work in this slice.
- No reasoning/thinking normalization.
- No hard-coded preference ontology, phrase classifier, or `prefs` magic command.

Keep the system simple: CLI shortcuts over SQLite, with raw SQL still available.

---

## Phase 1 — User-intent browsing primitives, no new schema required

### 1. `sessdb userlog`

Show user messages across sessions, newest first. This is the flagship primitive
for reconstructing user intent.

Example usage:

```bash
sessdb userlog
sessdb userlog --limit 100
sessdb userlog --since 30d
sessdb userlog --cwd codebrain
sessdb userlog --source claude
sessdb userlog --min-chars 40 --max-chars 4000
sessdb userlog --query "retrieval UX"
sessdb userlog --full
sessdb userlog --json
```

Default behavior:

- query only `actor='user' AND type='message'`
- newest first
- live placements only
- truncate long messages
- optionally skip tiny low-signal messages via `--min-chars`
- return `session_id` and `seq` so another command can expand context

Example output shape:

```text
2026-06-10  pi  pi:abc123...  seq 42  /repo/path
  also to look at sessions by recency. list sessions in interesting ways...
```

### 2. `sessdb recent`

List recent sessions by **latest user activity**, not merely latest agent event.

Example usage:

```bash
sessdb recent
sessdb recent --limit 50
sessdb recent --source pi
sessdb recent --cwd codebrain
sessdb recent --since 7d
sessdb recent --long
sessdb recent --json
```

Default sort should prefer latest user message:

```sql
MAX(user_message.ts) DESC
```

Fallback to `COALESCE(ended_at, started_at, created_at)` only when a session has
no user message.

Default output should include:

```text
latest_user_time | source | user_msg_count | cwd/repo | session id | title | last_user_preview
```

This makes recency mean "what the user was recently thinking about", not "what an
agent most recently emitted".

### 3. `sessdb turns <session>`

Display a session as user-centered turns instead of raw events.

Example usage:

```bash
sessdb turns <session>
sessdb turns <session> --agent-chars 300
sessdb turns <session> --tool-chars 80
sessdb turns <session> --show-tools
sessdb turns <session> --from-seq 100 --limit 20
sessdb turns <session> --around-seq 142 --context-turns 2
sessdb turns <session> --all
sessdb turns <session> --json
```

Default behavior:

- user message shown prominently
- assistant messages shown as short previews
- tool calls/results hidden or heavily truncated by default
- live branch only unless `--all`
- output includes seq/event ids so context can be expanded further

Example output shape:

```text
[12] user:
  I think subagent is not that important tbh...

     assistant:
       Right now it is queryable, but not obvious...
```

---

## Phase 2 — Practical indexes and views

### 4. Add basic session/event indexes

Add ordinary SQLite indexes to support recency and user-message browsing:

```sql
CREATE INDEX IF NOT EXISTS ix_sessions_started
  ON sessions(started_at);

CREATE INDEX IF NOT EXISTS ix_sessions_ended
  ON sessions(ended_at);

CREATE INDEX IF NOT EXISTS ix_sessions_source_started
  ON sessions(source, started_at);

CREATE INDEX IF NOT EXISTS ix_sessions_cwd_started
  ON sessions(cwd, started_at);

CREATE INDEX IF NOT EXISTS ix_events_actor_type_ts
  ON events(actor, type, ts);
```

These are cheap and help `userlog`, `recent`, and filtered browsing.

### 5. Add a user-activity summary view/table if needed

Start with live SQL. If `recent` becomes awkward or slow, add a rebuildable derived
view/table such as:

```sql
session_user_summary(
  session_id,
  last_user_ts,
  last_user_seq,
  last_user_text,
  user_msg_count,
  medium_user_msg_count
)
```

This is not an ontology. It is just a compact index of user activity per session.

### 6. Add `event_files`

`refs.files` already exists inside event JSON, but agents need a fast primitive for
file-to-session lookup. Add a derived table that unrolls file references.

Proposed schema:

```sql
CREATE TABLE IF NOT EXISTS event_files (
  file       TEXT NOT NULL,
  event_id   TEXT NOT NULL,
  session_id TEXT NOT NULL,
  seq        INTEGER NOT NULL,
  source     TEXT,
  machine    TEXT,
  repo       TEXT,
  cwd        TEXT,
  ts         TEXT,
  PRIMARY KEY (file, event_id, session_id)
);

CREATE INDEX IF NOT EXISTS ix_event_files_file
  ON event_files(file);

CREATE INDEX IF NOT EXISTS ix_event_files_session
  ON event_files(session_id, seq);

CREATE INDEX IF NOT EXISTS ix_event_files_repo_file
  ON event_files(repo, file);
```

Then add:

```bash
sessdb touched codebrain/ingest.py
sessdb touched ingest.py --basename
sessdb touched codebrain/ --prefix
sessdb touched codebrain/cli.py --limit 20
sessdb touched codebrain/cli.py --json
```

Default output should join file evidence back to user intent by showing the nearest
preceding user turn:

```text
time | source | session id | seq | file | nearest_user_preview | event_preview
```

Known limitation: shell-side file mutations remain invisible unless represented
in structured tool args or patch envelopes.

---

## Phase 3 — Agent-oriented search

### 7. `sessdb find`

Add a higher-level search command that ranks sessions and shows useful context,
instead of only returning individual FTS event hits.

Example usage:

```bash
sessdb find "sqlite refresh ingest"
sessdb find "codebrain ingest" --context-turns 2
sessdb find "rollback" --source codex --limit-sessions 10
sessdb find "tool result" --include-agent
```

Default behavior:

1. Search user messages first, and preferably user-only by default.
2. Rank by session relevance, not only event relevance.
3. Show matching user event plus nearby turn context.
4. Truncate assistant/tool output by default.
5. Search assistant/tool text only with `--include-agent` or similar.

This command should answer:

> Which sessions contain relevant user intent that I should inspect?

not merely:

> Which rows matched?

Implementation can start with the existing `events_fts` table joined through
`session_events` and `sessions`, filtered by `events.actor='user'`. A separate
user-only FTS table is optional later if ranking/performance needs it.

---

## Phase 4 — Optional materialized turns

Start by computing turns live from the `transcript` view. If this becomes slow or
awkward, add a rebuildable derived table:

```sql
CREATE TABLE IF NOT EXISTS session_turns (
  session_id     TEXT NOT NULL,
  turn_index     INTEGER NOT NULL,
  user_event_id  TEXT,
  seq_start      INTEGER NOT NULL,
  seq_end        INTEGER NOT NULL,
  ts             TEXT,
  user_text      TEXT,
  agent_preview  TEXT,
  PRIMARY KEY (session_id, turn_index)
);
```

Do not add this first. The current `transcript` view should be enough for an
initial implementation.

---

## Recommended implementation order

1. Add `sessdb userlog`.
2. Add `sessdb recent` sorted around latest user activity.
3. Add `sessdb turns <session>` with `--around-seq` / `--context-turns`.
4. Add basic session/event indexes.
5. Add `event_files` derivation and `sessdb touched`, showing nearest user intent.
6. Add `sessdb find` with user-only/user-first defaults and turn context.
7. Add `session_user_summary` or `session_turns` only if live SQL becomes slow or awkward.

This order keeps the work incremental and immediately useful. It improves agent
retrieval quality without committing to semantic search, ontologies, or a complex
access layer.

## Later

Once the composable browsing layer feels good, revisit:

- semantic/vector search
- chunking strategy
- session summaries
- cross-machine pool-ingest UX (implemented: read-time remote pool refresh + `ingest-pool`)
- sub-agent ingestion if it becomes important

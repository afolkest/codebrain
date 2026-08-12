# codebrain

`codebrain` is a local, searchable store of coding-agent sessions from Claude,
Codex, Cursor, and pi. It is meant for future agents trying to reconstruct user
intent: goals, constraints, preferences, decisions, corrections, and the context
around files or repos.

Raw evidence is the source of truth. The SQLite DB is a rebuildable cache:

```text
source logs / safe Cursor projection -> per-source adapters -> SQLite -> CLI / FTS / raw SQL
```

See [DESIGN.md](DESIGN.md) for architecture, [SCHEMA.md](SCHEMA.md) for the DB
contract, [SYNCING.md](SYNCING.md) for cross-machine sync, and
[`formats/`](formats/) for reverse-engineered source formats.

## Quick Start

```bash
pip install -e .
sessdb ingest
```

Daily read commands refresh first: changed local live logs, a safe projection of
settled Cursor sessions, and synced remote pool logs are delta-ingested before the
query runs. Use `--no-refresh` only when you explicitly want to skip that.

## Agent Workflow

Start broad, then pivot to evidence:

```bash
sessdb recent
sessdb userlog --query "preference or topic"
sessdb search "query terms" --around 1
sessdb turns <session> --around-seq <seq>
sessdb refs <session>
sessdb touched <path>
sessdb lineage <session>
```

Practical patterns:

- "What was I working on recently?" -> `recent`, then `turns <session>`.
- "What did I say about X?" -> `userlog --query X` or `search X --actor user`.
- "Why did this file change?" -> `touched path/to/file`, then `turns`.
- "What files/commands did that conversation touch?" -> `refs <session>`.
- "Is this a worker/sub-agent thread?" -> `lineage <session>`.
- "The DB view is not enough." -> `show <session> --all`, `grep`, or raw SQL.

## Core Commands

Intent browsing:

```bash
sessdb recent                 # sessions by latest clean human user activity
sessdb userlog                # newest clean human user messages
sessdb search <query>         # FTS over live authored event text
sessdb search <query> --around 2
sessdb turns <session>        # user-centered turn view
sessdb turns <session> --turn -1
```

Evidence pivots:

```bash
sessdb refs <session>         # files, commands, commits referenced in session
sessdb touched <path>         # sessions/events with structured file refs
sessdb touched name.py --basename
sessdb touched src/ --prefix
sessdb lineage <session>      # root, parent, current, children, siblings
```

Visibility and repair:

```bash
sessdb hide <session> --reason "noisy eval"
sessdb hidden
sessdb unhide <session>
sessdb collect --pool ~/codebrain-pool
sessdb collect --install-launchd --pool ~/codebrain-pool --interval 300
sessdb sweep --install-launchd --interval 300   # collect + refresh in the background
sessdb ingest                 # full local rebuild
sessdb ingest-pool            # explicit synced-pool repair/debug ingest
sessdb backfill-claude <zip-or-dir>
```

Escape hatches:

```bash
sessdb show <session> --all   # raw transcript view, including rolled-back events
sessdb list                   # session metadata by start time
sessdb grep <pattern>         # local source roots + remote pool; Cursor uses safe archive
sessdb schema                 # print DDL for sqlite3 clients
```

## Filters

Common filters compose across the discovery commands:

```bash
--source claude|codex|cursor|pi
--cwd <substring>
--after 2026-01-01
--before 7d
--exclude-recent 1h
--only-session <id-or-prefix>
--exclude-session <id-or-prefix>
--include-inherited
--include-subagents
--include-hidden | --only-hidden
--json
```

`search` also supports:

```bash
--actor user|assistant|tool
--type message|tool_call|tool_result
--show-tools
--user-chars 0 --agent-chars 0 --tool-chars 0
```

Default discovery excludes hidden sessions, sub-agent sessions, and inherited
pi/Cursor copies. Those are retrieval defaults, not data deletion.

## Human Intent vs Control Input

Some control surfaces send text into another worker/thread while the receiving
agent records it as a native `user` message. `codebrain` uses structured evidence
to keep those messages out of default human-intent retrieval:

- bmux event logs: `~/.bmux/events/bmux.jsonl`
- Codex MCP `codex` / `codex-reply` tool calls
- Codex multi-agent `send_input`
- Codex native inter-agent messages, which are parsed as non-user messages
- Cursor simulated messages, plan execution, and sub-agent kickoff input

Native user-message origins:

```text
human | master_control | unknown
```

Defaults use `--origin human`. Inspect control-plane input explicitly:

```bash
sessdb userlog --origin all
sessdb userlog --origin master-control
sessdb userlog --origin unknown
sessdb search "query" --actor user --origin all
sessdb codex-control-sync
sessdb cursor-provenance-sync
sessdb bmux-sync
```

`turns` and `show` keep transcript content complete and label non-human
user-message origins instead of hiding them.

## Sync Model

`collect` mirrors allowlisted raw evidence into an append-only pool:

```text
~/codebrain-pool/raw/<machine>/<source>/...
```

Sync that pool with Syncthing. Do not sync the SQLite DB or live tool homes.
Cursor is the deliberate source-boundary exception: collection copies only
codebrain's allowlisted immutable projection, never Cursor's live database.
Normal read commands use local live logs, the local settled Cursor projection,
and synced remote pool subtrees. See [SYNCING.md](SYNCING.md) for machine-name
aliases and stale-local-pool behavior.

`sweep` runs collect and then the same delta refresh the read commands use
(local sources, synced pool, provenance overlays) in one background pass.
Installing it with `sessdb sweep --install-launchd` replaces the collect-only
LaunchAgent (same label); read commands still refresh, but a periodic sweep
absorbs heavy agent activity in the background so reads stay fast.

Old Claude backups are imported into the pool, not restored into live
`~/.claude`:

```bash
sessdb backfill-claude ~/claude-restore --dry-run
sessdb backfill-claude ~/claude-restore
```

## Model

The normalized DB has one content table and one placement table:

- `events`: deduped event content with source-prefixed IDs.
- `session_events`: per-session order, parent, liveness, and inherited status.
- `sessions`: metadata, lineage, relation, spawn links, and tip.
- rebuildable overlays: FTS, file refs, provenance evidence/effective origins.

Read live transcript rows with:

```sql
SELECT * FROM transcript
WHERE session_id = ? AND live = 1
ORDER BY seq;
```

Source adapters normalize different raw shapes into that model:

- Claude: `parentUuid` tree, compaction reconnection, parallel tool results,
  sidechain de-duplication.
- Codex: flat append-only log, synthesized turn forest, rollback markers,
  control-message provenance, apply-patch refs, sub-agent/fork lineage.
- Cursor: ordered composer bubbles from an immutable safe projection, structured
  tool pairing/control provenance, copied-prefix inheritance, sub-agent lineage.
- pi: `parentId` tree, resume/branch copied prefixes, cross-file dedup,
  structured sub-agent lineage.

## Known Gaps

- Nested Claude/pi sub-agent transcripts are collected/backfilled but not part of
  normal top-level ingest discovery.
- Shell-side file mutations are not tracked in `refs` / `touched`.
- Codex reasoning is encrypted in current logs and excluded.
- Cursor CLI/ACP protobuf history, remote Background Agents, unordered orphan
  bubbles, and ancillary project artifacts are not ingested.
- Embeddings / sqlite-vec are not a current retrieval path.
- `title` and repo resolution are incomplete for some sources.

## Verify

```bash
python3 -m unittest discover
git diff --check
```

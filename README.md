# codebrain

A searchable store of my coding-agent sessions (Claude, Codex, pi). See
[DESIGN.md](DESIGN.md) for architecture, [SCHEMA.md](SCHEMA.md) for the canonical
schema, and [`formats/`](formats/) for the reverse-engineered log formats.

**Status:** all three sources land on one schema —
`raw logs → per-source adapter → SQLite (3 tables) → CLI + FTS + grep + raw SQL`.
Proven on real logs: **~1,970 sessions / ~380k events** from Claude, Codex, and pi,
ingested with zero errors (claude 90% live, codex 98%, pi 94%).

## Quickstart

```bash
pip install -e .            # or run as: python3 -m codebrain <cmd>

sessdb ingest               # first build from ~/.claude + ~/.codex + ~/.pi (read-only)
sessdb collect              # mirror raw logs → ~/codebrain-pool (append-only archive)
sessdb list                 # recent sessions (any source)
sessdb recent               # sessions by latest live user message
sessdb userlog              # recent live user messages (intent-first)
sessdb turns <session>      # user-centered turns with truncated agent context
sessdb show <session>       # a session's live transcript (--all includes rolled-back)
sessdb search <query>       # FTS5 over event text; filters + optional turn context
sessdb lineage <session>    # factual parent/child session lineage
sessdb refs <session>       # files/commands/commits referenced by a session
sessdb grep <pattern>       # ripgrep over the raw logs (all sources)
sessdb schema               # print the DDL
```

**Reads are always current.** Read commands (`list`/`recent`/`userlog`/`turns`/
`show`/`search`/`lineage`/`refs`) first delta-ingest whatever changed on disk (`refresh()`:
stat-scan + re-parse only new/grown files — tens of
ms when idle), so query results include sessions that are live *right now*; ask
about another session's last message seconds after it happened. `--no-refresh`
skips it; `sessdb ingest` is only for the first build or a full rebuild.

**Raw is archived.** `sessdb collect` mirrors the tool homes into an append-only
pool (`~/codebrain-pool/raw/<machine>/<source>/…`) so upstream cleanup can never
take sessions with it: allowlisted files only (credentials stay home), incremental
stat-compare sweeps (ms when idle), shrink-guarded, never deletes. Session-data
dirs are taken whole, so it also captures what ingest doesn't parse yet — subagent
transcripts + metadata, tool-result sidecars, session indexes, project memory,
task state, pre-edit file history. `sessdb collect --install-launchd` makes it a
periodic LaunchAgent (default every 30 min). Cross-machine sync later = point
Syncthing at the pool; per-machine subtrees can't conflict, and ingesting a synced
subtree keeps sessions labeled with their **origin** machine (from the
`raw/<machine>/` path, per SCHEMA.md).

The DB is a **rebuildable cache** (DESIGN.md golden rule): delete `~/.codebrain/codebrain.db`
and re-ingest anytime. It is never synced. Point any sqlite3 client at it for
arbitrary joins — the schema is the interface (`sessdb schema`).

## Model (SCHEMA.md)

- **`events`** — deduped content (`<source>:`-prefixed, copy-invariant ids).
- **`session_events`** — per-session placement (`seq`, `parent_event_id`, `live`,
  `inherited`); the forest lives here, so an event can be live in one session and
  rolled-back in another.
- **`sessions`** — metadata + lineage/sub-agent links + tip.

Read a transcript: `SELECT * FROM transcript WHERE session_id=? AND live=1 ORDER BY seq`.

## What each adapter handles

- **Claude** (`parentUuid` tree) — bridged-parent linearization, parallel-tool
  results, compaction reconnection, resume re-emission dedup.
- **Codex** (flat append-only log, no native tree) — **synthesized** turn forest:
  turns anchored on the clean `user_message` (works on 0.39→0.137, incl. the old
  logs that lack `task_started`); `thread_rolled_back{n}` pops live user-turns as
  dead side branches; `apply_patch`/`patch_apply_end` files; sub-agent/fork lineage.
- **pi** (`parentId` tree) — the cross-file case the schema exists for: resume/branch
  copies the parent's live prefix **verbatim** into a new file, so a shared event is
  **one `events` row** with N placements (origin `inherited=0` + copies `inherited=1`),
  keyed on the copy-invariant `pi:<8hex>:<ts>`.

## Current limitations

- **Main transcript path only** — sub-agents are deferred: Claude `<sessionId>/subagents/`
  (inline `isSidechain` copies are ignored), pi `<session>/<runId>/run-<i>/`. Codex
  sub-agent rollouts *are* ingested (they're standalone files) with a parent-session
  link; the spawn-event link (`spawn_event_id`) is a later cross-file slice.
- Refresh covers **this machine's** tool homes; the pool is collected locally,
  but cross-machine replication (point Syncthing at the pool) and
  embeddings/sqlite-vec are later slices.
- `bash`-side file mutations aren't tracked in `refs` (known gap across all sources);
  Codex reasoning is encrypted (≥2026-04) and excluded everywhere.

## Tests

Stdlib `unittest`, no dependencies — run from the repo root:

```bash
python3 -m unittest discover        # 64 tests, a second or two
```

Synthetic JSONL fixtures (inline, next to the assertions, so each doubles as a
shape spec) pin every adapter and every bug we've fixed: the Codex full-rollback
**null tip**, MCP capture/dedup, `apply_patch` refs, the **pi cross-file dedup**
(one `events` row, N placements), and the conflict-skip + malformed-record
hardening. A shared invariant check — source-prefixed unique ids, parents resolve
in-session, **no live event hanging off a dead parent**, tip is live-or-null, no
parent cycles — runs on every fixture *and* over a sample of the real logs
(`test_smoke_real`, which skips cleanly on machines without them). `test_refresh`
pins the delta path: only changed files re-parse, a grown file flips liveness,
upstream deletion never deletes from the archive, and the FTS triggers keep the
index current without rebuilds. `test_collect` pins the pool sweep: allowlists
keep credentials out, symlinks are never followed, the shrink guard keeps a
truncated source from clobbering the archive (without wedging recovery), stale
tmp files are pruned without touching a concurrent sweep's, the LaunchAgent
plist survives hostile path characters, and a pool subtree ingests exactly like
a live home — **with sessions keeping their origin machine's label**, so a
synced subtree from another machine is never mislabeled.

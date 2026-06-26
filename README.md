# codebrain

A searchable store of my coding-agent sessions (Claude, Codex, pi). See
[DESIGN.md](DESIGN.md) for architecture, [SCHEMA.md](SCHEMA.md) for the canonical
schema, and [`formats/`](formats/) for the reverse-engineered log formats.

**Status:** all three sources land on one schema —
`raw logs → per-source adapter → SQLite (canonical 3-table transcript model +
small rebuildable overlays) → CLI + FTS + grep + raw SQL`.
Proven on real logs: **~1,970 sessions / ~380k events** from Claude, Codex, and pi,
ingested with zero errors (claude 90% live, codex 98%, pi 94%).

## Quickstart

Install and build the local SQLite cache:

```bash
pip install -e .            # or run as: python3 -m codebrain <cmd>
sessdb ingest               # first build from ~/.claude + ~/.codex + ~/.pi (read-only)
```

Daily intent archaeology loop:

```bash
sessdb recent               # sessions by latest clean human user message
sessdb userlog              # newest clean human messages across sessions
sessdb search <query>       # FTS5 over event text; add --around N for turn context
sessdb turns <session>      # expand user-centered turns around a seq/session
sessdb lineage <session>    # factual parent/child session lineage
sessdb refs <session>       # conversation -> files/commands/commits
sessdb touched <path>       # file/artifact -> sessions/events
```

Default retrieval hides sessions with `sessions.hidden_at` set. Use
`sessdb hide <session> --reason <why>` for noisy/eval sessions, `sessdb unhide`
to restore them, `sessdb hidden` to audit them, and `--include-hidden` /
`--only-hidden` on discovery commands when needed. This is DB visibility only:
raw logs and pool files are not deleted, and deleting/rebuilding the DB loses
manual hidden markers.

**Reads are always current.** Read commands first delta-ingest changed local live
logs plus synced remote pool subtrees when `~/codebrain-pool/raw` exists. The
refresh is a stat-scan + re-parse only new/grown files, so it is usually ms when
idle. `--no-refresh` skips both live-home and pool refresh; lightweight bmux
provenance still syncs when its event log changed so human-intent defaults stay
clean. `sessdb ingest` is only for the first build or a full rebuild.

**bmux provenance keeps master-control prompts out of user-intent retrieval.**
When bmux sends text into a worker pane, the native agent transcript still records
that text as a `user` message. codebrain reads `~/.bmux/events/bmux.jsonl` and
labels matching transcript messages with an origin:

```text
human | master_control | unknown
```

Intent commands default to clean human input:

```bash
sessdb recent
sessdb userlog
sessdb search "query" --actor user
```

Use `--origin` when you need to inspect control messages too:

```bash
sessdb userlog --origin all
sessdb userlog --origin master-control
sessdb userlog --origin unknown
sessdb search "query" --actor user --origin all
sessdb bmux-sync             # explicitly rebuild the bmux provenance overlay
```

`sessdb turns <session>` and `sessdb show <session>` keep the transcript complete
and label non-human user-message origins instead of hiding them.

Sync / archive setup is separate from daily retrieval:

```bash
sessdb collect --pool ~/codebrain-pool
sessdb collect --install-launchd --pool ~/codebrain-pool --interval 300
```

`collect` mirrors allowlisted raw session/history files into
`~/codebrain-pool/raw/<machine>/<source>/…` so upstream cleanup cannot take them
with it. Point Syncthing at `~/codebrain-pool`; do **not** sync the SQLite DB or
live tool homes. See [SYNCING.md](SYNCING.md) for setup, machine-name aliases, and
latency details.

Ops / escape hatches:

```bash
sessdb list                 # session metadata by start time; recent is usually better
sessdb show <session>       # raw transcript view (--all includes rolled-back)
sessdb grep <pattern>       # grep local live logs + synced remote pool roots
sessdb bmux-sync            # rebuild bmux provenance labels from ~/.bmux/events/bmux.jsonl
sessdb schema               # print the DDL for direct sqlite3 queries
sessdb ingest-pool          # debug/repair explicit pool ingest; normal reads do this on demand
sessdb backfill-claude ~/claude-restore --dry-run   # inspect old Claude backup zips
sessdb hidden               # audit sessions hidden from default retrieval
sessdb unhide <session>     # restore a hidden session to default retrieval
```

Passing explicit paths to `sessdb grep` replaces the default live+remote scope.

**Old Claude backups are backfilled, not restored into live `~/.claude`.**
`sessdb backfill-claude <zip-or-dir>` scans historical Claude `.zip` snapshots
read-only, selects one best main transcript per structured Claude `sessionId`,
skips sessions already present in live `~/.claude`, retargets old top-level
`agent-*.jsonl` sidechain files into the modern `<session>/subagents/` sidecar
layout, skips `file-history/`, and writes a manifest into
`~/codebrain-pool/raw/claude-backfill/claude/`. Normal read commands ingest that
pool-shaped root on demand; explicit ingest remains available:

```bash
sessdb backfill-claude ~/claude-restore
sessdb ingest --source claude --raw-root ~/codebrain-pool/raw/claude-backfill/claude
```

The DB is a **rebuildable cache** (DESIGN.md golden rule): delete `~/.codebrain/codebrain.db`
and re-ingest anytime. It is never synced. Point any sqlite3 client at it for
arbitrary joins — the schema is the interface (`sessdb schema`).

## Model (SCHEMA.md)

- **`events`** — deduped content (`<source>:`-prefixed, copy-invariant ids).
- **`session_events`** — per-session placement (`seq`, `parent_event_id`, `live`,
  `inherited`); the forest lives here, so an event can be live in one session and
  rolled-back in another.
- **`sessions`** — metadata + lineage/sub-agent links + tip.
- **bmux overlay tables** — rebuildable provenance labels derived from bmux's
  event log, used to distinguish clean human input from master-control prompts.

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
- Refresh covers this machine's live tool homes plus synced remote pool subtrees;
  embeddings/sqlite-vec are later slices.
- `bash`-side file mutations aren't tracked in `refs`/`touched` (known gap across
  all sources); Codex reasoning is encrypted (≥2026-04) and excluded everywhere.

## Tests

Stdlib `unittest`, no dependencies — run from the repo root:

```bash
python3 -m unittest discover        # full test suite, a second or two
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

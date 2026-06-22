# Pool Sync Refresh Plan

## Goal

Make cross-machine history usable with normal `sessdb` read commands after the user
sets up Syncthing on `~/codebrain-pool`.

A normal command like:

```bash
sessdb search "pipeline redesign"
sessdb recent
sessdb touched docs/wip/pipeline-redesign.md
```

should see:

- fresh sessions from this machine's live tool homes (`~/.claude`, `~/.codex`, `~/.pi`)
- synced sessions from other machines' pool subtrees (`~/codebrain-pool/raw/<machine>/<source>`)

No SQLite DB sync. No manual "ingest remote logs" step for everyday use.

## Current state

There are already two working paths:

```text
local live tool homes -> SQLite
```

This happens automatically before read commands via `cli._open()` -> `ingest.refresh()`.

```text
local live tool homes -> ~/codebrain-pool/raw/<this-machine>/<source>
```

This happens via `sessdb collect`, optionally installed as a LaunchAgent.

The missing bridge is:

```text
synced remote pool subtrees -> SQLite
```

## Target steady-state flow

On each machine:

```text
~/.claude ~/.codex ~/.pi
  -> periodic sessdb collect
  -> ~/codebrain-pool/raw/<this-machine>/<source>
  -> Syncthing
  -> other machines' ~/codebrain-pool/raw/<this-machine>/<source>
  -> normal sessdb read command refreshes remote pool subtrees into local SQLite
```

User-facing latency:

- local current sessions: visible immediately on next `sessdb` command
- remote sessions: visible after remote collect interval + Syncthing latency + next local `sessdb` command

## Design principles

- Keep SQLite local and rebuildable. Do not sync `~/.codebrain/codebrain.db`.
- Keep tool homes local. Do not sync `~/.claude`, `~/.codex`, or `~/.pi` directly.
- Use the pool only for allowlisted raw session/history artifacts.
- Preserve origin machine from `raw/<machine>/<source>`.
- Treat machine names as stable identities. Prefer the default hostname, or one explicit
  alias used consistently by `collect`, launchd, and pool refresh.
- Do not let a stale local pool copy overwrite fresher live-home ingest.
- Avoid adding new product/retrieval primitives. This is ops/hardening for the existing system.

## Implementation plan

### 1. Add pool discovery/refresh helpers

In `codebrain/ingest.py`, add helpers roughly like:

```python
def local_machine_names(explicit: str | None = None) -> set[str]:
    ...

def discover_pool_roots(pool_root: Path, sources=SOURCES, machines=None,
                        include_local=False,
                        local_machines: set[str] | None = None) -> list[tuple[str, str, Path]]:
    ...

def refresh_pool(conn, pool_root: Path, sources=SOURCES, machines=None,
                 include_local=False,
                 local_machines: set[str] | None = None) -> dict:
    ...
```

`local_machine_names` should include at least `socket.gethostname()`, plus any
explicit configured/env alias this machine uses for collection. A minimal version can
honor environment variables such as `CODEBRAIN_MACHINE` and/or
`CODEBRAIN_LOCAL_MACHINES=mac-mini,mini` before a fuller config system exists.

`discover_pool_roots` scans:

```text
<pool>/raw/<machine>/<source>
```

and returns existing source roots only. It should ignore non-directories and validate
CLI-provided `--machine`/`--source` path components (no `/`, `..`, empty names).

`refresh_pool` calls existing `refresh(conn, sources=(source,), roots={source: root})`
for each discovered `(machine, source, root)`.

Important: pass `machine=None` when refreshing a pool root so existing
`_machine_for_root()` derives the origin machine from the path. This preserves the
remote machine label.

Return the same stats shape as `refresh()`:

```python
{"files": ..., "sessions": ..., "events": ..., "placements": ...,
 "skipped": ..., "conflicts": ..., "errors": ...}
```

plus `pool_roots` count and a list/count of skipped local roots for diagnostics if
useful. Root ordering should be deterministic (`machine`, then `source`) so repeated
runs behave consistently.

If the same `session_id` is parsed from more than one machine subtree, existing
upserts are last-writer-wins for `sessions` and placements. That should be rare, but
pool refresh can emit a low-noise warning/diagnostic count so copied/colliding
session ids are visible during setup.

### 2. Auto-refresh remote pool subtrees before read commands

In `codebrain/cli.py`, update `_open(args)`:

Current:

```python
stats = refresh(conn)
```

Target:

```python
live_stats = refresh(conn)
pool_stats = refresh_pool(conn, DEFAULT_POOL, include_local=False,
                          local_machines=local_machine_names())
```

Only do pool refresh if `DEFAULT_POOL / "raw"` exists.

Automatic read-time pool refresh should skip this machine's own pool subtree by
default. Reason: local live homes are fresher. A stale local pool file for the same
session can otherwise re-parse older placements after the live-home refresh and
replace fresher `session_events` for that session.

Use the local machine identity set, not just raw `socket.gethostname()`, so users who
collect with an alias or rename a host can still exclude their own stale local pool.
Docs should strongly recommend stable unique machine names across all synced devices.

Read-time behavior:

```text
local live homes -> SQLite
remote pool subtrees -> SQLite
query
```

`--no-refresh` should skip both live-home refresh and pool refresh.

Print a stderr refresh notice if either live or pool refresh ingested changed files.
Keep it compact, for example:

```text
(refreshed local 2 file(s), +10 events; pool 4 file(s), +30 events)
```

or retain the old message shape if only local files changed.

### 3. Add a manual/debug `ingest-pool` command

Add CLI command:

```bash
sessdb ingest-pool
sessdb ingest-pool --pool ~/codebrain-pool
sessdb ingest-pool --machine macbook
sessdb ingest-pool --source pi
sessdb ingest-pool --include-local
```

Recommended semantics:

- default: ingest remote pool subtrees only, same as read-time refresh
- `--include-local`: also ingest this machine's pool subtree; useful for fresh
  rebuilds, debugging, or machines where live homes are unavailable. On a non-empty
  DB this is an explicit footgun unless live homes are reparsed afterward, because a
  stale local pool can overwrite fresh placements and normal delta refresh may not
  notice unchanged live files. Prefer making the command safe by doing a final full
  live-home reparse for included local sources when live roots exist; otherwise print
  a prominent warning that this is for rebuild/offline use.
- `--machine`: restrict to one pool machine subtree; validate the component. If it
  names a local machine alias, require `--include-local` or print a clear
  skipped-local message
- `--source`: restrict to one source
- `--pool`: override default `~/codebrain-pool`

This command is not required in normal steady-state use once read-time pool refresh
exists. It is an explicit ops/debug escape hatch.

Do not implement `ingest-pool` by calling `_open(args)`: `_open()` would run the
automatic all-remote-pool refresh before the command's own `--machine`/`--source`
filters, making the debug command misleading. It should connect directly and call
only the requested `refresh_pool(...)` operation, plus the safety reparse/warning for
`--include-local`.

### 4. Add docs

Add `SYNCING.md` explaining the three layers:

```text
1. live tool homes: ~/.claude ~/.codex ~/.pi
2. syncable raw pool: ~/codebrain-pool/raw/<machine>/<source>
3. local SQLite cache: ~/.codebrain/codebrain.db
```

Setup instructions:

```bash
mkdir -p ~/codebrain-pool
sessdb collect --pool ~/codebrain-pool
sessdb collect --install-launchd --pool ~/codebrain-pool --interval 300
```

Syncthing instructions:

- sync `~/codebrain-pool`
- do not sync `~/.codebrain/codebrain.db`
- do not sync `~/.claude`, `~/.codex`, `~/.pi`
- choose stable unique machine names. If using `sessdb collect --machine <alias>`,
  configure the same alias for local pool exclusion (for example via the planned
  `CODEBRAIN_MACHINE` / `CODEBRAIN_LOCAL_MACHINES` support).
- add Syncthing ignore for temp copy files:

```text
(?d)**/*.part
```

Explain latency:

```text
local session: next sessdb command
remote session: remote collect interval + Syncthing latency + next local sessdb command
```

Update `README.md` to link `SYNCING.md` and clarify that normal reads refresh local
live homes plus remote synced pool subtrees.

### 5. Tests

Add tests around the new pool helpers and CLI behavior.

Minimum tests:

1. `refresh_pool` ingests a remote pool subtree and preserves origin machine from
   `raw/<machine>/<source>`.
2. `refresh_pool(... include_local=False)` skips all configured local machine aliases,
   not just `socket.gethostname()`.
3. A normal read command (`recent` or `search`) without `--no-refresh` sees a session
   that exists only in a remote pool subtree.
4. The same normal read command with `--no-refresh` does not ingest that remote pool
   subtree.
5. Auto read refresh is a no-op when `~/codebrain-pool` is absent.
6. Stale local pool does not overwrite fresher live-home state in the automatic path,
   including a custom local alias/hostname-change scenario.
7. `ingest-pool --include-local` can ingest a local pool subtree explicitly and either
   performs the safety live-home reparse or emits the expected warning.
8. `ingest-pool --machine <remote> --source pi` limits roots as expected and does not
   pre-ingest unrequested roots through `_open()`.
9. Invalid `--machine` values with path separators or `..` are rejected.

Existing tests already prove a pool subtree ingests like a live home and preserves
origin machine; reuse those fixture patterns from `tests/test_collect.py`.

## Edge cases / risks

### Stale local pool overwriting fresh live session

This is the main correctness risk. Avoid by skipping local pool in automatic read
refresh. Use a local-machine alias set rather than only `socket.gethostname()`.
Keep `--include-local` explicit, and make it safe or very loud:

- safe path: after including local pool, force a final live-home reparse for the same
  sources when live roots exist, so stale pool placements cannot remain authoritative
- warning path: document/print that `--include-local` is for fresh rebuilds or
  unavailable live homes, not routine use on a populated DB

### Duplicate events from local live home and remote pool

Expected and safe. Event ids are source-prefixed and copy-invariant. Placements are
per session. Different machines should have different session ids except for synced
copies of the same original logs, which dedupe through existing upserts.

Duplicate `session_id`s across different machine subtrees are less safe because
`sessions` and `session_events` are keyed only by `session_id`. Treat this as a setup
anomaly: process roots deterministically and surface a diagnostic if the same session
id is seen from multiple pool machine roots.

### Remote pool file still being synced

`collect` writes via `.part` then atomic rename. Syncthing should ignore `.part`.
If a file changes during sync, refresh stat tracking will re-parse on a later command
when mtime/size changes.

### Performance

Read-time pool refresh should be delta-based through `ingest_state`, so idle cost is
file discovery/stat scan. This is acceptable initially; measure before adding another
index/cache. If it becomes slow, polish mode can add diagnostics or a config switch.

### Machine name detection

Use `socket.gethostname()` as the default local identity, matching `collect` default,
but do not make it the only identity. Explicit `--machine` in `collect`, host renames,
or duplicate hostnames can make a local pool subtree look remote. Add a small local
alias mechanism before/with pool auto-refresh (environment variable is enough for this
slice), document stable unique machine names, and test custom-alias exclusion.

## Non-goals

- No syncing SQLite.
- No syncing tool homes directly.
- No new retrieval/product commands beyond `ingest-pool` ops/debug.
- No schema changes unless tests reveal a correctness issue.
- No benchmark work in this slice; finish cross-machine plumbing first.

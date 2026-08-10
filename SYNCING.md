# Cross-machine sync

`codebrain` syncs raw session evidence, not the SQLite cache. For Cursor, the
syncable evidence is codebrain's safe immutable projection—not Cursor's live
SQLite database.

## Layers

```text
1. live tool homes
   ~/.claude  ~/.codex  ~/.pi
   Cursor state.vscdb (read-only source)

      -> Cursor safe projection at ~/.codebrain/cursor-raw
      -> sessdb collect

2. syncable raw pool
   ~/codebrain-pool/raw/<machine>/<source>/...

      <-> Syncthing
      -> sessdb read-time refresh / sessdb ingest-pool

3. local SQLite cache
   ~/.codebrain/codebrain.db
```

Do **not** sync the SQLite DB. It is a rebuildable local cache.

## One-time setup on each Mac

Create the pool and collect once:

```bash
mkdir -p ~/codebrain-pool
sessdb collect --pool ~/codebrain-pool
```

Install periodic collection:

```bash
sessdb collect --install-launchd --pool ~/codebrain-pool --interval 300
```

That copies allowlisted session/history artifacts from local tool homes into:

```text
~/codebrain-pool/raw/<this-machine>/<source>/...
```

Every machine should write only its own `raw/<machine>/` subtree.

## Syncthing setup

Sync this folder:

```text
~/codebrain-pool
```

Do not sync:

```text
~/.codebrain/codebrain.db
~/.claude
~/.codex
~/.pi
~/Library/Application Support/Cursor
~/.cursor
~/.codebrain/cursor-raw
```

The local Cursor projection archive is collected into the pool; sync the pool,
not either its upstream database or its private working archive.

Add this Syncthing ignore pattern for collector temp files:

```text
(?d)**/*.part
```

## Normal use

After setup, normal read commands refresh both:

```text
local live tool homes and settled Cursor projections
remote synced pool subtrees
```

So these should see local current sessions plus synced remote history:

```bash
sessdb recent
sessdb search "some phrase"
sessdb touched docs/wip/pipeline-redesign.md
```

`--no-refresh` skips both live-home and pool refresh.

## Latency

Local sessions:

```text
tool writes ~/.pi -> next sessdb command refreshes live home -> visible
Cursor commits a settled composer
-> next sessdb command projects an immutable safe revision -> visible
```

Remote sessions:

```text
remote tool writes ~/.pi
-> remote collect interval
-> Syncthing latency
-> next local sessdb command refreshes remote pool
-> visible
```

The same remote sequence applies to Cursor after the remote collector projects a
settled revision. Active, queued, draft, or internally incomplete Cursor sessions
retain their last settled revision and become visible after a later successful
projection. Changed header tokens bypass exporter retry delay; active/incomplete
states retry on a short capped backoff, while unchanged drafts, absent source
rows, and structured source errors back off until the next daily retry or full
reconciliation.

With `--interval 300`, remote freshness is usually bounded by about five minutes
plus Syncthing latency and machine sleep/offline time, then whichever local
`sessdb` read command you run next.

## Manual/debug pool ingest

Normal commands ingest remote pool subtrees from the default `~/codebrain-pool` on
demand. If you use a custom pool path, automatic read-time refresh will not see it
yet; use `ingest-pool --pool ...` explicitly until a config/global pool option
exists.

For setup/debug, use:

```bash
sessdb ingest-pool
sessdb ingest-pool --pool ~/codebrain-pool
sessdb ingest-pool --machine macbook --source pi
```

By default this skips this machine's own pool subtree, because live tool homes are
fresher and should be authoritative.

To explicitly ingest local pool data, use:

```bash
sessdb ingest-pool --include-local
```

This reparses local live homes afterward when available, so a stale local pool copy
cannot remain authoritative in the DB. Prefer `--include-local` only for rebuilds,
debugging, or machines where live tool homes are unavailable.

## Machine names

The pool layout uses machine names as stable origins:

```text
~/codebrain-pool/raw/<machine>/<source>/...
```

By default `collect` uses `socket.gethostname()`. If you use a custom name:

```bash
sessdb collect --machine mini --pool ~/codebrain-pool
```

use that name consistently in the LaunchAgent and local exclusion config. Minimal
environment hooks:

```bash
export CODEBRAIN_MACHINE=mini
export CODEBRAIN_LOCAL_MACHINES=mini,old-mini-name
```

`CODEBRAIN_LOCAL_MACHINES` lets read-time pool refresh skip local aliases after a
host rename or custom collection name.

## Cursor safety boundary

Cursor stores local chat state at:

```text
~/Library/Application Support/Cursor/User/globalStorage/state.vscdb
```

That live SQLite database and its WAL contain more than transcript evidence,
including opaque application state and potentially sensitive configuration.
`codebrain` opens it with SQLite `mode=ro` and `query_only`, takes a coherent
WAL-aware read transaction, and recursively projects only reviewed transcript
fields. It never treats `state.vscdb`, its WAL/SHM files, `~/.cursor`, project
sidecars, logs, tool-output artifacts, or credentials as collection roots.
The boundary prevents unrelated application state from being swept up; it does
not make transcript content secret-free. Visible messages and lossless tool
arguments/results can contain source, paths, output, or credentials, so the safe
archive and pool remain private trusted-device storage.

The safe archive lives at `~/.codebrain/cursor-raw` and contains immutable,
content-hashed per-session revision segments. Each segment records the complete
logical bubble order and only new or changed allowlisted payloads; a revision is
ingestible only when its chain reconstructs fully. `collect` copies every
reconstructible segment to:

```text
~/codebrain-pool/raw/<machine>/cursor/sessions/<session-hash>/revisions/...
```

Ingest reads only the latest reconstructible revision for each session. Remote
out-of-order arrivals remain unavailable until their predecessors arrive. Pool
publication is create-only: identical arrivals are no-ops and a conflicting
file is preserved and reported rather than overwritten. Exporter state, locks,
and temporary files remain local and never enter the pool.

Export authority is split by caller. Read commands refresh opportunistically:
they never wait for a concurrent exporter (a held lock skips the export and
ingests the last published heads) and they project only sessions whose header
tokens changed. The periodic collector sweep is the authoritative exporter: it
owns the daily full reconcile, retry-due sessions, and stale temp pruning. A
read command is therefore never the process that pays for archive maintenance;
its Cursor view is at most one collector interval stale when it skips.

Read-time discovery caches no-follow revision metadata per archive root and
hashed session directory. Unchanged directories open no revision JSON; an
arrival, deletion, or in-place repair revalidates only the affected chain. The
cache is rebuildable and records selected and successfully handled heads
separately, so a write failure retries and a predecessor arriving after its
successor immediately unlocks the latest reconstructible head. Canonical Cursor
state advances only to a greater `(revision, snapshotDigest)` rank, so a stale
copy from another root cannot roll it back.

Default `sessdb grep` searches the safe Cursor archive plus pool roots; it never
falls back to the live database or broad Cursor home directories. A custom
Cursor raw root is always a consumer-only archive root and does not trigger a
live export.

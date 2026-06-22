# Cross-machine sync

`codebrain` syncs raw session archives, not the SQLite cache.

## Layers

```text
1. live tool homes
   ~/.claude  ~/.codex  ~/.pi

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
```

Add this Syncthing ignore pattern for collector temp files:

```text
(?d)**/*.part
```

## Normal use

After setup, normal read commands refresh both:

```text
local live tool homes
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
```

Remote sessions:

```text
remote tool writes ~/.pi
-> remote collect interval
-> Syncthing latency
-> next local sessdb command refreshes remote pool
-> visible
```

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

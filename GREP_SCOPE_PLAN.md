# Grep Scope Plan

Status: proposed.

## Goal

Make `sessdb grep` follow the same source precedence as normal read commands:

```text
local live tool homes + remote synced pool subtrees
```

This keeps raw grep as a forensic escape hatch without making it a confusing
second model of what codebrain can see.

## Current Problem

Read commands refresh and query:

```text
~/.claude ~/.codex ~/.pi
+ ~/codebrain-pool/raw/<remote-machine>/<source>
```

But `sessdb grep <pattern>` currently searches only default local live homes unless
the user passes explicit paths. After Syncthing, this is surprising: `search`,
`recent`, `userlog`, `refs`, and `touched` can see synced remote history, while
`grep` cannot.

Including all of `~/codebrain-pool/raw` by default is also wrong, because this
would duplicate this machine's sessions from both live homes and the local pool
mirror.

## Source-Of-Truth Rule

Keep the existing hierarchy:

- Local live homes are authoritative for this machine.
- Pool subtrees are authoritative for remote machines and historical/backfilled
  origins.
- SQLite is a rebuildable cache.

So default grep roots should be:

```text
local live roots:
  ~/.claude
  ~/.codex
  ~/.pi

remote pool roots:
  ~/codebrain-pool/raw/<remote-machine>/<source>

not local pool roots:
  ~/codebrain-pool/raw/<this-machine>/<source>
```

Explicit user-provided grep paths should continue to override default root
selection.

## Proposed Behavior

```bash
sessdb grep "needle"
```

Searches:

1. Existing local live source roots.
2. Existing remote synced pool source roots discovered via `discover_pool_roots`.
3. Excludes configured local machine aliases using `local_machine_names()`.
4. Keeps existing `file-history` exclusions.

```bash
sessdb grep "needle" ~/codebrain-pool/raw
```

Searches exactly the user-provided path scope, still applying safety exclusions
such as `file-history`.

## Implementation Sketch

Add a helper in `cli.py`:

```python
def _default_grep_roots():
    roots = [DEFAULT_CLAUDE_ROOT, DEFAULT_CODEX_ROOT, DEFAULT_PI_ROOT]
    roots += [
        root for _, _, root in discover_pool_roots(
            DEFAULT_POOL,
            include_local=False,
            local_machines=local_machine_names(),
        )
    ]
    return [str(p) for p in roots if Path(p).exists()]
```

Then update `cmd_grep` to use `_default_grep_roots()` when no explicit paths are
provided.

Reuse the existing `_grep_command()` exclusion behavior:

- `rg --glob '!**/file-history/**'`
- `grep --exclude-dir=file-history`
- drop explicit paths containing a `file-history` path component

## Tests

Add focused CLI helper tests:

1. Default grep roots include local live roots that exist.
2. Default grep roots include remote pool roots.
3. Default grep roots skip local pool roots.
4. `CODEBRAIN_MACHINE` / `CODEBRAIN_LOCAL_MACHINES` aliases are respected.
5. Explicit paths still override default roots.
6. `file-history` remains excluded for both default and explicit scopes.

Prefer unit tests around root construction and command construction; avoid
shelling out to `rg` in tests.

## Documentation

Update README/CHEATSHEET/SYNCING wording:

```text
grep = raw forensic search over local live homes + remote synced pool roots
```

Also mention that explicit paths override the default search universe.

## Non-Goals

- Do not make grep query SQLite.
- Do not add text classifiers or infer file/session semantics from free text.
- Do not sync or grep `file-history` bodies.
- Do not make local pool roots authoritative over local live homes.


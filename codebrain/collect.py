"""Collector: mirror this machine's raw agent logs into the append-only pool.

The pool (DESIGN.md) is the durable, syncable copy of the raw logs:
`<pool>/raw/<machine>/<source>/<original relpath>`. Each machine writes only its
own subtree, so replicating the pool across machines (Syncthing, later) can
never conflict. Relpaths mirror the tool home, so ingest can point at
`<pool>/raw/<machine>/<source>` exactly as it points at a live home.

A sweep is a one-way valve with archive guarantees:

- **Allowlists, never whole homes** — tool homes hold credentials (auth.json),
  settings, and the tools' own databases; none of that belongs in a synced pool.
- **Stat-compare incrementality** — copy2 preserves mtime, so "changed?" is a
  pure (size, mtime) compare against the pool copy itself. No bookkeeping
  table, nothing to drift.
- **Never deletes, never shrinks** — these logs are append-only, so a source
  file smaller than its pool copy is a regression signal (truncation, botched
  restore): the pool copy wins and the sweep warns.
- **tmp + atomic rename** — a crash mid-copy cannot leave a torn pool file.

This is durability only. Freshness is refresh-on-read (ingest.refresh), which
reads the live homes directly — a periodic launchd sweep is plenty here.
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
from pathlib import Path
from typing import Optional

from codebrain.ingest import (
    DEFAULT_CLAUDE_ROOT, DEFAULT_CODEX_ROOT, DEFAULT_PI_ROOT, SOURCES,
)

DEFAULT_POOL = Path.home() / "codebrain-pool"
LAUNCHD_LABEL = "com.codebrain.collect"

DEFAULT_ROOTS = {
    "claude": DEFAULT_CLAUDE_ROOT,
    "codex": DEFAULT_CODEX_ROOT,
    "pi": DEFAULT_PI_ROOT,
}

# What may leave a tool home, per source (globs relative to the home).
# Everything else — auth.json, config/settings, the tools' own sqlite, and
# anything snapshotting the environment (claude session-env/, shell-snapshots/:
# env vars hold tokens) — stays out. Session-data DIRECTORIES are taken whole:
# review of acc6608 found per-extension lists silently dropping transcript-
# referenced sidecars (tool-results/*.txt, subagents/*.meta.json), and a missed
# sidecar is permanent data loss while an extra file is just bytes.
PATTERNS = {
    "claude": (
        "projects/**/*",                  # transcripts, subagent files + meta,
                                          #   tool-results, indexes, memory
        "tasks/**/*",                     # per-session task state
        "todos/**/*",                     # per-session todo state (older versions)
        "teams/**/*",                     # agent-team inboxes
        "file-history/**/*",              # pre-edit file snapshots, session-keyed
        "history.jsonl",                  # prompt history
    ),
    "codex": (
        "sessions/**/*.jsonl",            # rollouts (Y/M/D tree)
        "archived_sessions/**/*.jsonl",
        "session_index.jsonl",            # titles (ingest's enrich reads this)
        "history.jsonl",
    ),
    "pi": (
        "agent/sessions/**/*",            # transcripts, run dirs, subagent artifacts
        "agent/run-history.jsonl",
    ),
}


def discover(source: str, raw_root: Path):
    """Allowlisted files under one tool home — regular files only, no symlinks
    (a link could smuggle content from outside the home into the pool)."""
    root = Path(raw_root)
    seen = set()
    for pattern in PATTERNS[source]:
        for f in root.glob(pattern):
            if f not in seen and not f.is_symlink() and f.is_file():
                seen.add(f)
    return sorted(seen)


def _prune_stale_parts(dest_root: Path, max_age_s: int = 3600) -> None:
    """Unlink tmp files orphaned by a crash/SIGTERM mid-copy. Age-gated so a
    concurrently running sweep's live tmp is never touched. (.part files are
    scratch, not archive — the never-delete rule doesn't apply to them.)"""
    import time
    cutoff = time.time() - max_age_s
    for p in dest_root.rglob("*.part"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def collect_source(source: str, raw_root: Optional[Path] = None,
                   pool_root: Path = DEFAULT_POOL, machine: Optional[str] = None) -> dict:
    machine = machine or socket.gethostname()
    root = Path(raw_root or DEFAULT_ROOTS[source])
    dest_root = Path(pool_root) / "raw" / machine / source
    stats = {"files": 0, "new": 0, "updated": 0, "unchanged": 0, "shrunk": 0, "errors": 0}
    if dest_root.is_dir():
        _prune_stale_parts(dest_root)
    for f in discover(source, root):
        stats["files"] += 1
        try:
            sst = f.stat()
        except OSError:
            continue  # vanished between discovery and now; next sweep
        dst = dest_root / f.relative_to(root)
        try:
            dst_st = dst.stat()
        except OSError:
            dst_st = None
        if dst_st is not None:
            # Exact float equality is correct where copy2 round-trips mtime (APFS,
            # ns precision). On a coarse-mtime pool filesystem it degrades to
            # recopying — wasteful but never wrong, and loud in the stats.
            if (sst.st_size, sst.st_mtime) == (dst_st.st_size, dst_st.st_mtime):
                stats["unchanged"] += 1
                continue
            if sst.st_size < dst_st.st_size:
                # Append-only logs don't shrink. Keep the bigger archive copy;
                # if the source later grows past it, the next sweep copies again.
                print(f"  ~ shrink-guard {source}: {f} is {sst.st_size}B < pool's "
                      f"{dst_st.st_size}B — keeping pool copy")
                stats["shrunk"] += 1
                continue
        # Dot-prefixed + per-process tmp: hidden from globs, and overlapping
        # sweeps (manual + launchd) can never write through the same tmp path.
        tmp = dst.with_name(f".{dst.name}.{os.getpid()}.part")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            # copy2 keeps mtime (→ next sweep's stat compare); follow_symlinks=False
            # closes the discover-then-copy race where a file becomes a symlink.
            shutil.copy2(f, tmp, follow_symlinks=False)
            os.replace(tmp, dst)
        except OSError as exc:
            print(f"  ! copy error {f}: {exc}")
            stats["errors"] += 1
            tmp.unlink(missing_ok=True)
            continue
        stats["new" if dst_st is None else "updated"] += 1
    return stats


def collect_all(sources=SOURCES, machine: Optional[str] = None,
                pool_root: Path = DEFAULT_POOL, roots: Optional[dict] = None) -> dict:
    total = {"files": 0, "new": 0, "updated": 0, "unchanged": 0, "shrunk": 0, "errors": 0}
    for src in sources:
        stats = collect_source(src, (roots or {}).get(src), pool_root, machine)
        print(f"  {src}: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
        for k, v in stats.items():
            total[k] += v
    return total


def _plist_dict(interval: int = 1800, pool_root: Path = DEFAULT_POOL,
                source: str = "all", machine: Optional[str] = None) -> dict:
    """The LaunchAgent definition, as data (plistlib serializes it — escaping of
    odd path characters comes free). Runs THIS interpreter (`sys.executable -m
    codebrain collect`) so the env that can import codebrain is the env launchd
    runs; WorkingDirectory covers the run-from-checkout (no pip install) case.
    RunAtLoad catches sweeps missed while the machine was off."""
    log = Path.home() / ".codebrain" / "logs" / "collect.log"
    argv = [sys.executable, "-m", "codebrain", "collect", "--pool", str(pool_root)]
    if source != "all":
        argv += ["--source", source]
    if machine:
        argv += ["--machine", machine]
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": argv,
        "WorkingDirectory": str(Path(__file__).resolve().parent.parent),
        "StartInterval": interval,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


def install_launchd(interval: int = 1800, pool_root: Path = DEFAULT_POOL,
                    source: str = "all", machine: Optional[str] = None) -> Path:
    """Write + (re)load a LaunchAgent that sweeps every `interval` seconds."""
    import plistlib
    import subprocess

    spec = _plist_dict(interval, pool_root, source, machine)
    Path(spec["StandardOutPath"]).parent.mkdir(parents=True, exist_ok=True)
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(plistlib.dumps(spec))
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"],
                   capture_output=True)  # unload a previous version, if any
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip()}")
    return plist_path

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
# Everything else — auth.json, config/settings, the tools' own sqlite — stays out.
PATTERNS = {
    "claude": (
        "projects/**/*.jsonl",            # transcripts + per-session subagent files
        "projects/*/sessions-index.json", # per-project title/summary index
        "projects/*/memory/*.md",         # agent-authored project memory
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


def collect_source(source: str, raw_root: Optional[Path] = None,
                   pool_root: Path = DEFAULT_POOL, machine: Optional[str] = None) -> dict:
    machine = machine or socket.gethostname()
    root = Path(raw_root or DEFAULT_ROOTS[source])
    dest_root = Path(pool_root) / "raw" / machine / source
    stats = {"files": 0, "new": 0, "updated": 0, "unchanged": 0, "shrunk": 0, "errors": 0}
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
        tmp = dst.with_name(dst.name + ".part")
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, tmp)  # preserves mtime → next sweep's stat compare works
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


def install_launchd(interval: int = 1800, pool_root: Path = DEFAULT_POOL) -> Path:
    """Write + (re)load a LaunchAgent that sweeps every `interval` seconds.

    Runs THIS interpreter (`sys.executable -m codebrain collect`) so the env that
    can import codebrain is the env launchd runs; WorkingDirectory covers the
    run-from-checkout (no pip install) case. RunAtLoad catches sweeps missed
    while the machine was off."""
    import subprocess

    log = Path.home() / ".codebrain" / "logs" / "collect.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    workdir = Path(__file__).resolve().parent.parent
    argv = [sys.executable, "-m", "codebrain", "collect", "--pool", str(pool_root)]
    items = "\n".join(f"    <string>{a}</string>" for a in argv)
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{items}
  </array>
  <key>WorkingDirectory</key>
  <string>{workdir}</string>
  <key>StartInterval</key>
  <integer>{interval}</integer>
  <key>RunAtLoad</key>
  <true/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>StandardOutPath</key>
  <string>{log}</string>
  <key>StandardErrorPath</key>
  <string>{log}</string>
</dict>
</plist>
"""
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(plist, encoding="utf-8")
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"],
                   capture_output=True)  # unload a previous version, if any
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip()}")
    return plist_path

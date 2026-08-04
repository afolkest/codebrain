"""Collector: mirror this machine's source evidence into the append-only pool.

The pool (DESIGN.md) is the durable, syncable evidence boundary at
`<pool>/raw/<machine>/<source>/...`. Claude/Codex/pi preserve allowlisted live-log
relpaths. Cursor preserves only codebrain's immutable safe-revision layout.
Each machine writes its own subtree; ordinary conflicts are avoided and an
immutable Cursor path conflict is retained and reported.

A sweep is a one-way valve with archive guarantees:

- **Allowlists, never whole homes** — tool homes hold credentials (auth.json),
  settings, and the tools' own databases; none of that belongs in a synced pool.
- **Source-specific durability** — ordinary logs use stat comparison, shrink
  guards, and temp-plus-rename; Cursor revisions use validated canonical bytes,
  descriptor-relative no-follow traversal, and create-only links.
- **Never deletes evidence** — cleanup removes only age-gated collector-owned
  temp names; no source revision or pool evidence is pruned.

This is durability only. Freshness is refresh-on-read (ingest.refresh), which
reads live homes and refreshes the local safe Cursor archive directly — a
periodic launchd sweep is plenty here.
"""
from __future__ import annotations

import os
import shutil
import socket
import stat
import sys
import time
from pathlib import Path
from typing import Optional

from codebrain import cursor_archive
from codebrain.ingest import (
    DEFAULT_CLAUDE_ROOT, DEFAULT_CODEX_ROOT, DEFAULT_CURSOR_ROOT, DEFAULT_PI_ROOT,
    SOURCES, export_local_cursor_archive,
)

DEFAULT_POOL = Path.home() / "codebrain-pool"
LAUNCHD_LABEL = "com.codebrain.collect"

DEFAULT_ROOTS = {
    "claude": DEFAULT_CLAUDE_ROOT,
    "codex": DEFAULT_CODEX_ROOT,
    "pi": DEFAULT_PI_ROOT,
    "cursor": DEFAULT_CURSOR_ROOT,
}


def _valid_machine_name(name: str) -> bool:
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name


def _machine_name(machine: Optional[str]) -> str:
    name = machine or os.environ.get("CODEBRAIN_MACHINE") or socket.gethostname()
    if not _valid_machine_name(name):
        raise ValueError(f"invalid machine {name!r}: must be a single path component")
    return name

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
    """Allowlisted source evidence — regular files only, no source symlinks."""
    root = Path(raw_root)
    if source == "cursor":
        return cursor_archive.discover_revisions(root)
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
    cutoff = time.time() - max_age_s
    for p in dest_root.rglob("*.part"):
        try:
            file_stat = os.lstat(p)
            if _is_collector_owned_part(p.name) \
                    and stat.S_ISREG(file_stat.st_mode) \
                    and file_stat.st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def collect_source(source: str, raw_root: Optional[Path] = None,
                   pool_root: Path = DEFAULT_POOL, machine: Optional[str] = None) -> dict:
    machine = _machine_name(machine)
    export_errors = 0
    if source == "cursor" and raw_root is None:
        root, export_stats = export_local_cursor_archive()
        export_errors = export_stats["errors"]
    else:
        root = Path(raw_root or DEFAULT_ROOTS[source])
    dest_root = Path(pool_root) / "raw" / machine / source
    stats = {"files": 0, "new": 0, "updated": 0, "unchanged": 0, "shrunk": 0,
             "errors": export_errors}
    if source == "cursor":
        return _collect_cursor_revisions(root, Path(pool_root), machine, stats)
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


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) \
    | getattr(os, "O_NOFOLLOW", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _collect_cursor_revisions(root: Path, pool_root: Path, machine: str,
                              stats: dict) -> dict:
    """Create-only Cursor replication anchored beneath a no-follow pool fd."""
    destination_fd = None
    try:
        try:
            destination_fd = _open_cursor_destination(
                pool_root, machine, create=False,
            )
            if destination_fd is not None:
                _prune_cursor_parts(destination_fd)
        except OSError as exc:
            print(f"  ! unsafe Cursor pool destination: {exc}")
            stats["errors"] += 1
            return stats

        for source_path in discover("cursor", root):
            stats["files"] += 1
            try:
                revision_bytes = cursor_archive.read_revision_bytes(source_path)
                relative = source_path.relative_to(root)
                relative_parts = _cursor_revision_parts(relative)
            except (OSError, ValueError, cursor_archive.CursorArchiveError) as exc:
                print(f"  ! invalid Cursor revision {source_path}: {exc}")
                stats["errors"] += 1
                continue

            try:
                if destination_fd is None:
                    destination_fd = _open_cursor_destination(
                        pool_root, machine, create=True,
                    )
                    if destination_fd is None:  # pragma: no cover - create=True
                        raise OSError("could not create Cursor pool destination")
                parent_fd = _open_cursor_revision_parent(
                    destination_fd, relative_parts[:-1],
                )
            except OSError as exc:
                print(f"  ! unsafe Cursor pool destination: {exc}")
                stats["errors"] += 1
                continue

            destination_name = relative_parts[-1]
            try:
                try:
                    os.stat(destination_name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    _record_cursor_existing(
                        parent_fd, destination_name, revision_bytes, stats,
                    )
                    continue
                _publish_cursor_revision(
                    parent_fd, destination_name, revision_bytes, stats,
                )
            except OSError as exc:
                print(f"  ! Cursor pool write error {source_path}: {exc}")
                stats["errors"] += 1
            finally:
                os.close(parent_fd)
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
    return stats


def _open_cursor_destination(pool_root: Path, machine: str,
                             create: bool) -> Optional[int]:
    """Open ``pool/raw/<machine>/cursor`` without following owned components."""
    pool_fd = _open_pool_root(Path(pool_root), create=create)
    if pool_fd is None:
        return None
    current_fd = pool_fd
    try:
        for name in ("raw", machine, "cursor"):
            child_fd = _open_child_directory(current_fd, name, create=create)
            if child_fd is None:
                return None
            os.close(current_fd)
            current_fd = child_fd
        result = current_fd
        current_fd = -1
        return result
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def _open_pool_root(pool_root: Path, create: bool) -> Optional[int]:
    if pool_root.name in ("", ".", ".."):
        raise OSError("Cursor pool root must name a directory")
    parent_fd = _open_directory(pool_root.parent, create=create)
    if parent_fd is None:
        return None
    try:
        return _open_child_directory(parent_fd, pool_root.name, create=create)
    finally:
        os.close(parent_fd)


def _open_directory(path: Path, create: bool) -> Optional[int]:
    """Open one caller-owned anchor, creating a missing suffix durably."""
    try:
        return os.open(path, _DIRECTORY_FLAGS)
    except FileNotFoundError:
        if not create:
            return None
    if path.name in ("", ".", "..") or path.parent == path:
        raise OSError(f"cannot create Cursor pool directory {path}")
    parent_fd = _open_directory(path.parent, create=True)
    if parent_fd is None:  # pragma: no cover - create=True
        raise OSError(f"cannot create Cursor pool parent {path.parent}")
    try:
        return _open_child_directory(parent_fd, path.name, create=True)
    finally:
        os.close(parent_fd)


def _open_child_directory(parent_fd: int, name: str,
                          create: bool) -> Optional[int]:
    if name in ("", ".", "..") or "/" in name or "\\" in name:
        raise OSError(f"unsafe Cursor pool path component {name!r}")
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        if not create:
            return None
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileExistsError:
        pass
    return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)


def _cursor_revision_parts(relative: Path) -> tuple[str, str, str, str]:
    parts = relative.parts
    if len(parts) != 4 or parts[0] != "sessions" or parts[2] != "revisions" \
            or len(parts[1]) != 64 \
            or any(c not in "0123456789abcdef" for c in parts[1]) \
            or not _is_cursor_revision_name(parts[3]):
        raise ValueError("invalid Cursor archive destination layout")
    return parts


def _is_cursor_revision_name(name: str) -> bool:
    try:
        revision, snapshot_hash = name.removesuffix(".json").split("-", 1)
    except ValueError:
        return False
    return name.endswith(".json") and len(revision) == 20 \
        and revision.isdigit() and len(snapshot_hash) == 64 \
        and all(c in "0123456789abcdef" for c in snapshot_hash)


def _open_cursor_revision_parent(destination_fd: int,
                                 parts: tuple[str, str, str]) -> int:
    current_fd = os.dup(destination_fd)
    try:
        for name in parts:
            child_fd = _open_child_directory(current_fd, name, create=True)
            if child_fd is None:  # pragma: no cover - create=True
                raise OSError(f"cannot create Cursor pool component {name!r}")
            os.close(current_fd)
            current_fd = child_fd
        result = current_fd
        current_fd = -1
        return result
    finally:
        if current_fd >= 0:
            os.close(current_fd)


def _record_cursor_existing(parent_fd: int, name: str, expected: bytes,
                            stats: dict) -> None:
    try:
        existing = _read_regular_file_at(parent_fd, name)
    except OSError as exc:
        print(f"  ! Cursor pool read error {name}: {exc}")
        stats["errors"] += 1
        return
    if existing == expected:
        stats["unchanged"] += 1
    else:
        print(f"  ! immutable Cursor revision conflict: keeping {name}")
        stats["errors"] += 1


def _publish_cursor_revision(parent_fd: int, name: str, data: bytes,
                             stats: dict) -> None:
    tmp_name = f".{name}.{os.getpid()}.part"
    fd = os.open(
        tmp_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _NOFOLLOW,
        stat.S_IRUSR | stat.S_IWUSR, dir_fd=parent_fd,
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(
                tmp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            os.unlink(tmp_name, dir_fd=parent_fd)
            _record_cursor_existing(parent_fd, name, data, stats)
            return
        os.unlink(tmp_name, dir_fd=parent_fd)
        os.fsync(parent_fd)
        stats["new"] += 1
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_name, dir_fd=parent_fd)
        except OSError:
            pass
        raise


def _read_regular_file_at(parent_fd: int, name: str) -> bytes:
    fd = os.open(name, os.O_RDONLY | _NOFOLLOW, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise OSError("not a private regular file")
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            return fh.read()
    finally:
        if fd >= 0:
            os.close(fd)


def _prune_cursor_parts(destination_fd: int, max_age_s: int = 3600) -> None:
    """Delete only stale temps whose names prove this collector owns them."""
    sessions_fd = _open_child_directory(destination_fd, "sessions", create=False)
    if sessions_fd is None:
        return
    try:
        with os.scandir(sessions_fd) as sessions:
            session_names = [entry.name for entry in sessions
                             if len(entry.name) == 64
                             and all(c in "0123456789abcdef" for c in entry.name)
                             and entry.is_dir(follow_symlinks=False)]
        for session_name in session_names:
            session_fd = _open_child_directory(
                sessions_fd, session_name, create=False,
            )
            if session_fd is None:
                continue
            try:
                revisions_fd = _open_child_directory(
                    session_fd, "revisions", create=False,
                )
                if revisions_fd is not None:
                    try:
                        _prune_cursor_revision_parts(revisions_fd, max_age_s)
                    finally:
                        os.close(revisions_fd)
            finally:
                os.close(session_fd)
    finally:
        os.close(sessions_fd)


def _prune_cursor_revision_parts(revisions_fd: int, max_age_s: int) -> None:
    cutoff = time.time() - max_age_s
    with os.scandir(revisions_fd) as entries:
        names = [entry.name for entry in entries if _is_cursor_owned_part(entry.name)]
    for name in names:
        try:
            file_stat = os.stat(name, dir_fd=revisions_fd, follow_symlinks=False)
            if stat.S_ISREG(file_stat.st_mode) and file_stat.st_mtime < cutoff:
                os.unlink(name, dir_fd=revisions_fd)
        except OSError:
            pass


def _is_cursor_owned_part(name: str) -> bool:
    destination = _collector_part_destination(name)
    return destination is not None and _is_cursor_revision_name(destination)


def _is_collector_owned_part(name: str) -> bool:
    return _collector_part_destination(name) is not None


def _collector_part_destination(name: str) -> Optional[str]:
    if not name.startswith(".") or not name.endswith(".part"):
        return None
    try:
        destination, process_id = name[1:-len(".part")].rsplit(".", 1)
    except ValueError:
        return None
    return destination if destination and process_id.isdigit() else None


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
    machine = machine or os.environ.get("CODEBRAIN_MACHINE")
    if machine:
        argv += ["--machine", _machine_name(machine)]
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

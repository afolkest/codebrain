"""Immutable archive for codebrain's safe Cursor transcript projection."""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import sqlite3
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from codebrain import cursor_export


ARCHIVE_VERSION = 1
FULL_RECONCILE_SECONDS = 24 * 60 * 60
PART_PRUNE_SECONDS = 60 * 60
ACTIVE_RETRY_SECONDS = 60
ACTIVE_RETRY_MAX_SECONDS = 60 * 60
LONG_RETRY_SECONDS = 24 * 60 * 60
EXPORTER_STATE_VERSION = 2
RETRY_CATEGORIES = frozenset({
    "active", "incomplete", "draft", "absent", "source-error",
})


class CursorArchiveError(RuntimeError):
    pass


@dataclass
class RevisionState:
    path: Path
    revision: int
    snapshot_digest: str
    snapshot: dict
    payloads: dict[str, dict]


@dataclass(frozen=True)
class CursorHead:
    """A validated archive head and its already-reconstructed projection."""

    path: Path
    composer_id: str
    revision: int
    snapshot_digest: str
    snapshot: dict


@dataclass(frozen=True)
class CursorSessionSignature:
    """Cheap cache key for one hashed Cursor archive session directory."""

    session_key: str
    revision_dir: Path
    signature: str


@dataclass(frozen=True)
class CursorArchiveScan:
    """Root membership key plus independently invalidatable session rows."""

    root_signature: str
    sessions: tuple[CursorSessionSignature, ...]


def canonical_bytes(value) -> bytes:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        )
        return (encoded + "\n").encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise CursorArchiveError("snapshot is not strict canonical JSON") from exc


def digest(value) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def session_directory(root: Path, composer_id: str) -> Path:
    safe = hashlib.sha256(composer_id.encode("utf-8")).hexdigest()
    return Path(root) / "sessions" / safe / "revisions"


def scan_archive_metadata(root: Path) -> CursorArchiveScan:
    """Return no-follow metadata signatures independently for each session.

    Revision JSON is never opened. The root signature covers session membership
    and entry types; each session signature covers only that session's revision
    directory and entries. Unsafe descendants participate in a signature but
    are never traversed. Unsafe root or ``sessions`` components are rejected.
    """
    root = Path(root)
    root_entries: list[list] = [["cursor-archive-membership", 1]]
    rows = []
    try:
        root_fd = os.open(root, _DIRECTORY_FLAGS)
    except FileNotFoundError:
        root_entries.append(["root", "missing"])
        return CursorArchiveScan(_signature(root_entries), ())
    except OSError as exc:
        raise CursorArchiveError("cannot safely inspect archive root") from exc
    try:
        if not stat.S_ISDIR(os.fstat(root_fd).st_mode):
            raise CursorArchiveError("archive root is not a directory")
        try:
            sessions_fd = os.open("sessions", _DIRECTORY_FLAGS, dir_fd=root_fd)
        except FileNotFoundError:
            root_entries.append(["sessions", "missing"])
            return CursorArchiveScan(_signature(root_entries), ())
        except OSError as exc:
            raise CursorArchiveError(
                "archive sessions component is not a safe directory"
            ) from exc
        try:
            root_entries.append([
                "sessions", *_membership_metadata_fields(os.fstat(sessions_fd)),
            ])
            for session_name, session_stat in _directory_metadata(sessions_fd):
                root_entries.append([
                    "session-entry", session_name,
                    *_membership_metadata_fields(session_stat),
                ])
                if not _is_session_hash(session_name) \
                        or not stat.S_ISDIR(session_stat.st_mode):
                    continue
                session_fd = _open_scan_child(sessions_fd, session_name)
                try:
                    session_entries: list[list] = [[
                        "session", session_name,
                        *_metadata_fields(os.fstat(session_fd)),
                    ]]
                    try:
                        revisions_stat = os.stat(
                            "revisions", dir_fd=session_fd, follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        session_entries.append([
                            "session-child", session_name, "revisions", "missing",
                        ])
                    except OSError as exc:
                        raise CursorArchiveError(
                            "cannot safely scan archive session"
                        ) from exc
                    else:
                        session_entries.append([
                            "session-child", session_name, "revisions",
                            *_metadata_fields(revisions_stat),
                        ])
                        if stat.S_ISDIR(revisions_stat.st_mode):
                            revisions_fd = _open_scan_child(session_fd, "revisions")
                            try:
                                session_entries.append([
                                    "revisions", session_name,
                                    *_metadata_fields(os.fstat(revisions_fd)),
                                ])
                                for name, file_stat in _directory_metadata(revisions_fd):
                                    session_entries.append([
                                        "revision-entry", session_name, name,
                                        *_metadata_fields(file_stat),
                                    ])
                            finally:
                                os.close(revisions_fd)
                    rows.append(CursorSessionSignature(
                        session_key=session_name,
                        revision_dir=Path(os.path.join(
                            root, "sessions", session_name, "revisions",
                        )),
                        signature=_signature(session_entries),
                    ))
                finally:
                    os.close(session_fd)
        finally:
            os.close(sessions_fd)
    finally:
        os.close(root_fd)
    return CursorArchiveScan(
        _signature(root_entries), tuple(sorted(rows, key=lambda row: row.session_key))
    )


def archive_metadata_signature(root: Path) -> str:
    """Return an aggregate signature; prefer ``scan_archive_metadata`` for caches."""
    scan = scan_archive_metadata(root)
    return _signature([
        scan.root_signature,
        [[row.session_key, row.signature] for row in scan.sessions],
    ])


def discover_heads(root: Path) -> list[Path]:
    """Newest reconstructible revision per session, independent of arrival order."""
    sessions = Path(root) / "sessions"
    if not _is_plain_directory(Path(root)) or not _is_plain_directory(sessions):
        return []
    heads = []
    for revision_dir in sorted(sessions.glob("*/revisions")):
        try:
            state = latest_complete_revision(revision_dir)
        except (OSError, CursorArchiveError):
            continue
        if state is not None:
            heads.append(state.path)
    return sorted(heads)


def discover_validated_heads(root: Path) -> list[CursorHead]:
    """Return each selected head with validated metadata and projection.

    Unlike ``discover_heads`` followed by ``read_latest_snapshot``, this parses
    and reconstructs each revision chain only once.
    """
    sessions = Path(root) / "sessions"
    if not _is_plain_directory(Path(root)) or not _is_plain_directory(sessions):
        return []
    heads = []
    for revision_dir in sorted(sessions.glob("*/revisions")):
        try:
            state = latest_complete_revision(revision_dir)
            if state is not None:
                heads.append(_public_head(state))
        except (OSError, CursorArchiveError):
            continue
    return sorted(heads, key=lambda head: head.path)


def discover_revisions(root: Path) -> list[Path]:
    """Every reconstructible immutable segment needed to replicate the archive."""
    sessions = Path(root) / "sessions"
    if not _is_plain_directory(Path(root)) or not _is_plain_directory(sessions):
        return []
    revisions = []
    for revision_dir in sorted(sessions.glob("*/revisions")):
        try:
            revisions.extend(state.path for state in _complete_revisions(revision_dir))
        except (OSError, CursorArchiveError):
            continue
    return sorted(set(revisions))


def read_revision_bytes(path: Path) -> bytes:
    """Read and validate one immutable segment without following its final link."""
    return canonical_bytes(_read_segment(Path(path)))


def read_latest_snapshot(head_path: Path) -> dict:
    """Reconstruct the complete logical projection selected by ``head_path``."""
    head_path = Path(head_path)
    states = _complete_revisions(head_path.parent)
    selected = next((s for s in states if s.path == head_path), None)
    if selected is None:
        raise CursorArchiveError(f"revision is not reconstructible: {head_path.name}")
    return _inflate_snapshot(selected.snapshot, selected.payloads)


def read_validated_head(head_path: Path) -> CursorHead:
    """Validate one selected head and return its rank metadata and projection."""
    head_path = Path(head_path)
    states = _complete_revisions(head_path.parent)
    selected = next((state for state in states if state.path == head_path), None)
    if selected is None:
        raise CursorArchiveError(f"revision is not reconstructible: {head_path.name}")
    return _public_head(selected)


def select_session_head(session: CursorSessionSignature) -> Optional[CursorHead]:
    """Validate and reconstruct the selected head for exactly one scan row."""
    revision_dir = Path(session.revision_dir)
    if not _is_session_hash(session.session_key) \
            or revision_dir.name != "revisions" \
            or revision_dir.parent.name != session.session_key:
        raise CursorArchiveError("session scan row does not match its archive path")
    state = latest_complete_revision(revision_dir)
    return _public_head(state) if state is not None else None


def latest_complete_revision(revision_dir: Path) -> Optional[RevisionState]:
    states = _complete_revisions(Path(revision_dir))
    return max(states, key=lambda s: (s.revision, s.snapshot_digest)) if states else None


def publish_snapshot(snapshot: dict, root: Path) -> Optional[Path]:
    """Publish one projection, or return None when its logical state is unchanged."""
    root = Path(root)
    with archive_lock(root) as root_fd:
        return _publish_snapshot_locked(snapshot, root, root_fd)


def export_cursor(db_path: Path = cursor_export.DEFAULT_CURSOR_DB,
                  root: Path = cursor_export.DEFAULT_CURSOR_ROOT,
                  full_reconcile: bool = False,
                  now: Optional[float] = None) -> dict:
    """Incrementally project changed Cursor sessions into immutable revisions."""
    root = Path(root)
    now = time.time() if now is None else now
    if isinstance(now, bool) or not isinstance(now, (int, float)) \
            or not math.isfinite(now):
        raise CursorArchiveError("export time must be finite")
    stats = {
        "candidates": 0, "published": 0, "unchanged": 0,
        "skipped": 0, "errors": 0,
    }
    with archive_lock(root) as root_fd:
        state = _read_exporter_state(root, now)
        last_full = state.get("lastFullReconcileAt")
        last_full_valid = not isinstance(last_full, bool) \
            and isinstance(last_full, (int, float)) and math.isfinite(last_full) \
            and last_full <= now
        if not last_full_valid:
            last_full = 0
        due_full = full_reconcile or not state or not last_full_valid or (
            now - last_full >= FULL_RECONCILE_SECONDS
        )
        last_part_prune = state.get("lastPartPruneAt")
        last_part_valid = not isinstance(last_part_prune, bool) \
            and isinstance(last_part_prune, (int, float)) \
            and math.isfinite(last_part_prune) and last_part_prune <= now
        due_part_prune = not last_part_valid \
            or now - last_part_prune >= PART_PRUNE_SECONDS
        if not last_part_valid:
            last_part_prune = 0
        try:
            conn = cursor_export.connect_cursor(db_path)
        except (OSError, sqlite3.Error, cursor_export.CursorSnapshotError):
            stats["errors"] += 1
            return stats
        try:
            if due_part_prune:
                try:
                    _prune_stale_parts(root)
                    last_part_prune = now
                except OSError:
                    stats["errors"] += 1
            with cursor_export.read_transaction(conn):
                tokens, invalid_header_ids = _header_tokens(conn)
                ids = set(tokens)
                ids.update(invalid_header_ids)
                if due_full:
                    ids.update(cursor_export.composer_ids(conn, include_data_only=True))
            raw_tokens = state.get("headerTokens")
            old_tokens = {
                sid: token for sid, token in raw_tokens.items()
                if isinstance(sid, str) and sid and isinstance(token, dict)
            } if isinstance(raw_tokens, dict) else {}
            retries = _retry_records(state, now)
            if due_full:
                retries = {sid: record for sid, record in retries.items() if sid in ids}
            ids.update(retries)
            candidates = sorted(
                sid for sid in ids
                if due_full or tokens.get(sid) != old_tokens.get(sid)
                or (sid in invalid_header_ids and sid not in retries)
                or (sid in retries and retries[sid]["nextAttemptAt"] <= now)
            )
            stats["candidates"] = len(candidates)
            new_tokens = {
                sid: token for sid, token in old_tokens.items()
                if not due_full or sid in tokens
            }
            new_retries = dict(retries)
            for sid in candidates:
                token_changed = sid in tokens \
                    and tokens.get(sid) != old_tokens.get(sid)
                previous_retry = None if token_changed else new_retries.get(sid)
                if sid in invalid_header_ids:
                    stats["errors"] += 1
                    # Forget the formerly valid token. Otherwise the invalid row
                    # looks changed forever and bypasses its own backoff; restoring
                    # the exact old valid token must also count as a fresh change.
                    new_tokens.pop(sid, None)
                    new_retries[sid] = _retry_record(
                        "source-error", now, previous_retry,
                    )
                    continue
                try:
                    with cursor_export.read_transaction(conn):
                        snapshot = cursor_export.project_session(conn, sid)
                    if snapshot is None:
                        stats["skipped"] += 1
                        new_retries[sid] = _retry_record(
                            "absent", now, previous_retry,
                        )
                        if sid in tokens:
                            new_tokens[sid] = tokens[sid]
                        continue
                    path = _publish_snapshot_locked(snapshot, root, root_fd)
                except cursor_export.CursorSessionUnsettled as exc:
                    stats["skipped"] += 1
                    new_retries[sid] = _retry_record(
                        exc.retry_category.value, now, previous_retry,
                    )
                    if sid in tokens:
                        new_tokens[sid] = tokens[sid]
                    continue
                except cursor_export.CursorSnapshotIncomplete:
                    stats["skipped"] += 1
                    new_retries[sid] = _retry_record(
                        "incomplete", now, previous_retry,
                    )
                    if sid in tokens:
                        new_tokens[sid] = tokens[sid]
                    continue
                except (OSError, sqlite3.Error, cursor_export.CursorSnapshotError,
                        CursorArchiveError):
                    stats["errors"] += 1
                    new_retries[sid] = _retry_record(
                        "source-error", now, previous_retry,
                    )
                    if sid in tokens:
                        new_tokens[sid] = tokens[sid]
                    continue
                if path is None:
                    stats["unchanged"] += 1
                else:
                    stats["published"] += 1
                if sid in tokens:
                    new_tokens[sid] = tokens[sid]
                new_retries.pop(sid, None)
        except (sqlite3.Error, CursorArchiveError):
            stats["errors"] += 1
            return stats
        finally:
            conn.close()

        new_state = {
            "version": EXPORTER_STATE_VERSION,
            "lastFullReconcileAt": now if due_full else last_full,
            "lastPartPruneAt": last_part_prune,
            "headerTokens": new_tokens,
            "retryRecords": new_retries,
        }
        if new_state != state:
            try:
                _write_exporter_state(root, new_state, root_fd)
            except (OSError, CursorArchiveError):
                stats["errors"] += 1
    return stats


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) \
    | getattr(os, "O_NOFOLLOW", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _metadata_fields(value: os.stat_result) -> list[int]:
    return [
        value.st_mode, value.st_dev, value.st_ino, value.st_nlink,
        value.st_size, value.st_mtime_ns, value.st_ctime_ns,
    ]


def _membership_metadata_fields(value: os.stat_result) -> list[int]:
    if stat.S_ISDIR(value.st_mode):
        return [value.st_mode, value.st_dev, value.st_ino]
    return _metadata_fields(value)


def _signature(value) -> str:
    # Metadata signatures are local rebuildable cache keys, not archive evidence.
    # Their inputs are only nested lists of strings/integers assembled above;
    # repr is deterministic for those types and materially cheaper than running
    # thousands of one-row structures through the strict JSON encoder.
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def _directory_metadata(directory_fd: int) -> list[tuple[str, os.stat_result]]:
    try:
        entries = sorted(os.scandir(directory_fd), key=lambda entry: entry.name)
        return [
            (entry.name, entry.stat(follow_symlinks=False)) for entry in entries
        ]
    except OSError as exc:
        raise CursorArchiveError("cannot safely scan archive directory") from exc


def _open_scan_child(parent_fd: int, name: str) -> int:
    try:
        fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise CursorArchiveError("archive changed during metadata scan") from exc
    if not stat.S_ISDIR(os.fstat(fd).st_mode):
        os.close(fd)
        raise CursorArchiveError("archive child is not a directory")
    return fd


def _is_session_hash(name: str) -> bool:
    if len(name) != 64 or not name.isascii() or name != name.lower():
        return False
    try:
        int(name, 16)
    except ValueError:
        return False
    return True


def _is_plain_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _open_private_root(root: Path) -> int:
    created = False
    try:
        root.mkdir(parents=True, mode=0o700)
        created = True
    except FileExistsError:
        pass
    if not _is_plain_directory(root):
        raise CursorArchiveError("archive root must be a real directory")
    try:
        fd = os.open(root, _DIRECTORY_FLAGS)
    except OSError as exc:
        raise CursorArchiveError("cannot safely open archive root") from exc
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise CursorArchiveError("archive root is not a directory")
        os.fchmod(fd, 0o700)
        if created:
            _fsync_directory(root.parent)
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_private_lock(root_fd: int) -> int:
    flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | _NOFOLLOW
    created = False
    try:
        fd = os.open(".export.lock", flags, 0o600, dir_fd=root_fd)
        created = True
    except FileExistsError:
        try:
            fd = os.open(".export.lock", os.O_RDWR | _NOFOLLOW, dir_fd=root_fd)
        except OSError as exc:
            raise CursorArchiveError("cannot safely open archive lock") from exc
    try:
        lock_stat = os.fstat(fd)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_nlink != 1:
            raise CursorArchiveError("archive lock must be a private regular file")
        os.fchmod(fd, 0o600)
        if created:
            os.fsync(root_fd)
        return fd
    except Exception:
        os.close(fd)
        raise


def _open_private_child(parent_fd: int, name: str) -> int:
    created = False
    try:
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    try:
        fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise CursorArchiveError(f"archive component {name!r} is not a safe directory") \
            from exc
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise CursorArchiveError(f"archive component {name!r} is not a directory")
        os.fchmod(fd, 0o700)
        if created:
            os.fsync(parent_fd)
        return fd
    except Exception:
        os.close(fd)
        raise


@contextmanager
def _open_revision_directory(root_fd: int, composer_id: str):
    safe = hashlib.sha256(composer_id.encode("utf-8")).hexdigest()
    opened = []
    try:
        parent_fd = root_fd
        for name in ("sessions", safe, "revisions"):
            parent_fd = _open_private_child(parent_fd, name)
            opened.append(parent_fd)
        yield opened[-1]
    finally:
        for fd in reversed(opened):
            os.close(fd)


@contextmanager
def archive_lock(root: Path):
    root = Path(root)
    root_fd = _open_private_root(root)
    fd = None
    locked = False
    try:
        fd = _open_private_lock(root_fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        locked = True
        yield root_fd
    finally:
        try:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            try:
                if fd is not None:
                    os.close(fd)
            finally:
                os.close(root_fd)


def _publish_snapshot_locked(snapshot: dict, root: Path, root_fd: int) -> Optional[Path]:
    composer_id = snapshot.get("composerId")
    if not isinstance(composer_id, str) or not composer_id:
        raise CursorArchiveError("snapshot has no composer id")
    logical, all_payloads = _deflate_snapshot(snapshot)
    snapshot_digest = digest(logical)
    revision_dir = session_directory(root, composer_id)
    with _open_revision_directory(root_fd, composer_id) as revision_fd:
        previous = latest_complete_revision(revision_dir)
        if previous is not None and previous.snapshot_digest == snapshot_digest:
            return None

        revision = previous.revision + 1 if previous else 1
        known = previous.payloads if previous else {}
        new_payloads = {
            payload_hash: payload for payload_hash, payload in all_payloads.items()
            if payload_hash not in known
        }
        segment = {
            "archiveVersion": ARCHIVE_VERSION,
            "composerId": composer_id,
            "revision": revision,
            "previousSnapshotDigest": previous.snapshot_digest if previous else None,
            "snapshotDigest": snapshot_digest,
            "snapshot": logical,
            "payloads": new_payloads,
        }
        name = f"{revision:020d}-{snapshot_digest}.json"
        destination = revision_dir / name
        _atomic_write(
            destination, canonical_bytes(segment), replace=False,
            directory_fd=revision_fd,
        )
        return destination


def _deflate_snapshot(snapshot: dict) -> tuple[dict, dict[str, dict]]:
    logical = {k: v for k, v in snapshot.items() if k != "order"}
    order = []
    payloads = {}
    source_order = snapshot.get("order")
    if not isinstance(source_order, list):
        raise CursorArchiveError("snapshot order is not a list")
    for item in source_order:
        if not isinstance(item, dict) or not isinstance(item.get("payload"), dict):
            raise CursorArchiveError("snapshot order item has no payload")
        payload = item["payload"]
        payload_hash = digest(payload)
        payloads[payload_hash] = payload
        entry = {k: v for k, v in item.items() if k != "payload"}
        entry["payloadHash"] = payload_hash
        order.append(entry)
    logical["order"] = order
    return logical, payloads


def _inflate_snapshot(logical: dict, payloads: dict[str, dict]) -> dict:
    if not isinstance(logical, dict) or not isinstance(logical.get("order"), list):
        raise CursorArchiveError("revision snapshot order is invalid")
    snapshot = {k: v for k, v in logical.items() if k != "order"}
    order = []
    for item in logical["order"]:
        if not isinstance(item, dict):
            raise CursorArchiveError("revision order item is invalid")
        payload_hash = item.get("payloadHash")
        if not isinstance(payload_hash, str):
            raise CursorArchiveError("revision payload hash is invalid")
        if payload_hash not in payloads:
            raise CursorArchiveError("revision is missing an ordered payload")
        entry = {k: v for k, v in item.items() if k != "payloadHash"}
        entry["payload"] = payloads[payload_hash]
        order.append(entry)
    snapshot["order"] = order
    return snapshot


def _public_head(state: RevisionState) -> CursorHead:
    snapshot = _inflate_snapshot(state.snapshot, state.payloads)
    composer_id = snapshot.get("composerId")
    if not isinstance(composer_id, str) or not composer_id:
        raise CursorArchiveError("revision snapshot has no composer id")
    return CursorHead(
        path=state.path,
        composer_id=composer_id,
        revision=state.revision,
        snapshot_digest=state.snapshot_digest,
        snapshot=snapshot,
    )


def _require_safe_revision_directory(revision_dir: Path) -> bool:
    revision_dir = Path(revision_dir)
    session_dir = revision_dir.parent
    sessions_dir = session_dir.parent
    root = sessions_dir.parent
    if revision_dir.name != "revisions" or sessions_dir.name != "sessions" \
            or len(session_dir.name) != 64 \
            or any(c not in "0123456789abcdef" for c in session_dir.name):
        raise CursorArchiveError("invalid archive directory layout")
    for path in (root, sessions_dir, session_dir, revision_dir):
        try:
            mode = os.lstat(path).st_mode
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise CursorArchiveError("cannot inspect archive directory") from exc
        if not stat.S_ISDIR(mode):
            raise CursorArchiveError("archive directory contains a symlink or non-directory")
    return True


def _read_private_text(path: Path) -> str:
    path = Path(path)
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise CursorArchiveError("cannot inspect archive file") from exc
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise CursorArchiveError("archive file must be a private regular file")
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError as exc:
        raise CursorArchiveError("cannot safely open archive file") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise CursorArchiveError("archive file must be a private regular file")
        with os.fdopen(fd, "r", encoding="utf-8", errors="strict") as fh:
            fd = -1
            return fh.read()
    finally:
        if fd >= 0:
            os.close(fd)


def _complete_revisions(revision_dir: Path) -> list[RevisionState]:
    revision_dir = Path(revision_dir)
    if not _require_safe_revision_directory(revision_dir):
        return []
    segments = []
    for path in sorted(revision_dir.glob("*.json")):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            segment = _read_segment(path)
        except (OSError, CursorArchiveError):
            continue
        segments.append((path, segment))

    complete: dict[tuple[str, int], RevisionState] = {}
    states = []
    pending = list(segments)
    progressed = True
    while pending and progressed:
        progressed = False
        remaining = []
        for path, segment in pending:
            previous_digest = segment["previousSnapshotDigest"]
            if previous_digest is None:
                if segment["revision"] != 1:
                    remaining.append((path, segment))
                    continue
                base_payloads = {}
            else:
                previous = complete.get((previous_digest, segment["revision"] - 1))
                if previous is None:
                    remaining.append((path, segment))
                    continue
                base_payloads = previous.payloads
            merged = dict(base_payloads)
            merged.update(segment["payloads"])
            try:
                _inflate_snapshot(segment["snapshot"], merged)
            except CursorArchiveError:
                remaining.append((path, segment))
                continue
            state = RevisionState(
                path=path, revision=segment["revision"],
                snapshot_digest=segment["snapshotDigest"],
                snapshot=segment["snapshot"], payloads=merged,
            )
            complete[(state.snapshot_digest, state.revision)] = state
            states.append(state)
            progressed = True
        pending = remaining
    return states


def _read_segment(path: Path) -> dict:
    try:
        segment = json.loads(_read_private_text(path))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise CursorArchiveError("invalid revision JSON") from exc
    if not isinstance(segment, dict) or isinstance(segment.get("archiveVersion"), bool) \
            or segment.get("archiveVersion") != ARCHIVE_VERSION:
        raise CursorArchiveError("unsupported revision archive version")
    if "previousSnapshotDigest" not in segment:
        raise CursorArchiveError("revision predecessor is missing")
    required = {
        "composerId": str, "revision": int, "snapshotDigest": str,
        "snapshot": dict, "payloads": dict,
    }
    for key, typ in required.items():
        if not isinstance(segment.get(key), typ) or (
                typ is int and isinstance(segment.get(key), bool)):
            raise CursorArchiveError(f"invalid revision field {key}")
    if not segment["composerId"] or segment["revision"] <= 0:
        raise CursorArchiveError("invalid revision identity")
    previous = segment.get("previousSnapshotDigest")
    if previous is not None and not isinstance(previous, str):
        raise CursorArchiveError("invalid previous revision digest")
    if digest(segment["snapshot"]) != segment["snapshotDigest"]:
        raise CursorArchiveError("snapshot digest mismatch")
    expected_name = f"{segment['revision']:020d}-{segment['snapshotDigest']}.json"
    if path.name != expected_name:
        raise CursorArchiveError("revision filename mismatch")
    if path.parent.parent.name != hashlib.sha256(
            segment["composerId"].encode("utf-8")).hexdigest():
        raise CursorArchiveError("revision session directory mismatch")
    if segment["snapshot"].get("composerId") != segment["composerId"]:
        raise CursorArchiveError("revision composer id mismatch")
    for payload_hash, payload in segment["payloads"].items():
        if not isinstance(payload_hash, str) or not isinstance(payload, dict) \
                or digest(payload) != payload_hash:
            raise CursorArchiveError("payload digest mismatch")
    return segment


def _header_tokens(conn) -> tuple[dict[str, dict], set[str]]:
    tokens = {}
    invalid_ids = set()
    for row in conn.execute(
        "SELECT composerId,createdAt,lastUpdatedAt,isArchived,isSubagent,"
        "recency,checkpointAt FROM composerHeaders"
    ):
        sid = row["composerId"]
        if not isinstance(sid, str) or not sid:
            continue
        token = {
            "createdAt": row["createdAt"], "lastUpdatedAt": row["lastUpdatedAt"],
            "isArchived": row["isArchived"], "isSubagent": row["isSubagent"],
            "recency": row["recency"], "checkpointAt": row["checkpointAt"],
        }
        try:
            canonical_bytes({sid: token})
        except CursorArchiveError:
            invalid_ids.add(sid)
            continue
        tokens[sid] = token
    return tokens, invalid_ids


def _retry_records(state: dict, now: float) -> dict[str, dict]:
    if state.get("version") == 1:
        return {
            sid: {
                "category": "active", "attempts": 0, "nextAttemptAt": now,
            }
            for sid in state.get("pendingComposerIds", [])
        }
    return {
        sid: dict(record) for sid, record in state.get("retryRecords", {}).items()
    }


def _retry_record(category: str, now: float,
                  previous: Optional[dict] = None) -> dict:
    if category not in RETRY_CATEGORIES:
        raise CursorArchiveError("unknown Cursor retry category")
    attempts = 1
    if previous is not None and previous.get("category") == category:
        attempts = previous["attempts"] + 1
    if category in {"active", "incomplete"}:
        delay = min(
            ACTIVE_RETRY_SECONDS * (2 ** min(attempts - 1, 30)),
            ACTIVE_RETRY_MAX_SECONDS,
        )
    else:
        delay = LONG_RETRY_SECONDS
    return {
        "category": category,
        "attempts": attempts,
        "nextAttemptAt": now + delay,
    }


def _prune_stale_parts(root: Path, max_age_seconds: int = 3600) -> None:
    cutoff = time.time() - max_age_seconds
    root = Path(root)
    candidates = list(root.glob(".exporter-state.json.*.part"))
    sessions = root / "sessions"
    if _is_plain_directory(sessions):
        for session_dir in sessions.iterdir():
            revision_dir = session_dir / "revisions"
            if len(session_dir.name) == 64 \
                    and all(c in "0123456789abcdef" for c in session_dir.name) \
                    and _is_plain_directory(session_dir) \
                    and _is_plain_directory(revision_dir):
                candidates.extend(revision_dir.glob(".*.part"))
    for path in candidates:
        try:
            if _is_owned_part(path) and stat.S_ISREG(os.lstat(path).st_mode) \
                    and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def _is_owned_part(path: Path) -> bool:
    name = path.name
    if name.startswith(".exporter-state.json.") and name.endswith(".part"):
        return name[len(".exporter-state.json."):-len(".part")].isdigit()
    if not name.startswith(".") or not name.endswith(".part"):
        return False
    core = name[1:-len(".part")]
    try:
        destination, process_id = core.rsplit(".", 1)
        revision, snapshot_hash = destination.removesuffix(".json").split("-", 1)
    except ValueError:
        return False
    return destination.endswith(".json") and len(revision) == 20 \
        and revision.isdigit() and len(snapshot_hash) == 64 \
        and all(c in "0123456789abcdef" for c in snapshot_hash) \
        and process_id.isdigit()


def _read_exporter_state(root: Path, now: float) -> dict:
    path = Path(root) / "exporter-state.json"
    try:
        state_stat = os.lstat(path)
    except FileNotFoundError:
        return {}
    except OSError:
        return {}
    if not stat.S_ISREG(state_stat.st_mode) or state_stat.st_nlink != 1:
        return {}
    try:
        value = json.loads(_read_private_text(path))
        canonical_bytes(value)
    except (OSError, ValueError, UnicodeDecodeError, RecursionError,
            CursorArchiveError):
        return {}
    if not isinstance(value, dict) or isinstance(value.get("version"), bool):
        return {}
    version = value.get("version")
    if version == 1:
        pending = value.get("pendingComposerIds", [])
        if not isinstance(pending, list) or any(
                not isinstance(sid, str) or not sid for sid in pending):
            return {}
        return value
    if version != EXPORTER_STATE_VERSION:
        return {}
    last_part = value.get("lastPartPruneAt", 0)
    if isinstance(last_part, bool) or not isinstance(last_part, (int, float)) \
            or not math.isfinite(last_part) or last_part > now:
        return {}
    retries = value.get("retryRecords", {})
    if not isinstance(retries, dict):
        return {}
    for sid, record in retries.items():
        if not isinstance(sid, str) or not sid or not isinstance(record, dict) \
                or set(record) != {"category", "attempts", "nextAttemptAt"} \
                or record.get("category") not in RETRY_CATEGORIES:
            return {}
        attempts = record.get("attempts")
        retry_at = record.get("nextAttemptAt")
        max_delay = ACTIVE_RETRY_MAX_SECONDS \
            if record.get("category") in {"active", "incomplete"} \
            else LONG_RETRY_SECONDS
        if isinstance(attempts, bool) or not isinstance(attempts, int) \
                or attempts <= 0 or isinstance(retry_at, bool) \
                or not isinstance(retry_at, (int, float)) \
                or not math.isfinite(retry_at) \
                or retry_at > now + max_delay:
            return {}
    return value


def _write_exporter_state(root: Path, state: dict, root_fd: int) -> None:
    _atomic_write(
        Path(root) / "exporter-state.json", canonical_bytes(state),
        directory_fd=root_fd,
    )


def _atomic_write(path: Path, data: bytes, replace: bool = True,
                  directory_fd: Optional[int] = None) -> None:
    if directory_fd is None:
        raise CursorArchiveError("atomic archive writes require a safe directory")
    tmp_name = f".{path.name}.{os.getpid()}.part"
    fd = os.open(
        tmp_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _NOFOLLOW,
        0o600, dir_fd=directory_fd,
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if replace:
            os.replace(
                tmp_name, path.name,
                src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
            )
        else:
            os.link(
                tmp_name, path.name,
                src_dir_fd=directory_fd, dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            os.unlink(tmp_name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp_name, dir_fd=directory_fd)
        except OSError:
            pass
        raise


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, _DIRECTORY_FLAGS)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)

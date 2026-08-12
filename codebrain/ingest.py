"""Ingest raw evidence into the local SQLite cache (idempotent, multi-source).

Claude/Codex/pi read allowlisted logs from a live home or matching pool subtree.
Cursor is the live-boundary exception: its application database is projected
read-only into codebrain's private safe archive first, and local, custom, and
pool roots all ingest only immutable projection heads. All upserts key on
copy-invariant ids, so re-running is a no-op and every source shares one deduped
`events` table.
"""
from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import stat
import sys
from pathlib import Path
from typing import Callable, Optional

from codebrain import cursor_archive, cursor_export
from codebrain.adapters import claude, codex, cursor, pi
from codebrain.db import (
    cursor_head_is_newer,
    record_cursor_head,
    upsert_cursor_event,
    upsert_event,
    upsert_placement,
    upsert_session,
)

DEFAULT_CLAUDE_ROOT = Path.home() / ".claude"
DEFAULT_CODEX_ROOT = Path.home() / ".codex"
DEFAULT_PI_ROOT = Path.home() / ".pi"
DEFAULT_CURSOR_DB = cursor_export.DEFAULT_CURSOR_DB
DEFAULT_CURSOR_ROOT = cursor_export.DEFAULT_CURSOR_ROOT

SOURCES = ("claude", "codex", "pi", "cursor")
STATS_KEYS = ("files", "sessions", "events", "placements", "skipped", "conflicts", "errors")
CURSOR_ARCHIVE_VALIDATOR_VERSION = 1


class _CursorHeadSuperseded(RuntimeError):
    """Another writer accepted an equal or greater Cursor head first."""


def _empty_stats(**extra) -> dict:
    out = {k: 0 for k in STATS_KEYS}
    out.update(extra)
    return out


def _add_stats(total: dict, stats: dict) -> None:
    for k in STATS_KEYS:
        total[k] += stats.get(k, 0)


# ---- discovery (top-level transcripts only; sub-agent files live deeper) ----

def discover_claude_files(raw_root: Path):
    projects = Path(raw_root) / "projects"
    return sorted(projects.glob("*/*.jsonl")) if projects.is_dir() else []


def discover_codex_files(raw_root: Path):
    root = Path(raw_root)
    files = sorted((root / "sessions").glob("*/*/*/rollout-*.jsonl"))
    files += sorted((root / "archived_sessions").glob("*.jsonl"))
    return files


def discover_pi_files(raw_root: Path):
    base = Path(raw_root) / "agent" / "sessions"
    return sorted(base.glob("*/*.jsonl")) if base.is_dir() else []


def discover_cursor_files(raw_root: Path):
    return cursor_archive.discover_heads(Path(raw_root))


def export_local_cursor_archive(authoritative: bool = True) -> tuple[Path, dict]:
    """Refresh the codebrain-owned safe archive when Cursor exists locally.

    ``authoritative=False`` is the read path: it never waits for a concurrent
    exporter (the ``busy`` stat records the skip) and projects only header-token
    changes, deferring full reconciles and retry-due sessions to the collector
    sweep — so a read command is never the process that pays for archive
    maintenance."""
    root = Path(DEFAULT_CURSOR_ROOT)
    empty = {
        "candidates": 0, "published": 0, "unchanged": 0,
        "skipped": 0, "errors": 0, "busy": 0,
    }
    if not Path(DEFAULT_CURSOR_DB).is_file():
        return root, empty
    try:
        return root, cursor_archive.export_cursor(
            db_path=Path(DEFAULT_CURSOR_DB), root=root,
            authoritative=authoritative,
        )
    except Exception as exc:  # noqa: BLE001 — retain and ingest the last good archive
        print(f"  ! Cursor export error: {exc}")
        empty["errors"] = 1
        return root, empty


def _load_codex_titles(raw_root: Path) -> dict:
    """{sessionUuid: thread_name} from session_index.jsonl (Codex has no in-file title)."""
    f = Path(raw_root) / "session_index.jsonl"
    titles: dict = {}
    if not f.is_file():
        return titles
    with open(f, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            i, t = r.get("id"), r.get("thread_name")
            if isinstance(i, str) and isinstance(t, str) and t:
                titles[i] = t
    return titles


def _record_state(conn, path: Path, st, session_id: Optional[str]) -> None:
    """Remember the stat a file had when parsed, so refresh() can skip it until it
    changes. Committed with the file's data (same transaction)."""
    conn.execute(
        "INSERT OR REPLACE INTO ingest_state (path, mtime, size, session_id) VALUES (?,?,?,?)",
        (str(path), st.st_mtime, st.st_size, session_id),
    )


def _ingest(conn, files, parse_fn: Callable, enrich: Optional[Callable] = None,
            processed_paths: Optional[set[Path]] = None) -> dict:
    stats = {"files": 0, "sessions": 0, "events": 0, "placements": 0,
             "skipped": 0, "conflicts": 0, "errors": 0}
    for path in files:
        stats["files"] += 1
        try:
            # Stat BEFORE parsing: if the file grows mid-parse, the recorded state
            # is already stale and the next refresh re-parses it. Never the reverse.
            st = path.stat()
        except OSError:
            continue  # vanished between discovery and now; retried next run
        try:
            parsed = parse_fn(path)
        except Exception as exc:  # noqa: BLE001 — one bad file shouldn't sink the run
            print(f"  ! parse error {path.name}: {exc}")
            stats["errors"] += 1
            _record_state(conn, path, st, None)  # don't re-error every refresh; a
            conn.commit()                        # changed file is retried anyway
            if processed_paths is not None:
                processed_paths.add(path)
            continue
        if parsed is None:
            stats["skipped"] += 1  # contentless file (empty / no emittable events)
            _record_state(conn, path, st, None)
            conn.commit()
            if processed_paths is not None:
                processed_paths.add(path)
            continue
        conflicts = 0
        try:
            if enrich is not None:
                enrich(parsed)
            # Take the writer lock BEFORE any read the write decisions depend on
            # (cursor head gate, event-row compares, placement diff). Skipping
            # unchanged rows means the first statement below may otherwise be a
            # read, letting two concurrent refreshers both decide against the
            # same stale state. (The old always-write upserts serialized this
            # region by accident.) Parsing stays outside the lock.
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
            is_cursor = parsed.session.source == "cursor"
            if is_cursor:
                if parsed.source_head is None:
                    raise ValueError("Cursor archive parse has no validated source head")
                if not cursor_head_is_newer(
                        conn, parsed.session.session_id, parsed.source_head):
                    # The selected archive head can fall back when a newer segment
                    # is missing or corrupt, and pool roots can arrive out of order.
                    # Record that this file was handled, but never regress canonical
                    # data below the greatest accepted source rank.
                    _record_state(conn, path, st, parsed.session.session_id)
                    conn.commit()
                    stats["skipped"] += 1
                    if processed_paths is not None:
                        processed_paths.add(path)
                    continue
            upsert_session(conn, parsed.session)
            skipped: set = set()
            for e in parsed.events:
                accepted = upsert_cursor_event(conn, e) if is_cursor \
                    else upsert_event(conn, e)
                if not accepted:  # copy-consistency conflict (SCHEMA.md)
                    conflicts += 1
                    skipped.add(e.event_id)
                    print(
                        f"  ~ conflict {path.name}: kept authoritative content "
                        f"for {e.event_id}"
                    )
            # A re-parse is authoritative for this session's placements: the DB
            # must end up exactly matching the parse (no stale placement survives
            # a rewritten/shrunk file). But write only the difference — a grown
            # append-only log re-derives thousands of unchanged placements, and
            # rewriting them all was measurable write amplification on refresh.
            existing = {
                r["event_id"]: (r["seq"], r["parent_event_id"], r["live"],
                                r["inherited"])
                for r in conn.execute(
                    "SELECT event_id, seq, parent_event_id, live, inherited "
                    "FROM session_events WHERE session_id=?",
                    (parsed.session.session_id,))
            }
            keep = [p for p in parsed.placements if p.event_id not in skipped]
            # skipped events stay stale-deleted: the row we'd point at holds
            # another session's content
            stale = set(existing) - {p.event_id for p in keep}
            if stale:
                conn.executemany(
                    "DELETE FROM session_events WHERE session_id=? AND event_id=?",
                    [(parsed.session.session_id, eid) for eid in stale])
            for p in keep:
                if existing.get(p.event_id) == (p.seq, p.parent_event_id,
                                                p.live, p.inherited):
                    continue
                upsert_placement(conn, p)
            _record_state(conn, path, st, parsed.session.session_id)
            if is_cursor and not record_cursor_head(
                    conn, parsed.session.session_id, parsed.source_head):
                raise _CursorHeadSuperseded
            conn.commit()
        except _CursorHeadSuperseded:
            # Two refreshes can both pass the read gate before either obtains the
            # SQLite write lock. The loser must roll back all provisional writes,
            # but this is deliberate stale/equal suppression, not a source error.
            conn.rollback()
            stats["skipped"] += 1
            if processed_paths is not None:
                processed_paths.add(path)
            continue
        except Exception as exc:  # noqa: BLE001 — a genuine DB error isolates one file
            print(f"  ! write error {path.name}: {exc}")
            stats["errors"] += 1
            conn.rollback()
            continue
        stats["sessions"] += 1
        stats["events"] += len(parsed.events)
        stats["placements"] += len(parsed.placements)
        stats["conflicts"] += conflicts
        if processed_paths is not None:
            processed_paths.add(path)
    return stats


def _machine_for_root(source: str, raw_root: Optional[Path], machine: Optional[str]) -> str:
    """Which machine produced the sessions under this root. A pool subtree carries
    its origin in the path — raw/<machine>/<source> (SCHEMA.md) — which is the only
    record of it: ingesting macmini's synced subtree on the macbook must NOT stamp
    those sessions 'macbook'. A live tool home is always this machine. An explicit
    machine= wins over both."""
    if machine:
        return machine
    if raw_root is not None:
        root = Path(raw_root)
        if root.name == source and root.parent.parent.name == "raw":
            return root.parent.name
    return os.environ.get("CODEBRAIN_MACHINE") or socket.gethostname()


def _valid_pool_component(name: str) -> bool:
    return bool(name) and name not in (".", "..") and "/" not in name and "\\" not in name


def _require_pool_component(name: str, label: str) -> str:
    if not _valid_pool_component(name):
        raise ValueError(f"invalid {label} {name!r}: must be a single path component")
    return name


def local_machine_names(explicit: Optional[str] = None) -> set[str]:
    """Machine labels that should be treated as local pool subtrees.

    The default collector uses socket.gethostname(); CODEBRAIN_MACHINE is a simple
    alias hook for users who choose a stable explicit machine name, and
    CODEBRAIN_LOCAL_MACHINES can list old/alternate aliases after a rename.
    """
    names = {socket.gethostname()}
    if explicit:
        names.add(explicit)
    env_one = os.environ.get("CODEBRAIN_MACHINE")
    if env_one:
        names.add(env_one)
    for name in os.environ.get("CODEBRAIN_LOCAL_MACHINES", "").split(","):
        if name.strip():
            names.add(name.strip())
    return {n for n in names if _valid_pool_component(n)}


def discover_pool_roots(pool_root: Path, sources=SOURCES, machines=None,
                        include_local: bool = False,
                        local_machines: Optional[set[str]] = None):
    """Return [(machine, source, root)] for pool raw/<machine>/<source> roots.

    By default local machine subtrees are skipped because live-home refresh is
    fresher; remote pool subtrees are the cross-machine sync input.
    """
    srcs = (sources,) if isinstance(sources, str) else tuple(sources)
    for src in srcs:
        if src not in SOURCES:
            raise ValueError(f"unknown source {src!r}")
    raw = Path(pool_root) / "raw"
    if machines is None:
        machine_names = None
    else:
        ms = (machines,) if isinstance(machines, str) else tuple(machines)
        machine_names = sorted(_require_pool_component(m, "machine") for m in ms)
    if not raw.is_dir():
        return []
    if machine_names is None:
        machine_names = sorted(p.name for p in raw.iterdir()
                               if p.is_dir() and _valid_pool_component(p.name))
    local = local_machines if local_machines is not None else local_machine_names()
    roots = []
    for machine in machine_names:
        if not include_local and machine in local:
            continue
        for src in srcs:
            root = raw / machine / src
            if root.is_dir():
                roots.append((machine, src, root))
    return roots


def refresh_pool(conn: sqlite3.Connection, pool_root: Path, sources=SOURCES, machines=None,
                 include_local: bool = False,
                 local_machines: Optional[set[str]] = None) -> dict:
    """Delta-ingest synced pool roots. Origin machine is derived from raw/<machine>/<source>."""
    srcs = (sources,) if isinstance(sources, str) else tuple(sources)
    roots = discover_pool_roots(pool_root, sources=srcs, machines=machines,
                                include_local=include_local,
                                local_machines=local_machines)
    skipped_local = 0
    if not include_local:
        local = local_machines if local_machines is not None else local_machine_names()
        raw = Path(pool_root) / "raw"
        if machines is None:
            count_machines = sorted(local)
        else:
            ms = (machines,) if isinstance(machines, str) else tuple(machines)
            count_machines = [m for m in ms if m in local]
        for machine in count_machines:
            if not _valid_pool_component(machine):
                continue
            for src in srcs:
                if (raw / machine / src).is_dir():
                    skipped_local += 1
    total = _empty_stats(pool_roots=len(roots), skipped_local_roots=skipped_local)
    seen_sessions: dict[str, tuple[str, str]] = {}
    duplicate_sessions = set()
    for machine, src, root in roots:
        root_prefix = str(root).rstrip("/") + "/%"
        before = {
            r["session_id"] for r in conn.execute(
                "SELECT session_id FROM ingest_state WHERE path LIKE ? AND session_id IS NOT NULL",
                (root_prefix,),
            )
        }
        stats = refresh(conn, sources=(src,), machine=None, roots={src: root})
        _add_stats(total, stats)
        after = {
            r["session_id"] for r in conn.execute(
                "SELECT session_id FROM ingest_state WHERE path LIKE ? AND session_id IS NOT NULL",
                (root_prefix,),
            )
        }
        for sid in before | after:
            prior = seen_sessions.get(sid)
            here = (machine, src)
            if prior is not None and prior != here:
                duplicate_sessions.add(sid)
            else:
                seen_sessions[sid] = here
    total["duplicate_sessions"] = len(duplicate_sessions)
    if duplicate_sessions:
        print(f"  ~ pool duplicate session ids across machine roots: {len(duplicate_sessions)}",
              file=sys.stderr)
    return total


def _source_jobs(source: str, machine: Optional[str], raw_root: Optional[Path] = None):
    """(files, parse_fn, enrich) for one source — shared by full ingest and refresh."""
    machine = _machine_for_root(source, raw_root, machine)
    if source == "claude":
        root = raw_root or DEFAULT_CLAUDE_ROOT
        return (discover_claude_files(root),
                lambda p: claude.parse_file(p, machine=machine), None)
    if source == "codex":
        root = raw_root or DEFAULT_CODEX_ROOT
        titles = _load_codex_titles(root)

        def enrich(parsed):
            uuid = parsed.session.session_id.split(":", 1)[-1]
            if uuid in titles:
                parsed.session.title = titles[uuid]

        return (discover_codex_files(root),
                lambda p: codex.parse_file(p, machine=machine), enrich)
    if source == "pi":
        root = raw_root or DEFAULT_PI_ROOT
        return (discover_pi_files(root),
                lambda p: pi.parse_file(p, machine=machine), None)
    if source == "cursor":
        root = raw_root or DEFAULT_CURSOR_ROOT
        return (discover_cursor_files(root),
                lambda p: cursor.parse_file(p, machine=machine), None)
    raise ValueError(f"unknown source {source!r}")


def ingest_source(conn: sqlite3.Connection, source: str,
                  raw_root: Optional[Path] = None, machine: Optional[str] = None) -> dict:
    export_errors = 0
    if source == "cursor" and raw_root is None:
        raw_root, export_stats = export_local_cursor_archive()
        export_errors = export_stats["errors"]
    files, parse_fn, enrich = _source_jobs(source, machine, raw_root)
    stats = _ingest(conn, files, parse_fn, enrich)
    stats["errors"] += export_errors
    return stats


def _cursor_root_key(root: Path) -> str:
    # Keep roots isolated without resolving/following a caller-controlled symlink.
    return os.path.abspath(os.fspath(root))


def _cursor_revision_identity(value):
    if not isinstance(value, str) or not value.endswith(".json"):
        return None
    try:
        revision, digest = value[:-5].split("-", 1)
    except ValueError:
        return None
    if len(revision) != 20 or not revision.isdigit() \
            or len(digest) != 64 \
            or any(char not in "0123456789abcdef" for char in digest):
        return None
    return int(revision), digest


def _cursor_revision_name(value) -> bool:
    return _cursor_revision_identity(value) is not None


def _cursor_cache_row_valid(row, signature: str, session_key: str) -> bool:
    if row is None or row["validator_version"] != CURSOR_ARCHIVE_VALIDATOR_VERSION \
            or row["signature"] != signature:
        return False
    selected = row["selected_name"]
    if selected is None:
        return row["selected_session_id"] is None \
            and row["selected_revision"] is None \
            and row["selected_digest"] is None \
            and row["handled_name"] is None
    revision = row["selected_revision"]
    digest = row["selected_digest"]
    identity = _cursor_revision_identity(selected)
    selected_session_id = row["selected_session_id"]
    composer_id = selected_session_id.removeprefix("cursor:") \
        if isinstance(selected_session_id, str) else ""
    return identity is not None and identity == (revision, digest) \
        and isinstance(selected_session_id, str) \
        and selected_session_id.startswith("cursor:") \
        and bool(composer_id) \
        and hashlib.sha256(composer_id.encode("utf-8")).hexdigest() == session_key \
        and isinstance(revision, int) and not isinstance(revision, bool) \
        and revision > 0 \
        and isinstance(digest, str) and len(digest) == 64 \
        and all(char in "0123456789abcdef" for char in digest) \
        and (row["handled_name"] is None
             or _cursor_revision_name(row["handled_name"]))


def _cursor_cached_selection_needs_validation(cached) -> bool:
    selected = cached["selected_name"]
    if selected is None:
        return False
    accepted_revision = cached["accepted_revision"]
    accepted_digest = cached["accepted_digest"]
    accepted = (accepted_revision, accepted_digest) \
        if isinstance(accepted_revision, int) \
        and not isinstance(accepted_revision, bool) and accepted_revision > 0 \
        and isinstance(accepted_digest, str) and len(accepted_digest) == 64 \
        and all(char in "0123456789abcdef" for char in accepted_digest) \
        else None
    selected_rank = cached["selected_revision"], cached["selected_digest"]
    return cached["handled_name"] != selected \
        or accepted is None or selected_rank > accepted


def _refresh_cursor(conn: sqlite3.Connection, raw_root: Path,
                    machine: Optional[str]) -> dict:
    """Refresh one safe Cursor archive with per-session validation caching."""
    stats = _empty_stats()
    root = Path(raw_root)
    sessions = root / "sessions"
    try:
        root_stat = os.lstat(root)
        sessions_stat = os.lstat(sessions)
    except FileNotFoundError:
        # A missing/transient root is not evidence that accepted sessions vanished.
        return stats
    except OSError:
        stats["errors"] += 1
        return stats
    if not stat.S_ISDIR(root_stat.st_mode) or not stat.S_ISDIR(sessions_stat.st_mode):
        stats["errors"] += 1
        return stats
    try:
        scan = cursor_archive.scan_archive_metadata(root)
    except (OSError, cursor_archive.CursorArchiveError):
        stats["errors"] += 1
        return stats

    root_key = _cursor_root_key(root)
    cached_rows = {
        row["session_key"]: row for row in conn.execute(
            "SELECT cached.*, accepted.revision AS accepted_revision, "
            "accepted.digest AS accepted_digest "
            "FROM cursor_archive_heads AS cached "
            "LEFT JOIN cursor_session_heads AS accepted "
            "ON accepted.session_id=cached.selected_session_id "
            "WHERE cached.root=?",
            (root_key,),
        )
    }
    parsed_heads: dict[Path, cursor_archive.CursorHead] = {}
    updates = {}
    candidates = []
    for session in scan.sessions:
        cached = cached_rows.get(session.session_key)
        cache_hit = _cursor_cache_row_valid(
            cached, session.signature, session.session_key,
        )
        validate = not cache_hit
        if cache_hit:
            validate = _cursor_cached_selection_needs_validation(cached)
            if not validate:
                continue

        try:
            head = cursor_archive.select_session_head(session)
        except (OSError, cursor_archive.CursorArchiveError):
            # Retain the old cache row and retry this session on the next scan.
            continue
        if head is None:
            selected_name = selected_session_id = selected_digest = None
            selected_revision = None
        else:
            selected_name = head.path.name
            selected_session_id = f"cursor:{head.composer_id}"
            selected_revision = head.revision
            selected_digest = head.snapshot_digest
        handled_name = cached["handled_name"] if cache_hit else None

        selected_path = session.revision_dir / selected_name \
            if selected_name is not None else None
        if selected_path is not None:
            candidates.append(selected_path)
            parsed_heads[selected_path] = head

        updates[session.session_key] = {
            "signature": session.signature,
            "selected_name": selected_name,
            "selected_session_id": selected_session_id,
            "selected_revision": selected_revision,
            "selected_digest": selected_digest,
            "handled_name": handled_name,
            "path": selected_path,
            "dirty": True,
        }

    processed: set[Path] = set()
    if candidates:
        producer = _machine_for_root("cursor", root, machine)
        stats = _ingest(
            conn, sorted(candidates),
            lambda path: cursor.parse_head(parsed_heads[path], machine=producer),
            processed_paths=processed,
        )

    for update in updates.values():
        selected = update["selected_name"]
        if selected is not None and update["path"] in processed:
            update["handled_name"] = selected

    try:
        dirty_updates = {
            session_key: update for session_key, update in updates.items()
            if update["dirty"]
        }
        absent = set(cached_rows) - {session.session_key for session in scan.sessions}
        if not dirty_updates and not absent:
            return stats
        conn.executemany(
            """
            INSERT INTO cursor_archive_heads (
              root, session_key, validator_version, signature, selected_name,
              selected_session_id, selected_revision, selected_digest, handled_name
            ) VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(root, session_key) DO UPDATE SET
              validator_version=excluded.validator_version,
              signature=excluded.signature,
              selected_name=excluded.selected_name,
              selected_session_id=excluded.selected_session_id,
              selected_revision=excluded.selected_revision,
              selected_digest=excluded.selected_digest,
              handled_name=excluded.handled_name
            """,
            [(
                root_key, session_key, CURSOR_ARCHIVE_VALIDATOR_VERSION,
                update["signature"], update["selected_name"],
                update["selected_session_id"], update["selected_revision"],
                update["selected_digest"], update["handled_name"],
            ) for session_key, update in dirty_updates.items()],
        )
        conn.executemany(
            "DELETE FROM cursor_archive_heads WHERE root=? AND session_key=?",
            [(root_key, session_key) for session_key in absent],
        )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        stats["errors"] += 1
    return stats


def refresh(conn: sqlite3.Connection, sources=SOURCES, machine: Optional[str] = None,
            roots: Optional[dict] = None) -> dict:
    """Delta ingest: re-parse only files that are new or whose (mtime, size) changed
    since ingest_state last saw them. Cheap enough to run before every read —
    tens of ms when nothing changed — which makes the DB effectively always
    current for this machine: there is no 'not ingested yet' window. The Cursor
    export here is opportunistic (non-blocking, header-token changes only); the
    collector sweep owns full reconciles, so a read never waits on maintenance.
    `roots` optionally overrides a source's raw root ({"pi": Path(...)})."""
    state = None
    total = {"files": 0, "sessions": 0, "events": 0, "placements": 0,
             "skipped": 0, "conflicts": 0, "errors": 0}
    for src in sources:
        raw_root = (roots or {}).get(src)
        if src == "cursor" and raw_root is None:
            raw_root, export_stats = export_local_cursor_archive(authoritative=False)
            total["errors"] += export_stats["errors"]
        if src == "cursor":
            stats = _refresh_cursor(
                conn, Path(raw_root or DEFAULT_CURSOR_ROOT), machine,
            )
            for key, value in stats.items():
                total[key] += value
            continue
        files, parse_fn, enrich = _source_jobs(src, machine, raw_root)
        if state is None:
            state = {
                row["path"]: (row["mtime"], row["size"])
                for row in conn.execute("SELECT path, mtime, size FROM ingest_state")
            }
        changed = []
        for f in files:
            try:
                st = f.stat()
            except OSError:
                continue
            if state.get(str(f)) != (st.st_mtime, st.st_size):
                changed.append(f)
        if changed:
            stats = _ingest(conn, changed, parse_fn, enrich)
            for k, v in stats.items():
                total[k] += v
    return total


def ingest_all(conn: sqlite3.Connection, sources=SOURCES, machine: Optional[str] = None) -> dict:
    """Full pass: parse every discovered file (first build / disaster recovery).
    Also primes ingest_state, so the next refresh() is a delta. FTS stays current
    via the events triggers — no rebuild step."""
    total = {"files": 0, "sessions": 0, "events": 0, "placements": 0,
             "skipped": 0, "conflicts": 0, "errors": 0}
    for src in sources:
        stats = ingest_source(conn, src, machine=machine)
        print(f"  {src}: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
        for k, v in stats.items():
            total[k] += v
    conn.commit()
    return total


# Back-compat helper (the spine's original entry point).
def ingest_claude(conn: sqlite3.Connection, raw_root: Path = DEFAULT_CLAUDE_ROOT,
                  machine: Optional[str] = None) -> dict:
    stats = ingest_source(conn, "claude", raw_root=raw_root, machine=machine)
    conn.commit()
    return stats

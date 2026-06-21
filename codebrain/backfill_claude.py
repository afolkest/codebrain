"""One-shot Claude backup backfill into the codebrain raw pool.

This is deliberately separate from the recurring collector and from ingest:
historical exports are messy (whole .claude snapshots, duplicate monthly copies,
and sensitive/noisy state), while ingest expects a Claude-shaped raw root.

Input:
  backup zip files, or directories containing backup zip files

Output:
  <pool>/raw/<origin>/claude/projects/...
  <pool>/raw/<origin>/claude/_codebrain_backfill_manifest.json
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

from codebrain.collect import DEFAULT_POOL

DEFAULT_ORIGIN = "claude-backfill"
MANIFEST_NAME = "_codebrain_backfill_manifest.json"


@dataclass(frozen=True)
class MemberRef:
    archive: Path
    name: str
    rel: str
    size: int
    date_time: tuple


@dataclass(frozen=True)
class Candidate:
    member: MemberRef
    session_id: str
    path_session_id: str
    session_ids_seen: tuple[str, ...]
    sha256: str
    valid_records: int
    json_errors: int
    sidechain_records: int
    main_records: int
    uuid_count: int
    main_uuid_count: int
    max_ts: str
    main_max_ts: str
    unique_uuids: frozenset[str]
    main_unique_uuids: frozenset[str]


@dataclass(frozen=True)
class CopyPlan:
    member: MemberRef
    kind: str
    sha256: Optional[str] = None


def _safe_rel(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return bool(parts) and all(p not in ("", ".", "..") for p in parts)


def _claude_rel(name: str) -> Optional[str]:
    """Return path relative to a .claude root, or None if this member is outside it."""
    normalized = name.replace("\\", "/")
    marker = "/.claude/"
    if marker in normalized:
        rel = normalized.split(marker, 1)[1]
    elif normalized.startswith(".claude/"):
        rel = normalized[len(".claude/"):]
    else:
        return None
    return rel if _safe_rel(rel) else None


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_IFMT(mode) == stat.S_IFLNK


def _is_main_transcript(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return (
        len(parts) == 3
        and parts[0] == "projects"
        and parts[2].endswith(".jsonl")
    )


def _is_project_sidecar(rel: str) -> bool:
    parts = PurePosixPath(rel).parts
    return len(parts) >= 4 and parts[0] == "projects"


def _member_ref(archive: Path, info: zipfile.ZipInfo, rel: str) -> MemberRef:
    return MemberRef(
        archive=Path(archive),
        name=info.filename,
        rel=rel,
        size=info.file_size,
        date_time=tuple(info.date_time),
    )


def _candidate_from_member(zf: zipfile.ZipFile, archive: Path,
                           info: zipfile.ZipInfo, rel: str) -> Candidate:
    h = hashlib.sha256()
    session_ids: list[str] = []
    session_seen = set()
    unique_uuids: set[str] = set()
    main_unique_uuids: set[str] = set()
    valid_records = 0
    json_errors = 0
    sidechain_records = 0
    main_records = 0
    max_ts = ""
    main_max_ts = ""
    first_session_id = None

    with zf.open(info, "r") as fh:
        for raw in fh:
            h.update(raw)
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                json_errors += 1
                continue
            if not isinstance(rec, dict):
                continue
            valid_records += 1
            sidechain = rec.get("isSidechain") is True
            if sidechain:
                sidechain_records += 1
            else:
                main_records += 1
            sid = rec.get("sessionId")
            if isinstance(sid, str) and sid:
                if first_session_id is None:
                    first_session_id = sid
                if sid not in session_seen:
                    session_ids.append(sid)
                    session_seen.add(sid)
            uuid = rec.get("uuid")
            if isinstance(uuid, str) and uuid:
                unique_uuids.add(uuid)
                if not sidechain:
                    main_unique_uuids.add(uuid)
            ts = rec.get("timestamp")
            if isinstance(ts, str) and ts > max_ts:
                max_ts = ts
            if not sidechain and isinstance(ts, str) and ts > main_max_ts:
                main_max_ts = ts

    path_session_id = PurePosixPath(rel).stem
    session_id = first_session_id or path_session_id
    return Candidate(
        member=_member_ref(archive, info, rel),
        session_id=session_id,
        path_session_id=path_session_id,
        session_ids_seen=tuple(session_ids),
        sha256=h.hexdigest(),
        valid_records=valid_records,
        json_errors=json_errors,
        sidechain_records=sidechain_records,
        main_records=main_records,
        uuid_count=len(unique_uuids),
        main_uuid_count=len(main_unique_uuids),
        max_ts=max_ts,
        main_max_ts=main_max_ts,
        unique_uuids=frozenset(unique_uuids),
        main_unique_uuids=frozenset(main_unique_uuids),
    )


def _session_id_from_jsonl(path: Path) -> Optional[str]:
    first_session_id = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                sid = rec.get("sessionId")
                if isinstance(sid, str) and sid:
                    first_session_id = sid
                    break
    except OSError:
        return None
    return first_session_id or path.stem


def _existing_session_ids(raw_root: Optional[Path]) -> tuple[set[str], dict]:
    stats = {"existing_root_files": 0, "existing_root_sessions": 0, "existing_root_errors": 0}
    if raw_root is None:
        return set(), stats
    projects = Path(raw_root).expanduser() / "projects"
    if not projects.is_dir():
        return set(), stats
    out = set()
    for path in sorted(projects.glob("*/*.jsonl")):
        stats["existing_root_files"] += 1
        sid = _session_id_from_jsonl(path)
        if sid is None:
            stats["existing_root_errors"] += 1
            continue
        out.add(sid)
    stats["existing_root_sessions"] = len(out)
    return out, stats


def _is_legacy_top_level_subagent(c: Candidate) -> bool:
    """Old Claude exports can put subagent files at projects/<project>/*.jsonl.

    Their records carry the parent's sessionId and sidechain marker. Treat them
    as sidecars and retarget them into the modern session/subagents directory so
    normal ingest does not parse them as main transcripts.
    """
    return (
        c.session_id != c.path_session_id
        and c.sidechain_records > 0
        and c.main_records == 0
    )


def _candidate_key(c: Candidate):
    # Structured transcript evidence first: newest event timestamp, then amount
    # of parsed main-session content. Inline sidechain records are deliberately
    # excluded because the Claude adapter skips them during main transcript ingest.
    return (
        c.main_max_ts or "",
        c.main_records,
        c.main_uuid_count,
        c.member.size,
        str(c.member.archive),
        c.member.name,
    )


def _decision(selected: Candidate, candidates: list[Candidate]) -> str:
    if len(candidates) == 1:
        return "only-candidate"
    hashes = {c.sha256 for c in candidates}
    if len(hashes) == 1:
        return "exact-duplicate"
    others = [c for c in candidates if c is not selected]
    if selected.main_unique_uuids and all(c.main_unique_uuids <= selected.main_unique_uuids for c in others):
        return "uuid-superset"
    return "latest-structured-timestamp"


def _candidate_manifest(c: Candidate) -> dict:
    return {
        "archive": str(c.member.archive),
        "member": c.member.name,
        "rel": c.member.rel,
        "bytes": c.member.size,
        "sha256": c.sha256,
        "session_id": c.session_id,
        "path_session_id": c.path_session_id,
        "session_ids_seen": list(c.session_ids_seen),
        "valid_records": c.valid_records,
        "json_errors": c.json_errors,
        "sidechain_records": c.sidechain_records,
        "main_records": c.main_records,
        "uuid_count": c.uuid_count,
        "main_uuid_count": c.main_uuid_count,
        "max_ts": c.max_ts,
        "main_max_ts": c.main_max_ts,
    }


def _discover_archives(inputs: list[Path]) -> list[Path]:
    archives: list[Path] = []
    seen = set()
    for p in inputs:
        p = Path(p).expanduser()
        if p.is_dir():
            found = sorted(p.glob("*.zip"))
        else:
            found = [p]
        for f in found:
            key = str(f.resolve()) if f.exists() else str(f)
            if key in seen:
                continue
            seen.add(key)
            if f.is_file() and f.suffix.lower() == ".zip":
                archives.append(f)
    return archives


def _scan_archives(archives: list[Path]) -> tuple[dict, list, list, dict]:
    stats = {
        "archives": len(archives),
        "main_candidates": 0,
        "legacy_subagents_seen": 0,
        "project_sidecars_seen": 0,
        "skipped_symlink": 0,
        "skipped_unsafe": 0,
        "skipped_unallowlisted": 0,
        "scan_errors": 0,
    }
    by_session: dict[str, list[Candidate]] = {}
    legacy_subagents: list[Candidate] = []
    project_sidecars: list[MemberRef] = []

    for archive in archives:
        try:
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    if _is_symlink(info):
                        stats["skipped_symlink"] += 1
                        continue
                    rel = _claude_rel(info.filename)
                    if rel is None:
                        stats["skipped_unallowlisted"] += 1
                        continue
                    if not _safe_rel(rel):
                        stats["skipped_unsafe"] += 1
                        continue
                    if _is_main_transcript(rel):
                        try:
                            c = _candidate_from_member(zf, archive, info, rel)
                        except (OSError, zipfile.BadZipFile, RuntimeError):
                            stats["scan_errors"] += 1
                            continue
                        if _is_legacy_top_level_subagent(c):
                            legacy_subagents.append(c)
                            stats["legacy_subagents_seen"] += 1
                        else:
                            by_session.setdefault(c.session_id, []).append(c)
                            stats["main_candidates"] += 1
                    elif _is_project_sidecar(rel):
                        project_sidecars.append(_member_ref(archive, info, rel))
                        stats["project_sidecars_seen"] += 1
                    else:
                        stats["skipped_unallowlisted"] += 1
        except (OSError, zipfile.BadZipFile):
            stats["scan_errors"] += 1

    return by_session, legacy_subagents, project_sidecars, stats


def _dest_path(dest_root: Path, rel: str) -> Path:
    return Path(dest_root).joinpath(*PurePosixPath(rel).parts)


def _stale_dest_path(dest_root: Path, run_id: str, rel: str) -> Path:
    return (Path(dest_root) / "_codebrain_backfill_stale" / run_id).joinpath(*PurePosixPath(rel).parts)


def _move_aside(dest_root: Path, rel: str, run_id: str, dry_run: bool) -> Optional[Path]:
    src = _dest_path(dest_root, rel)
    if not src.exists():
        return None
    if dry_run:
        return _stale_dest_path(dest_root, run_id, rel)
    dst = _stale_dest_path(dest_root, run_id, rel)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        base = dst
        i = 1
        while dst.exists():
            dst = base.with_name(f"{base.name}.{i}")
            i += 1
    os.replace(src, dst)
    return dst


def _ensure_parent_dir(dest_root: Path, rel: str, run_id: str) -> None:
    cur = Path(dest_root)
    parts = PurePosixPath(rel).parts[:-1]
    rel_parts: list[str] = []
    for part in parts:
        rel_parts.append(part)
        cur = cur / part
        if cur.exists() and not cur.is_dir():
            moved = _move_aside(dest_root, "/".join(rel_parts), run_id, False)
            if moved is None:
                raise OSError(f"could not move path blocking directory creation: {cur}")
        cur.mkdir(exist_ok=True)


def _retarget_member(member: MemberRef, rel: str) -> MemberRef:
    return MemberRef(
        archive=member.archive,
        name=member.name,
        rel=rel,
        size=member.size,
        date_time=member.date_time,
    )


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _zip_member_sha256(zf: zipfile.ZipFile, member: MemberRef) -> str:
    h = hashlib.sha256()
    with zf.open(member.name, "r") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _same_existing(dest: Path, zf: zipfile.ZipFile, plan: CopyPlan) -> bool:
    if not dest.is_file() or dest.stat().st_size != plan.member.size:
        return False
    expected = plan.sha256 or _zip_member_sha256(zf, plan.member)
    return _file_sha256(dest) == expected


def _member_mtime(member: MemberRef) -> Optional[float]:
    try:
        return time.mktime(tuple(member.date_time) + (0, 0, -1))
    except (OverflowError, TypeError, ValueError):
        return None


def _tmp_path(dest_root: Path, plan: CopyPlan) -> Path:
    token = hashlib.sha1(
        f"{plan.member.archive}\0{plan.member.name}\0{plan.member.rel}".encode()
    ).hexdigest()[:16]
    return Path(dest_root) / "_codebrain_backfill_tmp" / f"{os.getpid()}-{token}.part"


def _copy_member(zf: zipfile.ZipFile, plan: CopyPlan, dest_root: Path,
                 dry_run: bool, run_id: str) -> str:
    dest = _dest_path(dest_root, plan.member.rel)
    if dry_run:
        return "dry_run"
    if _same_existing(dest, zf, plan):
        return "unchanged"
    tmp = _tmp_path(dest_root, plan)
    moved: Optional[Path] = None
    try:
        tmp.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(plan.member.name, "r") as src, open(tmp, "wb") as out:
            for chunk in iter(lambda: src.read(1024 * 1024), b""):
                out.write(chunk)
        existed = dest.exists()
        _ensure_parent_dir(dest_root, plan.member.rel, run_id)
        if existed:
            moved = _move_aside(dest_root, plan.member.rel, run_id, dry_run)
        os.replace(tmp, dest)
        mtime = _member_mtime(plan.member)
        if mtime is not None:
            os.utime(dest, (mtime, mtime))
    except Exception:
        if moved is not None and not dest.exists():
            try:
                os.replace(moved, dest)
            except OSError:
                pass
        raise
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
    return "replaced" if moved is not None else "written"


def _copy_plans(plans: list[CopyPlan], dest_root: Path, dry_run: bool, run_id: str) -> dict:
    stats = {
        "planned": len(plans), "written": 0, "replaced": 0,
        "unchanged": 0, "dry_run": 0, "copy_errors": 0,
    }
    by_archive: dict[Path, list[CopyPlan]] = {}
    for plan in plans:
        by_archive.setdefault(plan.member.archive, []).append(plan)
    for archive, archive_plans in by_archive.items():
        try:
            with zipfile.ZipFile(archive) as zf:
                for plan in archive_plans:
                    try:
                        result = _copy_member(zf, plan, dest_root, dry_run, run_id)
                    except (OSError, zipfile.BadZipFile, RuntimeError):
                        stats["copy_errors"] += 1
                        continue
                    stats[result] += 1
        except (OSError, zipfile.BadZipFile):
            stats["copy_errors"] += len(archive_plans)
    return stats


def _collision_plan(plan: CopyPlan, run_id: str) -> CopyPlan:
    token = hashlib.sha1(f"{plan.member.archive}\0{plan.member.name}".encode()).hexdigest()[:16]
    rel = f"_codebrain_backfill_collisions/{run_id}/{plan.kind}/{plan.member.rel}.{token}"
    return CopyPlan(_retarget_member(plan.member, rel), f"{plan.kind}-collision", plan.sha256)


def _add_plan(plans_by_rel: dict[str, CopyPlan], plan: CopyPlan,
              collision_plans: list[CopyPlan], run_id: str) -> int:
    cur = plans_by_rel.get(plan.member.rel)
    if cur is None:
        plans_by_rel[plan.member.rel] = plan
        return 1
    if cur.member.archive == plan.member.archive and cur.member.name == plan.member.name:
        return 0
    if (plan.member.size, plan.member.date_time, str(plan.member.archive)) > (
        cur.member.size, cur.member.date_time, str(cur.member.archive)
    ):
        plans_by_rel[plan.member.rel] = plan
        collision_plans.append(_collision_plan(cur, run_id))
    else:
        collision_plans.append(_collision_plan(plan, run_id))
    return 0


def _prune_unplanned(dest_root: Path, planned_rels: set[str],
                     dry_run: bool, run_id: str) -> dict:
    """Move stale prior backfill outputs out of ingest-discovered locations.

    Backfill owns this generated origin subtree. Moving unplanned files aside
    preserves evidence while keeping `ingest --raw-root` from parsing stale
    winners left by a previous run.
    """
    stats = {"stale": 0, "stale_moved": 0, "stale_errors": 0}
    roots = [Path(dest_root) / "projects"]
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            rel = str(path.relative_to(dest_root).as_posix())
            if rel in planned_rels:
                continue
            stats["stale"] += 1
            try:
                if _move_aside(dest_root, rel, run_id, dry_run):
                    stats["stale_moved"] += 1
            except OSError:
                stats["stale_errors"] += 1
    return stats


def backfill(inputs, pool_root: Path = DEFAULT_POOL, origin: str = DEFAULT_ORIGIN,
             dry_run: bool = False,
             manifest_path: Optional[Path] = None,
             existing_root: Optional[Path] = None) -> dict:
    """Import historical Claude backup zips into a Claude-shaped raw pool subtree.

    The selection policy groups main transcripts by the structured Claude
    `sessionId` field when present (filename stem only as the documented fallback)
    and picks the candidate with the latest event timestamp, then the most parsed
    content. It never inspects prompt text to classify sessions.
    """
    if not origin or "/" in origin or origin in (".", ".."):
        raise ValueError("origin must be a single path component")

    input_paths = [Path(p) for p in (inputs if isinstance(inputs, (list, tuple)) else [inputs])]
    archives = _discover_archives(input_paths)
    by_session, legacy_subagents, project_sidecars, scan_stats = _scan_archives(archives)
    existing_session_ids, existing_stats = _existing_session_ids(existing_root)
    skipped_existing_sessions = sorted(set(by_session) & existing_session_ids)
    for session_id in skipped_existing_sessions:
        del by_session[session_id]

    dest_root = Path(pool_root).expanduser() / "raw" / origin / "claude"
    sessions = []
    plans_by_rel: dict[str, CopyPlan] = {}
    collision_plans: list[CopyPlan] = []
    selected: list[Candidate] = []
    sidecar_candidate_refs = 0
    sidecars_planned = 0
    legacy_subagent_candidate_refs = 0
    legacy_subagents_planned = 0
    legacy_by_session: dict[str, list[Candidate]] = {}
    for c in legacy_subagents:
        legacy_by_session.setdefault(c.session_id, []).append(c)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    for session_id in sorted(by_session):
        candidates = by_session[session_id]
        winner = max(candidates, key=_candidate_key)
        selected.append(winner)
        decision = _decision(winner, candidates)
        _add_plan(plans_by_rel, CopyPlan(winner.member, "main", winner.sha256),
                  collision_plans, run_id)

        winner_sidecar_prefix = winner.member.rel[:-len(".jsonl")] + "/"
        sidecars = []
        seen_sidecar_source = set()
        for c in candidates:
            source_prefix = c.member.rel[:-len(".jsonl")] + "/"
            for member in project_sidecars:
                if member.archive != c.member.archive or not member.rel.startswith(source_prefix):
                    continue
                ident = (str(member.archive), member.name)
                if ident in seen_sidecar_source:
                    continue
                seen_sidecar_source.add(ident)
                rel = winner_sidecar_prefix + member.rel[len(source_prefix):]
                sidecars.append(_retarget_member(member, rel))
        sidecar_candidate_refs += len(sidecars)
        for member in sidecars:
            sidecars_planned += _add_plan(
                plans_by_rel, CopyPlan(member, "project-sidecar"),
                collision_plans, run_id,
            )

        legacy_for_session = legacy_by_session.get(winner.session_id, [])
        legacy_subagent_candidate_refs += len(legacy_for_session)
        subagent_prefix = winner.member.rel[:-len(".jsonl")] + "/subagents/"
        for c in legacy_for_session:
            rel = subagent_prefix + c.path_session_id + ".jsonl"
            member = _retarget_member(c.member, rel)
            legacy_subagents_planned += _add_plan(
                plans_by_rel, CopyPlan(member, "legacy-subagent", c.sha256),
                collision_plans, run_id,
            )

        sessions.append({
            "session_id": session_id,
            "decision": decision,
            "selected": _candidate_manifest(winner),
            "candidates": [_candidate_manifest(c) for c in sorted(candidates, key=_candidate_key)],
            "project_sidecar_candidates": len(sidecars),
            "legacy_subagent_candidates": len(legacy_for_session),
        })

    plans = list(plans_by_rel.values()) + collision_plans
    planned_rels = {p.member.rel for p in plans}
    canonical_rels = set(plans_by_rel)
    stale_stats = _prune_unplanned(dest_root, planned_rels, dry_run, run_id)
    copy_stats = _copy_plans(plans, dest_root, dry_run, run_id)

    stats = {
        **scan_stats,
        **existing_stats,
        "skipped_existing_sessions": len(skipped_existing_sessions),
        "selected_sessions": len(selected),
        "duplicate_sessions": sum(1 for candidates in by_session.values() if len(candidates) > 1),
        "exact_duplicate_sessions": sum(
            1 for candidates in by_session.values()
            if len(candidates) > 1 and len({c.sha256 for c in candidates}) == 1
        ),
        "conflicting_duplicate_sessions": sum(
            1 for candidates in by_session.values()
            if len(candidates) > 1 and len({c.sha256 for c in candidates}) > 1
        ),
        "project_sidecar_candidate_refs": sidecar_candidate_refs,
        "project_sidecars_planned": sidecars_planned,
        "legacy_subagent_candidate_refs": legacy_subagent_candidate_refs,
        "legacy_subagents_planned": legacy_subagents_planned,
        "collision_files_planned": len(collision_plans),
        **stale_stats,
        **copy_stats,
    }

    manifest = {
        "kind": "codebrain.claude_backfill_manifest",
        "version": 1,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "inputs": [str(p) for p in input_paths],
        "archives": [str(p) for p in archives],
        "pool_root": str(Path(pool_root).expanduser()),
        "origin": origin,
        "dest_root": str(dest_root),
        "run_id": run_id,
        "existing_root": str(Path(existing_root).expanduser()) if existing_root is not None else None,
        "dry_run": dry_run,
        "stats": stats,
        "planned_paths": sorted(canonical_rels),
        "collision_paths": sorted(p.member.rel for p in collision_plans),
        "skipped_existing_session_ids": skipped_existing_sessions,
        "sessions": sessions,
    }

    if not dry_run:
        out = Path(manifest_path).expanduser() if manifest_path else dest_root / MANIFEST_NAME
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        manifest["manifest_path"] = str(out)
    return manifest

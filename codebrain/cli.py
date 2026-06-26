"""sessdb — thin CLI over the codebrain SQLite cache.

Daily archaeology:
  sessdb recent                         sessions by latest user activity
  sessdb userlog                        recent user messages (intent-first)
  sessdb search <query> [--around N]    FTS over filtered event text
  sessdb turns <session>                user-centered turn expansion
  sessdb lineage <session>              factual parent/child session lineage
  sessdb refs <session>                 conversation -> files/commands/commits
  sessdb touched <path>                 file/artifact -> sessions/events

Setup / repair:
  sessdb ingest                         full build/rebuild of the local DB
  sessdb collect [--install-launchd]    mirror raw logs into the append-only pool
  sessdb ingest-pool                    debug/repair ingest of synced pool subtrees
  sessdb backfill-claude <path>         import historical Claude backups into pool
  sessdb hide <session> --reason <why>  hide noisy sessions from default retrieval
  sessdb unhide <session>               restore sessions to default retrieval
  sessdb hidden                         audit hidden sessions

Escape hatches:
  sessdb show <session> [--all]         raw transcript view
  sessdb list [--limit N]               session metadata by start time
  sessdb grep <pattern> [paths...]      ripgrep raw logs (live + remote pool by default)
  sessdb schema                         print the DDL

Read commands refresh first: changed/new local live-home files and synced remote
pool files are delta-ingested before the query runs. Results stay current for this
machine and include synced remote history after Syncthing arrives. --no-refresh skips it.

Raw SQL escape hatch: just open the DB with any sqlite3 client (see --schema).
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codebrain import bmux
from codebrain.backfill_claude import DEFAULT_ORIGIN, backfill as backfill_claude
from codebrain.collect import DEFAULT_POOL, LAUNCHD_LABEL, collect_all, install_launchd
from codebrain.db import DEFAULT_DB, connect, has_fts5
from codebrain.ingest import (
    DEFAULT_CLAUDE_ROOT, DEFAULT_CODEX_ROOT, DEFAULT_PI_ROOT, SOURCES,
    discover_pool_roots, ingest_all, ingest_source, local_machine_names, refresh,
    refresh_pool,
)


def _refresh_notice(local_stats, pool_stats):
    parts = []
    if local_stats.get("files"):
        parts.append(f"local {local_stats['files']} file(s), +{local_stats['events']} events")
    if pool_stats.get("files"):
        parts.append(f"pool {pool_stats['files']} file(s), +{pool_stats['events']} events")
    if parts:
        print(f"(refreshed {'; '.join(parts)})", file=sys.stderr)


def _open(args, sync_bmux=True):
    """Connect and (unless --no-refresh) delta-ingest whatever changed on disk."""
    conn = connect(args.db)
    transcripts_changed = False
    if not getattr(args, "no_refresh", False):
        local_stats = refresh(conn)
        pool_stats = {"files": 0, "events": 0}
        if (Path(DEFAULT_POOL) / "raw").is_dir():
            pool_stats = refresh_pool(conn, Path(DEFAULT_POOL), include_local=False,
                                      local_machines=local_machine_names())
        _refresh_notice(local_stats, pool_stats)
        transcripts_changed = bool(local_stats.get("events") or pool_stats.get("events"))
    # bmux provenance overlay runs even under --no-refresh: because "no
    # event_origins row == human", a stale overlay would leak master-control into
    # the human bucket. The sync is gated (a log stat check + early return when
    # nothing changed), so under --no-refresh it's nearly free yet still reflects a
    # freshly-appended bmux send. Atomic (self-rolls-back, retries next read) and
    # best-effort — a bmux-side problem must never break a read.
    if sync_bmux:
        try:
            bmux.sync(conn, changed_hint=transcripts_changed)
        except Exception as exc:  # noqa: BLE001
            print(f"(bmux provenance skipped: {exc})", file=sys.stderr)
    return conn


def _oneline(s, n=200):
    if not s:
        return ""
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _short_path(path, n=70):
    if not path:
        return ""
    s = str(path)
    home = str(Path.home())
    if s == home:
        s = "~"
    elif s.startswith(home + "/"):
        s = "~" + s[len(home):]
    return s if len(s) <= n else "…" + s[-(n - 1):]


def _wrapped(label, text, chars=0, width=100):
    body = (text or "") if chars == 0 else _oneline(text, chars)
    if not body:
        return label.rstrip()
    return textwrap.fill(body, width=width, initial_indent=label,
                         subsequent_indent=" " * len(label))


def _since_cutoff(value):
    """Return a timestamp/date cutoff string for SQLite date comparisons.

    Accepts either an explicit timestamp/date (passed through) or a small
    relative duration like ``7d``/``12h``. Callers compare with SQLite date
    functions instead of raw text so fractional and non-fractional timestamps
    share the same boundary semantics.
    """
    if not value:
        return None
    v = value.strip()
    m = re.fullmatch(r"(\d+)([smhdw])", v)
    if not m:
        return v
    n = int(m.group(1))
    unit = m.group(2)
    delta = {
        "s": timedelta(seconds=n),
        "m": timedelta(minutes=n),
        "h": timedelta(hours=n),
        "d": timedelta(days=n),
        "w": timedelta(weeks=n),
    }[unit]
    return (datetime.now(timezone.utc) - delta).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _resolve_session(conn, ident: str):
    ident = (ident or "").strip()
    if not ident:
        return None
    row = conn.execute("SELECT session_id FROM sessions WHERE session_id = ?", (ident,)).fetchone()
    if row:
        return row["session_id"]
    patterns = [f"{ident}%", *(f"{source}:{ident}%" for source in SOURCES)]
    row = conn.execute(
        f"""
        SELECT session_id FROM sessions
        WHERE {' OR '.join('session_id LIKE ?' for _ in patterns)}
        ORDER BY session_id
        LIMIT 1
        """,
        patterns,
    ).fetchone()
    return row["session_id"] if row else None


def _session_match_patterns(ident: str) -> list[str]:
    return [f"{ident}%", *(f"{source}:{ident}%" for source in SOURCES)]


def _resolve_unique_session(conn, ident: str):
    ident = (ident or "").strip()
    if not ident:
        return None, []
    row = conn.execute("SELECT session_id FROM sessions WHERE session_id = ?", (ident,)).fetchone()
    if row:
        return row["session_id"], [row["session_id"]]
    patterns = _session_match_patterns(ident)
    rows = conn.execute(
        f"""
        SELECT session_id FROM sessions
        WHERE {' OR '.join('session_id LIKE ?' for _ in patterns)}
        ORDER BY session_id
        LIMIT 21
        """,
        patterns,
    ).fetchall()
    matches = [r["session_id"] for r in rows]
    return (matches[0] if len(matches) == 1 else None), matches


def _visibility_where(args, alias: str = "s"):
    if getattr(args, "include_hidden", False):
        return None
    if getattr(args, "only_hidden", False):
        return f"{alias}.hidden_at IS NOT NULL"
    return f"{alias}.hidden_at IS NULL"


def _append_visibility(where: list[str], args, alias: str = "s") -> None:
    clause = _visibility_where(args, alias)
    if clause:
        where.append(clause)


def _add_visibility_args(parser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--include-hidden", action="store_true",
                       help="include sessions hidden from default retrieval")
    group.add_argument("--only-hidden", action="store_true",
                       help="only show sessions hidden from default retrieval")


ORIGIN_CHOICES = ("human", "master-control", "unknown", "all")
# CLI origin token -> stored event_origins.origin value. A strict whitelist so the
# value interpolated into SQL below can never be attacker/caller controlled.
_ORIGIN_DB = {"master-control": "master_control", "unknown": "unknown"}


def _origin_where(args, session_col="t.session_id", event_col="t.event_id"):
    """Filter native user messages by bmux provenance.

    The default `human` protects the clean-intent bucket: absence of an
    `event_origins` row == human, so unmatched native user messages always pass.
    Returns a SQL clause (subquery against the rebuildable overlay) or None.
    """
    origin = getattr(args, "origin", None) or "human"
    if origin == "all":
        return None
    sub = (f"SELECT 1 FROM event_origins eo "
           f"WHERE eo.session_id = {session_col} AND eo.event_id = {event_col}")
    if origin == "human":
        return f"NOT EXISTS ({sub} AND eo.origin IN ('master_control', 'unknown'))"
    db_origin = _ORIGIN_DB.get(origin)
    if db_origin is None:
        return None  # unknown token (shouldn't reach here: argparse-constrained)
    return f"EXISTS ({sub} AND eo.origin = '{db_origin}')"


def _append_origin(where: list[str], args, session_col="t.session_id",
                   event_col="t.event_id") -> None:
    clause = _origin_where(args, session_col, event_col)
    if clause:
        where.append(clause)


def _add_origin_args(parser, default="human") -> None:
    parser.add_argument("--origin", choices=ORIGIN_CHOICES, default=default,
                        help="provenance of native user messages (default %(default)s; "
                             "bmux master-control kept out of the human-intent bucket)")


def _origin_label(origin) -> str:
    return f" ({origin})" if origin and origin != "human" else ""


def _timestamp_where(column: str, op: str) -> str:
    return f"julianday({column}) {op} julianday(?)"


def cmd_ingest(args):
    sources = SOURCES if args.source == "all" else (args.source,)
    if args.raw_root:
        if args.source == "all":
            print("--raw-root requires --source claude|codex|pi", file=sys.stderr)
            sys.exit(2)
    conn = connect(args.db)
    print(f"ingesting [{', '.join(sources)}] → {args.db}")
    if args.raw_root:
        total = ingest_source(conn, args.source, raw_root=Path(args.raw_root),
                              machine=args.machine)
        conn.commit()
    else:
        total = ingest_all(conn, sources=sources, machine=args.machine)
    conn.close()
    print("done: " + ", ".join(f"{k}={v}" for k, v in total.items()))


def cmd_collect(args):
    try:
        if args.install_launchd:
            path = install_launchd(interval=args.interval, pool_root=Path(args.pool),
                                   source=args.source, machine=args.machine)
            print(f"LaunchAgent loaded: {path}")
            print(f"  sweeps every {args.interval}s → {args.pool}  "
                  f"(log: ~/.codebrain/logs/collect.log)")
            print(f"  remove with: launchctl bootout gui/$(id -u)/{LAUNCHD_LABEL} && rm {path}")
            return
        sources = SOURCES if args.source == "all" else (args.source,)
        print(f"collecting [{', '.join(sources)}] → {args.pool}")
        total = collect_all(sources=sources, machine=args.machine, pool_root=Path(args.pool))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    print("done: " + ", ".join(f"{k}={v}" for k, v in total.items()))


def cmd_ingest_pool(args):
    sources = SOURCES if args.source == "all" else (args.source,)
    machines = (args.machine,) if args.machine else None
    conn = connect(args.db)
    local_names = local_machine_names()
    print(f"ingesting pool [{', '.join(sources)}] from {args.pool}")
    try:
        total = refresh_pool(
            conn, Path(args.pool), sources=sources, machines=machines,
            include_local=args.include_local, local_machines=local_names,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        conn.close()
        sys.exit(2)
    print("done: " + ", ".join(f"{k}={v}" for k, v in total.items()))
    if total.get("pool_roots") == 0:
        print(f"no matching pool roots under {Path(args.pool) / 'raw'}")
    if total.get("skipped_local_roots") and not args.include_local:
        print(f"skipped local pool root(s): {total['skipped_local_roots']} "
              "(--include-local to ingest explicitly)")
    if args.include_local:
        print("reparsing local live homes after --include-local to keep live logs authoritative")
        live_total = {"files": 0, "sessions": 0, "events": 0, "placements": 0,
                      "skipped": 0, "conflicts": 0, "errors": 0}
        for src in sources:
            stats = ingest_source(conn, src)
            for k in live_total:
                live_total[k] += stats.get(k, 0)
        conn.commit()
        print("live: " + ", ".join(f"{k}={v}" for k, v in live_total.items()))
    conn.close()


def cmd_backfill_claude(args):
    manifest = backfill_claude(
        [Path(p) for p in args.paths],
        pool_root=Path(args.pool),
        origin=args.origin,
        dry_run=args.dry_run,
        manifest_path=Path(args.manifest) if args.manifest else None,
        existing_root=DEFAULT_CLAUDE_ROOT,
    )
    if args.json:
        print(json.dumps(manifest, indent=2))
        return
    stats = manifest["stats"]
    print(f"backfilled Claude backups → {manifest['dest_root']}")
    print("done: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    if manifest.get("manifest_path"):
        print(f"manifest: {manifest['manifest_path']}")
    if not args.dry_run:
        print("ingest restored sessions with:")
        print(f"  sessdb ingest --source claude --raw-root {shlex.quote(manifest['dest_root'])}")


def _utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _resolve_unique_or_die(conn, ident: str):
    sid, matches = _resolve_unique_session(conn, ident)
    if sid:
        return sid
    if not matches:
        print(f"no session matching {ident!r}", file=sys.stderr)
    else:
        n = "20+" if len(matches) > 20 else str(len(matches))
        print(f"ambiguous session prefix {ident!r}; {n} matches, first few:", file=sys.stderr)
        for match in matches[:10]:
            print(f"  {match}", file=sys.stderr)
    sys.exit(1)


def cmd_hide(args):
    conn = _open(args)
    try:
        sids = [_resolve_unique_or_die(conn, ident) for ident in args.sessions]
    except SystemExit:
        conn.close()
        raise
    hidden_at = _utc_now()
    for sid in sids:
        conn.execute(
            "UPDATE sessions SET hidden_at=?, hidden_reason=? WHERE session_id=?",
            (hidden_at, args.reason, sid),
        )
    conn.commit()
    conn.close()
    for sid in sids:
        print(f"hidden: {sid}")
    print(f"done: hidden={len(sids)}")


def cmd_unhide(args):
    conn = _open(args)
    try:
        sids = [_resolve_unique_or_die(conn, ident) for ident in args.sessions]
    except SystemExit:
        conn.close()
        raise
    for sid in sids:
        conn.execute(
            "UPDATE sessions SET hidden_at=NULL, hidden_reason=NULL WHERE session_id=?",
            (sid,),
        )
    conn.commit()
    conn.close()
    for sid in sids:
        print(f"unhidden: {sid}")
    print(f"done: unhidden={len(sids)}")


def cmd_hidden(args):
    conn = _open(args)
    where = ["s.hidden_at IS NOT NULL"]
    params: list = []
    if args.source:
        where.append("s.source = ?")
        params.append(args.source)
    if args.cwd:
        where.append("COALESCE(s.cwd, '') LIKE ?")
        params.append(f"%{args.cwd}%")
    params.append(args.limit)
    rows = conn.execute(
        f"""
        SELECT s.session_id, s.source, s.machine, s.cwd, s.started_at,
               s.hidden_at, s.hidden_reason, s.title
        FROM sessions s
        WHERE {' AND '.join(where)}
        ORDER BY s.hidden_at DESC, s.session_id
        LIMIT ?
        """,
        params,
    ).fetchall()
    if args.json:
        json.dump([dict(r) for r in rows], sys.stdout, ensure_ascii=False)
        print()
    else:
        for r in rows:
            hidden_at = (r["hidden_at"] or "")[:19]
            started = (r["started_at"] or "")[:19]
            print(f"{hidden_at:19}  {started:19}  {r['source']:6}  {r['session_id']}")
            print(f"  reason: {r['hidden_reason']}")
            print(f"  cwd: {_short_path(r['cwd'])}")
    conn.close()


def cmd_list(args):
    conn = _open(args)
    where = []
    _append_visibility(where, args)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    rows = conn.execute(
        f"""
        SELECT s.session_id, s.source, s.started_at, s.cwd, s.title,
               s.hidden_at, s.hidden_reason,
               COUNT(se.event_id) AS n
        FROM sessions s
        LEFT JOIN session_events se ON se.session_id = s.session_id AND se.live = 1
        {where_sql}
        GROUP BY s.session_id
        ORDER BY s.started_at DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    for r in rows:
        started = (r["started_at"] or "")[:19]
        hidden = " hidden" if r["hidden_at"] else ""
        print(f"{started:19}  {r['n']:>5}  {r['source']:6}  {r['session_id']:<44}  "
              f"{_oneline(r['cwd'], 40):<40}  {_oneline(r['title'], 50)}{hidden}")
    conn.close()


def _is_synthetic_user_text(text) -> bool:
    text = (text or "").lstrip()
    return (
        text.startswith("<local-command-")
        or text.startswith("<command-name>")
        or text.startswith("<task-notification>")
    )


def _not_subagent_session_sql() -> str:
    """Exclude subagent/fork sessions via structured lineage/tool-call evidence.

    Existing DBs may not have relation='subagent' until their raw files are
    re-parsed with the newer pi adapter, so also inspect the branch-point event's
    raw toolCall block. This deliberately avoids classifying sessions from the
    child prompt text.
    """
    return """
        COALESCE(s.relation, '') != 'subagent'
        AND NOT EXISTS (
          SELECT 1
          FROM events bp, json_each(bp.raw, '$.message.content') AS block
          WHERE bp.event_id = s.branch_point_event_id
            AND json_extract(block.value, '$.type') = 'toolCall'
            AND json_extract(block.value, '$.name') = 'subagent'
            AND bp.event_id = 'pi:' || json_extract(bp.raw, '$.id') || ':' ||
                              json_extract(bp.raw, '$.timestamp') || ':' ||
                              json_extract(block.value, '$.id')
        )
    """


def _user_message_where(args):
    """Shared filters for user-intent browsing commands."""
    where = [
        "t.live = 1",
        "t.actor = 'user'",
        "t.type = 'message'",
        "COALESCE(t.text, '') <> ''",
        "t.text NOT LIKE '<local-command-%'",
        "t.text NOT LIKE '<command-name>%'",
        "t.text NOT LIKE '<task-notification>%'",
    ]
    params: list = []
    _append_visibility(where, args)
    _append_origin(where, args)  # default: clean human intent only
    if not getattr(args, "include_subagents", False):
        where.append(_not_subagent_session_sql())
    if not getattr(args, "include_inherited", False):
        where.append("t.inherited = 0")  # pi resume copies are context, not new user intent
    if getattr(args, "source", None):
        where.append("s.source = ?")
        params.append(args.source)
    if getattr(args, "cwd", None):
        where.append("COALESCE(s.cwd, '') LIKE ?")
        params.append(f"%{args.cwd}%")
    cutoff = _since_cutoff(getattr(args, "since", None))
    if cutoff:
        where.append(_timestamp_where("t.ts", ">="))
        params.append(cutoff)
    if getattr(args, "min_chars", 0):
        where.append("length(COALESCE(t.text, '')) >= ?")
        params.append(args.min_chars)
    if getattr(args, "max_chars", 0):
        where.append("length(COALESCE(t.text, '')) <= ?")
        params.append(args.max_chars)
    if getattr(args, "query", None):
        where.append("lower(t.text) LIKE ?")
        params.append(f"%{args.query.lower()}%")
    return where, params


def _userlog_rows(conn, args):
    """Recent live user-authored messages: the intent-first browsing primitive."""
    where, params = _user_message_where(args)
    params.append(args.limit)
    return conn.execute(
        f"""
        SELECT t.ts, s.source, t.session_id, t.seq, t.event_id, s.cwd, s.title,
               s.hidden_at, s.hidden_reason, eo.origin AS origin,
               t.text, length(COALESCE(t.text, '')) AS chars
        FROM transcript t
        JOIN sessions s ON s.session_id = t.session_id
        LEFT JOIN event_origins eo
               ON eo.session_id = t.session_id AND eo.event_id = t.event_id
        WHERE {' AND '.join(where)}
        ORDER BY t.ts DESC, t.session_id DESC, t.seq DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def _recent_rows(conn, args):
    """Sessions ranked by latest matching user message, not latest agent event."""
    where, params = _user_message_where(args)
    params.append(args.limit)
    return conn.execute(
        f"""
        WITH user_hits AS (
          SELECT t.session_id, t.ts, t.seq, t.event_id, t.text,
                 length(COALESCE(t.text, '')) AS chars, eo.origin AS origin
          FROM transcript t
          JOIN sessions s ON s.session_id = t.session_id
          LEFT JOIN event_origins eo
                 ON eo.session_id = t.session_id AND eo.event_id = t.event_id
          WHERE {' AND '.join(where)}
        ), ranked AS (
          SELECT user_hits.*,
                 COUNT(*) OVER (PARTITION BY session_id) AS user_msg_count,
                 ROW_NUMBER() OVER (
                   PARTITION BY session_id ORDER BY ts DESC, seq DESC
                 ) AS rn
          FROM user_hits
        ), live_counts AS (
          SELECT session_id, COUNT(*) AS live_event_count
          FROM session_events
          WHERE live = 1
          GROUP BY session_id
        )
        SELECT r.ts AS last_user_ts, s.source, r.session_id,
               r.seq AS last_user_seq, r.event_id AS last_user_event_id,
               s.cwd, s.title, r.text AS last_user_text, r.origin AS last_user_origin,
               s.hidden_at, s.hidden_reason,
               r.chars AS last_user_chars, r.user_msg_count,
               COALESCE(l.live_event_count, 0) AS live_event_count
        FROM ranked r
        JOIN sessions s ON s.session_id = r.session_id
        LEFT JOIN live_counts l ON l.session_id = r.session_id
        WHERE r.rn = 1
        ORDER BY r.ts DESC, r.session_id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def cmd_recent(args):
    conn = _open(args)
    rows = _recent_rows(conn, args)
    if args.json:
        out = [dict(r) for r in rows]
        json.dump(out, sys.stdout, ensure_ascii=False)
        print()
    else:
        for i, r in enumerate(rows):
            if i:
                print()
            sid = r["session_id"]
            seq = r["last_user_seq"]
            print(f"{(r['last_user_ts'] or '')[:19]}  {r['source']}  {_short_path(r['cwd'])}")
            print(f"session: {sid}")
            print(f"seq: {seq}  users: {r['user_msg_count']}  events: {r['live_event_count']}"
                  f"{_origin_label(r['last_user_origin'])}")
            if r["title"]:
                print(_wrapped("title: ", r["title"], 120))
            if r["hidden_at"]:
                print(f"hidden: {r['hidden_reason'] or r['hidden_at']}")
            print(_wrapped("user: ", r["last_user_text"], 0 if args.full else 260))
            print(f"expand: sessdb turns {sid} --around-seq {seq}")
    conn.close()


def cmd_userlog(args):
    conn = _open(args)
    rows = _userlog_rows(conn, args)
    if args.json:
        out = [dict(r) for r in rows]
        json.dump(out, sys.stdout, ensure_ascii=False)
        print()
    else:
        for i, r in enumerate(rows):
            if i:
                print()
            sid = r["session_id"]
            seq = r["seq"]
            print(f"{(r['ts'] or '')[:19]}  {r['source']}  {_short_path(r['cwd'])}")
            print(f"session: {sid}")
            print(f"seq: {seq}  chars: {r['chars']}{_origin_label(r['origin'])}")
            if r["hidden_at"]:
                print(f"hidden: {r['hidden_reason'] or r['hidden_at']}")
            print(_wrapped("user: ", r["text"], 0 if args.full else 320))
            print(f"expand: sessdb turns {sid} --around-seq {seq}")
    conn.close()


def _turn_rows(conn, session_id: str, include_all: bool):
    where = "WHERE t.session_id=?" + ("" if include_all else " AND t.live=1")
    return conn.execute(
        f"""
        SELECT t.seq, t.event_id, t.ts, t.actor, t.type, t.text, t.refs,
               t.live, t.inherited, eo.origin AS origin
        FROM transcript t
        LEFT JOIN event_origins eo
               ON eo.session_id = t.session_id AND eo.event_id = t.event_id
        {where}
        ORDER BY t.seq
        """,
        (session_id,),
    ).fetchall()


def _build_turns(rows):
    turns, cur = [], None

    def new_turn(user_row=None):
        seq = user_row["seq"] if user_row is not None else None
        return {
            "turn_index": len(turns),
            "seq_start": seq,
            "seq_end": seq,
            "ts": user_row["ts"] if user_row is not None else None,
            "user_seq": seq,
            "user_event_id": user_row["event_id"] if user_row is not None else None,
            "user_text": user_row["text"] if user_row is not None else None,
            "user_live": user_row["live"] if user_row is not None else None,
            "user_inherited": user_row["inherited"] if user_row is not None else None,
            "user_origin": user_row["origin"] if user_row is not None else None,
            "events": [],
        }

    for r in rows:
        if r["actor"] == "user" and r["type"] == "message" and _is_synthetic_user_text(r["text"]):
            continue
        is_user = r["actor"] == "user" and r["type"] == "message"
        if is_user:
            if cur is not None:
                turns.append(cur)
            cur = new_turn(r)
            continue
        if cur is None:
            cur = new_turn()
            cur["seq_start"] = r["seq"]
        cur["events"].append(dict(r))
        cur["seq_end"] = r["seq"]
    if cur is not None:
        turns.append(cur)
    for i, t in enumerate(turns):
        t["turn_index"] = i
    return turns


def _turn_window(turns, args):
    if args.around_seq is None:
        return turns[: args.limit] if args.limit else turns
    target = None
    for i, t in enumerate(turns):
        if t["seq_start"] is not None and t["seq_start"] <= args.around_seq <= t["seq_end"]:
            target = i
            break
    if target is None:
        for i, t in enumerate(turns):
            if t["seq_start"] is not None and t["seq_start"] >= args.around_seq:
                target = i
                break
    if target is None:
        target = max(0, len(turns) - 1)
    lo = max(0, target - args.context_turns)
    hi = min(len(turns), target + args.context_turns + 1)
    return turns[lo:hi]


def _event_preview(e, chars):
    return (e["text"] or "") if chars == 0 else _oneline(e["text"], chars)


def _placement_suffix(live, inherited):
    flags = []
    if live == 0:
        flags.append("rolled-back")
    if inherited:
        flags.append("inherited")
    return f" ({', '.join(flags)})" if flags else ""


def _turn_json(t, args):
    events, hidden = [], 0
    for e in t["events"]:
        is_tool = e["type"] in ("tool_call", "tool_result") or e["actor"] == "tool"
        if is_tool and not args.show_tools:
            hidden += 1
            continue
        chars = args.tool_chars if is_tool else args.agent_chars
        events.append({
            "seq": e["seq"], "event_id": e["event_id"], "ts": e["ts"],
            "actor": e["actor"], "type": e["type"], "live": e["live"],
            "inherited": e["inherited"], "preview": _event_preview(e, chars),
        })
    return {
        "turn_index": t["turn_index"], "seq_start": t["seq_start"], "seq_end": t["seq_end"],
        "ts": t["ts"], "user_seq": t["user_seq"], "user_event_id": t["user_event_id"],
        "user_text": t["user_text"], "user_live": t["user_live"],
        "user_inherited": t["user_inherited"], "user_origin": t["user_origin"],
        "hidden_tool_events": hidden,
        "events": events,
    }


def _print_turns(turns, args, indent=""):
    for t in turns:
        label = f"turn {t['turn_index']}  seq {t['seq_start']}..{t['seq_end']}"
        print(f"\n{indent}{label}")
        if t["user_seq"] is None:
            print(f"{indent}user: <none>")
        else:
            suffix = _placement_suffix(t["user_live"], t["user_inherited"])
            origin = _origin_label(t["user_origin"])
            print(_wrapped(f"{indent}user[{t['user_seq']}]{suffix}{origin}: ",
                           t["user_text"], args.user_chars))
        hidden = 0
        for e in t["events"]:
            is_tool = e["type"] in ("tool_call", "tool_result") or e["actor"] == "tool"
            if is_tool and not args.show_tools:
                hidden += 1
                continue
            chars = args.tool_chars if is_tool else args.agent_chars
            suffix = _placement_suffix(e["live"], e["inherited"])
            print(_wrapped(f"{indent}{e['actor']}/{e['type']}[{e['seq']}]{suffix}: ", e["text"], chars))
        if hidden:
            print(f"{indent}tools: {hidden} hidden (--show-tools)")


def cmd_turns(args):
    conn = _open(args)
    sid = _resolve_session(conn, args.session)
    if not sid:
        print(f"no session matching {args.session!r}", file=sys.stderr)
        sys.exit(1)
    rows = _turn_rows(conn, sid, args.all)
    turns = _turn_window(_build_turns(rows), args)
    if args.json:
        json.dump([_turn_json(t, args) for t in turns], sys.stdout, ensure_ascii=False)
        print()
    else:
        print(f"session: {sid}")
        _print_turns(turns, args)
    conn.close()


def cmd_show(args):
    conn = _open(args)
    sid = _resolve_session(conn, args.session)
    if not sid:
        print(f"no session matching {args.session!r}", file=sys.stderr)
        sys.exit(1)
    s = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    print(f"session {sid}")
    print(f"  cwd={s['cwd']}  started={s['started_at']}  title={s['title']}")
    where = "WHERE t.session_id=?" + ("" if args.all else " AND t.live=1")
    rows = conn.execute(
        f"""
        SELECT t.seq, t.ts, t.actor, t.type, t.text, t.refs, t.live, eo.origin AS origin
        FROM transcript t
        LEFT JOIN event_origins eo
               ON eo.session_id = t.session_id AND eo.event_id = t.event_id
        {where}
        ORDER BY t.seq
        """,
        (sid,),
    ).fetchall()
    glyph = {"message": " ", "tool_call": "⚙", "tool_result": "↩"}
    for r in rows:
        dead = "" if r["live"] else " (rolled-back)"
        origin = _origin_label(r["origin"])
        refs = json.loads(r["refs"] or "{}")
        extra = ""
        if refs.get("commands"):
            extra = f"  $ {_oneline(refs['commands'][0], 80)}"
        elif refs.get("files"):
            extra = f"  → {', '.join(refs['files'][:3])}"
        print(f"[{r['seq']:>4}] {r['actor']:9} {glyph.get(r['type'],' ')} "
              f"{_oneline(r['text'], 160)}{extra}{origin}{dead}")
    conn.close()


def _search_rows(conn, args):
    where = ["events_fts MATCH ?", "se.live = 1", "COALESCE(e.text, '') <> ''"]
    params: list = [args.query]
    _append_visibility(where, args)

    only_session = getattr(args, "only_session", None)
    if not getattr(args, "include_subagents", False):
        where.append(_not_subagent_session_sql())
    if not getattr(args, "include_inherited", False) and not only_session:
        where.append("se.inherited = 0")
    if getattr(args, "actor", None):
        where.append("e.actor = ?")
        params.append(args.actor)
        # Origin only means anything for user messages; --actor user defaults to
        # clean human intent.
        if args.actor == "user":
            _append_origin(where, args, "se.session_id", "e.event_id")
    if getattr(args, "type", None):
        where.append("e.type = ?")
        params.append(args.type)
    if getattr(args, "source", None):
        where.append("s.source = ?")
        params.append(args.source)
    if getattr(args, "cwd", None):
        where.append("COALESCE(s.cwd, '') LIKE ?")
        params.append(f"%{args.cwd}%")

    after = _since_cutoff(getattr(args, "after", None))
    if after:
        where.append(_timestamp_where("e.ts", ">="))
        params.append(after)
    before = _since_cutoff(getattr(args, "before", None))
    if before:
        where.append(_timestamp_where("e.ts", "<"))
        params.append(before)
    recent_cutoff = _since_cutoff(getattr(args, "exclude_recent", None))
    if recent_cutoff:
        where.append(_timestamp_where("e.ts", "<"))
        params.append(recent_cutoff)

    if only_session:
        sid = _resolve_session(conn, args.only_session)
        if sid:
            where.append("se.session_id = ?")
            params.append(sid)
        else:
            where.append("0")
    for ident in getattr(args, "exclude_session", None) or []:
        where.append("se.session_id != ?")
        params.append(_resolve_session(conn, ident) or ident)

    params.append(args.limit)
    return conn.execute(
        f"""
        SELECT e.event_id, e.actor, e.type, e.text, se.session_id, se.seq,
               se.inherited, e.ts, s.source, s.cwd, s.title,
               s.hidden_at, s.hidden_reason, eo.origin AS origin,
               snippet(events_fts, 0, '[', ']', '…', 12) AS snip
        FROM events_fts
        JOIN events e ON e.rowid = events_fts.rowid
        JOIN session_events se ON se.event_id = e.event_id
        JOIN sessions s ON s.session_id = se.session_id
        LEFT JOIN event_origins eo
               ON eo.session_id = se.session_id AND eo.event_id = e.event_id
        WHERE {' AND '.join(where)}
        ORDER BY rank, e.ts DESC, se.session_id DESC, se.seq DESC
        LIMIT ?
        """,
        params,
    ).fetchall()


def _search_row_json(r):
    return {
        "ts": r["ts"], "source": r["source"], "session_id": r["session_id"],
        "seq": r["seq"], "event_id": r["event_id"], "cwd": r["cwd"],
        "title": r["title"], "actor": r["actor"], "type": r["type"],
        "inherited": r["inherited"], "origin": r["origin"], "hidden_at": r["hidden_at"],
        "hidden_reason": r["hidden_reason"], "snippet": r["snip"], "text": r["text"],
        "expand_command": f"sessdb turns {r['session_id']} --around-seq {r['seq']}",
    }


def _search_turn_context(conn, r, args):
    window_args = argparse.Namespace(
        around_seq=r["seq"], context_turns=args.around, limit=0,
    )
    return _turn_window(_build_turns(_turn_rows(conn, r["session_id"], False)), window_args)


def _search_json(conn, rows, args):
    if args.around is None:
        return [_search_row_json(r) for r in rows]
    return [
        {
            "match": _search_row_json(r),
            "turns": [_turn_json(t, args) for t in _search_turn_context(conn, r, args)],
        }
        for r in rows
    ]


def cmd_search(args):
    conn = _open(args)
    if not has_fts5(conn):
        print("FTS5 unavailable in this sqlite build; use `sessdb grep` instead.", file=sys.stderr)
        sys.exit(2)
    rows = _search_rows(conn, args)
    if args.json:
        print(json.dumps(_search_json(conn, rows, args), indent=2))
    else:
        for r in rows:
            print(f"{(r['ts'] or '')[:19]}  {r['source']}  {_short_path(r['cwd'])}")
            print(f"session: {r['session_id']}")
            print(f"seq: {r['seq']}  event: {r['event_id']}  "
                  f"{r['actor']}/{r['type']}{_origin_label(r['origin'])}")
            if r["hidden_at"]:
                print(f"hidden: {r['hidden_reason'] or r['hidden_at']}")
            print(_wrapped("match: ", r["snip"], width=100))
            print(f"expand: sessdb turns {r['session_id']} --around-seq {r['seq']}")
            if args.around is not None:
                print("context:")
                _print_turns(_search_turn_context(conn, r, args), args, indent="  ")
            print()
    conn.close()


COMMIT_RE = re.compile(r"(?<![0-9A-Za-z])([0-9A-Fa-f]{7,40})(?![0-9A-Za-z])")
COMMIT_HAS_LETTER_RE = re.compile(r"[A-Fa-f]")


def _refs_list(value):
    return [x for x in value if isinstance(x, str) and x] if isinstance(value, list) else []


def _refs_json(value):
    try:
        refs = json.loads(value or "{}") if isinstance(value, str) else (value or {})
    except json.JSONDecodeError:
        refs = {}
    if not isinstance(refs, dict):
        refs = {}
    return {
        "files": _refs_list(refs.get("files")),
        "commands": _refs_list(refs.get("commands")),
    }


def _commit_tokens(text):
    out = []
    seen = set()
    for token in COMMIT_RE.findall(text or ""):
        if not COMMIT_HAS_LETTER_RE.search(token):
            continue
        token = token.lower()
        if token not in seen:
            out.append(token)
            seen.add(token)
    return out


def _refs_evidence(r, sid: str):
    return {
        "seq": r["seq"], "event_id": r["event_id"], "ts": r["ts"],
        "actor": r["actor"], "type": r["type"],
        "preview": _oneline(r["text"], 180),
        "expand_command": f"sessdb turns {sid} --around-seq {r['seq']}",
    }


def _refs_add(grouped, key, evidence):
    if not key:
        return
    item = grouped.setdefault(key, {"value": key, "events": []})
    seen = {(e["event_id"], e["seq"]) for e in item["events"]}
    ident = (evidence["event_id"], evidence["seq"])
    if ident not in seen:
        item["events"].append(evidence)


def _refs_rows(conn, sid: str, args):
    rows = _turn_rows(conn, sid, args.all)
    if args.around_seq is None:
        return rows, {"all": bool(args.all), "around_seq": None, "context_turns": None}
    window_args = argparse.Namespace(
        around_seq=args.around_seq, context_turns=args.context_turns, limit=0,
    )
    turns = _turn_window(_build_turns(rows), window_args)
    if not turns:
        return [], {"all": bool(args.all), "around_seq": args.around_seq,
                    "context_turns": args.context_turns, "seq_start": None, "seq_end": None}
    seqs = [x for t in turns for x in (t["seq_start"], t["seq_end"]) if x is not None]
    lo, hi = min(seqs), max(seqs)
    return [r for r in rows if lo <= r["seq"] <= hi], {
        "all": bool(args.all), "around_seq": args.around_seq,
        "context_turns": args.context_turns, "seq_start": lo, "seq_end": hi,
    }


def _refs_model(conn, sid: str, args):
    session = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    rows, scope = _refs_rows(conn, sid, args)
    files, commands, commits = {}, {}, {}
    for r in rows:
        evidence = _refs_evidence(r, sid)
        refs = _refs_json(r["refs"])
        for file in refs["files"]:
            _refs_add(files, file, evidence)
        for command in refs["commands"]:
            _refs_add(commands, command, evidence)
            for commit in _commit_tokens(command):
                _refs_add(commits, commit, evidence)
        for commit in _commit_tokens(r["text"]):
            _refs_add(commits, commit, evidence)
    return {
        "session": dict(session) if session else {"session_id": sid},
        "scope": scope,
        "files": sorted(files.values(), key=lambda x: x["value"]),
        "commands": sorted(commands.values(), key=lambda x: x["events"][0]["seq"]),
        "commits": sorted(commits.values(), key=lambda x: x["events"][0]["seq"]),
    }


def _git_show_file_hints(files, commit_item):
    commit_events = {e["event_id"] for e in commit_item.get("events", [])}
    hints = []
    for file in files or []:
        value = file["value"]
        if value.startswith(("/", "~")):
            continue
        file_events = {e["event_id"] for e in file.get("events", [])}
        if commit_events & file_events:
            hints.append(value)
    return hints


def _print_refs_group(name, items, *, command_hints=False, git_files=None):
    print(f"\n{name}:")
    if not items:
        print("  <none>")
        return
    for item in items:
        value = item["value"]
        print(_wrapped("  - ", value, 220))
        if command_hints:
            print(f"    git show {shlex.quote(value)}")
            for file in _git_show_file_hints(git_files, item)[:3]:
                print(f"    git show {shlex.quote(f'{value}:{file}')}")
        for e in item["events"][:5]:
            print(f"    seq {e['seq']}  {e['actor']}/{e['type']}  {e['event_id']}")
            if e["preview"]:
                print(_wrapped("      ", e["preview"], 180))
            print(f"      expand: {e['expand_command']}")
        if len(item["events"]) > 5:
            print(f"      ... {len(item['events']) - 5} more event(s)")


def cmd_refs(args):
    conn = _open(args)
    sid = _resolve_session(conn, args.session)
    if not sid:
        print(f"no session matching {args.session!r}", file=sys.stderr)
        sys.exit(1)
    model = _refs_model(conn, sid, args)
    if args.json:
        print(json.dumps(model, indent=2))
    else:
        s = model["session"]
        print(f"session: {sid}")
        print(f"source: {s.get('source')}  cwd: {_short_path(s.get('cwd'))}")
        scope = model["scope"]
        if scope.get("around_seq") is not None:
            print(f"scope: seq {scope.get('seq_start')}..{scope.get('seq_end')} "
                  f"around {scope['around_seq']} ±{scope['context_turns']} turn(s)")
        else:
            print("scope: " + ("all placements" if scope.get("all") else "live placements"))
        _print_refs_group("files", model["files"])
        _print_refs_group("commands", model["commands"])
        _print_refs_group("commits", model["commits"], command_hints=True, git_files=model["files"])
    conn.close()


def _path_norm(value):
    s = str(value or "").strip().replace("\\", "/")
    if s == "~" or s.startswith("~/"):
        s = str(Path.home()).replace("\\", "/") + s[1:]
    while s.startswith("./"):
        s = s[2:]
    return s


def _path_is_absolute_or_home(value):
    return _path_norm(value).startswith("/")


def _path_basename(value):
    value = _path_norm(value).rstrip("/")
    return value.rsplit("/", 1)[-1] if value else ""


def _path_with_cwd(value, cwd):
    v = _path_norm(value).strip("/")
    if not v:
        return ""
    if _path_is_absolute_or_home(value):
        return _path_norm(value).rstrip("/")
    c = _path_norm(cwd).rstrip("/")
    return f"{c}/{v}" if c else v


def _path_prefix_match(file_value, query, cwd=None):
    f = _path_norm(file_value).rstrip("/")
    q = _path_norm(query).rstrip("/")
    if not f or not q:
        return False
    if f == q or f.startswith(q + "/"):
        return True
    if cwd:
        af = _path_with_cwd(file_value, cwd)
        aq = _path_with_cwd(query, cwd)
        return af == aq or af.startswith(aq + "/")
    return False


def _path_exact_or_suffix_match(file_value, query, cwd=None):
    f = _path_norm(file_value).rstrip("/")
    q = _path_norm(query).rstrip("/")
    if not f or not q:
        return False
    if f == q:
        return True
    if cwd and _path_with_cwd(file_value, cwd) == _path_with_cwd(query, cwd):
        return True
    if not _path_is_absolute_or_home(query) and "/" in q:
        return f.endswith("/" + q)
    return False


def _touched_file_matches(file_value, cwd, args):
    if args.basename:
        return _path_basename(file_value) == _path_basename(args.path)
    if args.prefix:
        return _path_prefix_match(file_value, args.path, cwd)
    return _path_exact_or_suffix_match(file_value, args.path, cwd)


def _touched_mode(args):
    if args.basename:
        return "basename"
    if args.prefix:
        return "prefix"
    return "path"


def _nearest_user(conn, sid: str, seq: int, include_all: bool):
    live_clause = "" if include_all else "AND live=1"
    rows = conn.execute(
        f"""
        SELECT seq, event_id, ts, text, live, inherited
        FROM transcript
        WHERE session_id=? {live_clause}
          AND actor='user' AND type='message' AND seq <= ?
        ORDER BY seq DESC
        LIMIT 5
        """,
        (sid, seq),
    ).fetchall()
    for r in rows:
        if _is_synthetic_user_text(r["text"]):
            continue
        return {
            "seq": r["seq"], "event_id": r["event_id"], "ts": r["ts"],
            "text": r["text"], "preview": _oneline(r["text"], 240),
            "live": r["live"], "inherited": r["inherited"],
        }
    return None


def _touched_expand_command(r):
    suffix = " --all" if r["live"] == 0 else ""
    return f"sessdb turns {r['session_id']} --around-seq {r['seq']}{suffix}"


def _touched_refs_command(r):
    suffix = " --all" if r["live"] == 0 else ""
    return f"sessdb refs {r['session_id']} --around-seq {r['seq']} --context-turns 0{suffix}"


def _touched_row_json(conn, r, file_value, args):
    return {
        "ts": r["ts"], "source": r["source"], "session_id": r["session_id"],
        "seq": r["seq"], "event_id": r["event_id"], "cwd": r["cwd"],
        "title": r["title"], "actor": r["actor"], "type": r["type"],
        "live": r["live"], "inherited": r["inherited"],
        "hidden_at": r["hidden_at"], "hidden_reason": r["hidden_reason"],
        "file": file_value,
        "preview": _oneline(r["text"], 240),
        "nearest_user": _nearest_user(conn, r["session_id"], r["seq"], args.all),
        "expand_command": _touched_expand_command(r),
        "refs_command": _touched_refs_command(r),
    }


def _touched_candidates(conn, args):
    where = ["json_valid(t.refs)", "COALESCE(json_array_length(t.refs, '$.files'), 0) > 0"]
    params: list = []
    _append_visibility(where, args)
    if not args.all:
        where.append("t.live = 1")
    only_session = getattr(args, "only_session", None)
    if not getattr(args, "include_subagents", False):
        where.append(_not_subagent_session_sql())
    if not getattr(args, "include_inherited", False) and not only_session:
        where.append("t.inherited = 0")
    if getattr(args, "source", None):
        where.append("s.source = ?")
        params.append(args.source)
    if getattr(args, "cwd", None):
        where.append("COALESCE(s.cwd, '') LIKE ?")
        params.append(f"%{args.cwd}%")
    after = _since_cutoff(getattr(args, "after", None))
    if after:
        where.append(_timestamp_where("t.ts", ">="))
        params.append(after)
    before = _since_cutoff(getattr(args, "before", None))
    if before:
        where.append(_timestamp_where("t.ts", "<"))
        params.append(before)
    recent_cutoff = _since_cutoff(getattr(args, "exclude_recent", None))
    if recent_cutoff:
        where.append(_timestamp_where("t.ts", "<"))
        params.append(recent_cutoff)
    if only_session:
        sid = _resolve_session(conn, args.only_session)
        if sid:
            where.append("t.session_id = ?")
            params.append(sid)
        else:
            where.append("0")
    for ident in getattr(args, "exclude_session", None) or []:
        where.append("t.session_id != ?")
        params.append(_resolve_session(conn, ident) or ident)
    return conn.execute(
        f"""
        SELECT t.ts, s.source, t.session_id, t.seq, t.event_id, s.cwd, s.title,
               s.hidden_at, s.hidden_reason,
               t.actor, t.type, t.text, t.refs, t.live, t.inherited
        FROM transcript t
        JOIN sessions s ON s.session_id = t.session_id
        WHERE {' AND '.join(where)}
        ORDER BY julianday(t.ts) DESC, t.session_id DESC, t.seq DESC
        """,
        params,
    ).fetchall()


def _touched_matches(conn, args):
    out = []
    limit = max(0, args.limit)
    for r in _touched_candidates(conn, args):
        refs = _refs_json(r["refs"])
        for file_value in refs["files"]:
            if not _touched_file_matches(file_value, r["cwd"], args):
                continue
            out.append(_touched_row_json(conn, r, file_value, args))
            if limit and len(out) >= limit:
                return out
    return out


def _touched_query_json(args):
    return {
        "path": args.path, "mode": _touched_mode(args), "limit": args.limit,
        "source": args.source, "cwd": args.cwd, "after": args.after,
        "before": args.before, "exclude_session": args.exclude_session,
        "only_session": args.only_session, "exclude_recent": args.exclude_recent,
        "all": bool(args.all), "include_inherited": bool(args.include_inherited),
        "include_subagents": bool(args.include_subagents),
        "include_hidden": bool(args.include_hidden),
        "only_hidden": bool(args.only_hidden),
    }


def cmd_touched(args):
    conn = _open(args)
    matches = _touched_matches(conn, args)
    if args.json:
        print(json.dumps({"query": _touched_query_json(args), "matches": matches}, indent=2))
    else:
        print(f"path: {args.path}  mode: {_touched_mode(args)}")
        if not matches:
            print("<no structured file refs>")
        for i, r in enumerate(matches):
            if i:
                print()
            print(f"{(r['ts'] or '')[:19]}  {r['source']}  {_short_path(r['cwd'])}")
            print(f"session: {r['session_id']}")
            suffix = _placement_suffix(r["live"], r["inherited"])
            print(f"seq: {r['seq']}  event: {r['event_id']}  {r['actor']}/{r['type']}{suffix}")
            if r["hidden_at"]:
                print(f"hidden: {r['hidden_reason'] or r['hidden_at']}")
            print(_wrapped("file: ", r["file"], 180))
            if r["nearest_user"]:
                u = r["nearest_user"]
                usuffix = _placement_suffix(u["live"], u["inherited"])
                print(_wrapped(f"nearest_user[{u['seq']}]{usuffix}: ", u["text"], 260))
            if r["preview"]:
                print(_wrapped("event: ", r["preview"], 220))
            print(f"expand: {r['expand_command']}")
            print(f"refs: {r['refs_command']}")
    conn.close()


def _latest_user_for_session(conn, sid: str):
    rows = conn.execute(
        """
        SELECT ts, seq, event_id, text, inherited
        FROM transcript
        WHERE session_id=? AND live=1 AND actor='user' AND type='message'
          AND COALESCE(text, '') <> ''
        ORDER BY julianday(ts) DESC, seq DESC
        LIMIT 20
        """,
        (sid,),
    ).fetchall()
    for r in rows:
        if not _is_synthetic_user_text(r["text"]):
            return dict(r)
    return None


def _tool_name_for_event_raw(conn, event_id: str | None):
    if not event_id:
        return None
    row = conn.execute("SELECT raw FROM events WHERE event_id=?", (event_id,)).fetchone()
    if row is None:
        return None
    try:
        raw = json.loads(row["raw"] or "{}")
    except json.JSONDecodeError:
        return None
    content = ((raw.get("message") or {}).get("content") or [])
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "toolCall":
            continue
        bid = block.get("id")
        rid = raw.get("id")
        ts = raw.get("timestamp")
        expected = f"pi:{rid}:{ts}:{bid}" if rid and ts and bid else None
        if expected == event_id or (bid and event_id.endswith(f":{bid}")):
            return block.get("name")
    return None


def _lineage_summary(conn, sid: str):
    row = conn.execute("SELECT * FROM sessions WHERE session_id=?", (sid,)).fetchone()
    if row is None:
        return {"session_id": sid, "missing": True}
    out = dict(row)
    out["missing"] = False
    out["stored_relation"] = out.get("relation")
    if out.get("relation") != "subagent" and _tool_name_for_event_raw(conn, out.get("branch_point_event_id")) == "subagent":
        out["relation"] = "subagent"
        out["spawn_event_id"] = out.get("spawn_event_id") or out.get("branch_point_event_id")
    latest = _latest_user_for_session(conn, sid)
    if latest:
        latest["expand_command"] = f"sessdb turns {sid} --around-seq {latest['seq']}"
    out["last_user"] = latest
    out["expand_command"] = f"sessdb turns {sid}"
    return out


def _lineage_ancestors(conn, sid: str):
    ids, seen, cur = [], set(), sid
    while cur and cur not in seen and len(ids) < 100:
        ids.append(cur)
        seen.add(cur)
        row = conn.execute(
            "SELECT parent_session_id FROM sessions WHERE session_id=?", (cur,)
        ).fetchone()
        if row is None or not row["parent_session_id"]:
            break
        cur = row["parent_session_id"]
    return ids


def _related_session_ids(conn, where_sql: str, params: list, limit: int):
    total = conn.execute(f"SELECT COUNT(*) FROM sessions WHERE {where_sql}", params).fetchone()[0]
    ids = [
        r["session_id"] for r in conn.execute(
            f"""
            SELECT session_id
            FROM sessions
            WHERE {where_sql}
            ORDER BY COALESCE(julianday(created_at), julianday(started_at), julianday(ended_at), 0),
                     session_id
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    ]
    return ids, total


def _lineage_model(conn, sid: str, limit: int):
    ancestor_ids = _lineage_ancestors(conn, sid)
    root_id = ancestor_ids[-1] if ancestor_ids else sid
    current = _lineage_summary(conn, sid)
    parent_id = current.get("parent_session_id") if not current.get("missing") else None
    children_ids, children_total = _related_session_ids(
        conn, "parent_session_id=?", [sid], limit
    )
    if parent_id:
        sibling_ids, siblings_total = _related_session_ids(
            conn, "parent_session_id=? AND session_id != ?", [parent_id, sid], limit
        )
    else:
        sibling_ids, siblings_total = [], 0
    return {
        "root": _lineage_summary(conn, root_id),
        "parent": _lineage_summary(conn, parent_id) if parent_id else None,
        "current": current,
        "ancestors": [_lineage_summary(conn, x) for x in reversed(ancestor_ids)],
        "children": [_lineage_summary(conn, x) for x in children_ids],
        "children_total": children_total,
        "siblings": [_lineage_summary(conn, x) for x in sibling_ids],
        "siblings_total": siblings_total,
    }


def _print_lineage_summary(summary, *, indent="  "):
    sid = summary["session_id"]
    if summary.get("missing"):
        print(f"{indent}{sid}  (not in db)")
        return
    created = summary.get("created_at")
    started = summary.get("started_at")
    ended = summary.get("ended_at")
    bits = [sid, summary.get("source") or "?", _short_path(summary.get("cwd"))]
    if summary.get("relation"):
        bits.append(f"relation={summary['relation']}")
    if created:
        bits.append(f"created={created[:19]}")
    elif started:
        bits.append(f"started={started[:19]}")
    elif ended:
        bits.append(f"ended={ended[:19]}")
    print(f"{indent}" + "  ".join(bits))
    if summary.get("parent_session_id"):
        print(f"{indent}parent: {summary['parent_session_id']}")
    if summary.get("branch_point_event_id"):
        print(f"{indent}branch_point: {summary['branch_point_event_id']}")
    if summary.get("spawn_event_id"):
        print(f"{indent}spawn: {summary['spawn_event_id']}")
    latest = summary.get("last_user")
    if latest:
        print(_wrapped(f"{indent}last_user[{latest['seq']}]: ", latest["text"], 180))
        print(f"{indent}expand: {latest['expand_command']}")
    else:
        print(f"{indent}expand: {summary['expand_command']}")


def _print_lineage_section(name, items, total=None):
    print(f"\n{name}:")
    if items is None:
        print("  <none>")
        return
    if isinstance(items, dict):
        _print_lineage_summary(items)
        return
    if not items:
        print("  <none>")
        return
    for item in items:
        _print_lineage_summary(item)
    if total is not None and total > len(items):
        print(f"  ... showing {len(items)} of {total}; use --limit to show more")


def cmd_lineage(args):
    conn = _open(args)
    sid = _resolve_session(conn, args.session)
    if not sid:
        print(f"no session matching {args.session!r}", file=sys.stderr)
        sys.exit(1)
    model = _lineage_model(conn, sid, args.limit)
    if args.json:
        print(json.dumps(model, indent=2))
    else:
        path = " -> ".join(x["session_id"] for x in model["ancestors"])
        print(f"lineage: {path}")
        _print_lineage_section("root", model["root"])
        _print_lineage_section("parent", model["parent"])
        _print_lineage_section("current", model["current"])
        _print_lineage_section("children", model["children"], model["children_total"])
        _print_lineage_section("siblings", model["siblings"], model["siblings_total"])
    conn.close()


def cmd_grep(args):
    rg = shutil.which("rg")
    paths = args.paths if args.paths else _default_grep_roots()
    cmd = _grep_command(args.pattern, paths, rg)
    if cmd is None:
        sys.exit(1)
    sys.exit(subprocess.call(cmd))


def _default_grep_roots():
    roots = [DEFAULT_CLAUDE_ROOT, DEFAULT_CODEX_ROOT, DEFAULT_PI_ROOT]
    roots.extend(
        root for _, _, root in discover_pool_roots(
            Path(DEFAULT_POOL),
            include_local=False,
            local_machines=local_machine_names(),
        )
    )
    seen = set()
    existing = []
    for root in roots:
        path = Path(root)
        if not path.exists():
            continue
        value = str(path)
        if value in seen:
            continue
        seen.add(value)
        existing.append(value)
    return existing


def _grep_command(pattern: str, paths: list[str], rg: str | None):
    paths = [p for p in paths if "file-history" not in Path(p).parts]
    if not paths:
        return None
    if rg:
        return [rg, "--glob", "!**/file-history/**", "--", pattern, *paths]
    return ["grep", "-rn", "--exclude-dir=file-history", "--", pattern, *paths]


def cmd_bmux_sync(args):
    # Skip the automatic default-log hook in _open: this command rebuilds the
    # overlay explicitly (and possibly against a custom --log), so the hook would
    # be redundant or fight a non-default path.
    conn = _open(args, sync_bmux=False)
    path = Path(args.log) if args.log else bmux._default_log()
    stats = bmux.sync(conn, path, force=True)
    print("bmux provenance: " + ", ".join(f"{k}={v}" for k, v in stats.items()))
    if not path.exists():
        print(f"(no bmux event log at {path})", file=sys.stderr)
    conn.close()


def cmd_schema(args):
    from codebrain.db import SCHEMA
    print(SCHEMA)


def main(argv=None):
    p = argparse.ArgumentParser(prog="sessdb", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(DEFAULT_DB), help=f"SQLite path (default {DEFAULT_DB})")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("ingest", help="build/update the DB from raw logs")
    sp.add_argument("--source", choices=("all",) + SOURCES, default="all",
                    help="which source(s) to ingest (default all)")
    sp.add_argument("--machine", default=None, help="override hostname tag")
    sp.add_argument("--raw-root",
                    help="override one source root; requires --source claude|codex|pi")
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("collect", help="mirror raw logs into the append-only pool")
    sp.add_argument("--pool", default=str(DEFAULT_POOL), help=f"pool root (default {DEFAULT_POOL})")
    sp.add_argument("--source", choices=("all",) + SOURCES, default="all",
                    help="which source(s) to collect (default all)")
    sp.add_argument("--machine", default=None, help="override hostname subtree")
    sp.add_argument("--install-launchd", action="store_true",
                    help="install a LaunchAgent that sweeps periodically (macOS)")
    sp.add_argument("--interval", type=int, default=1800,
                    help="LaunchAgent sweep period in seconds (default 1800)")
    sp.set_defaults(func=cmd_collect)

    sp = sub.add_parser("ingest-pool", help="debug/repair ingest of synced pool subtrees")
    sp.add_argument("--pool", default=str(DEFAULT_POOL), help=f"pool root (default {DEFAULT_POOL})")
    sp.add_argument("--source", choices=("all",) + SOURCES, default="all",
                    help="which source(s) to ingest from the pool (default all)")
    sp.add_argument("--machine", help="only ingest one pool raw/<machine> subtree")
    sp.add_argument("--include-local", action="store_true",
                    help="also ingest local pool subtree, then reparse local live homes")
    sp.set_defaults(func=cmd_ingest_pool)

    sp = sub.add_parser("backfill-claude",
                        help="one-shot import of historical Claude backup zips into the pool")
    sp.add_argument("paths", nargs="+",
                    help="zip file(s), or directories containing .zip backups")
    sp.add_argument("--pool", default=str(DEFAULT_POOL), help=f"pool root (default {DEFAULT_POOL})")
    sp.add_argument("--origin", default=DEFAULT_ORIGIN,
                    help=f"pool raw/<origin>/claude subtree (default {DEFAULT_ORIGIN})")
    sp.add_argument("--manifest", help="write manifest to this path instead of the pool subtree")
    sp.add_argument("--dry-run", action="store_true",
                    help="scan and select, but do not write pool files or manifest")
    sp.add_argument("--json", action="store_true", help="emit the manifest JSON to stdout")
    sp.set_defaults(func=cmd_backfill_claude)

    sp = sub.add_parser("hide", help="hide sessions from default retrieval")
    sp.add_argument("sessions", nargs="+", help="session id or unique prefix")
    sp.add_argument("--reason", required=True, help="why this session is hidden")
    sp.add_argument("--no-refresh", action="store_true", help="skip the delta ingest")
    sp.set_defaults(func=cmd_hide)

    sp = sub.add_parser("unhide", help="restore sessions to default retrieval")
    sp.add_argument("sessions", nargs="+", help="session id or unique prefix")
    sp.add_argument("--no-refresh", action="store_true", help="skip the delta ingest")
    sp.set_defaults(func=cmd_unhide)

    sp = sub.add_parser("hidden", help="list sessions hidden from default retrieval")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--source", choices=SOURCES, help="filter by source")
    sp.add_argument("--cwd", help="substring filter on session cwd")
    sp.add_argument("--json", action="store_true", help="emit a JSON array")
    sp.add_argument("--no-refresh", action="store_true", help="skip the delta ingest")
    sp.set_defaults(func=cmd_hidden)

    sp = sub.add_parser("list", help="recent sessions")
    sp.add_argument("--limit", type=int, default=30)
    _add_visibility_args(sp)
    sp.add_argument("--no-refresh", action="store_true", help="skip the delta ingest")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("recent", help="sessions by latest live user message")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--since", help="timestamp/date cutoff, or relative duration like 7d/12h")
    sp.add_argument("--source", choices=SOURCES, help="filter by source")
    sp.add_argument("--cwd", help="substring filter on session cwd")
    sp.add_argument("--min-chars", type=int, default=0, help="minimum latest-considered user length")
    sp.add_argument("--max-chars", type=int, default=0, help="maximum latest-considered user length (0 = no cap)")
    sp.add_argument("--include-inherited", action="store_true",
                    help="include pi resume/branch copies (default: authored messages only)")
    sp.add_argument("--include-subagents", action="store_true",
                    help="include sessions spawned by subagent tool calls")
    sp.add_argument("--full", action="store_true", help="do not truncate last user preview")
    sp.add_argument("--json", action="store_true", help="emit a JSON array")
    _add_visibility_args(sp)
    _add_origin_args(sp)
    sp.add_argument("--no-refresh", action="store_true", help="skip the delta ingest")
    sp.set_defaults(func=cmd_recent)

    sp = sub.add_parser("userlog", help="recent live user messages (intent-first)")
    sp.add_argument("--limit", type=int, default=50)
    sp.add_argument("--since", help="timestamp/date cutoff, or relative duration like 7d/12h")
    sp.add_argument("--source", choices=SOURCES, help="filter by source")
    sp.add_argument("--cwd", help="substring filter on session cwd")
    sp.add_argument("--query", help="case-insensitive substring filter on user text")
    sp.add_argument("--min-chars", type=int, default=0, help="minimum message length")
    sp.add_argument("--max-chars", type=int, default=0, help="maximum message length (0 = no cap)")
    sp.add_argument("--include-inherited", action="store_true",
                    help="include pi resume/branch copies (default: authored messages only)")
    sp.add_argument("--include-subagents", action="store_true",
                    help="include sessions spawned by subagent tool calls")
    sp.add_argument("--full", action="store_true", help="do not truncate message text")
    sp.add_argument("--json", action="store_true", help="emit a JSON array")
    _add_visibility_args(sp)
    _add_origin_args(sp)
    sp.add_argument("--no-refresh", action="store_true", help="skip the delta ingest")
    sp.set_defaults(func=cmd_userlog)

    sp = sub.add_parser("turns", help="display a session as user-centered turns")
    sp.add_argument("session")
    sp.add_argument("--around-seq", type=int, help="show turns around this transcript seq")
    sp.add_argument("--context-turns", type=int, default=2,
                    help="turns before/after --around-seq (default 2)")
    sp.add_argument("--limit", type=int, default=50,
                    help="max turns without --around-seq; 0 means all (default 50)")
    sp.add_argument("--user-chars", type=int, default=500,
                    help="user preview chars; 0 means no cap (default 500)")
    sp.add_argument("--agent-chars", type=int, default=300,
                    help="assistant preview chars; 0 means no cap (default 300)")
    sp.add_argument("--tool-chars", type=int, default=80,
                    help="tool preview chars with --show-tools; 0 means no cap (default 80)")
    sp.add_argument("--show-tools", action="store_true", help="include tool calls/results")
    sp.add_argument("--all", action="store_true", help="include rolled-back events")
    sp.add_argument("--json", action="store_true", help="emit a JSON array")
    sp.add_argument("--no-refresh", action="store_true", help="skip the delta ingest")
    sp.set_defaults(func=cmd_turns)

    sp = sub.add_parser("show", help="a session's transcript")
    sp.add_argument("session")
    sp.add_argument("--all", action="store_true", help="include rolled-back events")
    sp.add_argument("--no-refresh", action="store_true", help="skip the delta ingest")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("search", help="FTS over event text")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=20)
    sp.add_argument("--around", type=int,
                    help="inline N user-centered turns before/after each hit")
    sp.add_argument("--user-chars", type=int, default=500,
                    help="user preview chars with --around; 0 means no cap (default 500)")
    sp.add_argument("--agent-chars", type=int, default=300,
                    help="assistant preview chars with --around; 0 means no cap (default 300)")
    sp.add_argument("--tool-chars", type=int, default=80,
                    help="tool preview chars with --around --show-tools; 0 means no cap (default 80)")
    sp.add_argument("--show-tools", action="store_true",
                    help="include tool calls/results in --around context")
    sp.add_argument("--actor", choices=("user", "assistant", "tool"), help="filter by event actor")
    sp.add_argument("--type", choices=("message", "tool_call", "tool_result"), help="filter by event type")
    sp.add_argument("--source", choices=SOURCES, help="filter by source")
    sp.add_argument("--cwd", help="substring filter on session cwd")
    sp.add_argument("--after", help="event timestamp lower bound, or relative duration like 7d/12h")
    sp.add_argument("--before", help="event timestamp upper bound, or relative duration like 7d/12h")
    sp.add_argument("--exclude-session", action="append", default=[],
                    help="exclude a session id/prefix; repeatable")
    sp.add_argument("--only-session", help="only search one session id/prefix, including inherited live context")
    sp.add_argument("--exclude-recent", help="exclude events newer than a relative duration like 1h")
    sp.add_argument("--include-inherited", action="store_true",
                    help="include pi resume/branch copies (default: authored events only)")
    sp.add_argument("--include-subagents", action="store_true",
                    help="include sessions spawned by subagent tool calls")
    sp.add_argument("--json", action="store_true", help="emit a JSON array")
    _add_visibility_args(sp)
    _add_origin_args(sp)  # applied only with --actor user
    sp.add_argument("--no-refresh", action="store_true", help="skip the delta ingest")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("lineage", help="factual parent/child session lineage")
    sp.add_argument("session")
    sp.add_argument("--limit", type=int, default=50,
                    help="maximum children/siblings to show (default 50)")
    sp.add_argument("--json", action="store_true", help="emit a JSON object")
    sp.add_argument("--no-refresh", action="store_true", help="skip the delta ingest")
    sp.set_defaults(func=cmd_lineage)

    sp = sub.add_parser("refs", help="files/commands/commits referenced by a session")
    sp.add_argument("session")
    sp.add_argument("--around-seq", type=int, help="only include refs in turns around this seq")
    sp.add_argument("--context-turns", type=int, default=2,
                    help="turns before/after --around-seq (default 2)")
    sp.add_argument("--all", action="store_true", help="include rolled-back events")
    sp.add_argument("--json", action="store_true", help="emit a JSON object")
    sp.add_argument("--no-refresh", action="store_true", help="skip the delta ingest")
    sp.set_defaults(func=cmd_refs)

    sp = sub.add_parser("touched", help="sessions/events with structured file refs")
    sp.add_argument("path")
    mode = sp.add_mutually_exclusive_group()
    mode.add_argument("--basename", action="store_true", help="match by basename only")
    mode.add_argument("--prefix", action="store_true", help="match a directory/path prefix")
    sp.add_argument("--limit", type=int, default=50,
                    help="maximum matches; 0 means all (default 50)")
    sp.add_argument("--source", choices=SOURCES, help="filter by source")
    sp.add_argument("--cwd", help="substring filter on session cwd")
    sp.add_argument("--after", help="event timestamp lower bound, or relative duration like 7d/12h")
    sp.add_argument("--before", help="event timestamp upper bound, or relative duration like 7d/12h")
    sp.add_argument("--exclude-session", action="append", default=[],
                    help="exclude a session id/prefix; repeatable")
    sp.add_argument("--only-session", help="only scan one session id/prefix, including inherited live context")
    sp.add_argument("--exclude-recent", help="exclude events newer than a relative duration like 1h")
    sp.add_argument("--include-inherited", action="store_true",
                    help="include pi resume/branch copies (default: authored events only)")
    sp.add_argument("--include-subagents", action="store_true",
                    help="include sessions spawned by subagent tool calls")
    sp.add_argument("--all", action="store_true", help="include rolled-back events")
    sp.add_argument("--json", action="store_true", help="emit a JSON object")
    _add_visibility_args(sp)
    sp.add_argument("--no-refresh", action="store_true", help="skip the delta ingest")
    sp.set_defaults(func=cmd_touched)

    sp = sub.add_parser(
        "grep",
        help="ripgrep over live local logs and synced remote pool logs by default",
    )
    sp.add_argument("pattern")
    sp.add_argument(
        "paths",
        nargs="*",
        help="optional raw-log paths; when provided, replace the default search scope",
    )
    sp.set_defaults(func=cmd_grep)

    sp = sub.add_parser("bmux-sync",
                        help="rebuild the bmux provenance overlay from the bmux event log")
    sp.add_argument("--log", help=f"bmux event log path (default {bmux.DEFAULT_BMUX_LOG})")
    sp.add_argument("--no-refresh", action="store_true", help="skip the delta ingest")
    sp.set_defaults(func=cmd_bmux_sync)

    sp = sub.add_parser("schema", help="print the DDL")
    sp.set_defaults(func=cmd_schema)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

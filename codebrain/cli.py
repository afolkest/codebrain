"""sessdb — thin CLI over the codebrain SQLite cache.

  sessdb ingest [--source all]         full build/rebuild of the local DB
  sessdb collect [--install-launchd]   mirror raw logs into the append-only pool
  sessdb list [--limit N]              recent sessions
  sessdb recent [--limit N]            sessions by latest user activity
  sessdb userlog [--limit N]           recent user messages (intent-first)
  sessdb turns <session>               user-centered turns with truncated agent context
  sessdb show <session> [--all]        a session's transcript (live by default)
  sessdb search <query> [--around N]   FTS over filtered event text
  sessdb grep <pattern> [paths...]     ripgrep over the raw logs

Read commands refresh first: changed/new raw files are delta-ingested before the
query runs (ms when nothing changed), so results are always current for this
machine — including sessions that are live right now. --no-refresh skips it.

Raw SQL escape hatch: just open the DB with any sqlite3 client (see --schema).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

from codebrain.collect import DEFAULT_POOL, LAUNCHD_LABEL, collect_all, install_launchd
from codebrain.db import DEFAULT_DB, connect, has_fts5
from codebrain.ingest import (
    DEFAULT_CLAUDE_ROOT, DEFAULT_CODEX_ROOT, DEFAULT_PI_ROOT, SOURCES, ingest_all, refresh,
)


def _open(args):
    """Connect and (unless --no-refresh) delta-ingest whatever changed on disk."""
    conn = connect(args.db)
    if not getattr(args, "no_refresh", False):
        stats = refresh(conn)
        if stats["files"]:
            print(f"(refreshed {stats['files']} changed file(s), +{stats['events']} events)",
                  file=sys.stderr)
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


def _timestamp_where(column: str, op: str) -> str:
    return f"julianday({column}) {op} julianday(?)"


def cmd_ingest(args):
    conn = connect(args.db)
    sources = SOURCES if args.source == "all" else (args.source,)
    print(f"ingesting [{', '.join(sources)}] → {args.db}")
    total = ingest_all(conn, sources=sources, machine=args.machine)
    conn.close()
    print("done: " + ", ".join(f"{k}={v}" for k, v in total.items()))


def cmd_collect(args):
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
    print("done: " + ", ".join(f"{k}={v}" for k, v in total.items()))


def cmd_list(args):
    conn = _open(args)
    rows = conn.execute(
        """
        SELECT s.session_id, s.source, s.started_at, s.cwd, s.title,
               COUNT(se.event_id) AS n
        FROM sessions s
        LEFT JOIN session_events se ON se.session_id = s.session_id AND se.live = 1
        GROUP BY s.session_id
        ORDER BY s.started_at DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    for r in rows:
        started = (r["started_at"] or "")[:19]
        print(f"{started:19}  {r['n']:>5}  {r['source']:6}  {r['session_id']:<44}  "
              f"{_oneline(r['cwd'], 40):<40}  {_oneline(r['title'], 50)}")
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
               t.text, length(COALESCE(t.text, '')) AS chars
        FROM transcript t
        JOIN sessions s ON s.session_id = t.session_id
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
                 length(COALESCE(t.text, '')) AS chars
          FROM transcript t
          JOIN sessions s ON s.session_id = t.session_id
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
               s.cwd, s.title, r.text AS last_user_text,
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
            print(f"seq: {seq}  users: {r['user_msg_count']}  events: {r['live_event_count']}")
            if r["title"]:
                print(_wrapped("title: ", r["title"], 120))
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
            print(f"seq: {seq}  chars: {r['chars']}")
            print(_wrapped("user: ", r["text"], 0 if args.full else 320))
            print(f"expand: sessdb turns {sid} --around-seq {seq}")
    conn.close()


def _turn_rows(conn, session_id: str, include_all: bool):
    where = "WHERE session_id=?" + ("" if include_all else " AND live=1")
    return conn.execute(
        f"""
        SELECT seq, event_id, ts, actor, type, text, refs, live, inherited
        FROM transcript {where}
        ORDER BY seq
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
        "user_inherited": t["user_inherited"], "hidden_tool_events": hidden,
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
            print(_wrapped(f"{indent}user[{t['user_seq']}]{suffix}: ", t["user_text"], args.user_chars))
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
    where = "WHERE session_id=?" + ("" if args.all else " AND live=1")
    rows = conn.execute(
        f"SELECT seq, ts, actor, type, text, refs, live FROM transcript {where} ORDER BY seq",
        (sid,),
    ).fetchall()
    glyph = {"message": " ", "tool_call": "⚙", "tool_result": "↩"}
    for r in rows:
        dead = "" if r["live"] else " (rolled-back)"
        refs = json.loads(r["refs"] or "{}")
        extra = ""
        if refs.get("commands"):
            extra = f"  $ {_oneline(refs['commands'][0], 80)}"
        elif refs.get("files"):
            extra = f"  → {', '.join(refs['files'][:3])}"
        print(f"[{r['seq']:>4}] {r['actor']:9} {glyph.get(r['type'],' ')} "
              f"{_oneline(r['text'], 160)}{extra}{dead}")
    conn.close()


def _search_rows(conn, args):
    where = ["events_fts MATCH ?", "se.live = 1", "COALESCE(e.text, '') <> ''"]
    params: list = [args.query]

    only_session = getattr(args, "only_session", None)
    if not getattr(args, "include_subagents", False):
        where.append(_not_subagent_session_sql())
    if not getattr(args, "include_inherited", False) and not only_session:
        where.append("se.inherited = 0")
    if getattr(args, "actor", None):
        where.append("e.actor = ?")
        params.append(args.actor)
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
               snippet(events_fts, 0, '[', ']', '…', 12) AS snip
        FROM events_fts
        JOIN events e ON e.rowid = events_fts.rowid
        JOIN session_events se ON se.event_id = e.event_id
        JOIN sessions s ON s.session_id = se.session_id
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
        "inherited": r["inherited"], "snippet": r["snip"], "text": r["text"],
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
            print(f"seq: {r['seq']}  event: {r['event_id']}  {r['actor']}/{r['type']}")
            print(_wrapped("match: ", r["snip"], width=100))
            print(f"expand: sessdb turns {r['session_id']} --around-seq {r['seq']}")
            if args.around is not None:
                print("context:")
                _print_turns(_search_turn_context(conn, r, args), args, indent="  ")
            print()
    conn.close()


def cmd_grep(args):
    rg = shutil.which("rg")
    default_roots = [str(DEFAULT_CLAUDE_ROOT), str(DEFAULT_CODEX_ROOT), str(DEFAULT_PI_ROOT)]
    paths = args.paths or [p for p in default_roots if Path(p).exists()]
    if rg:
        sys.exit(subprocess.call([rg, args.pattern, *paths]))
    sys.exit(subprocess.call(["grep", "-rn", args.pattern, *paths]))


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

    sp = sub.add_parser("list", help="recent sessions")
    sp.add_argument("--limit", type=int, default=30)
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
    sp.add_argument("--no-refresh", action="store_true", help="skip the delta ingest")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("grep", help="ripgrep over raw logs (all sources by default)")
    sp.add_argument("pattern")
    sp.add_argument("paths", nargs="*")
    sp.set_defaults(func=cmd_grep)

    sp = sub.add_parser("schema", help="print the DDL")
    sp.set_defaults(func=cmd_schema)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

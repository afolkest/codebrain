"""sessdb — thin CLI over the codebrain SQLite cache.

  sessdb ingest [--source all]         full build/rebuild of the local DB
  sessdb list [--limit N]              recent sessions
  sessdb show <session> [--all]        a session's transcript (live by default)
  sessdb search <query> [--limit N]    FTS over event text
  sessdb grep <pattern> [paths...]     ripgrep over the raw logs

Read commands refresh first: changed/new raw files are delta-ingested before the
query runs (ms when nothing changed), so results are always current for this
machine — including sessions that are live right now. --no-refresh skips it.

Raw SQL escape hatch: just open the DB with any sqlite3 client (see --schema).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

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


def _resolve_session(conn, ident: str):
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE session_id = ? OR session_id LIKE ? LIMIT 1",
        (ident, f"%{ident}%"),
    ).fetchone()
    return row["session_id"] if row else None


def cmd_ingest(args):
    conn = connect(args.db)
    sources = SOURCES if args.source == "all" else (args.source,)
    print(f"ingesting [{', '.join(sources)}] → {args.db}")
    total = ingest_all(conn, sources=sources, machine=args.machine)
    conn.close()
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


def cmd_search(args):
    conn = _open(args)
    if not has_fts5(conn):
        print("FTS5 unavailable in this sqlite build; use `sessdb grep` instead.", file=sys.stderr)
        sys.exit(2)
    rows = conn.execute(
        """
        SELECT e.event_id, e.actor, e.type, se.session_id, se.seq, e.ts,
               snippet(events_fts, 0, '[', ']', '…', 12) AS snip
        FROM events_fts f
        JOIN events e ON e.rowid = f.rowid
        JOIN session_events se ON se.event_id = e.event_id AND se.live = 1
        WHERE events_fts MATCH ?
        ORDER BY rank
        LIMIT ?
        """,
        (args.query, args.limit),
    ).fetchall()
    for r in rows:
        print(f"{r['session_id']} [seq {r['seq']}] {r['actor']}/{r['type']}: {_oneline(r['snip'], 160)}")
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

    sp = sub.add_parser("list", help="recent sessions")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--no-refresh", action="store_true", help="skip the delta ingest")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("show", help="a session's transcript")
    sp.add_argument("session")
    sp.add_argument("--all", action="store_true", help="include rolled-back events")
    sp.add_argument("--no-refresh", action="store_true", help="skip the delta ingest")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("search", help="FTS over event text")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=20)
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

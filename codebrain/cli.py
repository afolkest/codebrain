"""sessdb — thin CLI over the codebrain SQLite cache.

  sessdb ingest [--raw ~/.claude]      build/update the local DB
  sessdb list [--limit N]              recent sessions
  sessdb show <session> [--all]        a session's transcript (live by default)
  sessdb search <query> [--limit N]    FTS over event text
  sessdb grep <pattern> [paths...]     ripgrep over the raw logs

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
from codebrain.ingest import DEFAULT_CLAUDE_ROOT, ingest_claude


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
    print(f"ingesting claude logs from {args.raw} → {args.db}")
    stats = ingest_claude(conn, raw_root=Path(args.raw))
    conn.close()
    print("done: " + ", ".join(f"{k}={v}" for k, v in stats.items()))


def cmd_list(args):
    conn = connect(args.db)
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
    conn = connect(args.db)
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
    conn = connect(args.db)
    if not has_fts5(conn):
        print("FTS5 unavailable in this sqlite build; use `sessdb grep` instead.", file=sys.stderr)
        sys.exit(2)
    rows = conn.execute(
        """
        SELECT f.event_id, e.actor, e.type, se.session_id, se.seq, e.ts,
               snippet(events_fts, 0, '[', ']', '…', 12) AS snip
        FROM events_fts f
        JOIN events e ON e.event_id = f.event_id
        JOIN session_events se ON se.event_id = f.event_id AND se.live = 1
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
    paths = args.paths or [str(Path(args.raw))]
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
    sp.add_argument("--raw", default=str(DEFAULT_CLAUDE_ROOT))
    sp.set_defaults(func=cmd_ingest)

    sp = sub.add_parser("list", help="recent sessions")
    sp.add_argument("--limit", type=int, default=30)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("show", help="a session's transcript")
    sp.add_argument("session")
    sp.add_argument("--all", action="store_true", help="include rolled-back events")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("search", help="FTS over event text")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("grep", help="ripgrep over raw logs")
    sp.add_argument("pattern")
    sp.add_argument("paths", nargs="*")
    sp.add_argument("--raw", default=str(DEFAULT_CLAUDE_ROOT))
    sp.set_defaults(func=cmd_grep)

    sp = sub.add_parser("schema", help="print the DDL")
    sp.set_defaults(func=cmd_schema)

    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()

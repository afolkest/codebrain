"""bmux provenance overlay.

codebrain indexes native transcripts; bmux can submit prompts into worker panes
on behalf of a master agent, and those land in the transcript as ordinary native
`user` messages. This module joins bmux's own control-plane event log to those
transcripts so master-injected text is kept out of the human-intent bucket —
*structurally* (resolved session + exact UTF-8 SHA-256), never by prompt style.

Boundaries (see plan Non-Goals): reads bmux's event log only; writes only the
two rebuildable overlay tables; never mutates events/session_events; an
unresolved/ambiguous submission is left unmatched (fail closed), never guessed.

Fail-closed posture (hardened after review): a match requires *both* a resolved
single session AND a parseable, correctly-ordered timestamp inside a narrow
window. Anything missing or ambiguous degrades to `unknown` or no row, never to a
silent human/master_control guess.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

SUBMIT_KINDS = ("bmux.send_submitted", "bmux.launch_prompt_submitted")
LINK_KINDS = ("bmux.pane_discovered", "bmux.pane_linked")

# A native user message is a candidate only if its ts sits at/after the bmux
# submission (the submission *causes* the message), within a narrow forward
# window. Real submissions land within ~100ms; the window only absorbs ingestion
# lag, and the small negative skew absorbs cross-process clock jitter. Ordered
# (not abs()) so a human message typed *before* a bmux send can never be claimed.
DEFAULT_WINDOW_SEC = 120
NEG_SKEW_SEC = 5


def _default_log() -> Path:
    """Resolved lazily (not import-time) so CODEBRAIN_BMUX_LOG can redirect it
    per-process — required for hermetic tests of the read-path hook."""
    return Path(os.environ.get(
        "CODEBRAIN_BMUX_LOG", Path.home() / ".bmux" / "events" / "bmux.jsonl"))


DEFAULT_BMUX_LOG = _default_log()  # for CLI help text / back-compat


def _read_jsonl(path: Path) -> list:
    out = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(rec)
    return out


def extract_launch_id(data, by_id: dict):
    """launch-id-first extraction across current + historical bmux shapes
    (see README bmux provenance notes). Returns (launch_id, via)."""
    if not isinstance(data, dict):
        return None, None
    if data.get("launch_id"):
        return data["launch_id"], "data.launch_id"
    lc = data.get("launch_correlation")
    if isinstance(lc, dict) and lc.get("launch_id"):
        return lc["launch_id"], "data.launch_correlation.launch_id"
    pb = data.get("previous_bmux")
    if isinstance(pb, dict) and pb.get("launch_id"):
        return pb["launch_id"], "data.previous_bmux.launch_id"
    aid = data.get("attempt_event_id")
    if aid and aid in by_id:
        lid, via = extract_launch_id(by_id[aid].get("data", {}), by_id)
        if lid:
            return lid, f"attempt({via})"
    return None, None


def _build_launch_index(events: list, by_id: dict) -> dict:
    """launch_id -> (codebrain_session_id, via), only when the launch_id resolves
    to exactly ONE session across all link events. A launch_id seen with two
    different sessions is dropped (fail closed) — first-writer-wins would risk
    attributing a control prompt to the wrong session."""
    candidates: dict = {}  # launch_id -> {session_id: via}
    for e in events:
        if e.get("kind") not in LINK_KINDS:
            continue
        d = e.get("data")
        if not isinstance(d, dict):
            continue
        sid = d.get("codebrain_session_id")
        if not sid:
            continue
        lid, via = extract_launch_id(d, by_id)
        if lid:
            candidates.setdefault(lid, {}).setdefault(sid, via)
    return {lid: (next(iter(sess)), next(iter(sess.values())))
            for lid, sess in candidates.items() if len(sess) == 1}


def read_submissions(path=None) -> list:
    """Parse the bmux log into resolved control-submission dicts (no DB needed).

    Drops submissions lacking a payload sha256 or a submitted_at — neither can be
    matched under the fail-closed rule, and a missing submitted_at also violates
    the overlay's NOT NULL contract. Each kept dict: kind, send_id, launch_id,
    submitted_at, codebrain_session_id (or None if unresolved), resolved_via,
    payload_sha256, payload_byte_count, payload_line_count, master_id, raw_event.
    """
    events = _read_jsonl(Path(path) if path else _default_log())
    by_id = {e.get("event_id"): e for e in events}
    launch_index = _build_launch_index(events, by_id)

    subs = []
    for e in events:
        kind = e.get("kind")
        if kind not in SUBMIT_KINDS:
            continue
        d = e.get("data")
        if not isinstance(d, dict):
            continue
        payload = d.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        sha = payload.get("sha256")
        submitted_at = d.get("submitted_at") or d.get("attempted_at")
        if not sha or not submitted_at:
            continue  # cannot satisfy the fail-closed match conditions
        if kind == "bmux.send_submitted":
            sid, via = d.get("codebrain_session_id"), "data.codebrain_session_id"
        else:
            lid, via0 = extract_launch_id(d, by_id)
            sid, src_via = launch_index.get(lid, (None, None))
            via = f"launch_id={lid}:{via0}->{src_via}" if lid else "no_launch_id"
        actor = e.get("actor")
        subs.append({
            "kind": kind,
            "send_id": d.get("send_id"),
            "launch_id": extract_launch_id(d, by_id)[0],
            "submitted_at": submitted_at,
            "codebrain_session_id": sid,
            "resolved_via": via,
            "payload_sha256": sha,
            "payload_byte_count": payload.get("byte_count"),
            "payload_line_count": payload.get("line_count"),
            "master_id": actor.get("master_id") if isinstance(actor, dict) else None,
            "raw_event": json.dumps(e, ensure_ascii=False),
        })
    return subs


def _to_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _sha256(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _candidate_user_messages(conn: sqlite3.Connection, session_id: str) -> list:
    """Live, *authored* native user messages for a session — the only events a
    bmux control submission can be matched against. inherited=0 excludes pi
    resume/branch copies (context, not new intent); the verdict is later
    propagated to every placement of the matched event so copies stay consistent."""
    return conn.execute(
        """
        SELECT event_id, ts, text FROM transcript
        WHERE session_id = ? AND actor = 'user' AND type = 'message'
              AND live = 1 AND inherited = 0 AND COALESCE(text, '') <> ''
        """,
        (session_id,),
    ).fetchall()


def _log_state(conn: sqlite3.Connection, path: Path):
    row = conn.execute(
        "SELECT mtime, size FROM ingest_state WHERE path = ?", (str(path),)
    ).fetchone()
    return (row["mtime"], row["size"]) if row else None


def _empty_stats(**extra) -> dict:
    base = {"submissions": 0, "stored": 0, "resolved": 0, "unresolved": 0,
            "master_control": 0, "unknown": 0, "skipped": 0}
    base.update(extra)
    return base


def sync(conn: sqlite3.Connection, path=None, window_sec: int = DEFAULT_WINDOW_SEC,
         changed_hint: bool = True, force: bool = False) -> dict:
    """Rebuild the bmux provenance overlay from the bmux event log.

    Idempotent and rebuildable. No-op (zeros) when the log is absent — bmux need
    not be installed. When neither the bmux log nor transcripts changed since the
    last successful run, returns early without touching the DB (`skipped=1`), so
    the read-path hook is free in steady state. Matching is scoped to the sessions
    bmux actually resolved, so it is cheap regardless of DB size. The whole
    rebuild runs in one transaction; any error rolls back, never leaving a
    half-deleted overlay.
    """
    path = Path(path) if path else _default_log()
    if not path.exists():
        return _empty_stats()

    st = path.stat()
    cur_state = (st.st_mtime, st.st_size)
    if not force and not changed_hint and _log_state(conn, path) == cur_state:
        return _empty_stats(skipped=1)  # nothing changed -> no-op

    stats = _empty_stats()
    try:
        # refresh() commits per-file before the read-path calls us, so a rollback
        # here only undoes this rebuild's own DML — never prior ingested work.
        subs = read_submissions(path)
        stats["submissions"] = len(subs)

        # Refresh the submissions mirror (small; full rewrite keeps it clean).
        conn.execute("DELETE FROM bmux_control_submissions")
        for s in subs:
            conn.execute(
                """
                INSERT OR REPLACE INTO bmux_control_submissions
                  (send_id, launch_id, kind, submitted_at, codebrain_session_id,
                   payload_sha256, payload_byte_count, payload_line_count, master_id,
                   resolved_via, raw_event)
                VALUES (:send_id, :launch_id, :kind, :submitted_at, :codebrain_session_id,
                   :payload_sha256, :payload_byte_count, :payload_line_count, :master_id,
                   :resolved_via, :raw_event)
                """,
                s,
            )
            stats["stored"] += 1

        # Rebuild only the bmux-evidenced verdicts.
        conn.execute("DELETE FROM event_origins WHERE evidence_kind = 'bmux'")

        # Candidate pairs: (submission, authored user message) with same resolved
        # session + identical UTF-8 SHA-256, msg ts in [submitted_at - skew,
        # submitted_at + window]. Both timestamps must parse, or the pair is
        # rejected (fail closed). One DB read per resolved session.
        msg_cache: dict = {}
        pairs = []        # (sub_idx, event_id) — verified timed matches
        degraded = set()  # event_ids: hash + session match, order unverifiable
        for i, s in enumerate(subs):
            sid = s["codebrain_session_id"]
            if not sid:
                stats["unresolved"] += 1
                continue
            stats["resolved"] += 1
            sub_dt = _to_dt(s["submitted_at"])
            if sid not in msg_cache:
                msg_cache[sid] = _candidate_user_messages(conn, sid)
            for r in msg_cache[sid]:
                if _sha256(r["text"]) != s["payload_sha256"]:
                    continue
                msg_dt = _to_dt(r["ts"])
                if sub_dt is None or msg_dt is None:
                    # Hash + session line up but order can't be verified: the
                    # message sits inside a specific bmux event's blast radius yet
                    # is unverifiable -> unknown, never clean human (plan
                    # "Ambiguity Scope"). Cannot be master_control without a time.
                    degraded.add(r["event_id"])
                    continue
                delta = (msg_dt - sub_dt).total_seconds()
                if delta < -NEG_SKEW_SEC or delta > window_sec:
                    continue  # timestamps readable and far apart -> unrelated, human
                pairs.append((i, r["event_id"]))

        # One-to-one or fail closed: an event is master_control only if it is
        # paired with exactly one submission AND that submission is paired with
        # exactly one event. Any other pairing inside a (session, hash, window)
        # cluster is unknown — plausibly bmux, not uniquely attributable.
        ev_subs: dict = {}   # event_id -> set(sub_idx)
        sub_evs: dict = {}   # sub_idx -> set(event_id)
        for sub_idx, eid in pairs:
            ev_subs.setdefault(eid, set()).add(sub_idx)
            sub_evs.setdefault(sub_idx, set()).add(eid)

        verdicts: dict = {}  # event_id -> (origin, evidence_id, reason)
        for eid, sub_idxs in ev_subs.items():
            unique = len(sub_idxs) == 1 and len(sub_evs[next(iter(sub_idxs))]) == 1
            if unique:
                ev = subs[next(iter(sub_idxs))]
                verdicts[eid] = ("master_control", ev["send_id"] or ev["launch_id"],
                                 "unique bmux control-submission match")
            else:
                verdicts[eid] = ("unknown", None,
                                 "ambiguous bmux control-submission candidates")
        # Degraded-evidence matches override to unknown — a hash+session match
        # whose order we cannot verify must not be promoted to master_control.
        for eid in degraded:
            verdicts[eid] = ("unknown", None,
                             "bmux payload hash match with unverifiable timestamp")

        for eid, (origin, evidence_id, reason) in verdicts.items():
            stats["master_control" if origin == "master_control" else "unknown"] += 1
            # Propagate the verdict to EVERY placement of this (copy-invariant)
            # event — inherited copies in resumed sessions must classify the same.
            for p in conn.execute(
                "SELECT session_id FROM session_events WHERE event_id = ?", (eid,)
            ).fetchall():
                conn.execute(
                    """
                    INSERT OR REPLACE INTO event_origins
                      (session_id, event_id, origin, evidence_kind, evidence_id, reason)
                    VALUES (?, ?, ?, 'bmux', ?, ?)
                    """,
                    (p["session_id"], eid, origin, evidence_id, reason),
                )

        # Remember the log stat so an unchanged log short-circuits next time.
        conn.execute(
            "INSERT OR REPLACE INTO ingest_state (path, mtime, size, session_id) "
            "VALUES (?, ?, ?, NULL)", (str(path), cur_state[0], cur_state[1]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return stats

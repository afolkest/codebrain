"""Path normalization primitives shared by the ingest layer and the CLI.

The files index (db.file_refs) stores a `basename` computed from each file ref;
`touched` matches a query's basename against it. Both sides MUST normalize
identically or the index would silently miss real matches — so the canonical
`path_norm`/`path_basename` live here, imported by both db.py (write side) and
cli.py (query side).
"""
from __future__ import annotations

from pathlib import Path


def path_norm(value) -> str:
    """Normalize a path string for comparison: unify separators, expand a leading
    ~, and strip leading ./ segments. Deliberately textual (no filesystem touch)."""
    s = str(value or "").strip().replace("\\", "/")
    if s == "~" or s.startswith("~/"):
        s = str(Path.home()).replace("\\", "/") + s[1:]
    while s.startswith("./"):
        s = s[2:]
    return s


def path_basename(value) -> str:
    value = path_norm(value).rstrip("/")
    return value.rsplit("/", 1)[-1] if value else ""

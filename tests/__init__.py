"""codebrain test suite (stdlib unittest — no third-party deps).

Run from the repo root:

    python3 -m unittest discover -v

Fixtures are synthetic JSONL built inline next to the assertions (so each test
doubles as documentation of the format shape it exercises); the real-corpus
smoke test (test_smoke_real) runs the same invariants over actual logs and
skips cleanly on machines without them.
"""
import os
import sys
from pathlib import Path

# Make `codebrain` importable from a bare checkout (no `pip install -e .` needed).
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Hermeticity: the refresh-on-read path runs the bmux provenance sync, which by
# default reads ~/.bmux/events/bmux.jsonl. Point it at a nonexistent path so no
# test (e.g. those that refresh without --no-refresh) depends on a dev machine's
# real bmux log. Tests that exercise the overlay redirect this env explicitly.
os.environ["CODEBRAIN_BMUX_LOG"] = str(_ROOT / "tests" / ".nonexistent-bmux.jsonl")

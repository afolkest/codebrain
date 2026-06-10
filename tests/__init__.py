"""codebrain test suite (stdlib unittest — no third-party deps).

Run from the repo root:

    python3 -m unittest discover -v

Fixtures are synthetic JSONL built inline next to the assertions (so each test
doubles as documentation of the format shape it exercises); the real-corpus
smoke test (test_smoke_real) runs the same invariants over actual logs and
skips cleanly on machines without them.
"""
import sys
from pathlib import Path

# Make `codebrain` importable from a bare checkout (no `pip install -e .` needed).
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

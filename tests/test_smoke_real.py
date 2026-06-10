"""Real-corpus smoke: run the schema invariants over a spread of ACTUAL logs.

This is the guard that synthetic fixtures can't be: it catches format shapes we
didn't imagine. It reads each tool's home read-only and skips cleanly on a
machine that doesn't have those logs (e.g. CI), so it's safe to commit.
"""
import unittest

from codebrain import ingest
from codebrain.adapters import claude, codex, pi
from tests._helpers import assert_session_invariants

SAMPLE = 12   # files per source, spread across the corpus


class TestRealCorpusSmoke(unittest.TestCase):
    def _run(self, files, parse, source):
        files = list(files)
        if not files:
            self.skipTest(f"no {source} logs on this machine")
        step = max(1, len(files) // SAMPLE)
        sample = files[::step][:SAMPLE]
        checked = 0
        for f in sample:
            with self.subTest(file=f.name):
                parsed = parse(f)
                if parsed is None:
                    continue   # contentless file (empty / no emittable events)
                assert_session_invariants(self, parsed, source)
                checked += 1
        if checked == 0:
            self.skipTest(f"no parseable {source} sessions in the sample")

    def test_claude(self):
        self._run(ingest.discover_claude_files(ingest.DEFAULT_CLAUDE_ROOT),
                  lambda f: claude.parse_file(f, machine="t"), "claude")

    def test_codex(self):
        self._run(ingest.discover_codex_files(ingest.DEFAULT_CODEX_ROOT),
                  lambda f: codex.parse_file(f, machine="t"), "codex")

    def test_pi(self):
        self._run(ingest.discover_pi_files(ingest.DEFAULT_PI_ROOT),
                  lambda f: pi.parse_file(f, machine="t"), "pi")


if __name__ == "__main__":
    unittest.main()

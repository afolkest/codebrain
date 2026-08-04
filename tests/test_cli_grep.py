import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from codebrain import cli


class TestGrepCli(unittest.TestCase):
    def test_default_roots_include_live_and_remote_pool_but_skip_local_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            live_claude = root / "live-claude"
            live_pi = root / "live-pi"
            safe_cursor = root / "safe-cursor"
            live_claude.mkdir()
            live_pi.mkdir()
            safe_cursor.mkdir()
            pool = root / "pool"
            remote_pi = pool / "raw" / "mini" / "pi"
            remote_cursor = pool / "raw" / "mini" / "cursor"
            local_claude = pool / "raw" / "local" / "claude"
            alias_codex = pool / "raw" / "alias" / "codex"
            remote_pi.mkdir(parents=True)
            remote_cursor.mkdir(parents=True)
            local_claude.mkdir(parents=True)
            alias_codex.mkdir(parents=True)

            with mock.patch("codebrain.cli.DEFAULT_CLAUDE_ROOT", live_claude), \
                 mock.patch("codebrain.cli.DEFAULT_CODEX_ROOT", root / "missing-codex"), \
                 mock.patch("codebrain.cli.DEFAULT_PI_ROOT", live_pi), \
                 mock.patch("codebrain.cli.DEFAULT_CURSOR_ROOT", safe_cursor), \
                 mock.patch("codebrain.cli.DEFAULT_POOL", pool), \
                 mock.patch("codebrain.ingest.socket.gethostname", return_value="host-under-test"), \
                 mock.patch.dict(
                     "os.environ",
                     {"CODEBRAIN_MACHINE": "local", "CODEBRAIN_LOCAL_MACHINES": "alias"},
                     clear=False,
                 ):
                roots = cli._default_grep_roots()

        self.assertEqual(roots, [
            str(live_claude), str(live_pi), str(safe_cursor),
            str(remote_pi), str(remote_cursor),
        ])
        self.assertNotIn(str(local_claude), roots)
        self.assertNotIn(str(alias_codex), roots)

    def test_default_roots_never_include_cursor_database_or_live_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            safe = root / "codebrain-cursor-raw"
            unsafe_home = root / ".cursor"
            unsafe_db = root / "Cursor" / "User" / "globalStorage" / "state.vscdb"
            safe.mkdir()
            unsafe_home.mkdir()
            unsafe_db.parent.mkdir(parents=True)
            unsafe_db.write_text("private", encoding="utf-8")
            with mock.patch("codebrain.cli.DEFAULT_CLAUDE_ROOT", root / "missing-claude"), \
                 mock.patch("codebrain.cli.DEFAULT_CODEX_ROOT", root / "missing-codex"), \
                 mock.patch("codebrain.cli.DEFAULT_PI_ROOT", root / "missing-pi"), \
                 mock.patch("codebrain.cli.DEFAULT_CURSOR_ROOT", safe), \
                 mock.patch("codebrain.cli.DEFAULT_POOL", root / "missing-pool"):
                roots = cli._default_grep_roots()

        self.assertEqual(roots, [str(safe)])
        self.assertNotIn(str(unsafe_home), roots)
        self.assertNotIn(str(unsafe_db), roots)

    def test_default_roots_exclude_symlinked_local_cursor_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unsafe_home = root / ".cursor"
            unsafe_home.mkdir()
            codebrain_home = root / ".codebrain"
            codebrain_home.mkdir()
            (codebrain_home / "cursor-raw").symlink_to(
                unsafe_home, target_is_directory=True,
            )

            with mock.patch("codebrain.cli.DEFAULT_CLAUDE_ROOT", root / "missing-claude"), \
                 mock.patch("codebrain.cli.DEFAULT_CODEX_ROOT", root / "missing-codex"), \
                 mock.patch("codebrain.cli.DEFAULT_PI_ROOT", root / "missing-pi"), \
                 mock.patch("codebrain.cli.DEFAULT_CURSOR_ROOT", codebrain_home / "cursor-raw"), \
                 mock.patch("codebrain.cli.DEFAULT_POOL", root / "missing-pool"):
                roots = cli._default_grep_roots()

        self.assertEqual(roots, [])
        self.assertNotIn(str(unsafe_home), roots)

    def test_default_roots_exclude_cursor_root_below_symlinked_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            redirected = root / "redirected"
            (redirected / "cursor-raw").mkdir(parents=True)
            (root / ".codebrain").symlink_to(redirected, target_is_directory=True)

            with mock.patch("codebrain.cli.DEFAULT_CLAUDE_ROOT", root / "missing-claude"), \
                 mock.patch("codebrain.cli.DEFAULT_CODEX_ROOT", root / "missing-codex"), \
                 mock.patch("codebrain.cli.DEFAULT_PI_ROOT", root / "missing-pi"), \
                 mock.patch(
                     "codebrain.cli.DEFAULT_CURSOR_ROOT", root / ".codebrain" / "cursor-raw",
                 ), \
                 mock.patch("codebrain.cli.DEFAULT_POOL", root / "missing-pool"):
                roots = cli._default_grep_roots()

        self.assertEqual(roots, [])

    def test_default_roots_exclude_symlinked_remote_cursor_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = root / "pool"
            remote_machine = pool / "raw" / "remote"
            remote_pi = remote_machine / "pi"
            remote_pi.mkdir(parents=True)
            unsafe_cursor = root / "unsafe-cursor"
            unsafe_cursor.mkdir()
            remote_cursor = remote_machine / "cursor"
            remote_cursor.symlink_to(
                unsafe_cursor, target_is_directory=True,
            )

            with mock.patch("codebrain.cli.DEFAULT_CLAUDE_ROOT", root / "missing-claude"), \
                 mock.patch("codebrain.cli.DEFAULT_CODEX_ROOT", root / "missing-codex"), \
                 mock.patch("codebrain.cli.DEFAULT_PI_ROOT", root / "missing-pi"), \
                 mock.patch("codebrain.cli.DEFAULT_CURSOR_ROOT", root / "missing-cursor"), \
                 mock.patch("codebrain.cli.DEFAULT_POOL", pool), \
                 mock.patch("codebrain.ingest.socket.gethostname", return_value="local"):
                roots = cli._default_grep_roots()

        self.assertEqual(roots, [str(remote_pi)])
        self.assertNotIn(str(remote_cursor), roots)

    def test_default_roots_exclude_remote_cursor_below_symlinked_machine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = root / "pool"
            raw = pool / "raw"
            raw.mkdir(parents=True)
            redirected_machine = root / "redirected-machine"
            (redirected_machine / "cursor").mkdir(parents=True)
            remote_machine = raw / "remote"
            remote_machine.symlink_to(redirected_machine, target_is_directory=True)

            with mock.patch("codebrain.cli.DEFAULT_CLAUDE_ROOT", root / "missing-claude"), \
                 mock.patch("codebrain.cli.DEFAULT_CODEX_ROOT", root / "missing-codex"), \
                 mock.patch("codebrain.cli.DEFAULT_PI_ROOT", root / "missing-pi"), \
                 mock.patch("codebrain.cli.DEFAULT_CURSOR_ROOT", root / "missing-cursor"), \
                 mock.patch("codebrain.cli.DEFAULT_POOL", pool), \
                 mock.patch("codebrain.ingest.socket.gethostname", return_value="local"):
                roots = cli._default_grep_roots()

        self.assertEqual(roots, [])

    def test_default_roots_handle_missing_pool_and_empty_live_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch("codebrain.cli.DEFAULT_CLAUDE_ROOT", root / "missing-claude"), \
                 mock.patch("codebrain.cli.DEFAULT_CODEX_ROOT", root / "missing-codex"), \
                 mock.patch("codebrain.cli.DEFAULT_PI_ROOT", root / "missing-pi"), \
                 mock.patch("codebrain.cli.DEFAULT_CURSOR_ROOT", root / "missing-cursor"), \
                 mock.patch("codebrain.cli.DEFAULT_POOL", root / "missing-pool"):
                roots = cli._default_grep_roots()

        self.assertEqual(roots, [])

    def test_default_roots_deduplicate_exact_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pool = root / "pool"
            remote_pi = pool / "raw" / "remote" / "pi"
            remote_pi.mkdir(parents=True)

            with mock.patch("codebrain.cli.DEFAULT_CLAUDE_ROOT", remote_pi), \
                 mock.patch("codebrain.cli.DEFAULT_CODEX_ROOT", root / "missing-codex"), \
                 mock.patch("codebrain.cli.DEFAULT_PI_ROOT", root / "missing-pi"), \
                 mock.patch("codebrain.cli.DEFAULT_CURSOR_ROOT", root / "missing-cursor"), \
                 mock.patch("codebrain.cli.DEFAULT_POOL", pool), \
                 mock.patch("codebrain.ingest.socket.gethostname", return_value="host-under-test"):
                roots = cli._default_grep_roots()

        self.assertEqual(roots, [str(remote_pi)])

    def test_explicit_paths_override_default_roots(self):
        args = SimpleNamespace(pattern="needle", paths=["/tmp/custom"])

        with mock.patch("codebrain.cli._default_grep_roots") as default_roots, \
             mock.patch("codebrain.cli.shutil.which", return_value="rg"), \
             mock.patch("codebrain.cli.subprocess.call", return_value=0) as call:
            with self.assertRaises(SystemExit) as cm:
                cli.cmd_grep(args)

        self.assertEqual(cm.exception.code, 0)
        default_roots.assert_not_called()
        call.assert_called_once_with(
            ["rg", "--glob", "!**/file-history/**", "--", "needle", "/tmp/custom"]
        )

    def test_ripgrep_excludes_file_history_dirs(self):
        cmd = cli._grep_command("needle", ["/tmp/.claude"], "rg")

        self.assertEqual(cmd[:4], ["rg", "--glob", "!**/file-history/**", "--"])
        self.assertEqual(cmd[4:], ["needle", "/tmp/.claude"])

    def test_grep_fallback_excludes_file_history_dirs(self):
        cmd = cli._grep_command("needle", ["/tmp/.claude"], None)

        self.assertEqual(
            cmd,
            ["grep", "-rn", "--exclude-dir=file-history", "--", "needle", "/tmp/.claude"],
        )

    def test_explicit_file_history_path_is_not_searched(self):
        cmd = cli._grep_command(
            "needle",
            ["/tmp/.claude/file-history/S/snap", "/tmp/.claude/projects"],
            "rg",
        )

        self.assertEqual(cmd[-1], "/tmp/.claude/projects")
        self.assertNotIn("/tmp/.claude/file-history/S/snap", cmd)

    def test_only_file_history_paths_return_no_command(self):
        cmd = cli._grep_command("needle", ["/tmp/.claude/file-history"], "rg")

        self.assertIsNone(cmd)

    def test_files_with_matches_flag_passes_through(self):
        rg = cli._grep_command("needle", ["/tmp/.claude"], "rg", files_only=True)
        self.assertEqual(rg, ["rg", "-l", "--glob", "!**/file-history/**", "--",
                              "needle", "/tmp/.claude"])
        grep = cli._grep_command("needle", ["/tmp/.claude"], None, files_only=True)
        self.assertEqual(grep, ["grep", "-rn", "-l", "--exclude-dir=file-history", "--",
                                "needle", "/tmp/.claude"])

    def test_count_flag_passes_through(self):
        rg = cli._grep_command("needle", ["/tmp/.claude"], "rg", count=True)
        self.assertEqual(rg, ["rg", "-c", "--glob", "!**/file-history/**", "--",
                              "needle", "/tmp/.claude"])

    def test_files_only_wins_over_count(self):
        cmd = cli._grep_command("needle", ["/tmp/.claude"], "rg", count=True, files_only=True)
        self.assertIn("-l", cmd)
        self.assertNotIn("-c", cmd)

    def test_grep_l_flag_wires_through_main_to_command(self):
        with mock.patch("codebrain.cli._default_grep_roots", return_value=["/tmp/.claude"]), \
             mock.patch("codebrain.cli.shutil.which", return_value="rg"), \
             mock.patch("codebrain.cli.subprocess.call", return_value=0) as call:
            with self.assertRaises(SystemExit) as cm:
                cli.main(["grep", "-l", "needle"])

        self.assertEqual(cm.exception.code, 0)
        call.assert_called_once_with(
            ["rg", "-l", "--glob", "!**/file-history/**", "--", "needle", "/tmp/.claude"]
        )


if __name__ == "__main__":
    unittest.main()

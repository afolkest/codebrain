import unittest

from codebrain import cli


class TestGrepCli(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class RepositoryHygieneTests(unittest.TestCase):
    def assert_git_ignored(self, relative_path):
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", "--", relative_path],
            cwd=ROOT,
            check=False,
        )
        self.assertEqual(result.returncode, 0, relative_path)

    def test_personal_review_drafts_remain_ignored(self):
        self.assert_git_ignored("另一个ai的评价.md")
        self.assert_git_ignored("docs/AI审查意见.md")

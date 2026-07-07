import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import github_helpers


class FakeResp:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {}


class CodePrDiffTests(unittest.TestCase):
    def setUp(self):
        github_helpers.clear_active_code_repo()

    def tearDown(self):
        github_helpers.clear_active_code_repo()

    def test_parse_pr_number(self):
        self.assertEqual(github_helpers._parse_pr_number(32), 32)
        self.assertEqual(github_helpers._parse_pr_number("#32"), 32)
        self.assertEqual(github_helpers._parse_pr_number("https://github.com/o/r/pull/32"), 32)
        self.assertEqual(github_helpers._parse_pr_number("PR 32 please"), 32)
        self.assertIsNone(github_helpers._parse_pr_number("no number here"))

    def test_not_configured_without_token(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIn("isn't configured", github_helpers.code_pr_diff(32))

    def test_returns_diff_and_requests_diff_media_type(self):
        diff = "diff --git a/README.md b/README.md\n@@\n+hello"
        mock_get = MagicMock(return_value=FakeResp(200, diff))
        with patch.dict(os.environ, {"GITHUB_TOKEN": "t", "GITHUB_CODE_REPO": "o/r"}, clear=True), \
                patch.object(github_helpers.requests, "get", mock_get):
            out = github_helpers.code_pr_diff("https://github.com/o/r/pull/32")

        self.assertIn("diff --git", out)
        url = mock_get.call_args.args[0]
        self.assertTrue(url.endswith("/repos/o/r/pulls/32"), url)
        self.assertEqual(
            mock_get.call_args.kwargs["headers"]["Accept"], "application/vnd.github.v3.diff"
        )

    def test_bad_pr_ref(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "t", "GITHUB_CODE_REPO": "o/r"}, clear=True):
            self.assertIn("Couldn't read a PR number", github_helpers.code_pr_diff("nope"))

    def test_pr_not_found(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "t", "GITHUB_CODE_REPO": "o/r"}, clear=True), \
                patch.object(github_helpers.requests, "get", MagicMock(return_value=FakeResp(404, ""))):
            self.assertIn("not found", github_helpers.code_pr_diff(999))


if __name__ == "__main__":
    unittest.main()

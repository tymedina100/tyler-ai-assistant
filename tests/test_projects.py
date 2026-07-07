import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# Make sibling test modules importable regardless of how the suite is launched
# (discover adds this dir; `python -m unittest tests.test_projects` does not).
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

import github_helpers
import projects


class ProjectRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        github_helpers.clear_active_code_repo()

    def tearDown(self):
        github_helpers.clear_active_code_repo()
        self.tmp.cleanup()

    def _env(self, **extra):
        env = {"DATA_DIR": self.tmp.name}
        env.update(extra)
        return env

    def test_registry_loads_default_projects(self):
        reg = projects.load_registry()
        self.assertIn("assistant", reg)
        self.assertIn("vantage", reg)
        self.assertIn("card-tracker", reg)
        self.assertEqual(reg["vantage"]["repo"], "tymedina100/vantage")

    def test_set_active_project_selects_and_persists(self):
        with patch.dict(os.environ, self._env(), clear=False):
            profile, err = projects.set_active_project("vantage")
            self.assertIsNone(err)
            self.assertEqual(profile["repo"], "tymedina100/vantage")

            key, prof = projects.get_active_project()
            self.assertEqual(key, "vantage")
            self.assertTrue((Path(self.tmp.name) / "active_project.json").exists())

    def test_unknown_project_gives_useful_error(self):
        with patch.dict(os.environ, self._env(), clear=False):
            profile, err = projects.set_active_project("nope")
        self.assertIsNone(profile)
        self.assertIn("Unknown project", err)
        self.assertIn("vantage", err)  # lists the known keys

    def test_active_project_drives_github_code_config(self):
        with patch.dict(os.environ, self._env(GITHUB_TOKEN="tok"), clear=False):
            projects.set_active_project("card-tracker")
            _token, repo, base = github_helpers._code_config()
        self.assertEqual(repo, "tymedina100/card-tracker")
        self.assertEqual(base, "main")

    def test_falls_back_to_env_without_active_project(self):
        env = self._env(GITHUB_TOKEN="tok", GITHUB_CODE_REPO="me/envrepo", GITHUB_CODE_BASE="dev")
        with patch.dict(os.environ, env, clear=False):
            projects.clear_active_project()
            _token, repo, base = github_helpers._code_config()
        self.assertEqual(repo, "me/envrepo")
        self.assertEqual(base, "dev")

    def test_branch_name_helper(self):
        self.assertEqual(
            github_helpers.branch_name("vantage", "Add safe-to-spend card"),
            "ai/vantage/add-safe-to-spend-card",
        )

    def test_find_by_linear_project(self):
        self.assertEqual(projects.find_by_linear_project("Worthlane"), "vantage")
        self.assertEqual(projects.find_by_linear_project("worthlane"), "vantage")  # case-insensitive
        self.assertEqual(projects.find_by_linear_project("Tyler AI Assistant"), "assistant")
        self.assertEqual(projects.find_by_linear_project("Card Tracker"), "card-tracker")
        # Name fallback: an alias that isn't the exact linear_project still maps.
        self.assertEqual(projects.find_by_linear_project("Vantage"), "vantage")
        self.assertIsNone(projects.find_by_linear_project("Some Unrelated Project"))
        self.assertIsNone(projects.find_by_linear_project(""))


class CommandParsingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_hardening import import_main_with_stubs
        cls.main = import_main_with_stubs()

    def test_project_command_dispatches_with_case_preserved(self):
        main = self.main
        calls = []
        with patch.object(main, "handle_project_command",
                          lambda arg: calls.append(arg) or "ok"), \
                patch("builtins.print"):
            self.assertTrue(main.handle_command("/project use Vantage"))
        self.assertEqual(calls, [" use Vantage"])

    def test_linear_command_dispatches_with_case_preserved(self):
        main = self.main
        calls = []
        with patch.object(main, "handle_linear_command",
                          lambda arg: calls.append(arg) or "ok"), \
                patch("builtins.print"):
            self.assertTrue(main.handle_command("/linear create Add watchlist alerts"))
        self.assertEqual(calls, [" create Add watchlist alerts"])


if __name__ == "__main__":
    unittest.main()

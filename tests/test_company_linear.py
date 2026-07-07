import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import company_mode
import company_linear


def _seed_project(path):
    """Create a proposed project with the default 4-task plan and return it."""
    company_mode.set_daily_budget(50, path)
    company_mode.assign_goal(
        "launch beta",
        configured_agent_keys=["manager", "code", "research", "write", "editor"],
        specialist_keys=["code", "research", "write", "editor"],
        path=path,
    )
    return company_mode.load_state(path)["projects"][-1]


class CompanyLinearBridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "company_state.json"
        # Ensure a clean hook slate so tests don't leak into each other.
        company_mode.on_project_activated = None
        company_mode.on_task_status_change = None

    def tearDown(self):
        company_mode.on_project_activated = None
        company_mode.on_task_status_change = None
        self.tmp.cleanup()

    def _issue_factory(self):
        counter = {"n": 0}

        def make(*args, **kwargs):
            counter["n"] += 1
            n = counter["n"]
            return {"id": f"i{n}", "identifier": f"VAN-{n}", "url": f"https://l/VAN-{n}"}, None

        return make

    def test_not_enabled_is_a_noop(self):
        project = _seed_project(self.path)
        with patch.object(company_linear.linear_helpers, "is_configured", lambda: False):
            created = company_linear.ensure_issues_for_project(project["id"], path=self.path)
        self.assertEqual(created, [])
        tasks = company_mode.project_tasks(company_mode.load_state(self.path), project["id"])
        self.assertTrue(all(not t["linear_issue_id"] for t in tasks))

    def test_ensure_issues_creates_persists_and_is_idempotent(self):
        project = _seed_project(self.path)
        make = self._issue_factory()
        with patch.object(company_linear.linear_helpers, "is_configured", lambda: True), \
                patch.object(company_linear, "_active_project_key", lambda: None), \
                patch.object(company_linear.linear_helpers, "create_issue", side_effect=make):
            created = company_linear.ensure_issues_for_project(project["id"], path=self.path)

        tasks = company_mode.project_tasks(company_mode.load_state(self.path), project["id"])
        self.assertEqual(len(created), len(tasks))
        for t in tasks:
            self.assertTrue(t["linear_issue_id"])
            self.assertTrue(t["linear_identifier"].startswith("VAN-"))

        # Second call creates nothing (already mirrored).
        second_make = MagicMock()
        with patch.object(company_linear.linear_helpers, "is_configured", lambda: True), \
                patch.object(company_linear, "_active_project_key", lambda: None), \
                patch.object(company_linear.linear_helpers, "create_issue", second_make):
            again = company_linear.ensure_issues_for_project(project["id"], path=self.path)
        self.assertEqual(again, [])
        second_make.assert_not_called()

    def test_sync_status_done_moves_state_and_comments(self):
        project = _seed_project(self.path)
        make = self._issue_factory()
        with patch.object(company_linear.linear_helpers, "is_configured", lambda: True), \
                patch.object(company_linear, "_active_project_key", lambda: None), \
                patch.object(company_linear.linear_helpers, "create_issue", side_effect=make):
            company_linear.ensure_issues_for_project(project["id"], path=self.path)

        task = company_mode.project_tasks(company_mode.load_state(self.path), project["id"])[0]
        company_mode.update_task_status(
            task["id"], "done", result="did the thing", artifacts=["github: pr/1"], path=self.path
        )

        update_mock = MagicMock(return_value=({}, None))
        comment_mock = MagicMock(return_value=({}, None))
        with patch.object(company_linear.linear_helpers, "is_configured", lambda: True), \
                patch.object(company_linear.linear_helpers, "update_issue_status", update_mock), \
                patch.object(company_linear.linear_helpers, "add_comment", comment_mock):
            company_linear.sync_task_status(task["id"], "done", "in_progress", path=self.path)

        self.assertEqual(update_mock.call_args.args[1], "Done")
        comment_body = comment_mock.call_args.args[1]
        self.assertIn("did the thing", comment_body)
        self.assertIn("pr/1", comment_body)

    def test_sync_status_noop_when_task_not_mirrored(self):
        project = _seed_project(self.path)
        task = company_mode.project_tasks(company_mode.load_state(self.path), project["id"])[0]
        update_mock = MagicMock()
        with patch.object(company_linear.linear_helpers, "is_configured", lambda: True), \
                patch.object(company_linear.linear_helpers, "update_issue_status", update_mock):
            company_linear.sync_task_status(task["id"], "done", "in_progress", path=self.path)
        update_mock.assert_not_called()

    def test_register_wires_company_mode_hook(self):
        project = _seed_project(self.path)
        calls = []
        with patch.object(company_linear, "sync_task_status",
                          lambda *a, **k: calls.append(a)):
            company_linear.register()
            task = company_mode.project_tasks(company_mode.load_state(self.path), project["id"])[0]
            company_mode.update_task_status(task["id"], "in_progress", path=self.path)

        self.assertTrue(any(c[0] == task["id"] and c[1] == "in_progress" for c in calls))


if __name__ == "__main__":
    unittest.main()

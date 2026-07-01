import tempfile
import unittest
from pathlib import Path

import company_mode


class CompanyModeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.state_path = Path(self.tmpdir.name) / "company_state.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_load_state_defaults_without_file(self):
        state = company_mode.load_state(self.state_path)

        self.assertEqual(state["company"]["mode"], "running")
        self.assertEqual(state["company"]["daily_budget_usd"], 0.0)
        self.assertEqual(state["projects"], [])
        self.assertEqual(state["tasks"], [])

    def test_set_budget_and_assign_goal_reserves_budget(self):
        result = company_mode.set_daily_budget(20, self.state_path)
        self.assertIn("$20.00", result)

        assigned = company_mode.assign_goal(
            "Build a tiny paid landing page product",
            configured_agent_keys=["manager", "code", "research"],
            specialist_keys=["code", "research", "write", "tasks"],
            path=self.state_path,
        )
        state = company_mode.load_state(self.state_path)

        self.assertIn("Company goal accepted", assigned)
        self.assertEqual(len(state["projects"]), 1)
        self.assertEqual(len(state["tasks"]), 4)
        self.assertEqual(state["company"]["reserved_today_usd"], 8.0)
        self.assertEqual(company_mode.remaining_budget(state), 12.0)

    def test_assign_blocks_when_budget_is_too_small(self):
        company_mode.set_daily_budget(2, self.state_path)
        result = company_mode.assign_goal(
            "Build a marketplace",
            configured_agent_keys=["manager"],
            specialist_keys=["code", "research", "write", "tasks"],
            path=self.state_path,
        )

        self.assertIn("Blocked", result)
        self.assertEqual(company_mode.load_state(self.state_path)["projects"], [])

    def test_task_status_transition_moves_reserved_to_spent(self):
        company_mode.set_daily_budget(20, self.state_path)
        company_mode.assign_goal(
            "Ship a sellable artifact",
            configured_agent_keys=["manager", "code"],
            specialist_keys=["code", "research", "write", "tasks"],
            path=self.state_path,
        )
        state = company_mode.load_state(self.state_path)
        task_id = state["tasks"][0]["id"]

        result = company_mode.update_task_status(task_id, "done", result="PR opened", path=self.state_path)
        state = company_mode.load_state(self.state_path)

        self.assertIn("updated to done", result)
        self.assertEqual(state["tasks"][0]["spent_usd"], 2.0)
        self.assertEqual(state["company"]["reserved_today_usd"], 6.0)
        self.assertEqual(state["company"]["spent_today_usd"], 2.0)

    def test_command_parsing_and_pause_resume(self):
        self.assertEqual(company_mode.parse_company_command("/setbudget $25"), ("/setbudget", "$25"))
        self.assertIsNone(company_mode.parse_company_command("hello team"))

        paused = company_mode.handle_company_command(
            "/pausecompany",
            configured_agent_keys=["manager"],
            specialist_keys=[],
            path=self.state_path,
        )
        blocked = company_mode.handle_company_command(
            "/assign Build something",
            configured_agent_keys=["manager"],
            specialist_keys=[],
            path=self.state_path,
        )
        resumed = company_mode.handle_company_command(
            "/resumecompany",
            configured_agent_keys=["manager"],
            specialist_keys=[],
            path=self.state_path,
        )

        self.assertIn("paused", paused.lower())
        self.assertIn("paused", blocked.lower())
        self.assertIn("resumed", resumed.lower())

    def test_daily_report_and_roster_delivery(self):
        report = company_mode.build_daily_report(self.state_path)
        roster = company_mode.configured_roster(["manager", "code"], ["code", "write"])

        self.assertIn("Daily Company Report", report)
        self.assertTrue(roster["code"]["speaks_as_self"])
        self.assertFalse(roster["write"]["speaks_as_self"])
        self.assertEqual(roster["write"]["delivery"], "via_miles")

    def test_record_delegation_adds_done_task_to_active_project(self):
        company_mode.set_daily_budget(20, self.state_path)
        company_mode.assign_goal(
            "Validate a product idea",
            configured_agent_keys=["manager", "research"],
            specialist_keys=["research"],
            path=self.state_path,
        )

        task_id = company_mode.record_delegation("research", "Check buyer pain", "Found demand.", self.state_path)
        state = company_mode.load_state(self.state_path)
        recorded = [task for task in state["tasks"] if task["id"] == task_id][0]

        self.assertEqual(recorded["status"], "done")
        self.assertEqual(recorded["owner"], "research")
        self.assertIn("Found demand", recorded["result"])


if __name__ == "__main__":
    unittest.main()

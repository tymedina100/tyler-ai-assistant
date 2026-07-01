import os
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
        self.assertEqual(state["company"]["daily_budget_usd"], company_mode.DEFAULT_DAILY_BUDGET_USD)
        self.assertEqual(state["projects"], [])
        self.assertEqual(state["tasks"], [])

    def test_data_dir_honors_env_override(self):
        original = os.environ.pop("DATA_DIR", None)
        try:
            self.assertEqual(company_mode._data_dir(), company_mode.BASE_DIR)
            os.environ["DATA_DIR"] = self.tmpdir.name
            self.assertEqual(company_mode._data_dir(), Path(self.tmpdir.name))
        finally:
            os.environ.pop("DATA_DIR", None)
            if original is not None:
                os.environ["DATA_DIR"] = original

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
        self.assertEqual(state["company"]["reserved_today_usd"], 4.0)  # 4 tasks x $1 reserve
        self.assertEqual(company_mode.remaining_budget(state), 16.0)

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
        self.assertEqual(state["tasks"][0]["spent_usd"], 1.0)  # defaults to the $1 reserve
        self.assertEqual(state["company"]["reserved_today_usd"], 3.0)  # 4 - this task's 1
        self.assertEqual(state["company"]["spent_today_usd"], 1.0)

    def test_command_parsing_and_pause_resume(self):
        self.assertEqual(company_mode.parse_company_command("/setbudget $25"), ("/setbudget", "$25"))
        self.assertIsNone(company_mode.parse_company_command("hello team"))

    def test_parse_company_command_strips_bot_mention(self):
        # Telegram appends @botname to commands in multi-bot groups.
        self.assertEqual(company_mode.parse_company_command("/dailyreport@TyManagerBot"), ("/dailyreport", ""))
        self.assertEqual(company_mode.parse_company_command("/setbudget@TyManagerBot 25"), ("/setbudget", "25"))

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

    # --- v2: checkpointed execution + metered spend ---

    def _assign(self, goal="Ship a sellable artifact", configured=("manager", "code")):
        company_mode.set_daily_budget(20, self.state_path)
        company_mode.assign_goal(
            goal,
            configured_agent_keys=list(configured),
            specialist_keys=["code", "research", "write", "tasks"],
            path=self.state_path,
        )

    def test_assign_goal_accepts_dynamic_task_plan(self):
        company_mode.set_daily_budget(20, self.state_path)
        plan = [("research", "Validate the niche"), ("write", "Draft the sales copy")]
        result = company_mode.assign_goal(
            "Some goal", ["manager", "research", "write"],
            specialist_keys=["research", "write"], path=self.state_path, tasks=plan,
        )
        state = company_mode.load_state(self.state_path)

        self.assertIn("Company goal accepted", result)
        self.assertEqual([t["owner"] for t in state["tasks"]], ["research", "write"])
        self.assertEqual([t["title"] for t in state["tasks"]], ["Validate the niche", "Draft the sales copy"])
        self.assertEqual(state["company"]["reserved_today_usd"], 2.0)  # 2 tasks x $1

    def test_assign_goal_falls_back_to_default_when_no_plan(self):
        company_mode.set_daily_budget(20, self.state_path)
        company_mode.assign_goal(
            "Some goal", ["manager"], specialist_keys=["code"], path=self.state_path, tasks=None,
        )
        state = company_mode.load_state(self.state_path)
        self.assertEqual(len(state["tasks"]), len(company_mode.DEFAULT_ASSIGN_TASKS))

    def test_link_sync_revenue_and_pnl(self):
        company_mode.set_daily_budget(20, self.state_path)
        company_mode.assign_goal(
            "Build a widget", ["manager", "code"], specialist_keys=["code"], path=self.state_path,
        )
        state = company_mode.load_state(self.state_path)
        task_id = state["tasks"][0]["id"]
        company_mode.update_task_status(task_id, "done", spent_usd=1.5, path=self.state_path)

        msg = company_mode.link_product("https://tymedina.gumroad.com/l/widget", self.state_path)
        self.assertIn("Linked", msg)

        gumroad = [{
            "id": "P1", "short_url": "https://tymedina.gumroad.com/l/widget",
            "sales_count": 3, "sales_usd_cents": 5700,
        }]
        company_mode.sync_revenue(gumroad, self.state_path)
        state = company_mode.load_state(self.state_path)
        product = state["products"][0]
        self.assertEqual(product["sales_count"], 3)
        self.assertEqual(product["revenue_usd"], 57.0)
        self.assertEqual(product["gumroad_product_id"], "P1")

        rows, totals = company_mode.product_pnl(state)
        self.assertEqual(rows[0]["spend"], 1.5)
        self.assertEqual(rows[0]["revenue"], 57.0)
        self.assertEqual(rows[0]["net"], 55.5)
        self.assertEqual(totals["net"], 55.5)
        self.assertIn("net +$55.50", company_mode.render_pnl(self.state_path))

    def test_link_product_requires_a_project(self):
        result = company_mode.link_product("https://x.gumroad.com/l/y", self.state_path)
        self.assertIn("No project", result)

    def test_assigned_project_starts_proposed(self):
        self._assign()
        state = company_mode.load_state(self.state_path)
        self.assertEqual(company_mode.active_project(state)["status"], "proposed")

    def test_approve_project_activates_and_returns_id(self):
        self._assign()
        message, project_id = company_mode.approve_project(self.state_path)
        self.assertIn("Approved", message)
        self.assertIsNotNone(project_id)
        state = company_mode.load_state(self.state_path)
        self.assertEqual(company_mode.active_project(state)["status"], "active")

    def test_task_status_reconciles_actual_spend(self):
        self._assign()
        state = company_mode.load_state(self.state_path)
        task_id = state["tasks"][0]["id"]

        company_mode.update_task_status(task_id, "done", result="done", spent_usd=0.5, path=self.state_path)
        state = company_mode.load_state(self.state_path)
        task = next(t for t in state["tasks"] if t["id"] == task_id)

        self.assertEqual(task["spent_usd"], 0.5)  # actual, not the $1 reserve
        self.assertEqual(state["company"]["spent_today_usd"], 0.5)
        self.assertEqual(state["company"]["reserved_today_usd"], 3.0)  # 4 - this task's 1 reserve

    def test_next_planned_task_advances_after_completion(self):
        self._assign()
        state = company_mode.load_state(self.state_path)
        project = company_mode.active_project(state)
        first = company_mode.next_planned_task(state, project["id"])
        self.assertIsNotNone(first)

        company_mode.update_task_status(first["id"], "done", spent_usd=0.1, path=self.state_path)
        state = company_mode.load_state(self.state_path)
        second = company_mode.next_planned_task(state, project["id"])
        self.assertNotEqual(first["id"], second["id"])

    def test_mark_task_blocked_records_spend_and_artifacts(self):
        self._assign()
        state = company_mode.load_state(self.state_path)
        task_id = state["tasks"][0]["id"]

        company_mode.mark_task_blocked(
            task_id, "needs approval", spent_usd=0.25,
            artifacts=["file: files/x.txt"], path=self.state_path,
        )
        state = company_mode.load_state(self.state_path)
        task = next(t for t in state["tasks"] if t["id"] == task_id)

        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["spent_usd"], 0.25)
        self.assertIn("file: files/x.txt", task["artifacts"])
        self.assertIn("file: files/x.txt", company_mode.active_project(state)["artifacts"])

    def test_cancel_project_releases_reserve(self):
        self._assign()
        result = company_mode.cancel_project(self.state_path)
        self.assertIn("Cancelled", result)
        state = company_mode.load_state(self.state_path)
        self.assertEqual(state["company"]["reserved_today_usd"], 0.0)
        self.assertIsNone(state["company"]["active_project_id"])

    def test_record_adhoc_spend_counts_against_budget(self):
        self._assign()
        company_mode.record_adhoc_spend(1.5, artifacts=["file: files/n.txt"], path=self.state_path)
        state = company_mode.load_state(self.state_path)
        self.assertEqual(state["company"]["spent_today_usd"], 1.5)
        self.assertIn("file: files/n.txt", company_mode.active_project(state)["artifacts"])

    def test_prior_work_summary_chains_completed_tasks(self):
        self._assign()
        state = company_mode.load_state(self.state_path)
        project = company_mode.active_project(state)
        tasks = company_mode.project_tasks(state, project["id"])
        first, second = tasks[0], tasks[1]

        company_mode.update_task_status(
            first["id"], "done", result="Found strong demand from freelancers.",
            artifacts=["file: files/pack.md"], path=self.state_path,
        )
        state = company_mode.load_state(self.state_path)
        summary = company_mode.prior_work_summary(state, project["id"], second["id"])

        # The completed task's result + deliverable are handed forward...
        self.assertIn("Found strong demand", summary)
        self.assertIn("file: files/pack.md", summary)
        # ...but a still-planned task contributes nothing yet.
        self.assertNotIn(second["title"], summary)

    def test_prior_work_summary_empty_when_nothing_done(self):
        self._assign()
        state = company_mode.load_state(self.state_path)
        project = company_mode.active_project(state)
        first = company_mode.project_tasks(state, project["id"])[0]
        self.assertEqual(company_mode.prior_work_summary(state, project["id"], first["id"]), "")

    def test_mark_project_published_sets_status(self):
        self._assign()
        company_mode.approve_project(self.state_path)
        msg = company_mode.mark_project_published(self.state_path)
        self.assertIn("published", msg.lower())
        state = company_mode.load_state(self.state_path)
        self.assertEqual(company_mode.active_project(state)["status"], "published")

    def test_publish_command_is_recognized(self):
        self.assertEqual(company_mode.parse_company_command("/publish"), ("/publish", ""))

    def test_record_delegation_can_meter_spend(self):
        self._assign(configured=("manager", "research"))
        company_mode.record_delegation(
            "research", "look into demand", "found some", self.state_path,
            spent_usd=0.3, artifacts=["note"],
        )
        state = company_mode.load_state(self.state_path)
        self.assertEqual(state["company"]["spent_today_usd"], 0.3)


if __name__ == "__main__":
    unittest.main()

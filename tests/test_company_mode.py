import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

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
        original_rv = os.environ.pop("RAILWAY_VOLUME_MOUNT_PATH", None)
        try:
            self.assertEqual(company_mode._data_dir(), company_mode.BASE_DIR)
            os.environ["DATA_DIR"] = self.tmpdir.name
            self.assertEqual(company_mode._data_dir(), Path(self.tmpdir.name))
            # With DATA_DIR unset, it falls back to the Railway volume mount path.
            os.environ.pop("DATA_DIR", None)
            os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = self.tmpdir.name
            self.assertEqual(company_mode._data_dir(), Path(self.tmpdir.name))
        finally:
            os.environ.pop("DATA_DIR", None)
            os.environ.pop("RAILWAY_VOLUME_MOUNT_PATH", None)
            if original is not None:
                os.environ["DATA_DIR"] = original
            if original_rv is not None:
                os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = original_rv

    def test_set_budget_and_assign_goal_reserves_budget(self):
        result = company_mode.set_daily_budget(20, self.state_path)
        self.assertIn("$20.00", result)

        assigned = company_mode.assign_goal(
            "Build a tiny paid landing page product",
            configured_agent_keys=["manager", "code", "research"],
            specialist_keys=["code", "research", "write", "task", "editor"],
            path=self.state_path,
        )
        state = company_mode.load_state(self.state_path)

        self.assertIn("Company goal accepted", assigned)
        self.assertEqual(len(state["projects"]), 1)
        self.assertEqual(len(state["tasks"]), 4)
        self.assertEqual(state["company"]["reserved_today_usd"], 4.0)  # 4 tasks x $1 reserve
        # The configured emergency reserve is intentionally unavailable to ordinary tasks.
        self.assertEqual(company_mode.remaining_budget(state), 15.75)

    def test_assign_blocks_when_budget_is_too_small(self):
        company_mode.set_daily_budget(2, self.state_path)
        result = company_mode.assign_goal(
            "Build a marketplace",
            configured_agent_keys=["manager"],
            specialist_keys=["code", "research", "write", "task", "editor"],
            path=self.state_path,
        )

        self.assertIn("Blocked", result)
        self.assertEqual(company_mode.load_state(self.state_path)["projects"], [])

    def test_task_status_transition_moves_reserved_to_spent(self):
        company_mode.set_daily_budget(20, self.state_path)
        company_mode.assign_goal(
            "Ship a sellable artifact",
            configured_agent_keys=["manager", "code"],
            specialist_keys=["code", "research", "write", "task", "editor"],
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
            specialist_keys=["code", "research", "write", "task", "editor"],
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

    def test_build_task_prompt_injects_deliverable_content(self):
        project = {"id": "proj_1", "goal": "Ship a guide"}
        task = {"owner": "write", "title": "Refine the copy"}

        prompt = company_mode.build_task_prompt(
            project, task,
            prior_work="- code (Build): done",
            deliverable_name="guide.md",
            deliverable_content="ACTUAL FILE BODY that the writer must extend.",
        )

        # The real file content and name are in the prompt so the agent extends it.
        self.assertIn("ACTUAL FILE BODY", prompt)
        self.assertIn("guide.md", prompt)
        self.assertIn("same file", prompt.lower())
        self.assertIn("code (Build)", prompt)  # prior-work summary still included

    def test_build_task_prompt_truncates_long_content(self):
        project = {"id": "p", "goal": "g"}
        task = {"owner": "write", "title": "t"}
        big = "x" * (company_mode.DELIVERABLE_INJECT_CHARS + 500)
        prompt = company_mode.build_task_prompt(project, task, deliverable_name="f.md", deliverable_content=big)
        self.assertIn("[truncated]", prompt)
        self.assertEqual(prompt.count("x"), company_mode.DELIVERABLE_INJECT_CHARS)

    def test_autonomous_prompts_stop_on_external_dependencies_without_revision_loops(self):
        project = {"id": "p", "goal": "Validate supplied run evidence"}
        worker = {
            "owner": "general",
            "title": "Draft the validation",
            "enforce_authorization": True,
            "authorization_level": "propose",
        }
        editor = {
            "owner": "editor",
            "title": "Review the validation",
            "enforce_authorization": True,
            "authorization_level": "observe",
        }

        worker_prompt = company_mode.build_task_prompt(project, worker)
        editor_prompt = company_mode.build_task_prompt(project, editor)

        self.assertIn("BLOCKED - NEEDS HUMAN REVIEW", worker_prompt)
        self.assertIn("MISSING_ACCESS", worker_prompt)
        self.assertIn("REVISIONS REQUIRED only", editor_prompt)
        self.assertIn("Do not spend a revision round", editor_prompt)

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

    def test_assign_blocks_while_a_project_is_still_open(self):
        self._assign()
        result = company_mode.assign_goal(
            "A second goal", ["manager", "code"],
            specialist_keys=["code", "research", "write", "task", "editor"],
            path=self.state_path,
        )
        self.assertIn("Blocked", result)
        state = company_mode.load_state(self.state_path)
        self.assertEqual(len(state["projects"]), 1)  # the second goal was never created

    def test_assign_allowed_again_after_cancel(self):
        self._assign()
        company_mode.cancel_project(self.state_path)
        result = company_mode.assign_goal(
            "A second goal", ["manager", "code"],
            specialist_keys=["code", "research", "write", "task", "editor"],
            path=self.state_path,
        )
        self.assertIn("Company goal accepted", result)

    def test_cancel_by_id_reaches_a_project_no_longer_tracked_as_active(self):
        # Simulate the orphaning bug this guards against: an old open project whose
        # id has fallen out of active_project_id (e.g. a pre-guard state file).
        self._assign(goal="Old orphaned goal")
        state = company_mode.load_state(self.state_path)
        orphan_id = state["company"]["active_project_id"]
        state["company"]["active_project_id"] = None  # simulate it being superseded
        company_mode.save_state(state, self.state_path)

        result = company_mode.cancel_project(self.state_path, project_id=orphan_id)
        self.assertIn("Cancelled", result)
        state = company_mode.load_state(self.state_path)
        self.assertEqual(state["company"]["reserved_today_usd"], 0.0)
        self.assertEqual(company_mode.open_projects(state), [])

    def test_render_company_status_surfaces_stray_open_projects(self):
        self._assign(goal="Old orphaned goal")
        state = company_mode.load_state(self.state_path)
        state["company"]["active_project_id"] = None
        company_mode.save_state(state, self.state_path)

        status = company_mode.render_company_status(self.state_path)
        self.assertIn("older open project(s)", status)
        self.assertIn("/cancel proj_", status)

    def test_editor_revision_flag_surfaces_in_status_and_report(self):
        self._assign()
        state = company_mode.load_state(self.state_path)
        project_id = state["company"]["active_project_id"]

        company_mode.set_project_revision_flag(project_id, "APPROVED - looks solid.", self.state_path)
        self.assertNotIn("REVISIONS REQUIRED", company_mode.render_company_status(self.state_path))

        company_mode.set_project_revision_flag(project_id, "REVISIONS REQUIRED\n1. Fix X.", self.state_path)
        self.assertIn("REVISIONS REQUIRED", company_mode.render_company_status(self.state_path))
        self.assertIn("REVISIONS REQUIRED", company_mode.build_daily_report(self.state_path))

    def test_start_revision_round_queues_owners_plus_editor(self):
        self._assign()  # default plan: research, code, write, editor
        state = company_mode.load_state(self.state_path)
        project_id = state["company"]["active_project_id"]
        for task in company_mode.project_tasks(state, project_id):
            company_mode.update_task_status(task["id"], "done", spent_usd=0.1, path=self.state_path)
        company_mode.set_project_revision_flag(project_id, "REVISIONS REQUIRED\n1. Fix it.", self.state_path)

        created, note = company_mode.start_revision_round(project_id, ["manager", "code"], self.state_path)
        self.assertTrue(created)
        self.assertIn("round 1", note)

        state = company_mode.load_state(self.state_path)
        new_tasks = [t for t in company_mode.project_tasks(state, project_id) if t["status"] == "planned"]
        owners = [t["owner"] for t in new_tasks]
        # one task per original non-editor owner, replayed in order, then a fresh editor pass
        self.assertEqual(owners, ["research", "code", "write", "editor"])
        self.assertEqual(state["company"]["reserved_today_usd"], 4.0)  # 4 new tasks x $1

    def test_start_revision_round_blocked_when_budget_too_small(self):
        # $5 covers the initial 4-task plan ($4) but leaves only $1 - not enough
        # for a second round (also 4 tasks: the 3 non-editor owners + a re-review).
        company_mode.set_daily_budget(5, self.state_path)
        self._assign_no_budget_reset()
        state = company_mode.load_state(self.state_path)
        project_id = state["company"]["active_project_id"]
        company_mode.set_project_revision_flag(project_id, "REVISIONS REQUIRED", self.state_path)

        created, note = company_mode.start_revision_round(project_id, ["manager", "code"], self.state_path)
        self.assertFalse(created)
        self.assertIn("not enough budget", note)

    def _assign_no_budget_reset(self):
        company_mode.assign_goal(
            "Ship a sellable artifact", ["manager", "code"],
            specialist_keys=["code", "research", "write", "task", "editor"],
            path=self.state_path,
        )

    def test_prior_work_summary_keeps_each_historical_editor_result(self):
        self._assign()
        state = company_mode.load_state(self.state_path)
        project_id = state["company"]["active_project_id"]
        tasks = company_mode.project_tasks(state, project_id)
        editor_task = next(t for t in tasks if t["owner"] == "editor")
        other_task = next(t for t in tasks if t["owner"] != "editor")

        long_feedback = "REVISIONS REQUIRED\n" + "\n".join(
            f"{i}. Fix item {i} because the copy is inaccurate and needs a citation." for i in range(1, 40)
        )
        self.assertGreater(len(long_feedback), 1000)  # longer than the stored task result would allow

        company_mode.update_task_status(
            editor_task["id"], "done", "STORED HISTORICAL EDITOR RESULT", path=self.state_path
        )
        company_mode.set_project_revision_flag(project_id, long_feedback, self.state_path)

        state = company_mode.load_state(self.state_path)
        summary = company_mode.prior_work_summary(state, project_id, other_task["id"])
        self.assertIn("STORED HISTORICAL EDITOR RESULT", summary)
        self.assertNotIn("Fix item 39", summary)

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

    def test_editor_context_preserves_full_worker_result_over_display_limit(self):
        self._assign()
        state = company_mode.load_state(self.state_path)
        project = company_mode.active_project(state)
        tasks = company_mode.project_tasks(state, project["id"])
        worker = next(task for task in tasks if task["owner"] != "editor")
        editor = next(task for task in tasks if task["owner"] == "editor")
        long_result = "start-" + ("x" * 6000) + "-review-tail"
        self.assertGreater(len(long_result), 5000)

        company_mode.update_task_status(
            worker["id"], "done", result=long_result, path=self.state_path
        )
        state = company_mode.load_state(self.state_path)
        persisted = next(task for task in state["tasks"] if task["id"] == worker["id"])
        summary = company_mode.prior_work_summary(state, project["id"], editor["id"])

        self.assertEqual(persisted["result"], long_result)
        self.assertFalse(persisted["result_truncated"])
        self.assertIn("LATEST REVIEW CANDIDATE", summary)
        self.assertIn("review-tail", summary)
        self.assertNotIn("...[truncated]", summary)

    def test_oversized_task_result_is_capped_in_state_and_reviewer_context(self):
        self._assign()
        state = company_mode.load_state(self.state_path)
        project = company_mode.active_project(state)
        tasks = company_mode.project_tasks(state, project["id"])
        worker = next(task for task in tasks if task["owner"] != "editor")
        editor = next(task for task in tasks if task["owner"] == "editor")
        oversized = (
            "result-start-"
            + ("x" * company_mode.MAX_TASK_STORED_RESULT_CHARS)
            + "-result-tail"
        )

        company_mode.update_task_status(
            worker["id"], "done", result=oversized, path=self.state_path
        )
        state = company_mode.load_state(self.state_path)
        persisted = next(task for task in state["tasks"] if task["id"] == worker["id"])
        summary = company_mode.prior_work_summary(state, project["id"], editor["id"])

        self.assertEqual(len(persisted["result"]), company_mode.MAX_TASK_STORED_RESULT_CHARS)
        self.assertTrue(persisted["result_truncated"])
        self.assertNotIn("result-tail", persisted["result"])
        self.assertIn("LATEST REVIEW CANDIDATE", summary)
        self.assertIn("...[truncated]", summary)
        self.assertNotIn("result-tail", summary)

    def test_state_normalization_caps_task_result_and_revision_feedback(self):
        state = company_mode.new_state()
        state["tasks"] = [{
            "id": "task_oversized",
            "result": "r" * (company_mode.MAX_TASK_STORED_RESULT_CHARS + 100),
            "revision_feedback": "f" * (company_mode.MAX_REVIEW_FEEDBACK_CHARS + 100),
        }]

        normalized = company_mode.normalize_state(state)
        task = normalized["tasks"][0]

        self.assertEqual(len(task["result"]), company_mode.MAX_TASK_STORED_RESULT_CHARS)
        self.assertTrue(task["result_truncated"])
        self.assertEqual(
            len(task["revision_feedback"]), company_mode.MAX_REVIEW_FEEDBACK_CHARS
        )
        self.assertTrue(task["revision_feedback_truncated"])

    def test_review_feedback_and_history_are_capped(self):
        self._assign()
        project_id = company_mode.load_state(self.state_path)["company"]["active_project_id"]
        total_reviews = company_mode.MAX_EDITOR_FEEDBACK_HISTORY + 3
        for attempt in range(total_reviews):
            feedback = (
                f"APPROVED {attempt}: "
                + ("f" * company_mode.MAX_REVIEW_FEEDBACK_CHARS)
                + "-feedback-tail"
            )
            company_mode.set_project_revision_flag(project_id, feedback, self.state_path)

        state = company_mode.load_state(self.state_path)
        project = next(item for item in state["projects"] if item["id"] == project_id)
        history = project["editor_feedback_history"]

        self.assertEqual(len(project["last_editor_feedback"]), company_mode.MAX_REVIEW_FEEDBACK_CHARS)
        self.assertTrue(project["last_editor_feedback_truncated"])
        self.assertNotIn("feedback-tail", project["last_editor_feedback"])
        self.assertEqual(len(history), company_mode.MAX_EDITOR_FEEDBACK_HISTORY)
        self.assertTrue(project["editor_feedback_history_truncated"])
        self.assertEqual(history[0]["attempt"], total_reviews - len(history) + 1)
        self.assertTrue(all(entry["feedback_truncated"] for entry in history))
        self.assertTrue(all(
            len(entry["feedback"]) <= company_mode.MAX_REVIEW_FEEDBACK_CHARS
            for entry in history
        ))

    def test_configured_limit_honors_hard_maximum(self):
        with mock.patch.dict(os.environ, {"TEST_CHAR_LIMIT": "999999"}):
            value = company_mode._configured_positive_int(
                "TEST_CHAR_LIMIT", 100, maximum=50000
            )
        self.assertEqual(value, 50000)

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

    def test_normalize_state_migrates_retired_task_owners(self):
        state = company_mode.new_state()
        state["tasks"] = [
            {"id": "t1", "owner": "news"},
            {"id": "t2", "owner": "tasks"},
            {"id": "t3", "owner": "weather"},
            {"id": "t4", "owner": "code"},
        ]

        normalized = company_mode.normalize_state(state)

        self.assertEqual(
            [t["owner"] for t in normalized["tasks"]],
            ["research", "task", "task", "code"],
        )

    def test_record_delegation_can_meter_spend(self):
        self._assign(configured=("manager", "research"))
        company_mode.record_delegation(
            "research", "look into demand", "found some", self.state_path,
            spent_usd=0.3, artifacts=["note"],
        )
        state = company_mode.load_state(self.state_path)
        self.assertEqual(state["company"]["spent_today_usd"], 0.3)

    # --- hardened persistence, atomic budget ledger, and bounded review ---

    def test_corrupt_state_is_quarantined_and_never_resets_budget_silently(self):
        self.state_path.write_text('{"company": ', encoding="utf-8")

        with self.assertRaises(company_mode.StateCorruptionError) as caught:
            company_mode.load_state(self.state_path)

        self.assertFalse(self.state_path.exists())
        self.assertTrue(caught.exception.quarantine_path.exists())
        self.assertEqual(caught.exception.quarantine_path.read_text(encoding="utf-8"), '{"company": ')

    def test_budget_date_uses_configured_timezone(self):
        instant = datetime(2026, 1, 1, 7, 30, tzinfo=timezone.utc)
        with mock.patch.dict(os.environ, {"TIMEZONE": "America/Phoenix"}):
            self.assertEqual(company_mode.today_key(instant), "2026-01-01")
        with mock.patch.dict(os.environ, {"TIMEZONE": "Pacific/Honolulu"}):
            self.assertEqual(company_mode.today_key(instant), "2025-12-31")

    def test_emergency_reserve_is_excluded_from_ordinary_work(self):
        with mock.patch.dict(os.environ, {"COMPANY_EMERGENCY_RESERVE_USD": "0.25"}):
            company_mode.set_daily_budget(1.0, self.state_path)
            self.assertEqual(company_mode.remaining_budget(company_mode.load_state(self.state_path)), 0.75)
            company_mode.reserve_budget(0.75, self.state_path, context="task")
            with self.assertRaises(company_mode.BudgetExceededError):
                company_mode.reserve_budget(0.01, self.state_path, context="task")
            emergency = company_mode.reserve_budget(
                0.25, self.state_path, context="escalation", allow_emergency=True
            )
            self.assertTrue(emergency["uses_emergency_reserve"])

    def test_reserve_reconcile_and_release_persist_attribution_usage_and_precision(self):
        company_mode.set_daily_budget(10, self.state_path)
        reservation = company_mode.reserve_budget(
            1.1234564,
            self.state_path,
            context="task",
            project_id="project-a",
            task_id="task-a",
            agent="engineer",
            model="small-model",
            reason="implementation",
        )
        self.assertEqual(reservation["amount_usd"], 1.123456)

        cost = company_mode.reconcile_budget(
            reservation["id"],
            0.2345678,
            self.state_path,
            usage_records=[{"prompt_tokens": 11, "completion_tokens": 7}],
        )
        self.assertEqual(cost["amount_usd"], 0.234568)
        self.assertEqual(cost["cost_basis"], "actual")
        self.assertEqual(cost["total_tokens"], 18)
        self.assertEqual(cost["project_id"], "project-a")
        self.assertEqual(cost["model"], "small-model")

        released = company_mode.reserve_budget(0.5, self.state_path, reason="optional")
        company_mode.release_budget(released["id"], self.state_path, reason="deferred")
        state = company_mode.load_state(self.state_path)
        self.assertEqual(state["company"]["reserved_today_usd"], 0.0)
        self.assertEqual(state["company"]["spent_today_usd"], 0.234568)
        self.assertEqual(state["budget_reservations"][-1]["status"], "released")

    def test_reconcile_persists_bounded_structured_model_route_decisions(self):
        company_mode.set_daily_budget(10, self.state_path)
        reservation = company_mode.reserve_budget(
            0.5, self.state_path, context="telegram manager request"
        )
        decisions = []
        for index in range(company_mode.MAX_MODEL_ROUTE_DECISIONS + 2):
            decisions.append({
                "agent": f"agent-{index}",
                "task_type": "coding",
                "complexity": "standard",
                "risk": "low",
                "uses_tools": True,
                "tool_count": 4,
                "estimated_input_tokens": 3000,
                "estimated_output_tokens": 800,
                "remaining_budget_usd": 0.45,
                "model": "gpt-5.4-mini",
                "model_level": "standard",
                "estimated_cost_usd": 0.006,
                "status": "selected",
                "deferral_reason": "",
                "reason": (
                    "Selected the lowest-cost capable model."
                    if index < company_mode.MAX_MODEL_ROUTE_DECISIONS + 1
                    else "r" * (company_mode.MAX_MODEL_ROUTE_REASON_CHARS + 50)
                ),
            })

        cost = company_mode.reconcile_budget(
            reservation["id"],
            0.01,
            self.state_path,
            model_route_decisions=[None, "ignored", *decisions],
        )

        self.assertEqual(
            len(cost["model_route_decisions"]),
            company_mode.MAX_MODEL_ROUTE_DECISIONS,
        )
        self.assertTrue(cost["model_route_decisions_truncated"])
        self.assertEqual(cost["model_route_decisions"][0]["agent"], "agent-2")
        latest = cost["model_route_decisions"][-1]
        self.assertEqual(latest["model"], "gpt-5.4-mini")
        self.assertEqual(latest["estimated_input_tokens"], 3000)
        self.assertEqual(len(latest["reason"]), company_mode.MAX_MODEL_ROUTE_REASON_CHARS)
        self.assertTrue(latest["reason_truncated"])

        persisted = company_mode.load_state(self.state_path)["cost_entries"][-1]
        self.assertEqual(persisted["model_route_decisions"], cost["model_route_decisions"])
        self.assertTrue(persisted["model_route_decisions_truncated"])

    def test_record_adhoc_spend_can_persist_zero_cost_route_deferral(self):
        company_mode.record_adhoc_spend(
            0.0,
            path=self.state_path,
            context="telegram route deferral",
            model_route_decisions=[{
                "agent": "router",
                "task_type": "routing",
                "complexity": "lightweight",
                "risk": "low",
                "uses_tools": False,
                "tool_count": 0,
                "estimated_input_tokens": "invalid",
                "estimated_output_tokens": 800,
                "remaining_budget_usd": "NaN",
                "model": "",
                "model_level": "",
                "estimated_cost_usd": 0.002,
                "status": "deferred",
                "deferral_reason": "insufficient_budget",
                "reason": "No capable model fits the remaining budget.",
            }],
        )

        entry = company_mode.load_state(self.state_path)["cost_entries"][-1]
        self.assertEqual(entry["amount_usd"], 0.0)
        self.assertEqual(entry["model_route_decisions"][0]["agent"], "router")
        self.assertEqual(
            entry["model_route_decisions"][0]["deferral_reason"],
            "insufficient_budget",
        )
        self.assertEqual(entry["model_route_decisions"][0]["estimated_input_tokens"], 0)
        self.assertEqual(entry["model_route_decisions"][0]["remaining_budget_usd"], 0.0)

    def test_concurrent_reservations_cannot_overspend(self):
        company_mode.set_daily_budget(2.25, self.state_path)  # $2 ordinary + $0.25 emergency

        def attempt(_):
            try:
                return company_mode.reserve_budget(0.5, self.state_path)["id"]
            except company_mode.BudgetExceededError:
                return None

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(attempt, range(12)))

        self.assertEqual(sum(value is not None for value in results), 4)
        state = company_mode.load_state(self.state_path)
        self.assertEqual(state["company"]["reserved_today_usd"], 2.0)
        self.assertEqual(len([r for r in state["budget_reservations"] if r["status"] == "reserved"]), 4)

    def test_concurrent_approval_cannot_overwrite_a_budget_reservation(self):
        company_mode.set_daily_budget(3.0, self.state_path)
        self._assign()

        with ThreadPoolExecutor(max_workers=2) as pool:
            approval = pool.submit(company_mode.approve_project, self.state_path, notify_hooks=False)
            reservation = pool.submit(
                company_mode.reserve_budget,
                0.10,
                self.state_path,
                context="concurrent-check",
            )
            approval.result()
            held = reservation.result()

        state = company_mode.load_state(self.state_path)
        self.assertEqual(company_mode.active_project(state)["status"], "active")
        self.assertTrue(any(value["id"] == held["id"] for value in state["budget_reservations"]))
        self.assertGreaterEqual(state["company"]["reserved_today_usd"], 0.10)

    def test_task_reservation_expands_atomically_without_using_emergency_budget(self):
        company_mode.set_daily_budget(1.0, self.state_path)
        company_mode.assign_goal(
            "Inspect safely",
            ["manager", "editor"],
            specialist_keys=["editor"],
            path=self.state_path,
            tasks=[
                {"owner": "manager", "title": "Inspect", "estimate_usd": 0.10},
                {"owner": "editor", "title": "Review", "estimate_usd": 0.10},
            ],
        )
        state = company_mode.load_state(self.state_path)
        first, second = state["tasks"]

        expanded = company_mode.expand_task_budget_reservation(
            first["id"], 0.40, 0.50, self.state_path
        )
        repeated = company_mode.expand_task_budget_reservation(
            first["id"], 0.40, 0.50, self.state_path
        )
        denied = company_mode.expand_task_budget_reservation(
            second["id"], 0.30, 0.50, self.state_path
        )
        final = company_mode.load_state(self.state_path)
        saved_first, saved_second = final["tasks"]

        self.assertTrue(expanded["expanded"])
        self.assertEqual(expanded["amount_usd"], 0.50)
        self.assertFalse(repeated["expanded"])
        self.assertEqual(repeated["reason"], "already_sufficient")
        self.assertEqual(saved_first["reserved_usd"], 0.50)
        self.assertFalse(denied["expanded"])
        self.assertEqual(denied["reason"], "insufficient_ordinary_budget")
        self.assertEqual(saved_second["reserved_usd"], 0.10)
        self.assertEqual(final["company"]["reserved_today_usd"], 0.60)
        self.assertEqual(company_mode.remaining_budget(final), 0.15)

        company_mode.update_task_status(
            first["id"], "done", spent_usd=0.42, path=self.state_path
        )
        reconciled = company_mode.load_state(self.state_path)
        self.assertEqual(reconciled["company"]["spent_today_usd"], 0.42)
        self.assertEqual(reconciled["company"]["reserved_today_usd"], 0.10)
        self.assertEqual(company_mode.remaining_budget(reconciled), 0.23)

    def test_concurrent_task_expansions_cannot_claim_the_same_budget(self):
        company_mode.set_daily_budget(1.0, self.state_path)
        company_mode.assign_goal(
            "Inspect concurrently",
            ["manager", "editor"],
            specialist_keys=["editor"],
            path=self.state_path,
            tasks=[
                {"owner": "manager", "title": "Inspect", "estimate_usd": 0.10},
                {"owner": "editor", "title": "Review", "estimate_usd": 0.10},
            ],
        )
        task_ids = [task["id"] for task in company_mode.load_state(self.state_path)["tasks"]]

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda task_id: company_mode.expand_task_budget_reservation(
                    task_id, 0.50, 0.50, self.state_path
                ),
                task_ids,
            ))

        final = company_mode.load_state(self.state_path)
        self.assertEqual(sum(result["expanded"] for result in results), 1)
        self.assertEqual(final["company"]["reserved_today_usd"], 0.60)
        self.assertEqual(company_mode.remaining_budget(final), 0.15)

    def test_persisted_company_output_redacts_common_github_credentials(self):
        self._assign()
        state = company_mode.load_state(self.state_path)
        task_id = state["tasks"][0]["id"]
        company_mode.update_task_status(
            task_id,
            "done",
            "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz123456",
            spent_usd=0.0,
            path=self.state_path,
        )

        raw = self.state_path.read_text(encoding="utf-8")

        self.assertNotIn("ghp_", raw)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456", raw)
        self.assertIn("[REDACTED]", raw)

    def test_assign_goal_accepts_rich_task_metadata_and_old_tuples(self):
        company_mode.set_daily_budget(10, self.state_path)
        plan = [
            ("research", "Legacy tuple"),
            {
                "owner": "code",
                "title": "Implement guarded change",
                "estimate_usd": 0.1256789,
                "acceptance_criteria": ["Focused tests pass"],
                "authorization_level": "modify_locally",
                "model": "standard-model",
                "model_reason": "Moderate coding task",
                "prior_model_fingerprint": "prior-1",
                "feedback_fingerprint": "feedback-1",
            },
        ]
        company_mode.assign_goal(
            "Harden one workflow", ["manager", "code", "research"], path=self.state_path, tasks=plan
        )
        state = company_mode.load_state(self.state_path)
        legacy, rich = state["tasks"]
        self.assertEqual(legacy["execution_attempts"], 0)
        self.assertEqual(rich["estimate_usd"], 0.125679)
        self.assertEqual(rich["acceptance_criteria"], ["Focused tests pass"])
        self.assertEqual(rich["authorization_level"], "modify_locally")
        self.assertEqual(rich["model_reason"], "Moderate coding task")
        self.assertEqual(rich["prior_model_fingerprints"], ["prior-1"])
        self.assertEqual(rich["feedback_fingerprints"], ["feedback-1"])

    def test_update_task_status_persists_usage_and_estimated_label(self):
        self._assign()
        task_id = company_mode.load_state(self.state_path)["tasks"][0]["id"]
        company_mode.update_task_status(
            task_id,
            "done",
            path=self.state_path,
            usage_records={"input_tokens": 9, "output_tokens": 4},
            model="small-model",
            model_reason="Routine extraction",
        )
        state = company_mode.load_state(self.state_path)
        task = next(item for item in state["tasks"] if item["id"] == task_id)
        cost = next(item for item in state["cost_entries"] if item["task_id"] == task_id)
        self.assertEqual(task["total_tokens"], 13)
        self.assertEqual(task["model"], "small-model")
        self.assertEqual(cost["cost_basis"], "estimated")
        self.assertEqual(cost["total_tokens"], 13)

    def test_update_task_status_persists_bounded_team_help_evidence(self):
        self._assign()
        task_id = company_mode.load_state(self.state_path)["tasks"][0]["id"]
        event = {
            "requesting_agent": "code",
            "helper_agent": "research",
            "question": "Confirm the primary-source requirement.",
            "reason": "The implementation depends on the policy source.",
            "response": "Use the official policy and record its retrieval date.",
            "helper_model": "gpt-5.4-nano",
            "model_reason": "A lightweight source check needs only text retrieval.",
            "task_type": "research",
            "complexity": "lightweight",
            "risk": "low",
            "status": "completed",
            "request_delivery": "direct",
            "routing_delivery": "direct",
            "response_delivery": "relayed_by_manager",
            "created_at": "2026-08-08T12:00:00+00:00",
            "completed_at": "2026-08-08T12:00:01+00:00",
            "input_tokens": 21,
            "output_tokens": 8,
            "cost_usd": 0.0001234,
        }

        company_mode.update_task_status(
            task_id,
            "in_progress",
            path=self.state_path,
            model="gpt-5.4-mini",
            model_reason="Standard implementation route.",
            team_help_events=[event],
        )
        # A caller may replay its complete evidence snapshot at terminal update.
        # Persist the exchange once rather than duplicating it.
        company_mode.update_task_status(
            task_id,
            "done",
            spent_usd=0.001,
            path=self.state_path,
            team_help_events=[event],
        )

        task = next(
            value for value in company_mode.load_state(self.state_path)["tasks"]
            if value["id"] == task_id
        )
        self.assertEqual(len(task["team_help_events"]), 1)
        persisted = task["team_help_events"][0]
        self.assertEqual(persisted["requesting_agent"], "code")
        self.assertEqual(persisted["helper_agent"], "research")
        self.assertEqual(persisted["helper_model"], "gpt-5.4-nano")
        self.assertEqual(persisted["request_delivery"], "direct")
        self.assertEqual(persisted["routing_delivery"], "direct")
        self.assertEqual(persisted["response_delivery"], "relayed_by_manager")
        self.assertEqual(
            persisted["model_reason"],
            "A lightweight source check needs only text retrieval.",
        )
        self.assertEqual(persisted["input_tokens"], 21)
        self.assertEqual(persisted["output_tokens"], 8)
        self.assertEqual(persisted["cost_usd"], 0.000123)
        self.assertFalse(task["team_help_events_truncated"])
        self.assertEqual(
            task["attempt_history"][0]["model_reason"],
            "Standard implementation route.",
        )

    def test_team_help_normalization_keeps_latest_three_and_bounds_all_text(self):
        self._assign()
        state = company_mode.load_state(self.state_path)
        task = state["tasks"][0]
        task["team_help_events"] = [
            {
                "requesting_agent": f"requester-{index}",
                "helper_agent": "h" * (company_mode.MAX_TEAM_HELP_METADATA_CHARS + 20),
                "question": "q" * (company_mode.MAX_TEAM_HELP_QUESTION_CHARS + 20),
                "reason": "r" * (company_mode.MAX_TEAM_HELP_REASON_CHARS + 20),
                "response": "a" * (company_mode.MAX_TEAM_HELP_RESPONSE_CHARS + 20),
                "helper_model": "m" * (company_mode.MAX_TEAM_HELP_METADATA_CHARS + 20),
                "model_reason": "d" * (
                    company_mode.MAX_TEAM_HELP_MODEL_REASON_CHARS + 20
                ),
                "task_type": "research",
                "complexity": "lightweight",
                "risk": "low",
                "status": "completed",
                "created_at": "2026-08-08T12:00:00+00:00",
                "completed_at": "2026-08-08T12:00:01+00:00",
                "input_tokens": -2,
                "output_tokens": "invalid",
                "cost_usd": 0.001,
                "unexpected_unbounded_field": "x" * 10000,
            }
            for index in range(5)
        ]
        company_mode.save_state(state, self.state_path)

        persisted = company_mode.load_state(self.state_path)["tasks"][0]
        events = persisted["team_help_events"]
        self.assertEqual(
            [event["requesting_agent"] for event in events],
            ["requester-2", "requester-3", "requester-4"],
        )
        self.assertTrue(persisted["team_help_events_truncated"])
        self.assertTrue(events[0]["question_truncated"])
        self.assertTrue(events[0]["reason_truncated"])
        self.assertTrue(events[0]["response_truncated"])
        self.assertTrue(events[0]["model_reason_truncated"])
        self.assertEqual(len(events[0]["question"]), company_mode.MAX_TEAM_HELP_QUESTION_CHARS)
        self.assertEqual(len(events[0]["response"]), company_mode.MAX_TEAM_HELP_RESPONSE_CHARS)
        self.assertEqual(
            len(events[0]["helper_agent"]), company_mode.MAX_TEAM_HELP_METADATA_CHARS
        )
        self.assertEqual(events[0]["input_tokens"], 0)
        self.assertEqual(events[0]["output_tokens"], 0)
        self.assertNotIn("unexpected_unbounded_field", events[0])

    def test_team_help_evidence_is_redacted_before_persistence(self):
        self._assign()
        task_id = company_mode.load_state(self.state_path)["tasks"][0]["id"]
        secret = "ghp_abcdefghijklmnopqrstuvwxyz123456"
        company_mode.update_task_status(
            task_id,
            "done",
            spent_usd=0.0,
            path=self.state_path,
            team_help_events=[{
                "requesting_agent": "code",
                "helper_agent": "research",
                "question": f"Check GITHUB_TOKEN={secret}",
                "response": f"Never expose {secret} in a report.",
                "helper_model": "gpt-5.4-nano",
                "model_reason": "Lightweight redaction check.",
                "status": "completed",
            }],
        )

        raw = self.state_path.read_text(encoding="utf-8")
        self.assertNotIn("ghp_", raw)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456", raw)
        self.assertIn("[REDACTED]", raw)

    def test_failure_classifier_covers_actionable_categories(self):
        cases = {
            "API key is missing": "missing_access",
            "Need more information about the target": "missing_information",
            "Required tool is unavailable": "unavailable_tool",
            "403 permission denied": "permission",
            "Daily budget exhausted": "budget",
            "Request timed out": "transient",
            "Needs approval from the owner": "decision",
            "Unexpected parser failure": "technical",
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(company_mode.classify_failure(message), expected)

    def test_repeated_substantially_identical_editor_feedback_blocks_no_progress(self):
        self._assign()
        project_id = company_mode.load_state(self.state_path)["company"]["active_project_id"]
        first = "REVISIONS REQUIRED: fix the missing source and clarify the conclusion."
        second = "Revisions required - please fix missing source and clarify the conclusion!"
        self.assertEqual(company_mode.set_project_revision_flag(project_id, first, self.state_path), "revise")
        self.assertEqual(company_mode.set_project_revision_flag(project_id, second, self.state_path), "blocked")
        project = company_mode.active_project(company_mode.load_state(self.state_path))
        self.assertEqual(project["status"], "blocked")
        self.assertEqual(project["failure_classification"], "no_progress")
        self.assertFalse(project["needs_revision"])

    def test_editor_external_dependency_blocks_before_any_revision_round(self):
        self._assign()
        project_id = company_mode.load_state(self.state_path)["company"]["active_project_id"]

        verdict = company_mode.set_project_revision_flag(
            project_id,
            "REVISIONS REQUIRED: I cannot access the actual last five run records; "
            "the owner must provide them.",
            self.state_path,
        )

        project = company_mode.active_project(company_mode.load_state(self.state_path))
        self.assertEqual(verdict, "blocked")
        self.assertFalse(project["needs_revision"])
        self.assertEqual(project["revision_round"], 0)
        self.assertEqual(project["failure_classification"], "missing_access")

        # Missing evidence that can be fixed from supplied context remains revisable.
        company_mode.set_project_revision_flag(
            project_id,
            "APPROVED: reset the review state for the control assertion.",
            self.state_path,
        )
        fixable = company_mode.set_project_revision_flag(
            project_id,
            "REVISIONS REQUIRED: cite the supplied run evidence in the comparison.",
            self.state_path,
        )
        self.assertEqual(fixable, "revise")

    def test_external_dependency_detection_is_fail_closed_without_false_access_matches(self):
        self.assertEqual(
            company_mode.classify_editor_verdict(
                "APPROVED: The draft looks polished, but I cannot access the required logs."
            ),
            "blocked",
        )
        self.assertEqual(
            company_mode.classify_editor_verdict(
                "REVISIONS REQUIRED: Explain what users should do when they cannot "
                "access the actual run data."
            ),
            "revise",
        )
        self.assertEqual(
            company_mode.classify_editor_verdict(
                "APPROVED: The deliverable correctly states that it cannot access "
                "the actual run records."
            ),
            "approved",
        )
        self.assertEqual(
            company_mode.classify_failure(
                "cannot access local variable 'result' where it is not associated "
                "with a value"
            ),
            "technical",
        )

    def test_start_revision_round_enforces_cap_itself(self):
        self._assign()
        state = company_mode.load_state(self.state_path)
        project_id = state["company"]["active_project_id"]
        company_mode.active_project(state)["revision_round"] = company_mode.MAX_REVISION_ROUNDS
        company_mode.save_state(state, self.state_path)

        created, note = company_mode.start_revision_round(project_id, ["manager", "code"], self.state_path)

        self.assertFalse(created)
        self.assertIn("maximum revision rounds", note)
        project = company_mode.active_project(company_mode.load_state(self.state_path))
        self.assertEqual(project["status"], "blocked")
        self.assertEqual(project["failure_classification"], "no_progress")

    def test_autonomous_revision_preserves_authorization_routing_and_criteria(self):
        company_mode.set_daily_budget(5, self.state_path)
        criteria = ["No external action", "The validation result is explicit"]
        plan = [
            {
                "owner": "code",
                "title": "Inspect the configuration",
                "estimate_usd": 0.10,
                "acceptance_criteria": criteria,
                "authorization_level": "observe",
                "enforce_authorization": True,
                "task_type": "status_update",
                "complexity": "lightweight",
                "risk": "low",
                "required_capabilities": ["text"],
                "model": "small-model",
                "model_reason": "Least-cost capable model",
            },
            {
                "owner": "editor",
                "title": "Review the result",
                "estimate_usd": 0.12,
                "acceptance_criteria": criteria,
                "authorization_level": "observe",
                "enforce_authorization": True,
                "task_type": "review",
                "complexity": "standard",
                "risk": "low",
                "required_capabilities": ["review"],
                "model": "review-model",
                "model_reason": "Bounded review route",
            },
        ]
        company_mode.assign_goal(
            "Validate safely", ["manager", "code", "editor"],
            path=self.state_path, tasks=plan,
            project_metadata={"autonomous_run_id": "run-1", "source": "autonomous_daily_run"},
        )
        project_id = company_mode.load_state(self.state_path)["company"]["active_project_id"]
        company_mode.set_project_revision_flag(
            project_id, "REVISIONS REQUIRED: make the validation result explicit.", self.state_path
        )

        hook = mock.Mock()
        with mock.patch.object(company_mode, "on_project_activated", hook):
            created, _note = company_mode.start_revision_round(
                project_id, ["manager", "code", "editor"], self.state_path
            )
        self.assertTrue(created)
        hook.assert_not_called()
        revised = company_mode.project_tasks(company_mode.load_state(self.state_path), project_id)[-2:]
        self.assertEqual([task["estimate_usd"] for task in revised], [0.10, 0.12])
        # A revision is new work after a rejected result.  Preserve its scope and
        # estimate, but require a fresh runtime routing decision instead of silently
        # inheriting the model that produced/reviewed the rejected candidate.
        self.assertEqual([task["model"] for task in revised], ["", ""])
        self.assertEqual([task["model_reason"] for task in revised], ["", ""])
        self.assertTrue(all(task["enforce_authorization"] for task in revised))
        self.assertTrue(all(task["acceptance_criteria"] == criteria for task in revised))
        self.assertTrue(all(task["revision_round"] == 1 for task in revised))
        self.assertTrue(all(
            task["revision_feedback"]
            == "REVISIONS REQUIRED: make the validation result explicit."
            for task in revised
        ))

    def test_reject_revision_prompts_snapshot_feedback_and_mark_latest_candidate(self):
        self._assign()
        state = company_mode.load_state(self.state_path)
        project = company_mode.active_project(state)
        project_id = project["id"]
        original_tasks = company_mode.project_tasks(state, project_id)
        original_worker = next(task for task in original_tasks if task["owner"] != "editor")
        original_editor = next(task for task in original_tasks if task["owner"] == "editor")
        company_mode.update_task_status(
            original_worker["id"], "done", "ORIGINAL FAILED CANDIDATE", path=self.state_path
        )
        company_mode.update_task_status(
            original_editor["id"], "done", "STORED FIRST REVIEW", path=self.state_path
        )
        feedback = "REVISIONS REQUIRED\n1. State the exact configured timezone."
        company_mode.set_project_revision_flag(project_id, feedback, self.state_path)

        created, _note = company_mode.start_revision_round(
            project_id, ["manager", "code", "editor"], self.state_path
        )
        self.assertTrue(created)
        state = company_mode.load_state(self.state_path)
        project = next(item for item in state["projects"] if item["id"] == project_id)
        revision_tasks = [
            task for task in company_mode.project_tasks(state, project_id)
            if task["revision_round"] == 1
        ]
        revision_worker = next(task for task in revision_tasks if task["owner"] != "editor")
        revision_editor = next(task for task in revision_tasks if task["owner"] == "editor")
        self.assertTrue(all(task["revision_feedback"] == feedback for task in revision_tasks))

        worker_prior = company_mode.prior_work_summary(
            state, project_id, revision_worker["id"]
        )
        worker_prompt = company_mode.build_task_prompt(project, revision_worker, worker_prior)
        self.assertEqual(worker_prompt.count("Latest required changes:"), 1)
        self.assertEqual(worker_prompt.count(feedback), 1)
        self.assertIn("Revision round: 1", worker_prompt)
        self.assertIn("Address every applicable required change explicitly", worker_prompt)

        company_mode.update_task_status(
            revision_worker["id"], "done", "ROUND ONE REVISED CANDIDATE", path=self.state_path
        )
        state = company_mode.load_state(self.state_path)
        reviewer_prior = company_mode.prior_work_summary(
            state, project_id, revision_editor["id"]
        )
        reviewer_prompt = company_mode.build_task_prompt(
            project, revision_editor, reviewer_prior
        )

        self.assertIn("STORED FIRST REVIEW", reviewer_prior)
        self.assertNotIn(feedback, reviewer_prior)
        self.assertIn(
            "LATEST REVIEW CANDIDATE: research", reviewer_prior
        )
        self.assertIn("ROUND ONE REVISED CANDIDATE", reviewer_prior)
        self.assertEqual(reviewer_prompt.count("Latest required changes:"), 1)
        self.assertEqual(reviewer_prompt.count(feedback), 1)
        self.assertIn(
            "Review the result labeled LATEST REVIEW CANDIDATE", reviewer_prompt
        )

    def test_autonomous_task_prompt_makes_acceptance_and_authorization_explicit(self):
        prompt = company_mode.build_task_prompt(
            {"id": "p", "goal": "Validate safely"},
            {
                "id": "t",
                "owner": "manager",
                "title": "Inspect only",
                "acceptance_criteria": ["Report the timezone"],
                "authorization_level": "observe",
                "enforce_authorization": True,
            },
        )
        self.assertIn("Acceptance criteria", prompt)
        self.assertIn("Report the timezone", prompt)
        self.assertIn("Authorization level: observe", prompt)
        self.assertIn("Do not create or modify files", prompt)

    def test_execution_attempt_cap_moves_task_to_blocked(self):
        self._assign()
        task_id = company_mode.load_state(self.state_path)["tasks"][0]["id"]
        with mock.patch.object(company_mode, "MAX_EXECUTION_ATTEMPTS", 1):
            company_mode.update_task_status(task_id, "in_progress", path=self.state_path)
            company_mode.update_task_status(task_id, "in_progress", path=self.state_path)
        task = next(item for item in company_mode.load_state(self.state_path)["tasks"] if item["id"] == task_id)
        self.assertEqual(task["status"], "blocked")
        self.assertEqual(task["failure_classification"], "no_progress")


if __name__ == "__main__":
    unittest.main()

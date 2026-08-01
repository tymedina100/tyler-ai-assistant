import asyncio
import importlib
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import autonomous_workflow
import company_mode


def import_group_bot_with_stub_main():
    specialist_keys = [
        "code", "research", "write", "task", "marketing", "editor", "finance",
        "calendar", "gmail", "linear", "sales", "analytics",
    ]
    specialists = {
        key: {
            "name": key.title(),
            "label": f"{key.title()} ({key.title()} Agent)",
            "tool_names": ["read_file", "send_email"],
        }
        for key in specialist_keys
    }
    fake_main = types.SimpleNamespace(
        ExecutionBudgetExceededError=type(
            "ExecutionBudgetExceededError", (RuntimeError,), {}
        ),
        SPECIALISTS=specialists,
        TOOLS=[{"type": "function", "name": "read_file"}, {"type": "function", "name": "send_email"}],
        CONFIRMATION_MODE="enabled",
        pending_actions={},
        logger=types.SimpleNamespace(error=Mock(), info=Mock(), warning=Mock()),
        projects=types.SimpleNamespace(
            get_active_project=lambda: (None, None),
            set_active_project=lambda key: ({"name": key, "repo": "owner/repo"}, None),
            clear_active_project=lambda: None,
            begin_scoped_project=lambda key: ({"name": key, "repo": "owner/repo"}, None, (object(), object())),
            end_scoped_project=lambda tokens: None,
        ),
        FAST_MODEL="gpt-5.4-mini",
        TIMEZONE="America/Phoenix",
        BRIEFING_TIME="08:00",
        EVENT_ALERT_MINUTES=15,
        set_execution_sink=Mock(),
        set_company_execution=Mock(),
        set_conversation=Mock(),
        set_reply_context=Mock(),
    )
    fake_dotenv = types.SimpleNamespace(load_dotenv=lambda: None)
    sys.modules.pop("group_bot", None)
    with patch.dict(
        os.environ,
        {"TELEGRAM_GROUP_CHAT_ID": "-1001", "TELEGRAM_ALLOWED_USER_IDS": "42"},
        clear=True,
    ), patch.dict(sys.modules, {"main": fake_main, "dotenv": fake_dotenv}):
        module = importlib.import_module("group_bot")
    return module, fake_main


class GroupAutonomyTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.group, cls.fake_main = import_group_bot_with_stub_main()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("group_bot", None)

    async def test_live_autorun_requires_enabled_and_non_dry_configuration(self):
        update = types.SimpleNamespace(message=types.SimpleNamespace(reply_text=AsyncMock()))
        disabled = autonomous_workflow.AutonomyConfig(enabled=False, dry_run=True)
        with patch.object(self.group, "AUTONOMY_CONFIG", disabled), patch.object(
            self.group, "_run_autonomy_session", new=AsyncMock()
        ) as run:
            await self.group.handle_autorun_command(update, "/autorun live")
        run.assert_not_awaited()
        self.assertIn("disabled", update.message.reply_text.await_args.args[0].lower())

        update.message.reply_text.reset_mock()
        locked = autonomous_workflow.AutonomyConfig(enabled=True, dry_run=True)
        with patch.object(self.group, "AUTONOMY_CONFIG", locked), patch.object(
            self.group, "_run_autonomy_session", new=AsyncMock()
        ) as run:
            await self.group.handle_autorun_command(update, "/autorun live")
        run.assert_not_awaited()
        self.assertIn("dry_run=true", update.message.reply_text.await_args.args[0].lower())

    async def test_manual_dry_run_returns_report_without_live_executor(self):
        update = types.SimpleNamespace(message=types.SimpleNamespace(reply_text=AsyncMock()))
        report = {
            "dry_run": True,
            "cycle_reports": [],
            "telegram_summary": "Autonomous run: dry_run",
            "report_path": "C:/tmp/run.json",
        }
        with patch.object(
            self.group, "_run_autonomy_session", new=AsyncMock(return_value=report)
        ) as run, patch.object(self.group, "reply_chunks", new=AsyncMock()) as reply:
            await self.group.handle_autorun_command(update, "/autorun dry-run")
        run.assert_awaited_once_with("telegram", dry_run=True)
        self.assertIn("dry_run", reply.await_args.args[1])

    async def test_manual_live_command_starts_one_bounded_session(self):
        update = types.SimpleNamespace(message=types.SimpleNamespace(reply_text=AsyncMock()))
        config = autonomous_workflow.AutonomyConfig(enabled=True, dry_run=False)
        report = {
            "dry_run": False,
            "cycle_reports": [],
            "escalations": [],
            "telegram_summary": "Autonomous session: completed",
        }
        with patch.object(self.group, "AUTONOMY_CONFIG", config), patch.object(
            self.group, "autonomy_runner_task", None
        ), patch.object(
            self.group, "_run_autonomy_session", new=AsyncMock(return_value=report)
        ) as run, patch.object(self.group, "post_to_group", new=AsyncMock()) as post:
            await self.group.handle_autorun_command(update, "/autorun live")
            session_task = self.group.autonomy_runner_task
            self.assertIsNotNone(session_task)
            await session_task

        run.assert_awaited_once_with("telegram", dry_run=False)
        self.assertEqual(
            [call.args for call in post.await_args_list],
            [("Autonomous session: completed", "manager")],
        )
        self.assertIn("bounded autonomous session", update.message.reply_text.await_args.args[0])
        self.assertIsNone(self.group.autonomy_runner_task)

    async def test_autorun_retry_resets_one_item_without_starting_work(self):
        update = types.SimpleNamespace(message=types.SimpleNamespace(reply_text=AsyncMock()))
        workflow = types.SimpleNamespace(
            retry_item=Mock(return_value=(
                True,
                "Roadmap item AUTO-RECOVERY-001 is ready. No model was invoked.",
            ))
        )
        with patch.object(self.group, "autonomy_runner_task", None), patch.object(
            self.group, "_get_autonomy_workflow", return_value=workflow
        ), patch.object(self.group, "_run_autonomy_session", new=AsyncMock()) as run:
            await self.group.handle_autorun_command(
                update,
                "/autorun retry AUTO-RECOVERY-001",
                allow_live=True,
            )

        workflow.retry_item.assert_called_once_with("AUTO-RECOVERY-001")
        run.assert_not_awaited()
        self.assertIn("No model was invoked", update.message.reply_text.await_args.args[0])

    async def test_autorun_retry_is_refused_in_dm_and_during_an_active_run(self):
        update = types.SimpleNamespace(message=types.SimpleNamespace(reply_text=AsyncMock()))
        workflow = types.SimpleNamespace(retry_item=Mock(return_value=(True, "ready")))
        with patch.object(
            self.group, "_get_autonomy_workflow", return_value=workflow
        ):
            await self.group.handle_autorun_command(
                update,
                "/autorun retry AUTO-RECOVERY-001",
                allow_live=False,
            )
        workflow.retry_item.assert_not_called()
        self.assertIn("group operating room", update.message.reply_text.await_args.args[0])

        update.message.reply_text.reset_mock()
        active = types.SimpleNamespace(done=lambda: False)
        with patch.object(self.group, "autonomy_runner_task", active), patch.object(
            self.group, "_get_autonomy_workflow", return_value=workflow
        ):
            await self.group.handle_autorun_command(
                update,
                "/autorun retry AUTO-RECOVERY-001",
                allow_live=True,
            )
        workflow.retry_item.assert_not_called()
        self.assertIn("no roadmap state was changed", update.message.reply_text.await_args.args[0])

    async def test_autorun_retry_reports_persistent_state_failure_without_starting_work(self):
        update = types.SimpleNamespace(message=types.SimpleNamespace(reply_text=AsyncMock()))
        workflow = types.SimpleNamespace(
            retry_item=Mock(side_effect=OSError("simulated state write failure"))
        )
        with patch.object(self.group, "autonomy_runner_task", None), patch.object(
            self.group, "_get_autonomy_workflow", return_value=workflow
        ), patch.object(self.group, "_run_autonomy_session", new=AsyncMock()) as run:
            await self.group.handle_autorun_command(
                update,
                "/autorun retry AUTO-RECOVERY-001",
                allow_live=True,
            )

        run.assert_not_awaited()
        self.assertIn(
            "persistent state could not be updated safely",
            update.message.reply_text.await_args.args[0],
        )

    async def test_runtime_deferral_protects_supervised_work_and_pending_confirmation(self):
        active_runner = types.SimpleNamespace(done=lambda: False)
        with patch.object(self.group, "company_runner_task", active_runner), patch.object(
            self.group.main, "pending_actions", {}
        ), patch.object(self.group.company_mode, "load_state") as load_state:
            running = await self.group._autonomy_runtime_deferral()

        self.assertEqual(running["status"], "deferred")
        self.assertIn("already running", running["reason"])
        self.assertFalse(running["model_invoked"])
        load_state.assert_not_called()

        with patch.object(self.group, "company_runner_task", None), patch.object(
            self.group.main, "pending_actions", {"group": {"kind": "send_email"}}
        ), patch.object(self.group.company_mode, "load_state") as load_state:
            waiting = await self.group._autonomy_runtime_deferral()

        self.assertEqual(waiting["status"], "deferred")
        self.assertIn("confirmation", waiting["reason"].lower())
        self.assertFalse(waiting["model_invoked"])
        load_state.assert_not_called()

    async def test_persisted_open_project_deferral_has_owner_recovery_action(self):
        state = {
            "company": {"mode": "running"},
            "projects": [{"id": "proj_stale", "status": "active"}],
        }
        with patch.object(self.group, "company_runner_task", None), patch.object(
            self.group.main, "pending_actions", {"group": None}
        ), patch.object(self.group.company_mode, "load_state", return_value=state):
            result = await self.group._autonomy_runtime_deferral()

        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["failure_classification"], "decision_required")
        self.assertIn("proj_stale", result["reason"])
        self.assertIn("/company", result["human_action"])
        self.assertIn("/approve", result["human_action"])
        self.assertIn("/cancel proj_stale", result["human_action"])
        self.assertIn("/autorun live", result["human_action"])
        self.assertTrue(result["attempted"])
        self.assertEqual(result["actual_cost_usd"], 0.0)
        self.assertFalse(result["model_invoked"])

    async def test_group_cancel_passes_explicit_project_id(self):
        update = types.SimpleNamespace(
            message=types.SimpleNamespace(text="/cancel proj_stale", reply_text=AsyncMock())
        )
        usernames = {key: f"{key}_bot" for key in self.group.BOT_KEYS}
        with patch.object(self.group, "bot_usernames", usernames), patch.object(
            self.group, "_handle_pending_confirmation", new=AsyncMock(return_value=False)
        ), patch.object(self.group, "_cancel_running_plan") as cancel_runner, patch.object(
            self.group.company_mode, "cancel_project", return_value="Cancelled."
        ) as cancel_project, patch.object(self.group, "reply_chunks", new=AsyncMock()) as reply:
            await self.group.handle_group_message(update)

        cancel_runner.assert_called_once_with()
        cancel_project.assert_called_once_with(
            self.group.company_mode.COMPANY_STATE_FILE,
            "proj_stale",
        )
        reply.assert_awaited_once_with(update.message, "Cancelled.")

    async def test_budget_approved_worker_and_reviewer_complete_the_autonomous_item(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "company.json"
            company_mode.set_daily_budget(1.0, path)
            original_load_state = company_mode.load_state

            worker_decision = types.SimpleNamespace(
                model_id="worker-model",
                reason="Small observe task fits the worker model.",
                estimated_cost_usd=0.01,
                deferred=False,
                deferral_reason=None,
            )
            review_decision = types.SimpleNamespace(
                model_id="review-model",
                reason="Explicit criteria require one bounded review.",
                estimated_cost_usd=0.01,
                deferred=False,
                deferral_reason=None,
            )
            project = {"id": "assistant", "project_key": "assistant", "name": "Assistant"}
            roadmap_item = {
                "id": "AUTO-INTEGRATION-1",
                "title": "Inspect the autonomous configuration",
                "description": "Produce one bounded configuration note.",
                "agent_owner": "manager",
                "task_type": "status_update",
                "complexity": "lightweight",
                "risk": "low",
                "required_capabilities": ["text"],
                "authorization_level": "observe",
                "acceptance_criteria": ["The note reports the configured schedule."],
                "estimated_input_tokens": 500,
                "estimated_output_tokens": 100,
            }

            async def complete_task(company_project, task):
                company_mode.update_task_status(
                    task["id"], "in_progress", path=path,
                    model=task.get("model"), model_reason=task.get("model_reason"),
                )
                if task["owner"] == "editor":
                    result = "APPROVED: every acceptance criterion is satisfied."
                    company_mode.update_task_status(
                        task["id"], "done", result, [], 0.01, path,
                        model=task.get("model"), model_reason=task.get("model_reason"),
                        feedback=result,
                    )
                    company_mode.set_project_revision_flag(company_project["id"], result, path)
                else:
                    company_mode.update_task_status(
                        task["id"], "done", "Schedule verified.",
                        ["file: files/config-note.md"], 0.01, path,
                        model=task.get("model"), model_reason=task.get("model_reason"),
                    )
                return "done"

            with patch.object(self.group.company_mode, "COMPANY_STATE_FILE", path), patch.object(
                self.group.company_mode,
                "load_state",
                side_effect=lambda selected_path=path: original_load_state(selected_path),
            ), patch.object(
                self.group, "_autonomy_runtime_deferral", new=AsyncMock(return_value=None)
            ), patch.object(
                self.group, "AUTONOMY_ROUTER",
                types.SimpleNamespace(route=Mock(return_value=review_decision)),
            ), patch.object(
                self.group, "_run_one_task", side_effect=complete_task
            ), patch.object(
                self.group, "post_to_group", new=AsyncMock()
            ), patch.object(
                self.group.company_linear, "finalize_source_issue", return_value=None
            ):
                result = await self.group._execute_autonomy_item(
                    project, roadmap_item, worker_decision, "run-integration-1"
                )

            final = original_load_state(path)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["review_outcome"], "approved")
        self.assertEqual(result["result_text"], "Schedule verified.")
        self.assertEqual(result["result_agent"], "general")
        self.assertEqual(result["files_changed"], ["files/config-note.md"])
        self.assertEqual(result["models"], ["worker-model", "review-model"])
        self.assertEqual(result["actual_cost_usd"], 0.02)
        self.assertEqual(final["company"]["reserved_today_usd"], 0.0)
        self.assertEqual(final["projects"][0]["status"], "completed")
        self.assertTrue(all(task["status"] == "done" for task in final["tasks"]))

    async def test_run_autonomy_session_calls_bounded_workflow_api(self):
        report = {"cycle_reports": [], "telegram_summary": "Session complete"}
        workflow = types.SimpleNamespace(run_session=Mock(return_value=report))
        with patch.object(self.group, "_get_autonomy_workflow", return_value=workflow):
            returned = await self.group._run_autonomy_session("scheduled", dry_run=False)

        self.assertIs(returned, report)
        workflow.run_session.assert_called_once_with(
            trigger_source="scheduled",
            dry_run=False,
        )

    async def test_scheduled_live_session_deduplicates_escalations_and_posts_one_summary(self):
        report = {
            "dry_run": False,
            "cycle_reports": [
                {
                    "escalations": [
                        "Project A needs repository access.",
                        "Project A needs repository access.",
                    ],
                },
                {
                    "escalations": [
                        "Project A needs repository access.",
                        "Project B needs an owner decision.",
                    ],
                },
            ],
            "escalations": [
                "Project A needs repository access.",
                "Project B needs an owner decision.",
            ],
            "telegram_summary": "Autonomous session: needs_human",
        }
        with patch.object(
            self.group, "_run_autonomy_session", new=AsyncMock(return_value=report)
        ) as run, patch.object(
            self.group.autonomous_workflow,
            "format_telegram_deliverable",
            return_value="",
        ), patch.object(self.group, "post_to_group", new=AsyncMock()) as post:
            returned = await self.group._run_and_post_autonomy("scheduled", dry_run=False)

        self.assertIs(returned, report)
        run.assert_awaited_once_with("scheduled", dry_run=False)
        self.assertEqual(
            [call.args for call in post.await_args_list],
            [
                ("Project A needs repository access.", "manager"),
                ("Project B needs an owner decision.", "manager"),
                ("Autonomous session: needs_human", "manager"),
            ],
        )
        self.assertIsNone(self.group.autonomy_runner_task)

    async def test_live_session_posts_each_child_deliverable_in_order_then_one_summary(self):
        children = [
            {"id": "cycle-1", "telegram_summary": "DO NOT POST CHILD 1"},
            {"id": "cycle-2", "telegram_summary": "DO NOT POST CHILD 2"},
            {
                "id": "cycle-3",
                "idea_proposals": [{"idea": "Add a deployment health digest"}],
                "telegram_summary": "DO NOT POST CHILD 3",
            },
        ]
        report = {
            "dry_run": False,
            "escalations": [],
            "cycle_reports": children,
            "telegram_summary": "Autonomous session: completed",
        }
        with patch.object(
            self.group, "_run_autonomy_session", new=AsyncMock(return_value=report)
        ), patch.object(
            self.group.autonomous_workflow,
            "format_telegram_deliverable",
            side_effect=[
                "Autonomous deliverable: first",
                "Autonomous deliverable: second",
                "Lumen idea plan: deployment health digest",
            ],
        ) as formatter, patch.object(self.group, "post_to_group", new=AsyncMock()) as post:
            await self.group._run_and_post_autonomy("scheduled", dry_run=False)

        self.assertEqual([call.args[0] for call in formatter.call_args_list], children)
        self.assertEqual(
            [call.args for call in post.await_args_list],
            [
                ("Autonomous deliverable: first", "manager"),
                ("Autonomous deliverable: second", "manager"),
                ("Lumen idea plan: deployment health digest", "manager"),
                ("Autonomous session: completed", "manager"),
            ],
        )
        self.assertEqual(
            sum(call.args[0] == "Autonomous session: completed" for call in post.await_args_list),
            1,
        )
        self.assertFalse(any("DO NOT POST CHILD" in call.args[0] for call in post.await_args_list))

    async def test_scheduled_dry_run_session_posts_no_telegram_message(self):
        report = {
            "dry_run": True,
            "escalations": [],
            "cycle_reports": [{"telegram_summary": "Child dry run"}],
            "telegram_summary": "Autonomous session: dry_run",
            "report_path": "C:/tmp/run.json",
        }
        with patch.object(
            self.group, "_run_autonomy_session", new=AsyncMock(return_value=report)
        ), patch.object(self.group, "post_to_group", new=AsyncMock()) as post:
            await self.group._run_and_post_autonomy("scheduled", dry_run=True)

        post.assert_not_awaited()

    async def test_autonomy_runner_task_stays_active_for_entire_session(self):
        observed = []

        async def session(_trigger_source, *, dry_run=None):
            current = asyncio.current_task()
            observed.append(self.group.autonomy_runner_task is current)
            await asyncio.sleep(0)
            observed.append(self.group.autonomy_runner_task is current)
            return {
                "dry_run": bool(dry_run),
                "cycle_reports": [],
                "telegram_summary": "Autonomous session: completed",
            }

        with patch.object(self.group, "_run_autonomy_session", side_effect=session), patch.object(
            self.group, "post_to_group", new=AsyncMock()
        ):
            await self.group._run_and_post_autonomy("scheduled", dry_run=False)

        self.assertEqual(observed, [True, True])
        self.assertIsNone(self.group.autonomy_runner_task)

    async def test_external_action_escalates_without_model_or_company_call(self):
        with patch.object(self.group, "_company_budget_snapshot") as budget, patch.object(
            self.group.company_mode, "load_state"
        ) as load:
            result = await self.group._execute_autonomy_item(
                {"id": "assistant"},
                {"id": "AUTO-X", "title": "Deploy", "authorization_level": "external_action"},
                types.SimpleNamespace(model_id="should-not-run"),
                "run-1",
            )
        self.assertEqual(result["status"], "needs_human")
        self.assertEqual(result["failure_classification"], "decision_required")
        budget.assert_not_called()
        load.assert_not_called()

        result = await self.group._execute_autonomy_item(
            {"id": "assistant"},
            {"id": "AUTO-Y", "title": "Edit", "authorization_level": "modify_local"},
            types.SimpleNamespace(model_id="should-not-run"),
            "run-2",
        )
        self.assertEqual(result["status"], "needs_human")
        self.assertFalse(result["model_invoked"])
        self.assertIn("isolated executor", result["reason"])

    async def test_routed_task_uses_selected_model_and_read_only_tools(self):
        task = {
            "id": "task-1",
            "owner": "manager",
            "title": "Inspect",
            "model": "gpt-5.4-nano",
            "model_reason": "Lightweight inspection",
            "estimate_usd": 0.10,
            "execution_attempts": 0,
            "attempt_history": [],
            "authorization_level": "observe",
            "enforce_authorization": True,
        }
        project = {"id": "project-1", "title": "Project", "goal": "Inspect"}
        sink = {"cost_usd": 0.0, "artifacts": [], "usage_records": [], "context": "test"}
        ask = Mock(return_value="Completed the inspection.")
        fake_main = self.group.main
        fake_main.set_conversation = Mock()
        fake_main.set_reply_context = Mock()
        fake_main.set_execution_sink = Mock()
        fake_main.set_company_execution = Mock()
        fake_main.ask_ai = ask
        fake_main.describe_pending_action = Mock()
        fake_main.pending_actions = {}
        with patch.object(self.group.company_mode, "update_task_status", return_value="ok"), patch.object(
            self.group.company_mode, "load_state", return_value={"company": {}}
        ), patch.object(self.group.company_mode, "render_money", return_value="Budget ok"), patch.object(
            self.group, "post_to_group", new=AsyncMock()
        ), patch.object(self.group, "post_agent_answer_to_group", new=AsyncMock()):
            outcome = await self.group._execute_routed_task(project, task, None, "prompt", sink)

        self.assertEqual(outcome, "done")
        kwargs = ask.call_args.kwargs
        self.assertEqual(kwargs["model"], "gpt-5.4-nano")
        self.assertEqual(kwargs["allowed_tool_names"], {"read_file"})
        self.assertFalse(kwargs["include_memories"])
        self.assertFalse(any(call.args == (True,) for call in fake_main.set_company_execution.call_args_list))

    async def test_company_task_sink_uses_its_persisted_reservation_as_hard_envelope(self):
        project = {"id": "project-1", "title": "Project", "goal": "Inspect"}
        task = {
            "id": "task-1", "project_id": "project-1", "owner": "manager",
            "title": "Inspect", "reserved_usd": 0.125,
        }
        state = {"projects": [project], "tasks": [task], "company": {}}
        with patch.object(
            self.group.company_mode, "load_state", return_value=state
        ), patch.object(
            self.group.company_mode, "prior_work_summary", return_value=""
        ), patch.object(
            self.group, "_load_project_deliverable", return_value=("", "")
        ), patch.object(
            self.group.company_mode, "build_task_prompt", return_value="prompt"
        ), patch.object(
            self.group, "_execute_routed_task", new=AsyncMock(return_value="done")
        ) as execute:
            outcome = await self.group._run_one_task(project, task)

        self.assertEqual(outcome, "done")
        sink = execute.await_args.args[4]
        self.assertEqual(sink["budget_cap_usd"], 0.125)

    async def test_metered_no_usage_charges_reserved_estimate_as_estimated(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "company.json"
            company_mode.set_daily_budget(1.0, path)
            with patch.object(self.group.company_mode, "COMPANY_STATE_FILE", path):
                result, receipt = await self.group._run_metered(
                    lambda: "ok",
                    estimate_usd=0.10,
                    context="test no usage",
                    project_id="project-a",
                    task_id="task-a",
                    return_receipt=True,
                )
            state = company_mode.load_state(path)
        self.assertEqual(result, "ok")
        self.assertEqual(state["company"]["reserved_today_usd"], 0.0)
        self.assertEqual(state["company"]["spent_today_usd"], 0.10)
        self.assertEqual(state["cost_entries"][-1]["cost_basis"], "estimated")
        self.assertEqual(receipt["project_id"], "project-a")
        self.assertEqual(receipt["task_id"], "task-a")

    async def test_budget_guard_before_first_call_reconciles_known_zero_spend(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "company.json"
            company_mode.set_daily_budget(1.0, path)

            def guard_blocks():
                sink = self.group.main.set_execution_sink.call_args.args[0]
                sink["budget_guard_blocked"] = True
                raise self.group.main.ExecutionBudgetExceededError("request cannot fit")

            self.group.main.set_execution_sink.reset_mock()
            with patch.object(self.group.company_mode, "COMPANY_STATE_FILE", path):
                with self.assertRaises(self.group.main.ExecutionBudgetExceededError):
                    await self.group._run_metered(
                        guard_blocks,
                        estimate_usd=0.10,
                        context="strict preflight",
                        strict_budget=True,
                    )
            state = company_mode.load_state(path)

        strict_sink = self.group.main.set_execution_sink.call_args_list[0].args[0]
        self.assertEqual(strict_sink["budget_cap_usd"], 0.10)
        self.assertEqual(state["company"]["reserved_today_usd"], 0.0)
        self.assertEqual(state["company"]["spent_today_usd"], 0.0)
        self.assertEqual(state["cost_entries"][-1]["cost_basis"], "actual")

    async def test_cancelled_metered_call_finishes_before_budget_reconciliation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "company.json"
            company_mode.set_daily_budget(1.0, path)
            started = threading.Event()
            finished = threading.Event()

            def slow_paid_call():
                started.set()
                time.sleep(0.03)
                finished.set()
                return "late result"

            with patch.object(self.group.company_mode, "COMPANY_STATE_FILE", path):
                task = asyncio.create_task(self.group._run_metered(
                    slow_paid_call,
                    estimate_usd=0.10,
                    context="test cancellation accounting",
                ))
                self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            state = company_mode.load_state(path)

        self.assertTrue(finished.is_set())
        self.assertEqual(state["company"]["reserved_today_usd"], 0.0)
        self.assertEqual(state["company"]["spent_today_usd"], 0.10)
        self.assertEqual(state["cost_entries"][-1]["cost_basis"], "estimated")

    async def test_idle_ideation_returns_run_level_metering_metadata(self):
        decision = types.SimpleNamespace(
            model_id="creative-model",
            reason="Balanced model selected for controlled ideation.",
            estimated_cost_usd=0.08,
            deferred=False,
            deferral_reason="",
        )
        receipt = {
            "amount_usd": 0.03,
            "cost_basis": "actual",
            "input_tokens": 70,
            "output_tokens": 30,
            "total_tokens": 100,
        }
        self.group.main.generate_controlled_ideas = Mock()
        with patch.object(
            self.group, "_company_budget_snapshot", return_value={"remaining_usd": 1.0}
        ), patch.object(
            self.group.AUTONOMY_ROUTER, "route", return_value=decision
        ), patch.object(
            self.group,
            "_run_metered",
            new=AsyncMock(return_value=([{"idea": "Bounded proposal"}], receipt)),
        ) as metered:
            result = await self.group._generate_autonomy_ideas({"projects": []}, 1)

        self.assertEqual(result["ideas"][0]["idea"], "Bounded proposal")
        self.assertEqual(result["actual_cost_usd"], 0.03)
        self.assertFalse(result["cost_is_estimated"])
        self.assertEqual(result["token_usage"]["total_tokens"], 100)
        self.assertEqual(result["agent"], "creative")
        self.assertEqual(result["model"], "creative-model")
        self.assertTrue(metered.await_args.kwargs["return_receipt"])
        self.assertTrue(metered.await_args.kwargs["strict_budget"])
        self.assertEqual(metered.await_args.kwargs["project_id"], "idea_backlog")

    async def test_idle_ideation_turns_strict_budget_guard_into_deferral(self):
        decision = types.SimpleNamespace(
            model_id="gpt-5.4-mini",
            reason="Standard ideation route.",
            estimated_cost_usd=0.01,
            deferred=False,
            deferral_reason="",
        )
        error = self.group.main.ExecutionBudgetExceededError(
            "The creative request cannot fit its reservation."
        )
        with patch.object(
            self.group, "_autonomy_runtime_deferral", new=AsyncMock(return_value=None)
        ), patch.object(
            self.group, "_company_budget_snapshot", return_value={"remaining_usd": 1.0}
        ), patch.object(
            self.group.AUTONOMY_ROUTER, "route", return_value=decision
        ), patch.object(
            self.group, "_run_metered", new=AsyncMock(side_effect=error)
        ):
            result = await self.group._generate_autonomy_ideas({"projects": []}, 1)

        self.assertTrue(result["deferred"])
        self.assertEqual(result["actual_cost_usd"], 0.0)
        self.assertIn("cannot fit", result["deferral_reason"])

    async def test_full_idea_backlog_skips_routing_and_paid_generation(self):
        config = autonomous_workflow.AutonomyConfig(idea_backlog_limit=2)
        state = {
            "projects": [],
            "idea_backlog": [
                {"id": "idea-1", "idea": "First"},
                {"id": "idea-2", "idea": "Second"},
            ],
        }
        with patch.object(self.group, "AUTONOMY_CONFIG", config), patch.object(
            self.group, "_autonomy_runtime_deferral", new=AsyncMock()
        ) as runtime, patch.object(
            self.group, "_company_budget_snapshot"
        ) as budget, patch.object(
            self.group.AUTONOMY_ROUTER, "route"
        ) as route, patch.object(
            self.group, "_run_metered", new=AsyncMock()
        ) as metered:
            result = await self.group._generate_autonomy_ideas(state, 1)

        self.assertEqual(result["ideas"], [])
        self.assertTrue(result["deferred"])
        self.assertTrue(result["idle"])
        self.assertEqual(result["deferral_kind"], "backlog_full")
        self.assertEqual(result["actual_cost_usd"], 0.0)
        self.assertIn("configured limit of 2", result["deferral_reason"])
        runtime.assert_not_awaited()
        budget.assert_not_called()
        route.assert_not_called()
        metered.assert_not_awaited()

    async def test_idle_ideation_defers_before_routing_when_owner_state_is_open(self):
        blocker = {
            "status": "deferred",
            "reason": "A Telegram confirmation is already waiting for the owner.",
            "actual_cost_usd": 0.0,
            "model_invoked": False,
        }
        with patch.object(
            self.group, "_autonomy_runtime_deferral", new=AsyncMock(return_value=blocker)
        ), patch.object(self.group.AUTONOMY_ROUTER, "route") as route, patch.object(
            self.group, "_run_metered", new=AsyncMock()
        ) as metered:
            result = await self.group._generate_autonomy_ideas({"projects": []}, 1)

        self.assertTrue(result["deferred"])
        self.assertEqual(result["actual_cost_usd"], 0.0)
        route.assert_not_called()
        metered.assert_not_awaited()

    async def test_company_task_without_usage_reconciles_held_estimate(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "company.json"
            company_mode.set_daily_budget(1.0, path)
            company_mode.assign_goal(
                "Inspect safely", ["manager"], path=path,
                tasks=[{
                    "owner": "manager", "title": "Inspect", "estimate_usd": 0.10,
                    "model": "test-model", "model_reason": "test",
                    "authorization_level": "observe", "enforce_authorization": True,
                }],
            )
            _message, project_id = company_mode.approve_project(path, notify_hooks=False)
            state = company_mode.load_state(path)
            project = next(value for value in state["projects"] if value["id"] == project_id)
            task = company_mode.project_tasks(state, project_id)[0]
            self.group.main.ask_ai = Mock(return_value="Inspection complete.")
            self.group.main.pending_actions = {}
            sink = {"cost_usd": 0.0, "artifacts": [], "usage_records": [], "context": "test"}
            with patch.object(self.group.company_mode, "COMPANY_STATE_FILE", path), patch.object(
                self.group, "post_to_group", new=AsyncMock()
            ), patch.object(self.group, "post_agent_answer_to_group", new=AsyncMock()):
                outcome = await self.group._execute_routed_task(project, task, None, "prompt", sink)
            final = company_mode.load_state(path)
            saved = company_mode.project_tasks(final, project_id)[0]

        self.assertEqual(outcome, "done")
        self.assertEqual(saved["spent_usd"], 0.10)
        self.assertEqual(saved["cost_basis"], "estimated")
        self.assertEqual(final["company"]["reserved_today_usd"], 0.0)

    async def test_editor_provider_failure_stops_instead_of_starting_revision(self):
        task = {
            "id": "editor-1", "owner": "editor", "title": "Review", "model": "review-model",
            "model_reason": "test", "estimate_usd": 0.10,
            "execution_attempts": company_mode.MAX_EXECUTION_ATTEMPTS - 1,
            "attempt_history": [], "authorization_level": "observe", "enforce_authorization": True,
        }
        project = {"id": "project-1", "title": "Project"}
        sink = {"cost_usd": 0.0, "artifacts": [], "usage_records": [], "context": "test"}
        self.group.main.ask_specialist = Mock(
            return_value="Sorry, something went wrong while contacting the AI service."
        )
        with patch.object(self.group.company_mode, "update_task_status", return_value="ok") as update, patch.object(
            self.group, "post_to_group", new=AsyncMock()
        ), patch.object(self.group, "post_agent_answer_to_group", new=AsyncMock()):
            outcome = await self.group._execute_routed_task(project, task, "editor", "prompt", sink)

        self.assertEqual(outcome, "blocked")
        terminal = [call for call in update.call_args_list if len(call.args) > 1 and call.args[1] == "needs_human"]
        self.assertEqual(len(terminal), 1)

    async def test_successful_worker_result_is_persisted_without_pre_review_truncation(self):
        result = "complete evidence\n" + ("x" * 7000)
        task = {
            "id": "worker-long", "owner": "manager", "title": "Long proposal",
            "model": "worker-model", "model_reason": "test", "estimate_usd": 0.10,
            "execution_attempts": 0, "attempt_history": [],
            "authorization_level": "observe", "enforce_authorization": True,
        }
        project = {"id": "project-1", "title": "Project"}
        sink = {
            "cost_usd": 0.0,
            "artifacts": [],
            "usage_records": [],
            "context": "test",
        }
        self.group.main.ask_ai = Mock(return_value=result)
        self.group.main.pending_actions = {}
        with patch.object(
            self.group.company_mode, "update_task_status", return_value="ok"
        ) as update, patch.object(
            self.group.company_mode, "load_state", return_value={"company": {}}
        ), patch.object(
            self.group.company_mode, "render_money", return_value="Budget ok"
        ), patch.object(
            self.group, "post_to_group", new=AsyncMock()
        ), patch.object(
            self.group, "post_agent_answer_to_group", new=AsyncMock()
        ):
            outcome = await self.group._execute_routed_task(
                project,
                task,
                None,
                "prompt",
                sink,
            )

        done_call = next(call for call in update.call_args_list if call.args[1] == "done")
        self.assertEqual(outcome, "done")
        self.assertEqual(done_call.args[2], result)
        self.assertGreater(len(done_call.args[2]), company_mode.MAX_TASK_RESULT_CHARS)

    async def test_time_limit_waits_for_thread_and_stops_without_retry(self):
        task = {
            "id": "slow-1", "owner": "manager", "title": "Slow", "model": "test-model",
            "model_reason": "test", "estimate_usd": 0.10, "execution_attempts": 0,
            "attempt_history": [], "authorization_level": "observe", "enforce_authorization": True,
        }
        project = {"id": "project-1", "title": "Project"}
        sink = {"cost_usd": 0.0, "artifacts": [], "usage_records": [], "context": "test"}

        def slow_answer(*args, **kwargs):
            time.sleep(0.03)
            return "Completed after waiting."

        self.group.main.ask_ai = Mock(side_effect=slow_answer)
        with patch.dict(os.environ, {"AUTONOMY_TASK_TIMEOUT_SECONDS": "0.001"}), patch.object(
            self.group.company_mode, "update_task_status", return_value="ok"
        ) as update, patch.object(self.group, "post_to_group", new=AsyncMock()), patch.object(
            self.group, "post_agent_answer_to_group", new=AsyncMock()
        ):
            started = time.monotonic()
            outcome = await self.group._execute_routed_task(project, task, None, "prompt", sink)
            elapsed = time.monotonic() - started

        self.assertEqual(outcome, "blocked")
        self.assertGreaterEqual(elapsed, 0.025)
        self.assertEqual(self.group.main.ask_ai.call_count, 1)
        self.assertTrue(any(len(call.args) > 1 and call.args[1] == "needs_human" for call in update.call_args_list))

    async def test_blocked_worker_closes_project_and_releases_later_reservations(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "company.json"
            company_mode.set_daily_budget(1.0, path)
            company_mode.assign_goal(
                "Bounded project", ["manager", "editor"], path=path,
                tasks=[
                    {"owner": "manager", "title": "Work", "estimate_usd": 0.10},
                    {"owner": "editor", "title": "Review", "estimate_usd": 0.10},
                ],
            )
            _message, project_id = company_mode.approve_project(path, notify_hooks=False)

            async def block_current(_project, current_task):
                company_mode.update_task_status(
                    current_task["id"], "needs_human", "Missing access", [], 0.0, path,
                    failure_classification="missing_access",
                )
                return "blocked"

            original_load_state = company_mode.load_state
            with patch.object(self.group.company_mode, "COMPANY_STATE_FILE", path), patch.object(
                self.group.company_mode,
                "load_state",
                side_effect=lambda selected_path=path: original_load_state(selected_path),
            ), patch.object(
                self.group, "_run_one_task", side_effect=block_current
            ), patch.object(self.group, "post_to_group", new=AsyncMock()):
                await self.group.run_company_plan(project_id)
            final = company_mode.load_state(path)
            project = next(value for value in final["projects"] if value["id"] == project_id)
            tasks = company_mode.project_tasks(final, project_id)

        self.assertEqual(
            project["status"], "blocked", msg=str(self.group.main.logger.error.call_args_list)
        )
        self.assertEqual(tasks[1]["status"], "blocked")
        self.assertEqual(tasks[1]["reserved_usd"], 0.0)
        self.assertEqual(final["company"]["reserved_today_usd"], 0.0)
        self.assertEqual(company_mode.open_projects(final), [])

    async def test_crashed_runner_blocks_project_charges_current_hold_and_releases_later_hold(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "company.json"
            company_mode.set_daily_budget(1.0, path)
            company_mode.assign_goal(
                "Crash-safe project", ["general", "editor"], path=path,
                tasks=[
                    {"owner": "general", "title": "Work", "estimate_usd": 0.10},
                    {"owner": "editor", "title": "Review", "estimate_usd": 0.10},
                ],
            )
            _message, project_id = company_mode.approve_project(path, notify_hooks=False)

            async def crash_after_start(_project, current_task):
                company_mode.update_task_status(
                    current_task["id"], "in_progress", path=path,
                    model="worker-model", model_reason="test crash cleanup",
                )
                raise RuntimeError("simulated runner crash")

            original_load_state = company_mode.load_state
            with patch.object(self.group.company_mode, "COMPANY_STATE_FILE", path), patch.object(
                self.group.company_mode,
                "load_state",
                side_effect=lambda selected_path=path: original_load_state(selected_path),
            ), patch.object(
                self.group, "_run_one_task", side_effect=crash_after_start
            ), patch.object(self.group, "post_to_group", new=AsyncMock()):
                await self.group.run_company_plan(project_id)
            final = original_load_state(path)
            project = next(value for value in final["projects"] if value["id"] == project_id)
            current, later = company_mode.project_tasks(final, project_id)

        self.assertEqual(project["status"], "blocked")
        self.assertEqual(current["status"], "needs_human")
        self.assertEqual(current["cost_basis"], "estimated")
        self.assertEqual(current["spent_usd"], 0.10)
        self.assertEqual(later["status"], "blocked")
        self.assertEqual(later["spent_usd"], 0.0)
        self.assertEqual(final["company"]["reserved_today_usd"], 0.0)
        self.assertEqual(final["company"]["spent_today_usd"], 0.10)
        self.assertEqual(company_mode.open_projects(final), [])

    async def test_cancelled_runner_blocks_project_and_releases_reservations(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "company.json"
            company_mode.set_daily_budget(1.0, path)
            company_mode.assign_goal(
                "Cancellation-safe project", ["general", "editor"], path=path,
                tasks=[
                    {"owner": "general", "title": "Work", "estimate_usd": 0.10},
                    {"owner": "editor", "title": "Review", "estimate_usd": 0.10},
                ],
            )
            _message, project_id = company_mode.approve_project(path, notify_hooks=False)

            async def cancel_after_start(_project, current_task):
                company_mode.update_task_status(
                    current_task["id"], "in_progress", path=path,
                    model="worker-model", model_reason="test cancellation cleanup",
                )
                raise asyncio.CancelledError()

            original_load_state = company_mode.load_state
            with patch.object(self.group.company_mode, "COMPANY_STATE_FILE", path), patch.object(
                self.group.company_mode,
                "load_state",
                side_effect=lambda selected_path=path: original_load_state(selected_path),
            ), patch.object(
                self.group, "_run_one_task", side_effect=cancel_after_start
            ), patch.object(self.group, "post_to_group", new=AsyncMock()):
                with self.assertRaises(asyncio.CancelledError):
                    await self.group.run_company_plan(project_id)
            final = original_load_state(path)
            project = next(value for value in final["projects"] if value["id"] == project_id)
            current, later = company_mode.project_tasks(final, project_id)

        self.assertEqual(project["status"], "blocked")
        self.assertEqual(current["status"], "needs_human")
        self.assertEqual(current["cost_basis"], "estimated")
        self.assertEqual(current["spent_usd"], 0.10)
        self.assertEqual(later["status"], "blocked")
        self.assertEqual(final["company"]["reserved_today_usd"], 0.0)
        self.assertEqual(company_mode.open_projects(final), [])


if __name__ == "__main__":
    unittest.main()

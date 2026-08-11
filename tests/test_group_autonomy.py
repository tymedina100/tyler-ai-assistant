import asyncio
import importlib
import json
import os
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import autonomous_workflow
import company_mode


CHAT_PROMPT_MARKERS = (
    "OWNER ACTION NEEDED",
    "Autonomous deliverable",
    "trigger=",
    "human_review=",
    "Model:",
    "Routing:",
    "Task:",
    "Agent:",
    "Attempted:",
    "Blocked by:",
    "Question:",
    "Why:",
    "Result:",
    "AUTONOMY_HELP_REQUEST",
    "FINAL ANSWER",
    "FILES_CHANGED:",
    "REVISIONS REQUIRED",
    "BLOCKED - NEEDS HUMAN REVIEW",
)


def assert_conversational_chat(test_case, text):
    for marker in CHAT_PROMPT_MARKERS:
        test_case.assertNotIn(marker, text)


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
    fake_main.get_pending_action = lambda: fake_main.pending_actions.get("group")
    fake_main.set_pending_action = lambda action: fake_main.pending_actions.__setitem__(
        "group", action
    )
    fake_main.clear_pending_action = lambda: fake_main.pending_actions.pop("group", None)
    fake_main.describe_pending_action = lambda pending: str(
        pending.get("type") or "staged action"
    )
    fake_main.confirm_pending_action = Mock(return_value="confirmed")
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

    def setUp(self):
        self.fake_main.pending_actions.clear()
        self.group.autonomy_runner_task = None
        self.group.company_runner_task = None
        self.group.TEAM_SMOKE_SEND_INTERVAL_SECONDS = 0
        self.pending_action_guard_patcher = patch.object(
            self.group.company_mode,
            "require_no_pending_revenue_action",
            return_value=None,
        )
        self.pending_action_guard = self.pending_action_guard_patcher.start()
        self.addCleanup(self.pending_action_guard_patcher.stop)

    async def test_telegram_roster_check_retries_without_logging_exception_text(self):
        self.fake_main.logger.warning.reset_mock()
        calls = 0

        async def flaky_identity_check():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("https://api.telegram.org/botSECRET/getMe")
            return "verified"

        with patch.object(
            self.group, "TELEGRAM_ROSTER_CHECK_ATTEMPTS", 2
        ), patch.object(
            self.group, "TELEGRAM_ROSTER_RETRY_DELAY_SECONDS", 0
        ):
            ok, value = await self.group._telegram_roster_call(
                "code", "identity", flaky_identity_check
            )

        self.assertTrue(ok)
        self.assertEqual(value, "verified")
        self.assertEqual(calls, 2)
        logged = repr(self.fake_main.logger.warning.mock_calls)
        self.assertIn("RuntimeError", logged)
        self.assertNotIn("botSECRET", logged)

    def test_duplicate_configured_identity_always_fails_startup_policy(self):
        roster = self.group.telegram_roster_health
        agent_info = {
            "manager": {
                "env_var": "TELEGRAM_MANAGER_BOT_TOKEN",
                "label": "Miles",
            },
            "code": {
                "env_var": "TELEGRAM_CODE_BOT_TOKEN",
                "label": "Patch",
            },
            "general": {
                "env_var": "TELEGRAM_GENERAL_BOT_TOKEN",
                "label": "Robin",
            },
        }
        duplicate = "123456:secret-token"
        health = roster.evaluate_roster(
            specialist_keys=("code",),
            agent_info=agent_info,
            token_values={
                "TELEGRAM_MANAGER_BOT_TOKEN": duplicate,
                "TELEGRAM_CODE_BOT_TOKEN": duplicate,
                "TELEGRAM_GENERAL_BOT_TOKEN": "",
            },
            identities={},
            group_memberships={},
        )

        with patch.object(self.group, "BOT_KEYS", ["manager", "code"]):
            with self.assertRaises(SystemExit) as caught:
                self.group._enforce_configured_identity_safety(health)

        self.assertIn("manager, code", str(caught.exception))
        self.assertNotIn(duplicate, str(caught.exception))

    def test_non_identity_roster_issues_remain_allowed_in_relay_mode(self):
        roster = self.group.telegram_roster_health
        health = roster.RosterHealth((
            roster.AgentRosterHealth(
                key="code",
                label="Patch",
                env_var="TELEGRAM_CODE_BOT_TOKEN",
                username="patch_bot",
                issues=(
                    roster.RosterIssue(roster.PRIVACY_ENABLED, "privacy"),
                    roster.RosterIssue(roster.CHECK_UNAVAILABLE, "membership"),
                ),
            ),
        ))

        with patch.object(self.group, "BOT_KEYS", ["code"]):
            self.group._enforce_configured_identity_safety(health)

    async def test_autonomous_team_handoff_bypasses_suppression_as_configured_bot(self):
        manager_bot = object()
        code_bot = object()
        suppression = self.group._suppress_company_updates.set(True)
        try:
            with patch.dict(
                self.group.bots,
                {"manager": manager_bot, "code": code_bot},
                clear=True,
            ), patch.object(
                self.group, "send_chunks", new=AsyncMock()
            ) as send:
                status = await self.group.post_team_handoff(
                    "code", "Vera, the bounded implementation is ready for review."
                )
        finally:
            self.group._suppress_company_updates.reset(suppression)

        send.assert_awaited_once_with(
            code_bot,
            self.group.GROUP_CHAT_ID,
            "Vera, the bounded implementation is ready for review.",
        )
        self.assertEqual(status, "direct")

    async def test_autonomous_team_handoff_uses_explicit_miles_relay_and_char_cap(self):
        manager_bot = object()
        suppression = self.group._suppress_company_updates.set(True)
        try:
            with patch.dict(
                self.group.bots, {"manager": manager_bot}, clear=True
            ), patch.object(
                self.group, "AUTONOMY_TEAM_CHAT_MAX_CHARS", 40
            ), patch.object(
                self.group, "send_chunks", new=AsyncMock()
            ) as send:
                status = await self.group.post_team_handoff("code", "x" * 100)
        finally:
            self.group._suppress_company_updates.reset(suppression)

        relayed = send.await_args.args[2]
        self.assertTrue(relayed.startswith("Code: "))
        self.assertTrue(relayed.endswith("..."))
        self.assertLessEqual(len(relayed), 40)
        self.assertEqual(status, "relayed_by_manager")

    async def test_autonomous_team_handoff_reports_telegram_delivery_failure(self):
        manager_bot = object()
        suppression = self.group._suppress_company_updates.set(True)
        failure_token = self.group._autonomy_team_handoff_failed.set(False)
        try:
            with patch.dict(
                self.group.bots, {"manager": manager_bot}, clear=True
            ), patch.object(
                self.group, "send_chunks", new=AsyncMock(side_effect=RuntimeError("offline"))
            ):
                status = await self.group.post_team_handoff("manager", "Handoff")
            self.assertTrue(self.group._autonomy_team_handoff_failed.get())
        finally:
            self.group._autonomy_team_handoff_failed.reset(failure_token)
            self.group._suppress_company_updates.reset(suppression)

        self.assertEqual(status, "delivery_failed")

    async def test_autonomous_team_handoff_can_be_disabled(self):
        manager_bot = object()
        suppression = self.group._suppress_company_updates.set(True)
        try:
            with patch.dict(
                self.group.bots, {"manager": manager_bot}, clear=True
            ), patch.object(
                self.group, "AUTONOMY_TEAM_CHAT_ENABLED", False
            ), patch.object(
                self.group, "send_chunks", new=AsyncMock()
            ) as send:
                status = await self.group.post_team_handoff("manager", "No broadcast")
        finally:
            self.group._suppress_company_updates.reset(suppression)

        send.assert_not_awaited()
        self.assertEqual(status, "suppressed")

    def _smoke_roster_agent(self, key, *, ready=True):
        roster = self.group.telegram_roster_health
        issues = () if ready else (
            roster.RosterIssue(
                roster.MISSING_TOKEN,
                f"Set {self.group.AGENT_INFO[key]['env_var']}.",
            ),
        )
        return roster.AgentRosterHealth(
            key=key,
            label=self.group.AGENT_INFO[key]["label"],
            env_var=self.group.AGENT_INFO[key]["env_var"],
            username=f"{key}_bot" if ready else "",
            issues=issues,
        )

    async def test_team_smoke_directs_only_ready_bots_and_relays_missing_roles(self):
        manager_bot = object()
        code_bot = object()
        unhealthy_analytics_bot = object()
        roster = self.group.telegram_roster_health.RosterHealth((
            self._smoke_roster_agent("manager"),
            self._smoke_roster_agent("code"),
            self._smoke_roster_agent("analytics", ready=False),
            self._smoke_roster_agent("general", ready=False),
        ))

        with tempfile.TemporaryDirectory() as temp, patch.dict(
            self.group.main.SPECIALISTS,
            {
                "code": self.fake_main.SPECIALISTS["code"],
                "analytics": self.fake_main.SPECIALISTS["analytics"],
            },
            clear=True,
        ), patch.object(
            self.group, "send_chunks", new=AsyncMock()
        ) as send:
            report = await self.group.run_team_transport_smoke(
                bot_map={
                    "manager": manager_bot,
                    "code": code_bot,
                    "analytics": unhealthy_analytics_bot,
                },
                roster_health=roster,
                trigger_source="test",
                report_dir=temp,
                smoke_id="team_smoke_test",
            )

            persisted = json.loads(
                (Path(temp) / "team_smoke_test.json").read_text(encoding="utf-8")
            )

        destination_bots = [call.args[0] for call in send.await_args_list]
        self.assertEqual(
            destination_bots,
            [manager_bot, code_bot, manager_bot, manager_bot, manager_bot],
        )
        self.assertNotIn(unhealthy_analytics_bot, destination_bots)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["expected_count"], 4)
        self.assertEqual(report["direct_count"], 2)
        self.assertEqual(report["relayed_count"], 2)
        self.assertEqual(report["failed_count"], 0)
        self.assertFalse(report["model_invoked"])
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["actual_or_reconciled_cost_usd"], 0.0)
        self.assertFalse(report["roadmap_state_changed"])
        self.assertEqual(persisted["final_delivery"], "direct")
        self.assertEqual(persisted["status"], "partial")

    async def test_team_smoke_passes_only_for_full_direct_expected_roster(self):
        manager_bot = object()
        code_bot = object()
        general_bot = object()
        ghost_bot = object()
        roster_module = self.group.telegram_roster_health
        roster = roster_module.RosterHealth((
            self._smoke_roster_agent("manager"),
            self._smoke_roster_agent("code"),
            self._smoke_roster_agent("general"),
        ))

        with tempfile.TemporaryDirectory() as temp, patch.dict(
            self.group.main.SPECIALISTS,
            {"code": self.fake_main.SPECIALISTS["code"]},
            clear=True,
        ), patch.object(
            self.group, "send_chunks", new=AsyncMock()
        ) as send:
            report = await self.group.run_team_transport_smoke(
                bot_map={
                    "manager": manager_bot,
                    "code": code_bot,
                    "general": general_bot,
                    "ghost": ghost_bot,
                },
                roster_health=roster,
                trigger_source="test",
                report_dir=temp,
                smoke_id="team_smoke_pass",
            )

        destinations = [call.args[0] for call in send.await_args_list]
        self.assertEqual(
            destinations,
            [manager_bot, code_bot, general_bot, manager_bot],
        )
        self.assertNotIn(ghost_bot, destinations)
        self.assertEqual(report["expected_count"], 3)
        self.assertEqual(report["direct_count"], 3)
        self.assertEqual(report["relayed_count"], 0)
        self.assertEqual(report["status"], "passed")

    async def test_team_smoke_missing_health_entry_cannot_pass_as_direct(self):
        manager_bot = object()
        code_bot = object()
        research_bot = object()
        general_bot = object()
        roster = self.group.telegram_roster_health.RosterHealth((
            self._smoke_roster_agent("manager"),
            self._smoke_roster_agent("code"),
            # Research is deliberately absent despite its bot client being present.
            self._smoke_roster_agent("general"),
        ))

        with tempfile.TemporaryDirectory() as temp, patch.dict(
            self.group.main.SPECIALISTS,
            {
                "code": self.fake_main.SPECIALISTS["code"],
                "research": self.fake_main.SPECIALISTS["research"],
            },
            clear=True,
        ), patch.object(
            self.group, "send_chunks", new=AsyncMock()
        ) as send:
            report = await self.group.run_team_transport_smoke(
                bot_map={
                    "manager": manager_bot,
                    "code": code_bot,
                    "research": research_bot,
                    "general": general_bot,
                },
                roster_health=roster,
                trigger_source="test",
                report_dir=temp,
                smoke_id="team_smoke_missing_health",
            )

        destinations = [call.args[0] for call in send.await_args_list]
        self.assertNotIn(research_bot, destinations)
        self.assertEqual(report["direct_count"], 3)
        self.assertEqual(report["relayed_count"], 1)
        self.assertFalse(report["roster_complete"])
        self.assertEqual(report["status"], "partial")

    async def test_team_smoke_continues_after_delivery_failure(self):
        manager_bot = object()
        code_bot = object()
        research_bot = object()
        roster = self.group.telegram_roster_health.RosterHealth((
            self._smoke_roster_agent("manager"),
            self._smoke_roster_agent("code"),
            self._smoke_roster_agent("research"),
            self._smoke_roster_agent("general", ready=False),
        ))
        attempted = []

        async def send_with_one_failure(bot, chat_id, text):
            attempted.append(bot)
            if bot is code_bot:
                raise RuntimeError("https://api.telegram.org/botSECRET/sendMessage")

        self.fake_main.logger.error.reset_mock()
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            self.group.main.SPECIALISTS,
            {
                "code": self.fake_main.SPECIALISTS["code"],
                "research": self.fake_main.SPECIALISTS["research"],
            },
            clear=True,
        ), patch.object(
            self.group, "send_chunks", new=AsyncMock(side_effect=send_with_one_failure)
        ):
            report = await self.group.run_team_transport_smoke(
                bot_map={
                    "manager": manager_bot,
                    "code": code_bot,
                    "research": research_bot,
                },
                roster_health=roster,
                trigger_source="test",
                report_dir=temp,
                smoke_id="team_smoke_failure",
            )

        self.assertEqual(
            attempted,
            [manager_bot, code_bot, research_bot, manager_bot, manager_bot],
        )
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["failed_count"], 1)
        self.assertEqual(report["deliveries"][1]["delivery"], "delivery_failed")
        logged = repr(self.fake_main.logger.error.mock_calls)
        self.assertIn("RuntimeError", logged)
        self.assertNotIn("botSECRET", logged)

    async def test_team_smoke_retries_one_bounded_telegram_rate_limit(self):
        bot = object()
        with patch.object(
            self.group,
            "send_chunks",
            new=AsyncMock(side_effect=[self.group.RetryAfter(0), None]),
        ) as send:
            result = await self.group._team_smoke_send(
                "bounded check",
                "manager",
                {"manager": bot},
            )

        self.assertEqual(
            result,
            "direct",
            repr({
                "send_calls": send.await_args_list,
                "errors": self.fake_main.logger.error.mock_calls,
                "warnings": self.fake_main.logger.warning.mock_calls,
            }),
        )
        self.assertEqual(send.await_count, 2)

    async def test_team_smoke_shared_lock_prevents_cross_process_overlap(self):
        from filelock import FileLock

        with tempfile.TemporaryDirectory() as temp:
            lock_path = Path(temp) / "autonomy_run.lock"
            held = FileLock(str(lock_path))
            held.acquire()
            try:
                with patch.object(
                    self.group, "send_chunks", new=AsyncMock()
                ) as send:
                    with self.assertRaises(self.group.TeamExecutionOverlapError):
                        await self.group.run_team_transport_smoke(
                            bot_map={"manager": object()},
                            trigger_source="test",
                            report_dir=temp,
                            smoke_id="team_smoke_overlap",
                        )
                send.assert_not_awaited()
            finally:
                held.release()

    async def test_company_runner_uses_same_cross_process_execution_gate(self):
        from filelock import FileLock

        with tempfile.TemporaryDirectory() as temp:
            lock_path = Path(temp) / "autonomy_run.lock"
            held = FileLock(str(lock_path))
            held.acquire()
            try:
                with patch.object(
                    self.group,
                    "_run_company_plan_locked",
                    new=AsyncMock(),
                ) as run, patch.object(
                    self.group, "post_to_group", new=AsyncMock()
                ) as post:
                    await self.group.run_company_plan(
                        "project-1",
                        execution_lock_path=lock_path,
                    )
                run.assert_not_awaited()
                self.assertIn("did not start", post.await_args.args[0])
            finally:
                held.release()

    async def test_team_smoke_persistence_failure_sends_nothing(self):
        roster = self.group.telegram_roster_health.RosterHealth((
            self._smoke_roster_agent("manager"),
            self._smoke_roster_agent("general", ready=False),
        ))
        with patch.dict(
            self.group.main.SPECIALISTS, {}, clear=True
        ), patch.object(
            self.group, "_persist_team_smoke_report", side_effect=OSError("offline")
        ), patch.object(
            self.group, "send_chunks", new=AsyncMock()
        ) as send:
            with self.assertRaises(RuntimeError):
                await self.group.run_team_transport_smoke(
                    bot_map={"manager": object()},
                    roster_health=roster,
                    trigger_source="test",
                    smoke_id="team_smoke_no_store",
                )

        send.assert_not_awaited()

    async def test_team_smoke_telegram_trigger_requires_allowlisted_owner(self):
        with self.assertRaises(PermissionError):
            await self.group.run_team_transport_smoke(
                bot_map={"manager": object()},
                trigger_source="telegram",
                requested_by_user_id=999,
                smoke_id="team_smoke_unauthorized",
            )

    async def test_team_smoke_command_rejects_unauthorized_and_bot_authors(self):
        run_smoke = AsyncMock()
        for user in (
            types.SimpleNamespace(id=999, is_bot=False),
            types.SimpleNamespace(id=42, is_bot=True),
        ):
            update = types.SimpleNamespace(
                effective_user=user,
                effective_chat=types.SimpleNamespace(
                    id=self.group.GROUP_CHAT_ID,
                    type="supergroup",
                ),
                message=types.SimpleNamespace(
                    text="/autorun team-smoke",
                    reply_text=AsyncMock(),
                ),
            )
            with patch.object(
                self.group, "run_team_transport_smoke", new=run_smoke
            ):
                await self.group.handle_manager_message(update, None)

        run_smoke.assert_not_awaited()

    async def test_autorun_team_smoke_is_group_only_and_refuses_overlap(self):
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=42),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        run_smoke = AsyncMock(return_value={"final_delivery": "direct"})
        with patch.object(
            self.group, "run_team_transport_smoke", new=run_smoke
        ):
            await self.group.handle_autorun_command(
                update, "/autorun team-smoke", allow_live=False
            )
        run_smoke.assert_not_awaited()
        self.assertIn("group operating room", update.message.reply_text.await_args.args[0])

        update.message.reply_text.reset_mock()
        active = types.SimpleNamespace(done=lambda: False)
        with patch.object(
            self.group, "run_team_transport_smoke", new=run_smoke
        ), patch.object(self.group, "autonomy_runner_task", active):
            await self.group.handle_autorun_command(
                update, "/autorun team-smoke", allow_live=True
            )
        run_smoke.assert_not_awaited()
        self.assertIn("active", update.message.reply_text.await_args.args[0])

    async def test_autorun_team_smoke_calls_no_model_or_budget_path(self):
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=42),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        roster = self.group.telegram_roster_health.RosterHealth((
            self._smoke_roster_agent("manager"),
            self._smoke_roster_agent("general", ready=False),
        ))
        run_smoke = AsyncMock(return_value={"final_delivery": "direct"})
        with patch.object(
            self.group, "telegram_roster_status", roster
        ), patch.object(
            self.group, "run_team_transport_smoke", new=run_smoke
        ), patch.object(
            self.group, "_run_metered", new=AsyncMock()
        ) as metered, patch.object(
            self.group, "_get_autonomy_workflow"
        ) as workflow, patch.object(
            self.group.company_mode, "reserve_budget"
        ) as reserve, patch.object(
            self.group.company_mode, "reconcile_budget"
        ) as reconcile:
            await self.group.handle_autorun_command(
                update, "/autorun team-smoke", allow_live=True
            )

        run_smoke.assert_awaited_once()
        self.assertEqual(
            run_smoke.await_args.kwargs["requested_by_user_id"], 42
        )
        metered.assert_not_awaited()
        workflow.assert_not_called()
        reserve.assert_not_called()
        reconcile.assert_not_called()

    async def test_one_shot_team_smoke_never_starts_polling(self):
        identity_manager = types.SimpleNamespace(
            id=101,
            is_bot=True,
            username="miles_bot",
            can_read_all_group_messages=True,
        )
        identity_code = types.SimpleNamespace(
            id=102,
            is_bot=True,
            username="code_bot",
            can_read_all_group_messages=True,
        )
        member = types.SimpleNamespace(status="member", is_member=True)
        manager_bot = types.SimpleNamespace(
            get_me=AsyncMock(return_value=identity_manager),
            get_chat_member=AsyncMock(return_value=member),
        )
        code_bot = types.SimpleNamespace(
            get_me=AsyncMock(return_value=identity_code),
            get_chat_member=AsyncMock(return_value=member),
        )
        app_manager = types.SimpleNamespace(
            bot=manager_bot,
            initialize=AsyncMock(),
            shutdown=AsyncMock(),
            updater=types.SimpleNamespace(start_polling=AsyncMock()),
        )
        app_code = types.SimpleNamespace(
            bot=code_bot,
            initialize=AsyncMock(),
            shutdown=AsyncMock(),
            updater=types.SimpleNamespace(start_polling=AsyncMock()),
        )
        builder = Mock()
        builder.token.return_value = builder
        builder.build.side_effect = [app_manager, app_code]
        run_smoke = AsyncMock(return_value={"status": "partial"})
        token_env = {
            self.group.AGENT_INFO["manager"]["env_var"]: "manager-token",
            self.group.AGENT_INFO["code"]["env_var"]: "code-token",
        }

        with patch.dict(
            os.environ, token_env, clear=True
        ), patch.object(
            self.group, "ApplicationBuilder", return_value=builder
        ), patch.object(
            self.group, "run_team_transport_smoke", new=run_smoke
        ):
            report = await self.group.run_team_transport_smoke_one_shot()

        self.assertEqual(report["status"], "partial")
        app_manager.initialize.assert_awaited_once()
        app_code.initialize.assert_awaited_once()
        app_manager.updater.start_polling.assert_not_awaited()
        app_code.updater.start_polling.assert_not_awaited()
        app_manager.shutdown.assert_awaited_once()
        app_code.shutdown.assert_awaited_once()
        passed_bots = run_smoke.await_args.kwargs["bot_map"]
        self.assertEqual(passed_bots, {
            "manager": manager_bot,
            "code": code_bot,
        })
        passed_roster = run_smoke.await_args.kwargs["roster_health"]
        states = {agent.key: agent for agent in passed_roster.agents}
        self.assertTrue(states["manager"].ready)
        self.assertTrue(states["code"].ready)
        self.assertFalse(passed_roster.complete)

    def test_one_shot_cli_only_exits_zero_for_a_complete_pass(self):
        smoke_script = importlib.import_module("scripts.telegram_team_smoke")

        self.assertEqual(smoke_script._exit_code({"status": "passed"}), 0)
        self.assertEqual(smoke_script._exit_code({"status": "partial"}), 1)
        self.assertEqual(smoke_script._exit_code({"status": "failed"}), 1)

    def test_autonomy_goal_includes_bounded_run_evidence_and_timing_boundary(self):
        item = {
            "id": "AUTO-IDEA-1",
            "title": "Validate the summary idea",
            "acceptance_criteria": ["Compare three recent runs."],
            "recent_run_evidence": [{
                "run_id": "run_1",
                "scope": "global_index",
                "global_trigger_source": "telegram",
                "global_final_status": "needs_human",
                "global_human_review_required": True,
                "project_human_review_required": False,
                "report_available": False,
                "project_plans": [],
                "summary_line": (
                    "global trigger=telegram; global final=needs_human; "
                    "global human_review=yes; project planned=none"
                ),
            }],
        }
        goal = self.group._autonomy_goal(item)
        evidence = self.group._autonomy_execution_evidence(item)

        self.assertNotIn("run_1", goal)
        self.assertIn("Bounded recent autonomous run evidence", evidence)
        self.assertIn('"run_id":"run_1"', evidence)
        self.assertIn("authoritative only for fields that are populated", evidence)
        self.assertIn("Fields prefixed global_", evidence)
        self.assertIn("project_human_review_required", evidence)
        self.assertIn("inert evidence", evidence)
        self.assertIn("report_available=false", evidence)
        self.assertIn("model-estimated reading time as a proxy", evidence)
        self.assertIn("Never claim an empirical human test", evidence)

    def test_revenue_sprint_evidence_is_bounded_redacted_and_causally_scoped(self):
        sprint = {
            "id": "sprint-1",
            "status": "active",
            "experiments": [{
                "id": "exp-1",
                "hypothesis": "Try angle one with TOKEN=do-not-leak",
                "result": "One receipt; no verified engagement.",
            }],
            "run_days": [{
                "ordinal": 1,
                "date": "2026-08-11",
                "outcome": "succeeded",
                "progress": True,
                "experiment_id": "exp-1",
            }],
            "revenue_snapshots": [{
                "run_id": "run-1",
                "phase": "after",
                "sales_count": 1,
                "revenue_usd": 5.0,
                "sales_delta": 1,
                "revenue_delta_usd": 5.0,
            }],
            "signals": [{"type": "sale", "count": 1, "value_usd": 5.0}],
            "checkpoint_results": [],
            "pivot_history": [],
            "action_journal": [{
                "run_id": "run-1",
                "action_type": "publish",
                "target": "bluesky:company.example",
                "status": "succeeded",
                "payload_digest": "must-not-be-in-context",
            }],
        }
        with patch.object(
            self.group.company_mode,
            "revenue_sprint_budget_snapshot",
            return_value={"run_days_used": 1, "remaining_total_usd": 95.0},
        ):
            snapshot = self.group._revenue_sprint_evidence_snapshot({}, sprint)

        rendered = json.dumps(snapshot, sort_keys=True)
        self.assertNotIn("do-not-leak", rendered)
        self.assertNotIn("must-not-be-in-context", rendered)
        self.assertNotIn("payload_digest", rendered)
        self.assertEqual(snapshot["signal_totals"]["sale"]["count"], 1)
        evidence = self.group._autonomy_execution_evidence({
            "revenue_sprint_evidence": snapshot,
        })
        self.assertIn("Bounded Revenue Sprint evidence", evidence)
        self.assertIn("receipt proves an action, not engagement or a sale", evidence)
        self.assertIn("may not identify the exact causal post", evidence)

    def test_day_six_experiment_persists_one_checkpoint_driven_variable(self):
        item = {
            "id": "D06",
            "title": "Pivot",
            "description": "Change one evidenced variable.",
            "revenue_sprint_run_day": 6,
            "external_action": {"action_type": "publish"},
        }
        sprint = {
            "checkpoint_results": [{
                "day": 5,
                "decision": "pivot",
                "evidence": {"meaningful_interest_count": 0},
            }],
        }

        experiment = self.group._campaign_experiment(item, sprint)

        self.assertEqual(experiment["changed_variable"], "call_to_action")
        self.assertIn("Day-5 decision=pivot", experiment["evidence_basis"])
        self.assertIn("meaningful_interest_count", experiment["evidence_basis"])

    def test_structured_worker_block_preserves_specific_failure_category(self):
        self.assertEqual(
            self.group._answer_failure_classification(
                "BLOCKED - NEEDS HUMAN REVIEW: MISSING_ACCESS\n"
                "The required source cannot be accessed."
            ),
            "missing_access",
        )
        self.assertIsNone(
            self.group._answer_failure_classification(
                "The proposal explains why access controls are important."
            )
        )

    async def test_run_one_task_appends_transient_evidence_to_the_actual_prompt(self):
        project = {"id": "project-1", "goal": "Inspect recent runs"}
        task = {
            "id": "task-1",
            "owner": "general",
            "title": "Inspect",
            "authorization_level": "observe",
            "enforce_authorization": True,
            "reserved_usd": 0.1,
        }
        evidence = "Bounded evidence: run_prior_evidence"
        token = self.group._autonomy_evidence_context.set(evidence)
        try:
            with patch.object(
                self.group.company_mode,
                "load_state",
                return_value={"company": {}, "tasks": []},
            ), patch.object(
                self.group.company_mode,
                "prior_work_summary",
                return_value="",
            ), patch.object(
                self.group,
                "_load_project_deliverable",
                return_value=(None, None),
            ), patch.object(
                self.group,
                "_execute_routed_task",
                new=AsyncMock(return_value="done"),
            ) as routed:
                result = await self.group._run_one_task(project, task)
                revision_task = dict(
                    task,
                    id="revision-1",
                    title="Revise the evidence review",
                )
                revision_result = await self.group._run_one_task(
                    project,
                    revision_task,
                )
        finally:
            self.group._autonomy_evidence_context.reset(token)

        self.assertEqual(result, "done")
        self.assertEqual(revision_result, "done")
        self.assertEqual(len(routed.await_args_list), 2)
        self.assertTrue(
            all(evidence in call.args[3] for call in routed.await_args_list)
        )
        self.assertEqual(self.group._autonomy_evidence_context.get(), "")

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
            "telegram_summary": "Dry run complete. Nothing was executed or changed.",
            "report_path": "C:/tmp/run.json",
        }
        with patch.object(
            self.group, "_run_autonomy_session", new=AsyncMock(return_value=report)
        ) as run, patch.object(self.group, "reply_chunks", new=AsyncMock()) as reply:
            await self.group.handle_autorun_command(update, "/autorun dry-run")
        run.assert_awaited_once_with("telegram", dry_run=True)
        message = reply.await_args.args[1]
        self.assertIn("Dry run complete", message)
        self.assertIn("saved the full dry-run record", message)
        self.assertNotIn("C:/tmp/run.json", message)
        assert_conversational_chat(self, message)

    async def test_manual_live_command_starts_one_bounded_session(self):
        update = types.SimpleNamespace(message=types.SimpleNamespace(reply_text=AsyncMock()))
        config = autonomous_workflow.AutonomyConfig(enabled=True, dry_run=False)
        report = {
            "dry_run": False,
            "cycle_reports": [],
            "escalations": [],
            "telegram_summary": "We're done for this session.",
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
            [("We're done for this session.", "manager")],
        )
        acknowledgement = update.message.reply_text.await_args.args[0]
        self.assertTrue(acknowledgement.startswith("Got it"))
        self.assertIn("highest-priority ready item", acknowledgement)
        self.assertIn("today's limits", acknowledgement)
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

    async def test_autorun_queue_stages_owner_confirmation_without_mutating_state(self):
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=42),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        preview = {
            "manifest_id": "assistant-production-v1",
            "manifest_revision": "revision-pack-1",
            "project_id": "assistant",
            "project_name": "Tyler AI Assistant",
            "goal_id": "assistant-production-autonomy",
            "goal_title": "Harden production autonomy",
            "item_count": 3,
            "roadmap_item_ids": ["AUTO-PROD-001", "AUTO-PROD-002", "AUTO-PROD-003"],
            "authorization_levels": ["observe", "propose"],
            "already_queued": False,
        }
        workflow = types.SimpleNamespace(
            preview_roadmap_pack=Mock(return_value=(True, preview)),
            queue_roadmap_pack=Mock(),
        )
        with patch.object(self.group, "autonomy_runner_task", None), patch.object(
            self.group, "_get_autonomy_workflow", return_value=workflow
        ), patch.object(
            self.group, "_run_autonomy_session", new=AsyncMock()
        ) as run, patch.object(
            self.group, "reply_chunks", new=AsyncMock()
        ) as reply:
            await self.group.handle_autorun_command(
                update, "/autorun queue assistant-production-v1", allow_live=True
            )

        workflow.preview_roadmap_pack.assert_called_once_with("assistant-production-v1")
        workflow.queue_roadmap_pack.assert_not_called()
        run.assert_not_awaited()
        pending = self.fake_main.pending_actions["group"]
        self.assertEqual(pending["type"], "autonomy_roadmap_pack")
        self.assertEqual(pending["manifest_id"], "assistant-production-v1")
        self.assertEqual(pending["expected_revision"], "revision-pack-1")
        self.assertEqual(pending["project_id"], "assistant")
        self.assertEqual(pending["goal_id"], "assistant-production-autonomy")
        self.assertEqual(pending["item_count"], 3)
        self.assertEqual(pending["requested_by_user_id"], 42)
        rendered = reply.await_args.args[1]
        self.assertIn("assistant-production-v1", rendered)
        self.assertIn("revision-pack-1", rendered)
        self.assertIn("Tyler AI Assistant", rendered)
        self.assertIn("Harden production autonomy", rendered)
        self.assertIn("AUTO-PROD-001", rendered)
        self.assertIn("observe, propose", rendered)
        self.assertIn("Already queued: no", rendered)
        self.assertIn("Reply /confirm", rendered)

    async def test_autorun_queue_reports_an_already_queued_pack_without_staging(self):
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=42),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        preview = {
            "manifest_id": "assistant-production-v1",
            "manifest_revision": "revision-pack-1",
            "project_id": "assistant",
            "project_name": "Tyler AI Assistant",
            "goal_id": "assistant-production-autonomy",
            "goal_title": "Harden production autonomy",
            "item_count": 3,
            "roadmap_item_ids": ["AUTO-PROD-001", "AUTO-PROD-002", "AUTO-PROD-003"],
            "authorization_levels": ["propose"],
            "already_queued": True,
        }
        workflow = types.SimpleNamespace(
            preview_roadmap_pack=Mock(return_value=(True, preview)),
            queue_roadmap_pack=Mock(),
        )
        with patch.object(
            self.group, "_get_autonomy_workflow", return_value=workflow
        ), patch.object(self.group, "reply_chunks", new=AsyncMock()) as reply:
            await self.group.handle_autorun_command(
                update, "/autorun queue assistant-production-v1", allow_live=True
            )

        workflow.queue_roadmap_pack.assert_not_called()
        self.assertNotIn("group", self.fake_main.pending_actions)
        rendered = reply.await_args.args[1]
        self.assertIn("already queued", rendered.lower())
        self.assertIn("Already queued: yes", rendered)
        self.assertIn("No approval was staged", rendered)

    async def test_already_queued_revenue_pack_can_restage_missing_activation(self):
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=42),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        sprint = {
            "id": "sprint-1",
            "product": {"name": "Kit", "url": "https://example.test/kit"},
            "channel": {"id": "bluesky:company.example"},
            "total_ai_budget_usd": 100.0,
            "daily_ai_budget_usd": 5.0,
            "run_days": 20,
            "action_policy": {
                "revision": "policy-1",
                "allowed_external_actions": [{
                    "action_type": "publish",
                    "target": "bluesky:company.example",
                    "daily_cap": 1,
                    "total_cap": 20,
                }],
            },
        }
        preview = {
            "manifest_id": "revenue-pack-v1",
            "manifest_revision": "revision-pack-1",
            "project_id": "assistant",
            "project_name": "Assistant",
            "goal_id": "revenue-goal",
            "goal_title": "Validate revenue",
            "item_count": 20,
            "roadmap_item_ids": ["D01"],
            "authorization_levels": ["external_action"],
            "already_queued": True,
            "revenue_sprint": sprint,
        }
        workflow = types.SimpleNamespace(
            preview_roadmap_pack=Mock(return_value=(True, preview)),
            queue_roadmap_pack=Mock(),
        )
        with patch.object(
            self.group, "_get_autonomy_workflow", return_value=workflow
        ), patch.object(
            self.group.company_mode, "load_state", return_value={"revenue_sprints": []}
        ), patch.object(
            self.group.company_mode, "active_revenue_sprint", return_value=None
        ), patch.object(
            self.group, "reply_chunks", new=AsyncMock()
        ) as reply:
            await self.group.handle_autorun_command(
                update, "/autorun queue revenue-pack-v1", allow_live=True
            )

        pending = self.fake_main.pending_actions["group"]
        self.assertTrue(pending["activation_only"])
        self.assertEqual(pending["revenue_sprint_id"], "sprint-1")
        self.assertIn("activation staged", reply.await_args.args[1].lower())
        self.assertIn("Reply /confirm", reply.await_args.args[1])
        workflow.queue_roadmap_pack.assert_not_called()

    async def test_confirmed_activation_recovery_runs_preflight_without_reimport(self):
        sprint = {
            "id": "sprint-1",
            "product": {"name": "Kit", "url": "https://example.test/kit"},
            "channel": {"id": "bluesky:company.example"},
            "action_policy": {"revision": "policy-1", "allowed_external_actions": []},
        }
        self.fake_main.pending_actions["group"] = {
            "type": "autonomy_roadmap_pack",
            "manifest_id": "revenue-pack-v1",
            "expected_revision": "revision-pack-1",
            "revenue_sprint_id": "sprint-1",
            "activation_only": True,
            "requested_by_user_id": 42,
        }
        workflow = types.SimpleNamespace(
            preview_roadmap_pack=Mock(return_value=(True, {
                "manifest_id": "revenue-pack-v1",
                "manifest_revision": "revision-pack-1",
                "already_queued": True,
                "revenue_sprint": sprint,
            })),
            queue_roadmap_pack=Mock(return_value=(
                True,
                "Roadmap pack was already queued; no duplicate items were created.",
            )),
        )
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=42),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        with patch.object(
            self.group, "_get_autonomy_workflow", return_value=workflow
        ), patch.object(
            self.group,
            "_activate_revenue_sprint",
            new=AsyncMock(return_value={"id": "sprint-1"}),
        ) as activate, patch.object(
            self.group, "reply_chunks", new=AsyncMock()
        ) as reply:
            handled = await self.group._handle_pending_confirmation(update, "/confirm")

        self.assertTrue(handled)
        activate.assert_awaited_once_with(
            sprint,
            approval_source="telegram_owner:42",
        )
        self.assertNotIn("group", self.fake_main.pending_actions)
        self.assertIn("Activated Revenue Sprint sprint-1", reply.await_args.args[1])

    async def test_autorun_queue_requires_manifest_group_and_idle_runners(self):
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=42),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        workflow = types.SimpleNamespace(preview_roadmap_pack=Mock())

        await self.group.handle_autorun_command(
            update, "/autorun queue", allow_live=True
        )
        self.assertIn("Usage", update.message.reply_text.await_args.args[0])

        update.message.reply_text.reset_mock()
        with patch.object(self.group, "_get_autonomy_workflow", return_value=workflow):
            await self.group.handle_autorun_command(
                update, "/autorun queue assistant-production-v1", allow_live=False
            )
        self.assertIn("group operating room", update.message.reply_text.await_args.args[0])
        workflow.preview_roadmap_pack.assert_not_called()

        update.message.reply_text.reset_mock()
        active = types.SimpleNamespace(done=lambda: False)
        with patch.object(self.group, "autonomy_runner_task", active), patch.object(
            self.group, "_get_autonomy_workflow", return_value=workflow
        ):
            await self.group.handle_autorun_command(
                update, "/autorun queue assistant-production-v1", allow_live=True
            )
        self.assertIn("already active", update.message.reply_text.await_args.args[0])
        workflow.preview_roadmap_pack.assert_not_called()

        update.message.reply_text.reset_mock()
        with patch.object(self.group, "autonomy_runner_task", None), patch.object(
            self.group, "company_runner_task", active
        ), patch.object(self.group, "_get_autonomy_workflow", return_value=workflow):
            await self.group.handle_autorun_command(
                update, "/autorun queue assistant-production-v1", allow_live=True
            )
        self.assertIn("Company Mode is already running", update.message.reply_text.await_args.args[0])
        workflow.preview_roadmap_pack.assert_not_called()

    async def test_confirmed_roadmap_pack_queues_once_and_clears_approval(self):
        self.fake_main.pending_actions["group"] = {
            "type": "autonomy_roadmap_pack",
            "manifest_id": "assistant-production-v1",
            "expected_revision": "revision-pack-1",
            "project_id": "assistant",
            "goal_id": "assistant-production-autonomy",
            "item_count": 3,
            "requested_by_user_id": 42,
        }
        workflow = types.SimpleNamespace(
            preview_roadmap_pack=Mock(return_value=(True, {
                "manifest_id": "assistant-production-v1",
                "manifest_revision": "revision-pack-1",
            })),
            queue_roadmap_pack=Mock(return_value=(
                True,
                "Queued 3 ready roadmap items. No autonomous run was started.",
            ))
        )
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=42),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        with patch.object(
            self.group, "_get_autonomy_workflow", return_value=workflow
        ), patch.object(self.group, "reply_chunks", new=AsyncMock()) as reply:
            handled = await self.group._handle_pending_confirmation(
                update, "/confirm@TyManagerBot"
            )

        self.assertTrue(handled)
        workflow.queue_roadmap_pack.assert_called_once_with(
            "assistant-production-v1",
            expected_revision="revision-pack-1",
            approval_source="telegram_owner:42",
        )
        self.assertNotIn("group", self.fake_main.pending_actions)
        self.assertIn("Queued 3", reply.await_args.args[1])

    async def test_roadmap_pack_confirmation_is_owner_bound_and_cancel_is_safe(self):
        pending = {
            "type": "autonomy_roadmap_pack",
            "manifest_id": "assistant-production-v1",
            "expected_revision": "revision-pack-1",
            "requested_by_user_id": 42,
        }
        self.fake_main.pending_actions["group"] = pending
        workflow = types.SimpleNamespace(queue_roadmap_pack=Mock())
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=99),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        with patch.object(self.group, "_get_autonomy_workflow", return_value=workflow):
            handled = await self.group._handle_pending_confirmation(update, "/confirm")

        self.assertTrue(handled)
        workflow.queue_roadmap_pack.assert_not_called()
        self.assertIn("group", self.fake_main.pending_actions)
        self.assertIn("Only the owner", update.message.reply_text.await_args.args[0])

        update.effective_user.id = 42
        update.message.reply_text.reset_mock()
        handled = await self.group._handle_pending_confirmation(update, "/cancel")
        self.assertTrue(handled)
        self.assertNotIn("group", self.fake_main.pending_actions)
        self.assertIn("No roadmap state changed", update.message.reply_text.await_args.args[0])
        workflow.queue_roadmap_pack.assert_not_called()

    async def test_failed_roadmap_pack_confirmation_retains_staged_approval(self):
        self.fake_main.pending_actions["group"] = {
            "type": "autonomy_roadmap_pack",
            "manifest_id": "assistant-production-v1",
            "expected_revision": "revision-pack-1",
            "requested_by_user_id": 42,
        }
        workflow = types.SimpleNamespace(
            preview_roadmap_pack=Mock(return_value=(True, {
                "manifest_id": "assistant-production-v1",
                "manifest_revision": "revision-pack-1",
            })),
            queue_roadmap_pack=Mock(return_value=(
                False,
                "Another autonomous run holds the lock.",
            ))
        )
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=42),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        with patch.object(
            self.group, "_get_autonomy_workflow", return_value=workflow
        ), patch.object(self.group, "reply_chunks", new=AsyncMock()) as reply:
            await self.group._handle_pending_confirmation(update, "/confirm")

        self.assertIn("group", self.fake_main.pending_actions)
        self.assertIn("approval remains staged", reply.await_args.args[1])

    async def test_autorun_promote_stages_owner_confirmation_without_mutating_state(self):
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=42),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        preview = {
            "idea_id": "idea-1",
            "idea": "Add a concise run summary",
            "problem_addressed": "Outcomes are hard to scan.",
            "expected_value": "Faster owner triage.",
            "project_id": "assistant",
            "project_name": "Tyler AI Assistant",
            "goal_id": "assistant-autonomy",
            "roadmap_item_id": "AUTO-IDEA-1",
            "title": "Add a concise run summary",
            "status": "ready",
            "authorization_level": "propose",
            "acceptance_criteria": ["Draft three examples.", "Record a recommendation."],
            "estimated_ai_cost_usd": 0.01,
            "proposal_revision": "revision-1",
        }
        workflow = types.SimpleNamespace(
            preview_idea_promotion=Mock(return_value=(True, preview)),
            promote_idea=Mock(),
        )
        with patch.object(self.group, "autonomy_runner_task", None), patch.object(
            self.group, "_get_autonomy_workflow", return_value=workflow
        ), patch.object(
            self.group, "_run_autonomy_session", new=AsyncMock()
        ) as run, patch.object(
            self.group, "reply_chunks", new=AsyncMock()
        ) as reply:
            await self.group.handle_autorun_command(
                update, "/autorun promote idea-1", allow_live=True
            )

        workflow.preview_idea_promotion.assert_called_once_with("idea-1", None)
        workflow.promote_idea.assert_not_called()
        run.assert_not_awaited()
        pending = self.fake_main.pending_actions["group"]
        self.assertEqual(pending["type"], "autonomy_idea_promotion")
        self.assertEqual(pending["idea_id"], "idea-1")
        self.assertEqual(pending["expected_revision"], "revision-1")
        self.assertEqual(pending["expected_goal_id"], "assistant-autonomy")
        self.assertEqual(pending["requested_by_user_id"], 42)
        rendered = reply.await_args.args[1]
        self.assertIn("AUTO-IDEA-1", rendered)
        self.assertIn("Acceptance criteria", rendered)
        self.assertIn("Reply /confirm", rendered)
        self.assertIn("no model was invoked", rendered.lower())

    async def test_autorun_promote_requires_id_group_and_idle_runner(self):
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=42),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        workflow = types.SimpleNamespace(preview_idea_promotion=Mock())

        await self.group.handle_autorun_command(
            update, "/autorun promote", allow_live=True
        )
        self.assertIn("Usage", update.message.reply_text.await_args.args[0])

        update.message.reply_text.reset_mock()
        with patch.object(self.group, "_get_autonomy_workflow", return_value=workflow):
            await self.group.handle_autorun_command(
                update, "/autorun promote idea-1", allow_live=False
            )
        self.assertIn("group operating room", update.message.reply_text.await_args.args[0])
        workflow.preview_idea_promotion.assert_not_called()

        update.message.reply_text.reset_mock()
        active = types.SimpleNamespace(done=lambda: False)
        with patch.object(self.group, "autonomy_runner_task", active), patch.object(
            self.group, "_get_autonomy_workflow", return_value=workflow
        ):
            await self.group.handle_autorun_command(
                update, "/autorun promote idea-1", allow_live=True
            )
        self.assertIn("already active", update.message.reply_text.await_args.args[0])
        workflow.preview_idea_promotion.assert_not_called()

        update.message.reply_text.reset_mock()
        with patch.object(self.group, "autonomy_runner_task", None), patch.object(
            self.group, "company_runner_task", active
        ), patch.object(self.group, "_get_autonomy_workflow", return_value=workflow):
            await self.group.handle_autorun_command(
                update, "/autorun promote idea-1", allow_live=True
            )
        self.assertIn("Company Mode is already running", update.message.reply_text.await_args.args[0])
        workflow.preview_idea_promotion.assert_not_called()

    async def test_autorun_promote_rejects_ambiguous_preview_without_staging(self):
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=42),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        workflow = types.SimpleNamespace(
            preview_idea_promotion=Mock(return_value=(
                False,
                "Several active projects could receive this idea.",
            ))
        )
        with patch.object(self.group, "autonomy_runner_task", None), patch.object(
            self.group, "_get_autonomy_workflow", return_value=workflow
        ):
            await self.group.handle_autorun_command(
                update, "/autorun promote idea-1", allow_live=True
            )

        self.assertNotIn("group", self.fake_main.pending_actions)
        self.assertIn("Several active projects", update.message.reply_text.await_args.args[0])

    async def test_confirmed_idea_promotion_queues_once_and_clears_approval(self):
        pending = {
            "type": "autonomy_idea_promotion",
            "idea_id": "idea-1",
            "project_id": "assistant",
            "expected_revision": "revision-1",
            "expected_roadmap_item_id": "AUTO-IDEA-1",
            "expected_goal_id": "assistant-autonomy",
            "requested_by_user_id": 42,
        }
        self.fake_main.pending_actions["group"] = pending
        workflow = types.SimpleNamespace(
            promote_idea=Mock(return_value=(
                True,
                "Promoted idea to ready roadmap work. No autonomous run was started.",
            ))
        )
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=42),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        with patch.object(
            self.group, "_get_autonomy_workflow", return_value=workflow
        ), patch.object(self.group, "reply_chunks", new=AsyncMock()) as reply:
            handled = await self.group._handle_pending_confirmation(
                update, "/confirm@TyManagerBot"
            )

        self.assertTrue(handled)
        workflow.promote_idea.assert_called_once_with(
            "idea-1",
            project_id="assistant",
            expected_revision="revision-1",
            expected_roadmap_item_id="AUTO-IDEA-1",
            expected_goal_id="assistant-autonomy",
            approval_source="telegram_owner:42",
        )
        self.assertNotIn("group", self.fake_main.pending_actions)
        self.assertIn("ready roadmap", reply.await_args.args[1])

    async def test_failed_promotion_confirmation_retains_approval_and_cancel_is_safe(self):
        pending = {
            "type": "autonomy_idea_promotion",
            "idea_id": "idea-1",
            "project_id": "assistant",
            "expected_revision": "revision-1",
            "expected_roadmap_item_id": "AUTO-IDEA-1",
            "requested_by_user_id": 42,
        }
        self.fake_main.pending_actions["group"] = pending
        workflow = types.SimpleNamespace(
            promote_idea=Mock(return_value=(False, "Another autonomous run holds the lock."))
        )
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=42),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        with patch.object(
            self.group, "_get_autonomy_workflow", return_value=workflow
        ), patch.object(self.group, "reply_chunks", new=AsyncMock()) as reply:
            await self.group._handle_pending_confirmation(update, "/confirm")
        self.assertIn("group", self.fake_main.pending_actions)
        self.assertIn("approval remains staged", reply.await_args.args[1])

        update.message.reply_text.reset_mock()
        await self.group._handle_pending_confirmation(update, "/autorun status")
        self.assertIn("group", self.fake_main.pending_actions)
        self.assertIn("still staged", update.message.reply_text.await_args.args[0])
        self.assertEqual(workflow.promote_idea.call_count, 1)

        with patch.object(self.group, "_get_autonomy_workflow", return_value=workflow):
            await self.group._handle_pending_confirmation(update, "/cancel")
        self.assertNotIn("group", self.fake_main.pending_actions)
        self.assertIn("remains proposed", update.message.reply_text.await_args.args[0])
        self.assertEqual(workflow.promote_idea.call_count, 1)

    async def test_promotion_persistence_exception_retains_staged_approval(self):
        self.fake_main.pending_actions["group"] = {
            "type": "autonomy_idea_promotion",
            "idea_id": "idea-1",
            "project_id": "assistant",
            "expected_revision": "revision-1",
            "expected_roadmap_item_id": "AUTO-IDEA-1",
            "requested_by_user_id": 42,
        }
        workflow = types.SimpleNamespace(
            promote_idea=Mock(side_effect=OSError("simulated persistent write failure"))
        )
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=42),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        with patch.object(self.group, "_get_autonomy_workflow", return_value=workflow):
            await self.group._handle_pending_confirmation(update, "/confirm")

        self.assertIn("group", self.fake_main.pending_actions)
        self.assertIn("not persisted safely", update.message.reply_text.await_args.args[0])
        self.assertNotIn("simulated", update.message.reply_text.await_args.args[0])

    async def test_wrong_user_cannot_confirm_staged_idea_promotion(self):
        self.fake_main.pending_actions["group"] = {
            "type": "autonomy_idea_promotion",
            "idea_id": "idea-1",
            "project_id": "assistant",
            "expected_revision": "revision-1",
            "expected_roadmap_item_id": "AUTO-IDEA-1",
            "requested_by_user_id": 42,
        }
        workflow = types.SimpleNamespace(promote_idea=Mock())
        update = types.SimpleNamespace(
            effective_user=types.SimpleNamespace(id=99),
            message=types.SimpleNamespace(reply_text=AsyncMock()),
        )
        with patch.object(self.group, "_get_autonomy_workflow", return_value=workflow):
            await self.group._handle_pending_confirmation(update, "/confirm")

        workflow.promote_idea.assert_not_called()
        self.assertIn("group", self.fake_main.pending_actions)
        self.assertIn("Only the owner", update.message.reply_text.await_args.args[0])

    async def test_autonomy_status_lists_stable_proposed_idea_ids(self):
        workflow = types.SimpleNamespace(
            load_state=Mock(return_value={
                "run_control": {"recent_runs": []},
                "idea_backlog": [
                    {"id": "idea-1", "idea": "First proposal", "status": "proposed"},
                    {"id": "idea-2", "idea": "Already queued", "status": "promoted"},
                ],
            }),
            select_actionable_item=Mock(return_value=None),
        )
        with patch.object(
            self.group, "_get_autonomy_workflow", return_value=workflow
        ), patch.object(self.group, "_company_budget_snapshot", return_value={
            "spent_today_usd": 0.01,
            "reserved_today_usd": 0.0,
            "remaining_usd": 4.74,
            "daily_budget_usd": 5.0,
        }):
            status = self.group._autonomy_status_text()

        self.assertIn("Proposed idea backlog: 1", status)
        self.assertIn("idea-1: First proposal", status)
        self.assertNotIn("idea-2: Already queued", status)
        self.assertIn("/autorun promote <idea-id>", status)

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
                "recent_run_evidence": [{
                    "run_id": "run_prior_evidence",
                    "scope": "global_report",
                    "global_trigger_source": "telegram",
                    "global_final_status": "completed",
                    "global_human_review_required": False,
                    "project_human_review_required": False,
                    "report_available": True,
                    "summary_line": (
                        "global trigger=telegram; global final=completed; "
                        "global human_review=no"
                    ),
                }],
            }
            routed_prompts = []

            async def complete_task(company_project, task):
                routed_prompts.append(
                    company_mode.build_task_prompt(company_project, task)
                    + "\n\n"
                    + self.group._autonomy_evidence_context.get()
                )
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
        self.assertEqual(len(routed_prompts), 2)
        self.assertTrue(all("run_prior_evidence" in prompt for prompt in routed_prompts))
        persisted_company_state = json.dumps(final)
        self.assertNotIn("run_prior_evidence", persisted_company_state)
        self.assertEqual(self.group._autonomy_evidence_context.get(), "")

    async def test_run_autonomy_session_calls_bounded_workflow_api(self):
        report = {"cycle_reports": [], "telegram_summary": "Session complete"}
        workflow = types.SimpleNamespace(run_session=Mock(return_value=report))
        options = {
            "eligible_item_ids": ["AUTO-1"],
            "max_selected_items": None,
            "allow_ideation": True,
            "report_metadata": None,
            "campaign_id": None,
        }
        with patch.object(self.group, "_get_autonomy_workflow", return_value=workflow), patch.object(
            self.group,
            "_revenue_sprint_session_options",
            new=AsyncMock(return_value=options),
        ):
            returned = await self.group._run_autonomy_session("scheduled", dry_run=False)

        self.assertIs(returned, report)
        workflow.run_session.assert_called_once_with(
            trigger_source="scheduled",
            dry_run=False,
            eligible_item_ids=["AUTO-1"],
            max_selected_items=None,
            allow_ideation=True,
            report_metadata=None,
        )

    def test_revenue_sprint_manifest_maps_only_the_exact_company_bluesky_account(self):
        manifest = {
            "id": "sprint-company-bluesky",
            "product": {
                "id": "kit",
                "name": "Freelancer Cold-Email Starter Pack",
                "url": "https://tymedina.gumroad.com/l/freelancer-cold-email",
            },
            "channel": {"id": "bluesky:freelanceremailkit.bsky.social"},
            "total_ai_budget_usd": 100.0,
            "daily_ai_budget_usd": 5.0,
            "run_days": 20,
            "checkpoint_thresholds": {
                "day_5_meaningful_interest": {"minimum_meaningful_interactions": 1},
                "day_15_sale_or_strong_intent": {
                    "minimum_sales": 1,
                    "minimum_strong_intent_signals": 1,
                },
                "max_consecutive_no_progress_days": 3,
                "trailing_window_days": 7,
                "minimum_gross_revenue_usd_per_day": 5.0,
                "minimum_trailing_gross_revenue_usd": 35.0,
                "require_nonnegative_contribution": True,
            },
            "action_policy": {
                "revision": "bluesky-publish-v1",
                "allowed_external_actions": [{
                    "action_type": "publish",
                    "target": "bluesky:freelanceremailkit.bsky.social",
                    "daily_cap": 1,
                    "total_cap": 20,
                }],
            },
        }
        product = {
            "project_id": "proj-product",
            "gumroad_product_id": "gumroad-1",
        }

        payload = self.group._revenue_sprint_manifest_payload(
            manifest,
            approval_source="telegram_owner:42",
            product=product,
        )

        self.assertEqual(payload["channel"]["type"], "bluesky")
        self.assertEqual(
            payload["channel"]["account_id"], "freelanceremailkit.bsky.social"
        )
        self.assertEqual(
            payload["automation_policy"]["allowed_targets"]["publish"],
            ["bluesky:freelanceremailkit.bsky.social"],
        )
        self.assertEqual(payload["automation_policy"]["daily_action_caps"]["publish"], 1)
        self.assertEqual(payload["automation_policy"]["total_action_caps"]["publish"], 20)
        self.assertEqual(payload["automation_policy"]["approved_by"], "telegram_owner:42")
        self.assertEqual(payload["product"]["ownership"], "company_owned")
        self.assertFalse(payload["product"]["personal_fallback_allowed"])

    async def test_campaign_session_selects_one_day_and_weekend_live_is_zero_action(self):
        item = {
            "id": "SPRINT-D01",
            "revenue_sprint_id": "sprint-1",
            "revenue_sprint_run_day": 1,
        }
        workflow = types.SimpleNamespace(load_state=Mock(return_value={
            "projects": [{"roadmap_items": [item]}],
        }))
        sprint = {
            "id": "sprint-1",
            "status": "active",
            "timezone": "America/Phoenix",
            "run_days": [],
            "channel": {"destination_scope": "bluesky:freelanceremailkit.bsky.social"},
            "product": {"gumroad_url": "https://example.test/product"},
            "pivot_required": False,
        }
        company_state = {"company": {"active_revenue_sprint_id": "sprint-1"}}
        budget = {"run_days_used": 0, "max_run_days": 20}

        class SaturdayDateTime:
            @classmethod
            def now(cls, zone):
                return datetime(2026, 8, 8, 9, 0, tzinfo=zone)

        with patch.object(self.group.company_mode, "load_state", return_value=company_state), patch.object(
            self.group.company_mode, "active_revenue_sprint", return_value=sprint
        ), patch.object(
            self.group.company_mode, "revenue_sprint_budget_snapshot", return_value=budget
        ), patch.object(self.group, "datetime", SaturdayDateTime):
            preview = await self.group._revenue_sprint_session_options(workflow, dry_run=True)
            live = await self.group._revenue_sprint_session_options(workflow, dry_run=False)

        self.assertEqual(preview["eligible_item_ids"], ["SPRINT-D01"])
        self.assertEqual(preview["max_selected_items"], 1)
        self.assertFalse(preview["allow_ideation"])
        self.assertEqual(live["eligible_item_ids"], [])
        self.assertFalse(live["report_metadata"]["weekday_eligible"])

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
            "telegram_summary": "I paused this session because one item needs you.",
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
                ("I paused this session because one item needs you.", "manager"),
            ],
        )
        self.assertIsNone(self.group.autonomy_runner_task)

    async def test_live_session_posts_only_lumen_child_then_one_summary(self):
        children = [
            {
                "id": "cycle-1",
                "result_agent": "code",
                "telegram_summary": "DO NOT POST CHILD 1",
            },
            {
                "id": "cycle-2",
                "result_agent": "general",
                "telegram_summary": "DO NOT POST CHILD 2",
            },
            {
                "id": "cycle-3",
                "result_agent": "creative",
                "idea_proposals": [{"idea": "Add a deployment health digest"}],
                "telegram_summary": "DO NOT POST CHILD 3",
            },
        ]
        report = {
            "dry_run": False,
            "escalations": [],
            "cycle_reports": children,
            "telegram_summary": "We're done for this session.",
        }
        with patch.object(
            self.group, "_run_autonomy_session", new=AsyncMock(return_value=report)
        ), patch.object(
            self.group.autonomous_workflow,
            "format_telegram_deliverable",
            return_value="I found one deployment-health idea worth considering.",
        ) as formatter, patch.object(self.group, "post_to_group", new=AsyncMock()) as post:
            await self.group._run_and_post_autonomy("scheduled", dry_run=False)

        self.assertEqual([call.args[0] for call in formatter.call_args_list], [children[2]])
        self.assertEqual(
            [call.args for call in post.await_args_list],
            [
                ("I found one deployment-health idea worth considering.", "creative"),
                ("We're done for this session.", "manager"),
            ],
        )
        self.assertEqual(
            sum(call.args[0] == "We're done for this session." for call in post.await_args_list),
            1,
        )
        self.assertFalse(any("DO NOT POST CHILD" in call.args[0] for call in post.await_args_list))

    async def test_live_session_falls_back_to_completed_deliverable_when_team_chat_is_disabled(self):
        child = {
            "id": "cycle-1",
            "result_agent": "code",
            "tasks_selected": [
                {"id": "AUTO-042", "title": "Recover stale locks", "status": "completed"}
            ],
            "result_text": "Implemented and verified safe recovery.",
        }
        report = {
            "dry_run": False,
            "escalations": [],
            "cycle_reports": [child],
            "telegram_summary": "We're done for this session.",
        }
        with patch.object(
            self.group, "AUTONOMY_TEAM_CHAT_ENABLED", False
        ), patch.object(
            self.group, "_run_autonomy_session", new=AsyncMock(return_value=report)
        ), patch.object(
            self.group.autonomous_workflow,
            "format_telegram_deliverable",
            return_value="I finished AUTO-042 and saved the result.",
        ) as formatter, patch.object(
            self.group, "post_to_group", new=AsyncMock()
        ) as post:
            await self.group._run_and_post_autonomy("scheduled", dry_run=False)

        formatter.assert_called_once_with(child)
        self.assertEqual(
            [call.args for call in post.await_args_list],
            [
                ("I finished AUTO-042 and saved the result.", "code"),
                ("We're done for this session.", "manager"),
            ],
        )

    async def test_live_session_falls_back_when_an_essential_handoff_failed(self):
        child = {
            "id": "cycle-1",
            "result_agent": "code",
            "team_handoff_failed": True,
            "tasks_selected": [
                {"id": "AUTO-042", "title": "Recover stale locks", "status": "completed"}
            ],
            "result_text": "Implemented and verified safe recovery.",
        }
        report = {
            "dry_run": False,
            "escalations": [],
            "cycle_reports": [child],
            "telegram_summary": "We're done for this session.",
        }
        with patch.object(
            self.group, "AUTONOMY_TEAM_CHAT_ENABLED", True
        ), patch.object(
            self.group, "_run_autonomy_session", new=AsyncMock(return_value=report)
        ), patch.object(
            self.group.autonomous_workflow,
            "format_telegram_deliverable",
            return_value="I finished AUTO-042 and saved the result.",
        ) as formatter, patch.object(
            self.group, "post_to_group", new=AsyncMock()
        ) as post:
            await self.group._run_and_post_autonomy("scheduled", dry_run=False)

        formatter.assert_called_once_with(child)
        self.assertEqual(
            [call.args for call in post.await_args_list],
            [
                ("I finished AUTO-042 and saved the result.", "code"),
                ("We're done for this session.", "manager"),
            ],
        )

    async def test_scheduled_dry_run_session_posts_no_telegram_message(self):
        report = {
            "dry_run": True,
            "escalations": [],
            "cycle_reports": [{"telegram_summary": "Child dry run"}],
            "telegram_summary": "Dry run complete. Nothing was executed or changed.",
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
                "telegram_summary": "We're done for this session.",
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

    async def test_budget_only_preflight_deferral_waits_without_owner_escalation(self):
        plan = {
            "deferred": True,
            "reason": "A bounded review could not be funded.",
            "deferral_reason": "insufficient_budget",
            "decisions": [{"deferred": True, "deferral_reason": "insufficient_budget"}],
        }
        with patch.object(
            self.group, "_autonomy_runtime_deferral", new=AsyncMock(return_value=None)
        ), patch.object(
            self.group, "_company_budget_snapshot", return_value={"remaining_usd": 0.01}
        ), patch.object(
            self.group.autonomy_team, "build_company_plan", return_value=plan
        ):
            result = await self.group._execute_autonomy_item(
                {"id": "assistant"},
                {"id": "AUTO-BUDGET", "title": "Review", "authorization_level": "propose"},
                types.SimpleNamespace(model_id="gpt-5.4-mini"),
                "run-budget",
            )

        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["failure_classification"], "budget_exhausted")
        self.assertEqual(result["human_action"], "")
        self.assertFalse(result["model_invoked"])

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

    async def test_model_campaign_worker_is_blocked_before_tool_or_context(self):
        target = "bluesky:company.example"
        capability = {
            "allowed": True,
            "reason": "allowed",
            "campaign_id": "sprint-1",
            "campaign_status": "active",
            "action_type": "publish",
            "target": target,
            "policy_revision": "policy-1",
            "requested_policy_revision": "policy-1",
        }
        task = {
            "id": "campaign-worker-1",
            "owner": "marketing",
            "title": "Publish one bounded company post",
            "model": "gpt-5.4-mini",
            "model_reason": "Standard campaign copy and one exact tool call.",
            "estimate_usd": 0.10,
            "execution_attempts": 0,
            "attempt_history": [],
            "authorization_level": "external_action",
            "enforce_authorization": True,
        }
        project = {
            "id": "project-1",
            "title": "Campaign",
            "goal": "Validate one channel",
            "campaign_id": "sprint-1",
            "revenue_sprint_run_id": "run-1",
            "external_action": {
                "action_type": "publish",
                "target": target,
                "policy_revision": "policy-1",
            },
        }
        sink = {
            "cost_usd": 0.0,
            "artifacts": [],
            "usage_records": [],
            "context": "test",
            "campaign_id": "sprint-1",
            "revenue_sprint_run_id": "run-1",
            "campaign_action_type": "publish",
            "campaign_action_target": target,
            "campaign_policy_revision": "policy-1",
            "dry_run": False,
        }
        observed = []

        def ask_specialist(_key, _prompt, **kwargs):
            context = self.group.revenue_actions.current_campaign_action_context()
            observed.append(context)
            self.assertEqual(
                kwargs["allowed_tool_names"],
                {"read_file", "campaign_publish_bluesky"},
            )
            return "Published one verified company-owned campaign post."

        self.fake_main.ask_specialist = Mock(side_effect=ask_specialist)
        state = {"projects": [project], "tasks": [{**task, "status": "done"}], "company": {}}
        with patch.dict(
            self.fake_main.SPECIALISTS["marketing"],
            {"tool_names": ["read_file", "campaign_publish_bluesky", "send_email"]},
        ), patch.object(
            self.group.company_mode,
            "revenue_action_capability",
            return_value=capability,
        ) as capability_call, patch.object(
            self.group.company_mode, "update_task_status", return_value="ok"
        ), patch.object(
            self.group.company_mode, "load_state", return_value=state
        ), patch.object(
            self.group.company_mode, "next_planned_task", return_value=None
        ), patch.object(
            self.group.company_mode, "render_money", return_value="Budget ok"
        ), patch.object(
            self.group, "post_to_group", new=AsyncMock()
        ), patch.object(
            self.group, "post_agent_answer_to_group", new=AsyncMock()
        ), patch.object(
            self.group, "post_team_handoff", new=AsyncMock()
        ):
            outcome = await self.group._execute_routed_task(
                project, task, "marketing", "prompt", sink
            )

        self.assertEqual(outcome, "blocked")
        self.assertEqual(observed, [])
        self.assertIsNone(
            self.group.revenue_actions.current_campaign_action_context()
        )
        capability_call.assert_not_called()
        self.fake_main.ask_specialist.assert_not_called()

    async def test_campaign_missing_company_account_stops_before_budget_or_model(self):
        target = "bluesky:company.example"
        item = {
            "id": "CAMPAIGN-D01",
            "title": "Publish",
            "authorization_level": "external_action",
            "revenue_sprint_id": "sprint-1",
            "external_action": {
                "action_type": "publish",
                "target": target,
                "policy_revision": "policy-1",
            },
        }
        sprint = {
            "id": "sprint-1",
            "status": "active",
            "automation_policy": {
                "revision": "policy-1",
                "allowed_action_types": ["publish"],
                "allowed_targets": {"publish": [target]},
            },
        }
        with patch.object(
            self.group.company_mode,
            "load_state",
            return_value={"revenue_sprints": [sprint]},
        ), patch.object(
            self.group.company_mode,
            "active_revenue_sprint",
            return_value=sprint,
        ), patch.object(
            self.group, "_autonomy_runtime_deferral", new=AsyncMock(return_value=None)
        ), patch.object(
            self.group.revenue_actions,
            "revenue_action_target_readiness",
            return_value={
                "ready": False,
                "needs_human": True,
                "reason": "NEEDS HUMAN: configure the dedicated company account.",
            },
        ) as readiness, patch.object(
            self.group, "_company_budget_snapshot"
        ) as budget:
            result = await self.group._execute_autonomy_item(
                {"id": "assistant"},
                item,
                types.SimpleNamespace(model_id="should-not-run"),
                "run-1",
            )

        self.assertEqual(result["status"], "needs_human")
        self.assertEqual(result["failure_classification"], "missing_access")
        self.assertFalse(result["model_invoked"])
        self.assertIn("company account", result["reason"])
        readiness.assert_called_once_with(
            "publish", target, verify_identity=True
        )
        budget.assert_not_called()

    async def test_prepare_campaign_fetches_prior_bluesky_metrics_before_claim(self):
        prior_action = {
            "id": "action-prior",
            "run_id": "run-prior",
            "action_type": "publish",
            "target": "bluesky:company.example",
            "status": "succeeded",
            "provider_receipt": {
                "uri": "at://did:plc:company/app.bsky.feed.post/1",
                "cid": "cid-1",
            },
            "result": "Bluesky created uri=at://did:plc:company/post/1 cid=cid-1.",
        }
        sprint = {
            "id": "sprint-1",
            "status": "active",
            "product": {"gumroad_url": "https://company.gumroad.com/l/kit"},
            "action_journal": [prior_action, {
                "id": "action-failed",
                "action_type": "publish",
                "target": "bluesky:company.example",
                "status": "failed",
            }],
        }
        item = {
            "id": "CAMPAIGN-D02",
            "revenue_sprint_id": "sprint-1",
            "external_action": {"action_type": "publish"},
        }
        products = [{
            "short_url": "https://company.gumroad.com/l/kit",
            "published": True,
        }]
        engagement = [{"action_id": "action-prior", "like_count": 2}]
        order = []

        with patch.object(
            self.group.company_mode, "load_state", return_value={"revenue_sprints": [sprint]}
        ), patch.object(
            self.group.company_mode, "active_revenue_sprint", return_value=sprint
        ), patch.object(
            self.group.gumroad_helpers, "list_products", return_value=(products, None)
        ), patch.object(
            self.group.revenue_actions,
            "fetch_bluesky_engagement",
            create=True,
            side_effect=lambda actions: order.append(("fetch", actions)) or engagement,
        ) as fetch, patch.object(
            self.group.company_mode,
            "claim_revenue_sprint_run",
            side_effect=lambda *args, **kwargs: order.append(("claim", args[0])) or {"status": "claimed"},
        ), patch.object(
            self.group.company_mode,
            "record_bluesky_engagement_snapshot",
            create=True,
            side_effect=lambda *args, **kwargs: order.append(("engagement", args[1], args[2])),
        ) as record_engagement, patch.object(
            self.group.company_mode,
            "record_revenue_snapshot",
            side_effect=lambda *args, **kwargs: order.append(("gumroad", args[1], args[2])),
        ), patch.object(
            self.group.company_mode,
            "sync_revenue",
            side_effect=lambda *args, **kwargs: order.append(("sync",)),
        ):
            await self.group._prepare_campaign_item(item, "run-current")

        fetch.assert_called_once_with([prior_action])
        record_engagement.assert_called_once_with(
            engagement,
            "before",
            "run-current",
            sprint_id="sprint-1",
        )
        self.assertEqual(
            [event[0] for event in order],
            ["fetch", "claim", "engagement", "gumroad", "sync"],
        )

    async def test_prepare_campaign_pending_action_stops_before_provider_reads(self):
        item = {
            "id": "D02",
            "revenue_sprint_id": "sprint-1",
            "external_action": {"action_type": "publish"},
        }
        self.pending_action_guard.side_effect = self.group.company_mode.RevenueSprintError(
            "A prior external action remains claimed; reconcile action action-1."
        )

        with patch.object(
            self.group.company_mode, "load_state"
        ) as load_state, patch.object(
            self.group.gumroad_helpers, "list_products"
        ) as products, patch.object(
            self.group.revenue_actions, "fetch_bluesky_engagement"
        ) as engagement, patch.object(
            self.group.company_mode, "claim_revenue_sprint_run"
        ) as claim:
            with self.assertRaises(self.group.company_mode.RevenueSprintError) as caught:
                await self.group._prepare_campaign_item(item, "run-next")

        self.assertIn("reconcile action", str(caught.exception))
        self.pending_action_guard.assert_called_once_with(sprint_id="sprint-1")
        load_state.assert_not_called()
        products.assert_not_called()
        engagement.assert_not_called()
        claim.assert_not_called()

    async def test_prepare_campaign_fetch_failure_consumes_no_run_day(self):
        sprint = {
            "id": "sprint-1",
            "status": "active",
            "product": {"gumroad_url": "https://company.gumroad.com/l/kit"},
            "action_journal": [],
        }
        item = {"id": "D01", "revenue_sprint_id": "sprint-1"}
        products = [{
            "short_url": "https://company.gumroad.com/l/kit",
            "published": True,
        }]

        with patch.object(
            self.group.company_mode, "load_state", return_value={"revenue_sprints": [sprint]}
        ), patch.object(
            self.group.company_mode, "active_revenue_sprint", return_value=sprint
        ), patch.object(
            self.group.gumroad_helpers, "list_products", return_value=(products, None)
        ), patch.object(
            self.group.revenue_actions,
            "fetch_bluesky_engagement",
            create=True,
            side_effect=RuntimeError("provider unavailable"),
        ) as fetch, patch.object(
            self.group.company_mode, "claim_revenue_sprint_run"
        ) as claim:
            with self.assertRaises(self.group.company_mode.RevenueSprintError) as caught:
                await self.group._prepare_campaign_item(item, "run-1")

        self.assertIn("no campaign day was consumed", str(caught.exception))
        fetch.assert_called_once_with([])
        claim.assert_not_called()

    async def test_prepare_campaign_legacy_post_without_receipt_fails_before_claim(self):
        sprint = {
            "id": "sprint-1",
            "status": "active",
            "product": {"gumroad_url": "https://company.gumroad.com/l/kit"},
            "action_journal": [{
                "id": "legacy-action",
                "run_id": "legacy-run",
                "action_type": "publish",
                "target": "bluesky:company.example",
                "status": "succeeded",
                "provider_receipt": {},
            }],
        }
        products = [{
            "short_url": "https://company.gumroad.com/l/kit",
            "published": True,
        }]

        with patch.object(
            self.group.company_mode, "load_state", return_value={"revenue_sprints": [sprint]}
        ), patch.object(
            self.group.company_mode, "active_revenue_sprint", return_value=sprint
        ), patch.object(
            self.group.gumroad_helpers, "list_products", return_value=(products, None)
        ), patch.object(
            self.group.revenue_actions.requests, "get"
        ) as get, patch.object(
            self.group.company_mode, "claim_revenue_sprint_run"
        ) as claim:
            with self.assertRaises(self.group.company_mode.RevenueSprintError) as caught:
                await self.group._prepare_campaign_item(
                    {"revenue_sprint_id": "sprint-1"}, "run-current"
                )

        self.assertIn("no campaign day was consumed", str(caught.exception))
        get.assert_not_called()
        claim.assert_not_called()

    async def test_prepare_campaign_engagement_persistence_failure_stops_claimed_run(self):
        sprint = {
            "id": "sprint-1",
            "status": "active",
            "product": {"gumroad_url": "https://company.gumroad.com/l/kit"},
            "action_journal": [],
        }
        item = {"id": "D01", "revenue_sprint_id": "sprint-1"}
        products = [{
            "short_url": "https://company.gumroad.com/l/kit",
            "published": True,
        }]
        complete = Mock()
        stop = Mock()

        with patch.object(
            self.group.company_mode, "load_state", return_value={"revenue_sprints": [sprint]}
        ), patch.object(
            self.group.company_mode, "active_revenue_sprint", return_value=sprint
        ), patch.object(
            self.group.gumroad_helpers, "list_products", return_value=(products, None)
        ), patch.object(
            self.group.revenue_actions,
            "fetch_bluesky_engagement",
            create=True,
            return_value=[],
        ), patch.object(
            self.group.company_mode, "claim_revenue_sprint_run", return_value={"status": "claimed"}
        ), patch.object(
            self.group.company_mode,
            "record_bluesky_engagement_snapshot",
            create=True,
            side_effect=OSError("disk unavailable"),
        ), patch.object(
            self.group.company_mode, "complete_revenue_sprint_run", complete
        ), patch.object(
            self.group.company_mode, "stop_revenue_sprint", stop
        ), patch.object(
            self.group.company_mode, "record_revenue_snapshot"
        ) as revenue_snapshot:
            with self.assertRaises(OSError):
                await self.group._prepare_campaign_item(item, "run-1")

        complete.assert_called_once_with(
            "run-1",
            "needs_human",
            sprint_id="sprint-1",
            progress=False,
            result=(
                "The required before-execution Bluesky engagement snapshot could not "
                "be persisted."
            ),
        )
        stop.assert_called_once_with(
            sprint_id="sprint-1",
            reason="before_bluesky_engagement_snapshot_failed",
        )
        revenue_snapshot.assert_not_called()

    async def test_prepare_campaign_dry_run_does_not_fetch_engagement(self):
        with patch.object(
            self.group.revenue_actions,
            "fetch_bluesky_engagement",
            create=True,
        ) as fetch, patch.object(
            self.group.gumroad_helpers, "list_products"
        ) as products, patch.object(
            self.group.company_mode, "claim_revenue_sprint_run"
        ) as claim:
            result = await self.group._prepare_campaign_item(
                {"revenue_sprint_id": "sprint-1"},
                "run-1",
                dry_run=True,
            )

        self.assertIsNone(result)
        fetch.assert_not_called()
        products.assert_not_called()
        claim.assert_not_called()

    async def test_campaign_completion_derives_progress_from_commercial_evidence(self):
        target = "bluesky:company.example"
        payload_digest = "a" * 64
        item = {
            "id": "CAMPAIGN-D01",
            "revenue_sprint_id": "sprint-1",
            "external_action": {
                "action_type": "publish",
                "target": target,
                "policy_revision": "policy-1",
            },
        }
        sprint = {
            "id": "sprint-1",
            "status": "active",
            "action_journal": [{
                "id": "action-1",
                "run_id": "run-1",
                "action_type": "publish",
                "target": target,
                "status": "succeeded",
                "provider_receipt": {
                    "uri": "at://did:plc:company/app.bsky.feed.post/1",
                    "cid": "cid-1",
                },
                "metadata": {"payload_digest": payload_digest},
            }],
        }
        project = {
            "campaign_id": "sprint-1",
            "revenue_sprint_run_id": "run-1",
            "approved_revenue_action": {
                "payload_digest": payload_digest,
                "action_type": "publish",
                "target": target,
                "policy_revision": "policy-1",
            },
        }
        products = [{
            "id": "gumroad-1",
            "short_url": "https://company.gumroad.com/l/kit",
            "sales_count": 0,
            "sales_usd_cents": 0,
            "published": True,
        }]
        order = []
        complete = Mock(return_value={"status": "completed", "progress": False})

        with patch.object(
            self.group.company_mode,
            "load_state",
            return_value={"revenue_sprints": [sprint], "projects": [project]},
        ), patch.object(
            self.group.company_mode, "active_revenue_sprint", return_value=sprint
        ), patch.object(
            self.group.revenue_actions,
            "fetch_bluesky_engagement",
            create=True,
            side_effect=lambda actions: order.append("engagement_fetch") or [{
                "action_id": "action-1",
                "like_count": 0,
            }],
        ), patch.object(
            self.group.company_mode,
            "record_bluesky_engagement_snapshot",
            create=True,
            side_effect=lambda *args, **kwargs: order.append("engagement_persist") or {
                "phase": "after"
            },
        ), patch.object(
            self.group.gumroad_helpers,
            "list_products",
            side_effect=lambda: order.append("gumroad_fetch") or (products, None),
        ), patch.object(
            self.group.company_mode,
            "record_revenue_snapshot",
            side_effect=lambda *args, **kwargs: order.append("gumroad_persist") or {
                "sales_delta": 0
            },
        ), patch.object(
            self.group.company_mode,
            "sync_revenue",
            side_effect=lambda *args, **kwargs: order.append("gumroad_sync"),
        ), patch.object(
            self.group.company_mode,
            "complete_revenue_sprint_run",
            side_effect=lambda *args, **kwargs: order.append("run_complete") or complete(
                *args, **kwargs
            ),
        ):
            returned = await self.group._complete_campaign_item(
                item,
                "run-1",
                {"status": "completed", "result_text": "Provider receipt persisted."},
            )

        self.assertEqual(returned["status"], "completed")
        complete.assert_called_once()
        self.assertIsNone(complete.call_args.kwargs["progress"])
        self.assertEqual(
            order,
            [
                "engagement_fetch",
                "engagement_persist",
                "gumroad_fetch",
                "gumroad_persist",
                "gumroad_sync",
                "run_complete",
            ],
        )

    async def test_campaign_completion_engagement_fetch_failure_stops_without_republish(self):
        target = "bluesky:company.example"
        payload_digest = "a" * 64
        action = {
            "id": "action-current",
            "run_id": "run-1",
            "action_type": "publish",
            "target": target,
            "status": "succeeded",
            "provider_receipt": {
                "uri": "at://did:plc:company/app.bsky.feed.post/current",
                "cid": "cid-current",
            },
            "metadata": {"payload_digest": payload_digest},
        }
        sprint = {"id": "sprint-1", "status": "active", "action_journal": [action]}
        project = {
            "campaign_id": "sprint-1",
            "revenue_sprint_run_id": "run-1",
            "approved_revenue_action": {
                "payload_digest": payload_digest,
                "action_type": "publish",
                "target": target,
                "policy_revision": "policy-1",
            },
        }
        item = {
            "revenue_sprint_id": "sprint-1",
            "external_action": {
                "action_type": "publish",
                "target": target,
                "policy_revision": "policy-1",
            },
        }
        products = [{"short_url": "https://company.gumroad.com/l/kit", "published": True}]
        stop = Mock()
        complete = Mock(return_value={"status": "completed"})

        with patch.object(
            self.group.company_mode,
            "load_state",
            return_value={"revenue_sprints": [sprint], "projects": [project]},
        ), patch.object(
            self.group.company_mode, "active_revenue_sprint", return_value=sprint
        ), patch.object(
            self.group.revenue_actions,
            "fetch_bluesky_engagement",
            create=True,
            side_effect=RuntimeError("read failed"),
        ) as fetch, patch.object(
            self.group.company_mode,
            "record_bluesky_engagement_snapshot",
            create=True,
        ) as record_engagement, patch.object(
            self.group.company_mode, "revenue_sprint_status", return_value={"active": True}
        ), patch.object(
            self.group.company_mode, "stop_revenue_sprint", stop
        ), patch.object(
            self.group.gumroad_helpers, "list_products", return_value=(products, None)
        ), patch.object(
            self.group.company_mode, "record_revenue_snapshot", return_value={}
        ), patch.object(
            self.group.company_mode, "sync_revenue", return_value=None
        ), patch.object(
            self.group.company_mode, "complete_revenue_sprint_run", complete
        ):
            returned = await self.group._complete_campaign_item(
                item,
                "run-1",
                {"status": "completed", "result_text": "Provider receipt persisted."},
            )

        self.assertEqual(returned["status"], "needs_human")
        self.assertIn("not retried", returned["reason"])
        fetch.assert_called_once_with([action])
        record_engagement.assert_not_called()
        stop.assert_called_once_with(
            sprint_id="sprint-1",
            reason="after_bluesky_engagement_snapshot_failed",
        )
        complete.assert_called_once()

    async def test_autonomous_worker_posts_assignment_then_reviewer_handoff(self):
        task = {
            "id": "worker-1",
            "owner": "code",
            "title": "Implement the bounded change",
            "model": "gpt-5.4-mini",
            "model_reason": "Standard coding task fits the lower-cost capable model.",
            "execution_attempts": 0,
            "attempt_history": [],
        }
        project = {"id": "project-1", "title": "Project", "goal": "Ship safely"}
        editor = {
            "id": "review-1",
            "project_id": "project-1",
            "owner": "editor",
            "status": "planned",
        }
        state = {"projects": [project], "tasks": [{**task, "status": "done"}, editor], "company": {}}
        sink = {"cost_usd": 0.0, "artifacts": [], "usage_records": [], "context": "test"}
        self.fake_main.ask_specialist = Mock(return_value="Implemented and verified the bounded change.")

        with patch.object(
            self.group.company_mode, "update_task_status", return_value="ok"
        ), patch.object(
            self.group.company_mode, "load_state", return_value=state
        ), patch.object(
            self.group.company_mode, "next_planned_task", return_value=editor
        ), patch.object(
            self.group.company_mode, "render_money", return_value="Budget ok"
        ), patch.object(
            self.group, "post_to_group", new=AsyncMock()
        ), patch.object(
            self.group, "post_agent_answer_to_group", new=AsyncMock()
        ), patch.object(
            self.group, "post_team_handoff", new=AsyncMock()
        ) as handoff:
            outcome = await self.group._execute_routed_task(
                project, task, "code", "prompt", sink
            )

        self.assertEqual(outcome, "done")
        self.assertEqual([call.args[0] for call in handoff.await_args_list], ["manager", "code"])
        assignment = handoff.await_args_list[0].args[1]
        ready_for_review = handoff.await_args_list[1].args[1]
        self.assertTrue(assignment.startswith("Code, please take"))
        self.assertIn("gpt-5.4-mini", assignment)
        self.assertIn("Vera, I finished", ready_for_review)
        self.assertIn("Implemented and verified", ready_for_review)
        self.assertIn("saved the full result", ready_for_review)
        for message in (assignment, ready_for_review):
            assert_conversational_chat(self, message)
            self.assertLessEqual(len(message), self.group.AUTONOMY_TEAM_CHAT_MAX_CHARS)

    async def test_autonomous_worker_gets_one_independently_routed_teammate_answer(self):
        task = {
            "id": "worker-help", "owner": "code", "title": "Validate a claim",
            "model": "worker-model", "model_reason": "Coding route",
            "execution_attempts": 0, "attempt_history": [],
            "authorization_level": "observe", "enforce_authorization": True,
        }
        project = {"id": "project-1", "title": "Project", "goal": "Validate"}
        state = {"projects": [project], "tasks": [{**task, "status": "done"}], "company": {}}
        sink = {
            "cost_usd": 0.0, "artifacts": [], "usage_records": [],
            "context": "test", "budget_cap_usd": 1.0,
        }
        calls = []

        def ask_specialist(key, prompt, **kwargs):
            calls.append((key, kwargs.get("model"), kwargs.get("allowed_tool_names")))
            if key == "research":
                return "The source confirms the narrow factual claim."
            if len([row for row in calls if row[0] == "code"]) == 1:
                return (
                    'AUTONOMY_HELP_REQUEST {"helper":"research","question":'
                    '"Is this claim supported?","reason":"The final answer needs an '
                    'independent source check.","task_type":"classification",'
                    '"complexity":"lightweight","risk":"low"}'
                )
            return "Completed with the teammate's evidence and explicit criteria."

        self.fake_main.ask_specialist = Mock(side_effect=ask_specialist)
        helper_route = types.SimpleNamespace(
            model_id="gpt-5.4-nano", model="gpt-5.4-nano", deferred=False,
            reason="Lightweight no-tool help fits the lowest-cost capable model.",
            deferral_reason=None,
        )
        with patch.object(
            self.group, "_company_task_route", return_value=helper_route
        ) as route, patch.object(
            self.group.company_mode, "update_task_status", return_value="ok"
        ) as update, patch.object(
            self.group.company_mode, "load_state", return_value=state
        ), patch.object(
            self.group.company_mode, "next_planned_task", return_value=None
        ), patch.object(
            self.group.company_mode, "render_money", return_value="Budget ok"
        ), patch.object(
            self.group, "post_to_group", new=AsyncMock()
        ), patch.object(
            self.group, "post_agent_answer_to_group", new=AsyncMock()
        ), patch.object(
            self.group,
            "post_team_handoff",
            new=AsyncMock(side_effect=[
                "direct",
                "delivery_failed",
                "direct",
                "relayed_by_manager",
            ]),
        ) as handoff:
            outcome = await self.group._execute_routed_task(
                project, task, "code", "prompt", sink
            )

        self.assertEqual(outcome, "done")
        self.assertEqual([row[0] for row in calls], ["code", "research", "code"])
        self.assertEqual(calls[1], ("research", "gpt-5.4-nano", set()))
        self.assertEqual(route.call_args.kwargs["has_tools"], False)
        self.assertEqual(len(sink["team_help_events"]), 1)
        event = sink["team_help_events"][0]
        self.assertEqual(event["helper_agent"], "research")
        self.assertEqual(event["helper_model"], "gpt-5.4-nano")
        self.assertIn("lowest-cost", event["model_reason"])
        self.assertEqual(event["request_delivery"], "delivery_failed")
        self.assertEqual(event["routing_delivery"], "direct")
        self.assertEqual(event["response_delivery"], "relayed_by_manager")
        self.assertEqual(
            [call.args[0] for call in handoff.await_args_list],
            ["manager", "code", "manager", "research"],
        )
        visible = [call.args[1] for call in handoff.await_args_list]
        helper_name = self.group._agent_display_name("research")
        self.assertIn(f"{helper_name}, can you help me", visible[1])
        self.assertIn(f"{helper_name}, please take that focused check", visible[2])
        self.assertIn("Code, I checked it", visible[3])
        self.assertIn("The source confirms", visible[3])
        for message in visible:
            assert_conversational_chat(self, message)
            self.assertLessEqual(len(message), self.group.AUTONOMY_TEAM_CHAT_MAX_CHARS)
        terminal = [
            call for call in update.call_args_list
            if len(call.args) > 1 and call.args[1] == "done"
        ][0]
        self.assertEqual(len(terminal.kwargs["team_help_events"]), 1)

    def test_teammate_help_parser_rejects_malformed_self_and_editor_requests(self):
        cases = (
            (
                "malformed",
                "AUTONOMY_HELP_REQUEST {not-json}",
                "not valid JSON",
            ),
            (
                "self",
                (
                    'AUTONOMY_HELP_REQUEST {"helper":"code","question":"Check it",'
                    '"reason":"Need a second opinion"}'
                ),
                "cannot request help from itself",
            ),
            (
                "reviewer",
                (
                    'AUTONOMY_HELP_REQUEST {"helper":"editor","question":"Check it",'
                    '"reason":"Need a second opinion"}'
                ),
                "independent final reviewer",
            ),
        )

        for label, payload, expected in cases:
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, expected):
                self.group._parse_team_help_request(payload, "code")

    async def test_helper_returning_its_own_help_request_stops_without_resuming_worker(self):
        task = {
            "id": "helper-recursion", "owner": "code", "title": "Validate",
            "model": "worker-model", "model_reason": "Coding route",
            "execution_attempts": 0, "attempt_history": [],
        }
        project = {"id": "project-1", "title": "Project", "goal": "Validate"}
        sink = {"cost_usd": 0.0, "artifacts": [], "usage_records": [], "context": "test"}
        worker_request = (
            'AUTONOMY_HELP_REQUEST {"helper":"research","question":"Check it",'
            '"reason":"Need evidence","task_type":"research",'
            '"complexity":"standard","risk":"low"}'
        )
        helper_request = (
            'AUTONOMY_HELP_REQUEST {"helper":"write","question":"Draft it",'
            '"reason":"Need wording","task_type":"documentation",'
            '"complexity":"standard","risk":"low"}'
        )
        self.fake_main.ask_specialist = Mock(
            side_effect=[worker_request, helper_request]
        )
        route = types.SimpleNamespace(
            model_id="helper-model", model="helper-model", deferred=False,
            reason="Independent helper route.", deferral_reason=None,
        )
        with patch.object(
            self.group, "_company_task_route", return_value=route
        ), patch.object(
            self.group, "_remaining_task_headroom", return_value=1.0
        ), patch.object(
            self.group.company_mode, "update_task_status", return_value="ok"
        ) as update, patch.object(
            self.group, "post_to_group", new=AsyncMock()
        ), patch.object(
            self.group, "post_agent_answer_to_group", new=AsyncMock()
        ), patch.object(
            self.group, "post_team_handoff", new=AsyncMock()
        ):
            outcome = await self.group._execute_routed_task(
                project, task, "code", "prompt", sink
            )

        self.assertEqual(outcome, "blocked")
        self.assertEqual(self.fake_main.ask_specialist.call_count, 2)
        self.assertEqual(sink["team_help_events"][0]["status"], "failed")
        terminal = [
            call for call in update.call_args_list
            if len(call.args) > 1 and call.args[1] == "needs_human"
        ]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0].kwargs["failure_classification"], "no_progress")

    async def test_second_teammate_request_stops_at_one_hop(self):
        task = {
            "id": "worker-help-cap", "owner": "code", "title": "Validate",
            "model": "worker-model", "model_reason": "Coding route",
            "execution_attempts": 0, "attempt_history": [],
        }
        project = {"id": "project-1", "title": "Project", "goal": "Validate"}
        sink = {"cost_usd": 0.0, "artifacts": [], "usage_records": [], "context": "test"}
        request = (
            'AUTONOMY_HELP_REQUEST {"helper":"research","question":"Check it",'
            '"reason":"Need evidence","task_type":"research",'
            '"complexity":"standard","risk":"low"}'
        )
        responses = iter([request, "One focused answer.", request])
        self.fake_main.ask_specialist = Mock(side_effect=lambda *args, **kwargs: next(responses))
        route = types.SimpleNamespace(
            model_id="helper-model", model="helper-model", deferred=False,
            reason="Independent helper route.", deferral_reason=None,
        )
        with patch.object(
            self.group, "_company_task_route", return_value=route
        ), patch.object(
            self.group.company_mode, "update_task_status", return_value="ok"
        ) as update, patch.object(
            self.group, "post_to_group", new=AsyncMock()
        ), patch.object(
            self.group, "post_agent_answer_to_group", new=AsyncMock()
        ), patch.object(
            self.group, "post_team_handoff", new=AsyncMock()
        ):
            outcome = await self.group._execute_routed_task(
                project, task, "code", "prompt", sink
            )

        self.assertEqual(outcome, "blocked")
        self.assertEqual(self.fake_main.ask_specialist.call_count, 3)
        terminal = [
            call for call in update.call_args_list
            if len(call.args) > 1 and call.args[1] == "needs_human"
        ][0]
        self.assertEqual(terminal.kwargs["failure_classification"], "no_progress")

    async def test_teammate_help_budget_deferral_starts_no_helper_and_needs_no_owner(self):
        task = {
            "id": "worker-help-budget", "owner": "code", "title": "Validate",
            "model": "worker-model", "model_reason": "Coding route",
            "execution_attempts": 0, "attempt_history": [],
        }
        project = {"id": "project-1", "title": "Project", "goal": "Validate"}
        sink = {"cost_usd": 0.0, "artifacts": [], "usage_records": [], "context": "test"}
        request = (
            'AUTONOMY_HELP_REQUEST {"helper":"research","question":"Check it",'
            '"reason":"Need evidence","task_type":"research",'
            '"complexity":"advanced","risk":"high"}'
        )
        self.fake_main.ask_specialist = Mock(return_value=request)
        route = types.SimpleNamespace(
            model_id=None, model=None, deferred=True,
            reason="Cheapest capable helper exceeds remaining budget.",
            deferral_reason="insufficient_budget",
        )
        with patch.object(
            self.group, "_company_task_route", return_value=route
        ), patch.object(
            self.group.company_mode, "update_task_status", return_value="ok"
        ) as update, patch.object(
            self.group, "post_to_group", new=AsyncMock()
        ) as post, patch.object(
            self.group, "post_agent_answer_to_group", new=AsyncMock()
        ), patch.object(
            self.group, "post_team_handoff", new=AsyncMock()
        ):
            outcome = await self.group._execute_routed_task(
                project, task, "code", "prompt", sink
            )

        self.assertEqual(outcome, "blocked")
        self.assertEqual(self.fake_main.ask_specialist.call_count, 1)
        terminal = [
            call for call in update.call_args_list
            if len(call.args) > 1 and call.args[1] == "blocked"
        ][0]
        self.assertEqual(terminal.kwargs["failure_classification"], "budget")
        self.assertEqual(sink["team_help_events"][0]["status"], "deferred")
        self.assertTrue(any("No owner action" in call.args[0] for call in post.await_args_list))

    async def test_teammate_missing_access_escalates_once_without_resuming_worker(self):
        task = {
            "id": "worker-help-access", "owner": "code", "title": "Validate",
            "model": "worker-model", "model_reason": "Coding route",
            "execution_attempts": 0, "attempt_history": [],
        }
        project = {"id": "project-1", "title": "Project", "goal": "Validate"}
        sink = {"cost_usd": 0.0, "artifacts": [], "usage_records": [], "context": "test"}
        request = (
            'AUTONOMY_HELP_REQUEST {"helper":"research","question":"Read the source",'
            '"reason":"The source is required","task_type":"research",'
            '"complexity":"standard","risk":"medium"}'
        )
        responses = iter([
            request,
            "BLOCKED - NEEDS HUMAN REVIEW: MISSING_ACCESS\nThe required source is private.",
        ])
        self.fake_main.ask_specialist = Mock(side_effect=lambda *args, **kwargs: next(responses))
        route = types.SimpleNamespace(
            model_id="helper-model", model="helper-model", deferred=False,
            reason="Independent research route.", deferral_reason=None,
        )
        with patch.object(
            self.group, "_company_task_route", return_value=route
        ), patch.object(
            self.group.company_mode, "update_task_status", return_value="ok"
        ) as update, patch.object(
            self.group, "post_to_group", new=AsyncMock()
        ) as post, patch.object(
            self.group, "post_agent_answer_to_group", new=AsyncMock()
        ), patch.object(
            self.group, "post_team_handoff", new=AsyncMock()
        ):
            outcome = await self.group._execute_routed_task(
                project, task, "code", "prompt", sink
            )

        self.assertEqual(outcome, "blocked")
        self.assertEqual(self.fake_main.ask_specialist.call_count, 2)
        terminal = [
            call for call in update.call_args_list
            if len(call.args) > 1 and call.args[1] == "needs_human"
        ]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0].kwargs["failure_classification"], "missing_access")
        self.assertEqual(sink["team_help_events"][0]["status"], "failed")
        self.assertEqual(len(post.await_args_list), 2)  # starting note + one escalation

    async def test_helper_budget_guard_exception_stops_before_worker_resume(self):
        task = {
            "id": "helper-budget-guard", "owner": "code", "title": "Validate",
            "model": "worker-model", "model_reason": "Coding route",
            "execution_attempts": 0, "attempt_history": [],
        }
        project = {"id": "project-1", "title": "Project", "goal": "Validate"}
        sink = {"cost_usd": 0.0, "artifacts": [], "usage_records": [], "context": "test"}
        request = (
            'AUTONOMY_HELP_REQUEST {"helper":"research","question":"Check it",'
            '"reason":"Need evidence","task_type":"research",'
            '"complexity":"standard","risk":"low"}'
        )

        def ask_specialist(key, _prompt, **_kwargs):
            if key == "code":
                return request
            sink["budget_guard_blocked"] = True
            raise self.fake_main.ExecutionBudgetExceededError(
                "The next helper request exceeds the reserved budget envelope."
            )

        self.fake_main.ask_specialist = Mock(side_effect=ask_specialist)
        route = types.SimpleNamespace(
            model_id="helper-model", model="helper-model", deferred=False,
            reason="Independent helper route.", deferral_reason=None,
        )
        with patch.object(
            self.group, "_company_task_route", return_value=route
        ), patch.object(
            self.group, "_remaining_task_headroom", return_value=1.0
        ), patch.object(
            self.group.company_mode, "update_task_status", return_value="ok"
        ) as update, patch.object(
            self.group, "post_to_group", new=AsyncMock()
        ), patch.object(
            self.group, "post_agent_answer_to_group", new=AsyncMock()
        ), patch.object(
            self.group, "post_team_handoff", new=AsyncMock()
        ):
            outcome = await self.group._execute_routed_task(
                project, task, "code", "prompt", sink
            )

        self.assertEqual(outcome, "blocked")
        self.assertEqual(self.fake_main.ask_specialist.call_count, 2)
        self.assertEqual(sink["team_help_events"][0]["status"], "failed")
        terminal = [
            call for call in update.call_args_list
            if len(call.args) > 1 and call.args[1] == "blocked"
        ]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0].kwargs["failure_classification"], "budget")
        self.assertEqual(terminal[0].args[4], 0.0)

    async def test_worker_resume_budget_guard_stops_without_retrying(self):
        task = {
            "id": "resume-budget-guard", "owner": "code", "title": "Validate",
            "model": "worker-model", "model_reason": "Coding route",
            "execution_attempts": 0, "attempt_history": [],
        }
        project = {"id": "project-1", "title": "Project", "goal": "Validate"}
        sink = {"cost_usd": 0.0, "artifacts": [], "usage_records": [], "context": "test"}
        request = (
            'AUTONOMY_HELP_REQUEST {"helper":"research","question":"Check it",'
            '"reason":"Need evidence","task_type":"research",'
            '"complexity":"standard","risk":"low"}'
        )
        calls = []

        def ask_specialist(key, _prompt, **_kwargs):
            calls.append(key)
            if calls == ["code"]:
                return request
            if calls == ["code", "research"]:
                return "The evidence supports the claim."
            sink["budget_guard_blocked"] = True
            raise self.fake_main.ExecutionBudgetExceededError(
                "The resumed worker request exceeds the reserved budget envelope."
            )

        self.fake_main.ask_specialist = Mock(side_effect=ask_specialist)
        route = types.SimpleNamespace(
            model_id="helper-model", model="helper-model", deferred=False,
            reason="Independent helper route.", deferral_reason=None,
        )
        with patch.object(
            self.group, "_company_task_route", return_value=route
        ), patch.object(
            self.group, "_remaining_task_headroom", return_value=1.0
        ), patch.object(
            self.group.company_mode, "update_task_status", return_value="ok"
        ) as update, patch.object(
            self.group, "post_to_group", new=AsyncMock()
        ), patch.object(
            self.group, "post_agent_answer_to_group", new=AsyncMock()
        ), patch.object(
            self.group, "post_team_handoff", new=AsyncMock()
        ):
            outcome = await self.group._execute_routed_task(
                project, task, "code", "prompt", sink
            )

        self.assertEqual(outcome, "blocked")
        self.assertEqual(calls, ["code", "research", "code"])
        self.assertEqual(sink["team_help_events"][0]["status"], "completed")
        terminal = [
            call for call in update.call_args_list
            if len(call.args) > 1 and call.args[1] == "blocked"
        ]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0].kwargs["failure_classification"], "budget")

    def test_tool_backed_lightweight_runtime_route_requires_tool_capable_model(self):
        task = {
            "owner": "manager", "task_type": "classification",
            "complexity": "lightweight", "risk": "low",
            "required_capabilities": ["text"],
            "estimated_input_tokens": 1000, "estimated_output_tokens": 200,
        }
        no_tools = self.group._company_task_route(
            task, remaining_usd=1.0, has_tools=False
        )
        with_tools = self.group._company_task_route(
            task, remaining_usd=1.0, has_tools=True
        )
        self.assertEqual(no_tools.model_id, "gpt-5.4-nano")
        self.assertEqual(with_tools.model_id, "gpt-5.4-mini")
        self.assertIn("tool_use", with_tools.reason)

    def test_advanced_no_tool_helper_route_selects_stronger_model(self):
        decision = self.group._company_task_route(
            {
                "owner": "research",
                "task_type": "architecture_decision",
                "complexity": "advanced",
                "risk": "high",
                "required_capabilities": ["text"],
                "estimated_input_tokens": 5000,
                "estimated_output_tokens": 600,
            },
            remaining_usd=5.0,
            has_tools=False,
        )

        self.assertFalse(decision.deferred)
        self.assertEqual(decision.model_id, "gpt-5.6-sol")
        self.assertIn("risk=high", decision.reason)

    def test_blank_agent_answer_is_a_technical_failure(self):
        self.assertEqual(self.group._answer_failure_classification(""), "technical")
        self.assertEqual(self.group._answer_failure_classification("   "), "technical")

    async def test_autonomous_reviewer_posts_approval_and_revision_as_vera(self):
        project = {"id": "project-1", "title": "Project", "goal": "Ship safely"}
        worker = {
            "id": "worker-1",
            "project_id": "project-1",
            "owner": "code",
            "status": "done",
        }
        sink = {"cost_usd": 0.0, "artifacts": [], "usage_records": [], "context": "test"}

        for verdict, answer, expected_text in (
            ("approved", "APPROVED - all criteria have evidence.", "Approved."),
            ("revise", "REVISIONS REQUIRED\n1. Add the missing test.", "Code, I need one change"),
        ):
            with self.subTest(verdict=verdict):
                task = {
                    "id": f"review-{verdict}",
                    "owner": "editor",
                    "title": (
                        "Review the completed result and respond APPROVED, "
                        "REVISIONS REQUIRED, or BLOCKED - NEEDS HUMAN REVIEW"
                    ),
                    "model": "gpt-5.4-mini",
                    "model_reason": "Standard evidence review.",
                    "execution_attempts": 0,
                    "attempt_history": [],
                }
                state = {
                    "projects": [project],
                    "tasks": [worker, {**task, "project_id": "project-1", "status": "done"}],
                    "company": {},
                }
                self.fake_main.ask_specialist = Mock(return_value=answer)
                with patch.object(
                    self.group.company_mode, "update_task_status", return_value="ok"
                ), patch.object(
                    self.group.company_mode, "set_project_revision_flag", return_value=verdict
                ), patch.object(
                    self.group.company_mode, "load_state", return_value=state
                ), patch.object(
                    self.group.company_mode, "render_money", return_value="Budget ok"
                ), patch.object(
                    self.group, "post_to_group", new=AsyncMock()
                ), patch.object(
                    self.group, "post_agent_answer_to_group", new=AsyncMock()
                ), patch.object(
                    self.group, "post_team_handoff", new=AsyncMock()
                ) as handoff:
                    outcome = await self.group._execute_routed_task(
                        project, task, "editor", "prompt", dict(sink)
                    )

                self.assertEqual(outcome, "done")
                assignment = handoff.await_args_list[0].args[1]
                self.assertEqual(handoff.await_args_list[0].args[0], "manager")
                self.assertIn("please review the completed work", assignment)
                assert_conversational_chat(self, assignment)
                self.assertEqual(handoff.await_args_list[-1].args[0], "editor")
                review_message = handoff.await_args_list[-1].args[1]
                self.assertIn(expected_text, review_message)
                self.assertIn("All criteria have evidence" if verdict == "approved" else "Add the missing test", review_message)
                self.assertNotIn(answer.splitlines()[0], review_message)
                assert_conversational_chat(self, review_message)

    async def test_autonomous_reviewer_posts_structured_block_as_vera(self):
        project = {"id": "project-1", "title": "Project", "goal": "Ship safely"}
        task = {
            "id": "review-blocked",
            "owner": "editor",
            "title": "Review against acceptance criteria",
            "model": "gpt-5.4-mini",
            "model_reason": "Standard evidence review.",
            "execution_attempts": 0,
            "attempt_history": [],
        }
        answer = "BLOCKED - NEEDS HUMAN REVIEW: MISSING_ACCESS\nProvide repository access."
        sink = {"cost_usd": 0.0, "artifacts": [], "usage_records": [], "context": "test"}
        self.fake_main.ask_specialist = Mock(return_value=answer)
        with patch.object(
            self.group.company_mode, "update_task_status", return_value="ok"
        ), patch.object(
            self.group.autonomous_workflow, "format_escalation", return_value="Escalated"
        ), patch.object(
            self.group, "post_to_group", new=AsyncMock()
        ), patch.object(
            self.group, "post_team_handoff", new=AsyncMock()
        ) as handoff:
            outcome = await self.group._execute_routed_task(
                project, task, "editor", "prompt", sink
            )

        self.assertEqual(outcome, "blocked")
        self.assertEqual(handoff.await_args_list[-1].args[0], "editor")
        review_message = handoff.await_args_list[-1].args[1]
        self.assertIn("I can't approve", review_message)
        self.assertIn("Missing access", review_message)
        self.assertIn("Provide repository access", review_message)
        self.assertNotIn("BLOCKED - NEEDS HUMAN REVIEW", review_message)
        assert_conversational_chat(self, review_message)

    async def test_autonomous_retry_posts_the_stronger_model_decision(self):
        project = {"id": "project-1", "title": "Project", "goal": "Ship safely"}
        task = {
            "id": "worker-retry",
            "owner": "code",
            "title": "Debug the issue",
            "model": "gpt-5.4-mini",
            "model_reason": "Standard debugging route.",
            "execution_attempts": 0,
            "attempt_history": [],
        }
        editor = {"id": "review-1", "owner": "editor", "status": "planned"}
        state = {"projects": [project], "tasks": [{**task, "status": "done"}, editor], "company": {}}
        sink = {"cost_usd": 0.0, "artifacts": [], "usage_records": [], "context": "test"}
        self.fake_main.ask_specialist = Mock(
            side_effect=["Sorry, something went wrong processing that.", "Recovered result."]
        )
        stronger = types.SimpleNamespace(
            deferred=False,
            model_id="gpt-5.6-sol",
            reason="The failed Mini attempt requires a stronger untried model.",
        )
        with patch.object(
            self.group, "_company_task_route", return_value=stronger
        ), patch.object(
            self.group.company_mode, "update_task_status", return_value="ok"
        ), patch.object(
            self.group.company_mode, "load_state", return_value=state
        ), patch.object(
            self.group.company_mode, "next_planned_task", return_value=editor
        ), patch.object(
            self.group.company_mode, "render_money", return_value="Budget ok"
        ), patch.object(
            self.group, "post_to_group", new=AsyncMock()
        ), patch.object(
            self.group, "post_agent_answer_to_group", new=AsyncMock()
        ), patch.object(
            self.group, "post_team_handoff", new=AsyncMock()
        ) as handoff:
            outcome = await self.group._execute_routed_task(
                project, task, "code", "prompt", sink
            )

        self.assertEqual(outcome, "done")
        retry_messages = [call.args[1] for call in handoff.await_args_list if "once more" in call.args[1]]
        self.assertEqual(len(retry_messages), 1)
        self.assertIn("gpt-5.6-sol", retry_messages[0])
        self.assertNotIn("stronger untried model", retry_messages[0])
        assert_conversational_chat(self, retry_messages[0])

    async def test_company_task_sink_uses_its_persisted_reservation_as_hard_envelope(self):
        project = {"id": "project-1", "title": "Project", "goal": "Inspect"}
        task = {
            "id": "task-1", "project_id": "project-1", "owner": "manager",
            "title": "Inspect", "reserved_usd": 0.125,
            "budget_reservation_id": "res-1",
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
        with patch.object(
            self.group.company_mode,
            "expand_task_budget_reservation",
            return_value={"reason": "expanded", "amount_usd": 0.30},
        ) as expand:
            self.assertEqual(sink["budget_top_up"](0.20, 0.30), 0.30)
        expand.assert_called_once_with(
            "task-1", 0.20, 0.30, path=self.group.company_mode.COMPANY_STATE_FILE
        )
        self.assertEqual(sink["budget_top_up_reason"], "expanded")

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
        result = "complete evidence FULL_REPORT_ONLY_SENTINEL\n" + ("x" * 7000)
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
        ), patch.object(
            self.group, "post_team_handoff", new=AsyncMock()
        ) as handoff:
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
        self.assertTrue(handoff.await_args_list)
        for call in handoff.await_args_list:
            self.assertNotIn("FULL_REPORT_ONLY_SENTINEL", call.args[1])
            assert_conversational_chat(self, call.args[1])

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
        self.assertTrue(sink["deadline_exceeded"])
        self.assertTrue(any(len(call.args) > 1 and call.args[1] == "needs_human" for call in update.call_args_list))

    async def test_invoke_preserves_cancellation_when_joined_worker_errors(self):
        started = threading.Event()
        release = threading.Event()

        def fail_after_cancel(*args, **kwargs):
            started.set()
            release.wait(timeout=2)
            raise RuntimeError("worker failed after cancellation")

        self.group.main.ask_ai = Mock(side_effect=fail_after_cancel)
        sink = {
            "cost_usd": 0.0, "artifacts": [], "usage_records": [],
            "context": "test",
        }
        invocation = asyncio.create_task(self.group._invoke_company_agent(
            None,
            "prompt",
            "test-model",
            set(),
            sink,
            enforce_authorization=True,
        ))
        self.assertTrue(await asyncio.to_thread(started.wait, 1))
        invocation.cancel()
        release.set()

        with self.assertRaises(asyncio.CancelledError):
            await invocation
        self.assertTrue(any(
            "RuntimeError" in str(call)
            for call in self.group.main.logger.error.call_args_list
        ))

    def test_unmeasured_call_never_releases_partial_measured_spend(self):
        sink = {
            "cost_usd": 0.10,
            "budget_cap_usd": 0.50,
            "usage_records": [{"model": "small", "cost_usd": 0.10}],
            "unmeasured_model_calls": 1,
            "budget_guard_blocked": True,
        }

        self.assertIsNone(self.group._sink_spend_for_reconciliation(sink))

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
        self.assertEqual(tasks[1]["failure_classification"], "missing_access")
        self.assertEqual(project["failure_classification"], "missing_access")
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

    def test_reactive_router_stays_lightweight_for_architecture_text(self):
        sink = {
            "cost_usd": 0.0,
            "budget_cap_usd": 1.0,
            "model_route_decisions": [],
        }
        with patch.object(
            self.group.main,
            "current_execution_sink",
            new=Mock(return_value=sink),
            create=True,
        ), patch.dict(os.environ, {
            "REACTIVE_ROUTING_INPUT_TOKENS": "3000",
            "REACTIVE_ROUTING_OUTPUT_TOKENS": "800",
        }):
            selected = self.group._route_reactive_model(
                "router",
                "Route this production architecture and security review.",
                (),
            )

        self.assertEqual(selected, "gpt-5.4-nano")
        route = sink["model_route_decisions"][0]
        self.assertEqual(route["task_type"], "routing")
        self.assertEqual(route["complexity"], "lightweight")
        self.assertEqual(route["risk"], "low")
        self.assertNotIn("prompt", route)

    def test_reactive_standard_tool_task_selects_mini(self):
        sink = {
            "cost_usd": 0.0,
            "budget_cap_usd": 1.0,
            "model_route_decisions": [],
        }
        with patch.object(
            self.group.main,
            "current_execution_sink",
            new=Mock(return_value=sink),
            create=True,
        ):
            selected = self.group._route_reactive_model(
                "code", "Implement a small parser and run its tests.", ["read_file"]
            )

        self.assertEqual(selected, "gpt-5.4-mini")
        route = sink["model_route_decisions"][0]
        self.assertEqual(route["task_type"], "coding")
        self.assertTrue(route["uses_tools"])
        self.assertEqual(route["model_level"], "standard")

    def test_reactive_advanced_high_risk_task_selects_sol(self):
        sink = {
            "cost_usd": 0.0,
            "budget_cap_usd": 1.0,
            "model_route_decisions": [],
        }
        with patch.object(
            self.group.main,
            "current_execution_sink",
            new=Mock(return_value=sink),
            create=True,
        ):
            selected = self.group._route_reactive_model(
                "code",
                "Review the production security architecture before deployment.",
                ["read_file"],
            )

        self.assertEqual(selected, "gpt-5.6-sol")
        route = sink["model_route_decisions"][0]
        self.assertEqual(route["task_type"], "security_review")
        self.assertEqual(route["complexity"], "advanced")
        self.assertEqual(route["risk"], "high")

    def test_reactive_complex_debugging_selects_terra(self):
        sink = {
            "cost_usd": 0.0,
            "budget_cap_usd": 1.0,
            "model_route_decisions": [],
        }
        with patch.object(
            self.group.main,
            "current_execution_sink",
            new=Mock(return_value=sink),
            create=True,
        ):
            selected = self.group._route_reactive_model(
                "code",
                "Perform root-cause analysis for this difficult debugging failure.",
                ["read_file"],
            )

        self.assertEqual(selected, "gpt-5.6-terra")
        route = sink["model_route_decisions"][0]
        self.assertEqual(route["task_type"], "complex_debugging")
        self.assertEqual(route["model_level"], "advanced")

    def test_reactive_insufficient_budget_starts_no_provider_call(self):
        sink = {
            "cost_usd": 0.0,
            "budget_cap_usd": 0.0001,
            "model_route_decisions": [],
        }
        provider_call = Mock(return_value="should not run")

        def admitted_call():
            self.group._route_reactive_model("router", "Route this.", ())
            return provider_call()

        with patch.object(
            self.group.main,
            "current_execution_sink",
            new=Mock(return_value=sink),
            create=True,
        ):
            with self.assertRaises(self.fake_main.ExecutionBudgetExceededError):
                admitted_call()

        provider_call.assert_not_called()
        self.assertTrue(sink["budget_guard_blocked"])
        self.assertEqual(sink["model_route_decisions"][0]["status"], "deferred")
        self.assertEqual(sink["model_route_decisions"][0]["model"], "")

    async def test_metered_reconciliation_persists_reactive_route(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "company.json"
            company_mode.set_daily_budget(1.0, path)
            current = {"sink": None}

            def set_sink(value):
                current["sink"] = value

            def routed_work():
                return self.group._route_reactive_model(
                    "code", "Implement a small parser.", ["read_file"]
                )

            with patch.object(
                self.group.company_mode, "COMPANY_STATE_FILE", path
            ), patch.object(
                self.group.main, "set_execution_sink", side_effect=set_sink
            ), patch.object(
                self.group.main,
                "current_execution_sink",
                new=lambda: current["sink"],
                create=True,
            ):
                result, receipt = await self.group._run_metered(
                    routed_work,
                    estimate_usd=0.10,
                    context="telegram route persistence test",
                    agent="code",
                    return_receipt=True,
                    strict_budget=True,
                )
            state = company_mode.load_state(path)

        self.assertEqual(result, "gpt-5.4-mini")
        self.assertEqual(receipt["model_route_decisions"][0]["model"], "gpt-5.4-mini")
        entry = state["cost_entries"][-1]
        self.assertEqual(entry["agent"], "code")
        self.assertEqual(entry["model"], "gpt-5.4-mini")
        self.assertIn("Reactive model route", entry["reason"])
        self.assertIn("code->gpt-5.4-mini", entry["reason"])

    async def test_metered_route_deferral_persists_reason_with_zero_spend(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "company.json"
            company_mode.set_daily_budget(1.0, path)
            current = {"sink": None}

            def set_sink(value):
                current["sink"] = value

            def deferred_work():
                current["sink"]["budget_cap_usd"] = 0.0001
                return self.group._route_reactive_model(
                    "router", "Route this request.", ()
                )

            with patch.object(
                self.group.company_mode, "COMPANY_STATE_FILE", path
            ), patch.object(
                self.group.main, "set_execution_sink", side_effect=set_sink
            ), patch.object(
                self.group.main,
                "current_execution_sink",
                new=lambda: current["sink"],
                create=True,
            ):
                with self.assertRaises(self.fake_main.ExecutionBudgetExceededError):
                    await self.group._run_metered(
                        deferred_work,
                        estimate_usd=0.10,
                        context="telegram route deferral persistence test",
                        agent="router",
                        strict_budget=True,
                    )
            state = company_mode.load_state(path)

        entry = state["cost_entries"][-1]
        self.assertEqual(entry["amount_usd"], 0.0)
        self.assertEqual(entry["model"], "")
        self.assertIn("router->deferred", entry["reason"])
        self.assertIn("insufficient_budget", entry["reason"])

    async def test_metered_reservation_denial_persists_zero_cost_deferral(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "company.json"
            # The default $0.25 emergency reserve leaves no ordinary budget here.
            company_mode.set_daily_budget(0.25, path)
            provider_call = Mock(return_value="must not run")

            with patch.object(
                self.group.company_mode, "COMPANY_STATE_FILE", path
            ):
                with self.assertRaises(company_mode.BudgetExceededError):
                    await self.group._run_metered(
                        provider_call,
                        estimate_usd=0.01,
                        context="telegram exhausted-budget test",
                        agent="code",
                        strict_budget=True,
                    )
            state = company_mode.load_state(path)

        provider_call.assert_not_called()
        self.assertEqual(state["company"]["spent_today_usd"], 0.0)
        self.assertEqual(state["company"]["reserved_today_usd"], 0.0)
        entry = state["cost_entries"][-1]
        self.assertEqual(entry["amount_usd"], 0.0)
        self.assertEqual(entry["context"], "telegram exhausted-budget test")
        self.assertEqual(entry["agent"], "code")
        self.assertIn("reserve", entry["reason"].lower())
        decision = entry["model_route_decisions"][0]
        self.assertEqual(decision["status"], "deferred")
        self.assertEqual(decision["deferral_reason"], "reservation_denied")
        self.assertEqual(state["events"][-1]["type"], "budget_deferred")

    async def test_group_router_budget_failure_does_not_fallback_to_miles(self):
        error = self.fake_main.ExecutionBudgetExceededError(
            "The routing request cannot fit its strict budget envelope."
        )
        update = types.SimpleNamespace(
            message=types.SimpleNamespace(
                text="Please review the architecture.", reply_text=AsyncMock()
            )
        )
        usernames = {key: f"{key}_bot" for key in self.group.BOT_KEYS}
        ask_manager = Mock(return_value="Miles should not run")
        with patch.object(
            self.group, "bot_usernames", usernames
        ), patch.object(
            self.group, "_handle_pending_confirmation", new=AsyncMock(return_value=False)
        ), patch.object(
            self.group, "_maybe_handle_project_linear_command", new=AsyncMock(return_value=False)
        ), patch.object(
            self.group.company_mode, "parse_company_command", return_value=None
        ), patch.object(
            self.group.company_mode, "handle_company_command", return_value=None
        ), patch.object(
            self.group.main, "select_group_responders", new=Mock(), create=True
        ), patch.object(
            self.group.main, "ask_manager", new=ask_manager, create=True
        ), patch.object(
            self.group, "_run_metered", new=AsyncMock(side_effect=error)
        ) as metered, patch.object(
            self.group, "reply_chunks", new=AsyncMock()
        ) as reply:
            await self.group.handle_group_message(update)

        ask_manager.assert_not_called()
        self.assertEqual(metered.await_count, 1)
        self.assertEqual(metered.await_args.kwargs["agent"], "router")
        self.assertTrue(metered.await_args.kwargs["strict_budget"])
        self.assertIn("before another fallback", reply.await_args.args[1])


if __name__ == "__main__":
    unittest.main()

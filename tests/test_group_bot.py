import asyncio
import importlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from test_hardening import import_main_with_stubs  # noqa: F401 (shares stub set)
    from test_hardening import FakeChromaClient
except ImportError:  # running as a package module (python -m unittest tests.test_group_bot)
    from tests.test_hardening import import_main_with_stubs  # noqa: F401
    from tests.test_hardening import FakeChromaClient

import types


def import_group_bot_with_stubs():
    """Import group_bot (and the main it depends on) with the same third-party stubs
    test_hardening uses, plus the env vars group_bot requires at import time."""
    fake_date_parser = types.SimpleNamespace(parse=lambda value: value)
    stubs = {
        "chromadb": types.SimpleNamespace(PersistentClient=FakeChromaClient),
        "dateutil": types.SimpleNamespace(parser=fake_date_parser),
        "dateutil.parser": fake_date_parser,
        "openai": types.SimpleNamespace(OpenAI=lambda: object()),
        "requests": types.SimpleNamespace(get=lambda *a, **k: None, post=lambda *a, **k: None),
        "tavily": types.SimpleNamespace(TavilyClient=lambda: object()),
        "dotenv": types.SimpleNamespace(load_dotenv=lambda: None),
    }
    env = {
        "TELEGRAM_GROUP_CHAT_ID": "-1001234567890",
        "TELEGRAM_ALLOWED_USER_IDS": "12345",
    }

    for module in ("group_bot", "main"):
        sys.modules.pop(module, None)
    with patch.dict(os.environ, env), patch.dict(sys.modules, stubs):
        return importlib.import_module("group_bot")


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text):
        self.replies.append(text)


class FakeUpdate:
    def __init__(self):
        self.message = FakeMessage()
        self.effective_user = types.SimpleNamespace(id=12345)


class StripBotSuffixTests(unittest.TestCase):
    """Telegram appends @botname to a command tapped from the command menu in a
    multi-bot group. _strip_bot_suffix must normalize that on the LEADING command
    token only, leaving ordinary text (mentions, email addresses) untouched."""

    @classmethod
    def setUpClass(cls):
        cls.gb = import_group_bot_with_stubs()

    def test_strips_suffix_from_bare_command(self):
        self.assertEqual(self.gb._strip_bot_suffix("/confirm@TyManagerBot"), "/confirm")

    def test_plain_command_unchanged(self):
        self.assertEqual(self.gb._strip_bot_suffix("/confirm"), "/confirm")

    def test_strips_suffix_but_keeps_arguments(self):
        self.assertEqual(
            self.gb._strip_bot_suffix("/project@TyManagerBot use vantage"),
            "/project use vantage",
        )

    def test_surrounding_whitespace_is_stripped(self):
        self.assertEqual(self.gb._strip_bot_suffix("  /confirm@TyManagerBot  "), "/confirm")

    def test_non_command_text_is_untouched(self):
        self.assertEqual(self.gb._strip_bot_suffix("hello team"), "hello team")
        self.assertEqual(
            self.gb._strip_bot_suffix("email bob@example.com about the launch"),
            "email bob@example.com about the launch",
        )
        self.assertEqual(self.gb._strip_bot_suffix("@TyManagerBot"), "@TyManagerBot")


class PendingConfirmationTests(unittest.TestCase):
    """/confirm must approve a staged sensitive action even when Telegram sends it
    as /confirm@BotName - before the fix, that exact-match miss CANCELLED the very
    action the user was trying to approve."""

    @classmethod
    def setUpClass(cls):
        cls.gb = import_group_bot_with_stubs()
        cls.main = cls.gb.main

    def setUp(self):
        self.main.set_conversation("test:groupbot-confirm")
        self.main.clear_pending_action()

    def tearDown(self):
        self.main.clear_pending_action()

    def _stage_write(self):
        self.main.set_pending_action(
            {"type": "write_file", "filename": "a.txt", "content": "x"}
        )

    def test_confirm_with_bot_suffix_confirms(self):
        self._stage_write()
        update = FakeUpdate()
        with patch.object(self.main, "confirm_pending_action", lambda pending: "CONFIRMED-OK"):
            handled = asyncio.run(
                self.gb._handle_pending_confirmation(update, "/confirm@TyManagerBot")
            )
        self.assertTrue(handled)
        self.assertIn("CONFIRMED-OK", update.message.replies)
        self.assertIsNone(self.main.get_pending_action())

    def test_plain_confirm_still_confirms(self):
        self._stage_write()
        update = FakeUpdate()
        with patch.object(self.main, "confirm_pending_action", lambda pending: "CONFIRMED-OK"):
            handled = asyncio.run(self.gb._handle_pending_confirmation(update, "/confirm"))
        self.assertTrue(handled)
        self.assertIn("CONFIRMED-OK", update.message.replies)

    def test_other_text_cancels(self):
        self._stage_write()
        update = FakeUpdate()

        def must_not_run(pending):
            raise AssertionError("cancel path must not execute the staged action")

        with patch.object(self.main, "confirm_pending_action", must_not_run):
            handled = asyncio.run(self.gb._handle_pending_confirmation(update, "never mind"))
        self.assertTrue(handled)
        self.assertTrue(any("Cancelled" in reply for reply in update.message.replies))
        self.assertIsNone(self.main.get_pending_action())

    def test_confirm_with_bot_suffix_confirms_publish(self):
        self.main.set_pending_action(
            {"type": "publish", "project_id": "proj_1", "title": "Pack"}
        )
        update = FakeUpdate()
        with patch.object(self.gb.company_mode, "mark_project_published", lambda: "PUBLISHED-OK"):
            handled = asyncio.run(
                self.gb._handle_pending_confirmation(update, "/confirm@TyManagerBot")
            )
        self.assertTrue(handled)
        self.assertTrue(any("PUBLISHED-OK" in reply for reply in update.message.replies))

    def test_returns_false_when_nothing_staged(self):
        update = FakeUpdate()
        handled = asyncio.run(self.gb._handle_pending_confirmation(update, "/confirm"))
        self.assertFalse(handled)
        self.assertEqual(update.message.replies, [])


class SlashInterceptTests(unittest.TestCase):
    """/today, /project, and /linear must still be intercepted (not routed to Miles
    as plain chat) when Telegram appends the bot's @username to them."""

    @classmethod
    def setUpClass(cls):
        cls.gb = import_group_bot_with_stubs()
        cls.main = cls.gb.main

    def test_today_with_bot_suffix_is_intercepted(self):
        update = FakeUpdate()
        with patch.object(self.main, "handle_today_command", lambda: "TODAY-OK"):
            handled = asyncio.run(
                self.gb._maybe_handle_project_linear_command(update, "/today@TyManagerBot")
            )
        self.assertTrue(handled)
        self.assertIn("TODAY-OK", update.message.replies)

    def test_project_with_bot_suffix_passes_arguments(self):
        update = FakeUpdate()
        captured = {}

        def fake_project(rest):
            captured["rest"] = rest
            return "PROJECT-OK"

        with patch.object(self.main, "handle_project_command", fake_project):
            handled = asyncio.run(
                self.gb._maybe_handle_project_linear_command(
                    update, "/project@TyManagerBot use vantage"
                )
            )
        self.assertTrue(handled)
        self.assertEqual(captured["rest"].strip(), "use vantage")
        self.assertIn("PROJECT-OK", update.message.replies)

    def test_project_brainstorm_uses_strict_metered_model_path(self):
        update = FakeUpdate()
        with patch.object(self.main, "handle_project_command") as command, patch.object(
            self.gb,
            "_run_metered",
            new=AsyncMock(return_value="PROJECT-PLAN"),
        ) as metered:
            handled = asyncio.run(
                self.gb._maybe_handle_project_linear_command(
                    update,
                    "/project@TyManagerBot brainstorm assistant improve onboarding",
                )
            )

        self.assertTrue(handled)
        command.assert_not_called()
        metered.assert_awaited_once()
        self.assertIs(metered.await_args.args[0], command)
        self.assertEqual(
            metered.await_args.args[1].strip(),
            "brainstorm assistant improve onboarding",
        )
        self.assertEqual(metered.await_args.kwargs["context"], "telegram project brainstorm")
        self.assertEqual(metered.await_args.kwargs["agent"], "manager")
        self.assertTrue(metered.await_args.kwargs["strict_budget"])
        self.assertTrue(metered.await_args.kwargs["no_model_is_zero"])
        self.assertEqual(update.message.replies, ["PROJECT-PLAN"])

    def test_linear_with_bot_suffix_is_intercepted(self):
        update = FakeUpdate()
        with patch.object(self.main, "handle_linear_command", lambda rest: "LINEAR-OK"):
            handled = asyncio.run(
                self.gb._maybe_handle_project_linear_command(update, "/linear@TyManagerBot issues")
            )
        self.assertTrue(handled)
        self.assertIn("LINEAR-OK", update.message.replies)

    def test_linear_from_sprint_uses_strict_metered_model_path(self):
        update = FakeUpdate()
        with patch.object(self.main, "handle_linear_command") as command, patch.object(
            self.gb,
            "_run_metered",
            new=AsyncMock(return_value="LINEAR-PLAN"),
        ) as metered:
            handled = asyncio.run(
                self.gb._maybe_handle_project_linear_command(
                    update,
                    "/linear@TyManagerBot from-sprint assistant harden routing",
                )
            )

        self.assertTrue(handled)
        command.assert_not_called()
        metered.assert_awaited_once()
        self.assertIs(metered.await_args.args[0], command)
        self.assertEqual(
            metered.await_args.args[1].strip(),
            "from-sprint assistant harden routing",
        )
        self.assertEqual(metered.await_args.kwargs["context"], "telegram linear from-sprint")
        self.assertEqual(metered.await_args.kwargs["agent"], "linear")
        self.assertTrue(metered.await_args.kwargs["strict_budget"])
        self.assertTrue(metered.await_args.kwargs["no_model_is_zero"])
        self.assertEqual(update.message.replies, ["LINEAR-PLAN"])

    def test_read_only_project_and_linear_commands_remain_unmetered(self):
        project_update = FakeUpdate()
        linear_update = FakeUpdate()
        with patch.object(
            self.main, "handle_project_command", return_value="PROJECT-STATUS"
        ) as project_command, patch.object(
            self.main, "handle_linear_command", return_value="LINEAR-ISSUES"
        ) as linear_command, patch.object(
            self.gb, "_run_metered", new=AsyncMock()
        ) as metered:
            project_handled = asyncio.run(
                self.gb._maybe_handle_project_linear_command(
                    project_update, "/project status"
                )
            )
            linear_handled = asyncio.run(
                self.gb._maybe_handle_project_linear_command(
                    linear_update, "/linear issues"
                )
            )

        self.assertTrue(project_handled)
        self.assertTrue(linear_handled)
        project_command.assert_called_once_with(" status")
        linear_command.assert_called_once_with(" issues")
        metered.assert_not_awaited()
        self.assertEqual(project_update.message.replies, ["PROJECT-STATUS"])
        self.assertEqual(linear_update.message.replies, ["LINEAR-ISSUES"])

    def test_model_command_admission_failure_replies_once_with_action(self):
        update = FakeUpdate()
        failure = self.main.ExecutionBudgetExceededError("daily budget exhausted")
        with patch.object(
            self.gb,
            "_run_metered",
            new=AsyncMock(side_effect=failure),
        ) as metered:
            handled = asyncio.run(
                self.gb._maybe_handle_project_linear_command(
                    update, "/project brainstorm assistant improve onboarding"
                )
            )

        self.assertTrue(handled)
        metered.assert_awaited_once()
        self.assertEqual(len(update.message.replies), 1)
        self.assertIn("AI admission stopped", update.message.replies[0])
        self.assertIn("daily budget exhausted", update.message.replies[0])
        self.assertIn("Action:", update.message.replies[0])
        self.assertIn("/status", update.message.replies[0])

    def test_plain_chat_is_not_intercepted(self):
        update = FakeUpdate()
        handled = asyncio.run(
            self.gb._maybe_handle_project_linear_command(update, "hello team")
        )
        self.assertFalse(handled)
        self.assertEqual(update.message.replies, [])


if __name__ == "__main__":
    unittest.main()

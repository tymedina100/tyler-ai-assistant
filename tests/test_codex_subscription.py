import json
import subprocess
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_subscription import QuotaEvidence, SubscriptionUnavailable, analyze


class SubscriptionTests(unittest.TestCase):
    def options(self):
        return dict(binary="/trusted/codex", model="explicit-model", effort="low",
                    quota=QuotaEvidence(time.time(), 25), enabled=True)

    @patch("codex_subscription.subprocess.run")
    def test_disabled_stale_and_exhausted_never_start(self, run):
        for overrides in ({"enabled": False}, {"quota": QuotaEvidence(time.time()-301, 1)},
                          {"quota": QuotaEvidence(time.time(), 91)},
                          {"quota": QuotaEvidence(time.time(), 1, True)}):
            with self.assertRaises(SubscriptionUnavailable):
                analyze("context", **(self.options() | overrides))
        run.assert_not_called()

    @patch("codex_subscription.subprocess.run")
    def test_api_login_never_runs_inference(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "Logged in using API key", "")
        with self.assertRaises(SubscriptionUnavailable):
            analyze("context", **self.options())
        self.assertEqual(run.call_count, 1)

    def test_success_scrubs_keys_and_requires_structured_terminal_result(self):
        calls = []
        def runner(command, **kwargs):
            calls.append(command)
            self.assertNotIn("OPENAI_API_KEY", kwargs["env"])
            self.assertNotIn("CODEX_API_KEY", kwargs["env"])
            if "login" in command:
                return subprocess.CompletedProcess(command, 0, "", "Logged in using ChatGPT")
            self.assertIn("--ignore-user-config", command)
            self.assertIn("read-only", command)
            self.assertIn('forced_login_method="chatgpt"', command)
            self.assertIn("shell_tool", command)
            Path(command[command.index("--output-last-message")+1]).write_text(json.dumps({"summary": "Source-backed result"}))
            return subprocess.CompletedProcess(command, 0, json.dumps({"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 4}}), "")
        with patch.dict("os.environ", {"OPENAI_API_KEY": "never-copy", "CODEX_API_KEY": "never-copy"}), patch("codex_subscription.subprocess.run", side_effect=runner):
            result = analyze("context", **self.options())
        self.assertEqual(result["usage"]["output_tokens"], 4)
        self.assertEqual(len(calls), 2)
        self.assertFalse(result["automaticRetry"])

    @patch("codex_subscription.subprocess.run")
    def test_failed_run_is_not_retried(self, run):
        run.side_effect = [subprocess.CompletedProcess([], 0, "Logged in using ChatGPT", ""),
                           subprocess.CompletedProcess([], 1, "", "private failure")]
        with self.assertRaisesRegex(SubscriptionUnavailable, "no retry"):
            analyze("context", **self.options())
        self.assertEqual(run.call_count, 2)

    @patch("codex_subscription.subprocess.run")
    def test_timeout_is_terminal_without_second_attempt(self, run):
        run.side_effect = [subprocess.CompletedProcess([], 0, "Logged in using ChatGPT", ""),
                           subprocess.TimeoutExpired("codex", 1)]
        with self.assertRaises(subprocess.TimeoutExpired):
            analyze("context", **self.options())
        self.assertEqual(run.call_count, 2)

    def test_unexpected_tool_attempt_is_not_accepted_as_analysis(self):
        def runner(command, **kwargs):
            if "login" in command:
                return subprocess.CompletedProcess(command, 0, "Logged in using ChatGPT", "")
            Path(command[command.index("--output-last-message")+1]).write_text('{"summary":"Do not accept"}')
            events = [{"type": "item.completed", "item": {"type": "command_execution"}},
                      {"type": "turn.completed", "usage": {}}]
            return subprocess.CompletedProcess(command, 0, "\n".join(map(json.dumps, events)), "")
        with patch("codex_subscription.subprocess.run", side_effect=runner):
            with self.assertRaisesRegex(SubscriptionUnavailable, "tool action"):
                analyze("context", **self.options())

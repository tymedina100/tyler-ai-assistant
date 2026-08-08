import importlib
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import company_mode


class FakeCollection:
    def count(self):
        return 0

    def add(self, **kwargs):
        return None

    def query(self, **kwargs):
        return {"documents": [[]], "distances": [[]]}


class FakeChromaClient:
    def __init__(self, path):
        self.path = path

    def get_or_create_collection(self, name):
        return FakeCollection()


class FakeUsage:
    """Stand-in for an OpenAI usage object with arbitrary token-count attributes."""
    def __init__(self, **fields):
        self.__dict__.update(fields)


def import_main_with_stubs():
    fake_chromadb = types.SimpleNamespace(PersistentClient=FakeChromaClient)
    fake_openai = types.SimpleNamespace(OpenAI=lambda: object())
    fake_tavily = types.SimpleNamespace(TavilyClient=lambda: object())
    fake_dotenv = types.SimpleNamespace(load_dotenv=lambda: None)
    fake_requests = types.SimpleNamespace(get=lambda *args, **kwargs: None, post=lambda *args, **kwargs: None)
    fake_date_parser = types.SimpleNamespace(parse=lambda value: value)
    fake_dateutil = types.SimpleNamespace(parser=fake_date_parser)
    stubs = {
        "chromadb": fake_chromadb,
        "dateutil": fake_dateutil,
        "dateutil.parser": fake_date_parser,
        "openai": fake_openai,
        "requests": fake_requests,
        "tavily": fake_tavily,
        "dotenv": fake_dotenv,
    }

    sys.modules.pop("main", None)
    with patch.dict(sys.modules, stubs):
        return importlib.import_module("main")


class HardeningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_stubs()

    def test_safe_file_path_rejects_traversal(self):
        self.assertIsNone(self.main.get_safe_file_path("../.env"))

    def test_group_dockerfile_packages_office_api_runtime_dependency(self):
        dockerfile = (ROOT / "Dockerfile.group").read_text(encoding="utf-8")
        self.assertIn("office_api.py", dockerfile)
        self.assertIn("office_metrics.py", dockerfile)

    def test_safe_file_path_strips_redundant_files_prefix(self):
        # An agent passing "files/pack.md" should hit the same file as "pack.md",
        # not files/files/pack.md.
        self.assertEqual(
            self.main.get_safe_file_path("files/pack.md"),
            self.main.get_safe_file_path("pack.md"),
        )
        self.assertEqual(
            self.main.get_safe_file_path("./pack.md"),
            self.main.get_safe_file_path("pack.md"),
        )
        # Traversal is still blocked after stripping.
        self.assertIsNone(self.main.get_safe_file_path("files/../../.env"))

    def test_write_file_refuses_oversized_content(self):
        content = "x" * (self.main.MAX_WRITE_FILE_CHARS + 1)
        result = self.main.write_file("oversized.txt", content)
        self.assertIn("Refused to write", result)
        self.assertFalse((self.main.FILES_DIR / "oversized.txt").exists())

    def test_read_limited_text_truncates_large_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
            f.write("x" * (self.main.MAX_READ_FILE_CHARS + 10))
            path = Path(f.name)

        try:
            content = self.main.read_limited_text(path)
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(content.count("x"), self.main.MAX_READ_FILE_CHARS)
        self.assertIn("File truncated", content)

    def test_missing_optional_integrations_return_friendly_messages(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIn("Todoist isn't configured", self.main.create_task("buy milk"))
            self.assertIn("Todoist isn't configured", self.main.list_tasks())
            self.assertIn("OpenWeatherMap isn't configured", self.main.get_weather("Phoenix"))

            with patch("builtins.print") as mocked_print:
                self.assertIsNone(self.main.search_web("latest news"))
                printed = " ".join(str(call.args[0]) for call in mocked_print.call_args_list)
                self.assertIn("TAVILY_API_KEY", printed)

    def test_tool_argument_redaction(self):
        redacted = self.main.redact_tool_arguments({
            "filename": "draft.txt",
            "content": "private file text",
            "subject": "private subject",
            "code": "print('secret')",
        })

        self.assertEqual(redacted["filename"], "draft.txt")
        self.assertNotIn("private file text", redacted["content"])
        self.assertNotIn("private subject", redacted["subject"])
        self.assertNotIn("secret", redacted["code"])

    def test_pending_actions_are_scoped_per_conversation(self):
        self.main.set_conversation("telegram:1:100")
        self.main.set_pending_action({"type": "write_file", "filename": "a.txt", "content": "a"})

        self.main.set_conversation("telegram:2:200")
        self.assertIsNone(self.main.get_pending_action())

        self.main.set_conversation("telegram:1:100")
        self.assertEqual(self.main.get_pending_action()["filename"], "a.txt")
        self.main.clear_pending_action()

    def test_pending_action_description_includes_company_context(self):
        description = self.main.describe_pending_action({
            "type": "send_email",
            "to": "customer@example.com",
            "company_context": "Project proj_123 / task task_456",
        })

        self.assertIn("email to customer@example.com", description)
        self.assertIn("proj_123", description)

    # --- v2: metering + company-execution gating ---

    def test_usage_to_usd_responses_shape(self):
        usage = FakeUsage(input_tokens=1000, output_tokens=1000)
        # PREMIUM_MODEL gpt-5.6-sol priced (0.005, 0.030) per 1k -> 0.005 + 0.030 = 0.035
        self.assertAlmostEqual(self.main.usage_to_usd(self.main.PREMIUM_MODEL, usage), 0.035, places=6)

    def test_usage_to_usd_embedding_shape(self):
        usage = FakeUsage(prompt_tokens=1000, total_tokens=1000)
        # EMBEDDING priced (0.00002, 0.0); output = total - input = 0
        self.assertAlmostEqual(
            self.main.usage_to_usd(self.main.EMBEDDING_MODEL_NAME, usage), 0.00002, places=6
        )

    def test_usage_to_usd_unknown_model_uses_default(self):
        usage = FakeUsage(input_tokens=1000, output_tokens=0)
        # DEFAULT_MODEL_PRICE input rate 0.01
        self.assertAlmostEqual(self.main.usage_to_usd("mystery-model", usage), 0.01, places=6)

    def test_usage_to_usd_none_and_bad_shape_are_zero_safe(self):
        self.assertEqual(self.main.usage_to_usd(self.main.PREMIUM_MODEL, None), 0.0)
        self.assertEqual(self.main.usage_to_usd(self.main.PREMIUM_MODEL, FakeUsage()), 0.0)

    def test_usage_to_usd_discounts_cached_input(self):
        # 800 of 1000 input tokens are cache hits, billed at 10% of the input rate.
        usage = FakeUsage(
            input_tokens=1000, output_tokens=1000,
            input_tokens_details=types.SimpleNamespace(cached_tokens=800),
        )
        # fresh 200 @ .005 + cached 800 @ .005*.10 + output 1000 @ .030
        #   = 0.001 + 0.0004 + 0.030 = 0.0314  (vs 0.035 charging cache at full rate)
        self.assertAlmostEqual(
            self.main.usage_to_usd(self.main.PREMIUM_MODEL, usage), 0.0314, places=6
        )

    def test_usage_to_usd_dict_shaped_cache_details(self):
        # Some SDK versions expose the details as a dict rather than an object.
        usage = FakeUsage(input_tokens=1000, output_tokens=0,
                          input_tokens_details={"cached_tokens": 1000})
        # all input cached: 1000 @ .005*.10 = 0.0005
        self.assertAlmostEqual(
            self.main.usage_to_usd(self.main.PREMIUM_MODEL, usage), 0.0005, places=6
        )

    def test_metered_usage_records_the_actual_active_team_agent(self):
        sink = {"cost_usd": 0.0, "usage_records": [], "active_agent": "research"}
        self.main.set_execution_sink(sink)
        try:
            self.main._accrue_cost(
                self.main.FAST_MODEL,
                FakeUsage(input_tokens=100, output_tokens=20),
            )
        finally:
            self.main.set_execution_sink(None)

        self.assertEqual(sink["usage_records"][0]["agent"], "research")

    def test_plan_company_goal_parses_and_falls_back(self):
        main = self.main

        class Resp:
            def __init__(self, text):
                self.output_text = text
                self.usage = None

        class Responses:
            def __init__(self, text):
                self.text = text

            def create(self, **kwargs):
                return Resp(self.text)

        class Client:
            def __init__(self, text):
                self.responses = Responses(text)

        good = '{"tasks": [{"owner": "research", "title": "Validate"}, {"owner": "write", "title": "Draft"}]}'
        with patch.object(main, "get_openai_client", lambda: Client(good)):
            plan = main.plan_company_goal("goal", ["research", "write", "code"])
        self.assertEqual(plan, [("research", "Validate"), ("write", "Draft")])

        # An owner not in the available set is dropped.
        mixed = '{"tasks": [{"owner": "gmail", "title": "Email"}, {"owner": "code", "title": "Build"}]}'
        with patch.object(main, "get_openai_client", lambda: Client(mixed)):
            plan = main.plan_company_goal("goal", ["code"])
        self.assertEqual(plan, [("code", "Build")])

        # Garbage output -> None so company_mode uses its default plan.
        with patch.object(main, "get_openai_client", lambda: Client("not json at all")):
            self.assertIsNone(main.plan_company_goal("goal", ["research"]))

    def test_recommend_next_move_returns_text_and_falls_back(self):
        main = self.main

        class Resp:
            def __init__(self, text):
                self.output_text = text
                self.usage = None

        class Responses:
            def __init__(self, text):
                self.text = text

            def create(self, **kwargs):
                return Resp(self.text)

        class Client:
            def __init__(self, text):
                self.responses = Responses(text)

        with patch.object(main, "get_openai_client", lambda: Client("Double down on the builder product. - Miles")):
            rec = main.recommend_next_move("some P&L")
        self.assertIn("Double down", rec)

        class Boom:
            def create(self, **kwargs):
                raise RuntimeError("boom")

        class BoomClient:
            responses = Boom()

        # Skip the retry back-off sleeps so the fallback test stays fast.
        with patch.object(main, "call_with_retries", lambda func, **kw: func()), \
                patch.object(main, "get_openai_client", lambda: BoomClient()):
            rec = main.recommend_next_move("some P&L")
        self.assertEqual(rec, main.NEXT_MOVE_FALLBACK)

    def test_run_with_tools_synthesizes_when_budget_exhausted(self):
        """When the tool loop runs out of iterations, it should make a final no-tools
        call and return that synthesized answer, not the canned failure message."""
        main = self.main
        calls = []

        class Item:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        class Resp:
            def __init__(self, output, output_text=""):
                self.output = output
                self.output_text = output_text
                self.usage = None

        class Responses:
            def create(self, **kwargs):
                calls.append(kwargs)
                if kwargs.get("tools"):
                    # Keep demanding a tool so the loop never breaks on its own.
                    return Resp([Item(
                        type="function_call", name="recall_memories",
                        arguments=json.dumps({"query": "x"}), call_id="c1",
                    )])
                # The final synthesis call omits tools -> return a real answer.
                return Resp([], output_text="Final synthesized answer.")

        class Client:
            responses = Responses()

        with patch.object(main, "get_openai_client", lambda: Client()):
            result = main.run_with_tools(
                "inst", [{"role": "user", "content": "hi"}],
                main.TOOLS, max_iterations=2, model=main.FAST_MODEL,
            )

        self.assertEqual(result, "Final synthesized answer.")
        self.assertEqual(len(calls), 3)          # 2 tool iterations + 1 synthesis
        self.assertNotIn("tools", calls[-1])     # synthesis call carried no tools

    def test_incomplete_no_function_response_gets_one_bounded_synthesis(self):
        main = self.main
        calls = []

        class InputTokens:
            @staticmethod
            def count(**kwargs):
                return types.SimpleNamespace(input_tokens=100)

        class Responses:
            input_tokens = InputTokens()

            @staticmethod
            def create(**kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    return types.SimpleNamespace(
                        output=[],
                        output_text="Partial answer that must not be accepted.",
                        status="incomplete",
                        incomplete_details=types.SimpleNamespace(
                            reason="max_output_tokens"
                        ),
                        usage=FakeUsage(input_tokens=100, output_tokens=10),
                    )
                return types.SimpleNamespace(
                    output=[], output_text="Complete synthesized answer.",
                    status="completed", incomplete_details=None,
                    usage=FakeUsage(input_tokens=100, output_tokens=10),
                )

        sink = {
            "cost_usd": 0.0, "usage_records": [], "artifacts": [],
            "budget_cap_usd": 1.0,
        }
        main.set_execution_sink(sink)
        try:
            with patch.object(
                main, "get_openai_client",
                return_value=types.SimpleNamespace(responses=Responses()),
            ):
                result = main.run_with_tools(
                    "bounded", [{"role": "user", "content": "inspect"}],
                    tools=[], max_iterations=1, model=main.FAST_MODEL,
                )
        finally:
            main.set_execution_sink(None)

        self.assertEqual(result, "Complete synthesized answer.")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("tools", calls[-1])
        self.assertIn("max_output_tokens", calls[-1])
        self.assertEqual(
            sink["response_completion_history"][0],
            {"status": "incomplete", "reason": "max_output_tokens"},
        )
        self.assertEqual(sink["last_response_status"], "completed")

    def test_blank_then_incomplete_synthesis_raises_for_strict_autonomy(self):
        main = self.main
        calls = []

        class InputTokens:
            @staticmethod
            def count(**kwargs):
                return types.SimpleNamespace(input_tokens=100)

        class Responses:
            input_tokens = InputTokens()

            @staticmethod
            def create(**kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    return types.SimpleNamespace(
                        output=[], output_text="", status="completed",
                        incomplete_details=None,
                        usage=FakeUsage(input_tokens=100, output_tokens=10),
                    )
                return types.SimpleNamespace(
                    output=[], output_text="", status="incomplete",
                    incomplete_details={"reason": "max_output_tokens"},
                    usage=FakeUsage(input_tokens=100, output_tokens=10),
                )

        sink = {
            "cost_usd": 0.0, "usage_records": [], "artifacts": [],
            "budget_cap_usd": 1.0,
        }
        main.set_execution_sink(sink)
        try:
            with patch.object(
                main, "get_openai_client",
                return_value=types.SimpleNamespace(responses=Responses()),
            ), self.assertRaises(main.IncompleteModelResponseError) as raised:
                main.run_with_tools(
                    "bounded", [{"role": "user", "content": "inspect"}],
                    tools=[], max_iterations=1, model=main.FAST_MODEL,
                )
        finally:
            main.set_execution_sink(None)

        self.assertEqual(len(calls), 2)
        self.assertIn("one bounded final synthesis attempt", str(raised.exception))
        self.assertEqual(sink["last_response_status"], "incomplete")
        self.assertEqual(sink["last_response_reason"], "max_output_tokens")
        self.assertEqual(company_mode.classify_failure(raised.exception), "technical")

    def test_incomplete_response_keeps_ordinary_api_failure_fallback(self):
        main = self.main

        class Responses:
            @staticmethod
            def create(**kwargs):
                return types.SimpleNamespace(
                    output=[], output_text="", status="incomplete",
                    incomplete_details={"reason": "max_output_tokens"}, usage=None,
                )

        with patch.object(
            main, "get_openai_client",
            return_value=types.SimpleNamespace(responses=Responses()),
        ):
            result = main.run_with_tools(
                "ordinary", [{"role": "user", "content": "inspect"}],
                tools=[], max_iterations=1, model=main.FAST_MODEL,
            )

        self.assertTrue(result.startswith("Sorry, something went wrong"))

    def test_strict_autonomy_truncates_each_tool_result_only_in_its_sink(self):
        main = self.main
        calls = []
        long_result = "evidence-" + ("x" * 1000)

        class InputTokens:
            @staticmethod
            def count(**kwargs):
                return types.SimpleNamespace(input_tokens=100)

        class Responses:
            input_tokens = InputTokens()

            @staticmethod
            def create(**kwargs):
                calls.append(kwargs)
                if len(calls) == 1:
                    call = types.SimpleNamespace(
                        type="function_call", name="safe_read",
                        arguments="{}", call_id="call-1",
                    )
                    return types.SimpleNamespace(
                        output=[call], output_text="", status="completed",
                        incomplete_details=None,
                        usage=FakeUsage(input_tokens=100, output_tokens=10),
                    )
                return types.SimpleNamespace(
                    output=[], output_text="bounded answer", status="completed",
                    incomplete_details=None,
                    usage=FakeUsage(input_tokens=100, output_tokens=10),
                )

        sink = {
            "cost_usd": 0.0, "usage_records": [], "artifacts": [],
            "budget_cap_usd": 1.0,
        }
        main.set_execution_sink(sink)
        try:
            with patch.dict(
                os.environ, {"AUTONOMY_MAX_TOOL_RESULT_CHARS": "256"}
            ), patch.object(
                main, "get_openai_client",
                return_value=types.SimpleNamespace(responses=Responses()),
            ), patch.object(main, "execute_tool", return_value=long_result):
                result = main.run_with_tools(
                    "bounded", [{"role": "user", "content": "inspect"}],
                    tools=[{"type": "function", "name": "safe_read"}],
                    max_iterations=2, model=main.FAST_MODEL,
                )
        finally:
            main.set_execution_sink(None)

        tool_output = next(
            item["output"] for item in calls[-1]["input"]
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        )
        self.assertEqual(result, "bounded answer")
        self.assertEqual(len(tool_output), 256)
        self.assertIn("tool result truncated", tool_output)
        self.assertEqual(sink["tool_result_truncation_count"], 1)
        with patch.dict(os.environ, {"AUTONOMY_MAX_TOOL_RESULT_CHARS": "256"}):
            self.assertEqual(main._bounded_autonomy_tool_result(long_result), long_result)

    def test_run_with_tools_rejects_unadvertised_function_call(self):
        main = self.main

        class Item:
            type = "function_call"
            name = "send_email"
            arguments = '{"to":"victim@example.com","subject":"x","body":"x"}'
            call_id = "call-1"

        class Response:
            usage = None

            def __init__(self, output, output_text=""):
                self.output = output
                self.output_text = output_text

        class Responses:
            def __init__(self):
                self.calls = 0
                self.inputs = []

            def create(self, **kwargs):
                self.inputs.append(kwargs["input"])
                self.calls += 1
                if self.calls == 1:
                    return Response([Item()])
                return Response([], "Continued safely without the tool.")

        responses = Responses()
        client = types.SimpleNamespace(responses=responses)
        with patch.object(main, "get_openai_client", return_value=client), patch.object(
            main, "execute_tool"
        ) as execute:
            result = main.run_with_tools(
                "read only", [{"role": "user", "content": "inspect"}],
                tools=[], max_iterations=2,
            )

        self.assertEqual(result, "Continued safely without the tool.")
        execute.assert_not_called()
        self.assertIn("Tool call denied", str(responses.inputs[-1]))

    def test_budgeted_tool_call_counts_input_and_caps_provider_output(self):
        main = self.main
        create_calls = []
        count_calls = []

        class InputTokens:
            @staticmethod
            def count(**kwargs):
                count_calls.append(kwargs)
                return types.SimpleNamespace(input_tokens=1000)

        class Responses:
            input_tokens = InputTokens()

            @staticmethod
            def create(**kwargs):
                create_calls.append(kwargs)
                return types.SimpleNamespace(
                    output=[], output_text="bounded answer",
                    usage=FakeUsage(input_tokens=1000, output_tokens=100),
                )

        sink = {
            "cost_usd": 0.0,
            "usage_records": [],
            "artifacts": [],
            "budget_cap_usd": 0.05,
        }
        main.set_execution_sink(sink)
        try:
            with patch.object(
                main, "get_openai_client",
                return_value=types.SimpleNamespace(responses=Responses()),
            ):
                result = main.run_with_tools(
                    "bounded", [{"role": "user", "content": "inspect"}],
                    tools=[], max_iterations=1, model=main.FAST_MODEL,
                )
        finally:
            main.set_execution_sink(None)

        self.assertEqual(result, "bounded answer")
        self.assertEqual(len(count_calls), 1)
        self.assertEqual(len(create_calls), 1)
        self.assertGreaterEqual(create_calls[0]["max_output_tokens"], 16)
        self.assertLessEqual(create_calls[0]["max_output_tokens"], 3000)
        self.assertEqual(count_calls[0]["tools"], [])

        with patch.dict(
            os.environ, {"AUTONOMY_MAX_OUTPUT_TOKENS_PER_CALL": ""}
        ):
            self.assertEqual(main._configured_budget_output_tokens(), 3000)

    def test_strict_responses_disable_sdk_retries_and_share_task_deadline(self):
        main = self.main
        option_calls = []

        class InputTokens:
            @staticmethod
            def count(**kwargs):
                return types.SimpleNamespace(input_tokens=100)

        class Responses:
            input_tokens = InputTokens()

            @staticmethod
            def create(**kwargs):
                return types.SimpleNamespace(
                    output=[], output_text="bounded answer",
                    usage=FakeUsage(input_tokens=100, output_tokens=10),
                )

        class Client:
            responses = Responses()

            def with_options(self, **kwargs):
                option_calls.append(kwargs)
                return self

        sink = {
            "cost_usd": 0.0, "usage_records": [], "artifacts": [],
            "budget_cap_usd": 0.05,
        }
        main.set_execution_sink(sink)
        try:
            with patch.dict(
                os.environ, {"AUTONOMY_TASK_TIMEOUT_SECONDS": "2"}
            ), patch.object(main, "get_openai_client", return_value=Client()):
                result = main.run_with_tools(
                    "bounded", [{"role": "user", "content": "inspect"}],
                    tools=[], max_iterations=1, model=main.FAST_MODEL,
                )
        finally:
            main.set_execution_sink(None)

        self.assertEqual(result, "bounded answer")
        self.assertGreaterEqual(len(option_calls), 2)  # input count + generation
        self.assertTrue(all(call["max_retries"] == 0 for call in option_calls))
        self.assertTrue(all(0 < call["timeout"] <= 2 for call in option_calls))
        self.assertLessEqual(option_calls[-1]["timeout"], option_calls[0]["timeout"])

    def test_run_with_tools_re_raises_an_expired_strict_deadline(self):
        main = self.main
        sink = {
            "cost_usd": 0.0,
            "usage_records": [],
            "artifacts": [],
            "budget_cap_usd": 0.05,
            "request_deadline_monotonic": time.monotonic() - 1,
        }
        client = types.SimpleNamespace(
            responses=types.SimpleNamespace(
                input_tokens=types.SimpleNamespace(
                    count=Mock(side_effect=AssertionError("count must not start"))
                ),
                create=Mock(side_effect=AssertionError("generation must not start")),
            )
        )

        main.set_execution_sink(sink)
        try:
            with patch.object(main, "get_openai_client", return_value=client), self.assertRaises(
                main.ExecutionDeadlineExceededError
            ):
                main.run_with_tools(
                    "bounded",
                    [{"role": "user", "content": "inspect"}],
                    tools=[],
                    max_iterations=1,
                    model=main.FAST_MODEL,
                )
        finally:
            main.set_execution_sink(None)

        self.assertTrue(sink["deadline_exceeded"])
        client.responses.input_tokens.count.assert_not_called()
        client.responses.create.assert_not_called()

    def test_one_missing_usage_response_consumes_hold_and_stops_tool_loop(self):
        main = self.main
        create_calls = []

        class InputTokens:
            @staticmethod
            def count(**kwargs):
                return types.SimpleNamespace(input_tokens=100)

        class Responses:
            input_tokens = InputTokens()

            @staticmethod
            def create(**kwargs):
                create_calls.append(kwargs)
                if len(create_calls) == 1:
                    call = types.SimpleNamespace(
                        type="function_call", name="safe_read",
                        arguments="{}", call_id="call-1",
                    )
                    return types.SimpleNamespace(
                        output=[call], output_text="",
                        usage=FakeUsage(input_tokens=100, output_tokens=10),
                    )
                return types.SimpleNamespace(
                    output=[], output_text="unmeasured answer", usage=None,
                )

        sink = {
            "cost_usd": 0.0, "usage_records": [], "artifacts": [],
            "budget_cap_usd": 0.05, "active_agent": "research",
        }
        main.set_execution_sink(sink)
        try:
            with patch.object(
                main, "get_openai_client",
                return_value=types.SimpleNamespace(responses=Responses()),
            ), patch.object(main, "execute_tool", return_value="evidence"), self.assertRaises(
                main.ExecutionBudgetExceededError
            ):
                main.run_with_tools(
                    "bounded", [{"role": "user", "content": "inspect"}],
                    tools=[{"type": "function", "name": "safe_read"}],
                    max_iterations=3, model=main.FAST_MODEL,
                )
        finally:
            main.set_execution_sink(None)

        self.assertEqual(len(create_calls), 2)
        self.assertEqual(sink["model_requests_started"], 2)
        self.assertEqual(sink["model_responses_with_usage"], 1)
        self.assertEqual(sink["unmeasured_model_calls"], 1)
        self.assertEqual(sink["cost_usd"], sink["budget_cap_usd"])
        self.assertEqual(sink["usage_records"][-1]["cost_kind"], "estimated_unmeasured")
        self.assertEqual(sink["usage_records"][-1]["agent"], "research")
        self.assertTrue(sink["budget_guard_blocked"])

    def test_budgeted_tool_call_fails_before_generation_when_input_does_not_fit(self):
        main = self.main
        generations = []

        class InputTokens:
            @staticmethod
            def count(**kwargs):
                return types.SimpleNamespace(input_tokens=100000)

        class Responses:
            input_tokens = InputTokens()

            @staticmethod
            def create(**kwargs):
                generations.append(kwargs)
                raise AssertionError("generation must not start")

        sink = {
            "cost_usd": 0.0,
            "usage_records": [],
            "artifacts": [],
            "budget_cap_usd": 0.001,
        }
        main.set_execution_sink(sink)
        try:
            with patch.object(
                main, "get_openai_client",
                return_value=types.SimpleNamespace(responses=Responses()),
            ), self.assertRaises(main.ExecutionBudgetExceededError):
                main.run_with_tools(
                    "too large", [{"role": "user", "content": "inspect"}],
                    tools=[], max_iterations=1, model=main.FAST_MODEL,
                )
        finally:
            main.set_execution_sink(None)

        self.assertEqual(generations, [])
        self.assertTrue(sink["budget_guard_blocked"])

    def test_later_tool_call_can_atomically_expand_its_strict_budget_envelope(self):
        main = self.main
        create_calls = []
        # Mirrors the production budget-audit failure: $0.504 initial cap,
        # $0.308994 already spent, then a 55,697-token gpt-5.6-sol request.
        counted_inputs = iter((100, 55697))
        top_ups = []

        class InputTokens:
            @staticmethod
            def count(**kwargs):
                return types.SimpleNamespace(input_tokens=next(counted_inputs))

        class Responses:
            input_tokens = InputTokens()

            @staticmethod
            def create(**kwargs):
                create_calls.append(kwargs)
                if len(create_calls) == 1:
                    call = types.SimpleNamespace(
                        type="function_call", name="safe_read", arguments="{}", call_id="call-1"
                    )
                    return types.SimpleNamespace(
                        output=[call], output_text="",
                        usage=FakeUsage(input_tokens=100, output_tokens=10),
                    )
                return types.SimpleNamespace(
                    output=[], output_text="bounded answer",
                    usage=FakeUsage(input_tokens=55697, output_tokens=20),
                )

        sink = {
            "cost_usd": 0.308994,
            "usage_records": [
                {"model": "gpt-5.6-sol", "cost_usd": 0.308994}
            ],
            "artifacts": [],
            "budget_cap_usd": 0.504,
        }

        def top_up(minimum_total, preferred_total):
            top_ups.append((minimum_total, preferred_total))
            return preferred_total

        sink["budget_top_up"] = top_up
        main.set_execution_sink(sink)
        try:
            with patch.object(
                main, "get_openai_client",
                return_value=types.SimpleNamespace(responses=Responses()),
            ), patch.object(main, "execute_tool", return_value="evidence"):
                result = main.run_with_tools(
                    "bounded", [{"role": "user", "content": "inspect"}],
                    tools=[{"type": "function", "name": "safe_read"}],
                    max_iterations=2, model="gpt-5.6-sol",
                )
        finally:
            main.set_execution_sink(None)

        self.assertEqual(result, "bounded answer")
        self.assertEqual(len(create_calls), 2)
        self.assertEqual(len(top_ups), 1)
        self.assertGreater(top_ups[0][1], top_ups[0][0])
        self.assertEqual(sink["budget_top_up_count"], 1)
        self.assertGreater(sink["budget_cap_usd"], 0.504)
        self.assertLess(sink["cost_usd"], sink["budget_cap_usd"])

    def test_denied_task_top_up_still_blocks_before_generation(self):
        main = self.main
        generations = []

        class InputTokens:
            @staticmethod
            def count(**kwargs):
                return types.SimpleNamespace(input_tokens=55697)

        class Responses:
            input_tokens = InputTokens()

            @staticmethod
            def create(**kwargs):
                generations.append(kwargs)
                raise AssertionError("generation must not start")

        sink = {
            "cost_usd": 0.308994,
            "usage_records": [],
            "artifacts": [],
            "budget_cap_usd": 0.504,
            "budget_top_up": lambda minimum, preferred: 0.504,
        }
        main.set_execution_sink(sink)
        try:
            with patch.object(
                main, "get_openai_client",
                return_value=types.SimpleNamespace(responses=Responses()),
            ), self.assertRaises(main.ExecutionBudgetExceededError):
                main.run_with_tools(
                    "bounded", [{"role": "user", "content": "inspect"}],
                    tools=[], max_iterations=1, model="gpt-5.6-sol",
                )
        finally:
            main.set_execution_sink(None)

        self.assertEqual(generations, [])
        self.assertTrue(sink["budget_guard_blocked"])
        self.assertIn("No safe ordinary-budget expansion", sink["budget_guard_reason"])

    def test_task_top_up_state_error_fails_closed_as_technical(self):
        main = self.main
        generations = []

        class InputTokens:
            @staticmethod
            def count(**kwargs):
                return types.SimpleNamespace(input_tokens=55697)

        class Responses:
            input_tokens = InputTokens()

            @staticmethod
            def create(**kwargs):
                generations.append(kwargs)
                raise AssertionError("generation must not start")

        def broken_top_up(minimum, preferred):
            raise OSError("state write failed")

        sink = {
            "cost_usd": 0.308994,
            "usage_records": [],
            "artifacts": [],
            "budget_cap_usd": 0.504,
            "budget_top_up": broken_top_up,
        }
        main.set_execution_sink(sink)
        try:
            with patch.object(
                main, "get_openai_client",
                return_value=types.SimpleNamespace(responses=Responses()),
            ), self.assertRaises(main.ExecutionReservationStateError) as raised:
                main.run_with_tools(
                    "bounded", [{"role": "user", "content": "inspect"}],
                    tools=[], max_iterations=1, model="gpt-5.6-sol",
                )
        finally:
            main.set_execution_sink(None)

        self.assertEqual(generations, [])
        self.assertTrue(sink["budget_guard_blocked"])
        self.assertEqual(sink["budget_top_up_error"], "OSError")
        self.assertEqual(company_mode.classify_failure(raised.exception), "technical")

    def test_controlled_ideation_uses_same_strict_output_envelope(self):
        main = self.main
        create_calls = []

        class InputTokens:
            @staticmethod
            def count(**kwargs):
                return types.SimpleNamespace(input_tokens=500)

        class Responses:
            input_tokens = InputTokens()

            @staticmethod
            def create(**kwargs):
                create_calls.append(kwargs)
                return types.SimpleNamespace(
                    output_text=json.dumps({"ideas": [{"idea": "Small experiment"}]}),
                    usage=FakeUsage(input_tokens=500, output_tokens=100),
                )

        sink = {
            "cost_usd": 0.0,
            "usage_records": [],
            "artifacts": [],
            "budget_cap_usd": 0.05,
        }
        main.set_execution_sink(sink)
        try:
            with patch.object(
                main, "get_openai_client",
                return_value=types.SimpleNamespace(responses=Responses()),
            ):
                ideas = main.generate_controlled_ideas(
                    "{}", limit=1, model=main.FAST_MODEL
                )
        finally:
            main.set_execution_sink(None)

        self.assertEqual(ideas[0]["idea"], "Small experiment")
        self.assertEqual(len(create_calls), 1)
        self.assertIn("max_output_tokens", create_calls[0])

    def test_measured_cost_overrun_is_recorded_and_stops_the_call(self):
        main = self.main

        class InputTokens:
            @staticmethod
            def count(**kwargs):
                return types.SimpleNamespace(input_tokens=0)

        class Responses:
            input_tokens = InputTokens()

            @staticmethod
            def create(**kwargs):
                return types.SimpleNamespace(
                    output=[], output_text="too expensive",
                    usage=FakeUsage(input_tokens=10000, output_tokens=10000),
                )

        sink = {
            "cost_usd": 0.0,
            "usage_records": [],
            "artifacts": [],
            "budget_cap_usd": 0.01,
        }
        main.set_execution_sink(sink)
        try:
            with patch.object(
                main, "get_openai_client",
                return_value=types.SimpleNamespace(responses=Responses()),
            ), self.assertRaises(main.ExecutionBudgetExceededError):
                main.run_with_tools(
                    "bounded", [{"role": "user", "content": "inspect"}],
                    tools=[], max_iterations=1, model=main.FAST_MODEL,
                )
        finally:
            main.set_execution_sink(None)

        self.assertGreater(sink["cost_usd"], sink["budget_cap_usd"])
        self.assertEqual(len(sink["usage_records"]), 1)
        self.assertTrue(sink["budget_guard_blocked"])

    def test_autonomous_model_call_can_exclude_conversation_memories(self):
        main = self.main
        with patch.object(main, "build_augmented_prompt") as augment, patch.object(
            main, "run_with_tools", return_value="safe result"
        ) as run:
            result = main.ask_ai(
                "structured roadmap task",
                record_history=False,
                allowed_tool_names=set(),
                include_memories=False,
            )
        self.assertEqual(result, "safe result")
        augment.assert_not_called()
        sent_input = run.call_args.args[1][0]["content"]
        self.assertIn("structured roadmap task", sent_input)
        self.assertNotIn("Relevant memories", sent_input)

    def test_company_execution_writes_file_directly_and_records_artifact(self):
        self.main.set_conversation("test:companywrite")
        sink = {"cost_usd": 0.0, "artifacts": [], "context": "Project p / task t"}
        self.main.set_execution_sink(sink)
        self.main.set_company_execution(True)
        try:
            result = self.main.execute_tool("write_file", {"filename": "v2_exec.txt", "content": "hi"})
            self.assertIn("Saved", result)
            self.assertTrue((self.main.FILES_DIR / "v2_exec.txt").exists())
            self.assertTrue(any("v2_exec.txt" in a for a in sink["artifacts"]))
        finally:
            (self.main.FILES_DIR / "v2_exec.txt").unlink(missing_ok=True)
            self.main.set_company_execution(False)
            self.main.set_execution_sink(None)

    def test_company_execution_still_stages_send_email_with_context(self):
        previous_mode = self.main.CONFIRMATION_MODE
        self.main.CONFIRMATION_MODE = "requires_confirmation"
        self.main.set_conversation("test:companyemail")
        self.main.set_execution_sink({"cost_usd": 0.0, "artifacts": [], "context": "Project p / task t"})
        self.main.set_company_execution(True)
        try:
            result = self.main.execute_tool(
                "send_email", {"to": "a@b.com", "subject": "Hi", "body": "x"}
            )
            self.assertIn("staged", result.lower())
            pending = self.main.get_pending_action()
            self.assertEqual(pending["type"], "send_email")
            self.assertEqual(pending["company_context"], "Project p / task t")
        finally:
            self.main.clear_pending_action()
            self.main.set_company_execution(False)
            self.main.set_execution_sink(None)
            self.main.CONFIRMATION_MODE = previous_mode

    # --- Vercel deploy: preview free, production gated ---

    def test_deploy_site_production_stages_for_confirm(self):
        previous_mode = self.main.CONFIRMATION_MODE
        self.main.CONFIRMATION_MODE = "requires_confirmation"
        self.main.set_conversation("test:deployprod")
        try:
            result = self.main.execute_tool(
                "deploy_site", {"project": "landing", "ref": "main", "target": "production"}
            )
            self.assertIn("staged", result.lower())
            pending = self.main.get_pending_action()
            self.assertEqual(pending["type"], "deploy")
            self.assertEqual(pending["project"], "landing")
            self.assertEqual(pending["target"], "production")
        finally:
            self.main.clear_pending_action()
            self.main.CONFIRMATION_MODE = previous_mode

    def test_deploy_site_preview_runs_without_staging(self):
        previous_mode = self.main.CONFIRMATION_MODE
        self.main.CONFIRMATION_MODE = "requires_confirmation"
        self.main.set_conversation("test:deploypreview")

        def fake_deploy(project, ref, target):
            return {"url": "https://x.vercel.app", "id": "d1", "readyState": "QUEUED",
                    "target": "preview"}, None

        try:
            with patch.object(self.main.deploy_helpers, "deploy", fake_deploy):
                result = self.main.execute_tool(
                    "deploy_site", {"project": "landing", "target": "preview"}
                )
            self.assertIn("x.vercel.app", result)
            self.assertIsNone(self.main.get_pending_action())  # preview never stages
        finally:
            self.main.clear_pending_action()
            self.main.CONFIRMATION_MODE = previous_mode

    def test_describe_pending_action_covers_deploy(self):
        desc = self.main.describe_pending_action(
            {"type": "deploy", "project": "landing", "ref": "main"}
        )
        self.assertIn("production deploy of landing", desc)

    # --- Railway writes are gated; reads are not ---

    def test_railway_set_var_stages_for_confirm(self):
        previous_mode = self.main.CONFIRMATION_MODE
        self.main.CONFIRMATION_MODE = "requires_confirmation"
        self.main.set_conversation("test:railwaysetvar")
        try:
            result = self.main.execute_tool("railway_set_var", {
                "project_id": "p1", "environment_id": "e1", "service_id": "s1",
                "name": "EXPO_PUBLIC_PLAID_ENABLED", "value": "false",
            })
            self.assertIn("staged", result.lower())
            pending = self.main.get_pending_action()
            self.assertEqual(pending["type"], "railway_set_var")
            self.assertEqual(pending["name"], "EXPO_PUBLIC_PLAID_ENABLED")
        finally:
            self.main.clear_pending_action()
            self.main.CONFIRMATION_MODE = previous_mode

    def test_railway_redeploy_stages_for_confirm(self):
        previous_mode = self.main.CONFIRMATION_MODE
        self.main.CONFIRMATION_MODE = "requires_confirmation"
        self.main.set_conversation("test:railwayredeploy")
        try:
            result = self.main.execute_tool(
                "railway_redeploy", {"service_id": "s1", "environment_id": "e1"})
            self.assertIn("staged", result.lower())
            self.assertEqual(self.main.get_pending_action()["type"], "railway_redeploy")
        finally:
            self.main.clear_pending_action()
            self.main.CONFIRMATION_MODE = previous_mode

    def test_railway_set_var_value_is_redacted_in_logs(self):
        redacted = self.main.redact_tool_arguments(
            {"name": "SECRET_KEY", "value": "s3cr3t-value"})
        self.assertEqual(redacted["name"], "SECRET_KEY")
        self.assertNotIn("s3cr3t-value", redacted["value"])

    def test_describe_pending_action_railway_hides_value(self):
        desc = self.main.describe_pending_action(
            {"type": "railway_set_var", "name": "SECRET_KEY", "value": "s3cr3t"})
        self.assertIn("SECRET_KEY", desc)
        self.assertNotIn("s3cr3t", desc)  # the value is never echoed

    def test_execute_tool_redacts_secret_from_stdout(self):
        # The stdout print goes to container/deploy logs, so a secret value must not
        # appear there either. Stage a railway_set_var (no network) and check the print.
        previous_mode = self.main.CONFIRMATION_MODE
        self.main.CONFIRMATION_MODE = "requires_confirmation"
        self.main.set_conversation("test:stdoutleak")
        with patch("builtins.print") as mocked_print:
            try:
                self.main.execute_tool("railway_set_var", {
                    "project_id": "p1", "environment_id": "e1",
                    "name": "SECRET_KEY", "value": "top-secret-value",
                })
            finally:
                self.main.clear_pending_action()
                self.main.CONFIRMATION_MODE = previous_mode
        printed = " ".join(str(c.args[0]) for c in mocked_print.call_args_list if c.args)
        self.assertNotIn("top-secret-value", printed)

    def test_railway_set_var_shared_scope_stages_with_none_service(self):
        previous_mode = self.main.CONFIRMATION_MODE
        self.main.CONFIRMATION_MODE = "requires_confirmation"
        self.main.set_conversation("test:railwayshared")
        try:
            result = self.main.execute_tool("railway_set_var", {
                "project_id": "p1", "environment_id": "e1",
                "name": "SHARED_FLAG", "value": "on",  # no service_id -> shared
            })
            self.assertIn("staged", result.lower())
            self.assertIsNone(self.main.get_pending_action()["service_id"])
        finally:
            self.main.clear_pending_action()
            self.main.CONFIRMATION_MODE = previous_mode


if __name__ == "__main__":
    unittest.main()

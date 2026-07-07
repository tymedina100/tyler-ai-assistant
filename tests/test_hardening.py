import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


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
        # PREMIUM_MODEL gpt-5.5 priced (0.005, 0.030) per 1k → 0.005 + 0.030 = 0.035
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


if __name__ == "__main__":
    unittest.main()

import importlib
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


if __name__ == "__main__":
    unittest.main()

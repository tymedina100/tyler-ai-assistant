import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

try:
    from test_hardening import import_main_with_stubs
except ImportError:
    from tests.test_hardening import import_main_with_stubs


class TodayCommandTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_stubs()

    def test_today_uses_command_center_helper_and_picks_one_first_action(self):
        issues = [
            {
                "identifier": "WOR-2",
                "title": "Worthlane later task",
                "url": "https://linear/WOR-2",
                "dueDate": "2026-07-06",
                "priority": 4,
                "priorityLabel": "Low",
                "updatedAt": "2026-07-06T10:00:00Z",
                "state": {"name": "Todo", "type": "unstarted"},
                "project": {"name": "Worthlane"},
                "labels": {"nodes": [{"name": "MVP"}]},
            },
            {
                "identifier": "VAN-46",
                "title": "Card Tracker watchlist persistence",
                "url": "https://linear/VAN-46",
                "dueDate": "2026-07-07",
                "priority": 1,
                "priorityLabel": "Low",
                "updatedAt": "2026-07-06T09:00:00Z",
                "state": {"name": "Todo", "type": "unstarted"},
                "project": {"name": "Card Tracker"},
                "labels": {"nodes": []},
            },
            {
                "identifier": "VO-1",
                "title": "Virtual Office parked cleanup",
                "url": "https://linear/VO-1",
                "dueDate": "2026-07-01",
                "priority": 4,
                "priorityLabel": "Urgent",
                "updatedAt": "2026-07-06T11:00:00Z",
                "state": {"name": "Todo", "type": "unstarted"},
                "project": {"name": "Virtual Office"},
                "labels": {"nodes": [{"name": "MVP"}]},
            },
        ]

        with patch.object(self.main.linear_helpers, "list_command_center_issues", return_value=(issues, None)) as helper:
            output = self.main.handle_today_command()

        helper.assert_called_once()
        self.assertIn("Current main project: Card Tracker", output)
        self.assertIn("FIRST: [VAN-46] Card Tracker watchlist persistence", output)
        self.assertIn("SECOND", output)
        self.assertNotIn("VO-1", output)  # only 1-2 tasks, and parked Virtual Office loses priority

    def test_today_command_is_wired_into_main_router(self):
        with patch.object(self.main, "handle_today_command", return_value="Today\nFIRST: do the thing"):
            buf = io.StringIO()
            with redirect_stdout(buf):
                handled = self.main.handle_command("/today")

        self.assertTrue(handled)
        self.assertIn("FIRST: do the thing", buf.getvalue())

    def test_done_issues_are_ignored(self):
        issues = [
            {
                "identifier": "VAN-1",
                "title": "Finished Card Tracker task",
                "url": "https://linear/VAN-1",
                "state": {"name": "Done", "type": "completed"},
                "project": {"name": "Card Tracker"},
                "labels": {"nodes": [{"name": "MVP"}]},
            },
            {
                "identifier": "ASSIST-1",
                "title": "Assistant task",
                "url": "https://linear/ASSIST-1",
                "state": {"name": "Todo", "type": "unstarted"},
                "project": {"name": "Tyler AI Assistant"},
                "labels": {"nodes": []},
            },
        ]
        with patch.object(self.main.linear_helpers, "list_command_center_issues", return_value=(issues, None)):
            output = self.main.handle_today_command()
        self.assertNotIn("VAN-1", output)
        self.assertIn("ASSIST-1", output)

    def test_group_bot_has_today_slash_intercept(self):
        group_bot = (ROOT / "group_bot.py").read_text(encoding="utf-8")
        self.assertIn('if lowered == "/today":', group_bot)
        self.assertIn("main.handle_today_command", group_bot)


if __name__ == "__main__":
    unittest.main()

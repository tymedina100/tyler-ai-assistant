import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tyleros_worker import format_briefing_date, format_today_briefing


class FormatTodayBriefingTests(unittest.TestCase):
    def test_formats_overdue_and_food_without_bodies(self):
        title, body = format_today_briefing(
            {
                "today": "2026-09-04",
                "overdue": [{"id": "1", "title": "Pay rent", "dueOn": "2026-09-01", "body": "secret"}],
                "dueToday": [],
                "upcoming": [{"id": "2", "title": "Call plumber", "dueOn": "2026-09-06"}],
                "needsTriage": [{"id": "3", "title": "Inbox capture"}],
                "expiringSoon": [
                    {
                        "id": "4",
                        "name": "Milk",
                        "location": "fridge",
                        "expiresOn": "2026-09-05",
                        "notes": "do not leak",
                    }
                ],
            }
        )

        self.assertEqual(title, "Today briefing — 4 Sep 2026")
        self.assertIn("## Overdue", body)
        self.assertIn("- Pay rent (due 2026-09-01)", body)
        self.assertIn("## Needs triage", body)
        self.assertIn("- Inbox capture", body)
        self.assertIn("## Next 7 days", body)
        self.assertIn("## Use soon", body)
        self.assertIn("- Milk (fridge, by 2026-09-05)", body)
        self.assertNotIn("secret", body)
        self.assertNotIn("do not leak", body)

    def test_empty_today_says_nothing_needs_you(self):
        title, body = format_today_briefing(
            {
                "today": "2026-09-04",
                "overdue": [],
                "dueToday": [],
                "upcoming": [],
                "needsTriage": [],
                "expiringSoon": [],
            }
        )
        self.assertEqual(title, "Today briefing — 4 Sep 2026")
        self.assertIn("Nothing needs you right now.", body)

    def test_briefing_date_drops_leading_zero(self):
        self.assertEqual(format_briefing_date("2026-09-04"), "4 Sep 2026")


if __name__ == "__main__":
    unittest.main()

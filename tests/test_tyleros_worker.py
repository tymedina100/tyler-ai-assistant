import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tyleros_worker import (
    format_briefing_date,
    format_today_briefing,
    identity_headers,
    today_has_material,
    work_and_tick_tokens,
)


EMPTY = {
    "today": "2026-09-04",
    "overdue": [],
    "dueToday": [],
    "upcoming": [],
    "needsTriage": [],
    "expiringSoon": [],
}


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

    def test_empty_today_is_not_material(self):
        self.assertFalse(today_has_material(EMPTY))
        title, body = format_today_briefing(EMPTY)
        self.assertEqual(title, "Today briefing — 4 Sep 2026")
        self.assertNotIn("Nothing needs you right now.", body)

    def test_titles_make_today_material(self):
        self.assertTrue(today_has_material({**EMPTY, "overdue": [{"title": "Pay rent"}]}))
        self.assertTrue(today_has_material({**EMPTY, "expiringSoon": [{"name": "Milk"}]}))
        self.assertFalse(today_has_material({**EMPTY, "overdue": [{"title": "  "}]}))

    def test_briefing_date_drops_leading_zero(self):
        self.assertEqual(format_briefing_date("2026-09-04"), "4 Sep 2026")


class CredentialIdentityTests(unittest.TestCase):
    def test_instance_credential_does_not_send_kind_and_cannot_tick(self):
        env = {"TYLEROS_RUNTIME_CREDENTIAL": "tylrt_" + "a" * 40}
        headers = identity_headers(env)
        self.assertEqual(headers["X-TylerOS-Role"], "miles")
        self.assertNotIn("X-TylerOS-Runtime-Kind", headers)
        work, tick = work_and_tick_tokens(env)
        self.assertEqual(work, env["TYLEROS_RUNTIME_CREDENTIAL"])
        self.assertEqual(tick, "")

    def test_system_token_still_sends_kind_and_ticks(self):
        env = {"RUNTIME_TOKEN": "system-token-value-that-is-long-enough"}
        headers = identity_headers(env)
        self.assertEqual(headers["X-TylerOS-Runtime-Kind"], "python")
        work, tick = work_and_tick_tokens(env)
        self.assertEqual(work, tick)
        self.assertEqual(work, env["RUNTIME_TOKEN"])

    def test_instance_credential_wins_work_token_over_system(self):
        env = {
            "TYLEROS_RUNTIME_CREDENTIAL": "tylrt_instance",
            "RUNTIME_TOKEN": "system-token-value-that-is-long-enough",
        }
        work, tick = work_and_tick_tokens(env)
        self.assertEqual(work, "tylrt_instance")
        self.assertEqual(tick, "system-token-value-that-is-long-enough")


if __name__ == "__main__":
    unittest.main()

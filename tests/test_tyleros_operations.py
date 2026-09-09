import unittest
from tyleros_worker import format_today_briefing, today_has_material


class OperationsBriefingTests(unittest.TestCase):
    def test_real_counts_render_beside_tasks(self):
        context = {"today": "2026-09-09", "dueToday": [{"title": "Take bins out"}],
                   "operations": {"savedNotes": 2, "pendingApprovals": 3, "failedJobs": 1}}
        _, body = format_today_briefing(context)
        self.assertIn("2 briefing notes saved", body)
        self.assertIn("3 proposals waiting", body)
        self.assertIn("1 job failed", body)
        self.assertIn("Take bins out", body)

    def test_operations_do_not_create_recursive_briefings(self):
        self.assertFalse(today_has_material({"operations": {"pendingApprovals": 4}}))

    def test_legacy_or_invalid_context_is_quiet(self):
        for value in [None, {}, {"savedNotes": True}, {"failedJobs": -1}, {"pendingApprovals": "inject prose"}]:
            _, body = format_today_briefing({"operations": value})
            self.assertEqual(body, "Today briefing")


class ConsumptionBriefingTests(unittest.TestCase):
    def test_yesterday_counts_render_without_inferred_nutrition_or_preferences(self):
        _, body = format_today_briefing({"consumptionYesterday": {"food": 3, "drink": 2, "description": "PRIVATE"}})
        self.assertIn("Food & drink logged yesterday", body)
        self.assertIn("3 food entries and 2 drink entries", body)
        self.assertNotIn("PRIVATE", body)

    def test_counts_alone_do_not_create_another_scheduled_briefing(self):
        self.assertFalse(today_has_material({"consumptionYesterday": {"food": 3, "drink": 2}}))

    def test_legacy_empty_and_invalid_counts_stay_quiet(self):
        for summary in [None, {}, {"food": 0, "drink": 0}, {"food": True, "drink": 1}, {"food": -1, "drink": 1}, {"food": "prose", "drink": 1}]:
            _, body = format_today_briefing({"consumptionYesterday": summary})
            self.assertEqual(body, "Today briefing")

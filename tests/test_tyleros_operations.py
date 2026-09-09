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

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import office_metrics
import office_state


class OfficeMetricsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "office_state.json"
        self.env = patch.dict(os.environ, {"DATA_DIR": self.temp.name}, clear=False)
        self.env.start()
        office_metrics._remote_cache.update(at=0.0, sales=None, linear=None)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_unconfigured_sources_degrade_gracefully(self):
        with patch.object(office_metrics.gumroad_helpers, "is_configured", return_value=False), \
                patch.object(office_metrics.linear_helpers, "is_configured", return_value=False):
            payload = office_metrics.get_metrics(force_refresh=True)
        self.assertEqual(payload["sales"], {"configured": False})
        self.assertEqual(payload["linear"], {"configured": False})
        self.assertIn("team", payload)

    def test_sales_totals_are_aggregated_across_products(self):
        products = [
            {"sales_count": 3, "sales_usd_cents": 2700},
            {"sales_count": 2, "sales_usd_cents": 5800},
        ]
        with patch.object(office_metrics.gumroad_helpers, "is_configured", return_value=True), \
                patch.object(office_metrics.gumroad_helpers, "list_products", return_value=(products, None)), \
                patch.object(office_metrics.linear_helpers, "is_configured", return_value=False):
            payload = office_metrics.get_metrics(force_refresh=True)
        self.assertEqual(payload["sales"]["sales_count"], 5)
        self.assertEqual(payload["sales"]["revenue_usd"], 85.0)
        self.assertEqual(payload["sales"]["products"], 2)

    def test_linear_counts_completed_today_and_in_progress(self):
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        issues = [
            {"state": {"type": "completed"}, "updatedAt": f"{today}T10:00:00.000Z"},
            {"state": {"type": "completed"}, "updatedAt": "2020-01-01T10:00:00.000Z"},
            {"state": {"type": "started"}, "updatedAt": f"{today}T09:00:00.000Z"},
            {"state": None, "updatedAt": f"{today}T09:00:00.000Z"},
        ]
        with patch.object(office_metrics.gumroad_helpers, "is_configured", return_value=False), \
                patch.object(office_metrics.linear_helpers, "is_configured", return_value=True), \
                patch.object(office_metrics.linear_helpers, "list_command_center_issues", return_value=(issues, None)):
            payload = office_metrics.get_metrics(force_refresh=True)
        self.assertEqual(payload["linear"]["completed_today"], 1)
        self.assertEqual(payload["linear"]["in_progress"], 1)

    def test_source_errors_become_short_strings_not_crashes(self):
        with patch.object(office_metrics.gumroad_helpers, "is_configured", return_value=True), \
                patch.object(office_metrics.gumroad_helpers, "list_products",
                             return_value=(None, "boom " * 100)), \
                patch.object(office_metrics.linear_helpers, "is_configured", return_value=False):
            payload = office_metrics.get_metrics(force_refresh=True)
        self.assertTrue(payload["sales"]["configured"])
        self.assertLessEqual(len(payload["sales"]["error"]), office_metrics.MAX_ERROR_CHARS)

    def test_team_metrics_reflect_roster_and_daily_counters(self):
        office_state.configure_agents({
            "manager": {"name": "Miles", "role": "Manager"},
            "code": {"name": "Patch", "role": "Engineering"},
        })
        office_state.set_agent_status("code", "thinking", "working")
        office_state.mark_message_received("hi")
        office_state.add_event("reply", "code", "done")
        with patch.object(office_metrics.gumroad_helpers, "is_configured", return_value=False), \
                patch.object(office_metrics.linear_helpers, "is_configured", return_value=False):
            team = office_metrics.get_metrics(force_refresh=True)["team"]
        self.assertEqual(team["team_size"], 2)
        self.assertEqual(team["active_now"], 1)
        self.assertEqual(team["messages_today"], 1)
        self.assertEqual(team["events_today"], 2)
        self.assertEqual(team["agent_events_today"], {"code": 1})

    def test_remote_sources_are_cached_between_calls(self):
        with patch.object(office_metrics.gumroad_helpers, "is_configured", return_value=True), \
                patch.object(office_metrics.gumroad_helpers, "list_products",
                             return_value=([], None)) as remote, \
                patch.object(office_metrics.linear_helpers, "is_configured", return_value=False):
            office_metrics.get_metrics(force_refresh=True)
            office_metrics.get_metrics()
            office_metrics.get_metrics()
        self.assertEqual(remote.call_count, 1)


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import office_state


class OfficeStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "office_state.json"
        self.env = patch.dict(os.environ, {"DATA_DIR": self.temp.name}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_missing_state_returns_empty_dashboard(self):
        state = office_state.get_state()
        self.assertEqual(state["agents"], {})
        self.assertEqual(state["events"], [])
        self.assertFalse(self.path.exists())

    def test_configure_agents_keeps_enabled_roster_metadata(self):
        state = office_state.configure_agents({
            "manager": {"name": "Miles", "role": "Manager"},
            "code": {"name": "Patch", "role": "Head of Engineering"},
        })
        self.assertEqual(set(state["agents"]), {"manager", "code"})
        self.assertEqual(state["agents"]["code"]["name"], "Patch")
        self.assertEqual(state["agents"]["code"]["status"], "idle")

    def test_status_and_preview_are_bounded_and_sanitized(self):
        office_state.configure_agents({"code": {"name": "Patch", "role": "Engineering"}})
        message = "Working\n" + ("x" * 300) + "\x00"
        state = office_state.set_agent_status("code", "thinking", message)
        agent = state["agents"]["code"]
        self.assertEqual(agent["status"], "thinking")
        self.assertNotIn("\n", agent["message"])
        self.assertNotIn("\x00", agent["message"])
        self.assertLessEqual(len(agent["message"]), office_state.MAX_PREVIEW_CHARS)

    def test_event_log_is_limited_to_the_most_recent_entries(self):
        for number in range(office_state.MAX_EVENTS + 4):
            office_state.add_event("reply", "code", f"event {number}")
        state = office_state.get_state()
        self.assertEqual(len(state["events"]), office_state.MAX_EVENTS)
        self.assertEqual(state["events"][0]["text"], f"event {office_state.MAX_EVENTS + 3}")

    def test_temporary_status_becomes_idle_after_expiry(self):
        now = datetime(2026, 7, 9, tzinfo=timezone.utc)
        office_state.configure_agents({"code": {"name": "Patch", "role": "Engineering"}})
        with patch.object(office_state, "_now", return_value=now):
            office_state.set_agent_status("code", "speaking", "Done", duration_seconds=5)
        with patch.object(office_state, "_now", return_value=now + timedelta(seconds=6)):
            state = office_state.get_state()
        self.assertEqual(state["agents"]["code"]["status"], "idle")
        self.assertIsNone(state["agents"]["code"]["message"])

    def test_malformed_state_recovers_without_crashing(self):
        self.path.write_text("not json", encoding="utf-8")
        state = office_state.get_state()
        self.assertEqual(state["agents"], {})
        event = office_state.mark_message_received("hello")
        self.assertEqual(event["kind"], "message")
        self.assertEqual(office_state.get_state()["events"][0]["text"], "hello")

    def test_invalid_status_is_rejected(self):
        with self.assertRaises(ValueError):
            office_state.set_agent_status("code", "busy")


if __name__ == "__main__":
    unittest.main()

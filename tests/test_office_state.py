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

    def test_agent_events_build_a_bounded_newest_first_history(self):
        office_state.configure_agents({"code": {"name": "Patch", "role": "Engineering"}})
        for number in range(office_state.MAX_AGENT_HISTORY + 3):
            office_state.add_event("reply", "code", f"event {number}")
        history = office_state.get_state()["agents"]["code"]["history"]
        self.assertEqual(len(history), office_state.MAX_AGENT_HISTORY)
        self.assertEqual(history[0]["text"], f"event {office_state.MAX_AGENT_HISTORY + 2}")
        self.assertEqual(history[-1]["text"], "event 3")

    def test_events_for_unknown_agents_do_not_create_roster_entries(self):
        office_state.configure_agents({"code": {"name": "Patch", "role": "Engineering"}})
        office_state.add_event("reply", "ghost", "not on the roster")
        office_state.mark_message_received("plain group message")
        state = office_state.get_state()
        self.assertEqual(set(state["agents"]), {"code"})
        self.assertEqual(state["agents"]["code"]["history"], [])

    def test_reconfigure_preserves_history_for_retained_agents_only(self):
        office_state.configure_agents({
            "code": {"name": "Patch", "role": "Engineering"},
            "write": {"name": "Quill", "role": "Writer"},
        })
        office_state.add_event("reply", "code", "shipped a fix")
        office_state.add_event("reply", "write", "drafted a post")
        state = office_state.configure_agents({
            "code": {"name": "Patch", "role": "Engineering"},
            "research": {"name": "Scout", "role": "Research"},
        })
        self.assertEqual(state["agents"]["code"]["history"][0]["text"], "shipped a fix")
        self.assertEqual(state["agents"]["research"]["history"], [])
        self.assertNotIn("write", state["agents"])

    def test_corrupted_history_is_sanitized_on_read(self):
        office_state.configure_agents({"code": {"name": "Patch", "role": "Engineering"}})
        raw = office_state._read_unlocked()
        raw["agents"]["code"]["history"] = "not a list"
        office_state._write_unlocked(raw)
        state = office_state.get_state()
        self.assertEqual(state["agents"]["code"]["history"], [])


if __name__ == "__main__":
    unittest.main()

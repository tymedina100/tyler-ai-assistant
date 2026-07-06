import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from test_hardening import import_main_with_stubs
except ImportError:  # running as a package module (python -m unittest tests.test_roster)
    from tests.test_hardening import import_main_with_stubs


MANIFEST_PATH = ROOT / "assets" / "telegram_profiles" / "profile_manifest.json"


class RosterConsistencyTests(unittest.TestCase):
    """Guards the wiring every roster change must keep in sync: SPECIALISTS,
    TOOLS, DELEGATION_TOOLS + their execute_tool branches, and the profile
    manifest. A rename/merge/new-hire that misses one of these fails here
    instead of at runtime."""

    @classmethod
    def setUpClass(cls):
        cls.main = import_main_with_stubs()

    def test_every_specialist_tool_exists(self):
        tool_names = {tool["name"] for tool in self.main.TOOLS}
        for key, profile in self.main.SPECIALISTS.items():
            for name in profile["tool_names"]:
                self.assertIn(name, tool_names, f"{key} lists unknown tool {name!r}")

    def test_retired_keys_gone_and_new_keys_present(self):
        keys = set(self.main.SPECIALISTS)
        self.assertTrue({"news", "tasks", "weather"}.isdisjoint(keys))
        self.assertTrue({"marketing", "editor", "finance"} <= keys)

    def test_every_delegation_tool_has_a_matching_branch(self):
        # execute_tool must have a branch for each delegation tool AND use the
        # same argument key its schema declares. A stubbed ask_specialist/ask_ai
        # returns a sentinel; anything else back means a missing branch
        # ("Unknown tool: ...") or an arg-key mismatch ("missing required argument").
        sentinel = "DELEGATION-OK"
        main = self.main
        with patch.object(main, "ask_specialist", lambda key, prompt, record_history=True: sentinel), \
                patch.object(main, "ask_ai", lambda prompt, record_history=True: sentinel), \
                patch.object(main, "on_delegation", None):
            for tool in main.DELEGATION_TOOLS:
                arg_key = tool["parameters"]["required"][0]
                result = main.execute_tool(tool["name"], {arg_key: "ping"})
                self.assertEqual(result, sentinel, f"{tool['name']} (arg {arg_key!r}) -> {result!r}")

    def test_manifest_keys_are_real_agents(self):
        entries = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        manifest_keys = {entry["key"] for entry in entries}
        self.assertTrue(
            manifest_keys <= set(self.main.SPECIALISTS) | {"manager"},
            f"manifest references unknown agents: {manifest_keys - set(self.main.SPECIALISTS) - {'manager'}}",
        )


if __name__ == "__main__":
    unittest.main()

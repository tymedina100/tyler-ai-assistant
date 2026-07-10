import unittest

import office_desktop


class OfficeDesktopLayoutTests(unittest.TestCase):
    def test_endpoint_normalizes_service_and_endpoint_urls(self):
        self.assertEqual(
            office_desktop.office_endpoint("https://office.example/"),
            "https://office.example/api/office-state",
        )
        self.assertEqual(
            office_desktop.office_endpoint("https://office.example/api/office-state"),
            "https://office.example/api/office-state",
        )

    def test_statuses_use_the_expected_dynamic_zones(self):
        self.assertEqual(office_desktop.scene_zone("research", "thinking"), "planning")
        self.assertEqual(office_desktop.scene_zone("code", "delegated"), "operations")
        self.assertEqual(office_desktop.scene_zone("write", "speaking"), "response")
        self.assertEqual(office_desktop.scene_zone("finance", "error"), "support")
        self.assertEqual(office_desktop.scene_zone("task", "idle"), "home")
        self.assertEqual(office_desktop.scene_zone("manager", "idle"), "operations")

    def test_known_agent_colors_and_home_positions_are_stable(self):
        self.assertEqual(office_desktop.agent_color("code"), "#3e8cff")
        self.assertEqual(office_desktop.home_position("code", 99), office_desktop.home_position("code", 0))
        self.assertNotEqual(office_desktop.agent_color("code"), office_desktop.agent_color("research"))

    def test_active_zone_positions_do_not_overlap(self):
        agents = [
            (f"agent-{number}", {"status": "thinking", "name": f"Agent {number}"})
            for number in range(5)
        ]
        placed = office_desktop.assign_scene_positions(agents)
        coordinates = {(item["x"], item["y"]) for item in placed}
        self.assertEqual(len(coordinates), len(placed))
        self.assertTrue(all(item["zone"] == "planning" for item in placed))

    def test_scene_assignment_is_deterministic_and_shortens_text(self):
        agents = [("research", {"status": "thinking", "name": "Scout"})]
        self.assertEqual(office_desktop.assign_scene_positions(agents), office_desktop.assign_scene_positions(agents))
        self.assertEqual(office_desktop._short("abcdef", 5), "ab...")


if __name__ == "__main__":
    unittest.main()

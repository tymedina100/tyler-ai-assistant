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

    def test_3d_coworker_asset_is_packaged_with_the_desktop_app(self):
        self.assertEqual(office_desktop.ROBOT_SPRITE_PATH.name, "coworker-3d.png")
        self.assertTrue(office_desktop.ROBOT_SPRITE_PATH.is_file())

    def test_3d_office_room_model_is_packaged_with_the_desktop_app(self):
        self.assertEqual(office_desktop.OFFICE_ROOM_PATH.name, "office-room.png")
        self.assertTrue(office_desktop.OFFICE_ROOM_PATH.is_file())

    def test_each_known_teammate_has_colored_3d_animation_frames(self):
        for key in office_desktop.AGENT_COLORS:
            for frame_name in office_desktop.SPRITE_FRAME_NAMES + office_desktop.WALK_FRAME_NAMES:
                self.assertTrue(office_desktop.robot_sprite_path(key, frame_name).is_file())

    def test_live_statuses_select_distinct_sprite_animation_sequences(self):
        self.assertEqual(office_desktop.sprite_frame_for_status("idle", 0), "idle")
        self.assertEqual(office_desktop.sprite_frame_for_status("idle", 3), "blink")
        self.assertEqual(office_desktop.sprite_frame_for_status("thinking", 0), "thinking")
        self.assertEqual(office_desktop.sprite_frame_for_status("speaking", 0), "speaking")
        self.assertEqual(office_desktop.sprite_frame_for_status("delegated", 2), "speaking")
        self.assertEqual(office_desktop.sprite_frame_for_status("error", 0), "blink")

    def test_walking_animation_alternates_feet(self):
        self.assertEqual(office_desktop.walking_sprite_frame(0), "walk-1")
        self.assertEqual(office_desktop.walking_sprite_frame(1), "walk-2")
        self.assertEqual(office_desktop.walking_sprite_frame(2), "walk-1")

    def test_walks_route_across_the_room_before_approaching_the_spot(self):
        self.assertEqual(office_desktop.walk_path((100, 200), (400, 500)), [(400, 200), (400, 500)])
        self.assertEqual(office_desktop.walk_path((100, 200), (100, 500)), [(100, 500)])
        self.assertEqual(office_desktop.walk_path((100, 200), (400, 200)), [(400, 200)])

    def test_agents_advance_at_a_constant_walking_speed(self):
        position, remaining, travelled = office_desktop.advance_along_path((0, 0), [(30, 0), (30, 40)], 9)
        self.assertEqual(position, (9.0, 0.0))
        self.assertEqual(remaining, [(30, 0), (30, 40)])
        self.assertEqual(travelled, 9)

    def test_agents_turn_corners_without_losing_pace(self):
        position, remaining, travelled = office_desktop.advance_along_path((27, 0), [(30, 0), (30, 40)], 9)
        self.assertEqual(position, (30.0, 6.0))
        self.assertEqual(remaining, [(30, 40)])
        self.assertEqual(travelled, 9)

    def test_agents_sit_at_their_desk_only_when_home_and_not_walking(self):
        self.assertTrue(office_desktop.seated_pose("home", False))
        self.assertFalse(office_desktop.seated_pose("home", True))
        self.assertFalse(office_desktop.seated_pose("planning", False))
        self.assertFalse(office_desktop.seated_pose("operations", False))


if __name__ == "__main__":
    unittest.main()

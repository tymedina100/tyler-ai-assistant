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

    def test_unobstructed_walks_go_straight_to_the_spot(self):
        self.assertEqual(office_desktop.walk_path((500, 500), (500, 600)), [(500, 600)])

    def test_walks_detour_around_the_operations_desk(self):
        start, target = (885, 355), (450, 232)
        self.assertFalse(office_desktop.path_is_clear(start, target))
        path = office_desktop.walk_path(start, target)
        self.assertGreater(len(path), 1)
        self.assertEqual(path[-1], target)
        desk_rects = office_desktop.SCENE_OBSTACLES[:3]
        anchor = start
        for waypoint in path:
            for step in range(1, 50):
                x = anchor[0] + (waypoint[0] - anchor[0]) * step / 50
                y = anchor[1] + (waypoint[1] - anchor[1]) * step / 50
                inside_desk = any(x1 <= x <= x2 and y1 <= y <= y2 for x1, y1, x2, y2 in desk_rects)
                self.assertFalse(inside_desk, f"walk crosses the operations desk at ({x:.0f}, {y:.0f})")
            anchor = waypoint

    def test_every_zone_is_reachable_from_every_workstation(self):
        for slot in office_desktop.HOME_SLOTS:
            for anchor in office_desktop.ZONE_ANCHORS.values():
                path = office_desktop.walk_path(slot, anchor)
                self.assertEqual(path[-1], anchor)

    def test_furniture_blocks_points_and_the_floor_stays_walkable(self):
        self.assertTrue(office_desktop.point_blocked(500, 320))  # operations desk
        self.assertTrue(office_desktop.point_blocked(5, 5))  # off the floor
        self.assertFalse(office_desktop.point_blocked(500, 500))  # open floor

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

    def test_two_delegates_pull_the_operations_group_into_a_huddle(self):
        agents = [
            ("manager", {"status": "delegated", "name": "Miles"}),
            ("marketing", {"status": "delegated", "name": "Mara"}),
            ("write", {"status": "delegated", "name": "Quill"}),
            ("code", {"status": "idle", "name": "Patch"}),
        ]
        placed = {item["key"]: item for item in office_desktop.assign_scene_positions(agents)}
        center_x, center_y = office_desktop.HUDDLE_CENTER
        for key in ("manager", "marketing", "write"):
            item = placed[key]
            distance = ((item["x"] - center_x) ** 2 + (item["y"] - center_y) ** 2) ** 0.5
            self.assertAlmostEqual(distance, office_desktop.HUDDLE_RADIUS, delta=0.2)
            self.assertIsNotNone(item["face"])
        positions = {(placed[key]["x"], placed[key]["y"]) for key in ("manager", "marketing", "write")}
        self.assertEqual(len(positions), 3)
        self.assertIsNone(placed["code"]["face"])

    def test_huddle_members_face_the_center_of_the_circle(self):
        import math
        x, y, face = office_desktop.huddle_position(0, 3)
        center_x, center_y = office_desktop.HUDDLE_CENTER
        expected = math.atan2(center_x - x, center_y - y)
        self.assertAlmostEqual(face, expected, places=2)

    def test_single_delegation_keeps_the_legacy_wedge_layout(self):
        agents = [
            ("manager", {"status": "delegated", "name": "Miles"}),
            ("code", {"status": "delegated", "name": "Patch"}),
        ]
        wedge = office_desktop.assign_scene_positions([agents[0]])
        self.assertEqual((wedge[0]["x"], wedge[0]["y"]), office_desktop.ZONE_ANCHORS["operations"])
        self.assertIsNone(wedge[0]["face"])

    def test_huddle_spots_stay_walkable_for_any_realistic_group_size(self):
        for total in range(2, 9):
            for slot in range(total):
                x, y, _ = office_desktop.huddle_position(slot, total)
                self.assertFalse(
                    office_desktop.point_blocked(x, y),
                    f"huddle slot {slot}/{total} at ({x}, {y}) is inside furniture",
                )
                path = office_desktop.walk_path((150, 170), (x, y))
                self.assertEqual(path[-1], (x, y))


if __name__ == "__main__":
    unittest.main()

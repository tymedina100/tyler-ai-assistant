"""Native isometric command-center client for the Railway-backed Virtual Office."""
import argparse
import json
import math
import os
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:  # Pillow lets the photographed room scale with the window; the app runs without it.
    from PIL import Image as PILImage
    from PIL import ImageTk as PILImageTk
except ImportError:
    PILImage = PILImageTk = None


POLL_MS = 1500
REQUEST_TIMEOUT_SECONDS = 8
STATUS_COLORS = {
    "idle": "#7a8799",
    "thinking": "#f7b844",
    "speaking": "#28b681",
    "delegated": "#5e7df6",
    "error": "#ef6461",
}
AGENT_COLORS = {
    "manager": "#ff9d4d", "code": "#3e8cff", "research": "#48b78a",
    "write": "#ad70e8", "task": "#ef7278", "marketing": "#f7ba3f",
    "editor": "#9a70d8", "finance": "#4bb3c5", "calendar": "#f07898",
    "gmail": "#ef8062", "linear": "#6174e8", "general": "#64748b",
    "sales": "#d4589e", "analytics": "#c0ca33",
}
HOME_SLOTS = [
    (150, 170), (300, 145), (700, 145), (850, 170),
    (120, 360), (885, 355), (170, 505), (315, 535),
    (680, 535), (835, 505), (440, 550), (560, 550),
    (65, 470), (925, 470),
]
ZONE_ANCHORS = {
    "planning": (500, 210), "operations": (500, 392), "response": (745, 432),
    "support": (185, 280),
}
ZONE_OFFSETS = [(0, 0), (-50, 22), (50, 22), (-78, 46), (78, 46), (0, 55)]
# When Miles delegates to 2+ teammates the operations group meets in a circle on
# the open rug south of the desk instead of the wedge above.
HUDDLE_CENTER = (500, 487)
HUDDLE_RADIUS = 42
ROBOT_SPRITE_PATH = Path(__file__).resolve().parent / "assets" / "office" / "coworker-3d.png"
ROBOT_SPRITE_DIRECTORY = Path(__file__).resolve().parent / "assets" / "office" / "sprites"
OFFICE_ROOM_PATH = Path(__file__).resolve().parent / "assets" / "office" / "office-room.png"
SPRITE_FRAME_NAMES = ("idle", "blink", "thinking", "speaking")
WALK_FRAME_NAMES = ("walk-1", "walk-2")
WALK_SPEED = 9.0  # scene pixels moved per 32 ms animation frame
STRIDE_LENGTH = 32.0  # scene pixels travelled before the feet swap
SCENE_BOUNDS = (35, 115, 965, 640)  # walkable floor in the 1000x650 scene space
OBSTACLE_MARGIN = 14
GRID_CELL = 25


def _scene_obstacles():
    """Furniture footprints in scene coordinates that agents must walk around."""
    obstacles = [
        (300, 291, 700, 345),  # operations desk, back run
        (300, 291, 460, 441),  # operations desk, left wing
        (555, 291, 700, 441),  # operations desk, right wing
        (50, 130, 205, 240),   # secure tools cabinet
        (20, 545, 150, 645),   # lounge sofas
        (730, 495, 850, 645),  # response podium console
    ]
    for slot_x, slot_y in HOME_SLOTS:
        obstacles.append((slot_x - 44, slot_y + 2, slot_x + 44, slot_y + 52))
    return tuple(obstacles)


SCENE_OBSTACLES = _scene_obstacles()


def point_blocked(x, y, margin=OBSTACLE_MARGIN):
    """True when a scene point is off the floor or inside a piece of furniture."""
    if not (SCENE_BOUNDS[0] <= x <= SCENE_BOUNDS[2] and SCENE_BOUNDS[1] <= y <= SCENE_BOUNDS[3]):
        return True
    return any(
        x1 - margin <= x <= x2 + margin and y1 - margin <= y <= y2 + margin
        for x1, y1, x2, y2 in SCENE_OBSTACLES
    )


def path_is_clear(start, end):
    """True when the straight segment between two points stays off the furniture."""
    distance = math.hypot(end[0] - start[0], end[1] - start[1])
    steps = max(int(distance / 8), 1)
    return all(
        not point_blocked(start[0] + (end[0] - start[0]) * step / steps,
                          start[1] + (end[1] - start[1]) * step / steps)
        for step in range(1, steps)
    )


def _cell_center(cell):
    return (
        SCENE_BOUNDS[0] + (cell[0] + 0.5) * GRID_CELL,
        SCENE_BOUNDS[1] + (cell[1] + 0.5) * GRID_CELL,
    )


_GRID_COLUMNS = int((SCENE_BOUNDS[2] - SCENE_BOUNDS[0]) // GRID_CELL)
_GRID_ROWS = int((SCENE_BOUNDS[3] - SCENE_BOUNDS[1]) // GRID_CELL)
_WALKABLE_CELLS = tuple(
    cell
    for cell in ((column, row) for row in range(_GRID_ROWS) for column in range(_GRID_COLUMNS))
    if not point_blocked(*_cell_center(cell))
)
_WALKABLE_SET = set(_WALKABLE_CELLS)


def _nearest_walkable_cell(point):
    return min(
        _WALKABLE_CELLS,
        key=lambda cell: (point[0] - _cell_center(cell)[0]) ** 2 + (point[1] - _cell_center(cell)[1]) ** 2,
    )


def _grid_route(start_cell, goal_cell):
    """Breadth-first route across walkable floor cells; diagonals cannot clip corners."""
    if start_cell == goal_cell:
        return [start_cell]
    came_from = {start_cell: None}
    queue = [start_cell]
    while queue:
        cell = queue.pop(0)
        if cell == goal_cell:
            break
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            neighbor = (cell[0] + dx, cell[1] + dy)
            if neighbor in came_from or neighbor not in _WALKABLE_SET:
                continue
            if dx and dy and not ((cell[0] + dx, cell[1]) in _WALKABLE_SET and (cell[0], cell[1] + dy) in _WALKABLE_SET):
                continue
            came_from[neighbor] = cell
            queue.append(neighbor)
    if goal_cell not in came_from:
        return None
    route = [goal_cell]
    while came_from[route[-1]] is not None:
        route.append(came_from[route[-1]])
    route.reverse()
    return route
STATUS_ANIMATION_SEQUENCES = {
    "idle": ("idle", "idle", "idle", "blink", "idle", "idle"),
    "thinking": ("thinking", "thinking", "blink", "thinking"),
    "speaking": ("speaking", "speaking", "idle", "speaking"),
    "delegated": ("thinking", "thinking", "speaking", "thinking"),
    "error": ("blink", "idle"),
}


def office_endpoint(api_url):
    """Accept either a Railway service URL or the full state-endpoint URL."""
    cleaned = (api_url or "").strip().rstrip("/")
    return cleaned if cleaned.endswith("/api/office-state") else f"{cleaned}/api/office-state"


def fetch_office_state(api_url, token):
    request = Request(
        office_endpoint(api_url),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        method="GET",
    )
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def _short(value, limit=72):
    value = " ".join(str(value or "").split())
    return value if len(value) <= limit else value[:limit - 3].rstrip() + "..."


def agent_color(key):
    """Use stable, original teammate colors across polling cycles."""
    return AGENT_COLORS.get(key, "#64748b")


def robot_sprite_path(key, frame_name):
    """Return the packaged colored 3D sprite frame for one teammate.

    New hires without a rendered sprite set fall back to the neutral 'general'
    frames so the desktop app never draws a missing image."""
    path = ROBOT_SPRITE_DIRECTORY / f"{key}-{frame_name}.png"
    if path.is_file():
        return path
    return ROBOT_SPRITE_DIRECTORY / f"general-{frame_name}.png"


def sprite_frame_for_status(status, animation_step):
    """Choose a deterministic animation frame from the live office status."""
    sequence = STATUS_ANIMATION_SEQUENCES.get(status, STATUS_ANIMATION_SEQUENCES["idle"])
    return sequence[animation_step % len(sequence)]


def walking_sprite_frame(animation_step):
    """Alternate feet while an agent is travelling to a new office zone."""
    return WALK_FRAME_NAMES[animation_step % len(WALK_FRAME_NAMES)]


def walk_path(current, target):
    """Route a walk around the office furniture instead of through it."""
    if path_is_clear(current, target):
        return [target]
    route = _grid_route(_nearest_walkable_cell(current), _nearest_walkable_cell(target))
    if route is None:
        return [target]
    points = [_cell_center(cell) for cell in route] + [target]
    smoothed = []
    anchor = current
    index = 0
    while index < len(points):
        farthest = index
        for probe in range(len(points) - 1, index - 1, -1):
            if path_is_clear(anchor, points[probe]):
                farthest = probe
                break
        smoothed.append(points[farthest])
        anchor = points[farthest]
        index = farthest + 1
    return smoothed


def advance_along_path(position, path, step):
    """Move a constant distance through the waypoints so the walking pace never drifts."""
    x, y = position
    remaining = list(path)
    travelled = 0.0
    while remaining and step > 0:
        waypoint_x, waypoint_y = remaining[0]
        dx, dy = waypoint_x - x, waypoint_y - y
        distance = math.hypot(dx, dy)
        if distance <= step:
            x, y = waypoint_x, waypoint_y
            travelled += distance
            step -= distance
            remaining.pop(0)
        else:
            x += dx / distance * step
            y += dy / distance * step
            travelled += step
            step = 0
    return (x, y), remaining, travelled


def seated_pose(zone, walking):
    """Agents settle into their desk chair whenever they are at their workstation."""
    return zone == "home" and not walking


def scene_zone(key, status):
    """Map live work status to a readable command-center zone."""
    if status == "error":
        return "support"
    if key == "manager" or status == "delegated":
        return "operations"
    if status == "thinking":
        return "planning"
    if status == "speaking":
        return "response"
    return "home"


def home_position(key, index):
    """Give known and future agents deterministic perimeter workstations."""
    if key in AGENT_COLORS:
        known_index = list(AGENT_COLORS).index(key)
        return HOME_SLOTS[known_index % len(HOME_SLOTS)]
    return HOME_SLOTS[index % len(HOME_SLOTS)]


def huddle_position(slot, total):
    """Evenly spaced spot on the huddle circle, slot 0 nearest the desk."""
    angle = -math.pi / 2 + slot * 2 * math.pi / max(total, 1)
    x = HUDDLE_CENTER[0] + math.cos(angle) * HUDDLE_RADIUS
    y = HUDDLE_CENTER[1] + math.sin(angle) * HUDDLE_RADIUS
    face = math.atan2(HUDDLE_CENTER[0] - x, HUDDLE_CENTER[1] - y)
    return round(x, 1), round(y, 1), round(face, 3)


def assign_scene_positions(agents):
    """Return stable, non-overlapping room positions for the current API roster."""
    placed = []
    zone_counts = {"planning": 0, "operations": 0, "response": 0, "support": 0}
    entries = []
    for index, (key, agent) in enumerate(agents):
        status = agent.get("status", "idle")
        entries.append((index, key, agent, status, scene_zone(key, status)))
    delegate_total = sum(1 for entry in entries if entry[3] == "delegated")
    operations_total = sum(1 for entry in entries if entry[4] == "operations")
    huddling = delegate_total >= 2
    for index, key, agent, status, zone in entries:
        face = None
        if zone == "home":
            x, y = home_position(key, index)
        elif zone == "operations" and huddling:
            x, y, face = huddle_position(zone_counts[zone], operations_total)
            zone_counts[zone] += 1
        else:
            base_x, base_y = ZONE_ANCHORS[zone]
            offset_x, offset_y = ZONE_OFFSETS[zone_counts[zone] % len(ZONE_OFFSETS)]
            zone_counts[zone] += 1
            x, y = base_x + offset_x, base_y + offset_y
        placed.append({"key": key, "agent": agent, "status": status, "zone": zone, "x": x, "y": y, "face": face})
    return placed


class VirtualOfficeDesktop:
    def __init__(self, root, api_url, token):
        self.root = root
        self.api_url = api_url
        self.token = token
        self.state = {"agents": {}, "events": []}
        self.connection_note = "Connecting to Railway..."
        self.fetching = False
        self.closed = False
        self.current_positions = {}
        self.target_positions = {}
        self.paths = {}
        self.facing = {}
        self.walk_distance = {}
        self.motion_phase = 0.0
        self.transition_running = False
        self.robot_sprite_source = None
        self.robot_sprite = None
        self.robot_sprites = self._load_robot_sprites()
        self.room_background = self._load_room_background()
        self.room_source = self._load_room_source()
        self.scaled_room = None
        self.scaled_room_size = None
        sprite_assets = self._load_robot_sprite()
        if sprite_assets:
            self.robot_sprite_source, self.robot_sprite = sprite_assets

        root.title("Tyler AI Assistant - Virtual Office")
        root.geometry("1380x900")
        root.minsize(1040, 720)
        root.configure(bg="#edf0f5")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.canvas = tk.Canvas(root, bg="#edf0f5", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda _event: self.render())
        self.render()
        self.poll()
        self.root.after(120, self._motion_tick)

    def close(self):
        self.closed = True
        self.root.destroy()

    def poll(self):
        if self.closed:
            return
        if not self.fetching:
            self.fetching = True
            threading.Thread(target=self._fetch_in_background, name="office-desktop-poll", daemon=True).start()
        self.root.after(POLL_MS, self.poll)

    def _motion_tick(self):
        """Keep active coworkers gently alive without changing the polling contract."""
        if self.closed:
            return
        self.motion_phase += 0.45
        self.render()
        self.root.after(120, self._motion_tick)

    @staticmethod
    def _load_robot_sprite():
        """Load the original 3D coworker asset, with a graceful vector fallback."""
        if not ROBOT_SPRITE_PATH.exists():
            return None
        try:
            source = tk.PhotoImage(file=str(ROBOT_SPRITE_PATH))
            return source, source.subsample(11, 11)
        except tk.TclError:
            return None

    @staticmethod
    def _load_room_background():
        """Load the polished room model, retaining Canvas scenery as a resize fallback."""
        if not OFFICE_ROOM_PATH.exists():
            return None
        try:
            return tk.PhotoImage(file=str(OFFICE_ROOM_PATH))
        except tk.TclError:
            return None

    @staticmethod
    def _load_room_source():
        """Keep the raw room photo around so Pillow can rescale it to any window size."""
        if PILImage is None or not OFFICE_ROOM_PATH.exists():
            return None
        try:
            return PILImage.open(OFFICE_ROOM_PATH).convert("RGB")
        except OSError:
            return None

    def _room_background_for(self, width, height):
        """Return the room photo at the requested size, rescaling and caching as needed."""
        if self.room_background and (width, height) == (self.room_background.width(), self.room_background.height()):
            return self.room_background
        if self.room_source is None or width < 1 or height < 1:
            return None
        if self.scaled_room_size != (width, height):
            resized = self.room_source.resize((width, height), PILImage.BILINEAR)
            self.scaled_room = PILImageTk.PhotoImage(resized)
            self.scaled_room_size = (width, height)
        return self.scaled_room

    @staticmethod
    def _mirror_horizontally(image):
        """Build a left-facing copy of a walk frame; Tk flips with a negative subsample."""
        try:
            mirrored = tk.PhotoImage(width=image.width(), height=image.height())
            mirrored.tk.call(mirrored, "copy", image, "-subsample", -1, 1)
            return mirrored
        except tk.TclError:
            return None

    @classmethod
    def _load_robot_sprites(cls):
        """Load the colored animation frames; individual missing assets fall back safely."""
        sprites = {}
        for key in AGENT_COLORS:
            frames = {}
            for frame_name in SPRITE_FRAME_NAMES + WALK_FRAME_NAMES:
                path = robot_sprite_path(key, frame_name)
                if not path.exists():
                    continue
                try:
                    frames[frame_name] = tk.PhotoImage(file=str(path)).subsample(3, 3)
                except tk.TclError:
                    continue
            for frame_name in WALK_FRAME_NAMES:
                frame = frames.get(frame_name)
                if frame is not None:
                    mirrored = cls._mirror_horizontally(frame)
                    if mirrored is not None:
                        frames[f"{frame_name}-left"] = mirrored
            if frames:
                sprites[key] = frames
        return sprites

    def _robot_sprite_for(self, key, status, walking=False, facing=1, stride_step=0):
        frames = self.robot_sprites.get(key) or self.robot_sprites.get("general", {})
        if frames:
            if walking:
                frame_name = walking_sprite_frame(stride_step)
                if facing < 0 and f"{frame_name}-left" in frames:
                    frame_name = f"{frame_name}-left"
            else:
                frame_name = sprite_frame_for_status(status, int(self.motion_phase / 1.35))
            return frames.get(frame_name) or next(iter(frames.values()))
        return self.robot_sprite

    def _positioned_items(self, agents):
        targets = assign_scene_positions(agents)
        target_keys = set()
        for item in targets:
            key = item["key"]
            target_keys.add(key)
            target = (item["x"], item["y"])
            current = self.current_positions.get(key)
            if current is None:
                self.current_positions[key] = target
                self.paths[key] = []
            elif self.target_positions.get(key) != target:
                self.paths[key] = walk_path(current, target)
            self.target_positions[key] = target
        for attribute in ("current_positions", "target_positions", "paths", "facing", "walk_distance"):
            store = getattr(self, attribute)
            setattr(self, attribute, {key: value for key, value in store.items() if key in target_keys})
        if any(self.paths.values()) and not self.transition_running:
            self.transition_running = True
            self.root.after(32, self._animate_positions)
        placed = []
        for item in targets:
            key = item["key"]
            walking = bool(self.paths.get(key))
            placed.append({
                **item,
                "x": self.current_positions[key][0],
                "y": self.current_positions[key][1],
                "walking": walking,
                "seated": seated_pose(item["zone"], walking),
                "facing": self.facing.get(key, 1),
            })
        return placed

    def _animate_positions(self):
        if self.closed:
            return
        moving = False
        for key, path in self.paths.items():
            if not path:
                continue
            current = self.current_positions[key]
            heading = path[0][0] - current[0]
            if abs(heading) > 0.5:
                self.facing[key] = 1 if heading > 0 else -1
            position, remaining, travelled = advance_along_path(current, path, WALK_SPEED)
            self.current_positions[key] = position
            self.paths[key] = remaining
            self.walk_distance[key] = self.walk_distance.get(key, 0.0) + travelled
            if remaining:
                moving = True
        self.render()
        if moving:
            self.root.after(32, self._animate_positions)
        else:
            self.transition_running = False

    def _fetch_in_background(self):
        try:
            result = (fetch_office_state(self.api_url, self.token), "Connected to Railway")
        except HTTPError as error:
            result = (None, f"Railway API error: {error.code}")
        except (URLError, TimeoutError, ValueError, OSError):
            result = (None, "Reconnecting to Railway...")
        if not self.closed:
            self.root.after(0, lambda: self._apply_fetch_result(*result))

    def _apply_fetch_result(self, state, note):
        self.fetching = False
        if state is not None and isinstance(state, dict):
            self.state = state
        self.connection_note = note
        self.render()

    @staticmethod
    def _rounded(canvas, x1, y1, x2, y2, radius, fill, outline="", width=1):
        radius = min(radius, (x2 - x1) / 2, (y2 - y1) / 2)
        canvas.create_rectangle(x1 + radius, y1, x2 - radius, y2, fill=fill, outline="", width=0)
        canvas.create_rectangle(x1, y1 + radius, x2, y2 - radius, fill=fill, outline="", width=0)
        for x, y in ((x1 + radius, y1 + radius), (x2 - radius, y1 + radius), (x1 + radius, y2 - radius), (x2 - radius, y2 - radius)):
            canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=fill, outline="", width=0)
        if outline:
            canvas.create_line(x1 + radius, y1, x2 - radius, y1, fill=outline, width=width)
            canvas.create_line(x2, y1 + radius, x2, y2 - radius, fill=outline, width=width)
            canvas.create_line(x2 - radius, y2, x1 + radius, y2, fill=outline, width=width)
            canvas.create_line(x1, y2 - radius, x1, y1 + radius, fill=outline, width=width)
            canvas.create_arc(x1, y1, x1 + radius * 2, y1 + radius * 2, start=90, extent=90, style=tk.ARC, outline=outline, width=width)
            canvas.create_arc(x2 - radius * 2, y1, x2, y1 + radius * 2, start=0, extent=90, style=tk.ARC, outline=outline, width=width)
            canvas.create_arc(x2 - radius * 2, y2 - radius * 2, x2, y2, start=270, extent=90, style=tk.ARC, outline=outline, width=width)
            canvas.create_arc(x1, y2 - radius * 2, x1 + radius * 2, y2, start=180, extent=90, style=tk.ARC, outline=outline, width=width)

    def render(self):
        canvas = self.canvas
        width, height = max(canvas.winfo_width(), 1040), max(canvas.winfo_height(), 720)
        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill="#edf0f5", outline="")
        self._draw_header(width)
        console_height = max(190, int(height * 0.25))
        scene = (28, 92, width - 28, height - console_height - 18)
        self._draw_room(*scene)
        agents = list((self.state.get("agents") or {}).items())
        if agents:
            scene_items = self._positioned_items(agents)
            self._draw_agents(scene, scene_items)
        else:
            scene_items = []
            canvas.create_text(
                (scene[0] + scene[2]) / 2, (scene[1] + scene[3]) / 2,
                text="Waiting for the Railway team to enter the office.", fill="#64748b",
                font=("Segoe UI", 15, "bold"),
            )
        self._draw_foreground_labels(*scene)
        self._draw_console(28, height - console_height, width - 28, height - 22, agents)

    def _draw_header(self, width):
        canvas = self.canvas
        canvas.create_rectangle(0, 0, width, 68, fill="#f8fafc", outline="#dce3ec")
        canvas.create_text(30, 23, anchor="w", text="TYLER AI ASSISTANT", fill="#64748b", font=("Segoe UI", 9, "bold"))
        canvas.create_text(30, 47, anchor="w", text="Virtual Office", fill="#172033", font=("Segoe UI", 20, "bold"))
        connected = self.connection_note == "Connected to Railway"
        self._rounded(canvas, width - 236, 19, width - 30, 49, 15, "#eaf8f1" if connected else "#fff6df")
        canvas.create_oval(width - 220, 30, width - 210, 40, fill="#28b681" if connected else "#f7b844", outline="")
        canvas.create_text(width - 202, 35, anchor="w", text=self.connection_note, fill="#415066", font=("Segoe UI", 9, "bold"))

    def _draw_room(self, left, top, right, bottom):
        canvas = self.canvas
        room_width, room_height = int(right - left), int(bottom - top)
        background = self._room_background_for(room_width, room_height)
        if background:
            canvas.create_image(left, top, image=background, anchor="nw")
            canvas.create_rectangle(left, top, right, bottom, outline="#d1d9e3")
            return
        canvas.create_rectangle(left, top, right, bottom, fill="#f7f8fb", outline="#dce3ec")
        # Original room planes and isometric platform.
        canvas.create_polygon(left + 145, top + 28, right - 80, top + 105, right - 180, bottom - 26, left + 48, bottom - 86,
                              fill="#edf0f4", outline="#d5dce5")
        canvas.create_polygon(left + 145, top + 28, right - 80, top + 105, right - 80, top + 210, left + 145, top + 135,
                              fill="#ffffff", outline="#e4e8ee")
        canvas.create_polygon(left + 48, bottom - 86, left + 145, top + 28, left + 145, top + 135, left + 48, bottom - 10,
                              fill="#f2f4f7", outline="#dfe5ec")
        for offset in range(-220, 980, 58):
            canvas.create_line(left + offset, bottom - 76, left + offset + 310, top + 115, fill="#e0e6ed")
            canvas.create_line(left + offset + 60, bottom - 18, left + offset + 370, top + 57, fill="#edf0f4")
        self._draw_whiteboard(left + 320, top + 108, 215, 94)
        self._draw_kanban(right - 335, top + 150, 175, 135)
        self._draw_lounge(left + 114, bottom - 190)
        self._draw_secure_cabinet(left + 95, top + 160)
        self._draw_desk((left + right) / 2 - 120, bottom - 220, 240, 112)
        self._draw_podium(right - 250, bottom - 200)
        self._draw_plant(left + 230, bottom - 130)
        self._draw_plant(right - 128, top + 285)

    def _draw_foreground_labels(self, left, top, right, bottom):
        """Draw zone labels last so their text remains readable over live avatars."""
        self._zone_label((left + right) / 2 - 92, top + 55, "PLANNING WALL", "ideas and research", "#f7b844")
        self._zone_label((left + right) / 2 - 155, bottom - 52, "OPERATIONS DESK", "team coordination", "#5e7df6")
        self._zone_label(right - 160, bottom - 152, "RESPONSE PODIUM", "live updates", "#28b681")
        self._zone_label(left + 150, top + 250, "SECURE TOOLS", "protected actions", "#ef6461")

    def _zone_label(self, x, y, title, subtitle, accent):
        canvas = self.canvas
        self._rounded(canvas, x - 76, y - 20, x + 76, y + 20, 10, "#ffffff", "#e0e6ed")
        canvas.create_oval(x - 64, y - 8, x - 56, y, fill=accent, outline="")
        canvas.create_text(x - 50, y - 4, anchor="w", text=title, fill="#1f2a3d", font=("Segoe UI", 7, "bold"))
        canvas.create_text(x - 50, y + 8, anchor="w", text=subtitle, fill="#7a8799", font=("Segoe UI", 6))

    def _draw_whiteboard(self, x, y, width, height):
        canvas = self.canvas
        canvas.create_rectangle(x + 7, y + 8, x + width + 7, y + height + 8, fill="#cfd6df", outline="")
        canvas.create_rectangle(x, y, x + width, y + height, fill="#fbfcfd", outline="#bfc8d3", width=2)
        canvas.create_line(x + 30, y + 35, x + 170, y + 56, fill="#9ab5d6", width=2)
        canvas.create_line(x + 52, y + 70, x + 128, y + 34, fill="#d6a7a7", width=2)
        canvas.create_line(x + 160, y + 30, x + 190, y + 76, fill="#87be9d", width=2)
        canvas.create_line(x + 18, y + height, x + 8, y + height + 34, fill="#aeb8c4", width=4)
        canvas.create_line(x + width - 18, y + height, x + width - 8, y + height + 34, fill="#aeb8c4", width=4)

    def _draw_kanban(self, x, y, width, height):
        canvas = self.canvas
        canvas.create_rectangle(x, y, x + width, y + height, fill="#ffffff", outline="#cbd4df")
        for column, color in enumerate(("#f6cfcf", "#f8df9e", "#bfe6d1")):
            card_x = x + 14 + column * 52
            canvas.create_rectangle(card_x, y + 20, card_x + 38, y + 47, fill=color, outline="")
            canvas.create_rectangle(card_x, y + 57, card_x + 38, y + 85, fill="#d7e1f5", outline="")
            canvas.create_rectangle(card_x, y + 95, card_x + 38, y + 120, fill="#e8d4f5", outline="")
        canvas.create_text(x + 12, y + 10, anchor="w", text="TEAM BOARD", fill="#556276", font=("Segoe UI", 7, "bold"))

    def _draw_lounge(self, x, y):
        canvas = self.canvas
        canvas.create_polygon(x, y + 50, x + 65, y + 26, x + 140, y + 52, x + 76, y + 78, fill="#d9e0ea", outline="#aeb9c8")
        canvas.create_rectangle(x + 18, y + 25, x + 78, y + 57, fill="#f4f6f9", outline="#b5c0ce")
        canvas.create_rectangle(x + 72, y + 34, x + 128, y + 62, fill="#f4f6f9", outline="#b5c0ce")
        canvas.create_oval(x + 142, y + 40, x + 178, y + 60, fill="#d2d9e2", outline="#aeb9c8")
        canvas.create_text(x + 42, y + 92, text="LOUNGE", fill="#64748b", font=("Segoe UI", 7, "bold"))

    def _draw_secure_cabinet(self, x, y):
        canvas = self.canvas
        canvas.create_polygon(x, y + 25, x + 60, y, x + 60, y + 110, x, y + 132, fill="#aeb8c4", outline="#7b8797")
        canvas.create_polygon(x + 60, y, x + 93, y + 18, x + 93, y + 102, x + 60, y + 110, fill="#7e8a99", outline="#6c7785")
        canvas.create_oval(x + 20, y + 44, x + 55, y + 79, fill="#5a6574", outline="#404a57")
        canvas.create_oval(x + 29, y + 53, x + 46, y + 70, fill="#d9e0e8", outline="")

    def _draw_desk(self, x, y, width, height):
        canvas = self.canvas
        canvas.create_polygon(x, y + 26, x + width - 35, y, x + width, y + 20, x + 35, y + 49, fill="#8b5f45", outline="#654330")
        canvas.create_polygon(x + 35, y + 49, x + width, y + 20, x + width, y + height - 20, x + 35, y + height, fill="#704a38", outline="#5c3b2f")
        canvas.create_rectangle(x + 72, y - 28, x + 142, y + 18, fill="#2b3440", outline="#17202b")
        canvas.create_rectangle(x + 77, y - 23, x + 137, y + 10, fill="#9bd0e8", outline="")
        canvas.create_line(x + 107, y + 18, x + 107, y + 35, fill="#303b48", width=3)
        canvas.create_line(x + 91, y + 35, x + 123, y + 35, fill="#303b48", width=3)
        canvas.create_oval(x + 165, y + 12, x + 180, y + 26, fill="#f5f7fa", outline="#d2d9e2")

    def _draw_podium(self, x, y):
        canvas = self.canvas
        canvas.create_polygon(x, y + 25, x + 64, y, x + 94, y + 18, x + 28, y + 44, fill="#c2cad5", outline="#8f9baa")
        canvas.create_polygon(x + 28, y + 44, x + 94, y + 18, x + 94, y + 105, x + 28, y + 128, fill="#939fad", outline="#7c8998")
        canvas.create_rectangle(x + 38, y + 46, x + 74, y + 73, fill="#2d3846", outline="#536171")
        canvas.create_rectangle(x + 43, y + 51, x + 69, y + 67, fill="#69c7d6", outline="")

    def _draw_plant(self, x, y):
        canvas = self.canvas
        canvas.create_oval(x, y + 30, x + 28, y + 49, fill="#d7dde5", outline="#adb8c5")
        for dx, dy in ((6, 0), (15, -8), (22, 2), (10, -13)):
            canvas.create_oval(x + dx - 7, y + dy, x + dx + 7, y + dy + 22, fill="#6fb788", outline="#4d8e68")

    def _draw_agents(self, scene, placed):
        left, top, right, bottom = scene
        scale_x, scale_y = (right - left) / 1000, (bottom - top) / 650
        for index, item in enumerate(placed):
            if item.get("seated"):
                continue
            home_x, home_y = home_position(item["key"], index)
            self._draw_empty_workstation(left + home_x * scale_x, top + home_y * scale_y)
        for item in sorted(placed, key=lambda entry: entry["y"]):
            x, y = left + item["x"] * scale_x, top + item["y"] * scale_y
            self._draw_agent(x, y, item)

    def _draw_agent(self, x, y, item):
        canvas = self.canvas
        agent, status, key = item["agent"], item["status"], item["key"]
        walking, seated, facing = item.get("walking", False), item.get("seated", False), item.get("facing", 1)
        color = agent_color(key)
        status_color = STATUS_COLORS.get(status, STATUS_COLORS["idle"])
        active = status in {"thinking", "speaking", "delegated"}
        phase = self.motion_phase + sum(ord(char) for char in key) * 0.08
        stride_progress = self.walk_distance.get(key, 0.0) / STRIDE_LENGTH
        sprite_bob = 0.0
        if walking:
            y -= abs(math.sin(stride_progress * math.pi)) * 3.0
        elif seated:
            y += 12
            sprite_bob = math.sin(phase * 2.2) * 0.9
        else:
            y += math.sin(phase) * (3 if status == "speaking" else 2 if active else 0)
        pulse = 1.0 + (math.sin(phase) + 1) * 0.8 if active else 1.0
        if seated:
            self._draw_chair(x, y)
        elif walking:
            canvas.create_oval(x - 26, y + 30, x + 26, y + 42, fill="#c8d0da", outline="")
        else:
            canvas.create_oval(x - 42, y + 25, x + 42, y + 43, fill="#c8d0da", outline="")
            canvas.create_oval(x - 41, y - 60, x + 41, y + 31, fill=color, outline="")
        sprite = self._robot_sprite_for(key, status, walking=walking, facing=facing, stride_step=int(stride_progress))
        if sprite:
            canvas.create_image(x, y + 36 + sprite_bob, image=sprite, anchor="s")
        else:
            self._draw_vector_robot(x, y + sprite_bob, color, facing if walking else 1)
        if seated:
            self._draw_desk_front(x, y, phase)
        canvas.create_oval(x + 30 - pulse * 5, y - 61 - pulse * 5, x + 30 + pulse * 5, y - 61 + pulse * 5, fill=status_color, outline="#ffffff", width=2)
        self._rounded(canvas, x - 58, y + 49, x + 58, y + 78, 8, "#ffffff", "#dbe2eb")
        canvas.create_text(x, y + 58, text=_short(agent.get("name") or key, 17), fill="#263246", font=("Segoe UI", 8, "bold"))
        canvas.create_text(x, y + 69, text=status.upper(), fill=status_color, font=("Segoe UI", 7, "bold"))
        message = _short(agent.get("message"), 62)
        if message and not walking:
            self._rounded(canvas, x + 32, y - 112, x + 175, y - 77, 8, "#ffffff", "#d9e1ea")
            canvas.create_text(x + 41, y - 95, anchor="w", width=124, text=message, fill="#38465a", font=("Segoe UI", 7))

    def _draw_vector_robot(self, x, y, color, facing=1):
        canvas = self.canvas
        canvas.create_oval(x - 16, y - 32, x + 16, y, fill=color, outline="#344152", width=1)
        canvas.create_oval(x - 22, y - 4, x + 22, y + 34, fill=color, outline="#344152", width=1)
        visor_shift = 4 * facing
        canvas.create_rectangle(x - 10 + visor_shift, y - 21, x + 12 + visor_shift, y - 10, fill="#233141", outline="", width=0)

    def _draw_chair(self, x, y):
        canvas = self.canvas
        canvas.create_oval(x - 30, y + 22, x + 30, y + 44, fill="#c8d0da", outline="")
        self._rounded(canvas, x - 48, y - 58, x + 48, y + 26, 16, "#4b586c", "#39465a")
        canvas.create_rectangle(x - 4, y + 26, x + 4, y + 38, fill="#39465a", outline="")

    def _draw_desk_front(self, x, y, phase):
        """Occlude the seated agent's legs with their desk so they read as sitting and typing."""
        canvas = self.canvas
        top = y + 2
        canvas.create_polygon(x - 58, top + 9, x - 42, top, x + 42, top, x + 58, top + 9, fill="#8b5f45", outline="#654330")
        canvas.create_rectangle(x - 58, top + 9, x + 58, top + 44, fill="#704a38", outline="#5c3b2f")
        canvas.create_line(x - 58, top + 16, x + 58, top + 16, fill="#5c3b2f")
        canvas.create_polygon(x - 20, top + 1, x + 16, top + 1, x + 20, top - 21, x - 24, top - 21, fill="#2b3440", outline="#17202b")
        glow = "#d7f0fa" if int(phase * 2) % 4 == 0 else "#9bd0e8"
        canvas.create_rectangle(x - 22, top - 19, x + 18, top - 15, fill=glow, outline="")
        lift = 2 if int(phase * 3) % 2 else 0
        canvas.create_oval(x - 14, top - 3 - lift, x - 7, top + 4 - lift, fill="#e8edf3", outline="#b9c3d0")
        canvas.create_oval(x + 2, top - 1 + lift, x + 9, top + 6 + lift, fill="#e8edf3", outline="#b9c3d0")
        canvas.create_oval(x + 32, top + 2, x + 42, top + 9, fill="#f5f7fa", outline="#b9c3d0")

    def _draw_empty_workstation(self, x, y):
        """Keep every teammate's own desk visible while they are away from it."""
        canvas = self.canvas
        top = y + 14
        self._rounded(canvas, x - 40, y - 30, x + 40, top + 20, 14, "#d3dae4", "#b8c2cf")
        canvas.create_polygon(x - 58, top + 9, x - 42, top, x + 42, top, x + 58, top + 9, fill="#a17a5e", outline="#84604a")
        canvas.create_rectangle(x - 58, top + 9, x + 58, top + 40, fill="#8b6650", outline="#755442")
        canvas.create_polygon(x - 20, top + 1, x + 16, top + 1, x + 20, top - 21, x - 24, top - 21, fill="#3a4451", outline="#232c37")

    def _draw_console(self, left, top, right, bottom, agents):
        canvas = self.canvas
        self._rounded(canvas, left, top, right, bottom, 16, "#ffffff", "#d9e0e9")
        canvas.create_text(left + 18, top + 20, anchor="w", text="ACTIVITY CONSOLE", fill="#1f2a3d", font=("Segoe UI", 9, "bold"))
        canvas.create_text(left + 18, top + 43, anchor="w", text="EVENT LOG", fill="#2f72d6", font=("Segoe UI", 8, "bold"))
        canvas.create_line(left + 16, top + 53, left + 84, top + 53, fill="#2f72d6", width=2)
        divider = right - max(270, int((right - left) * 0.26))
        canvas.create_line(divider, top + 18, divider, bottom - 16, fill="#e2e7ee")
        events = (self.state.get("events") or [])[:5]
        y = top + 76
        if not events:
            canvas.create_text(left + 18, y, anchor="w", text="Waiting for Telegram activity...", fill="#8290a3", font=("Segoe UI", 9))
        for event in events:
            kind = event.get("kind", "activity")
            dot = "#ef6461" if kind == "error" else "#f7b844" if kind == "message" else "#28b681"
            canvas.create_oval(left + 20, y - 5, left + 27, y + 2, fill=dot, outline="")
            canvas.create_text(left + 35, y - 2, anchor="w", text=(event.get("agent_key") or "Telegram").title(), fill="#4b5a70", font=("Segoe UI", 8, "bold"))
            canvas.create_text(left + 114, y - 2, anchor="w", width=divider - left - 145, text=_short(event.get("text"), 92), fill="#64748b", font=("Segoe UI", 8))
            canvas.create_text(divider - 14, y - 2, anchor="e", text=self._event_time(event.get("timestamp")), fill="#98a4b4", font=("Segoe UI", 7))
            y += 22
        active = sum(1 for _key, agent in agents if agent.get("status") != "idle")
        canvas.create_text(divider + 22, top + 38, anchor="w", text="TEAM OVERVIEW", fill="#1f2a3d", font=("Segoe UI", 9, "bold"))
        self._overview_chip(divider + 22, top + 62, "TEAM", str(len(agents)), "#5e7df6")
        self._overview_chip(divider + 108, top + 62, "ACTIVE", str(active), "#28b681")
        self._overview_chip(divider + 194, top + 62, "EVENTS", str(len(self.state.get("events") or [])), "#f7b844")
        y = top + 110
        for item in assign_scene_positions(agents)[:4]:
            canvas.create_oval(divider + 25, y - 4, divider + 32, y + 3, fill=STATUS_COLORS.get(item["status"], "#7a8799"), outline="")
            canvas.create_text(divider + 42, y, anchor="w", text=f"{item['agent'].get('name', item['key'])} - {item['zone']}", fill="#64748b", font=("Segoe UI", 8))
            y += 20

    def _overview_chip(self, x, y, label, value, color):
        canvas = self.canvas
        self._rounded(canvas, x, y, x + 76, y + 35, 9, "#f7f9fc", "#e1e7ee")
        canvas.create_text(x + 10, y + 10, anchor="w", text=label, fill="#8290a3", font=("Segoe UI", 6, "bold"))
        canvas.create_text(x + 10, y + 24, anchor="w", text=value, fill=color, font=("Segoe UI", 11, "bold"))

    @staticmethod
    def _event_time(value):
        try:
            return datetime.fromisoformat(value).astimezone().strftime("%I:%M %p").lstrip("0")
        except (TypeError, ValueError, OSError):
            return "now"


def main():
    parser = argparse.ArgumentParser(description="Open the native Tyler AI Assistant Virtual Office desktop app.")
    parser.add_argument("--api-url", default=os.environ.get("OFFICE_API_URL", ""), help="Railway service URL or office-state endpoint")
    parser.add_argument("--token", default=os.environ.get("OFFICE_API_TOKEN", ""), help="Office API bearer token")
    args = parser.parse_args()
    if not args.api_url or not args.token:
        parser.error("Set OFFICE_API_URL and OFFICE_API_TOKEN, or provide --api-url and --token.")
    try:
        root = tk.Tk()
    except tk.TclError as error:
        print(f"Could not start the desktop UI: {error}")
        raise SystemExit(1)
    VirtualOfficeDesktop(root, args.api_url, args.token)
    root.mainloop()


if __name__ == "__main__":
    main()

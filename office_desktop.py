"""Native isometric command-center client for the Railway-backed Virtual Office."""
import argparse
import json
import os
import threading
import tkinter as tk
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


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
}
HOME_SLOTS = [
    (150, 170), (300, 145), (700, 145), (850, 170),
    (120, 360), (885, 355), (170, 505), (315, 535),
    (680, 535), (835, 505), (440, 550), (560, 550),
]
ZONE_ANCHORS = {
    "planning": (500, 210), "operations": (500, 392), "response": (745, 432),
    "support": (860, 260),
}
ZONE_OFFSETS = [(0, 0), (-50, 22), (50, 22), (-78, 46), (78, 46), (0, 55)]


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


def assign_scene_positions(agents):
    """Return stable, non-overlapping room positions for the current API roster."""
    placed = []
    zone_counts = {"planning": 0, "operations": 0, "response": 0, "support": 0}
    for index, (key, agent) in enumerate(agents):
        status = agent.get("status", "idle")
        zone = scene_zone(key, status)
        if zone == "home":
            x, y = home_position(key, index)
        else:
            base_x, base_y = ZONE_ANCHORS[zone]
            offset_x, offset_y = ZONE_OFFSETS[zone_counts[zone] % len(ZONE_OFFSETS)]
            zone_counts[zone] += 1
            x, y = base_x + offset_x, base_y + offset_y
        placed.append({"key": key, "agent": agent, "status": status, "zone": zone, "x": x, "y": y})
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
        canvas.create_rectangle(x1 + radius, y1, x2 - radius, y2, fill=fill, outline=outline, width=width)
        canvas.create_rectangle(x1, y1 + radius, x2, y2 - radius, fill=fill, outline=outline, width=width)
        for x, y in ((x1 + radius, y1 + radius), (x2 - radius, y1 + radius), (x1 + radius, y2 - radius), (x2 - radius, y2 - radius)):
            canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=fill, outline=outline, width=width)

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
            self._draw_agents(scene, assign_scene_positions(agents))
        else:
            canvas.create_text(
                (scene[0] + scene[2]) / 2, (scene[1] + scene[3]) / 2,
                text="Waiting for the Railway team to enter the office.", fill="#64748b",
                font=("Segoe UI", 15, "bold"),
            )
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
        self._zone_label(left + 210, top + 83, "PLANNING WALL", "ideas and research", "#f7b844")
        self._zone_label((left + right) / 2, bottom - 56, "OPERATIONS DESK", "team coordination", "#5e7df6")
        self._zone_label(right - 250, bottom - 96, "RESPONSE PODIUM", "live updates", "#28b681")
        self._draw_whiteboard(left + 320, top + 108, 215, 94)
        self._draw_kanban(right - 335, top + 150, 175, 135)
        self._draw_lounge(left + 114, bottom - 190)
        self._draw_secure_cabinet(left + 95, top + 160)
        self._draw_desk((left + right) / 2 - 120, bottom - 220, 240, 112)
        self._draw_podium(right - 250, bottom - 200)
        self._draw_plant(left + 230, bottom - 130)
        self._draw_plant(right - 128, top + 285)

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
        canvas.create_text(x + 4, y + 150, anchor="w", text="SECURE TOOLS", fill="#64748b", font=("Segoe UI", 7, "bold"))

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
        for item in placed:
            x, y = left + item["x"] * scale_x, top + item["y"] * scale_y
            self._draw_agent(x, y, item)

    def _draw_agent(self, x, y, item):
        canvas = self.canvas
        agent, status, key = item["agent"], item["status"], item["key"]
        color = agent_color(key)
        status_color = STATUS_COLORS.get(status, STATUS_COLORS["idle"])
        canvas.create_oval(x - 25, y + 28, x + 25, y + 43, fill="#c8d0da", outline="")
        canvas.create_oval(x - 16, y - 32, x + 16, y, fill=color, outline="#344152", width=1)
        canvas.create_oval(x - 22, y - 4, x + 22, y + 34, fill=color, outline="#344152", width=1)
        canvas.create_rectangle(x - 10, y - 21, x + 12, y - 10, fill="#233141", outline="", width=0)
        canvas.create_oval(x + 10, y - 38, x + 20, y - 28, fill=status_color, outline="#ffffff", width=2)
        self._rounded(canvas, x - 49, y + 46, x + 49, y + 68, 8, "#ffffff", "#dbe2eb")
        canvas.create_text(x, y + 53, text=_short(agent.get("name") or key, 15), fill="#263246", font=("Segoe UI", 7, "bold"))
        canvas.create_text(x, y + 62, text=status.upper(), fill=status_color, font=("Segoe UI", 6, "bold"))
        message = _short(agent.get("message"), 62)
        if message:
            self._rounded(canvas, x + 25, y - 72, x + 168, y - 37, 8, "#ffffff", "#d9e1ea")
            canvas.create_text(x + 34, y - 55, anchor="w", width=124, text=message, fill="#38465a", font=("Segoe UI", 7))

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

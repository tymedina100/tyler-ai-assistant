"""Native desktop client for the Railway-backed Tyler AI Assistant Virtual Office."""
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
    "idle": "#93aaa0",
    "thinking": "#ffd77b",
    "speaking": "#8ed6ab",
    "delegated": "#f7a971",
    "error": "#f67c73",
}
AVATAR_COLORS = ["#ffc76b", "#b895ff", "#75d7d0", "#fc9e9e", "#9ddea1", "#ff9c79", "#d9a5f4", "#7ab4ff"]


def office_endpoint(api_url):
    """Accept either a Railway service URL or the full state-endpoint URL."""
    cleaned = (api_url or "").strip().rstrip("/")
    if cleaned.endswith("/api/office-state"):
        return cleaned
    return f"{cleaned}/api/office-state"


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
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


class VirtualOfficeDesktop:
    def __init__(self, root, api_url, token):
        self.root = root
        self.api_url = api_url
        self.token = token
        self.state = {"agents": {}, "events": []}
        self.connection_note = "Connecting to Railway…"
        self.fetching = False
        self.closed = False

        root.title("Tyler AI Assistant — Virtual Office")
        root.geometry("1220x760")
        root.minsize(900, 600)
        root.configure(bg="#10252b")
        root.protocol("WM_DELETE_WINDOW", self.close)

        self.canvas = tk.Canvas(root, bg="#10252b", highlightthickness=0)
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
            state = fetch_office_state(self.api_url, self.token)
            result = (state, "Connected to Railway")
        except HTTPError as error:
            result = (None, f"Railway API error: {error.code}")
        except (URLError, TimeoutError, ValueError, OSError):
            result = (None, "Reconnecting to Railway…")
        if not self.closed:
            self.root.after(0, lambda: self._apply_fetch_result(*result))

    def _apply_fetch_result(self, state, note):
        self.fetching = False
        if state is not None and isinstance(state, dict):
            self.state = state
        self.connection_note = note
        self.render()

    def render(self):
        canvas = self.canvas
        width = max(canvas.winfo_width(), 900)
        height = max(canvas.winfo_height(), 600)
        canvas.delete("all")

        canvas.create_rectangle(0, 0, width, height, fill="#10252b", outline="")
        canvas.create_rectangle(0, 0, width, 76, fill="#17353a", outline="")
        canvas.create_text(28, 24, anchor="w", text="TYLER AI ASSISTANT", fill="#8ed6ab", font=("Segoe UI", 9, "bold"))
        canvas.create_text(28, 51, anchor="w", text="Virtual Office", fill="#eff7e8", font=("Segoe UI", 24, "bold"))
        connected = self.connection_note == "Connected to Railway"
        canvas.create_oval(width - 215, 30, width - 203, 42, fill="#8ed6ab" if connected else "#ffd77b", outline="")
        canvas.create_text(width - 194, 36, anchor="w", text=self.connection_note, fill="#abc0b8", font=("Segoe UI", 9))

        floor_left, floor_top = 24, 96
        sidebar_width = max(270, int(width * 0.28))
        floor_right = width - sidebar_width - 36
        floor_bottom = height - 22
        canvas.create_rectangle(floor_left, floor_top, floor_right, floor_bottom, fill="#285058", outline="#416c6b", width=1)
        for x in range(floor_left - 100, floor_right, 62):
            canvas.create_line(x, floor_top, x + 230, floor_bottom, fill="#315b60", width=1)
        for x in range(floor_left, floor_right + 100, 62):
            canvas.create_line(x, floor_top, x - 230, floor_bottom, fill="#234a50", width=1)
        canvas.create_text(floor_left + 18, floor_top + 18, anchor="w", text="TODAY'S FLOOR", fill="#d8f0dc", font=("Segoe UI", 10, "bold"))

        agents = list((self.state.get("agents") or {}).items())
        if agents:
            self._draw_agents(floor_left, floor_top, floor_right, floor_bottom, agents)
        else:
            canvas.create_text(
                (floor_left + floor_right) / 2,
                (floor_top + floor_bottom) / 2,
                text="Waiting for the Railway group bot to open the office.",
                fill="#d8f0dc",
                font=("Segoe UI", 13, "bold"),
            )

        self._draw_activity_panel(floor_right + 12, floor_top, width - 24, floor_bottom)

    def _draw_agents(self, left, top, right, bottom, agents):
        columns = 2 if right - left >= 500 else 1
        card_width = (right - left - 48 - (columns - 1) * 18) / columns
        card_height = 160
        for index, (key, agent) in enumerate(agents):
            column = index % columns
            row = index // columns
            x = left + 24 + column * (card_width + 18)
            y = top + 52 + row * (card_height + 20)
            if y + card_height > bottom - 10:
                break
            self._draw_desk(x, y, card_width, card_height, key, agent, index)

    def _draw_desk(self, x, y, width, height, key, agent, index):
        canvas = self.canvas
        status = agent.get("status", "idle")
        accent = AVATAR_COLORS[index % len(AVATAR_COLORS)]
        canvas.create_rectangle(x + 8, y + 10, x + width + 8, y + height + 10, fill="#17383d", outline="")
        canvas.create_rectangle(x, y, x + width, y + height, fill="#315f62", outline="#79a8a2", width=1)
        canvas.create_polygon(x + 20, y + 84, x + width - 18, y + 72, x + width - 35, y + 116, x + 4, y + 128, fill="#bc7950", outline="#8b503b")
        canvas.create_line(x + 20, y + 84, x + 4, y + 128, fill="#70402e", width=3)
        canvas.create_line(x + width - 18, y + 72, x + width - 35, y + 116, fill="#70402e", width=3)

        canvas.create_text(x + 14, y + 15, anchor="w", text=_short(agent.get("name") or key, 22), fill="#f4f7de", font=("Segoe UI", 11, "bold"))
        canvas.create_text(x + 14, y + 32, anchor="w", text=_short(agent.get("role"), 34), fill="#c6d6cd", font=("Segoe UI", 8))
        status_text = status.upper()
        canvas.create_oval(x + width - 73, y + 12, x + width - 64, y + 21, fill=STATUS_COLORS.get(status, "#93aaa0"), outline="")
        canvas.create_text(x + width - 60, y + 16, anchor="w", text=status_text, fill="#d8e5dc", font=("Segoe UI", 7, "bold"))

        avatar_x, avatar_y = x + width * 0.5, y + 112
        canvas.create_oval(avatar_x - 16, avatar_y - 37, avatar_x + 16, avatar_y - 5, fill=accent, outline="#173b3d", width=2)
        canvas.create_rectangle(avatar_x - 22, avatar_y - 8, avatar_x + 22, avatar_y + 34, fill=accent, outline="#173b3d", width=2)
        canvas.create_line(avatar_x - 25, avatar_y + 35, avatar_x - 31, avatar_y + 48, fill="#173b3d", width=4)
        canvas.create_line(avatar_x + 25, avatar_y + 35, avatar_x + 31, avatar_y + 48, fill="#173b3d", width=4)
        canvas.create_line(avatar_x - 10, avatar_y - 24, avatar_x + 10, avatar_y - 24, fill="#173b3d", width=3)

        message = _short(agent.get("message"), 58)
        if message:
            bubble_x = x + width - 12
            bubble_y = y + 51
            canvas.create_rectangle(bubble_x - 142, bubble_y, bubble_x, bubble_y + 38, fill="#f5f5d9", outline="#e2ddbd")
            canvas.create_text(bubble_x - 134, bubble_y + 19, anchor="w", width=124, text=message, fill="#173538", font=("Segoe UI", 7))

    def _draw_activity_panel(self, left, top, right, bottom):
        canvas = self.canvas
        canvas.create_rectangle(left, top, right, bottom, fill="#17353a", outline="#416c6b", width=1)
        canvas.create_text(left + 16, top + 18, anchor="w", text="LIVE FEED", fill="#8ed6ab", font=("Segoe UI", 8, "bold"))
        canvas.create_text(left + 16, top + 39, anchor="w", text="Office activity", fill="#eff7e8", font=("Segoe UI", 13, "bold"))
        events = (self.state.get("events") or [])[:12]
        if not events:
            canvas.create_text(left + 16, top + 78, anchor="w", width=right - left - 32, text="New Railway group activity will appear here.", fill="#abc0b8", font=("Segoe UI", 9))
            return
        y = top + 68
        for event in events:
            if y + 48 > bottom - 8:
                break
            kind = event.get("kind", "activity")
            color = "#f67c73" if kind == "error" else "#ffd77b" if kind == "message" else "#8ed6ab"
            canvas.create_oval(left + 15, y + 8, left + 21, y + 14, fill=color, outline="")
            label = (event.get("agent_key") or "Telegram").title()
            timestamp = self._event_time(event.get("timestamp"))
            canvas.create_text(left + 28, y + 10, anchor="w", text=label, fill="#dce8df", font=("Segoe UI", 8, "bold"))
            canvas.create_text(right - 12, y + 10, anchor="e", text=timestamp, fill="#8fa9a1", font=("Segoe UI", 7))
            canvas.create_text(left + 28, y + 29, anchor="w", width=right - left - 42, text=_short(event.get("text"), 72), fill="#b9ccc4", font=("Segoe UI", 8))
            y += 48

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

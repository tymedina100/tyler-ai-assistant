"""Small, file-backed state store for the Railway-backed Virtual Office desktop app.

The Telegram worker writes state while its authenticated API reads it. Writes use
``os.replace`` so readers see either the old complete JSON document or the new one,
never a partial file.
"""
import copy
import json
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


BASE_DIR = Path(__file__).resolve().parent
STATE_FILENAME = "office_state.json"
MAX_EVENTS = 30
MAX_AGENT_HISTORY = 8
MAX_COUNTED_AGENTS = 24
MAX_PREVIEW_CHARS = 180
VALID_STATUSES = {"idle", "thinking", "speaking", "delegated", "error"}

_lock = threading.RLock()


def _data_dir():
    """Use the same persistence convention as the rest of the assistant."""
    return Path(os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or BASE_DIR)


def state_file():
    return _data_dir() / STATE_FILENAME


def _now():
    return datetime.now(timezone.utc)


def _timestamp(value=None):
    return (value or _now()).isoformat()


def _today():
    return _now().date().isoformat()


def _empty_counters():
    """One small rolling day of activity totals for the KPI wall (UTC dates)."""
    return {"date": _today(), "messages": 0, "events": 0, "agent_events": {}}


def _clean_counters(raw):
    if not isinstance(raw, dict):
        return _empty_counters()
    agent_events = {}
    if isinstance(raw.get("agent_events"), dict):
        for key, value in list(raw["agent_events"].items())[:MAX_COUNTED_AGENTS]:
            if isinstance(value, int) and value >= 0:
                agent_events[str(key)] = value
    return {
        "date": raw.get("date") if isinstance(raw.get("date"), str) else _today(),
        "messages": raw.get("messages") if isinstance(raw.get("messages"), int) else 0,
        "events": raw.get("events") if isinstance(raw.get("events"), int) else 0,
        "agent_events": agent_events,
    }


def _empty_state():
    return {
        "version": 1,
        "updated_at": _timestamp(),
        "agents": {},
        "events": [],
        "counters": _empty_counters(),
    }


def _preview(text):
    """Collapse potentially noisy Telegram text into a safe, bounded UI preview."""
    clean = " ".join("".join("" if ord(char) < 32 else char for char in str(text or "")).split())
    if len(clean) <= MAX_PREVIEW_CHARS:
        return clean
    return clean[:MAX_PREVIEW_CHARS - 1].rstrip() + "…"


def _clean_history(history):
    """Keep per-agent history a small bounded list of event dicts (same privacy rules)."""
    if not isinstance(history, list):
        return []
    return [item for item in history if isinstance(item, dict)][:MAX_AGENT_HISTORY]


def _read_unlocked(path=None):
    path = Path(path or state_file())
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return _empty_state()

    if not isinstance(raw, dict):
        return _empty_state()

    state = _empty_state()
    state["updated_at"] = raw.get("updated_at", state["updated_at"])
    agents = raw.get("agents")
    events = raw.get("events")
    state["agents"] = (
        {str(key): value for key, value in agents.items() if isinstance(value, dict)}
        if isinstance(agents, dict) else {}
    )
    state["events"] = [event for event in events if isinstance(event, dict)][:MAX_EVENTS] if isinstance(events, list) else []
    state["counters"] = _clean_counters(raw.get("counters"))
    return state


def _write_unlocked(state, path=None):
    path = Path(path or state_file())
    path.parent.mkdir(parents=True, exist_ok=True)
    state["version"] = 1
    state["updated_at"] = _timestamp()

    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temp_name = handle.name
            json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            try:
                os.unlink(temp_name)
            except OSError:
                pass


def configure_agents(agents, path=None):
    """Replace the dashboard roster with the currently enabled Telegram agents.

    ``agents`` is a mapping of agent key to ``{"name": ..., "role": ...}``.
    Existing statuses survive a group-bot restart when the agent remains enabled.
    """
    with _lock:
        state = _read_unlocked(path)
        previous = state["agents"]
        roster = {}
        for key, profile in agents.items():
            if not isinstance(profile, dict):
                continue
            old = previous.get(key, {}) if isinstance(previous.get(key), dict) else {}
            roster[key] = {
                "name": _preview(profile.get("name") or key),
                "role": _preview(profile.get("role") or "Team member"),
                "status": old.get("status") if old.get("status") in VALID_STATUSES else "idle",
                "message": old.get("message") if isinstance(old.get("message"), str) else None,
                "updated_at": old.get("updated_at") or _timestamp(),
                "expires_at": old.get("expires_at") if isinstance(old.get("expires_at"), str) else None,
                "history": _clean_history(old.get("history")),
            }
        state["agents"] = roster
        _write_unlocked(state, path)
        return get_state(path)


def set_agent_status(key, status, message=None, duration_seconds=None, path=None):
    """Update one agent's visible state, optionally expiring it back to idle."""
    if status not in VALID_STATUSES:
        raise ValueError(f"Unsupported office status: {status}")

    with _lock:
        state = _read_unlocked(path)
        agent = state["agents"].get(key)
        if not isinstance(agent, dict):
            # The office is intentionally a view of enabled Telegram bots only. A
            # disabled specialist can still work through Miles, but does not get a
            # desk until it is added to BOT_KEYS and the roster is reconfigured.
            return get_state(path)
        agent["status"] = status
        agent["message"] = _preview(message) if message else None
        agent["updated_at"] = _timestamp()
        if duration_seconds is not None and duration_seconds > 0:
            agent["expires_at"] = _timestamp(_now() + timedelta(seconds=duration_seconds))
        else:
            agent["expires_at"] = None
        _write_unlocked(state, path)
        return get_state(path)


def add_event(kind, agent_key, text, path=None):
    """Prepend one concise activity item and retain only the newest events.

    Events attributed to a rostered agent are also mirrored into that agent's
    own bounded ``history`` list, newest first, so per-agent views stay cheap.
    """
    with _lock:
        state = _read_unlocked(path)
        event = {
            "id": uuid4().hex,
            "timestamp": _timestamp(),
            "kind": _preview(kind) or "activity",
            "agent_key": agent_key or None,
            "text": _preview(text),
        }
        state["events"] = [event, *state["events"]][:MAX_EVENTS]
        agent = state["agents"].get(agent_key) if agent_key else None
        if isinstance(agent, dict):
            agent["history"] = [event, *_clean_history(agent.get("history"))][:MAX_AGENT_HISTORY]
        counters = state["counters"]
        if counters["date"] != _today():
            counters = _empty_counters()
            state["counters"] = counters
        counters["events"] += 1
        if event["kind"] == "message":
            counters["messages"] += 1
        if agent_key and (agent_key in counters["agent_events"] or len(counters["agent_events"]) < MAX_COUNTED_AGENTS):
            counters["agent_events"][agent_key] = counters["agent_events"].get(agent_key, 0) + 1
        _write_unlocked(state, path)
        return event


def mark_message_received(text, path=None):
    """Record a user-originated Telegram group message without storing full text."""
    return add_event("message", None, text, path)


def _has_expired(agent, now):
    expires_at = agent.get("expires_at")
    if not expires_at:
        return False
    try:
        return datetime.fromisoformat(expires_at) <= now
    except (TypeError, ValueError):
        return True


def get_state(path=None):
    """Return a read-only snapshot, treating expired temporary states as idle."""
    with _lock:
        state = copy.deepcopy(_read_unlocked(path))

    now = _now()
    for agent in state["agents"].values():
        if not isinstance(agent, dict):
            continue
        if _has_expired(agent, now):
            agent["status"] = "idle"
            agent["message"] = None
            agent["expires_at"] = None
        elif agent.get("status") not in VALID_STATUSES:
            agent["status"] = "idle"
        agent["history"] = _clean_history(agent.get("history"))
    if state["counters"]["date"] != _today():
        # yesterday's totals never masquerade as today's
        state["counters"] = _empty_counters()
    return state

"""Server-side KPI aggregation for the office dashboard wall.

Same philosophy as ``office_state``: bounded, privacy-conscious, and safe to
serve to the token-gated viewer. Remote sources (Gumroad, Linear) are cached
for a few minutes so a polling dashboard never hammers external APIs, and every
source degrades to ``{"configured": False}`` or a short error string instead of
failing the whole payload.
"""
import threading
import time
from datetime import datetime, timezone

import gumroad_helpers
import linear_helpers
import office_state


REMOTE_CACHE_TTL_SECONDS = 300
MAX_ERROR_CHARS = 120

_lock = threading.Lock()
_remote_cache = {"at": 0.0, "sales": None, "linear": None}


def _short_error(message):
    text = " ".join(str(message or "").split())
    return text[:MAX_ERROR_CHARS]


def _sales_metrics():
    """Totals across all Gumroad products; the money the company has earned."""
    if not gumroad_helpers.is_configured():
        return {"configured": False}
    products, error = gumroad_helpers.list_products()
    if error:
        return {"configured": True, "error": _short_error(error)}
    return {
        "configured": True,
        "products": len(products),
        "sales_count": sum(item.get("sales_count", 0) for item in products),
        "revenue_usd": round(sum(item.get("sales_usd_cents", 0) for item in products) / 100, 2),
    }


def _linear_metrics():
    """Issues completed today plus work in flight, from a bounded recent slice."""
    if not linear_helpers.is_configured():
        return {"configured": False}
    issues, error = linear_helpers.list_command_center_issues(limit=50)
    if error:
        return {"configured": True, "error": _short_error(error)}
    today = datetime.now(timezone.utc).date().isoformat()
    completed_today = 0
    in_progress = 0
    for issue in issues:
        state_type = (issue.get("state") or {}).get("type")
        if state_type == "completed" and str(issue.get("updatedAt", ""))[:10] == today:
            completed_today += 1
        elif state_type == "started":
            in_progress += 1
    return {"configured": True, "completed_today": completed_today, "in_progress": in_progress}


def _team_metrics():
    """Live roster activity from the bounded office state; always available."""
    state = office_state.get_state()
    agents = state.get("agents") or {}
    counters = state.get("counters") or {}
    return {
        "team_size": len(agents),
        "active_now": sum(1 for agent in agents.values() if agent.get("status") not in (None, "idle")),
        "messages_today": counters.get("messages", 0),
        "events_today": counters.get("events", 0),
        "agent_events_today": counters.get("agent_events", {}),
    }


def get_metrics(force_refresh=False):
    """Return the dashboard payload; remote sources are cached, team data is live."""
    with _lock:
        now = time.time()
        if force_refresh or _remote_cache["sales"] is None or now - _remote_cache["at"] >= REMOTE_CACHE_TTL_SECONDS:
            _remote_cache["sales"] = _sales_metrics()
            _remote_cache["linear"] = _linear_metrics()
            _remote_cache["at"] = now
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sales": _remote_cache["sales"],
            "linear": _remote_cache["linear"],
            "team": _team_metrics(),
        }

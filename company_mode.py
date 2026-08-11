import json
import os
import re
import hashlib
import tempfile
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from filelock import FileLock


BASE_DIR = Path(__file__).parent


def _data_dir():
    """Directory for persistent state. Uses DATA_DIR if set, else Railway's
    RAILWAY_VOLUME_MOUNT_PATH (auto-set when a volume is attached), else the project
    dir - so company_state.json survives redeploys whenever a volume is present."""
    return Path(os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or BASE_DIR)


COMPANY_STATE_FILE = _data_dir() / "company_state.json"

# Fresh state starts on a $5/day budget so a brand-new deploy can run a small plan
# without needing /setbudget first. Change it live any time with /setbudget <amount>.
DEFAULT_DAILY_BUDGET_USD = 5.0
# Per-task budget *reservation* (a soft pre-check hold). Kept small so a 4-task plan
# fits inside the $5 default; the real metered token cost replaces it as tasks finish.
DEFAULT_TASK_ESTIMATE_USD = 1.0
DEFAULT_EMERGENCY_RESERVE_USD = 0.25
ACCOUNTING_PLACES = Decimal("0.000001")
LOCK_TIMEOUT_SECONDS = 30
DEFAULT_REVENUE_SPRINT_TOTAL_AI_BUDGET_USD = 100.0
DEFAULT_REVENUE_SPRINT_DAILY_AI_BUDGET_USD = 5.0
DEFAULT_REVENUE_SPRINT_RUN_DAYS = 20
DEFAULT_REVENUE_SPRINT_NO_PROGRESS_LIMIT = 3
REVENUE_ACTION_TYPES = {"publish", "outreach", "purchase", "deploy"}
REVENUE_SIGNAL_TYPES = {
    "click", "like", "reply", "repost", "quote", "lead", "signup", "wishlist",
    "checkout_started",
    "strong_intent", "purchase_commitment", "sale", "bounce", "unsubscribe",
}
REVENUE_SPRINT_TERMINAL_STATUSES = {"stopped", "completed", "cancelled"}
REVENUE_SPRINT_ACTIVE_STATUSES = {"active"}
REVENUE_SPRINT_RECORD_LIMIT = 500
DEFAULT_ASSIGN_TASKS = [
    ("research", "Validate demand, competitors, and buyer pain for this goal."),
    ("code", "Identify the smallest buildable asset or PR that moves this goal forward."),
    ("write", "Draft the offer, positioning, landing-page copy, or sales collateral."),
    ("editor", "Review the deliverables against the goal: approve, or list the required revisions before shipping."),
]

# Owners retired in the roster reorg -> the agents that absorbed their duties.
# Applied in normalize_state so tasks stored before the reorg re-route to the
# merged agents instead of falling back to Miles.
LEGACY_OWNER_MAP = {"news": "research", "tasks": "task", "weather": "task"}

COMPANY_COMMANDS = {
    "/company",
    "/setbudget",
    "/assign",
    "/approve",
    "/cancel",
    "/publish",
    "/launch",
    "/link",
    "/products",
    "/revenue",
    "/status",
    "/dailyreport",
    "/pausecompany",
    "/resumecompany",
}


# Optional sync hooks so a project tracker (Linear) can mirror company work without
# coupling this pure state module to the network. The runtime (group_bot via
# company_linear.register) sets these; when None they're no-ops, so company_mode
# stays fully offline-testable. Mirrors the existing main.on_delegation pattern.
#   on_project_activated(project_id)              - a project became active (approve or
#                                                   a new revision round): mirror its
#                                                   not-yet-mirrored tasks as issues.
#   on_task_status_change(task_id, status, prev)  - a task changed status: sync the
#                                                   mirrored issue's state / comment.
on_project_activated = None
on_task_status_change = None


class StateCorruptionError(RuntimeError):
    """Persistent company state could not be decoded and was quarantined."""

    def __init__(self, path, quarantine_path, cause):
        self.path = Path(path)
        self.quarantine_path = Path(quarantine_path)
        super().__init__(
            f"Company state at {self.path} is corrupt. The unreadable file was moved "
            f"to {self.quarantine_path}; restore or inspect it before continuing. "
            f"Cause: {cause}"
        )


class BudgetExceededError(ValueError):
    """A reservation would consume money unavailable to its authorization context."""


class RevenueSprintError(ValueError):
    """A persisted Revenue Sprint invariant or transition would be violated."""


class RevenueActionError(RevenueSprintError):
    """A Revenue Sprint external action is outside its exact persisted grant."""


def _fire(hook, *args):
    """Call a sync hook if set, never letting a hook error break a state operation."""
    if hook is None:
        return
    try:
        hook(*args)
    except Exception:  # noqa: BLE001 - a tracker glitch must not corrupt company state
        pass


def _configured_timezone():
    name = (
        os.environ.get("BUDGET_TIMEZONE")
        or os.environ.get("AUTONOMY_TIMEZONE")
        or os.environ.get("TIMEZONE")
        or "America/Phoenix"
    ).strip() or "America/Phoenix"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Invalid TIMEZONE {name!r}; use an IANA time-zone name.") from exc


def _now():
    return datetime.now(_configured_timezone()).replace(microsecond=0)


def today_key(at=None):
    """The configured company's calendar date, never the host machine's local date."""
    moment = at or _now()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_configured_timezone())
    else:
        moment = moment.astimezone(_configured_timezone())
    return moment.date().isoformat()


def budget_timezone_name():
    return str(_configured_timezone())


def _env_amount(name, default):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return _amount(default)
    try:
        value = _amount(raw)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a non-negative USD amount.") from exc
    if value < 0:
        raise ValueError(f"{name} must be a non-negative USD amount.")
    return value


def _amount(value):
    """Normalize accounting values to six decimals; rendering still uses two."""
    if value in (None, ""):
        return 0.0
    try:
        return float(Decimal(str(value)).quantize(ACCOUNTING_PLACES, rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid USD amount: {value!r}") from exc


def _lock_path(path):
    path = Path(path)
    return path.with_name(f"{path.name}.lock")


@contextmanager
def _locked_path(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(_lock_path(path)), timeout=LOCK_TIMEOUT_SECONDS):
        yield path


def new_state():
    today = today_key()
    return {
        "company": {
            "mode": "running",
            "budget_date": today,
            "daily_budget_usd": _amount(DEFAULT_DAILY_BUDGET_USD),
            "emergency_reserve_usd": _env_amount(
                "COMPANY_EMERGENCY_RESERVE_USD", DEFAULT_EMERGENCY_RESERVE_USD
            ),
            "reserved_today_usd": 0.0,
            "spent_today_usd": 0.0,
            "active_project_id": None,
            "active_revenue_sprint_id": None,
        },
        "projects": [],
        "tasks": [],
        "events": [],
        "products": [],
        "budget_reservations": [],
        "cost_entries": [],
        "revenue_sprints": [],
    }


def _list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return deepcopy(value)
    if isinstance(value, tuple):
        return list(value)
    return [deepcopy(value)]


def _normalize_usage_records(records):
    normalized = []
    for raw in _list(records):
        if not isinstance(raw, dict):
            continue
        item = deepcopy(raw)
        item["input_tokens"] = int(item.get("input_tokens", item.get("prompt_tokens", 0)) or 0)
        item["output_tokens"] = int(item.get("output_tokens", item.get("completion_tokens", 0)) or 0)
        item["total_tokens"] = int(
            item.get("total_tokens", item["input_tokens"] + item["output_tokens"]) or 0
        )
        normalized.append(item)
    return normalized


def _normalize_model_route_decisions(records):
    """Keep a small, explicit audit schema for deterministic model decisions.

    Route records are operational metadata, not prompt storage.  Preserve enough
    detail to explain each selection or deferral while bounding both the number of
    decisions and every text/numeric field before persistent state is written.
    """

    normalized = []
    for raw in _list(records):
        if not isinstance(raw, dict):
            continue

        def metadata(name, limit=MAX_MODEL_ROUTE_METADATA_CHARS):
            return _bounded_text(raw.get(name), limit, strip=True)[0]

        def bounded_count(name, maximum=MAX_MODEL_ROUTE_TOKEN_COUNT):
            try:
                value = int(raw.get(name, 0) or 0)
            except (TypeError, ValueError, OverflowError):
                return 0
            return min(maximum, max(0, value))

        def bounded_amount(name):
            try:
                value = Decimal(str(raw.get(name, 0.0) or 0.0))
            except (InvalidOperation, TypeError, ValueError):
                return 0.0
            if not value.is_finite() or value < 0:
                return 0.0
            return _amount(min(value, Decimal(str(MAX_MODEL_ROUTE_USD))))

        reason, reason_truncated = _bounded_text(
            raw.get("reason"), MAX_MODEL_ROUTE_REASON_CHARS, strip=True
        )
        normalized.append({
            "agent": metadata("agent"),
            "task_type": metadata("task_type"),
            "complexity": metadata("complexity", MAX_MODEL_ROUTE_STATUS_CHARS),
            "risk": metadata("risk", MAX_MODEL_ROUTE_STATUS_CHARS),
            "uses_tools": bool(raw.get("uses_tools", False)),
            "tool_count": bounded_count("tool_count", MAX_MODEL_ROUTE_TOOL_COUNT),
            "estimated_input_tokens": bounded_count("estimated_input_tokens"),
            "estimated_output_tokens": bounded_count("estimated_output_tokens"),
            "remaining_budget_usd": bounded_amount("remaining_budget_usd"),
            "model": metadata("model"),
            "model_level": metadata("model_level", MAX_MODEL_ROUTE_STATUS_CHARS),
            "estimated_cost_usd": bounded_amount("estimated_cost_usd"),
            "status": metadata("status", MAX_MODEL_ROUTE_STATUS_CHARS),
            "deferral_reason": metadata("deferral_reason"),
            "reason": reason,
            "reason_truncated": bool(raw.get("reason_truncated")) or reason_truncated,
        })

    return (
        normalized[-MAX_MODEL_ROUTE_DECISIONS:],
        len(normalized) > MAX_MODEL_ROUTE_DECISIONS,
    )


def _normalize_team_help_events(records):
    """Normalize the bounded audit trail for one task's specialist help calls.

    Help messages are provider output and later become run-report evidence.  Keep
    only a small, explicit schema so an unexpected tool payload cannot grow the
    shared state file without bound.  The normal save-path redactor still runs on
    the returned values before they reach disk.
    """
    normalized = []
    for raw in _list(records):
        if not isinstance(raw, dict):
            continue

        def text_field(name, limit, *, strip=True):
            value, truncated = _bounded_text(raw.get(name), limit, strip=strip)
            return value, bool(raw.get(f"{name}_truncated")) or truncated

        question, question_truncated = text_field("question", MAX_TEAM_HELP_QUESTION_CHARS)
        reason, reason_truncated = text_field("reason", MAX_TEAM_HELP_REASON_CHARS)
        response, response_truncated = text_field("response", MAX_TEAM_HELP_RESPONSE_CHARS)
        model_reason, model_reason_truncated = text_field(
            "model_reason", MAX_TEAM_HELP_MODEL_REASON_CHARS
        )

        def metadata(name, limit=MAX_TEAM_HELP_METADATA_CHARS):
            return _bounded_text(raw.get(name), limit, strip=True)[0]

        def token_count(name):
            try:
                return max(0, int(raw.get(name, 0) or 0))
            except (TypeError, ValueError):
                return 0

        normalized.append({
            "requesting_agent": metadata("requesting_agent"),
            "helper_agent": metadata("helper_agent"),
            "question": question,
            "question_truncated": question_truncated,
            "reason": reason,
            "reason_truncated": reason_truncated,
            "response": response,
            "response_truncated": response_truncated,
            "helper_model": metadata("helper_model"),
            "model_reason": model_reason,
            "model_reason_truncated": model_reason_truncated,
            "task_type": metadata("task_type"),
            "complexity": metadata("complexity"),
            "risk": metadata("risk"),
            "status": metadata("status", MAX_TEAM_HELP_STATUS_CHARS),
            "request_delivery": metadata("request_delivery", MAX_TEAM_HELP_STATUS_CHARS),
            "routing_delivery": metadata("routing_delivery", MAX_TEAM_HELP_STATUS_CHARS),
            "response_delivery": metadata("response_delivery", MAX_TEAM_HELP_STATUS_CHARS),
            "created_at": metadata("created_at", MAX_TEAM_HELP_TIMESTAMP_CHARS),
            "completed_at": metadata("completed_at", MAX_TEAM_HELP_TIMESTAMP_CHARS),
            "input_tokens": token_count("input_tokens"),
            "output_tokens": token_count("output_tokens"),
            "cost_usd": _amount(raw.get("cost_usd", 0.0)),
        })
    return normalized[-MAX_TEAM_HELP_EVENTS:], len(normalized) > MAX_TEAM_HELP_EVENTS


def _bounded_text(value, limit, *, strip=False):
    """Return text constrained to a configured state/prompt boundary.

    The boolean records whether this call removed content.  Callers preserve an
    existing flag when normalizing already-truncated persisted state.
    """
    text = str(value or "")
    if strip:
        text = text.strip()
    truncated = len(text) > limit
    return text[:limit], truncated


def _normalize_project(project):
    if not isinstance(project, dict):
        return None
    item = deepcopy(project)
    feedback, feedback_truncated = _bounded_text(
        item.get("last_editor_feedback"), MAX_REVIEW_FEEDBACK_CHARS, strip=True
    )
    item["last_editor_feedback"] = feedback
    item["last_editor_feedback_truncated"] = bool(
        item.get("last_editor_feedback_truncated")
    ) or feedback_truncated

    history = []
    for raw in _list(item.get("editor_feedback_history")):
        if not isinstance(raw, dict):
            continue
        entry = deepcopy(raw)
        entry_feedback, entry_truncated = _bounded_text(
            entry.get("feedback"), MAX_REVIEW_FEEDBACK_CHARS, strip=True
        )
        entry["feedback"] = entry_feedback
        entry["feedback_truncated"] = bool(entry.get("feedback_truncated")) or entry_truncated
        history.append(entry)
    history_was_truncated = len(history) > MAX_EDITOR_FEEDBACK_HISTORY
    item["editor_feedback_history"] = history[-MAX_EDITOR_FEEDBACK_HISTORY:]
    item["editor_feedback_history_truncated"] = bool(
        item.get("editor_feedback_history_truncated")
    ) or history_was_truncated
    run_id, _run_id_truncated = _bounded_text(
        item.get("revenue_sprint_run_id"), 160, strip=True
    )
    item["revenue_sprint_run_id"] = run_id
    item["external_action"] = _normalize_external_action_metadata(
        item.get("external_action")
    )
    approved = item.get("approved_revenue_action")
    if isinstance(approved, dict):
        item["approved_revenue_action"] = {
            "worker_task_id": str(approved.get("worker_task_id") or "")[:160],
            "reviewer_task_id": str(approved.get("reviewer_task_id") or "")[:160],
            "payload_digest": str(approved.get("payload_digest") or "")[:64],
            "candidate_result_digest": str(
                approved.get("candidate_result_digest") or ""
            )[:64],
            "action_type": str(approved.get("action_type") or "")[:40],
            "target": str(approved.get("target") or "")[:500],
            "policy_revision": str(approved.get("policy_revision") or "")[:160],
            "approved_at": str(approved.get("approved_at") or "")[:100],
        }
    else:
        item["approved_revenue_action"] = {}
    return item


def _normalize_task(task):
    if not isinstance(task, dict):
        return None
    item = deepcopy(task)
    owner = item.get("owner", "manager")
    item["owner"] = LEGACY_OWNER_MAP.get(owner, owner)
    item.setdefault("project_id", "")
    item.setdefault("title", "Untitled task")
    item.setdefault("delivery", "via_miles")
    item.setdefault("status", "planned")
    item["estimate_usd"] = _amount(item.get("estimate_usd", 0.0))
    item["reserved_usd"] = _amount(item.get("reserved_usd", 0.0))
    item["spent_usd"] = _amount(item.get("spent_usd", 0.0))
    result, result_truncated = _bounded_text(
        item.get("result"), MAX_TASK_STORED_RESULT_CHARS
    )
    item["result"] = result
    item["result_truncated"] = bool(item.get("result_truncated")) or result_truncated
    item["artifacts"] = _list(item.get("artifacts"))
    item["notes"] = _list(item.get("notes"))
    criteria = item.get("acceptance_criteria", [])
    item["acceptance_criteria"] = [str(value) for value in _list(criteria) if str(value).strip()]
    item.setdefault("authorization_level", "propose")
    item["revision_round"] = int(item.get("revision_round", 0) or 0)
    revision_feedback, revision_feedback_truncated = _bounded_text(
        item.get("revision_feedback"), MAX_REVIEW_FEEDBACK_CHARS, strip=True
    )
    item["revision_feedback"] = revision_feedback
    item["revision_feedback_truncated"] = bool(
        item.get("revision_feedback_truncated")
    ) or revision_feedback_truncated
    item["execution_attempts"] = int(item.get("execution_attempts", 0) or 0)
    item["review_attempts"] = int(item.get("review_attempts", 0) or 0)
    item.setdefault("failure_classification", "")
    item.setdefault("model", "")
    item.setdefault("model_reason", "")
    prior_models = item.get("prior_model_fingerprints") or item.get("prior_model_fingerprint", [])
    item["prior_model_fingerprints"] = [str(value) for value in _list(prior_models) if value]
    feedback = item.get("feedback_fingerprints") or item.get("feedback_fingerprint", [])
    item["feedback_fingerprints"] = [str(value) for value in _list(feedback) if value]
    item["attempt_history"] = _list(item.get("attempt_history"))
    item["usage_records"] = _normalize_usage_records(item.get("usage_records"))
    help_events, help_events_truncated = _normalize_team_help_events(
        item.get("team_help_events")
    )
    item["team_help_events"] = help_events
    item["team_help_events_truncated"] = bool(
        item.get("team_help_events_truncated")
    ) or help_events_truncated
    item["input_tokens"] = int(item.get("input_tokens", 0) or 0)
    item["output_tokens"] = int(item.get("output_tokens", 0) or 0)
    item["total_tokens"] = int(item.get("total_tokens", 0) or 0)
    item.setdefault("budget_reservation_id", "")
    item["campaign_id"] = str(item.get("campaign_id") or "")
    item.setdefault("linear_issue_id", "")
    item.setdefault("linear_identifier", "")
    item.setdefault("linear_url", "")
    return item


def _normalize_reservation(entry):
    if not isinstance(entry, dict):
        return None
    item = deepcopy(entry)
    amount = item.get("amount_usd", item.get("reserved_usd", 0.0))
    item.setdefault("id", f"res_{uuid.uuid4().hex[:12]}")
    item.setdefault("budget_date", item.get("date", today_key()))
    item.setdefault("status", "reserved")
    item["amount_usd"] = _amount(amount)
    item["remaining_usd"] = _amount(
        item.get("remaining_usd", item["amount_usd"] if item["status"] == "reserved" else 0.0)
    )
    item["reconciled_usd"] = _amount(item.get("reconciled_usd", 0.0))
    item.setdefault("context", "task")
    for key in ("project_id", "task_id", "agent", "model", "reason"):
        item.setdefault(key, None if key.endswith("_id") else "")
    item.setdefault("created_at", _now().isoformat())
    item.setdefault("updated_at", item["created_at"])
    item["uses_emergency_reserve"] = bool(item.get("uses_emergency_reserve", False))
    item["campaign_id"] = str(item.get("campaign_id") or "")
    item["campaign_date"] = str(item.get("campaign_date") or item["budget_date"])
    return item


def _normalize_cost_entry(entry):
    if not isinstance(entry, dict):
        return None
    item = deepcopy(entry)
    item.setdefault("id", f"cost_{uuid.uuid4().hex[:12]}")
    item.setdefault("budget_date", item.get("date", today_key()))
    item["amount_usd"] = _amount(item.get("amount_usd", item.get("cost_usd", 0.0)))
    basis = str(item.get("cost_basis", item.get("label", "actual"))).lower()
    item["cost_basis"] = "estimated" if basis == "estimated" or item.get("is_estimated") else "actual"
    item["is_estimated"] = item["cost_basis"] == "estimated"
    item.setdefault("reservation_id", "")
    item.setdefault("context", "task")
    for key in ("project_id", "task_id", "agent", "model", "reason"):
        item.setdefault(key, None if key.endswith("_id") else "")
    item["usage_records"] = _normalize_usage_records(item.get("usage_records"))
    route_decisions, route_decisions_truncated = _normalize_model_route_decisions(
        item.get("model_route_decisions")
    )
    item["model_route_decisions"] = route_decisions
    item["model_route_decisions_truncated"] = bool(
        item.get("model_route_decisions_truncated")
    ) or route_decisions_truncated
    item["input_tokens"] = int(item.get("input_tokens", sum(r["input_tokens"] for r in item["usage_records"])) or 0)
    item["output_tokens"] = int(item.get("output_tokens", sum(r["output_tokens"] for r in item["usage_records"])) or 0)
    item["total_tokens"] = int(
        item.get("total_tokens", item["input_tokens"] + item["output_tokens"]) or 0
    )
    item.setdefault("created_at", _now().isoformat())
    item["campaign_id"] = str(item.get("campaign_id") or "")
    item["campaign_date"] = str(item.get("campaign_date") or item["budget_date"])
    return item


def _normalize_sprint_product(value):
    raw = value if isinstance(value, dict) else {}
    return {
        "project_id": str(raw.get("project_id") or "").strip(),
        "gumroad_product_id": str(raw.get("gumroad_product_id") or "").strip(),
        "gumroad_url": str(raw.get("gumroad_url") or "").strip().rstrip("/"),
        "title": str(raw.get("title") or "").strip(),
        "ownership": str(raw.get("ownership") or "").strip().lower(),
        "personal_fallback_allowed": bool(raw.get("personal_fallback_allowed", False)),
    }


def _normalize_sprint_channel(value):
    raw = value if isinstance(value, dict) else {}
    return {
        "type": str(raw.get("type") or "").strip().lower(),
        "account_id": str(raw.get("account_id") or "").strip(),
        "destination_scope": str(raw.get("destination_scope") or "").strip(),
        "name": str(raw.get("name") or raw.get("type") or "").strip(),
        "ownership": str(raw.get("ownership") or "").strip().lower(),
        "personal_fallback_allowed": bool(raw.get("personal_fallback_allowed", False)),
    }


def _normalize_external_action_metadata(value):
    """Normalize the only external-action fields allowed onto a project.

    This is execution control data, not arbitrary provider metadata. Rejecting
    extra keys keeps later sinks from accidentally trusting an unreviewed action
    parameter copied through project state.
    """
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        raise RevenueSprintError("Revenue Sprint external_action metadata must be an object.")
    if not value:
        return {}
    allowed_fields = {"action_type", "target", "policy_revision"}
    unexpected = sorted(set(value) - allowed_fields)
    if unexpected:
        raise RevenueSprintError(
            f"Unsupported Revenue Sprint external_action fields: {unexpected}"
        )
    action_type = str(value.get("action_type") or "").strip().lower()
    target = str(value.get("target") or "").strip()
    policy_revision = str(value.get("policy_revision") or "").strip()
    if action_type not in REVENUE_ACTION_TYPES:
        raise RevenueSprintError(
            f"Unsupported Revenue Sprint action type: {action_type!r}."
        )
    if not target or "*" in target:
        raise RevenueSprintError(
            "Revenue Sprint external_action target must be exact and cannot contain a wildcard."
        )
    if not policy_revision:
        raise RevenueSprintError(
            "Revenue Sprint external_action requires an exact policy_revision."
        )
    return {
        "action_type": action_type[:64],
        "target": target[:512],
        "policy_revision": policy_revision[:160],
    }


def _normalize_action_journal_metadata(value):
    """Bound untrusted adapter metadata before it reaches persistent state."""
    if not isinstance(value, dict):
        return {}
    normalized = {}
    for raw_key in sorted(value, key=lambda item: str(item))[:20]:
        key = str(raw_key or "").strip()[:64]
        if not key or key in normalized:
            continue
        raw_value = value[raw_key]
        if isinstance(raw_value, bool) or raw_value is None:
            normalized[key] = raw_value
        elif isinstance(raw_value, (int, float)):
            normalized[key] = raw_value
        else:
            normalized[key] = str(raw_value or "")[:512]
    return normalized


def _normalize_provider_receipt(value):
    """Persist only a small, flat string receipt returned by an action provider."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise RevenueActionError("Provider receipt must be a small string mapping.")
    if len(value) > 8:
        raise RevenueActionError("Provider receipt may contain at most 8 fields.")
    normalized = {}
    for raw_key in sorted(value, key=lambda item: str(item)):
        if not isinstance(raw_key, str) or not isinstance(value[raw_key], str):
            raise RevenueActionError("Provider receipt keys and values must be strings.")
        key = raw_key.strip().lower()
        receipt_value = value[raw_key].strip()
        if not key or not receipt_value:
            raise RevenueActionError("Provider receipt keys and values cannot be empty.")
        if len(key) > 64 or len(receipt_value) > 1000:
            raise RevenueActionError("Provider receipt field exceeds its persistence limit.")
        if key in normalized:
            raise RevenueActionError("Provider receipt contains duplicate normalized keys.")
        normalized[key] = receipt_value
    return normalized


_BLUESKY_ENGAGEMENT_FIELDS = ("like", "reply", "repost", "quote")


def _normalize_engagement_counts(value, *, require_all=False):
    if value is None and not require_all:
        return {}
    if not isinstance(value, Mapping):
        raise RevenueSprintError("Bluesky engagement counts must be a mapping.")
    normalized = {}
    for signal_type in _BLUESKY_ENGAGEMENT_FIELDS:
        candidates = (
            signal_type,
            f"{signal_type}_count",
            f"{signal_type}Count",
        )
        supplied = [value[name] for name in candidates if name in value]
        if not supplied:
            if require_all:
                raise RevenueSprintError(
                    f"Bluesky engagement observation is missing {signal_type} count."
                )
            continue
        if any(
            isinstance(item, bool) or not isinstance(item, int) or item < 0
            for item in supplied
        ):
            raise RevenueSprintError(
                f"Bluesky {signal_type} count must be an exact non-negative integer."
            )
        if len(supplied) > 1 and any(item != supplied[0] for item in supplied[1:]):
            raise RevenueSprintError(
                f"Bluesky engagement observation has conflicting {signal_type} counts."
            )
        count = supplied[0]
        normalized[signal_type] = count
    return normalized


def _normalize_action_policy(value):
    raw = value if isinstance(value, dict) else {}
    allowed_types = []
    for action_type in _list(raw.get("allowed_action_types")):
        normalized = str(action_type or "").strip().lower()
        if normalized in REVENUE_ACTION_TYPES and normalized not in allowed_types:
            allowed_types.append(normalized)

    raw_targets = raw.get("allowed_targets") if isinstance(raw.get("allowed_targets"), dict) else {}
    raw_daily = raw.get("daily_action_caps") if isinstance(raw.get("daily_action_caps"), dict) else {}
    raw_total = raw.get("total_action_caps") if isinstance(raw.get("total_action_caps"), dict) else {}
    targets = {}
    daily_caps = {}
    total_caps = {}
    for action_type in allowed_types:
        targets[action_type] = list(dict.fromkeys(
            str(target or "").strip()
            for target in _list(raw_targets.get(action_type))
            if str(target or "").strip()
        ))
        try:
            daily_caps[action_type] = max(0, int(raw_daily.get(action_type, 0) or 0))
            total_caps[action_type] = max(0, int(raw_total.get(action_type, 0) or 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Revenue action caps for {action_type!r} must be integers.") from exc
    return {
        "revision": str(raw.get("revision") or "").strip(),
        "allowed_action_types": allowed_types,
        "allowed_targets": targets,
        "daily_action_caps": daily_caps,
        "total_action_caps": total_caps,
        "purchase_daily_cap_usd": _amount(raw.get("purchase_daily_cap_usd", 0.0)),
        "purchase_total_cap_usd": _amount(raw.get("purchase_total_cap_usd", 0.0)),
        "approved_at": str(raw.get("approved_at") or "").strip(),
        "approved_by": str(raw.get("approved_by") or "").strip(),
    }


def _normalize_checkpoint_policy(value):
    raw = value if isinstance(value, dict) else {}
    interest_types = list(dict.fromkeys(
        str(signal or "").strip().lower()
        for signal in _list(raw.get("day5_interest_signal_types") or [
            "like", "reply", "repost", "quote", "click", "lead", "signup",
            "checkout_started", "wishlist", "strong_intent", "sale"
        ])
        if str(signal or "").strip()
    ))
    intent_types = list(dict.fromkeys(
        str(signal or "").strip().lower()
        for signal in _list(raw.get("day15_strong_intent_signal_types") or [
            "strong_intent", "checkout_started", "purchase_commitment"
        ])
        if str(signal or "").strip()
    ))
    unknown = (set(interest_types) | set(intent_types)) - REVENUE_SIGNAL_TYPES
    if unknown:
        raise ValueError(f"Unsupported Revenue Sprint signal types: {sorted(unknown)}")
    try:
        interest_minimum = max(1, int(raw.get("day5_min_interest_count", 1) or 1))
        intent_minimum = max(1, int(raw.get("day15_min_strong_intent_count", 1) or 1))
        sales_minimum = max(1, int(raw.get("day15_min_sales", 1) or 1))
        trailing_window_days = max(1, int(raw.get("trailing_window_days", 7) or 7))
    except (TypeError, ValueError) as exc:
        raise ValueError("Revenue checkpoint thresholds must be positive integers.") from exc
    gross_per_day = _amount(raw.get("minimum_gross_revenue_usd_per_day", 5.0))
    minimum_trailing_gross = _amount(raw.get("minimum_trailing_gross_revenue_usd", 35.0))
    if gross_per_day < 0 or minimum_trailing_gross < 0:
        raise ValueError("Revenue checkpoint economic thresholds cannot be negative.")
    raw_contribution = raw.get("require_nonnegative_contribution", True)
    require_contribution = (
        raw_contribution.strip().lower() not in {"false", "0", "no", "off"}
        if isinstance(raw_contribution, str)
        else bool(raw_contribution)
    )
    return {
        "day5_interest_signal_types": interest_types,
        "day5_min_interest_count": interest_minimum,
        "day15_strong_intent_signal_types": intent_types,
        "day15_min_strong_intent_count": intent_minimum,
        "day15_min_sales": sales_minimum,
        "trailing_window_days": trailing_window_days,
        "minimum_gross_revenue_usd_per_day": gross_per_day,
        "minimum_trailing_gross_revenue_usd": minimum_trailing_gross,
        "require_nonnegative_contribution": require_contribution,
    }


def _normalize_revenue_sprint(value):
    if not isinstance(value, dict):
        return None
    item = deepcopy(value)
    item.setdefault("id", f"sprint_{uuid.uuid4().hex[:12]}")
    item["id"] = str(item["id"] or "").strip()
    item["status"] = str(item.get("status") or "active").strip().lower()
    item["timezone"] = str(item.get("timezone") or budget_timezone_name()).strip()
    try:
        ZoneInfo(item["timezone"])
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Invalid Revenue Sprint timezone {item['timezone']!r}.") from exc
    item["total_ai_budget_usd"] = _amount(
        item.get("total_ai_budget_usd", DEFAULT_REVENUE_SPRINT_TOTAL_AI_BUDGET_USD)
    )
    item["daily_ai_budget_usd"] = _amount(
        item.get("daily_ai_budget_usd", DEFAULT_REVENUE_SPRINT_DAILY_AI_BUDGET_USD)
    )
    try:
        item["max_run_days"] = max(1, int(item.get("max_run_days", DEFAULT_REVENUE_SPRINT_RUN_DAYS)))
        item["max_consecutive_no_progress_days"] = max(
            1,
            int(item.get("max_consecutive_no_progress_days", DEFAULT_REVENUE_SPRINT_NO_PROGRESS_LIMIT)),
        )
        item["consecutive_no_progress_days"] = max(
            0, int(item.get("consecutive_no_progress_days", 0) or 0)
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("Revenue Sprint run limits must be positive integers.") from exc
    item["product"] = _normalize_sprint_product(item.get("product"))
    item["channel"] = _normalize_sprint_channel(item.get("channel"))
    item["automation_policy"] = _normalize_action_policy(item.get("automation_policy"))
    item["checkpoint_policy"] = _normalize_checkpoint_policy(item.get("checkpoint_policy"))
    for field in (
        "run_days",
        "revenue_snapshots",
        "signals",
        "experiments",
        "checkpoint_results",
        "pivot_history",
        "action_journal",
        "engagement_snapshots",
    ):
        item[field] = _list(item.get(field))[-REVENUE_SPRINT_RECORD_LIMIT:]
    for action in item["action_journal"]:
        if not isinstance(action, dict):
            continue
        action["provider_receipt"] = _normalize_provider_receipt(
            action.get("provider_receipt")
        )
        action["engagement_counts"] = _normalize_engagement_counts(
            action.get("engagement_counts")
        )
    item["pivot_required"] = bool(item.get("pivot_required", False))
    item.setdefault("phase", "validate")
    item.setdefault("stop_reason", "")
    item.setdefault("created_at", _now().isoformat())
    item.setdefault("started_at", item["created_at"])
    item.setdefault("updated_at", item["created_at"])
    item.setdefault("finished_at", None)
    return item


def normalize_state(state):
    base = new_state()
    if not isinstance(state, dict):
        return base

    normalized = deepcopy(base)
    for key in ("events", "products"):
        normalized[key] = _list(state.get(key, normalized[key]))
    normalized["projects"] = [
        project
        for project in (_normalize_project(raw) for raw in _list(state.get("projects")))
        if project is not None
    ]
    normalized["tasks"] = [
        task for task in (_normalize_task(raw) for raw in _list(state.get("tasks"))) if task is not None
    ]
    normalized["budget_reservations"] = [
        entry
        for entry in (_normalize_reservation(raw) for raw in _list(state.get("budget_reservations")))
        if entry is not None
    ]
    normalized["cost_entries"] = [
        entry
        for entry in (_normalize_cost_entry(raw) for raw in _list(state.get("cost_entries")))
        if entry is not None
    ]
    normalized["revenue_sprints"] = [
        entry
        for entry in (_normalize_revenue_sprint(raw) for raw in _list(state.get("revenue_sprints")))
        if entry is not None
    ]
    company = state.get("company", {}) if isinstance(state.get("company", {}), dict) else {}
    normalized["company"].update(company)
    normalized["company"]["daily_budget_usd"] = _amount(normalized["company"].get("daily_budget_usd"))
    normalized["company"]["emergency_reserve_usd"] = _amount(
        normalized["company"].get(
            "emergency_reserve_usd",
            _env_amount("COMPANY_EMERGENCY_RESERVE_USD", DEFAULT_EMERGENCY_RESERVE_USD),
        )
    )
    normalized["company"]["reserved_today_usd"] = _amount(
        normalized["company"].get("reserved_today_usd")
    )
    normalized["company"]["spent_today_usd"] = _amount(normalized["company"].get("spent_today_usd"))
    normalized["company"]["active_revenue_sprint_id"] = (
        str(normalized["company"].get("active_revenue_sprint_id") or "").strip() or None
    )
    sprint_ids = [str(item.get("id") or "") for item in normalized["revenue_sprints"]]
    if len(sprint_ids) != len(set(sprint_ids)):
        raise ValueError("Revenue Sprint IDs must be unique.")
    active_sprints = [
        item for item in normalized["revenue_sprints"]
        if item.get("status") in REVENUE_SPRINT_ACTIVE_STATUSES
    ]
    if len(active_sprints) > 1:
        raise ValueError("Only one Revenue Sprint may be active.")
    active_id = normalized["company"].get("active_revenue_sprint_id")
    if active_sprints:
        if active_id and active_id != active_sprints[0]["id"]:
            raise ValueError("Active Revenue Sprint pointer does not match the active campaign.")
        normalized["company"]["active_revenue_sprint_id"] = active_sprints[0]["id"]
    elif active_id:
        if active_id not in sprint_ids:
            raise ValueError("Active Revenue Sprint pointer references a missing campaign.")
        normalized["company"]["active_revenue_sprint_id"] = None

    if normalized["company"].get("budget_date") != today_key():
        normalized["company"]["budget_date"] = today_key()
        normalized["company"]["reserved_today_usd"] = 0.0
        normalized["company"]["spent_today_usd"] = 0.0
        for reservation in normalized["budget_reservations"]:
            if reservation["status"] == "reserved":
                reservation["status"] = "expired"
                reservation["remaining_usd"] = 0.0
                reservation["reason"] = reservation.get("reason") or "Budget date rolled over."
                reservation["updated_at"] = _now().isoformat()

    return normalized


def _quarantine_corrupt_state(path, cause):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine = path.with_name(f"{path.name}.corrupt.{stamp}.{uuid.uuid4().hex[:8]}")
    os.replace(path, quarantine)
    raise StateCorruptionError(path, quarantine, cause) from cause


def _load_state_unlocked(path):
    if not path.exists():
        quarantined = sorted(path.parent.glob(f"{path.name}.corrupt.*"), key=lambda item: item.stat().st_mtime)
        if quarantined:
            raise StateCorruptionError(
                path,
                quarantined[-1],
                RuntimeError("quarantined state has not been restored or replaced"),
            )
        return new_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _quarantine_corrupt_state(path, exc)
    if not isinstance(raw, dict):
        _quarantine_corrupt_state(path, TypeError("state root must be a JSON object"))
    return normalize_state(raw)


def _save_state_unlocked(state, path):
    normalized = normalize_state(state)
    # Persisted model/tool output is an audit surface, so apply the same recursive
    # redactor used by autonomous reports before bytes reach disk.
    from autonomous_workflow import redact_secrets
    normalized = redact_secrets(normalized)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(normalized, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return normalized


@contextmanager
def _state_transaction(path=COMPANY_STATE_FILE):
    with _locked_path(path) as locked:
        state = _load_state_unlocked(locked)
        yield state
        _save_state_unlocked(state, locked)


def load_state(path=COMPANY_STATE_FILE):
    with _locked_path(path) as locked:
        return _load_state_unlocked(locked)


def save_state(state, path=COMPANY_STATE_FILE):
    with _locked_path(path) as locked:
        return _save_state_unlocked(state, locked)


def _money(value):
    """Backward-compatible accounting helper; presentation uses ``:.2f``."""
    return _amount(value)


def remaining_budget(state, include_emergency=False):
    company = normalize_state(state)["company"]
    emergency = 0.0 if include_emergency else company.get("emergency_reserve_usd", 0.0)
    return _amount(max(
        0.0,
        company["daily_budget_usd"]
        - company["reserved_today_usd"]
        - company["spent_today_usd"]
        - emergency,
    ))


def _sprint_moment(sprint, at=None):
    zone = ZoneInfo(str(sprint.get("timezone") or budget_timezone_name()))
    moment = at or datetime.now(zone)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    else:
        moment = moment.astimezone(zone)
    return moment


def _sprint_date(sprint, at=None):
    return _sprint_moment(sprint, at).date().isoformat()


def active_revenue_sprint(state, sprint_id=None):
    """Return one exact persisted sprint, defaulting to the company's active ID."""

    target = str(sprint_id or state.get("company", {}).get("active_revenue_sprint_id") or "").strip()
    if not target:
        return None
    return next(
        (item for item in state.get("revenue_sprints", []) if str(item.get("id") or "") == target),
        None,
    )


def _campaign_commitments(state, sprint_id, campaign_date=None):
    costs = [
        entry
        for entry in state.get("cost_entries", [])
        if str(entry.get("campaign_id") or "") == str(sprint_id)
        and (campaign_date is None or str(entry.get("campaign_date") or entry.get("budget_date")) == campaign_date)
    ]
    reservations = [
        entry
        for entry in state.get("budget_reservations", [])
        if str(entry.get("campaign_id") or "") == str(sprint_id)
        and entry.get("status") == "reserved"
        and (campaign_date is None or str(entry.get("campaign_date") or entry.get("budget_date")) == campaign_date)
    ]
    return {
        "spent_usd": _amount(sum(_amount(entry.get("amount_usd")) for entry in costs)),
        "reserved_usd": _amount(sum(_amount(entry.get("remaining_usd")) for entry in reservations)),
    }


def revenue_sprint_budget_snapshot(state, sprint_id=None, at=None):
    sprint = active_revenue_sprint(state, sprint_id)
    if sprint is None:
        return {
            "active": False,
            "campaign_id": None,
            "status": "inactive",
            "campaign_date": None,
            "total_ai_budget_usd": 0.0,
            "daily_ai_budget_usd": 0.0,
            "spent_total_usd": 0.0,
            "reserved_total_usd": 0.0,
            "remaining_total_usd": 0.0,
            "spent_today_usd": 0.0,
            "reserved_today_usd": 0.0,
            "remaining_today_usd": 0.0,
            "ordinary_remaining_today_usd": 0.0,
            "emergency_reserve_usd": 0.0,
            "run_days_used": 0,
            "max_run_days": 0,
            "pivot_required": False,
            "stop_reason": "",
        }
    campaign_date = _sprint_date(sprint, at)
    total = _campaign_commitments(state, sprint["id"])
    daily = _campaign_commitments(state, sprint["id"], campaign_date)
    total_cap = _amount(sprint.get("total_ai_budget_usd"))
    daily_cap = _amount(sprint.get("daily_ai_budget_usd"))
    # The campaign's daily ceiling includes the Company's emergency reserve.  In
    # particular, a $5 campaign running inside a $10 Company day may spend at
    # most $4.75 on ordinary work when the reserve is $0.25; summaries and
    # escalations can use the final $0.25 but can never push the campaign above
    # its raw $5 ceiling.
    campaign_emergency_reserve = _amount(min(
        daily_cap,
        state.get("company", {}).get("emergency_reserve_usd", 0.0),
    ))
    remaining_today = _amount(
        max(0.0, daily_cap - daily["spent_usd"] - daily["reserved_usd"])
    )
    ordinary_remaining_today = _amount(max(
        0.0,
        daily_cap
        - campaign_emergency_reserve
        - daily["spent_usd"]
        - daily["reserved_usd"],
    ))
    run_days = [entry for entry in sprint.get("run_days", []) if isinstance(entry, dict)]
    return {
        "active": sprint.get("status") in REVENUE_SPRINT_ACTIVE_STATUSES,
        "campaign_id": sprint["id"],
        "status": sprint.get("status"),
        "campaign_date": campaign_date,
        "total_ai_budget_usd": total_cap,
        "daily_ai_budget_usd": daily_cap,
        "spent_total_usd": total["spent_usd"],
        "reserved_total_usd": total["reserved_usd"],
        "remaining_total_usd": _amount(max(0.0, total_cap - total["spent_usd"] - total["reserved_usd"])),
        "spent_today_usd": daily["spent_usd"],
        "reserved_today_usd": daily["reserved_usd"],
        "remaining_today_usd": remaining_today,
        "ordinary_remaining_today_usd": ordinary_remaining_today,
        "emergency_reserve_usd": campaign_emergency_reserve,
        "run_days_used": len(run_days),
        "max_run_days": int(sprint.get("max_run_days", DEFAULT_REVENUE_SPRINT_RUN_DAYS)),
        "pivot_required": bool(sprint.get("pivot_required")),
        "stop_reason": str(sprint.get("stop_reason") or ""),
    }


def _campaign_admission_available(state, campaign_id, *, allow_emergency=False):
    sprint = active_revenue_sprint(state, campaign_id)
    if sprint is None:
        raise RevenueSprintError(f"Revenue Sprint {campaign_id!r} was not found.")
    if sprint.get("status") not in REVENUE_SPRINT_ACTIVE_STATUSES:
        raise BudgetExceededError(
            f"Revenue Sprint {campaign_id!r} is {sprint.get('status')!r}; no new AI spend is allowed."
        )
    snapshot = revenue_sprint_budget_snapshot(state, campaign_id)
    daily_available = (
        snapshot["remaining_today_usd"]
        if allow_emergency
        else snapshot["ordinary_remaining_today_usd"]
    )
    return _amount(min(
        remaining_budget(state, include_emergency=allow_emergency),
        snapshot["remaining_total_usd"],
        daily_available,
    )), snapshot


def set_daily_budget(amount_usd, path=COMPANY_STATE_FILE):
    amount = _amount(amount_usd)
    if amount < 0:
        return "Budget must be zero or greater."

    with _state_transaction(path) as state:
        state["company"]["daily_budget_usd"] = amount
        state["company"]["budget_date"] = today_key()
    return f"Company budget set to ${amount:.2f} for today. Remaining: ${remaining_budget(state):.2f}."


def _attribution(context, project_id, task_id, agent, model, reason, campaign_id=None):
    if isinstance(context, dict):
        values = context
        context = values.get("context", values.get("kind", "task"))
        project_id = project_id or values.get("project_id") or values.get("project")
        task_id = task_id or values.get("task_id") or values.get("task")
        agent = agent or values.get("agent") or values.get("owner")
        model = model or values.get("model")
        reason = reason or values.get("reason")
        campaign_id = campaign_id or values.get("campaign_id") or values.get("sprint_id")
    return {
        "context": str(context or "task"),
        "project_id": project_id,
        "task_id": task_id,
        "agent": str(agent or ""),
        "model": str(model or ""),
        "reason": str(reason or ""),
        "campaign_id": str(campaign_id or ""),
    }


def _find_reservation(state, reservation_id):
    return next(
        (item for item in state.get("budget_reservations", []) if item.get("id") == reservation_id),
        None,
    )


def _reserve_budget_in_state(
    state,
    amount_usd,
    *,
    context="task",
    project_id=None,
    task_id=None,
    agent=None,
    model=None,
    reason="",
    reservation_id=None,
    allow_emergency=False,
    campaign_id=None,
):
    amount = _amount(amount_usd)
    if amount <= 0:
        raise ValueError("A budget reservation must be greater than zero.")
    attribution = _attribution(
        context, project_id, task_id, agent, model, reason, campaign_id
    )
    reservation_id = reservation_id or f"res_{uuid.uuid4().hex[:12]}"
    existing = _find_reservation(state, reservation_id)
    if existing:
        idempotency_fields = (
            "context", "project_id", "task_id", "agent", "model", "campaign_id"
        )
        if (
            existing["status"] == "reserved"
            and existing["amount_usd"] == amount
            and all(existing.get(field) == attribution.get(field) for field in idempotency_fields)
        ):
            return existing
        raise ValueError(f"Budget reservation id already exists: {reservation_id}")

    emergency_context = attribution["context"].lower() in {"emergency", "escalation", "summary"}
    may_use_emergency = bool(allow_emergency or emergency_context)
    available = remaining_budget(state, include_emergency=may_use_emergency)
    campaign_snapshot = None
    if attribution["campaign_id"]:
        campaign_available, campaign_snapshot = _campaign_admission_available(
            state, attribution["campaign_id"], allow_emergency=may_use_emergency
        )
        available = min(available, campaign_available)
    if amount > available:
        reserve_note = " including emergency reserve" if may_use_emergency else " (emergency reserve excluded)"
        campaign_daily_available = (
            campaign_snapshot["remaining_today_usd"]
            if may_use_emergency
            else campaign_snapshot["ordinary_remaining_today_usd"]
        ) if campaign_snapshot is not None else 0.0
        campaign_note = (
            f"; Revenue Sprint {attribution['campaign_id']} has "
            f"${campaign_daily_available:.2f} available for this context today and "
            f"${campaign_snapshot['remaining_total_usd']:.2f} total"
            if campaign_snapshot is not None
            else ""
        )
        raise BudgetExceededError(
            f"Cannot reserve ${amount:.2f}; only ${available:.2f} remains{reserve_note}{campaign_note}."
        )

    ordinary_available = remaining_budget(state)
    now = _now().isoformat()
    reservation = {
        "id": reservation_id,
        "budget_date": state["company"]["budget_date"],
        "status": "reserved",
        "amount_usd": amount,
        "remaining_usd": amount,
        "reconciled_usd": 0.0,
        **attribution,
        "uses_emergency_reserve": bool(
            amount > ordinary_available
            or (
                campaign_snapshot is not None
                and may_use_emergency
                and amount > campaign_snapshot["ordinary_remaining_today_usd"]
            )
        ),
        "campaign_date": (
            campaign_snapshot["campaign_date"]
            if campaign_snapshot is not None
            else state["company"]["budget_date"]
        ),
        "created_at": now,
        "updated_at": now,
    }
    state.setdefault("budget_reservations", []).append(reservation)
    state["company"]["reserved_today_usd"] = _amount(
        state["company"].get("reserved_today_usd", 0.0) + amount
    )
    add_event(
        state,
        "budget_reserved",
        f"Reserved budget for {attribution['context']}.",
        project_id=project_id,
        task_id=task_id,
        amount_usd=amount,
    )
    return reservation


def reserve_budget(
    amount_usd,
    path=COMPANY_STATE_FILE,
    *,
    context="task",
    project_id=None,
    task_id=None,
    agent=None,
    model=None,
    reason="",
    reservation_id=None,
    allow_emergency=False,
    campaign_id=None,
):
    """Atomically hold estimated spend so concurrent workers cannot oversubscribe."""
    with _state_transaction(path) as state:
        reservation = _reserve_budget_in_state(
            state,
            amount_usd,
            context=context,
            project_id=project_id,
            task_id=task_id,
            agent=agent,
            model=model,
            reason=reason,
            reservation_id=reservation_id,
            allow_emergency=allow_emergency,
            campaign_id=campaign_id,
        )
        result = deepcopy(reservation)
    return result


def expand_task_budget_reservation(
    task_id,
    minimum_total_usd,
    preferred_total_usd=None,
    path=COMPANY_STATE_FILE,
):
    """Atomically enlarge one active task's hold using ordinary budget only.

    Tool-loop input grows as repository evidence is appended.  The initial task
    estimate remains the admission-control hold, while this function lets the
    request guard claim otherwise-uncommitted budget before a later model call.
    Existing task/reviewer holds and the emergency reserve remain unavailable.
    """

    minimum = _amount(minimum_total_usd)
    preferred = _amount(
        minimum_total_usd if preferred_total_usd is None else preferred_total_usd
    )
    if minimum <= 0:
        raise ValueError("The minimum task budget must be greater than zero.")
    preferred = max(minimum, preferred)

    with _state_transaction(path) as state:
        task = next((item for item in state["tasks"] if item["id"] == task_id), None)
        if task is None:
            raise KeyError(f"Task not found: {task_id}")
        if task.get("status") not in {"planned", "in_progress"}:
            raise ValueError(
                f"Task {task_id} is {task.get('status')} and cannot expand its budget hold."
            )

        reservation_id = task.get("budget_reservation_id")
        reservation = _find_reservation(state, reservation_id) if reservation_id else None
        if reservation is None or reservation.get("status") != "reserved":
            raise ValueError(f"Task {task_id} has no active budget reservation to expand.")

        current = _amount(reservation.get("amount_usd", 0.0))
        minimum = max(current, minimum)
        preferred = max(minimum, preferred)
        available = remaining_budget(state)
        campaign_id = str(reservation.get("campaign_id") or "")
        if campaign_id:
            campaign_available, _campaign_snapshot = _campaign_admission_available(
                state, campaign_id, allow_emergency=False
            )
            available = min(available, campaign_available)
        maximum = _amount(current + available)
        if maximum < minimum:
            result = {
                "expanded": False,
                "reason": "insufficient_ordinary_budget",
                "task_id": task_id,
                "amount_usd": current,
                "added_usd": 0.0,
                "ordinary_remaining_usd": available,
                "campaign_id": campaign_id,
            }
        else:
            target = _amount(min(preferred, maximum))
            added = _amount(target - current)
            if added > 0:
                reservation["amount_usd"] = target
                reservation["remaining_usd"] = target
                reservation["updated_at"] = _now().isoformat()
                task["reserved_usd"] = target
                task["updated_at"] = _now().isoformat()
                state["company"]["reserved_today_usd"] = _amount(
                    state["company"].get("reserved_today_usd", 0.0) + added
                )
                add_event(
                    state,
                    "budget_reservation_expanded",
                    "Expanded an active task reservation for a bounded model request.",
                    project_id=task.get("project_id"),
                    task_id=task_id,
                    amount_usd=added,
                )
            result = {
                "expanded": added > 0,
                "reason": "expanded" if added > 0 else "already_sufficient",
                "task_id": task_id,
                "amount_usd": target,
                "added_usd": added,
                "ordinary_remaining_usd": remaining_budget(state),
                "campaign_id": campaign_id,
            }
        response = deepcopy(result)
    return response


def _record_cost_entry_in_state(
    state,
    amount_usd,
    *,
    reservation_id="",
    usage_records=None,
    model_route_decisions=None,
    estimated=False,
    context="task",
    project_id=None,
    task_id=None,
    agent=None,
    model=None,
    reason="",
    campaign_id=None,
    campaign_date=None,
):
    amount = _amount(amount_usd)
    attribution = _attribution(
        context, project_id, task_id, agent, model, reason, campaign_id
    )
    usage = _normalize_usage_records(usage_records)
    route_decisions, route_decisions_truncated = _normalize_model_route_decisions(
        model_route_decisions
    )
    input_tokens = sum(item["input_tokens"] for item in usage)
    output_tokens = sum(item["output_tokens"] for item in usage)
    total_tokens = sum(item["total_tokens"] for item in usage)
    entry = {
        "id": f"cost_{uuid.uuid4().hex[:12]}",
        "budget_date": state["company"]["budget_date"],
        "campaign_date": str(campaign_date or state["company"]["budget_date"]),
        "amount_usd": amount,
        "cost_basis": "estimated" if estimated else "actual",
        "is_estimated": bool(estimated),
        "reservation_id": reservation_id or "",
        **attribution,
        "usage_records": usage,
        "model_route_decisions": route_decisions,
        "model_route_decisions_truncated": route_decisions_truncated,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "created_at": _now().isoformat(),
    }
    state.setdefault("cost_entries", []).append(entry)
    state["company"]["spent_today_usd"] = _amount(
        state["company"].get("spent_today_usd", 0.0) + amount
    )
    return entry


def _reconcile_budget_in_state(
    state,
    reservation_id,
    actual_usd=None,
    *,
    usage_records=None,
    model_route_decisions=None,
    estimated=False,
    context=None,
    project_id=None,
    task_id=None,
    agent=None,
    model=None,
    reason="",
    campaign_id=None,
):
    reservation = _find_reservation(state, reservation_id)
    if reservation is None:
        raise KeyError(f"Budget reservation not found: {reservation_id}")
    if reservation["status"] == "reconciled":
        entry = next(
            (item for item in state.get("cost_entries", []) if item.get("id") == reservation.get("cost_entry_id")),
            None,
        )
        if entry is not None:
            return entry
        raise ValueError(f"Reservation {reservation_id} is reconciled without a cost entry.")
    if reservation["status"] != "reserved":
        raise ValueError(f"Reservation {reservation_id} is {reservation['status']} and cannot be reconciled.")

    held = _amount(reservation.get("remaining_usd", reservation.get("amount_usd", 0.0)))
    if actual_usd is None:
        actual = held
        estimated = True
    else:
        actual = _amount(actual_usd)
    if actual < 0:
        raise ValueError("Reconciled cost must be zero or greater.")

    reserved_campaign_id = str(reservation.get("campaign_id") or "")
    supplied_campaign_id = str(campaign_id or "")
    if supplied_campaign_id and reserved_campaign_id and supplied_campaign_id != reserved_campaign_id:
        raise ValueError(
            f"Reservation {reservation_id} belongs to Revenue Sprint {reserved_campaign_id!r}, "
            f"not {supplied_campaign_id!r}."
        )
    effective_campaign_id = reserved_campaign_id or supplied_campaign_id

    state["company"]["reserved_today_usd"] = _amount(
        max(0.0, state["company"].get("reserved_today_usd", 0.0) - held)
    )
    attribution = _attribution(
        context or reservation.get("context"),
        project_id or reservation.get("project_id"),
        task_id or reservation.get("task_id"),
        agent or reservation.get("agent"),
        model or reservation.get("model"),
        reason or reservation.get("reason"),
        effective_campaign_id,
    )
    entry = _record_cost_entry_in_state(
        state,
        actual,
        reservation_id=reservation_id,
        usage_records=usage_records,
        model_route_decisions=model_route_decisions,
        estimated=estimated,
        campaign_date=reservation.get("campaign_date") or reservation.get("budget_date"),
        **attribution,
    )
    reservation["status"] = "reconciled"
    reservation["remaining_usd"] = 0.0
    reservation["reconciled_usd"] = actual
    reservation["cost_basis"] = entry["cost_basis"]
    reservation["cost_entry_id"] = entry["id"]
    reservation["updated_at"] = _now().isoformat()
    add_event(
        state,
        "budget_reconciled",
        f"Reconciled {entry['cost_basis']} cost for {attribution['context']}.",
        project_id=attribution["project_id"],
        task_id=attribution["task_id"],
        amount_usd=actual,
    )
    if effective_campaign_id:
        sprint = active_revenue_sprint(state, effective_campaign_id)
        if sprint is not None:
            snapshot = revenue_sprint_budget_snapshot(state, effective_campaign_id)
            if actual > held + 0.000001:
                sprint["status"] = "stopped"
                sprint["stop_reason"] = "ai_budget_reconciliation_breach"
                sprint["finished_at"] = _now().isoformat()
                sprint["updated_at"] = sprint["finished_at"]
            elif snapshot["remaining_total_usd"] <= 0:
                sprint["status"] = "stopped"
                sprint["stop_reason"] = "campaign_ai_budget_exhausted"
                sprint["finished_at"] = _now().isoformat()
                sprint["updated_at"] = sprint["finished_at"]
    return entry


def reconcile_budget(
    reservation_id,
    actual_usd=None,
    path=COMPANY_STATE_FILE,
    *,
    usage_records=None,
    model_route_decisions=None,
    estimated=False,
    context=None,
    project_id=None,
    task_id=None,
    agent=None,
    model=None,
    reason="",
    campaign_id=None,
):
    """Atomically replace a reservation with measured (or labelled estimated) cost."""
    with _state_transaction(path) as state:
        entry = _reconcile_budget_in_state(
            state,
            reservation_id,
            actual_usd,
            usage_records=usage_records,
            model_route_decisions=model_route_decisions,
            estimated=estimated,
            context=context,
            project_id=project_id,
            task_id=task_id,
            agent=agent,
            model=model,
            reason=reason,
            campaign_id=campaign_id,
        )
        result = deepcopy(entry)
    return result


def _release_budget_in_state(state, reservation_id, reason=""):
    reservation = _find_reservation(state, reservation_id)
    if reservation is None:
        raise KeyError(f"Budget reservation not found: {reservation_id}")
    if reservation["status"] != "reserved":
        return reservation
    held = _amount(reservation.get("remaining_usd", reservation.get("amount_usd", 0.0)))
    state["company"]["reserved_today_usd"] = _amount(
        max(0.0, state["company"].get("reserved_today_usd", 0.0) - held)
    )
    reservation["status"] = "released"
    reservation["remaining_usd"] = 0.0
    reservation["release_reason"] = str(reason or "")
    reservation["updated_at"] = _now().isoformat()
    add_event(
        state,
        "budget_released",
        reason or f"Released budget for {reservation.get('context', 'task')}.",
        project_id=reservation.get("project_id"),
        task_id=reservation.get("task_id"),
        amount_usd=held,
    )
    return reservation


def release_budget(reservation_id, path=COMPANY_STATE_FILE, *, reason=""):
    """Atomically release an unused hold. Repeated calls are idempotent."""
    with _state_transaction(path) as state:
        reservation = _release_budget_in_state(state, reservation_id, reason)
        result = deepcopy(reservation)
    return result


def _validate_revenue_sprint_product(state, product):
    requested = _normalize_sprint_product(product)
    if not requested["project_id"] or not requested["gumroad_url"] or not requested["title"]:
        raise RevenueSprintError(
            "Revenue Sprint product requires exact project_id, gumroad_url, and title values."
        )
    matches = []
    for registered in state.get("products", []):
        registered_id = str(registered.get("gumroad_product_id") or "").strip()
        registered_url = str(registered.get("gumroad_url") or "").strip().rstrip("/")
        if (
            requested["gumroad_product_id"]
            and registered_id == requested["gumroad_product_id"]
        ) or (registered_url and registered_url == requested["gumroad_url"]):
            matches.append(registered)
    if len(matches) != 1:
        raise RevenueSprintError(
            "Revenue Sprint product must match exactly one existing Company Mode Gumroad product."
        )
    registered = matches[0]
    if str(registered.get("project_id") or "") != requested["project_id"]:
        raise RevenueSprintError("Revenue Sprint product project_id does not match the product registry.")
    if str(registered.get("title") or "").strip() != requested["title"]:
        raise RevenueSprintError("Revenue Sprint product title does not match the product registry.")
    registered_id = str(registered.get("gumroad_product_id") or "").strip()
    registered_url = str(registered.get("gumroad_url") or "").strip().rstrip("/")
    if requested["gumroad_url"] != registered_url:
        raise RevenueSprintError("Revenue Sprint Gumroad URL does not match the product registry.")
    if requested["gumroad_product_id"] and requested["gumroad_product_id"] != registered_id:
        raise RevenueSprintError("Revenue Sprint Gumroad product ID does not match the product registry.")
    requested["gumroad_product_id"] = requested["gumroad_product_id"] or registered_id
    if requested["ownership"] != "company_owned":
        raise RevenueSprintError(
            "Revenue Sprint product requires an explicitly confirmed company-owned Gumroad seller; personal sellers cannot be used as fallback."
        )
    if requested["personal_fallback_allowed"]:
        raise RevenueSprintError(
            "Revenue Sprint products cannot authorize fallback to a personal seller account."
        )
    return requested


def _validate_revenue_sprint_channel(channel):
    normalized = _normalize_sprint_channel(channel)
    if not all(normalized[field] for field in ("type", "account_id", "destination_scope")):
        raise RevenueSprintError(
            "Revenue Sprint channel requires exact type, account_id, and destination_scope values."
        )
    if "*" in normalized["destination_scope"]:
        raise RevenueSprintError("Revenue Sprint destination_scope cannot contain a wildcard.")
    if normalized["ownership"] != "company_owned":
        raise RevenueSprintError(
            "Revenue Sprint channel requires an explicitly confirmed company-owned promotional account; "
            "personal accounts cannot be used as fallback."
        )
    if normalized["personal_fallback_allowed"]:
        raise RevenueSprintError(
            "Revenue Sprint channels cannot authorize fallback to a personal account."
        )
    return normalized


def _validate_revenue_action_policy(policy):
    raw = policy if isinstance(policy, dict) else {}
    requested_types = [
        str(action_type or "").strip().lower()
        for action_type in _list(raw.get("allowed_action_types"))
    ]
    unsupported_types = sorted({
        action_type for action_type in requested_types
        if action_type not in REVENUE_ACTION_TYPES
    })
    if unsupported_types:
        raise RevenueSprintError(
            f"Unsupported Revenue Sprint action types: {unsupported_types}"
        )
    normalized = _normalize_action_policy(policy)
    if not normalized["revision"]:
        raise RevenueSprintError("Revenue Sprint automation policy requires an exact non-empty revision.")
    if not normalized["approved_at"] or not normalized["approved_by"]:
        raise RevenueSprintError(
            "Revenue Sprint automation policy requires approved_at and approved_by evidence."
        )
    if not normalized["allowed_action_types"]:
        raise RevenueSprintError("Revenue Sprint automation policy must allow at least one exact action type.")
    for action_type in normalized["allowed_action_types"]:
        targets = normalized["allowed_targets"].get(action_type, [])
        if not targets or any("*" in target for target in targets):
            raise RevenueSprintError(
                f"Revenue action {action_type!r} requires one or more exact non-wildcard targets."
            )
        daily_cap = normalized["daily_action_caps"].get(action_type, 0)
        total_cap = normalized["total_action_caps"].get(action_type, 0)
        if daily_cap <= 0 or total_cap <= 0 or daily_cap > total_cap:
            raise RevenueSprintError(
                f"Revenue action {action_type!r} requires positive daily/total count caps with daily <= total."
            )
    if "purchase" in normalized["allowed_action_types"]:
        if (
            normalized["purchase_daily_cap_usd"] <= 0
            or normalized["purchase_total_cap_usd"] <= 0
            or normalized["purchase_daily_cap_usd"] > normalized["purchase_total_cap_usd"]
        ):
            raise RevenueSprintError(
                "Automated purchases require separate positive daily and total USD caps."
            )
    elif normalized["purchase_daily_cap_usd"] or normalized["purchase_total_cap_usd"]:
        raise RevenueSprintError(
            "Purchase caps must be zero unless purchase is explicitly allowlisted."
        )
    return normalized


def start_revenue_sprint(
    product,
    channel,
    automation_policy,
    path=COMPANY_STATE_FILE,
    *,
    total_ai_budget_usd=DEFAULT_REVENUE_SPRINT_TOTAL_AI_BUDGET_USD,
    daily_ai_budget_usd=DEFAULT_REVENUE_SPRINT_DAILY_AI_BUDGET_USD,
    max_run_days=DEFAULT_REVENUE_SPRINT_RUN_DAYS,
    max_consecutive_no_progress_days=DEFAULT_REVENUE_SPRINT_NO_PROGRESS_LIMIT,
    checkpoint_policy=None,
    sprint_id=None,
    timezone_name=None,
):
    """Create the one active, owner-preauthorized Revenue Sprint."""

    total_cap = _amount(total_ai_budget_usd)
    daily_cap = _amount(daily_ai_budget_usd)
    try:
        run_limit = int(max_run_days)
        no_progress_limit = int(max_consecutive_no_progress_days)
    except (TypeError, ValueError) as exc:
        raise RevenueSprintError("Revenue Sprint run limits must be positive integers.") from exc
    if total_cap <= 0 or daily_cap <= 0 or daily_cap > total_cap:
        raise RevenueSprintError("Revenue Sprint AI budgets must be positive and daily cannot exceed total.")
    if run_limit <= 0 or no_progress_limit <= 0:
        raise RevenueSprintError("Revenue Sprint run limits must be positive integers.")
    zone_name = str(timezone_name or budget_timezone_name()).strip()
    try:
        ZoneInfo(zone_name)
    except ZoneInfoNotFoundError as exc:
        raise RevenueSprintError(f"Invalid Revenue Sprint timezone {zone_name!r}.") from exc

    with _state_transaction(path) as state:
        active = [
            item
            for item in state.get("revenue_sprints", [])
            if item.get("status") in REVENUE_SPRINT_ACTIVE_STATUSES
        ]
        if active:
            raise RevenueSprintError(f"Revenue Sprint {active[0]['id']!r} is already active.")
        normalized_product = _validate_revenue_sprint_product(state, product)
        normalized_channel = _validate_revenue_sprint_channel(channel)
        normalized_policy = _validate_revenue_action_policy(automation_policy)
        candidate_id = str(sprint_id or f"sprint_{uuid.uuid4().hex[:12]}").strip()
        if not candidate_id:
            raise RevenueSprintError("Revenue Sprint ID cannot be empty.")
        if any(str(item.get("id") or "") == candidate_id for item in state.get("revenue_sprints", [])):
            raise RevenueSprintError(f"Revenue Sprint ID already exists: {candidate_id}")
        now = _now().isoformat()
        sprint = _normalize_revenue_sprint({
            "id": candidate_id,
            "status": "active",
            "timezone": zone_name,
            "total_ai_budget_usd": total_cap,
            "daily_ai_budget_usd": daily_cap,
            "max_run_days": run_limit,
            "max_consecutive_no_progress_days": no_progress_limit,
            "product": normalized_product,
            "channel": normalized_channel,
            "automation_policy": normalized_policy,
            "checkpoint_policy": checkpoint_policy or {},
            "created_at": now,
            "started_at": now,
            "updated_at": now,
        })
        state.setdefault("revenue_sprints", []).append(sprint)
        state["company"]["active_revenue_sprint_id"] = sprint["id"]
        add_event(state, "revenue_sprint_started", f"Started Revenue Sprint {sprint['id']}.")
        result = deepcopy(sprint)
    return result


def stop_revenue_sprint(path=COMPANY_STATE_FILE, *, sprint_id=None, reason="owner_stopped"):
    bounded_reason = str(reason or "owner_stopped").strip()[:240]
    with _state_transaction(path) as state:
        sprint = active_revenue_sprint(state, sprint_id)
        if sprint is None:
            raise RevenueSprintError("Revenue Sprint was not found.")
        if sprint.get("status") not in REVENUE_SPRINT_TERMINAL_STATUSES:
            now = _now().isoformat()
            sprint["status"] = "stopped"
            sprint["stop_reason"] = bounded_reason
            sprint["finished_at"] = now
            sprint["updated_at"] = now
            add_event(state, "revenue_sprint_stopped", f"Stopped Revenue Sprint {sprint['id']}: {bounded_reason}")
        if state["company"].get("active_revenue_sprint_id") == sprint["id"]:
            state["company"]["active_revenue_sprint_id"] = None
        result = deepcopy(sprint)
    return result


def revenue_sprint_status(path=COMPANY_STATE_FILE, *, sprint_id=None, at=None):
    state = load_state(path)
    sprint = active_revenue_sprint(state, sprint_id)
    if sprint is None:
        return {"active": False, "campaign_id": None, "status": "inactive", "budget": revenue_sprint_budget_snapshot(state)}
    result = deepcopy(sprint)
    result["active"] = sprint.get("status") in REVENUE_SPRINT_ACTIVE_STATUSES
    result["campaign_id"] = sprint["id"]
    result["budget"] = revenue_sprint_budget_snapshot(state, sprint["id"], at)
    result["economic_verdict"] = _revenue_sprint_economic_verdict(state, sprint, at)
    return result


def _stop_sprint_in_state(state, sprint, reason, at=None):
    finished = _sprint_moment(sprint, at).isoformat()
    sprint["status"] = "stopped"
    sprint["stop_reason"] = str(reason or "stopped")[:240]
    sprint["finished_at"] = finished
    sprint["updated_at"] = finished
    if state["company"].get("active_revenue_sprint_id") == sprint["id"]:
        state["company"]["active_revenue_sprint_id"] = None


def _normalize_experiment(experiment):
    if not isinstance(experiment, dict):
        raise RevenueSprintError("A run claim requires one structured measurable experiment.")
    normalized = {
        "id": str(experiment.get("id") or "").strip(),
        "hypothesis": str(experiment.get("hypothesis") or "").strip(),
        "metric": str(experiment.get("metric") or "").strip(),
        "success_threshold": str(experiment.get("success_threshold") or "").strip(),
        "action_type": str(experiment.get("action_type") or "").strip().lower(),
    }
    if not all(normalized.values()):
        raise RevenueSprintError(
            "Experiment requires exact id, hypothesis, metric, success_threshold, and action_type."
        )
    if normalized["action_type"] not in REVENUE_ACTION_TYPES:
        raise RevenueSprintError(f"Unsupported experiment action type: {normalized['action_type']!r}.")
    changed_variable = str(experiment.get("changed_variable") or "").strip().lower()
    if changed_variable:
        if changed_variable not in {"target_pain", "proof_format", "call_to_action"}:
            raise RevenueSprintError(
                "Experiment changed_variable must be target_pain, proof_format, or call_to_action."
            )
        normalized["changed_variable"] = changed_variable
    evidence_basis = str(experiment.get("evidence_basis") or "").strip()[:1000]
    if evidence_basis:
        normalized["evidence_basis"] = evidence_basis
    return normalized


def _find_sprint_run(sprint, run_id):
    return next(
        (entry for entry in sprint.get("run_days", []) if str(entry.get("run_id") or "") == str(run_id)),
        None,
    )


def claim_revenue_sprint_run(
    run_id,
    experiment,
    path=COMPANY_STATE_FILE,
    *,
    sprint_id=None,
    at=None,
):
    """Claim one date-unique Monday-Friday experiment run, idempotently."""

    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        raise RevenueSprintError("Revenue Sprint run ID cannot be empty.")
    normalized_experiment = _normalize_experiment(experiment)
    with _state_transaction(path) as state:
        sprint = active_revenue_sprint(state, sprint_id)
        if sprint is None or sprint.get("status") not in REVENUE_SPRINT_ACTIVE_STATUSES:
            raise RevenueSprintError("No active Revenue Sprint can claim this run.")
        if normalized_experiment["action_type"] not in sprint["automation_policy"]["allowed_action_types"]:
            raise RevenueSprintError(
                f"Experiment action {normalized_experiment['action_type']!r} is not allowlisted."
            )
        moment = _sprint_moment(sprint, at)
        if moment.weekday() >= 5:
            raise RevenueSprintError("Revenue Sprint run days are Monday through Friday only.")
        campaign_date = moment.date().isoformat()
        prior_run = _find_sprint_run(sprint, normalized_run_id)
        if prior_run is not None:
            prior_experiment = next(
                (
                    entry for entry in sprint.get("experiments", [])
                    if entry.get("id") == prior_run.get("experiment_id")
                ),
                None,
            )
            same_experiment = bool(prior_experiment) and all(
                prior_experiment.get(field) == value
                for field, value in normalized_experiment.items()
            )
            if (
                prior_run.get("date") == campaign_date
                and same_experiment
            ):
                result = deepcopy(prior_run)
                result["idempotent_replay"] = True
                return result
            raise RevenueSprintError(f"Revenue Sprint run ID already exists: {normalized_run_id}")
        same_date = next(
            (entry for entry in sprint["run_days"] if entry.get("date") == campaign_date),
            None,
        )
        if same_date is not None:
            raise RevenueSprintError(
                f"Revenue Sprint date {campaign_date} is already claimed by {same_date.get('run_id')}."
            )
        if sprint.get("pivot_required"):
            raise RevenueSprintError("The day-5 pivot must be recorded before another experiment can start.")
        if len(sprint["run_days"]) >= int(sprint["max_run_days"]):
            _stop_sprint_in_state(state, sprint, "run_day_limit_reached", at)
            raise RevenueSprintError("Revenue Sprint run-day limit has been reached.")
        budget = revenue_sprint_budget_snapshot(state, sprint["id"], at)
        if budget["remaining_total_usd"] <= 0:
            _stop_sprint_in_state(state, sprint, "campaign_ai_budget_exhausted", at)
            raise RevenueSprintError("Revenue Sprint AI budget is exhausted.")
        if budget["ordinary_remaining_today_usd"] <= 0:
            raise RevenueSprintError(
                "Revenue Sprint ordinary daily AI budget is exhausted; the emergency reserve is preserved."
            )
        if any(entry.get("id") == normalized_experiment["id"] for entry in sprint["experiments"]):
            raise RevenueSprintError(
                f"Revenue Sprint experiment ID already exists: {normalized_experiment['id']}"
            )
        ordinal = len(sprint["run_days"]) + 1
        timestamp = moment.isoformat()
        experiment_record = {
            **normalized_experiment,
            "run_id": normalized_run_id,
            "date": campaign_date,
            "ordinal": ordinal,
            "status": "claimed",
            "result": "",
            "created_at": timestamp,
            "completed_at": None,
        }
        run_record = {
            "date": campaign_date,
            "ordinal": ordinal,
            "run_id": normalized_run_id,
            "experiment_id": normalized_experiment["id"],
            "status": "claimed",
            "outcome": "",
            "progress": None,
            "claimed_at": timestamp,
            "completed_at": None,
            "before_snapshot_id": None,
            "after_snapshot_id": None,
        }
        sprint["experiments"].append(experiment_record)
        sprint["run_days"].append(run_record)
        sprint["updated_at"] = timestamp
        add_event(state, "revenue_sprint_run_claimed", f"Claimed Revenue Sprint day {ordinal}.")
        result = deepcopy(run_record)
    return result


def _signal_count(sprint, signal_types):
    allowed = {str(value).lower() for value in signal_types}
    return sum(
        max(0, int(entry.get("count", 0) or 0))
        for entry in sprint.get("signals", [])
        if str(entry.get("type") or "").lower() in allowed
    )


def _campaign_sales_delta(sprint):
    snapshots = [entry for entry in sprint.get("revenue_snapshots", []) if isinstance(entry, dict)]
    if len(snapshots) < 2:
        return 0
    return max(0, int(snapshots[-1].get("sales_count", 0) or 0) - int(snapshots[0].get("sales_count", 0) or 0))


def _revenue_sprint_economic_verdict(state, sprint, at=None):
    policy = sprint["checkpoint_policy"]
    window_days = int(policy["trailing_window_days"])
    run_dates = list(dict.fromkeys(
        str(entry.get("date") or "")
        for entry in sprint.get("run_days", [])
        if str(entry.get("date") or "")
    ))
    observed_dates = run_dates[-window_days:]
    snapshots = [
        entry for entry in sprint.get("revenue_snapshots", [])
        if isinstance(entry, dict)
    ]
    trailing_gross = 0.0
    campaign_gross = 0.0
    if snapshots:
        latest = snapshots[-1]
        campaign_gross = _amount(
            _amount(latest.get("revenue_usd")) - _amount(snapshots[0].get("revenue_usd"))
        )
        if observed_dates:
            start_date = observed_dates[0]
            baselines = [
                entry for entry in snapshots
                if str(entry.get("campaign_date") or "") < start_date
                or (
                    str(entry.get("campaign_date") or "") == start_date
                    and entry.get("phase") == "before"
                )
            ]
            baseline = baselines[-1] if baselines else snapshots[0]
            trailing_gross = _amount(
                _amount(latest.get("revenue_usd")) - _amount(baseline.get("revenue_usd"))
            )
    trailing_gross = max(0.0, trailing_gross)
    campaign_gross = max(0.0, campaign_gross)
    gross_per_day = _amount(trailing_gross / window_days)
    campaign_spend = _campaign_commitments(state, sprint["id"])["spent_usd"]
    campaign_purchase_spend = _amount(sum(
        _purchase_commitment(entry)
        for entry in sprint.get("action_journal", [])
        if isinstance(entry, dict)
    ))
    observed_costs = _amount(campaign_spend + campaign_purchase_spend)
    observed_contribution = _amount(campaign_gross - observed_costs)
    meets_contribution = (
        observed_contribution >= 0
        if policy["require_nonnegative_contribution"]
        else True
    )
    target_demonstrated = bool(
        len(observed_dates) >= window_days
        and trailing_gross >= policy["minimum_trailing_gross_revenue_usd"]
        and gross_per_day >= policy["minimum_gross_revenue_usd_per_day"]
        and meets_contribution
    )
    return {
        "trailing_window_days": window_days,
        "observed_run_days": len(observed_dates),
        "trailing_gross_revenue_usd": trailing_gross,
        "trailing_gross_revenue_usd_per_day": gross_per_day,
        "minimum_gross_revenue_usd_per_day": policy["minimum_gross_revenue_usd_per_day"],
        "minimum_trailing_gross_revenue_usd": policy["minimum_trailing_gross_revenue_usd"],
        "campaign_gross_revenue_usd": campaign_gross,
        "campaign_ai_spend_usd": campaign_spend,
        "campaign_purchase_spend_usd": campaign_purchase_spend,
        "observed_costs_before_fees_usd": observed_costs,
        "observed_contribution_before_fees_usd": observed_contribution,
        "require_nonnegative_contribution": policy["require_nonnegative_contribution"],
        "target_demonstrated": target_demonstrated,
        "self_sustaining_verified": False,
        "fee_data_available": False,
        "scope": "before_unavailable_gumroad_and_infrastructure_fees",
        "evaluated_at": _sprint_moment(sprint, at).isoformat(),
    }


def _record_checkpoint_once(sprint, day, decision, evidence, at=None):
    existing = next(
        (entry for entry in sprint["checkpoint_results"] if int(entry.get("day", 0) or 0) == day),
        None,
    )
    if existing is not None:
        return existing
    entry = {
        "day": day,
        "decision": decision,
        "evidence": deepcopy(evidence),
        "evaluated_at": _sprint_moment(sprint, at).isoformat(),
    }
    sprint["checkpoint_results"].append(entry)
    return entry


def _evaluate_sprint_after_run(state, sprint, run_record, at=None):
    ordinal = int(run_record["ordinal"])
    policy = sprint["checkpoint_policy"]
    if ordinal >= 5:
        interest_count = _signal_count(sprint, policy["day5_interest_signal_types"])
        if not any(int(entry.get("day", 0) or 0) == 5 for entry in sprint["checkpoint_results"]):
            if interest_count >= int(policy["day5_min_interest_count"]):
                _record_checkpoint_once(sprint, 5, "continue", {"meaningful_interest_count": interest_count}, at)
            else:
                _record_checkpoint_once(sprint, 5, "pivot", {"meaningful_interest_count": interest_count}, at)
                sprint["pivot_required"] = True
                sprint["phase"] = "pivot"

    if ordinal >= 15 and not any(
        int(entry.get("day", 0) or 0) == 15 for entry in sprint["checkpoint_results"]
    ):
        sales = _campaign_sales_delta(sprint)
        strong_intent = _signal_count(sprint, policy["day15_strong_intent_signal_types"])
        evidence = {"campaign_sales": sales, "strong_intent_count": strong_intent}
        if sales >= int(policy["day15_min_sales"]) or strong_intent >= int(policy["day15_min_strong_intent_count"]):
            _record_checkpoint_once(sprint, 15, "continue", evidence, at)
        else:
            _record_checkpoint_once(sprint, 15, "stop", evidence, at)
            _stop_sprint_in_state(state, sprint, "day15_no_sale_or_strong_intent", at)

    if ordinal >= int(sprint["max_run_days"]):
        day = int(sprint["max_run_days"])
        _record_checkpoint_once(
            sprint,
            day,
            "stop",
            {
                "run_days_used": ordinal,
                "economic_verdict": _revenue_sprint_economic_verdict(state, sprint, at),
            },
            at,
        )
        _stop_sprint_in_state(
            state,
            sprint,
            "day20_limit_reached" if day == 20 else "run_day_limit_reached",
            at,
        )
    elif sprint["consecutive_no_progress_days"] >= int(sprint["max_consecutive_no_progress_days"]):
        _stop_sprint_in_state(state, sprint, "repeated_no_progress", at)


def complete_revenue_sprint_run(
    run_id,
    outcome,
    path=COMPANY_STATE_FILE,
    *,
    sprint_id=None,
    progress=None,
    result="",
    at=None,
):
    normalized_outcome = str(outcome or "").strip().lower()
    normalized_result = str(result or "")[:1000]
    progress_source = "automatic" if progress is None else "explicit"
    if normalized_outcome not in {"succeeded", "failed", "deferred", "needs_human", "cancelled"}:
        raise RevenueSprintError(f"Unsupported Revenue Sprint run outcome: {normalized_outcome!r}.")
    with _state_transaction(path) as state:
        sprint = active_revenue_sprint(state, sprint_id)
        if sprint is None:
            raise RevenueSprintError("Revenue Sprint was not found.")
        run_record = _find_sprint_run(sprint, run_id)
        if run_record is None:
            raise RevenueSprintError(f"Revenue Sprint run was not found: {run_id}")
        if run_record.get("status") == "completed":
            stored_source = run_record.get("progress_source")
            same_progress_request = (
                stored_source is None
                or (
                    stored_source == progress_source
                    and (progress is None or run_record.get("progress") == bool(progress))
                )
            )
            same_result = (
                "result" not in run_record
                or run_record.get("result") == normalized_result
            )
            if (
                run_record.get("outcome") == normalized_outcome
                and same_progress_request
                and same_result
            ):
                replay = deepcopy(run_record)
                replay["idempotent_replay"] = True
                return replay
            raise RevenueSprintError(f"Revenue Sprint run {run_id!r} is already complete.")
        automatic_progress = any(
            str(entry.get("run_id") or "") == str(run_id)
            and (int(entry.get("count", 0) or 0) > 0 or _amount(entry.get("value_usd")) > 0)
            for entry in sprint.get("signals", [])
        ) or any(
            str(entry.get("run_id") or "") == str(run_id)
            and (
                int(entry.get("sales_delta", 0) or 0) > 0
                or _amount(entry.get("revenue_delta_usd")) > 0
            )
            for entry in sprint.get("revenue_snapshots", [])
        )
        made_progress = automatic_progress if progress is None else bool(progress)
        timestamp = _sprint_moment(sprint, at).isoformat()
        run_record.update(
            status="completed",
            outcome=normalized_outcome,
            progress=made_progress,
            progress_source=progress_source,
            result=normalized_result,
            completed_at=timestamp,
        )
        experiment = next(
            (entry for entry in sprint["experiments"] if entry.get("id") == run_record.get("experiment_id")),
            None,
        )
        if experiment is not None:
            experiment["status"] = normalized_outcome
            experiment["result"] = normalized_result
            experiment["completed_at"] = timestamp
        sprint["consecutive_no_progress_days"] = (
            0 if made_progress else int(sprint.get("consecutive_no_progress_days", 0)) + 1
        )
        sprint["updated_at"] = timestamp
        _evaluate_sprint_after_run(state, sprint, run_record, at)
        add_event(state, "revenue_sprint_run_completed", f"Completed Revenue Sprint day {run_record['ordinal']}.")
        response = deepcopy(run_record)
        response["campaign_status"] = sprint["status"]
        response["stop_reason"] = sprint.get("stop_reason", "")
        response["pivot_required"] = bool(sprint.get("pivot_required"))
    return response


def record_revenue_sprint_pivot(
    description,
    path=COMPANY_STATE_FILE,
    *,
    sprint_id=None,
    run_id="",
    at=None,
):
    bounded = str(description or "").strip()[:1000]
    if not bounded:
        raise RevenueSprintError("A Revenue Sprint pivot requires a non-empty description.")
    with _state_transaction(path) as state:
        sprint = active_revenue_sprint(state, sprint_id)
        if sprint is None or sprint.get("status") not in REVENUE_SPRINT_ACTIVE_STATUSES:
            raise RevenueSprintError("No active Revenue Sprint can record a pivot.")
        if not sprint.get("pivot_required"):
            raise RevenueSprintError("Revenue Sprint does not currently require a pivot.")
        entry = {
            "run_id": str(run_id or ""),
            "description": bounded,
            "recorded_at": _sprint_moment(sprint, at).isoformat(),
        }
        sprint["pivot_history"].append(entry)
        sprint["pivot_required"] = False
        sprint["phase"] = "iterate"
        sprint["updated_at"] = entry["recorded_at"]
        result = deepcopy(entry)
    return result


def _record_revenue_signal_in_state(
    sprint,
    signal_type,
    *,
    run_id="",
    count=1,
    value_usd=0.0,
    evidence="",
    at=None,
    signal_id=None,
):
    normalized_type = str(signal_type or "").strip().lower()
    if normalized_type not in REVENUE_SIGNAL_TYPES:
        raise RevenueSprintError(f"Unsupported Revenue Sprint signal type: {normalized_type!r}.")
    try:
        normalized_count = int(count)
    except (TypeError, ValueError) as exc:
        raise RevenueSprintError("Revenue Sprint signal count must be a positive integer.") from exc
    normalized_value = _amount(value_usd)
    if normalized_count <= 0 or normalized_value < 0:
        raise RevenueSprintError("Revenue Sprint signal count must be positive and value cannot be negative.")
    target_id = str(signal_id or f"signal_{uuid.uuid4().hex[:12]}")
    existing = next((entry for entry in sprint["signals"] if entry.get("id") == target_id), None)
    if existing is not None:
        return existing
    entry = {
        "id": target_id,
        "type": normalized_type,
        "run_id": str(run_id or ""),
        "count": normalized_count,
        "value_usd": normalized_value,
        "evidence": str(evidence or "")[:600],
        "observed_at": _sprint_moment(sprint, at).isoformat(),
    }
    sprint["signals"].append(entry)
    return entry


def record_revenue_signal(
    signal_type,
    path=COMPANY_STATE_FILE,
    *,
    sprint_id=None,
    run_id="",
    count=1,
    value_usd=0.0,
    evidence="",
    at=None,
):
    with _state_transaction(path) as state:
        sprint = active_revenue_sprint(state, sprint_id)
        if sprint is None:
            raise RevenueSprintError("Revenue Sprint was not found.")
        if run_id and _find_sprint_run(sprint, run_id) is None:
            raise RevenueSprintError(f"Revenue Sprint run was not found: {run_id}")
        entry = _record_revenue_signal_in_state(
            sprint,
            signal_type,
            run_id=run_id,
            count=count,
            value_usd=value_usd,
            evidence=evidence,
            at=at,
        )
        sprint["updated_at"] = entry["observed_at"]
        result = deepcopy(entry)
    return result


def record_revenue_snapshot(
    gumroad_products,
    phase,
    run_id,
    path=COMPANY_STATE_FILE,
    *,
    sprint_id=None,
    at=None,
):
    """Persist one exact cumulative Gumroad before/after snapshot and deltas."""

    normalized_phase = str(phase or "").strip().lower()
    if normalized_phase not in {"before", "after"}:
        raise RevenueSprintError("Revenue snapshot phase must be 'before' or 'after'.")
    with _state_transaction(path) as state:
        sprint = active_revenue_sprint(state, sprint_id)
        if sprint is None:
            raise RevenueSprintError("Revenue Sprint was not found.")
        run_record = _find_sprint_run(sprint, run_id)
        if run_record is None:
            raise RevenueSprintError(f"Revenue Sprint run was not found: {run_id}")
        campaign_date = _sprint_date(sprint, at)
        if run_record.get("date") != campaign_date:
            raise RevenueSprintError(
                "Revenue snapshot must use the exact date of its claimed Revenue Sprint run."
            )
        if normalized_phase == "after" and not run_record.get("before_snapshot_id"):
            raise RevenueSprintError(
                "Revenue Sprint after snapshot requires the run's before snapshot first."
            )
        expected = sprint["product"]
        matches = []
        for product in gumroad_products or []:
            product_id = str(product.get("id") or product.get("gumroad_product_id") or "").strip()
            product_url = str(product.get("short_url") or product.get("gumroad_url") or "").strip().rstrip("/")
            if (
                expected.get("gumroad_product_id")
                and product_id == expected["gumroad_product_id"]
            ) or (product_url and product_url == expected.get("gumroad_url")):
                matches.append(product)
        if len(matches) != 1:
            raise RevenueSprintError(
                "Live revenue snapshot must match exactly one configured Gumroad product."
            )
        product = matches[0]
        product_id = str(product.get("id") or product.get("gumroad_product_id") or "").strip()
        product_url = str(product.get("short_url") or product.get("gumroad_url") or "").strip().rstrip("/")
        if expected.get("gumroad_product_id") and product_id != expected["gumroad_product_id"]:
            raise RevenueSprintError(
                "Live revenue snapshot Gumroad product ID does not match the configured product."
            )
        if product_url != expected.get("gumroad_url"):
            raise RevenueSprintError(
                "Live revenue snapshot Gumroad URL does not match the configured product."
            )
        try:
            sales_count = max(0, int(product.get("sales_count", 0) or 0))
        except (TypeError, ValueError) as exc:
            raise RevenueSprintError("Gumroad sales_count must be a non-negative integer.") from exc
        if product.get("sales_usd_cents") is not None:
            revenue_usd = _amount(_amount(product.get("sales_usd_cents")) / 100.0)
        else:
            revenue_usd = _amount(product.get("revenue_usd", 0.0))
        if revenue_usd < 0:
            raise RevenueSprintError("Gumroad cumulative revenue cannot be negative.")
        duplicate = next(
            (
                entry
                for entry in sprint["revenue_snapshots"]
                if entry.get("run_id") == str(run_id) and entry.get("phase") == normalized_phase
            ),
            None,
        )
        if duplicate is not None:
            if duplicate.get("sales_count") == sales_count and duplicate.get("revenue_usd") == revenue_usd:
                result = deepcopy(duplicate)
                result["idempotent_replay"] = True
                return result
            raise RevenueSprintError(
                f"Revenue snapshot {normalized_phase!r} for run {run_id!r} already exists with different totals."
            )
        previous = sprint["revenue_snapshots"][-1] if sprint["revenue_snapshots"] else None
        sales_delta = sales_count - int(previous.get("sales_count", 0) or 0) if previous else 0
        revenue_delta = _amount(revenue_usd - _amount(previous.get("revenue_usd"))) if previous else 0.0
        snapshot = {
            "id": f"snapshot_{uuid.uuid4().hex[:12]}",
            "run_id": str(run_id),
            "phase": normalized_phase,
            "campaign_date": _sprint_date(sprint, at),
            "gumroad_product_id": str(product.get("id") or product.get("gumroad_product_id") or ""),
            "gumroad_url": str(product.get("short_url") or product.get("gumroad_url") or "").rstrip("/"),
            "sales_count": sales_count,
            "revenue_usd": revenue_usd,
            "sales_delta": sales_delta,
            "revenue_delta_usd": revenue_delta,
            "captured_at": _sprint_moment(sprint, at).isoformat(),
        }
        sprint["revenue_snapshots"].append(snapshot)
        run_record[f"{normalized_phase}_snapshot_id"] = snapshot["id"]
        if sales_delta > 0:
            # A sale first observed in the next run's pre-action snapshot happened
            # after the prior run's last snapshot, so attribute it to that earlier
            # measurement window instead of claiming the new post caused it.
            signal_run_id = str(run_id)
            if normalized_phase == "before" and previous is not None:
                signal_run_id = str(previous.get("run_id") or "")
            _record_revenue_signal_in_state(
                sprint,
                "sale",
                run_id=signal_run_id,
                count=sales_delta,
                value_usd=max(0.0, revenue_delta),
                evidence=f"Gumroad cumulative snapshot {snapshot['id']}",
                at=at,
                signal_id=f"signal_{snapshot['id']}",
            )
        sprint["updated_at"] = snapshot["captured_at"]
        result = deepcopy(snapshot)
    return result


def _action_counts(sprint, action_type, campaign_date):
    counted_statuses = {"claimed", "succeeded", "failed", "uncertain"}
    entries = [
        entry
        for entry in sprint.get("action_journal", [])
        if entry.get("action_type") == action_type and entry.get("status") in counted_statuses
    ]
    return len(entries), sum(entry.get("budget_date") == campaign_date for entry in entries)


def _purchase_commitment(entry):
    if entry.get("action_type") != "purchase" or entry.get("status") == "cancelled":
        return 0.0
    if entry.get("status") == "claimed":
        return _amount(entry.get("reserved_purchase_usd"))
    return _amount(entry.get("actual_purchase_usd"))


def _revenue_action_capability_in_state(
    state,
    action_type,
    target,
    *,
    sprint_id=None,
    purchase_amount_usd=0.0,
    policy_revision=None,
    at=None,
):
    normalized_type = str(action_type or "").strip().lower()
    normalized_target = str(target or "").strip()
    try:
        requested_purchase = _amount(purchase_amount_usd)
    except ValueError:
        requested_purchase = -1.0
    sprint = active_revenue_sprint(state, sprint_id)
    campaign_date = _sprint_date(sprint, at) if sprint is not None else None
    policy = sprint.get("automation_policy", {}) if sprint is not None else {}
    persisted_revision = str(policy.get("revision") or "")
    requested_revision = str(policy_revision or "")
    total_cap = int((policy.get("total_action_caps") or {}).get(normalized_type, 0) or 0)
    daily_cap = int((policy.get("daily_action_caps") or {}).get(normalized_type, 0) or 0)
    total_count, daily_count = (
        _action_counts(sprint, normalized_type, campaign_date) if sprint is not None else (0, 0)
    )
    purchase_total_cap = _amount(policy.get("purchase_total_cap_usd", 0.0))
    purchase_daily_cap = _amount(policy.get("purchase_daily_cap_usd", 0.0))
    purchase_total = _amount(sum(_purchase_commitment(entry) for entry in sprint.get("action_journal", []))) if sprint else 0.0
    purchase_daily = _amount(sum(
        _purchase_commitment(entry)
        for entry in sprint.get("action_journal", [])
        if entry.get("budget_date") == campaign_date
    )) if sprint else 0.0
    result = {
        "allowed": False,
        "reason": "",
        "campaign_id": sprint.get("id") if sprint else None,
        "campaign_status": sprint.get("status") if sprint else "inactive",
        "campaign_date": campaign_date,
        "action_type": normalized_type,
        "target": normalized_target,
        "policy_revision": persisted_revision,
        "requested_policy_revision": requested_revision,
        "daily_count": daily_count,
        "daily_cap": daily_cap,
        "total_count": total_count,
        "total_cap": total_cap,
        "purchase_requested_usd": requested_purchase,
        "purchase_committed_today_usd": purchase_daily,
        "purchase_daily_cap_usd": purchase_daily_cap,
        "purchase_committed_total_usd": purchase_total,
        "purchase_total_cap_usd": purchase_total_cap,
    }
    if sprint is None:
        result["reason"] = "No Revenue Sprint is configured."
    elif sprint.get("status") not in REVENUE_SPRINT_ACTIVE_STATUSES:
        result["reason"] = f"Revenue Sprint is {sprint.get('status')!r}."
    elif not requested_revision or requested_revision != persisted_revision:
        result["reason"] = "Revenue action policy revision does not match the owner-confirmed grant."
    elif normalized_type not in REVENUE_ACTION_TYPES:
        result["reason"] = f"Unsupported Revenue Sprint action type: {normalized_type!r}."
    elif normalized_type not in policy.get("allowed_action_types", []):
        result["reason"] = f"Action type {normalized_type!r} is not allowlisted."
    elif not normalized_target or normalized_target not in (policy.get("allowed_targets") or {}).get(normalized_type, []):
        result["reason"] = f"Target {normalized_target!r} is not exactly allowlisted for {normalized_type}."
    elif daily_cap <= 0 or daily_count >= daily_cap:
        result["reason"] = f"Daily {normalized_type} action-count cap is exhausted."
    elif total_cap <= 0 or total_count >= total_cap:
        result["reason"] = f"Total {normalized_type} action-count cap is exhausted."
    elif requested_purchase < 0:
        result["reason"] = "Purchase amount must be a non-negative USD value."
    elif normalized_type != "purchase" and requested_purchase != 0:
        result["reason"] = "Only purchase actions may claim purchase spend."
    elif normalized_type == "purchase" and requested_purchase <= 0:
        result["reason"] = "Purchase actions require a positive amount claimed before execution."
    elif normalized_type == "purchase" and purchase_daily + requested_purchase > purchase_daily_cap:
        result["reason"] = "Daily automated-purchase cap would be exceeded."
    elif normalized_type == "purchase" and purchase_total + requested_purchase > purchase_total_cap:
        result["reason"] = "Total automated-purchase cap would be exceeded."
    else:
        result["allowed"] = True
        result["reason"] = "Exact action type, target, count caps, and purchase caps allow this claim."
    return result


def revenue_action_capability(
    action_type,
    target,
    path=COMPANY_STATE_FILE,
    *,
    sprint_id=None,
    purchase_amount_usd=0.0,
    policy_revision=None,
    at=None,
):
    state = load_state(path)
    return _revenue_action_capability_in_state(
        state,
        action_type,
        target,
        sprint_id=sprint_id,
        purchase_amount_usd=purchase_amount_usd,
        policy_revision=policy_revision,
        at=at,
    )


def claim_revenue_action(
    action_type,
    target,
    run_id,
    path=COMPANY_STATE_FILE,
    *,
    sprint_id=None,
    purchase_amount_usd=0.0,
    policy_revision=None,
    approved_payload_digest=None,
    idempotency_key=None,
    metadata=None,
    at=None,
):
    normalized_type = str(action_type or "").strip().lower()
    normalized_target = str(target or "").strip()
    normalized_run_id = str(run_id or "").strip()
    purchase_amount = _amount(purchase_amount_usd)
    requested_revision = str(policy_revision or "")
    normalized_approved_digest = str(approved_payload_digest or "").strip().lower()
    normalized_metadata = _normalize_action_journal_metadata(metadata)
    raw_key = str(idempotency_key or "").strip()
    if not raw_key:
        raw_key = hashlib.sha256(
            f"{sprint_id or ''}|{normalized_run_id}|{normalized_type}|{normalized_target}|{purchase_amount:.6f}".encode()
        ).hexdigest()
    with _state_transaction(path) as state:
        sprint = active_revenue_sprint(state, sprint_id)
        if sprint is None:
            raise RevenueActionError("No Revenue Sprint is configured.")
        approval_projects = [
            project
            for project in state.get("projects", [])
            if project.get("status") == "active"
            and str(project.get("campaign_id") or "") == str(sprint.get("id") or "")
            and str(project.get("revenue_sprint_run_id") or "") == normalized_run_id
        ]
        if len(approval_projects) != 1:
            raise RevenueActionError(
                "Revenue action requires one exact active project for this campaign run."
            )
        project = approval_projects[0]
        project_action = project.get("external_action") or {}
        approval = project.get("approved_revenue_action") or {}
        payload_digest = str(normalized_metadata.get("payload_digest") or "").strip().lower()
        if project.get("editor_verdict") != "approved":
            raise RevenueActionError("The campaign project's final editor verdict is not approved.")
        if (
            str(project_action.get("action_type") or "") != normalized_type
            or str(project_action.get("target") or "") != normalized_target
            or str(project_action.get("policy_revision") or "") != requested_revision
        ):
            raise RevenueActionError(
                "The claimed action does not match the active project's exact action binding."
            )
        if (
            str(approval.get("action_type") or "") != normalized_type
            or str(approval.get("target") or "") != normalized_target
            or str(approval.get("policy_revision") or "") != requested_revision
            or not str(approval.get("worker_task_id") or "")
            or not str(approval.get("reviewer_task_id") or "")
        ):
            raise RevenueActionError(
                "The claimed action does not match the persisted final-review approval."
            )
        worker_task = next(
            (
                task
                for task in state.get("tasks", [])
                if task.get("id") == approval.get("worker_task_id")
                and task.get("project_id") == project.get("id")
            ),
            None,
        )
        reviewer_task = next(
            (
                task
                for task in state.get("tasks", [])
                if task.get("id") == approval.get("reviewer_task_id")
                and task.get("project_id") == project.get("id")
            ),
            None,
        )
        candidate_digest = hashlib.sha256(
            str((worker_task or {}).get("result") or "").encode("utf-8")
        ).hexdigest()
        if (
            worker_task is None
            or reviewer_task is None
            or worker_task.get("owner") == "editor"
            or reviewer_task.get("owner") != "editor"
            or worker_task.get("status") not in {"done", "shipped"}
            or reviewer_task.get("status") not in {"done", "shipped"}
            or classify_editor_verdict(reviewer_task.get("result")) != "approved"
            or candidate_digest != str(approval.get("candidate_result_digest") or "")
        ):
            raise RevenueActionError(
                "The persisted campaign approval no longer matches its exact worker and reviewer."
            )
        persisted_digest = str(approval.get("payload_digest") or "").strip().lower()
        if (
            not re.fullmatch(r"[a-f0-9]{64}", normalized_approved_digest)
            or not re.fullmatch(r"[a-f0-9]{64}", payload_digest)
            or not re.fullmatch(r"[a-f0-9]{64}", persisted_digest)
            or normalized_approved_digest != payload_digest
            or normalized_approved_digest != persisted_digest
        ):
            raise RevenueActionError(
                "The claimed payload digest does not match the persisted final approval."
            )
        duplicate = next(
            (entry for entry in sprint["action_journal"] if entry.get("idempotency_key") == raw_key),
            None,
        )
        if duplicate is not None:
            exact = (
                duplicate.get("run_id") == normalized_run_id
                and duplicate.get("action_type") == normalized_type
                and duplicate.get("target") == normalized_target
                and _amount(duplicate.get("reserved_purchase_usd")) == purchase_amount
                and duplicate.get("policy_revision") == requested_revision
                and duplicate.get("approved_payload_digest") == normalized_approved_digest
                and duplicate.get("metadata", {}) == normalized_metadata
            )
            if not exact:
                raise RevenueActionError("Revenue action idempotency key was reused with different parameters.")
            result = deepcopy(duplicate)
            result["idempotent_replay"] = True
            return result
        run_record = _find_sprint_run(sprint, normalized_run_id)
        if run_record is None or run_record.get("status") != "claimed":
            raise RevenueActionError("Revenue action requires the exact currently claimed sprint run.")
        experiment = next(
            (
                entry
                for entry in sprint.get("experiments", [])
                if entry.get("id") == run_record.get("experiment_id")
            ),
            None,
        )
        if experiment is None or experiment.get("action_type") != normalized_type:
            raise RevenueActionError(
                "Revenue action type does not match the exact claimed sprint experiment."
            )
        capability = _revenue_action_capability_in_state(
            state,
            normalized_type,
            normalized_target,
            sprint_id=sprint["id"],
            purchase_amount_usd=purchase_amount,
            policy_revision=requested_revision,
            at=at,
        )
        if not capability["allowed"]:
            raise RevenueActionError(capability["reason"])
        if run_record.get("date") != capability["campaign_date"]:
            raise RevenueActionError("Revenue action run claim belongs to a different campaign date.")
        timestamp = _sprint_moment(sprint, at).isoformat()
        entry = {
            "id": f"action_{uuid.uuid4().hex[:12]}",
            "campaign_id": sprint["id"],
            "run_id": normalized_run_id,
            "budget_date": capability["campaign_date"],
            "action_type": normalized_type,
            "target": normalized_target,
            "status": "claimed",
            "idempotency_key": raw_key,
            "policy_revision": requested_revision,
            "approved_payload_digest": normalized_approved_digest,
            "reserved_purchase_usd": purchase_amount,
            "actual_purchase_usd": 0.0,
            "metadata": normalized_metadata,
            "provider_receipt": {},
            "engagement_counts": {},
            "result": "",
            "claimed_at": timestamp,
            "completed_at": None,
        }
        sprint["action_journal"].append(entry)
        sprint["updated_at"] = timestamp
        add_event(state, "revenue_action_claimed", f"Claimed {normalized_type} Revenue Sprint action.")
        result = deepcopy(entry)
    return result


def complete_revenue_action(
    action_id,
    status,
    path=COMPANY_STATE_FILE,
    *,
    sprint_id=None,
    actual_purchase_usd=None,
    result="",
    provider_receipt=None,
    at=None,
):
    normalized_status = str(status or "").strip().lower()
    normalized_result = str(result or "")[:1000]
    normalized_provider_receipt = _normalize_provider_receipt(provider_receipt)
    if normalized_status not in {"succeeded", "failed", "uncertain", "cancelled"}:
        raise RevenueActionError(f"Unsupported revenue action completion status: {normalized_status!r}.")
    with _state_transaction(path) as state:
        sprint = active_revenue_sprint(state, sprint_id)
        if sprint is None:
            raise RevenueActionError("Revenue Sprint was not found.")
        entry = next(
            (item for item in sprint["action_journal"] if item.get("id") == str(action_id)),
            None,
        )
        if entry is None:
            raise RevenueActionError(f"Revenue action was not found: {action_id}")
        reserved = _amount(entry.get("reserved_purchase_usd"))
        if entry.get("action_type") == "purchase":
            if actual_purchase_usd is None:
                actual = reserved if normalized_status in {"succeeded", "uncertain"} else 0.0
            else:
                actual = _amount(actual_purchase_usd)
            if actual < 0 or actual > reserved:
                raise RevenueActionError(
                    "Actual purchase spend must be between zero and the amount claimed before execution."
                )
        else:
            actual = _amount(actual_purchase_usd or 0.0)
            if actual:
                raise RevenueActionError("Non-purchase actions cannot record purchase spend.")
        if entry.get("status") != "claimed":
            if (
                entry.get("status") == normalized_status
                and _amount(entry.get("actual_purchase_usd")) == actual
                and entry.get("result", "") == normalized_result
                and _normalize_provider_receipt(entry.get("provider_receipt"))
                == normalized_provider_receipt
            ):
                replay = deepcopy(entry)
                replay["idempotent_replay"] = True
                return replay
            raise RevenueActionError(f"Revenue action {action_id!r} is already {entry.get('status')!r}.")
        timestamp = _sprint_moment(sprint, at).isoformat()
        entry["status"] = normalized_status
        entry["actual_purchase_usd"] = actual
        entry["result"] = normalized_result
        entry["provider_receipt"] = normalized_provider_receipt
        entry["completed_at"] = timestamp
        sprint["updated_at"] = timestamp
        if normalized_status == "uncertain":
            _stop_sprint_in_state(state, sprint, "external_action_outcome_uncertain", at)
        add_event(state, "revenue_action_completed", f"Revenue action {action_id} is {normalized_status}.")
        response = deepcopy(entry)
        response["campaign_status"] = sprint["status"]
    return response


def record_bluesky_engagement_snapshot(
    observations,
    phase,
    observed_run_id,
    path=COMPANY_STATE_FILE,
    *,
    sprint_id=None,
    at=None,
):
    """Record bounded cumulative engagement for exact persisted Bluesky posts.

    Provider observations are aggregate counts. Only increases above each action's
    persisted high-water counts become signals, and every signal is attributed to the
    run during which the engagement was observed rather than to the older post's run.
    """

    normalized_phase = str(phase or "").strip().lower()
    if normalized_phase not in {"before", "after"}:
        raise RevenueSprintError("Bluesky engagement snapshot phase must be 'before' or 'after'.")
    normalized_run_id = str(observed_run_id or "").strip()
    if not normalized_run_id:
        raise RevenueSprintError("Bluesky engagement snapshot requires an observed run ID.")
    if not isinstance(observations, (list, tuple)):
        raise RevenueSprintError("Bluesky engagement observations must be a list.")
    if len(observations) > 20:
        raise RevenueSprintError("Bluesky engagement snapshot may contain at most 20 observations.")

    with _state_transaction(path) as state:
        sprint = active_revenue_sprint(state, sprint_id)
        if sprint is None:
            raise RevenueSprintError("Revenue Sprint was not found.")
        observed_run = _find_sprint_run(sprint, normalized_run_id)
        if observed_run is None or observed_run.get("status") not in {"claimed", "completed"}:
            raise RevenueSprintError(
                "Bluesky engagement snapshot requires an existing claimed or completed run."
            )

        canonical = []
        seen_receipts = set()
        matched_actions = []
        for raw in observations:
            if not isinstance(raw, Mapping):
                raise RevenueSprintError("Each Bluesky engagement observation must be a mapping.")
            receipt_source = (
                raw.get("provider_receipt")
                if isinstance(raw.get("provider_receipt"), Mapping)
                else {"uri": raw.get("uri"), "cid": raw.get("cid")}
            )
            receipt = _normalize_provider_receipt(receipt_source)
            uri = str(receipt.get("uri") or "")
            cid = str(receipt.get("cid") or "")
            if not uri or not cid:
                raise RevenueSprintError(
                    "Each Bluesky engagement observation requires exact provider uri and cid."
                )
            receipt_key = (uri, cid)
            if receipt_key in seen_receipts:
                raise RevenueSprintError("Bluesky engagement observations must be unique.")
            seen_receipts.add(receipt_key)
            counts_source = (
                raw.get("engagement_counts")
                if isinstance(raw.get("engagement_counts"), Mapping)
                else raw
            )
            counts = _normalize_engagement_counts(counts_source, require_all=True)
            matches = []
            for action in sprint.get("action_journal", []):
                if (
                    not isinstance(action, dict)
                    or action.get("status") != "succeeded"
                    or action.get("action_type") != "publish"
                    or not str(action.get("target") or "").startswith("bluesky:")
                ):
                    continue
                action_receipt = _normalize_provider_receipt(
                    action.get("provider_receipt")
                )
                if action_receipt.get("uri") == uri and action_receipt.get("cid") == cid:
                    matches.append(action)
            if len(matches) != 1:
                raise RevenueSprintError(
                    "Bluesky engagement observation does not match one exact succeeded publish receipt."
                )
            action = matches[0]
            if "action_id" in raw and raw.get("action_id") != action.get("id"):
                raise RevenueSprintError(
                    "Bluesky engagement observation action ID does not match its exact receipt."
                )
            matched_actions.append((action, counts))
            canonical.append({
                "action_id": str(action.get("id") or ""),
                "action_run_id": str(action.get("run_id") or ""),
                "target": str(action.get("target") or ""),
                "provider_receipt": {"uri": uri, "cid": cid},
                "engagement_counts": counts,
            })
        canonical.sort(key=lambda item: item["action_id"])

        existing = next(
            (
                entry for entry in sprint.get("engagement_snapshots", [])
                if entry.get("observed_run_id") == normalized_run_id
                and entry.get("phase") == normalized_phase
            ),
            None,
        )
        if existing is not None:
            if existing.get("observations", []) == canonical:
                replay = deepcopy(existing)
                replay["idempotent_replay"] = True
                return replay
            raise RevenueSprintError(
                "Bluesky engagement snapshot run and phase already exist with different observations."
            )

        snapshot_key = hashlib.sha256(
            f"{sprint['id']}|{normalized_run_id}|{normalized_phase}".encode("utf-8")
        ).hexdigest()[:16]
        snapshot_id = f"engagement_{snapshot_key}"
        total_positive_deltas = {signal_type: 0 for signal_type in _BLUESKY_ENGAGEMENT_FIELDS}
        for action, counts in matched_actions:
            previous = _normalize_engagement_counts(action.get("engagement_counts"))
            high_water_counts = {}
            for signal_type in _BLUESKY_ENGAGEMENT_FIELDS:
                previous_count = int(previous.get(signal_type, 0))
                current_count = counts[signal_type]
                delta = max(0, current_count - previous_count)
                # Public reaction counts can fall when a user removes a reaction.
                # Retain the high-water mark so the same reaction returning later
                # cannot be counted twice or reset the no-progress guard.
                high_water_counts[signal_type] = max(previous_count, current_count)
                if not delta:
                    continue
                total_positive_deltas[signal_type] += delta
                _record_revenue_signal_in_state(
                    sprint,
                    signal_type,
                    run_id=normalized_run_id,
                    count=delta,
                    evidence=(
                        f"Bluesky engagement snapshot {snapshot_id} for action {action.get('id')}"
                    ),
                    at=at,
                    signal_id=f"signal_{snapshot_id}_{action.get('id')}_{signal_type}",
                )
            action["engagement_counts"] = high_water_counts

        timestamp = _sprint_moment(sprint, at).isoformat()
        snapshot = {
            "id": snapshot_id,
            "observed_run_id": normalized_run_id,
            "phase": normalized_phase,
            "observations": canonical,
            "positive_deltas": total_positive_deltas,
            "captured_at": timestamp,
        }
        sprint["engagement_snapshots"].append(snapshot)
        sprint["updated_at"] = timestamp
        add_event(
            state,
            "bluesky_engagement_snapshot_recorded",
            f"Recorded Bluesky engagement snapshot for {normalized_run_id} ({normalized_phase}).",
        )
        response = deepcopy(snapshot)
    return response


def pause_company(path=COMPANY_STATE_FILE):
    with _state_transaction(path) as state:
        state["company"]["mode"] = "paused"
        add_event(state, "company_paused", "Company Mode paused.")
    return "Company Mode paused. I will still answer status/report commands, but I will not start new assigned work."


def resume_company(path=COMPANY_STATE_FILE):
    with _state_transaction(path) as state:
        state["company"]["mode"] = "running"
        add_event(state, "company_resumed", "Company Mode resumed.")
    return "Company Mode resumed. Miles can plan supervised work again."


def add_event(state, event_type, summary, project_id=None, task_id=None, amount_usd=None):
    event = {
        "timestamp": _now().isoformat(),
        "type": event_type,
        "summary": summary,
    }
    if project_id:
        event["project_id"] = project_id
    if task_id:
        event["task_id"] = task_id
    if amount_usd is not None:
        event["amount_usd"] = _money(amount_usd)
    state.setdefault("events", []).append(event)
    return event


def configured_roster(configured_agent_keys, specialist_keys=None):
    specialist_keys = specialist_keys or []
    configured = set(configured_agent_keys)
    roster = {}
    for key in specialist_keys:
        roster[key] = {
            "speaks_as_self": key in configured,
            "delivery": "direct_bot" if key in configured else "via_miles",
        }
    return roster


def _project_title(goal):
    words = re.sub(r"\s+", " ", goal).strip()
    if len(words) <= 72:
        return words
    return words[:69].rstrip() + "..."


def _owner_for(preferred_owner, configured_agent_keys):
    if preferred_owner in configured_agent_keys:
        return preferred_owner, "direct_bot"
    return preferred_owner, "via_miles"


def ensure_editor_gate(tasks, review_title):
    """Guarantee the editor is the FINAL task in a plan so it reviews the FINAL
    deliverable. The runner only sets needs_revision when an editor task runs and
    finalizes after all tasks are done, so an editor in the MIDDLE would let later
    production tasks change the work after approval yet still finalize off a stale
    verdict. A mid-plan editor is left in place (it just isn't the gate); a review is
    appended whenever the last task isn't already the editor. Returns a new list of
    (owner, title) tuples."""
    tasks = list(tasks or [])
    if not tasks or tasks[-1][0] != "editor":
        tasks.append(("editor", review_title))
    return tasks


def _new_task(project_id, owner, delivery, title, estimate=DEFAULT_TASK_ESTIMATE_USD, **metadata):
    task = {
        "id": f"task_{uuid.uuid4().hex[:8]}",
        "project_id": project_id,
        "title": title,
        "owner": owner,
        "delivery": delivery,
        "status": "planned",
        "estimate_usd": _amount(estimate),
        "reserved_usd": _amount(estimate),
        "spent_usd": 0.0,
        "created_at": _now().isoformat(),
        "updated_at": _now().isoformat(),
        "result": "",
        "result_truncated": False,
        "artifacts": [],
        "notes": [],
        "acceptance_criteria": [],
        "authorization_level": "propose",
        "revision_round": 0,
        "revision_feedback": "",
        "revision_feedback_truncated": False,
        "execution_attempts": 0,
        "review_attempts": 0,
        "failure_classification": "",
        "model": "",
        "model_reason": "",
        "prior_model_fingerprints": [],
        "feedback_fingerprints": [],
        "attempt_history": [],
        "usage_records": [],
        "team_help_events": [],
        "team_help_events_truncated": False,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "budget_reservation_id": "",
        # Linear mirror (populated by the company_linear bridge once the project is
        # approved). Empty when Linear isn't configured or the task isn't mirrored yet.
        "linear_issue_id": "",
        "linear_identifier": "",
        "linear_url": "",
    }
    task.update({key: deepcopy(value) for key, value in metadata.items() if value is not None})
    return _normalize_task(task)


def _task_plan_spec(raw):
    if isinstance(raw, dict):
        spec = deepcopy(raw)
        owner = spec.pop("owner", spec.pop("agent", "manager"))
        title = spec.pop("title", spec.pop("task", "Untitled task"))
        estimate = _amount(spec.pop("estimate_usd", DEFAULT_TASK_ESTIMATE_USD))
        return owner, title, estimate, spec
    if not isinstance(raw, (list, tuple)) or len(raw) < 2:
        raise ValueError("Each task plan item must be an (owner, title) tuple or task dict.")
    owner, title = raw[0], raw[1]
    estimate = _amount(raw[2]) if len(raw) >= 3 and isinstance(raw[2], (int, float, Decimal)) else DEFAULT_TASK_ESTIMATE_USD
    metadata = raw[3] if len(raw) >= 4 and isinstance(raw[3], dict) else {}
    return owner, title, estimate, deepcopy(metadata)


def assign_goal(
    goal,
    configured_agent_keys,
    specialist_keys=None,
    path=COMPANY_STATE_FILE,
    tasks=None,
    project_metadata=None,
):
    # `tasks` is an optional dynamic work plan: a list of (owner, title) tuples chosen
    # for THIS goal (see main.plan_company_goal). When None, fall back to the fixed
    # DEFAULT_ASSIGN_TASKS so behavior is unchanged.
    goal = goal.strip()
    if not goal:
        return "Usage: /assign <goal>"

    tasks_to_create = []
    total_estimate = 0.0
    for raw in (tasks or DEFAULT_ASSIGN_TASKS):
        preferred_owner, title, estimate, metadata = _task_plan_spec(raw)
        if estimate < 0:
            raise ValueError("Task estimates must be zero or greater.")
        owner, delivery = _owner_for(preferred_owner, configured_agent_keys)
        tasks_to_create.append((owner, delivery, str(title), estimate, metadata))
        total_estimate = _amount(total_estimate + estimate)

    requested_campaign_id = (
        str(project_metadata.get("campaign_id") or "").strip()
        if isinstance(project_metadata, dict)
        else ""
    )
    requested_sprint_run_id = (
        str(project_metadata.get("revenue_sprint_run_id") or "").strip()[:160]
        if isinstance(project_metadata, dict)
        else ""
    )
    requested_external_action = _normalize_external_action_metadata(
        project_metadata.get("external_action")
        if isinstance(project_metadata, dict)
        else None
    )
    if (requested_sprint_run_id or requested_external_action) and not requested_campaign_id:
        raise RevenueSprintError(
            "Revenue Sprint run/action metadata requires an exact campaign_id."
        )

    with _state_transaction(path) as state:
        if state["company"]["mode"] == "paused":
            return "Company Mode is paused. Use /resumecompany before assigning new work."

        existing = active_project(state)
        if existing and existing["status"] in {"proposed", "active"}:
            return (
                f"Blocked: '{existing['title']}' ({existing['id']}) is still {existing['status']}. "
                f"/approve or /cancel it before assigning something new - otherwise its reserved "
                f"budget would be orphaned."
            )
        available = remaining_budget(state)
        if requested_campaign_id:
            campaign_available, _campaign_snapshot = _campaign_admission_available(
                state, requested_campaign_id, allow_emergency=False
            )
            available = min(available, campaign_available)
            sprint = active_revenue_sprint(state, requested_campaign_id)
            if requested_sprint_run_id:
                run_record = _find_sprint_run(sprint, requested_sprint_run_id)
                if run_record is None:
                    raise RevenueSprintError(
                        f"Revenue Sprint run {requested_sprint_run_id!r} was not claimed."
                    )
            if requested_external_action:
                if not requested_sprint_run_id:
                    raise RevenueSprintError(
                        "Revenue Sprint external_action requires an exact claimed run ID."
                    )
                policy = sprint.get("automation_policy", {})
                action_type = requested_external_action["action_type"]
                target = requested_external_action["target"]
                if requested_external_action["policy_revision"] != policy.get("revision"):
                    raise RevenueSprintError(
                        "Revenue Sprint external_action policy_revision does not match the owner grant."
                    )
                if action_type not in policy.get("allowed_action_types", []):
                    raise RevenueSprintError(
                        f"Revenue Sprint external action {action_type!r} is not allowlisted."
                    )
                if target not in (policy.get("allowed_targets") or {}).get(action_type, []):
                    raise RevenueSprintError(
                        f"Revenue Sprint external action target {target!r} is not exactly allowlisted."
                    )
                experiment_record = next(
                    (
                        entry for entry in sprint.get("experiments", [])
                        if entry.get("id") == run_record.get("experiment_id")
                    ),
                    {},
                )
                experiment_action = str(
                    experiment_record.get("action_type") or ""
                ).strip().lower()
                if experiment_action and experiment_action != action_type:
                    raise RevenueSprintError(
                        "Revenue Sprint external_action does not match the claimed run experiment."
                    )
        if available < total_estimate:
            return (
                f"Blocked: assigning this goal reserves about ${total_estimate:.2f}, "
                f"but only ${available:.2f} remains today. Raise /setbudget, "
                f"rescope the goal, or defer it."
            )

        project_id = f"proj_{uuid.uuid4().hex[:8]}"
        project = {
            "id": project_id,
            "title": _project_title(goal),
            "goal": goal,
            "status": "proposed",
            "created_at": _now().isoformat(),
            "task_ids": [],
            "artifacts": [],
            "notes": [],
            "revision_round": 0,
            "review_attempts": 0,
            "failure_classification": "",
            "editor_feedback_history": [],
        }
        # Autonomous runs attach only control-plane identifiers and acceptance
        # metadata here.  Keep an allowlist so callers cannot accidentally copy a
        # prompt, credential, or arbitrary private project data into the ledger.
        allowed_project_metadata = {
            "source",
            "project_key",
            "roadmap_project_id",
            "roadmap_item_id",
            "autonomous_run_id",
            "authorization_level",
            "acceptance_criteria",
            "campaign_id",
            "revenue_sprint_run_id",
            "external_action",
        }
        if isinstance(project_metadata, dict):
            project.update({
                key: deepcopy(value)
                for key, value in project_metadata.items()
                if key in allowed_project_metadata
                and key not in {"revenue_sprint_run_id", "external_action"}
                and value is not None
            })
        project["revenue_sprint_run_id"] = requested_sprint_run_id
        project["external_action"] = deepcopy(requested_external_action)
        state["projects"].append(project)
        state["company"]["active_project_id"] = project_id

        for owner, delivery, title, estimate, metadata in tasks_to_create:
            if requested_campaign_id:
                metadata.setdefault("campaign_id", requested_campaign_id)
            task = _new_task(project_id, owner, delivery, title, estimate, **metadata)
            if estimate:
                reservation = _reserve_budget_in_state(
                    state,
                    estimate,
                    context="task",
                    project_id=project_id,
                    task_id=task["id"],
                    agent=owner,
                    model=task.get("model"),
                    reason=f"Planned task: {title}",
                    campaign_id=requested_campaign_id,
                )
                task["budget_reservation_id"] = reservation["id"]
            state["tasks"].append(task)
            project["task_ids"].append(task["id"])

        add_event(state, "goal_assigned", f"Assigned company goal: {goal}", project_id=project_id, amount_usd=total_estimate)

    simple_specs = [(owner, delivery, title, estimate) for owner, delivery, title, estimate, _ in tasks_to_create]
    return render_assignment(project, simple_specs, state, specialist_keys or [])


def update_task_status(
    task_id,
    status,
    result="",
    artifacts=None,
    spent_usd=None,
    path=COMPANY_STATE_FILE,
    usage_records=None,
    failure=None,
    failure_classification=None,
    model=None,
    model_reason=None,
    feedback=None,
    team_help_events=None,
):
    hook_args = None
    with _state_transaction(path) as state:
        task = next((item for item in state["tasks"] if item["id"] == task_id), None)
        if task is None:
            return f"Task not found: {task_id}"
        previous_status = task["status"]
        if status == "in_progress":
            task["execution_attempts"] = int(task.get("execution_attempts", 0)) + 1
            task.setdefault("attempt_history", []).append({
                "attempt": task["execution_attempts"],
                "started_at": _now().isoformat(),
                "model": model or task.get("model", ""),
                "model_reason": model_reason or task.get("model_reason", ""),
            })
            if task["execution_attempts"] > MAX_EXECUTION_ATTEMPTS:
                status = "blocked"
                result = result or f"Execution attempt cap ({MAX_EXECUTION_ATTEMPTS}) reached."
                failure_classification = "no_progress"
                spent_usd = 0.0 if spent_usd is None else spent_usd
        if task.get("owner") == "editor" and status in {"done", "blocked", "needs_human"} and previous_status not in {"done", "blocked", "needs_human"}:
            task["review_attempts"] = int(task.get("review_attempts", 0)) + 1
        task["status"] = status
        task["updated_at"] = _now().isoformat()
        if result:
            bounded_result, result_truncated = _bounded_text(
                result, MAX_TASK_STORED_RESULT_CHARS
            )
            task["result"] = bounded_result
            task["result_truncated"] = result_truncated
        if model is not None:
            previous_model = task.get("model")
            if previous_model and previous_model != model:
                task.setdefault("prior_model_fingerprints", []).append(_fingerprint(previous_model))
            task["model"] = model
        if model_reason is not None:
            task["model_reason"] = model_reason
        if team_help_events is not None:
            incoming, incoming_truncated = _normalize_team_help_events(team_help_events)
            combined = list(task.get("team_help_events", []))
            for event in incoming:
                # Replaying a completion update must not duplicate the exact same
                # help record. Distinct retries retain their different timestamps.
                if event not in combined:
                    combined.append(event)
            normalized_help, combined_truncated = _normalize_team_help_events(combined)
            task["team_help_events"] = normalized_help
            task["team_help_events_truncated"] = bool(
                task.get("team_help_events_truncated")
            ) or incoming_truncated or combined_truncated
        classification = failure_classification or failure
        if classification:
            task["failure_classification"] = classification if classification in FAILURE_CLASSIFICATIONS else classify_failure(classification)
        if feedback:
            fingerprint = _fingerprint(_normalize_feedback(feedback))
            if fingerprint in task.setdefault("feedback_fingerprints", []):
                task["status"] = "blocked"
                task["failure_classification"] = "no_progress"
            else:
                task["feedback_fingerprints"].append(fingerprint)
        usage = _normalize_usage_records(usage_records)
        if usage:
            task.setdefault("usage_records", []).extend(usage)
            task["input_tokens"] = int(task.get("input_tokens", 0)) + sum(item["input_tokens"] for item in usage)
            task["output_tokens"] = int(task.get("output_tokens", 0)) + sum(item["output_tokens"] for item in usage)
            task["total_tokens"] = int(task.get("total_tokens", 0)) + sum(item["total_tokens"] for item in usage)
        if artifacts:
            task["artifacts"].extend(artifacts)
            project = next((p for p in state.get("projects", []) if p["id"] == task["project_id"]), None)
            if project:
                project.setdefault("artifacts", []).extend(artifacts)
        terminal = {"done", "shipped", "blocked", "needs_human"}
        if task["status"] in terminal and previous_status not in terminal:
            spend = task["reserved_usd"] if spent_usd is None else _amount(spent_usd)
            estimated = spent_usd is None
            task["spent_usd"] = spend
            reservation_id = task.get("budget_reservation_id")
            reservation = _find_reservation(state, reservation_id) if reservation_id else None
            if reservation and reservation["status"] == "reserved":
                entry = _reconcile_budget_in_state(
                    state,
                    reservation_id,
                    spend,
                    usage_records=usage,
                    estimated=estimated,
                    context="task",
                    project_id=task.get("project_id"),
                    task_id=task_id,
                    agent=task.get("owner"),
                    model=task.get("model"),
                    reason=task.get("failure_classification") or task["status"],
                )
                task["cost_entry_id"] = entry["id"]
                task["cost_basis"] = entry["cost_basis"]
            else:
                held = task.get("reserved_usd", 0.0)
                state["company"]["reserved_today_usd"] = _amount(max(0, state["company"]["reserved_today_usd"] - held))
                _record_cost_entry_in_state(
                    state, spend, usage_records=usage, estimated=estimated, context="task",
                    project_id=task.get("project_id"), task_id=task_id, agent=task.get("owner"), model=task.get("model"),
                    reason=task.get("failure_classification") or task["status"],
                    campaign_id=task.get("campaign_id"),
                )
            task["reserved_usd"] = 0.0
        add_event(state, "task_updated", f"{task_id} moved to {task['status']}.", task_id=task_id, amount_usd=task["spent_usd"])
        hook_args = (task_id, task["status"], previous_status)
        message = f"{task_id} updated to {task['status']}."
    _fire(on_task_status_change, *hook_args)
    return message


def set_task_linear(task_id, issue_id, identifier, url, path=COMPANY_STATE_FILE):
    """Persist a mirrored Linear issue's ids onto a task. Used by the company_linear
    bridge after it creates the issue. Does NOT fire hooks (avoids re-entrancy)."""
    with _state_transaction(path) as state:
        for task in state["tasks"]:
            if task["id"] == task_id:
                task["linear_issue_id"] = issue_id or ""
                task["linear_identifier"] = identifier or ""
                task["linear_url"] = url or ""
                return True
    return False


def set_project_source_issue(project_id, issue, path=COMPANY_STATE_FILE):
    """Tag a project with the Linear issue it was created from (via /linear do). The
    company_linear bridge then tracks THAT issue as the project's umbrella - moving it
    to In Progress on start and Done/revisions on completion - instead of creating a
    new issue per task. `issue` is a dict with id/identifier/url."""
    with _state_transaction(path) as state:
        for project in state["projects"]:
            if project["id"] == project_id:
                project["source_linear_issue"] = {
                    "id": issue.get("id", ""),
                    "identifier": issue.get("identifier", ""),
                    "url": issue.get("url", ""),
                }
                return True
    return False


def project_source_issue(state, project_id):
    """Return a project's source Linear issue dict, or None."""
    for project in state.get("projects", []):
        if project["id"] == project_id:
            return project.get("source_linear_issue")
    return None


def mark_task_blocked(task_id, reason, spent_usd=None, artifacts=None, path=COMPANY_STATE_FILE):
    """Park a task that needs your approval to proceed (a gated action was staged).
    Records any real spend/artifacts produced so far and releases its reserve."""
    return update_task_status(
        task_id,
        "blocked",
        result=reason,
        artifacts=artifacts,
        spent_usd=spent_usd,
        path=path,
        failure_classification=classify_failure(reason),
    )


def next_planned_task(state, project_id):
    """The first not-yet-worked task of a project, in creation order, or None."""
    for task in project_tasks(state, project_id):
        if task["status"] == "planned":
            return task
    return None


def approve_project(path=COMPANY_STATE_FILE, *, notify_hooks=True):
    """Flip the active project from 'proposed' to 'active' so the engine may run it.
    Returns (message, project_id_or_None) - the caller starts the runner if an id
    comes back."""
    with _state_transaction(path) as state:
        project = active_project(state)
        if not project:
            return "No project to approve. Use /assign <goal> first.", None
        if state["company"]["mode"] == "paused":
            return "Company Mode is paused. Use /resumecompany before approving work.", None
        if project["status"] == "active":
            return f"{project['title']} is already approved and running.", project["id"]
        if project["status"] != "proposed":
            return f"{project['title']} is {project['status']} - nothing to approve.", None

        project["status"] = "active"
        project_id = project["id"]
        project_title = project["title"]
        add_event(state, "project_approved", f"Approved project: {project_title}", project_id=project_id)
    # Now the project is really running: mirror its tasks into the tracker (Linear).
    if notify_hooks:
        _fire(on_project_activated, project_id)
    return f"Approved: {project_title}. Starting the work plan now.", project_id


def cancel_project(path=COMPANY_STATE_FILE, project_id=None):
    """Cancel a project and release any budget still reserved for its open tasks.
    Defaults to the active project; pass project_id to reach one that's no longer
    tracked as active (e.g. an older project a later /assign superseded without
    ever being approved or cancelled - see open_projects)."""
    with _state_transaction(path) as state:
        if project_id:
            project = next((p for p in state["projects"] if p["id"] == project_id), None)
            if not project:
                return f"No project found with id {project_id}."
            if project["status"] not in {"proposed", "active"}:
                return f"{project['title']} is already {project['status']} - nothing to cancel."
        else:
            project = active_project(state)
            if not project:
                return "No active project to cancel."

        released = 0.0
        for task in project_tasks(state, project["id"]):
            if task["status"] not in {"planned", "in_progress", "blocked", "needs_human"}:
                continue
            held = _amount(task.get("reserved_usd", 0.0))
            if held:
                released = _amount(released + held)
                reservation = _find_reservation(state, task.get("budget_reservation_id"))
                if reservation and reservation["status"] == "reserved":
                    _release_budget_in_state(state, reservation["id"], "Project cancelled.")
                else:
                    state["company"]["reserved_today_usd"] = _amount(
                        max(0.0, state["company"]["reserved_today_usd"] - held)
                    )
                task["reserved_usd"] = 0.0
            task["status"] = "cancelled"

        project["status"] = "cancelled"
        if state["company"].get("active_project_id") == project["id"]:
            state["company"]["active_project_id"] = None
        add_event(state, "project_cancelled", f"Cancelled project: {project['title']}",
                  project_id=project["id"], amount_usd=released)
    return f"Cancelled {project['title']} and released ${released:.2f} of reserved budget."


def open_projects(state):
    """All not-yet-terminal projects (proposed or active), including ones no longer
    referenced by active_project_id (e.g. superseded by a later /assign). Used to
    surface budget an orphaned project is still holding."""
    return [p for p in state.get("projects", []) if p["status"] in {"proposed", "active"}]


def _stray_project_lines(state, exclude_id=None):
    strays = [p for p in open_projects(state) if p["id"] != exclude_id]
    if not strays:
        return []
    lines = [f"Note: {len(strays)} older open project(s) not tracked as active:"]
    for p in strays:
        held = _money(sum(t["reserved_usd"] for t in project_tasks(state, p["id"])))
        lines.append(f"- {p['title']} ({p['id']}), ${held:.2f} still reserved - /cancel {p['id']} to release it")
    return lines


def _configured_positive_int(name, default, minimum=1, maximum=None):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(value, maximum)
    return value


# Hard stops are configuration, not merely runner conventions: the state layer enforces
# them even when a caller forgets to check first.
MAX_REVISION_ROUNDS = _configured_positive_int("MAX_REVISION_ROUNDS", 2, minimum=0)
MAX_EXECUTION_ATTEMPTS = _configured_positive_int("MAX_EXECUTION_ATTEMPTS", 3)
MAX_TASK_RESULT_CHARS = _configured_positive_int(
    "MAX_TASK_RESULT_CHARS", 5000, maximum=20000
)
# Persist enough output for evidence-based review, but never let one provider reply
# grow the shared JSON state or a later review prompt without bound.  These limits
# are intentionally separate from the smaller Telegram/report display limit above.
MAX_TASK_STORED_RESULT_CHARS = _configured_positive_int(
    "MAX_TASK_STORED_RESULT_CHARS",
    20000,
    minimum=MAX_TASK_RESULT_CHARS,
    maximum=50000,
)
MAX_REVIEW_FEEDBACK_CHARS = _configured_positive_int(
    "MAX_REVIEW_FEEDBACK_CHARS", 12000, maximum=20000
)
MAX_EDITOR_FEEDBACK_HISTORY = _configured_positive_int(
    "MAX_EDITOR_FEEDBACK_HISTORY", 10, maximum=50
)
# A task may ask for one teammate per execution by default, and execution itself is
# capped.  Persist at most three exchanges even if runtime configuration is looser,
# so repeated attempts cannot turn the company state into an unbounded transcript.
MAX_TEAM_HELP_EVENTS = 3
MAX_TEAM_HELP_QUESTION_CHARS = 1000
MAX_TEAM_HELP_REASON_CHARS = 500
MAX_TEAM_HELP_RESPONSE_CHARS = 3000
MAX_TEAM_HELP_MODEL_REASON_CHARS = 500
MAX_TEAM_HELP_METADATA_CHARS = 120
MAX_TEAM_HELP_STATUS_CHARS = 40
MAX_TEAM_HELP_TIMESTAMP_CHARS = 64
MAX_MODEL_ROUTE_DECISIONS = 20
MAX_MODEL_ROUTE_REASON_CHARS = 800
MAX_MODEL_ROUTE_METADATA_CHARS = 120
MAX_MODEL_ROUTE_STATUS_CHARS = 40
MAX_MODEL_ROUTE_TOKEN_COUNT = 10_000_000
MAX_MODEL_ROUTE_TOOL_COUNT = 10_000
MAX_MODEL_ROUTE_USD = 1_000_000
FAILURE_CLASSIFICATIONS = {
    "missing_access",
    "missing_information",
    "unavailable_tool",
    "permission",
    "budget",
    "transient",
    "technical",
    "decision",
    "no_progress",
}


def classify_failure(error):
    """Classify a failure into an actionable stop/retry category."""
    text = str(error or "").strip().lower()
    patterns = (
        ("budget", ("budget", "insufficient funds", "cost cap", "quota exhausted")),
        ("permission", ("permission denied", "forbidden", "unauthorized", "not authorized", "403")),
        ("unavailable_tool", ("tool unavailable", "tool is unavailable", "unavailable_tool", "command not found", "not installed", "missing tool")),
        ("missing_access", ("missing access", "missing_access", "no access", "credentials", "api key", "access token", "sign in", "login required")),
        ("missing_information", ("missing information", "missing_information", "need more information", "insufficient context", "ambiguous requirement")),
        ("decision", ("needs approval", "need approval", "human decision", "choose between", "owner decision")),
        ("transient", ("timeout", "timed out", "rate limit", "429", "temporarily unavailable", "connection reset", "503")),
    )
    for classification, needles in patterns:
        if any(needle in text for needle in needles):
            return classification
    return "technical"


def _normalize_feedback(feedback):
    text = re.sub(r"\d+", "#", str(feedback or "").lower())
    return re.sub(r"[^a-z#]+", " ", text).strip()


def _fingerprint(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:20]


def _substantially_same_feedback(left, right):
    left, right = _normalize_feedback(left), _normalize_feedback(right)
    if not left or not right:
        return left == right
    if left == right:
        return True
    left_words, right_words = set(left.split()), set(right.split())
    union = left_words | right_words
    jaccard = len(left_words & right_words) / len(union) if union else 1.0
    return jaccard >= 0.9 or SequenceMatcher(None, left, right).ratio() >= 0.9


def _explicit_external_dependency(value):
    """Classify only explicit claims that a revision needs unavailable input.

    Generic criticism such as "missing evidence" remains revisable.  These narrow
    patterns cover dependencies the current team cannot fix by rewriting the same
    submission, preventing paid review loops that cannot make progress.
    """

    text = re.sub(r"[_-]+", " ", str(value or "").strip().lower())
    text = re.sub(r"\s+", " ", text)
    structured = re.match(
        r"^blocked\s+needs human review\b.{0,160}\b"
        r"(missing access|missing information|unavailable tool)\b",
        text,
    )
    if structured:
        return {
            "missing access": "missing_access",
            "missing information": "missing_information",
            "unavailable tool": "unavailable_tool",
        }[structured.group(1)]
    access_pattern = re.compile(
        r"\b(?:i|we|the (?:current )?team|the worker|the reviewer)\s+"
        r"(?:cannot|can't|am unable to|are unable to|is unable to|"
        r"am not able to|are not able to|is not able to)\s+access (?:the )?"
        r"(?:actual|required|source|original|last|run|logs?|records?|data|inputs?)\b"
    )
    if access_pattern.search(text):
        return "missing_access"
    owner_dependency = any(phrase in text for phrase in (
        "owner must provide the missing",
        "owner must provide the required",
        "must be provided by the owner before",
        "requires owner input before",
        "requires an owner decision before",
    ))
    hypothetical = re.search(
        r"\b(?:explain|document|describe|clarify)\b.{0,60}\bowner\b",
        text,
    )
    if owner_dependency and not hypothetical:
        return "missing_information"
    return None


def classify_editor_verdict(editor_answer):
    """Map the Managing Editor's reply to one of three outcomes:
      - "approved": ship it.
      - "blocked":  can't be finished by the team - it needs human/external input
                    (access, credentials, a dashboard/runtime check, a decision). STOP
                    and escalate rather than looping revisions.
      - "revise":   the team can fix it themselves; run another revision round.
    A missing/garbled verdict is "revise" (never a silent pass)."""
    text = (editor_answer or "").strip().upper()
    if _explicit_external_dependency(editor_answer):
        return "blocked"
    if text.startswith("APPROVED"):
        return "approved"
    if text.startswith("BLOCKED") or "NEEDS HUMAN REVIEW" in text or "NEEDS YOUR REVIEW" in text:
        return "blocked"
    return "revise"


def set_project_revision_flag(project_id, editor_answer, path=COMPANY_STATE_FILE):
    """Record the Managing Editor's verdict on a project's deliverables. Stores the
    three-way verdict (see classify_editor_verdict), keeps needs_revision for the
    'revise' case (the revision-round loop reads it), and keeps bounded feedback so
    a revision round or escalation can relay the requirements without allowing one
    provider response to grow persistent state indefinitely."""
    verdict = classify_editor_verdict(editor_answer)
    dependency_failure = _explicit_external_dependency(editor_answer)
    feedback_text, feedback_truncated = _bounded_text(
        editor_answer, MAX_REVIEW_FEEDBACK_CHARS, strip=True
    )
    with _state_transaction(path) as state:
        project = next((item for item in state["projects"] if item["id"] == project_id), None)
        if project is None:
            return None
        history = project.setdefault("editor_feedback_history", [])
        repeated = verdict == "revise" and any(
            _substantially_same_feedback(item.get("feedback", ""), feedback_text)
            for item in history
            if item.get("verdict") == "revise"
        )
        if repeated:
            verdict = "blocked"
            project["failure_classification"] = "no_progress"
            project["status"] = "blocked"
            add_event(
                state,
                "review_no_progress",
                "Repeated substantially identical editor feedback; stopped revision loop.",
                project_id=project_id,
            )
        elif verdict == "blocked" and dependency_failure:
            project["failure_classification"] = dependency_failure
            add_event(
                state,
                "review_external_dependency",
                "Review identified required input the current team cannot access.",
                project_id=project_id,
            )
        project["review_attempts"] = int(project.get("review_attempts", 0)) + 1
        project["editor_verdict"] = verdict
        project["needs_revision"] = verdict == "revise"
        project["last_editor_feedback"] = feedback_text
        project["last_editor_feedback_truncated"] = feedback_truncated
        history.append({
            "attempt": project["review_attempts"],
            "verdict": verdict,
            "feedback": feedback_text,
            "feedback_truncated": feedback_truncated,
            "fingerprint": _fingerprint(_normalize_feedback(feedback_text)),
            "timestamp": _now().isoformat(),
        })
        if len(history) > MAX_EDITOR_FEEDBACK_HISTORY:
            del history[:-MAX_EDITOR_FEEDBACK_HISTORY]
            project["editor_feedback_history_truncated"] = True
        else:
            project.setdefault("editor_feedback_history_truncated", False)
    return verdict


def bind_approved_revenue_action(
    project_id,
    worker_task_id,
    payload_digest,
    path=COMPANY_STATE_FILE,
):
    """Bind Vera's final approval to one exact campaign draft before provider I/O.

    Only hashes and control-plane identifiers are persisted here; public post copy
    remains in the bounded worker result. The caller must publish a payload whose
    digest matches this record, and the action journal independently stores the
    same digest when it claims the provider action.
    """

    normalized_digest = str(payload_digest or "").strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", normalized_digest):
        raise RevenueActionError("An exact SHA-256 campaign payload digest is required.")
    normalized_project_id = str(project_id or "").strip()
    normalized_task_id = str(worker_task_id or "").strip()
    with _state_transaction(path) as state:
        project = next(
            (entry for entry in state["projects"] if entry.get("id") == normalized_project_id),
            None,
        )
        if project is None or project.get("status") != "active":
            raise RevenueActionError("The campaign project is not active for approval binding.")
        if project.get("editor_verdict") != "approved":
            raise RevenueActionError("Vera has not approved the current campaign draft.")
        campaign_id = str(project.get("campaign_id") or "").strip()
        run_id = str(project.get("revenue_sprint_run_id") or "").strip()
        action = project.get("external_action") or {}
        if not campaign_id or not run_id or not action:
            raise RevenueActionError("The project lacks its exact campaign action binding.")
        current_round = int(project.get("revision_round", 0) or 0)
        project_task_rows = project_tasks(state, normalized_project_id)
        candidates = [
            task for task in project_task_rows
            if task.get("owner") != "editor"
            and task.get("status") in {"done", "shipped"}
            and int(task.get("revision_round", 0) or 0) == current_round
        ]
        if not candidates or candidates[-1].get("id") != normalized_task_id:
            raise RevenueActionError(
                "The approved campaign draft is not the latest worker candidate."
            )
        reviewers = [
            task for task in project_task_rows
            if task.get("owner") == "editor"
            and task.get("status") in {"done", "shipped"}
            and int(task.get("revision_round", 0) or 0) == current_round
        ]
        if not reviewers or classify_editor_verdict(reviewers[-1].get("result")) != "approved":
            raise RevenueActionError("The current revision has no final approved review.")
        candidate_result = str(candidates[-1].get("result") or "")
        candidate_result_digest = hashlib.sha256(
            candidate_result.encode("utf-8")
        ).hexdigest()
        record = {
            "worker_task_id": normalized_task_id,
            "reviewer_task_id": str(reviewers[-1].get("id") or ""),
            "payload_digest": normalized_digest,
            "candidate_result_digest": candidate_result_digest,
            "action_type": str(action.get("action_type") or ""),
            "target": str(action.get("target") or ""),
            "policy_revision": str(action.get("policy_revision") or ""),
            "approved_at": _now().isoformat(),
        }
        existing = project.get("approved_revenue_action") or {}
        # Normalization preserves a fixed, blank approval shape for older state
        # files.  Treat that placeholder as unbound; only a persisted payload
        # digest represents a real prior Vera approval.
        if existing.get("payload_digest"):
            comparable = {key: existing.get(key) for key in record if key != "approved_at"}
            expected = {key: value for key, value in record.items() if key != "approved_at"}
            if comparable != expected:
                raise RevenueActionError(
                    "The campaign approval binding already exists with different content."
                )
            return deepcopy(existing)
        project["approved_revenue_action"] = record
        add_event(
            state,
            "revenue_action_draft_approved",
            "Bound Vera's approval to one exact campaign payload digest.",
            project_id=normalized_project_id,
            task_id=normalized_task_id,
        )
        return deepcopy(record)


def block_project(
    project_id,
    path=COMPANY_STATE_FILE,
    *,
    reason="",
    failure_classification="decision",
):
    """Stop an open project and close every remaining budget reservation safely.

    Planned-task holds are released. An in-progress task is conservatively charged
    at its held estimate because a crashed or cancelled worker may have consumed
    provider tokens without returning usage.
    """
    with _state_transaction(path) as state:
        project = next((p for p in state["projects"] if p["id"] == project_id), None)
        if not project or project.get("status") not in {"proposed", "active"}:
            return False
        released = 0.0
        estimated_spend = 0.0
        classification = (
            failure_classification
            if failure_classification in FAILURE_CLASSIFICATIONS
            else classify_failure(failure_classification)
        )
        for task in project_tasks(state, project_id):
            if task["status"] not in {"planned", "in_progress"}:
                continue
            held = _amount(task.get("reserved_usd", 0.0))
            reservation = _find_reservation(state, task.get("budget_reservation_id"))
            if task["status"] == "in_progress":
                if reservation and reservation["status"] == "reserved":
                    entry = _reconcile_budget_in_state(
                        state,
                        reservation["id"],
                        held,
                        estimated=True,
                        context="task",
                        project_id=project_id,
                        task_id=task.get("id"),
                        agent=task.get("owner"),
                        model=task.get("model"),
                        reason=reason or "Autonomous runner stopped unexpectedly.",
                    )
                    task["cost_entry_id"] = entry["id"]
                    task["cost_basis"] = entry["cost_basis"]
                elif held:
                    state["company"]["reserved_today_usd"] = _amount(
                        max(0.0, state["company"]["reserved_today_usd"] - held)
                    )
                    entry = _record_cost_entry_in_state(
                        state,
                        held,
                        estimated=True,
                        context="task",
                        project_id=project_id,
                        task_id=task.get("id"),
                        agent=task.get("owner"),
                        model=task.get("model"),
                        reason=reason or "Autonomous runner stopped unexpectedly.",
                    )
                    task["cost_entry_id"] = entry["id"]
                    task["cost_basis"] = entry["cost_basis"]
                estimated_spend = _amount(estimated_spend + held)
                task["spent_usd"] = held
                task["status"] = "needs_human"
            else:
                if held:
                    released = _amount(released + held)
                    if reservation and reservation["status"] == "reserved":
                        _release_budget_in_state(
                            state, reservation["id"], "Project blocked for human review."
                        )
                    else:
                        state["company"]["reserved_today_usd"] = _amount(
                            max(0.0, state["company"]["reserved_today_usd"] - held)
                        )
                task["status"] = "blocked"
            task["reserved_usd"] = 0.0
            task["failure_classification"] = classification
            if reason:
                task["result"], task["result_truncated"] = _bounded_text(
                    reason, MAX_TASK_STORED_RESULT_CHARS
                )
        project["status"] = "blocked"
        project["failure_classification"] = classification
        add_event(
            state,
            "project_blocked",
            f"Blocked (needs user review): {project['title']}",
            project_id=project_id,
            amount_usd=estimated_spend or released or None,
        )
        return True


def complete_project(project_id, path=COMPANY_STATE_FILE):
    """Atomically mark one project complete without risking a stale budget write."""
    with _state_transaction(path) as state:
        project = next((value for value in state["projects"] if value["id"] == project_id), None)
        if project is None:
            return False
        project["status"] = "completed"
        add_event(state, "project_completed", f"Completed project: {project['title']}", project_id=project_id)
        return True


def start_revision_round(project_id, configured_agent_keys, path=COMPANY_STATE_FILE):
    """Queue another pass at a project the Managing Editor rejected: one task per
    original non-editor owner (in their first-appearance order) to address her
    feedback, followed by a fresh editor re-review. Called in a loop by the
    runner until she approves or the budget can't cover another round. Returns
    (created, message) - created=False means the caller should complete the
    project as-is instead of looping."""
    notify_hooks = True
    with _state_transaction(path) as state:
        project = next((p for p in state["projects"] if p["id"] == project_id), None)
        if not project:
            return False, "Project not found."
        notify_hooks = not bool(project.get("autonomous_run_id"))
        current_round = int(project.get("revision_round", 0))
        if current_round >= MAX_REVISION_ROUNDS:
            for task in project_tasks(state, project_id):
                if task["status"] not in {"planned", "in_progress"}:
                    continue
                reservation = _find_reservation(state, task.get("budget_reservation_id"))
                if reservation and reservation["status"] == "reserved":
                    _release_budget_in_state(state, reservation["id"], "Revision-round cap reached.")
                elif task.get("reserved_usd"):
                    state["company"]["reserved_today_usd"] = _amount(
                        max(0.0, state["company"]["reserved_today_usd"] - task["reserved_usd"])
                    )
                task["reserved_usd"] = 0.0
                task["status"] = "blocked"
                task["failure_classification"] = "no_progress"
            project["status"] = "blocked"
            project["needs_revision"] = False
            project["failure_classification"] = "no_progress"
            add_event(state, "revision_cap_reached", "Revision-round cap reached.", project_id=project_id)
            return False, f"maximum revision rounds reached ({MAX_REVISION_ROUNDS}); needs human review"
        if project.get("failure_classification") == "no_progress":
            return False, "repeated review feedback produced no progress; needs human review"

        owners_in_order = []
        owner_templates = {}
        for task in project_tasks(state, project_id):
            if task["owner"] != "editor" and task["owner"] not in owners_in_order:
                owners_in_order.append(task["owner"])
                owner_templates[task["owner"]] = task
            elif task["owner"] == "editor" and "editor" not in owner_templates:
                owner_templates["editor"] = task
        if not owners_in_order:
            return False, "No revisable owners found in this project."

        round_number = current_round + 1
        specs = [
            (owner, f"Revision round {round_number}: address the Managing Editor's required changes in your part of the shared deliverable.")
            for owner in owners_in_order
        ]
        specs.append(("editor", f"Re-review the round {round_number} revisions against the original goal; approve, or list any remaining required changes."))
        estimates = {
            owner: _amount(owner_templates.get(owner, {}).get("estimate_usd", DEFAULT_TASK_ESTIMATE_USD))
            for owner, _ in specs
        }
        total_estimate = _amount(sum(estimates.values()))
        campaign_id = str(project.get("campaign_id") or "").strip()
        available = remaining_budget(state)
        if campaign_id:
            campaign_available, _campaign_snapshot = _campaign_admission_available(
                state,
                campaign_id,
                allow_emergency=False,
            )
            available = min(available, campaign_available)
        if available < total_estimate:
            return False, (
                f"not enough budget left for another revision round "
                f"(needs ~${total_estimate:.2f}, ${available:.2f} remaining)"
            )

        for preferred_owner, title in specs:
            owner, delivery = _owner_for(preferred_owner, configured_agent_keys)
            template = owner_templates.get(preferred_owner, {})
            copied_metadata = {
                key: deepcopy(template.get(key))
                for key in (
                    "acceptance_criteria",
                    "authorization_level",
                    "enforce_authorization",
                    "roadmap_item_id",
                    "task_type",
                    "complexity",
                    "risk",
                    "required_capabilities",
                    "estimated_input_tokens",
                    "estimated_output_tokens",
                    "campaign_external_action",
                    "campaign_product_url",
                    "campaign_changed_variable",
                    "campaign_evidence_basis",
                )
                if template.get(key) is not None
            }
            # Keep the exact review that caused this round attached to every task in
            # the round.  A later review must not silently rewrite the instructions
            # that a worker received, and workers should not have to infer the latest
            # required changes from a growing history of prior task summaries.
            copied_metadata["revision_round"] = round_number
            copied_metadata["revision_feedback"] = str(
                project.get("last_editor_feedback") or ""
            ).strip()
            copied_metadata["revision_feedback_truncated"] = bool(
                project.get("last_editor_feedback_truncated")
            )
            estimate = estimates[preferred_owner]
            task = _new_task(project_id, owner, delivery, title, estimate, **copied_metadata)
            reservation = _reserve_budget_in_state(
                state, estimate, context="revision", project_id=project_id,
                task_id=task["id"], agent=owner, model=task.get("model"),
                reason=f"Revision round {round_number}",
                campaign_id=campaign_id,
            )
            task["budget_reservation_id"] = reservation["id"]
            state["tasks"].append(task)
            project["task_ids"].append(task["id"])

        project["revision_round"] = round_number
        add_event(state, "revision_round_started", f"Started revision round {round_number} for {project['title']}",
                  project_id=project_id, amount_usd=total_estimate)
    # Mirror the freshly queued revision tasks into the tracker too (idempotent).
    if notify_hooks:
        _fire(on_project_activated, project_id)
    return True, f"Editor requires revisions - starting round {round_number} (${total_estimate:.2f} reserved)."


def record_delegation(owner, request_text, answer_text, path=COMPANY_STATE_FILE,
                      spent_usd=None, artifacts=None, usage_records=None, model="", model_reason=""):
    # New kwargs go AFTER path so the existing positional-path callers still work.
    spend = _amount(spent_usd) if spent_usd else 0.0
    artifacts = list(artifacts or [])
    with _state_transaction(path) as state:
        project = active_project(state)
        if not project:
            add_event(state, "delegation", f"{owner} handled delegated work without an active project.",
                      amount_usd=spend or None)
            if spend or usage_records:
                _record_cost_entry_in_state(
                    state, spend, usage_records=usage_records, context="delegation",
                    agent=owner, model=model, reason="Delegated work outside a project.",
                )
            return None

        task = _new_task(
            project["id"], owner, "direct_bot", request_text.strip()[:120] or "Delegated work", 0.0,
            status="done", spent_usd=spend, result=answer_text[:1000], artifacts=artifacts,
            notes=["Recorded from Miles delegation."], model=model, model_reason=model_reason,
            usage_records=usage_records,
        )
        task_id = task["id"]
        state["tasks"].append(task)
        project["task_ids"].append(task_id)
        if spend or usage_records:
            entry = _record_cost_entry_in_state(
                state, spend, usage_records=usage_records, context="delegation",
                project_id=project["id"], task_id=task_id, agent=owner, model=model,
                reason="Recorded from Miles delegation.",
            )
            task["cost_entry_id"] = entry["id"]
            task["cost_basis"] = entry["cost_basis"]
        if artifacts:
            project.setdefault("artifacts", []).extend(artifacts)
        add_event(state, "delegation", f"{owner} handled delegated work: {task['title']}",
                  project_id=project["id"], task_id=task_id, amount_usd=spend or None)
        return task_id


def mark_project_published(path=COMPANY_STATE_FILE):
    """Mark the active project as published after the user approves the publish
    package. This is the supervised gate - the actual upload to Gumroad is done by
    the user (no upload API exists)."""
    with _state_transaction(path) as state:
        project = active_project(state)
        if not project:
            return "No active project to publish."
        project["status"] = "published"
        project_title = project["title"]
        add_event(state, "project_published", f"Approved publishing: {project_title}", project_id=project["id"])
    return f"Marked '{project_title}' as published. Nice work shipping it."


def record_adhoc_spend(
    spent_usd,
    artifacts=None,
    path=COMPANY_STATE_FILE,
    *,
    usage_records=None,
    model_route_decisions=None,
    context="adhoc",
    project_id=None,
    task_id=None,
    agent="manager",
    model="",
    reason="Ad-hoc chat spend.",
    campaign_id=None,
):
    """Count real spend from an ad-hoc chat turn (e.g. delegating to Miles in the
    group) against today's budget, and attach any produced artifacts to the active
    project. Keeps the daily ledger honest for work that happens outside the
    autonomous engine. A no-op when there's nothing to record."""
    spend = _amount(spent_usd) if spent_usd else 0.0
    artifacts = list(artifacts or [])
    if not spend and not artifacts and not usage_records and not model_route_decisions:
        return

    with _state_transaction(path) as state:
        project = active_project(state)
        effective_project_id = project_id or (project.get("id") if project else None)
        effective_campaign_id = str(
            campaign_id
            or (project.get("campaign_id") if project else "")
            or ""
        )
        if spend and effective_campaign_id:
            available, _snapshot = _campaign_admission_available(
                state, effective_campaign_id, allow_emergency=False
            )
            if spend > available:
                raise BudgetExceededError(
                    f"Cannot record ${spend:.2f}; only ${available:.2f} remains in the Revenue Sprint envelope."
                )
        if spend or usage_records or model_route_decisions:
            _record_cost_entry_in_state(
                state, spend, usage_records=usage_records,
                model_route_decisions=model_route_decisions, context=context,
                project_id=effective_project_id, task_id=task_id, agent=agent, model=model, reason=reason,
                campaign_id=effective_campaign_id,
            )
        if project and artifacts:
            project.setdefault("artifacts", []).extend(artifacts)
        add_event(state, "adhoc_spend", reason, project_id=effective_project_id, task_id=task_id, amount_usd=spend or None)


def active_project(state):
    project_id = state["company"].get("active_project_id")
    for project in state.get("projects", []):
        if project["id"] == project_id:
            return project
    return None


def record_budget_deferral(
    path=COMPANY_STATE_FILE,
    *,
    context="admission",
    project_id=None,
    task_id=None,
    agent="manager",
    model="",
    reason="Budget reservation was denied.",
    campaign_id=None,
):
    """Persist a known-zero admission failure without changing today's spend.

    Reservation denial happens before a model execution sink exists, so normal
    reconciliation has nothing to close.  This explicit zero-cost record keeps the
    refusal visible in the same ledger as admitted calls while leaving both spent and
    reserved totals unchanged.
    """
    bounded_reason, _truncated = _bounded_text(
        reason, MAX_MODEL_ROUTE_REASON_CHARS, strip=True
    )
    bounded_reason = bounded_reason or "Budget reservation was denied."
    with _state_transaction(path) as state:
        project = active_project(state)
        effective_project_id = project_id or (project.get("id") if project else None)
        effective_campaign_id = str(
            campaign_id
            or (project.get("campaign_id") if project else "")
            or ""
        )
        decision = {
            "agent": agent,
            "task_type": "budget_admission",
            "complexity": "",
            "risk": "low",
            "uses_tools": False,
            "tool_count": 0,
            "estimated_input_tokens": 0,
            "estimated_output_tokens": 0,
            "remaining_budget_usd": remaining_budget(state),
            "model": model,
            "model_level": "",
            "estimated_cost_usd": 0.0,
            "status": "deferred",
            "deferral_reason": "reservation_denied",
            "reason": bounded_reason,
        }
        entry = _record_cost_entry_in_state(
            state,
            0.0,
            model_route_decisions=[decision],
            context=context,
            project_id=effective_project_id,
            task_id=task_id,
            agent=agent,
            model=model,
            reason=bounded_reason,
            campaign_id=effective_campaign_id,
        )
        add_event(
            state,
            "budget_deferred",
            f"Budget admission deferred for {str(context or 'request')[:160]}.",
            project_id=effective_project_id,
            task_id=task_id,
        )
        result = deepcopy(entry)
    return result


def project_tasks(state, project_id):
    return [task for task in state.get("tasks", []) if task.get("project_id") == project_id]


# --------------------------------------------------------------------------- #
# Products + revenue (v3 money loop). A product links a project to a live Gumroad
# listing so we can compare what the company SPENT building it against what it EARNED.
# --------------------------------------------------------------------------- #

def _latest_project(state):
    """The active project, or the most recently created one if none is active."""
    project = active_project(state)
    if project:
        return project
    projects = state.get("projects", [])
    return projects[-1] if projects else None


def project_spend(state, project_id):
    """Total real USD metered against a project (sum of its tasks' spent_usd)."""
    return _money(sum(task.get("spent_usd", 0.0) for task in project_tasks(state, project_id)))


def link_product(gumroad_url, path=COMPANY_STATE_FILE):
    """Attach a live Gumroad URL to the active (or most recent) project, creating a
    product registry entry so /revenue can track its sales."""
    gumroad_url = (gumroad_url or "").strip()
    if not gumroad_url:
        return "Usage: /link <gumroad-product-url>"

    with _state_transaction(path) as state:
        project = _latest_project(state)
        if not project:
            return "No project to link yet. /assign and build something first."

        for product in state["products"]:
            if product["project_id"] == project["id"]:
                product["gumroad_url"] = gumroad_url
                return f"Updated the Gumroad link for '{project['title']}'."

        state["products"].append({
            "project_id": project["id"],
            "title": project["title"],
            "gumroad_url": gumroad_url,
            "gumroad_product_id": None,
            "sales_count": 0,
            "revenue_usd": 0.0,
            "last_synced": None,
        })
        add_event(state, "product_linked", f"Linked {project['title']} to {gumroad_url}", project_id=project["id"])
        return f"Linked '{project['title']}' to {gumroad_url}. Run /revenue to pull its sales."


def sync_revenue(gumroad_products, path=COMPANY_STATE_FILE):
    """Update the product registry from a list of Gumroad products (dicts with
    short_url, id, sales_count, sales_usd_cents). Matches on the linked short_url.
    Pure - the caller fetches from Gumroad and passes the data in."""
    by_url = {p.get("short_url", "").rstrip("/"): p for p in (gumroad_products or []) if p.get("short_url")}
    now = _now().isoformat()
    with _state_transaction(path) as state:
        for product in state["products"]:
            match = by_url.get((product.get("gumroad_url") or "").rstrip("/"))
            if not match:
                continue
            product["gumroad_product_id"] = match.get("id")
            product["sales_count"] = match.get("sales_count", 0) or 0
            product["revenue_usd"] = _money((match.get("sales_usd_cents", 0) or 0) / 100.0)
            product["last_synced"] = now
        result = deepcopy(state)
    return result


def product_pnl(state):
    """Per-product P&L: [{title, url, spend, revenue, net, sales_count}], plus totals."""
    rows = []
    for product in state.get("products", []):
        spend = project_spend(state, product["project_id"])
        revenue = _money(product.get("revenue_usd", 0.0))
        rows.append({
            "title": product["title"],
            "url": product.get("gumroad_url", ""),
            "spend": spend,
            "revenue": revenue,
            "net": _money(revenue - spend),
            "sales_count": product.get("sales_count", 0),
        })
    totals = {
        "spend": _money(sum(r["spend"] for r in rows)),
        "revenue": _money(sum(r["revenue"] for r in rows)),
        "net": _money(sum(r["net"] for r in rows)),
        "sales_count": sum(r["sales_count"] for r in rows),
    }
    return rows, totals


def render_products(path=COMPANY_STATE_FILE):
    state = load_state(path)
    products = state.get("products", [])
    if not products:
        return "No products linked yet. Ship something, then /link <gumroad-url>."
    lines = ["Products"]
    for product in products:
        synced = product.get("last_synced")
        synced_note = f" (synced {synced[:16]})" if synced else " (not synced - run /revenue)"
        lines.append(
            f"- {product['title']}: {product.get('sales_count', 0)} sales, "
            f"${_money(product.get('revenue_usd', 0.0)):.2f}{synced_note}\n  {product.get('gumroad_url', '')}"
        )
    return "\n".join(lines)


def render_pnl(path=COMPANY_STATE_FILE):
    """P&L view for /revenue: spend vs revenue per product and overall."""
    state = load_state(path)
    rows, totals = product_pnl(state)
    if not rows:
        return "No products linked yet. Ship something, then /link <gumroad-url> and /revenue."
    lines = ["Company P&L (spend vs revenue)"]
    for row in rows:
        sign = "+" if row["net"] >= 0 else "-"
        lines.append(
            f"- {row['title']}: {row['sales_count']} sales | earned ${row['revenue']:.2f} | "
            f"spent ${row['spend']:.2f} | net {sign}${abs(row['net']):.2f}"
        )
    sign = "+" if totals["net"] >= 0 else "-"
    lines.append(
        f"Total: {totals['sales_count']} sales | earned ${totals['revenue']:.2f} | "
        f"spent ${totals['spend']:.2f} | net {sign}${abs(totals['net']):.2f}"
    )
    return "\n".join(lines)


def prior_work_summary(state, project_id, current_task_id, limit_chars=1500):
    """A compact summary of what earlier tasks in this project already produced, so
    the next agent can build on it instead of duplicating it. Includes each completed
    task's owner, title, result (truncated), and any deliverables. Excludes the task
    being run now. Returns "" when nothing is done yet.

    A current editor task receives a larger bounded view of historical work plus the
    stored latest worker result, explicitly marked as the review candidate.  Review
    feedback for a revision round is injected separately from the task's immutable
    ``revision_feedback`` snapshot, rather than being substituted into historical
    editor task blocks."""
    tasks = project_tasks(state, project_id)
    current_task = next(
        (task for task in tasks if task["id"] == current_task_id),
        None,
    )
    reviewer_limit = (
        max(limit_chars, MAX_TASK_RESULT_CHARS)
        if current_task and current_task.get("owner") == "editor"
        else limit_chars
    )
    latest_candidate_id = None
    if current_task and current_task.get("owner") == "editor":
        current_round = int(current_task.get("revision_round", 0) or 0)
        candidates = [
            task
            for task in tasks
            if task["id"] != current_task_id
            and task["status"] in {"done", "shipped"}
            and task.get("owner") != "editor"
            and int(task.get("revision_round", 0) or 0) == current_round
        ]
        if not candidates:
            candidates = [
                task
                for task in tasks
                if task["id"] != current_task_id
                and task["status"] in {"done", "shipped"}
                and task.get("owner") != "editor"
            ]
        if candidates:
            latest_candidate_id = candidates[-1]["id"]

    lines = []
    for task in tasks:
        if task["id"] == current_task_id or task["status"] not in {"done", "shipped"}:
            continue
        result = (task.get("result") or "").strip()
        is_latest_candidate = task["id"] == latest_candidate_id
        block_limit = MAX_TASK_STORED_RESULT_CHARS if is_latest_candidate else reviewer_limit
        result_was_truncated = bool(task.get("result_truncated")) or len(result) > block_limit
        if result_was_truncated:
            marker = " ...[truncated]"
            result = result[:max(0, block_limit - len(marker))] + marker
        if is_latest_candidate:
            block = f"- LATEST REVIEW CANDIDATE: {task['owner']} ({task['title']})"
        else:
            block = f"- {task['owner']} ({task['title']})"
        if result:
            block += f":\n  Result: {result}"
        if task.get("artifacts"):
            block += f"\n  Deliverables: {', '.join(task['artifacts'])}"
        lines.append(block)
    return "\n".join(lines)


DELIVERABLE_INJECT_CHARS = 5000


def build_task_prompt(project, task, prior_work="", deliverable_name=None, deliverable_content=None):
    """Build the prompt for one company task. Beyond the goal + prior-work summary, it
    injects the CURRENT deliverable's actual content when available so the agent builds
    on the real file (even one a teammate saved to GitHub that this agent can't read
    with its own tools) instead of producing a fragmented, duplicate file."""
    prompt = (
        f"You are {task['owner']} working on a company project. Deliver a concrete result.\n"
        f"Company goal: {project['goal']}\n"
        f"Your task: {task['title']}\n"
    )
    acceptance_criteria = [
        str(criterion).strip()
        for criterion in task.get("acceptance_criteria", [])
        if str(criterion).strip()
    ]
    if acceptance_criteria:
        prompt += "\nAcceptance criteria (evaluate these explicitly):\n"
        prompt += "\n".join(f"- {criterion}" for criterion in acceptance_criteria) + "\n"
    revision_feedback, prompt_feedback_truncated = _bounded_text(
        task.get("revision_feedback"), MAX_REVIEW_FEEDBACK_CHARS, strip=True
    )
    if revision_feedback and (
        prompt_feedback_truncated or task.get("revision_feedback_truncated")
    ):
        marker = " ...[truncated]"
        revision_feedback = (
            revision_feedback[:max(0, MAX_REVIEW_FEEDBACK_CHARS - len(marker))] + marker
        )
    if revision_feedback:
        prompt += "\nLatest required changes:\n"
        prompt += f"Revision round: {int(task.get('revision_round', 0) or 0)}\n"
        prompt += f"---\n{revision_feedback}\n---\n"
        if task.get("owner") == "editor":
            prompt += (
                "Verify whether the latest revision candidate addresses every applicable "
                "required change; do not treat an older failed result as the candidate.\n"
            )
        else:
            prompt += (
                "Address every applicable required change explicitly, and state the "
                "result or evidence for each one.\n"
            )
    if prior_work:
        prompt += (
            "\nYour teammates have ALREADY completed earlier steps on this project. Build "
            "on their work - do NOT redo it or produce a duplicate:\n"
            f"{prior_work}\n"
        )
        if task.get("owner") == "editor":
            prompt += (
                "Review the result labeled LATEST REVIEW CANDIDATE as the current "
                "submission. Treat other task results as historical context only.\n"
            )
    if deliverable_content:
        snippet = deliverable_content[:DELIVERABLE_INJECT_CHARS]
        truncated = "\n... [truncated]" if len(deliverable_content) > DELIVERABLE_INJECT_CHARS else ""
        prompt += (
            f"\nThe project's current deliverable file is `{deliverable_name}`. Here is its "
            "CURRENT content - EXTEND or REFINE this same file (write to the same filename); "
            "do NOT create a new or companion file:\n"
            f"---\n{snippet}{truncated}\n---\n"
        )
    campaign_action = (
        task.get("campaign_external_action")
        if isinstance(task.get("campaign_external_action"), dict)
        else {}
    )
    campaign_product_url = str(task.get("campaign_product_url") or "").strip()
    campaign_changed_variable = str(
        task.get("campaign_changed_variable") or ""
    ).strip()
    campaign_evidence_basis = str(
        task.get("campaign_evidence_basis") or ""
    ).strip()
    if campaign_action and campaign_changed_variable:
        prompt += (
            "\nPersisted experiment control:\n"
            f"- change exactly this major variable: {campaign_changed_variable}\n"
            f"- evidence basis: {campaign_evidence_basis or 'No basis was persisted; block instead of inventing one.'}\n"
            "Keep the other major offer variables stable enough for comparison.\n"
        )
    if campaign_action and task.get("owner") == "editor":
        prompt += (
            "\nCampaign review boundary: review the exact LATEST REVIEW CANDIDATE "
            "as a draft; do not call a campaign tool or publish it. APPROVED is valid "
            "only when the candidate contains one strict CAMPAIGN_DRAFT_JSON envelope, "
            "uses the exact approved target and product URL, meets every acceptance "
            "criterion that can be evaluated before publication, and contains no "
            "unsupported claim. Criteria requiring a provider receipt or post-action "
            "revenue snapshot are deterministic coordinator gates after approval; assess "
            "whether the draft/control metadata can satisfy them, but do not pretend that "
            "post-publication evidence already exists. Otherwise request concrete "
            "revisions or block for missing evidence/access.\n"
        )

    # Legacy/manual Company Mode keeps its established produce behavior.  Autonomous
    # tasks opt into explicit authorization enforcement via structured task metadata.
    if task.get("enforce_authorization"):
        authorization = str(task.get("authorization_level", "propose")).strip().lower()
        authorization = {"modify_locally": "modify_local"}.get(authorization, authorization)
        if task.get("owner") == "editor":
            prompt += (
                "\nReview verdict contract: use REVISIONS REQUIRED only for concrete "
                "changes the current team can make with the supplied evidence and allowed "
                "tools. If required evidence, access, a tool, or an owner decision is "
                "unavailable, start exactly with BLOCKED - NEEDS HUMAN REVIEW and include "
                "one category: MISSING_ACCESS, MISSING_INFORMATION, or UNAVAILABLE_TOOL. "
                "Do not spend a revision round asking the team to obtain unavailable input.\n"
            )
        else:
            prompt += (
                "\nFailure contract: if a required input, access grant, tool, or owner "
                "decision is unavailable after using the supplied context and allowed "
                "tools, do not invent a placeholder. Start exactly with BLOCKED - NEEDS "
                "HUMAN REVIEW and include one category: MISSING_ACCESS, "
                "MISSING_INFORMATION, or UNAVAILABLE_TOOL, followed by the exact action "
                "the owner must take.\n"
            )
        prompt += f"\nAuthorization level: {authorization}.\n"
        if authorization in {"observe", "propose"}:
            prompt += (
                "Return the requested inspection, proposal, or draft in your response. "
                "Do not create or modify files, branches, issues, reminders, messages, "
                "deployments, or any external system."
            )
        elif authorization == "modify_local":
            prompt += (
                "You may create a reversible local deliverable or propose a change on an "
                "isolated review branch. Do not deploy, merge, publish, send, purchase, "
                "delete, or alter production systems."
            )
        else:
            prompt += (
                "Do not execute an external action. Describe the exact proposed action and "
                "the human approval needed, then stop."
            )
    else:
        prompt += (
            "\nIf you produce a file or open a pull request, do it now with your tools - that "
            "saved output is recorded as this task's deliverable."
        )
    if campaign_action and task.get("owner") != "editor":
        action_type = str(campaign_action.get("action_type") or "").strip().lower()
        target = str(campaign_action.get("target") or "").strip()
        example = {
            "action_type": action_type,
            "target": target,
            "text": "<one complete 1-300 character Bluesky post containing the URL once>",
            "url": campaign_product_url,
        }
        prompt += (
            "\n\nCampaign draft boundary: do not call a campaign tool and do not publish. "
            "The coordinator can act only after Vera approves this exact draft. Return "
            "exactly the marker CAMPAIGN_DRAFT_JSON: on one line followed by one JSON "
            "object on the next line, with no code fence or surrounding prose. Use only "
            "the keys action_type, target, text, and url. The action_type, target, and "
            "URL shown below are immutable; write only the public post text.\n"
            "CAMPAIGN_DRAFT_JSON:\n"
            f"{json.dumps(example, ensure_ascii=False, separators=(',', ':'))}"
        )
    prompt += (
        " Focus only on YOUR task, keep it tight and in scope, and don't repeat what a "
        "teammate already delivered."
    )
    return prompt


def render_money(state):
    company = state["company"]
    return (
        f"Budget: ${company['daily_budget_usd']:.2f} | "
        f"reserved ${company['reserved_today_usd']:.2f} | "
        f"spent ${company['spent_today_usd']:.2f} | "
        f"remaining ${remaining_budget(state):.2f}"
    )


def render_company_status(path=COMPANY_STATE_FILE):
    state = load_state(path)
    project = active_project(state)
    lines = [
        "Company Mode",
        f"Mode: {state['company']['mode']}",
        render_money(state),
    ]
    if not project:
        lines.append("Active project: none")
        lines.append("Next move: set /setbudget, then /assign a sellable product goal.")
        lines.extend(_stray_project_lines(state))
        return "\n".join(lines)

    tasks = project_tasks(state, project["id"])
    open_tasks = [task for task in tasks if task["status"] not in {"done", "shipped", "cancelled"}]
    lines.append(f"Active project: {project['title']} ({project['id']}) - {project['status']}")
    if project.get("needs_revision"):
        lines.append("Editor verdict: REVISIONS REQUIRED - not ready to ship.")
    lines.append(f"Open tasks: {len(open_tasks)}/{len(tasks)}")
    for task in open_tasks[:6]:
        suffix = " via Miles" if task["delivery"] == "via_miles" else ""
        linear = f" [{task['linear_identifier']}]" if task.get("linear_identifier") else ""
        lines.append(f"- {task['id']} [{task['status']}] {task['owner']}{suffix}: {task['title']} (${task['estimate_usd']:.2f}){linear}")

    artifacts = [a for task in tasks for a in task.get("artifacts", [])]
    if artifacts:
        lines.append(f"Artifacts so far: {len(artifacts)} (see /dailyreport)")
    if project["status"] == "proposed":
        lines.append("Reply /approve to start the work plan, or /cancel to drop it.")
    lines.extend(_stray_project_lines(state, exclude_id=project["id"]))
    return "\n".join(lines)


def render_assignment(project, task_specs, state, specialist_keys):
    lines = [
        "Miles: Company goal accepted.",
        f"Project: {project['title']} ({project['id']})",
        render_money(state),
        "Work plan:",
    ]
    for owner, delivery, title, estimate in task_specs:
        suffix = " via Miles" if delivery == "via_miles" else ""
        lines.append(f"- {owner}{suffix}: {title} (${estimate:.2f} reserved)")
    lines.append("Reply /approve to start the work plan, or /cancel to drop it and release the budget.")
    lines.append("Approval gates remain active for sending, deleting, publishing, deploying, paid spend, or new-agent creation.")
    return "\n".join(lines)


def build_daily_report(path=COMPANY_STATE_FILE):
    state = load_state(path)
    project = active_project(state)
    lines = [
        "Daily Company Report",
        render_money(state),
    ]
    if not project:
        lines.append("No active project today.")
        lines.append("Recommendation: choose one sellable product goal and run /assign.")
        return "\n".join(lines)

    tasks = project_tasks(state, project["id"])
    done = [task for task in tasks if task["status"] in {"done", "shipped"}]
    blocked = [task for task in tasks if task["status"] == "blocked"]
    open_tasks = [task for task in tasks if task["status"] not in {"done", "shipped", "blocked", "cancelled"}]

    lines.append(f"Project: {project['title']}")
    if project.get("needs_revision"):
        lines.append("Editor verdict: REVISIONS REQUIRED - not ready to ship.")
    lines.append(f"Shipped/done: {len(done)} | Open: {len(open_tasks)} | Blocked: {len(blocked)}")
    if done:
        lines.append("Completed:")
        lines.extend(f"- {task['owner']}: {task['title']}" for task in done[:5])
    if blocked:
        lines.append("Blocked:")
        lines.extend(f"- {task['owner']}: {task['title']}" for task in blocked[:5])
    if open_tasks:
        lines.append("Next:")
        lines.extend(f"- {task['owner']}: {task['title']}" for task in open_tasks[:5])

    artifacts = [a for task in tasks for a in task.get("artifacts", [])]
    if artifacts:
        lines.append("Artifacts (deliverables produced):")
        lines.extend(f"- {a}" for a in artifacts[:10])

    if project.get("needs_revision"):
        lines.append("Recommendation: address the editor's required revisions before publishing or launching this.")
    else:
        lines.append("Recommendation: keep scope tight, finish one artifact, then decide whether to sell, validate, or build tomorrow.")
    return "\n".join(lines)


def parse_company_command(text):
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    parts = stripped.split(maxsplit=1)
    # In a group with multiple bots, Telegram appends the bot's @username to a
    # command (e.g. "/dailyreport@TyManagerBot"). Strip it so the command still
    # matches - otherwise it falls through and gets misrouted to a specialist.
    command = parts[0].lower().split("@", 1)[0]
    if command not in COMPANY_COMMANDS:
        return None
    arg = parts[1].strip() if len(parts) > 1 else ""
    return command, arg


def handle_company_command(text, configured_agent_keys, specialist_keys=None, path=COMPANY_STATE_FILE):
    parsed = parse_company_command(text)
    if parsed is None:
        return None

    command, arg = parsed
    if command in {"/company", "/status"}:
        return render_company_status(path)
    if command == "/dailyreport":
        return build_daily_report(path)
    if command == "/pausecompany":
        return pause_company(path)
    if command == "/resumecompany":
        return resume_company(path)
    if command == "/cancel":
        return cancel_project(path, project_id=arg or None)
    if command == "/link":
        return link_product(arg, path)
    if command == "/products":
        return render_products(path)
    if command == "/approve":
        # Flipping status is fine here, but only group_bot can actually start the
        # background runner - it intercepts /approve before reaching this branch.
        message, _ = approve_project(path)
        return message
    if command == "/setbudget":
        if not arg:
            return "Usage: /setbudget <amount_usd>"
        amount_text = arg.replace("$", "").strip()
        try:
            return set_daily_budget(float(amount_text), path)
        except ValueError:
            return "Usage: /setbudget <amount_usd>"
    if command == "/assign":
        return assign_goal(arg, configured_agent_keys, specialist_keys=specialist_keys, path=path)
    return None

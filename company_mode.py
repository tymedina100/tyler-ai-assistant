import json
import os
import re
import hashlib
import tempfile
import uuid
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
        },
        "projects": [],
        "tasks": [],
        "events": [],
        "products": [],
        "budget_reservations": [],
        "cost_entries": [],
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
    item["input_tokens"] = int(item.get("input_tokens", 0) or 0)
    item["output_tokens"] = int(item.get("output_tokens", 0) or 0)
    item["total_tokens"] = int(item.get("total_tokens", 0) or 0)
    item.setdefault("budget_reservation_id", "")
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
    item["input_tokens"] = int(item.get("input_tokens", sum(r["input_tokens"] for r in item["usage_records"])) or 0)
    item["output_tokens"] = int(item.get("output_tokens", sum(r["output_tokens"] for r in item["usage_records"])) or 0)
    item["total_tokens"] = int(
        item.get("total_tokens", item["input_tokens"] + item["output_tokens"]) or 0
    )
    item.setdefault("created_at", _now().isoformat())
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


def set_daily_budget(amount_usd, path=COMPANY_STATE_FILE):
    amount = _amount(amount_usd)
    if amount < 0:
        return "Budget must be zero or greater."

    with _state_transaction(path) as state:
        state["company"]["daily_budget_usd"] = amount
        state["company"]["budget_date"] = today_key()
    return f"Company budget set to ${amount:.2f} for today. Remaining: ${remaining_budget(state):.2f}."


def _attribution(context, project_id, task_id, agent, model, reason):
    if isinstance(context, dict):
        values = context
        context = values.get("context", values.get("kind", "task"))
        project_id = project_id or values.get("project_id") or values.get("project")
        task_id = task_id or values.get("task_id") or values.get("task")
        agent = agent or values.get("agent") or values.get("owner")
        model = model or values.get("model")
        reason = reason or values.get("reason")
    return {
        "context": str(context or "task"),
        "project_id": project_id,
        "task_id": task_id,
        "agent": str(agent or ""),
        "model": str(model or ""),
        "reason": str(reason or ""),
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
):
    amount = _amount(amount_usd)
    if amount <= 0:
        raise ValueError("A budget reservation must be greater than zero.")
    attribution = _attribution(context, project_id, task_id, agent, model, reason)
    reservation_id = reservation_id or f"res_{uuid.uuid4().hex[:12]}"
    existing = _find_reservation(state, reservation_id)
    if existing:
        if existing["status"] == "reserved" and existing["amount_usd"] == amount:
            return existing
        raise ValueError(f"Budget reservation id already exists: {reservation_id}")

    emergency_context = attribution["context"].lower() in {"emergency", "escalation", "summary"}
    may_use_emergency = bool(allow_emergency or emergency_context)
    available = remaining_budget(state, include_emergency=may_use_emergency)
    if amount > available:
        reserve_note = " including emergency reserve" if may_use_emergency else " (emergency reserve excluded)"
        raise BudgetExceededError(
            f"Cannot reserve ${amount:.2f}; only ${available:.2f} remains{reserve_note}."
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
        "uses_emergency_reserve": amount > ordinary_available,
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
        maximum = _amount(current + available)
        if maximum < minimum:
            result = {
                "expanded": False,
                "reason": "insufficient_ordinary_budget",
                "task_id": task_id,
                "amount_usd": current,
                "added_usd": 0.0,
                "ordinary_remaining_usd": available,
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
            }
        response = deepcopy(result)
    return response


def _record_cost_entry_in_state(
    state,
    amount_usd,
    *,
    reservation_id="",
    usage_records=None,
    estimated=False,
    context="task",
    project_id=None,
    task_id=None,
    agent=None,
    model=None,
    reason="",
):
    amount = _amount(amount_usd)
    attribution = _attribution(context, project_id, task_id, agent, model, reason)
    usage = _normalize_usage_records(usage_records)
    input_tokens = sum(item["input_tokens"] for item in usage)
    output_tokens = sum(item["output_tokens"] for item in usage)
    total_tokens = sum(item["total_tokens"] for item in usage)
    entry = {
        "id": f"cost_{uuid.uuid4().hex[:12]}",
        "budget_date": state["company"]["budget_date"],
        "amount_usd": amount,
        "cost_basis": "estimated" if estimated else "actual",
        "is_estimated": bool(estimated),
        "reservation_id": reservation_id or "",
        **attribution,
        "usage_records": usage,
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
    estimated=False,
    context=None,
    project_id=None,
    task_id=None,
    agent=None,
    model=None,
    reason="",
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
    )
    entry = _record_cost_entry_in_state(
        state,
        actual,
        reservation_id=reservation_id,
        usage_records=usage_records,
        estimated=estimated,
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
    return entry


def reconcile_budget(
    reservation_id,
    actual_usd=None,
    path=COMPANY_STATE_FILE,
    *,
    usage_records=None,
    estimated=False,
    context=None,
    project_id=None,
    task_id=None,
    agent=None,
    model=None,
    reason="",
):
    """Atomically replace a reservation with measured (or labelled estimated) cost."""
    with _state_transaction(path) as state:
        entry = _reconcile_budget_in_state(
            state,
            reservation_id,
            actual_usd,
            usage_records=usage_records,
            estimated=estimated,
            context=context,
            project_id=project_id,
            task_id=task_id,
            agent=agent,
            model=model,
            reason=reason,
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
        if remaining_budget(state) < total_estimate:
            return (
                f"Blocked: assigning this goal reserves about ${total_estimate:.2f}, "
                f"but only ${remaining_budget(state):.2f} remains today. Raise /setbudget, "
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
        }
        if isinstance(project_metadata, dict):
            project.update({
                key: deepcopy(value)
                for key, value in project_metadata.items()
                if key in allowed_project_metadata and value is not None
            })
        state["projects"].append(project)
        state["company"]["active_project_id"] = project_id

        for owner, delivery, title, estimate, metadata in tasks_to_create:
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
                "attempt": task["execution_attempts"], "started_at": _now().isoformat(), "model": model or task.get("model", "")
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
        if remaining_budget(state) < total_estimate:
            return False, (
                f"not enough budget left for another revision round "
                f"(needs ~${total_estimate:.2f}, ${remaining_budget(state):.2f} remaining)"
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
                    "model",
                    "model_reason",
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
    context="adhoc",
    project_id=None,
    task_id=None,
    agent="manager",
    model="",
    reason="Ad-hoc chat spend.",
):
    """Count real spend from an ad-hoc chat turn (e.g. delegating to Miles in the
    group) against today's budget, and attach any produced artifacts to the active
    project. Keeps the daily ledger honest for work that happens outside the
    autonomous engine. A no-op when there's nothing to record."""
    spend = _amount(spent_usd) if spent_usd else 0.0
    artifacts = list(artifacts or [])
    if not spend and not artifacts and not usage_records:
        return

    with _state_transaction(path) as state:
        project = active_project(state)
        effective_project_id = project_id or (project.get("id") if project else None)
        if spend or usage_records:
            _record_cost_entry_in_state(
                state, spend, usage_records=usage_records, context=context,
                project_id=effective_project_id, task_id=task_id, agent=agent, model=model, reason=reason,
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

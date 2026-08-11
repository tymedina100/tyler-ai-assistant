"""Safe autonomous roadmap selection and daily-run coordination.

This module is deliberately independent from Telegram and paid model calls.  It
owns only the durable control-plane state, selection rules, run reports, and a
small callback boundary into the existing Company Mode runner.  A dry run never
invokes the execution or idea-generation callbacks.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import os
import re
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout as FileLockTimeout


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ROADMAP_FILE = BASE_DIR / "config" / "autonomous-roadmap.json"
DEFAULT_ROADMAP_PACK_DIR = BASE_DIR / "config" / "autonomous-projects"
STATE_VERSION = 1
TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_RESULT_PREVIEW_CHARS = 360
TELEGRAM_CHAT_RECAP_LIMIT = 1600
TELEGRAM_CHAT_TRANSITION_LIMIT = 900
RECENT_RUN_EVIDENCE_LIMIT = 5
RECENT_RUN_EVIDENCE_TEXT_CHARS = 180
RECENT_RUN_REPORT_MAX_BYTES = 256 * 1024
TERMINAL_TASK_STATUSES = {"complete", "completed", "done", "approved", "shipped"}
ACTIONABLE_TASK_STATUSES = {"planned", "ready", "pending", "todo", "deferred", "retry"}
RETRYABLE_ITEM_STATUSES = {"blocked", "needs_human", "deferred"}
RETRY_BLOCKED_PROJECT_STATUSES = {
    "paused",
    "archived",
    "cancelled",
    "complete",
    "completed",
}
HUMAN_RESOLUTION_HISTORY_LIMIT = 50
ROADMAP_PACK_MAX_ITEMS = 25
ROADMAP_PACK_MAX_BYTES = 512 * 1024
ROADMAP_PACK_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,79}")
SESSION_REPORT_METADATA_MAX_BYTES = 16 * 1024
REVENUE_SPRINT_TOTAL_AI_BUDGET_USD = 100.0
REVENUE_SPRINT_DAILY_AI_BUDGET_USD = 5.0
REVENUE_SPRINT_RUN_DAYS = 20
REVENUE_SPRINT_KNOWN_ACTION_TYPES = frozenset({
    "publish",
    "outreach",
    "purchase",
    "deploy",
})
REVENUE_SPRINT_ACTION_TARGET_RE = re.compile(
    r"[a-z0-9][a-z0-9._-]*(?::[A-Za-z0-9][A-Za-z0-9._-]*)+"
)
RETRY_RESET_FIELDS = {
    "blocked_reason",
    "blocker",
    "blocking_reason",
    "error",
    "failure",
    "failure_classification",
    "failure_reason",
    "last_error",
    "last_failure",
    "needs_human_reason",
    "terminal_reason",
}
_UNSET = object()


class AuthorizationLevel(str, Enum):
    """Maximum side-effect level an autonomous run may perform."""

    OBSERVE = "observe"
    PROPOSE = "propose"
    MODIFY_LOCAL = "modify_local"
    EXTERNAL_ACTION = "external_action"


_AUTHORIZATION_RANK = {
    AuthorizationLevel.OBSERVE: 0,
    AuthorizationLevel.PROPOSE: 1,
    AuthorizationLevel.MODIFY_LOCAL: 2,
    AuthorizationLevel.EXTERNAL_ACTION: 3,
}


def _authorization(value: Any, default: AuthorizationLevel = AuthorizationLevel.PROPOSE) -> AuthorizationLevel:
    try:
        return value if isinstance(value, AuthorizationLevel) else AuthorizationLevel(str(value).strip().lower())
    except (TypeError, ValueError):
        return default


def _env_first(*names: str, default: Optional[str] = None) -> Optional[str]:
    for name in names:
        value = os.environ.get(name)
        if value is not None:
            return value
    return default


def _safe_bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _safe_int(value: Any, default: int, minimum: int = 0, maximum: Optional[int] = None) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if parsed < minimum or (maximum is not None and parsed > maximum):
        return default
    return parsed


def _safe_float(value: Any, default: float, minimum: float = 0.0) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _safe_schedule_time(value: Any) -> str:
    text = str(value or "08:00").strip()
    try:
        hour_text, minute_text = text.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError
        return f"{hour:02d}:{minute:02d}"
    except (TypeError, ValueError):
        return "08:00"


def _safe_timezone(value: Any) -> str:
    name = str(value or "America/Phoenix").strip()
    try:
        ZoneInfo(name)
        return name
    except Exception:
        return "America/Phoenix"


def _safe_schedule_days(value: Any) -> str:
    text = str(value or "mon-fri").strip().lower()
    # APScheduler accepts expressions such as mon-fri and mon,wed,fri.  Keep the
    # accepted surface narrow so a typo cannot silently produce a surprising job.
    if re.fullmatch(r"(?:mon|tue|wed|thu|fri|sat|sun)(?:-(?:mon|tue|wed|thu|fri|sat|sun))?(?:,(?:mon|tue|wed|thu|fri|sat|sun))*", text):
        return text
    return "mon-fri"


def _default_data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR") or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or BASE_DIR)


@dataclass(frozen=True)
class AutonomyConfig:
    """Typed autonomous-run configuration with conservative defaults."""

    enabled: bool = False
    dry_run: bool = True
    schedule_time: str = "08:00"
    schedule_days: str = "mon-fri"
    timezone: str = "America/Phoenix"
    daily_budget_usd: float = 5.0
    emergency_reserve_usd: float = 0.25
    max_tasks_per_run: int = 10
    max_ideas_per_run: int = 3
    max_session_minutes: int = 120
    min_task_reservation_usd: float = 0.05
    idea_backlog_limit: int = 50
    max_execution_attempts: int = 2
    stale_run_minutes: int = 180
    max_authorization: AuthorizationLevel = AuthorizationLevel.PROPOSE
    data_dir: Path = BASE_DIR
    roadmap_seed_path: Path = DEFAULT_ROADMAP_FILE
    roadmap_pack_dir: Path = DEFAULT_ROADMAP_PACK_DIR
    lock_timeout_seconds: float = 0.0

    @property
    def schedule_hour(self) -> int:
        return int(self.schedule_time.split(":", 1)[0])

    @property
    def schedule_minute(self) -> int:
        return int(self.schedule_time.split(":", 1)[1])

    @property
    def days(self) -> str:
        return self.schedule_days

    @property
    def timezone_name(self) -> str:
        return self.timezone

    @classmethod
    def from_env(cls) -> "AutonomyConfig":
        """Load configuration without failing startup on malformed values.

        ``AUTONOMY_*`` is canonical.  A few ``AUTONOMOUS_*`` aliases are
        accepted to make operator configuration forgiving during rollout.
        """

        data_dir = Path(
            _env_first("AUTONOMY_DATA_DIR", default=None)
            or os.environ.get("DATA_DIR")
            or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
            or BASE_DIR
        )
        seed_path = Path(
            _env_first("AUTONOMY_ROADMAP_FILE", "AUTONOMOUS_ROADMAP_FILE", default=str(DEFAULT_ROADMAP_FILE))
            or DEFAULT_ROADMAP_FILE
        )
        pack_dir = Path(
            _env_first(
                "AUTONOMY_PROJECT_PACK_DIR",
                "AUTONOMOUS_PROJECT_PACK_DIR",
                default=str(DEFAULT_ROADMAP_PACK_DIR),
            )
            or DEFAULT_ROADMAP_PACK_DIR
        )
        return cls(
            enabled=_safe_bool(_env_first("AUTONOMY_ENABLED", "AUTONOMOUS_ENABLED"), False),
            dry_run=_safe_bool(_env_first("AUTONOMY_DRY_RUN", "AUTONOMOUS_DRY_RUN"), True),
            schedule_time=_safe_schedule_time(
                _env_first("AUTONOMY_SCHEDULE_TIME", "AUTONOMY_TIME", "AUTONOMOUS_SCHEDULE_TIME", default="08:00")
            ),
            schedule_days=_safe_schedule_days(
                _env_first("AUTONOMY_SCHEDULE_DAYS", "AUTONOMY_DAYS", "AUTONOMOUS_SCHEDULE_DAYS", default="mon-fri")
            ),
            timezone=_safe_timezone(
                _env_first("AUTONOMY_TIMEZONE", "AUTONOMOUS_TIMEZONE", default="America/Phoenix")
            ),
            daily_budget_usd=_safe_float(
                _env_first("AUTONOMY_DAILY_BUDGET_USD", "AUTONOMOUS_DAILY_BUDGET_USD"), 5.0
            ),
            emergency_reserve_usd=_safe_float(
                _env_first("AUTONOMY_EMERGENCY_RESERVE_USD", "AUTONOMOUS_EMERGENCY_RESERVE_USD"), 0.25
            ),
            max_tasks_per_run=_safe_int(
                _env_first("AUTONOMY_MAX_TASKS_PER_RUN", "AUTONOMOUS_MAX_TASKS_PER_RUN"),
                10,
                minimum=1,
                maximum=50,
            ),
            max_ideas_per_run=_safe_int(
                _env_first("AUTONOMY_MAX_IDEAS_PER_RUN", "AUTONOMOUS_MAX_IDEAS_PER_RUN"), 3, minimum=0, maximum=10
            ),
            max_session_minutes=_safe_int(
                _env_first("AUTONOMY_MAX_SESSION_MINUTES", "AUTONOMOUS_MAX_SESSION_MINUTES"),
                120,
                minimum=1,
                maximum=1440,
            ),
            min_task_reservation_usd=_safe_float(
                _env_first("AUTONOMY_MIN_TASK_RESERVATION_USD"), 0.05, minimum=0.001
            ),
            idea_backlog_limit=_safe_int(
                _env_first("AUTONOMY_IDEA_BACKLOG_LIMIT", "AUTONOMOUS_IDEA_BACKLOG_LIMIT"), 50, minimum=1, maximum=1000
            ),
            max_execution_attempts=_safe_int(
                _env_first("AUTONOMY_MAX_EXECUTION_ATTEMPTS", "AUTONOMOUS_MAX_EXECUTION_ATTEMPTS"), 2, minimum=1, maximum=10
            ),
            stale_run_minutes=_safe_int(
                _env_first("AUTONOMY_STALE_RUN_MINUTES", "AUTONOMOUS_STALE_RUN_MINUTES"), 180, minimum=1
            ),
            max_authorization=_authorization(
                _env_first("AUTONOMY_MAX_AUTHORIZATION", "AUTONOMOUS_MAX_AUTHORIZATION", default="propose")
            ),
            data_dir=data_dir,
            roadmap_seed_path=seed_path,
            roadmap_pack_dir=pack_dir,
            lock_timeout_seconds=_safe_float(_env_first("AUTONOMY_LOCK_TIMEOUT_SECONDS"), 0.0),
        )


class CorruptAutonomyStateError(RuntimeError):
    """Raised while quarantined autonomy state still requires owner recovery."""

    def __init__(self, path: Path, quarantine_path: Path, recovery_path: Optional[Path] = None):
        super().__init__(f"Autonomy state was corrupt and quarantined at {quarantine_path}")
        self.path = path
        self.quarantine_path = quarantine_path
        self.recovery_path = recovery_path


class RoadmapItemRetryError(ValueError):
    """Raised when a persisted roadmap item cannot be safely reset for retry."""


class IdeaPromotionError(ValueError):
    """Raised when a proposed idea cannot be safely converted to roadmap work."""


class RoadmapPackError(ValueError):
    """Raised when an owner-approved roadmap pack cannot be queued safely."""


_SECRET_KEY_RE = re.compile(
    r"(?i)^(?:(?:[a-z0-9]+[_-])*(?:api[_-]?key|token|access[_-]?token|refresh[_-]?token|bot[_-]?token|password|secret|client[_-]?secret|private[_-]?key|database[_-]?url)|authorization|cookie|credential)$"
)
_SECRET_VALUE_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bgh(?:p|o|u|s|r)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(
        r"(?i)\b(?:[A-Z0-9_]*(?:API_KEY|TOKEN|PASSWORD|SECRET|PRIVATE_KEY)|AUTHORIZATION|DATABASE_URL)\s*[:=]\s*[^\s,;]+"
    ),
)


def _redact_text(value: str) -> str:
    redacted = value
    for pattern in _SECRET_VALUE_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    # URLs with embedded basic-auth credentials should never enter an audit log.
    redacted = re.sub(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@", r"\1[REDACTED]@", redacted)
    return redacted


def redact_secrets(value: Any) -> Any:
    """Recursively redact secret-looking keys and embedded credential values."""

    if isinstance(value, Mapping):
        cleaned = {}
        for key, child in value.items():
            key_text = str(key)
            cleaned[key] = "[REDACTED]" if _SECRET_KEY_RE.fullmatch(key_text) else redact_secrets(child)
        return cleaned
    if isinstance(value, list):
        return [redact_secrets(child) for child in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(child) for child in value)
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(redact_secrets(value), stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def _default_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "schema_version": STATE_VERSION,
        "projects": [],
        "idea_backlog": [],
        "roadmap_pack_history": [],
        "run_control": {
            "active_run": None,
            "scheduled_dates": {},
            "stale_recoveries": [],
            "recent_runs": [],
        },
        "budget_tracking": {
            "date": None,
            "actual_or_reconciled_cost_usd": 0.0,
            "cost_is_estimated": True,
        },
    }


def _normalize_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("Autonomy state must be a JSON object")
    state = deepcopy(raw)
    state["version"] = int(state.get("version", state.get("schema_version", STATE_VERSION)))
    state["schema_version"] = state["version"]
    if state["version"] > STATE_VERSION:
        raise ValueError(f"Unsupported autonomy state version {state['version']}")
    state.setdefault("projects", [])
    state.setdefault("idea_backlog", [])
    state.setdefault("roadmap_pack_history", [])
    state.setdefault("run_control", {})
    if not isinstance(state["run_control"], dict):
        raise ValueError("Autonomy run_control must be an object")
    state["run_control"].setdefault("active_run", None)
    state["run_control"].setdefault("scheduled_dates", {})
    state["run_control"].setdefault("stale_recoveries", [])
    state["run_control"].setdefault("recent_runs", [])
    if not isinstance(state["run_control"]["scheduled_dates"], dict):
        raise ValueError("Autonomy scheduled_dates must be an object")
    if state["run_control"]["active_run"] is not None and not isinstance(state["run_control"]["active_run"], dict):
        raise ValueError("Autonomy active_run must be an object or null")
    if not isinstance(state["run_control"]["stale_recoveries"], list):
        raise ValueError("Autonomy stale_recoveries must be a list")
    if not isinstance(state["run_control"]["recent_runs"], list):
        raise ValueError("Autonomy recent_runs must be a list")
    state.setdefault("budget_tracking", {})
    if not isinstance(state["budget_tracking"], dict):
        raise ValueError("Autonomy budget_tracking must be an object")
    state["budget_tracking"].setdefault("date", None)
    state["budget_tracking"].setdefault("actual_or_reconciled_cost_usd", 0.0)
    state["budget_tracking"].setdefault("cost_is_estimated", True)
    if (
        not isinstance(state["projects"], list)
        or not isinstance(state["idea_backlog"], list)
        or not isinstance(state["roadmap_pack_history"], list)
    ):
        raise ValueError(
            "Autonomy projects, idea_backlog, and roadmap_pack_history must be lists"
        )
    for project in state["projects"]:
        if not isinstance(project, dict):
            raise ValueError("Every autonomy project must be an object")
        project.setdefault("goals", [])
        project.setdefault("roadmap_items", project.pop("roadmap", []))
        if not isinstance(project["goals"], list) or not isinstance(project["roadmap_items"], list):
            raise ValueError("Project goals and roadmap_items must be lists")
        for item in project.get("roadmap_items", []):
            if not isinstance(item, dict):
                raise ValueError("Every autonomy roadmap item must be an object")
            item.setdefault("status", "planned")
            item.setdefault("priority", 0)
            item.setdefault("dependencies", [])
            item.setdefault("blockers", [])
            item.setdefault("acceptance_criteria", [])
            item.setdefault("agent_owner", "manager")
            item.setdefault("previous_attempts", [])
            item.setdefault("previous_models", [])
            item.setdefault("human_resolution_history", [])
            item.setdefault("human_decision_required", False)
            item.setdefault("human_action", "")
            item.setdefault("authorization_level", AuthorizationLevel.PROPOSE.value)
            for field in (
                "dependencies",
                "blockers",
                "acceptance_criteria",
                "previous_attempts",
                "previous_models",
                "human_resolution_history",
            ):
                if not isinstance(item[field], list):
                    raise ValueError(f"Roadmap item {field} must be a list")
            item["human_resolution_history"] = item["human_resolution_history"][
                -HUMAN_RESOLUTION_HISTORY_LIMIT:
            ]
    return state


def _active_promotion_projects(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    inactive = RETRY_BLOCKED_PROJECT_STATUSES | {"done"}
    return [
        project
        for project in state.get("projects", []) or []
        if isinstance(project, dict)
        and str(project.get("status") or "active").strip().lower() not in inactive
    ]


def _find_unique_idea(state: Mapping[str, Any], idea_id: str) -> dict[str, Any]:
    target_id = str(idea_id or "").strip()
    if not target_id:
        raise IdeaPromotionError("A non-empty idea ID is required for promotion.")
    matches = [
        idea
        for idea in state.get("idea_backlog", []) or []
        if isinstance(idea, dict) and str(idea.get("id") or "").strip() == target_id
    ]
    if not matches:
        raise IdeaPromotionError(f"Idea {target_id!r} was not found in the persistent backlog.")
    if len(matches) != 1:
        raise IdeaPromotionError(
            f"Idea ID {target_id!r} is ambiguous: {len(matches)} backlog records match; "
            "no state was changed."
        )
    return matches[0]


def _resolve_promotion_project(
    state: Mapping[str, Any],
    idea: Mapping[str, Any],
    requested_project_id: Optional[str],
) -> dict[str, Any]:
    active_projects = _active_promotion_projects(state)
    explicit_id = str(requested_project_id or "").strip()
    linked_id = str(idea.get("target_project_id") or "").strip()
    target_id = explicit_id or linked_id
    if target_id:
        matches = [
            project
            for project in active_projects
            if str(project.get("id") or "").strip() == target_id
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise IdeaPromotionError(
                f"Project ID {target_id!r} is ambiguous; no promotion was staged."
            )
        raise IdeaPromotionError(
            f"Project {target_id!r} is missing or inactive; no promotion was staged."
        )
    if len(active_projects) == 1:
        return active_projects[0]
    if not active_projects:
        raise IdeaPromotionError(
            "No active project can receive this idea; activate a project before promotion."
        )
    choices = ", ".join(
        str(project.get("id") or project.get("name") or "unknown")
        for project in active_projects[:10]
    )
    raise IdeaPromotionError(
        "Several active projects could receive this idea. Choose one explicitly with "
        f"/autorun promote {str(idea.get('id') or '<idea-id>')} <project-id>. "
        f"Active projects: {choices}."
    )


def _promotion_goal_id(project: Mapping[str, Any], idea: Mapping[str, Any]) -> Optional[str]:
    requested = str(idea.get("target_goal_id") or "").strip()
    goals = [goal for goal in project.get("goals", []) or [] if isinstance(goal, Mapping)]
    if requested:
        matches = [
            goal for goal in goals if str(goal.get("id") or "").strip() == requested
        ]
        if len(matches) != 1:
            raise IdeaPromotionError(
                f"Goal {requested!r} is missing or ambiguous; no promotion was staged."
            )
        goal_status = str(matches[0].get("status") or "active").strip().lower()
        if goal_status in {"archived", "cancelled", "complete", "completed", "done"}:
            raise IdeaPromotionError(
                f"Goal {requested!r} is inactive; no promotion was staged."
            )
        return requested
    active = [
        goal
        for goal in goals
        if str(goal.get("status") or "active").strip().lower()
        not in {"archived", "cancelled", "complete", "completed", "done"}
    ]
    return str(active[0].get("id")) if len(active) == 1 and active[0].get("id") else None


def _promotion_roadmap_id(state: Mapping[str, Any], idea_id: str) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "-", str(idea_id)).strip("-").upper()
    if token.startswith("IDEA-"):
        token = token[5:]
    token = token[:32] or hashlib.sha256(str(idea_id).encode("utf-8")).hexdigest()[:10].upper()
    candidate = f"AUTO-IDEA-{token}"
    existing_ids = {
        str(item.get("id") or "").strip()
        for project in state.get("projects", []) or []
        if isinstance(project, Mapping)
        for item in project.get("roadmap_items", []) or []
        if isinstance(item, Mapping)
    }
    if candidate in existing_ids:
        raise IdeaPromotionError(
            f"Roadmap item ID {candidate!r} already exists; no state was changed."
        )
    return candidate


def _promotion_acceptance_criteria(idea: Mapping[str, Any]) -> list[str]:
    next_step = re.sub(
        r"\s+", " ", str(idea.get("recommended_next_validation_step") or "")
    ).strip()[:600]
    expected_value = re.sub(
        r"\s+", " ", str(idea.get("expected_value") or "")
    ).strip()[:600]
    return [
        (
            f"Complete and document this validation step: {next_step}"
            if next_step
            else "Complete one bounded validation of the proposed idea and document the method."
        ),
        (
            f"Evaluate the evidence against the expected value: {expected_value}"
            if expected_value
            else "Evaluate the evidence against a clearly stated expected value."
        ),
        "Record the evidence and a build, revise, or reject recommendation in a reviewable deliverable.",
        "Do not implement, deploy, publish, or take another external action in this proposal-only task.",
    ]


def _promotion_description(idea: Mapping[str, Any]) -> str:
    fields = [
        ("Problem", idea.get("problem_addressed")),
        ("Expected value", idea.get("expected_value")),
        ("Target user", idea.get("target_user")),
        ("Relationship to goals", idea.get("relationship_to_current_goals")),
        ("Recommended validation", idea.get("recommended_next_validation_step")),
    ]
    return "\n".join(
        f"{label}: {str(value).strip()[:800]}"
        for label, value in fields
        if str(value or "").strip()
    )


def _idea_promotion_revision(idea: Mapping[str, Any]) -> str:
    fields = (
        "id",
        "idea",
        "problem_addressed",
        "expected_value",
        "target_user",
        "estimated_effort",
        "estimated_ai_cost_usd",
        "risks",
        "relationship_to_current_goals",
        "recommended_next_validation_step",
        "target_project_id",
        "target_goal_id",
        "status",
        "source_run_id",
    )
    payload = {field: idea.get(field) for field in fields}
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _build_idea_promotion(
    state: Mapping[str, Any],
    idea_id: str,
    requested_project_id: Optional[str] = None,
    *,
    expected_revision: Optional[str] = None,
    expected_roadmap_item_id: Optional[str] = None,
    expected_goal_id: Any = _UNSET,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    active_run = (state.get("run_control", {}) or {}).get("active_run")
    if active_run:
        run_id = str(active_run.get("run_id") or "unknown")
        raise IdeaPromotionError(
            f"Idea promotion is unavailable while autonomous run {run_id!r} is active."
        )
    idea = _find_unique_idea(state, idea_id)
    status = str(idea.get("status") or "proposed").strip().lower()
    if status != "proposed":
        raise IdeaPromotionError(
            f"Idea {str(idea.get('id'))!r} is {status!r}; only proposed ideas can be promoted."
        )
    revision = _idea_promotion_revision(idea)
    if expected_revision and revision != str(expected_revision):
        raise IdeaPromotionError(
            f"Idea {str(idea.get('id'))!r} changed after approval was staged; review it again."
        )
    existing_source_items = [
        item
        for candidate_project in state.get("projects", []) or []
        if isinstance(candidate_project, Mapping)
        for item in candidate_project.get("roadmap_items", []) or []
        if isinstance(item, Mapping)
        and str(item.get("source_idea_id") or "").strip() == str(idea.get("id") or "").strip()
    ]
    if existing_source_items:
        raise IdeaPromotionError(
            f"Idea {str(idea.get('id'))!r} already has linked roadmap work; "
            "no duplicate was created."
        )
    project = _resolve_promotion_project(state, idea, requested_project_id)
    roadmap_item_id = _promotion_roadmap_id(state, str(idea.get("id")))
    if expected_roadmap_item_id and roadmap_item_id != str(expected_roadmap_item_id):
        raise IdeaPromotionError(
            "The reviewed roadmap destination changed after approval was staged; review it again."
        )
    title = re.sub(r"\s+", " ", str(idea.get("idea") or "")).strip()
    if not title:
        raise IdeaPromotionError("The proposed idea has no title; no promotion was staged.")
    title = title[:200]
    if not str(idea.get("recommended_next_validation_step") or "").strip():
        raise IdeaPromotionError(
            f"Idea {str(idea.get('id'))!r} has no recommended validation step; "
            "no promotion was staged."
        )
    effort = str(idea.get("estimated_effort") or "").strip().lower()
    complexity = {"small": "lightweight", "medium": "standard", "large": "advanced"}.get(
        effort, "standard"
    )
    goal_id = _promotion_goal_id(project, idea)
    if expected_goal_id is not _UNSET and goal_id != expected_goal_id:
        raise IdeaPromotionError(
            "The reviewed roadmap goal changed after approval was staged; review it again."
        )
    acceptance_criteria = _promotion_acceptance_criteria(idea)
    description = _promotion_description(idea)
    roadmap_item = {
        "id": roadmap_item_id,
        "goal_id": goal_id,
        "title": f"Validate idea: {title}",
        "description": description,
        "priority": 50,
        "status": "ready",
        "dependencies": [],
        "blockers": [],
        "acceptance_criteria": acceptance_criteria,
        "agent_owner": "manager",
        "task_type": "planning",
        "complexity": complexity,
        "risk": "low",
        "required_capabilities": ["text", "reasoning", "planning"],
        "authorization_level": AuthorizationLevel.PROPOSE.value,
        "estimated_input_tokens": 2200,
        "estimated_output_tokens": 700,
        "estimated_ai_cost_usd": _money(idea.get("estimated_ai_cost_usd", 0.0)),
        "previous_attempts": [],
        "previous_models": [],
        "human_resolution_history": [],
        "human_decision_required": False,
        "human_action": "",
        "requires_recent_run_evidence": _requires_recent_run_evidence({
            "title": title,
            "description": description,
            "acceptance_criteria": acceptance_criteria,
        }),
        "source_idea_id": str(idea.get("id")),
        "source_idea_fingerprint": str(idea.get("fingerprint") or _idea_fingerprint(idea)),
        "source_run_id": str(idea.get("source_run_id") or ""),
        "proposal_revision": revision,
    }
    return idea, project, roadmap_item


_ROADMAP_PACK_TOP_LEVEL_FIELDS = {
    "schema_version",
    "manifest_id",
    "summary",
    "target_project_id",
    "goal",
    "roadmap_items",
    "revenue_sprint",
}
_ROADMAP_PACK_GOAL_FIELDS = {"id", "title", "description", "status"}
_ROADMAP_PACK_ITEM_FIELDS = {
    "id",
    "goal_id",
    "title",
    "description",
    "priority",
    "status",
    "dependencies",
    "blockers",
    "acceptance_criteria",
    "agent_owner",
    "task_type",
    "complexity",
    "risk",
    "required_capabilities",
    "authorization_level",
    "estimated_input_tokens",
    "estimated_cached_input_tokens",
    "estimated_output_tokens",
    "estimated_ai_cost_usd",
    "requires_recent_run_evidence",
    "previous_attempts",
    "previous_models",
    "human_decision_required",
    "human_action",
    "revenue_sprint_run_day",
    "external_action",
}
_REVENUE_SPRINT_FIELDS = {
    "id",
    "product",
    "channel",
    "total_ai_budget_usd",
    "daily_ai_budget_usd",
    "daily_budget_includes_emergency_reserve",
    "run_days",
    "checkpoint_thresholds",
    "action_policy",
}
_REVENUE_SPRINT_PRODUCT_FIELDS = {"id", "name", "url"}
_REVENUE_SPRINT_CHANNEL_FIELDS = {"id"}
_REVENUE_SPRINT_CHECKPOINT_FIELDS = {
    "day_5_meaningful_interest",
    "day_15_sale_or_strong_intent",
    "day_20_unconditional_stop",
    "max_consecutive_no_progress_days",
    "trailing_window_days",
    "minimum_gross_revenue_usd_per_day",
    "minimum_trailing_gross_revenue_usd",
    "require_nonnegative_contribution",
}
_REVENUE_SPRINT_DAY_5_FIELDS = {"run_day", "minimum_meaningful_interactions"}
_REVENUE_SPRINT_DAY_15_FIELDS = {
    "run_day",
    "minimum_sales",
    "minimum_strong_intent_signals",
    "satisfy",
}
_REVENUE_SPRINT_DAY_20_FIELDS = {"run_day", "unconditional_stop"}
_REVENUE_SPRINT_ACTION_POLICY_FIELDS = {
    "revision",
    "require_owner_confirmation",
    "allowed_external_actions",
    "daily_purchase_cap_usd",
    "total_purchase_cap_usd",
}
_REVENUE_SPRINT_POLICY_ACTION_FIELDS = {
    "action_type",
    "target",
    "daily_cap",
    "total_cap",
}
_REVENUE_SPRINT_ITEM_ACTION_FIELDS = {"action_type", "target", "policy_revision"}
_ROADMAP_RECORD_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
_ROADMAP_PACK_GOAL_HASH_FIELDS = ("id", "title", "description")
_ROADMAP_PACK_ITEM_HASH_FIELDS = (
    "id",
    "goal_id",
    "title",
    "description",
    "priority",
    "dependencies",
    "acceptance_criteria",
    "agent_owner",
    "task_type",
    "complexity",
    "risk",
    "required_capabilities",
    "authorization_level",
    "estimated_input_tokens",
    "estimated_cached_input_tokens",
    "estimated_output_tokens",
    "estimated_ai_cost_usd",
    "requires_recent_run_evidence",
)
_ROADMAP_PACK_GOAL_HASH_FIELDS_V2 = (*_ROADMAP_PACK_GOAL_HASH_FIELDS, "revenue_sprint")
_ROADMAP_PACK_ITEM_HASH_FIELDS_V2 = (
    *_ROADMAP_PACK_ITEM_HASH_FIELDS,
    "revenue_sprint_id",
    "revenue_sprint_run_day",
    "action_policy_revision",
    "external_action",
)


def _roadmap_pack_revision(manifest: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        manifest,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _roadmap_pack_record_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _roadmap_pack_record_projection(
    value: Mapping[str, Any],
    fields: tuple[str, ...],
) -> dict[str, Any]:
    return {field: deepcopy(value.get(field)) for field in fields}


def _load_roadmap_pack(
    pack_dir: Path,
    manifest_id: str,
) -> tuple[dict[str, Any], str]:
    """Load one repository-owned pack without permitting path traversal or symlinks."""

    requested_id = str(manifest_id or "").strip()
    if not ROADMAP_PACK_ID_RE.fullmatch(requested_id):
        raise RoadmapPackError(
            "Roadmap pack IDs must use lowercase letters, numbers, dots, dashes, or "
            "underscores and be at most 80 characters."
        )
    root = Path(pack_dir)
    if not root.is_absolute():
        root = BASE_DIR / root
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise RoadmapPackError(
            f"The configured roadmap-pack directory is unavailable for {requested_id!r}."
        ) from exc
    source = resolved_root / f"{requested_id}.json"
    if source.is_symlink():
        raise RoadmapPackError("Roadmap pack symlinks are not accepted.")
    try:
        resolved_source = source.resolve(strict=True)
        resolved_source.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise RoadmapPackError(
            f"Roadmap pack {requested_id!r} was not found in the configured pack directory."
        ) from exc
    if not resolved_source.is_file():
        raise RoadmapPackError(f"Roadmap pack {requested_id!r} is not a regular file.")
    try:
        with resolved_source.open("rb") as stream:
            raw = stream.read(ROADMAP_PACK_MAX_BYTES + 1)
        if len(raw) > ROADMAP_PACK_MAX_BYTES:
            raise RoadmapPackError(
                f"Roadmap pack {requested_id!r} exceeds the {ROADMAP_PACK_MAX_BYTES}-byte limit."
            )
        loaded = json.loads(raw.decode("utf-8"))
    except RoadmapPackError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise RoadmapPackError(
            f"Roadmap pack {requested_id!r} is not readable valid JSON."
        ) from exc
    if not isinstance(loaded, dict):
        raise RoadmapPackError("A roadmap pack must be a JSON object.")
    if str(loaded.get("manifest_id") or "").strip() != requested_id:
        raise RoadmapPackError(
            "The roadmap pack manifest_id does not match its requested filename."
        )
    try:
        revision = _roadmap_pack_revision(loaded)
    except (TypeError, ValueError) as exc:
        raise RoadmapPackError("The roadmap pack contains unsupported JSON values.") from exc
    return deepcopy(loaded), revision


def _pack_text(
    value: Any,
    field: str,
    *,
    maximum: int = 4000,
) -> str:
    if not isinstance(value, str):
        raise RoadmapPackError(f"Roadmap pack field {field!r} must be text.")
    text = value.strip()
    if not text:
        raise RoadmapPackError(f"Roadmap pack field {field!r} must be non-empty.")
    if len(text) > maximum:
        raise RoadmapPackError(
            f"Roadmap pack field {field!r} exceeds its {maximum}-character limit."
        )
    return text


def _pack_string_list(
    value: Any,
    field: str,
    *,
    require_values: bool = False,
    maximum_items: int = 20,
    maximum_chars: int = 2000,
) -> list[str]:
    if not isinstance(value, list):
        raise RoadmapPackError(f"Roadmap pack field {field!r} must be a list.")
    if len(value) > maximum_items:
        raise RoadmapPackError(
            f"Roadmap pack field {field!r} exceeds its {maximum_items}-item limit."
        )
    result: list[str] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, str):
            raise RoadmapPackError(
                f"Roadmap pack field {field!r} entry {index + 1} must be text."
            )
        result.append(_pack_text(entry, f"{field}[{index}]", maximum=maximum_chars))
    if require_values and not result:
        raise RoadmapPackError(f"Roadmap pack field {field!r} must not be empty.")
    if len(result) != len(set(result)):
        raise RoadmapPackError(f"Roadmap pack field {field!r} contains duplicates.")
    return result


def _pack_mapping(value: Any, field: str, allowed_fields: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RoadmapPackError(f"Roadmap pack field {field!r} must be an object.")
    unknown = set(value) - allowed_fields
    if unknown:
        raise RoadmapPackError(
            f"Roadmap pack field {field!r} contains unsupported fields: "
            f"{', '.join(sorted(unknown))}."
        )
    return value


def _pack_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RoadmapPackError(f"Roadmap pack field {field!r} must be a positive integer.")
    return value


def _pack_positive_number(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise RoadmapPackError(f"Roadmap pack field {field!r} must be a positive number.")
    return float(value)


def _prepare_revenue_sprint(raw: Any) -> dict[str, Any]:
    sprint = _pack_mapping(raw, "revenue_sprint", _REVENUE_SPRINT_FIELDS)
    missing = _REVENUE_SPRINT_FIELDS - set(sprint)
    if missing:
        raise RoadmapPackError(
            "Roadmap pack revenue_sprint is missing required fields: "
            f"{', '.join(sorted(missing))}."
        )

    sprint_id = _pack_text(sprint.get("id"), "revenue_sprint.id", maximum=80)
    if not ROADMAP_PACK_ID_RE.fullmatch(sprint_id):
        raise RoadmapPackError("Roadmap pack revenue_sprint.id has an invalid format.")

    product = _pack_mapping(
        sprint.get("product"), "revenue_sprint.product", _REVENUE_SPRINT_PRODUCT_FIELDS
    )
    if set(product) != _REVENUE_SPRINT_PRODUCT_FIELDS:
        raise RoadmapPackError(
            "Roadmap pack revenue_sprint.product must contain exactly id, name, and url."
        )
    product_id = _pack_text(product.get("id"), "revenue_sprint.product.id", maximum=120)
    if not _ROADMAP_RECORD_ID_RE.fullmatch(product_id):
        raise RoadmapPackError("Roadmap pack revenue_sprint.product.id has an invalid format.")
    product_name = _pack_text(product.get("name"), "revenue_sprint.product.name", maximum=240)
    product_url = _pack_text(product.get("url"), "revenue_sprint.product.url", maximum=1000)
    parsed_url = urlsplit(product_url)
    hostname = str(parsed_url.hostname or "").lower()
    if (
        parsed_url.scheme.lower() != "https"
        or not (hostname == "gumroad.com" or hostname.endswith(".gumroad.com"))
        or parsed_url.username is not None
        or parsed_url.password is not None
        or not parsed_url.path.strip("/")
        or parsed_url.query
        or parsed_url.fragment
    ):
        raise RoadmapPackError(
            "Roadmap pack revenue_sprint.product.url must be a specific HTTPS Gumroad product URL."
        )

    channel = _pack_mapping(
        sprint.get("channel"), "revenue_sprint.channel", _REVENUE_SPRINT_CHANNEL_FIELDS
    )
    if set(channel) != _REVENUE_SPRINT_CHANNEL_FIELDS:
        raise RoadmapPackError("Roadmap pack revenue_sprint.channel must contain exactly id.")
    channel_id = _pack_text(channel.get("id"), "revenue_sprint.channel.id", maximum=160)
    if not REVENUE_SPRINT_ACTION_TARGET_RE.fullmatch(channel_id) or "*" in channel_id:
        raise RoadmapPackError(
            "Roadmap pack revenue_sprint.channel.id must name a concrete, namespaced channel."
        )

    total_budget = _pack_positive_number(
        sprint.get("total_ai_budget_usd"), "revenue_sprint.total_ai_budget_usd"
    )
    daily_budget = _pack_positive_number(
        sprint.get("daily_ai_budget_usd"), "revenue_sprint.daily_ai_budget_usd"
    )
    if not math.isclose(total_budget, REVENUE_SPRINT_TOTAL_AI_BUDGET_USD):
        raise RoadmapPackError(
            f"Roadmap pack revenue_sprint.total_ai_budget_usd must be "
            f"{REVENUE_SPRINT_TOTAL_AI_BUDGET_USD:.2f}."
        )
    if not math.isclose(daily_budget, REVENUE_SPRINT_DAILY_AI_BUDGET_USD):
        raise RoadmapPackError(
            f"Roadmap pack revenue_sprint.daily_ai_budget_usd must be "
            f"{REVENUE_SPRINT_DAILY_AI_BUDGET_USD:.2f}."
        )
    if sprint.get("daily_budget_includes_emergency_reserve") is not True:
        raise RoadmapPackError(
            "Roadmap pack revenue_sprint.daily_budget_includes_emergency_reserve must be true."
        )
    run_days = _pack_positive_int(sprint.get("run_days"), "revenue_sprint.run_days")
    if run_days != REVENUE_SPRINT_RUN_DAYS:
        raise RoadmapPackError(
            f"Roadmap pack revenue_sprint.run_days must be {REVENUE_SPRINT_RUN_DAYS}."
        )

    checkpoints = _pack_mapping(
        sprint.get("checkpoint_thresholds"),
        "revenue_sprint.checkpoint_thresholds",
        _REVENUE_SPRINT_CHECKPOINT_FIELDS,
    )
    if set(checkpoints) != _REVENUE_SPRINT_CHECKPOINT_FIELDS:
        missing_checkpoints = _REVENUE_SPRINT_CHECKPOINT_FIELDS - set(checkpoints)
        raise RoadmapPackError(
            "Roadmap pack revenue_sprint.checkpoint_thresholds is missing required fields: "
            f"{', '.join(sorted(missing_checkpoints))}."
        )
    day_5 = _pack_mapping(
        checkpoints.get("day_5_meaningful_interest"),
        "revenue_sprint.checkpoint_thresholds.day_5_meaningful_interest",
        _REVENUE_SPRINT_DAY_5_FIELDS,
    )
    if set(day_5) != _REVENUE_SPRINT_DAY_5_FIELDS or day_5.get("run_day") != 5:
        raise RoadmapPackError("The meaningful-interest checkpoint must be fully specified for run day 5.")
    day_5_minimum = _pack_positive_int(
        day_5.get("minimum_meaningful_interactions"),
        "revenue_sprint.checkpoint_thresholds.day_5_meaningful_interest.minimum_meaningful_interactions",
    )
    day_15 = _pack_mapping(
        checkpoints.get("day_15_sale_or_strong_intent"),
        "revenue_sprint.checkpoint_thresholds.day_15_sale_or_strong_intent",
        _REVENUE_SPRINT_DAY_15_FIELDS,
    )
    if set(day_15) != _REVENUE_SPRINT_DAY_15_FIELDS or day_15.get("run_day") != 15:
        raise RoadmapPackError("The sale-or-strong-intent checkpoint must be fully specified for run day 15.")
    day_15_sales = _pack_positive_int(
        day_15.get("minimum_sales"),
        "revenue_sprint.checkpoint_thresholds.day_15_sale_or_strong_intent.minimum_sales",
    )
    day_15_intent = _pack_positive_int(
        day_15.get("minimum_strong_intent_signals"),
        "revenue_sprint.checkpoint_thresholds.day_15_sale_or_strong_intent.minimum_strong_intent_signals",
    )
    if day_15.get("satisfy") != "any":
        raise RoadmapPackError("The day-15 checkpoint satisfy field must be 'any'.")
    day_20 = _pack_mapping(
        checkpoints.get("day_20_unconditional_stop"),
        "revenue_sprint.checkpoint_thresholds.day_20_unconditional_stop",
        _REVENUE_SPRINT_DAY_20_FIELDS,
    )
    if (
        set(day_20) != _REVENUE_SPRINT_DAY_20_FIELDS
        or day_20.get("run_day") != 20
        or day_20.get("unconditional_stop") is not True
    ):
        raise RoadmapPackError("The day-20 checkpoint must be an unconditional stop on run day 20.")
    no_progress_days = _pack_positive_int(
        checkpoints.get("max_consecutive_no_progress_days"),
        "revenue_sprint.checkpoint_thresholds.max_consecutive_no_progress_days",
    )
    if no_progress_days != 3:
        raise RoadmapPackError("The revenue sprint must stop after 3 consecutive no-progress days.")
    trailing_days = _pack_positive_int(
        checkpoints.get("trailing_window_days"),
        "revenue_sprint.checkpoint_thresholds.trailing_window_days",
    )
    if trailing_days > run_days:
        raise RoadmapPackError("The revenue sprint trailing window cannot exceed its run days.")
    minimum_per_day = _pack_positive_number(
        checkpoints.get("minimum_gross_revenue_usd_per_day"),
        "revenue_sprint.checkpoint_thresholds.minimum_gross_revenue_usd_per_day",
    )
    minimum_trailing = _pack_positive_number(
        checkpoints.get("minimum_trailing_gross_revenue_usd"),
        "revenue_sprint.checkpoint_thresholds.minimum_trailing_gross_revenue_usd",
    )
    if not math.isclose(minimum_trailing, minimum_per_day * trailing_days):
        raise RoadmapPackError(
            "The trailing gross-revenue threshold must equal the per-day threshold times the trailing window."
        )
    if checkpoints.get("require_nonnegative_contribution") is not True:
        raise RoadmapPackError("The revenue sprint must require nonnegative contribution.")

    action_policy = _pack_mapping(
        sprint.get("action_policy"),
        "revenue_sprint.action_policy",
        _REVENUE_SPRINT_ACTION_POLICY_FIELDS,
    )
    required_policy_fields = {
        "revision",
        "require_owner_confirmation",
        "allowed_external_actions",
    }
    if not required_policy_fields.issubset(action_policy):
        raise RoadmapPackError(
            "Roadmap pack revenue_sprint.action_policy must contain revision, "
            "require_owner_confirmation, and allowed_external_actions."
        )
    revision = _pack_text(
        action_policy.get("revision"), "revenue_sprint.action_policy.revision", maximum=120
    )
    if not _ROADMAP_RECORD_ID_RE.fullmatch(revision):
        raise RoadmapPackError("Roadmap pack revenue_sprint.action_policy.revision has an invalid format.")
    if action_policy.get("require_owner_confirmation") is not True:
        raise RoadmapPackError(
            "Roadmap pack revenue_sprint.action_policy.require_owner_confirmation must be true."
        )
    raw_actions = action_policy.get("allowed_external_actions")
    if not isinstance(raw_actions, list) or not 1 <= len(raw_actions) <= 8:
        raise RoadmapPackError(
            "Roadmap pack revenue_sprint.action_policy.allowed_external_actions must contain 1 to 8 actions."
        )
    clean_actions: list[dict[str, Any]] = []
    action_pairs: set[tuple[str, str]] = set()
    for index, raw_action in enumerate(raw_actions):
        prefix = f"revenue_sprint.action_policy.allowed_external_actions[{index}]"
        action = _pack_mapping(raw_action, prefix, _REVENUE_SPRINT_POLICY_ACTION_FIELDS)
        if set(action) != _REVENUE_SPRINT_POLICY_ACTION_FIELDS:
            raise RoadmapPackError(
                f"Roadmap pack field {prefix!r} must contain exactly action_type, target, daily_cap, and total_cap."
            )
        action_type = _pack_text(action.get("action_type"), f"{prefix}.action_type", maximum=40).lower()
        if action_type not in REVENUE_SPRINT_KNOWN_ACTION_TYPES:
            raise RoadmapPackError(
                f"Roadmap pack field {prefix!r} has an unsupported external action type."
            )
        target = _pack_text(action.get("target"), f"{prefix}.target", maximum=240)
        if not REVENUE_SPRINT_ACTION_TARGET_RE.fullmatch(target) or "*" in target:
            raise RoadmapPackError(
                f"Roadmap pack field {prefix!r} must name a concrete, namespaced target."
            )
        if action_type in {"publish", "outreach"} and target != channel_id:
            raise RoadmapPackError(
                f"Roadmap pack {action_type} actions must target the configured sprint channel."
            )
        daily_cap = _pack_positive_int(action.get("daily_cap"), f"{prefix}.daily_cap")
        total_cap = _pack_positive_int(action.get("total_cap"), f"{prefix}.total_cap")
        if daily_cap > total_cap:
            raise RoadmapPackError(f"Roadmap pack field {prefix!r} daily_cap cannot exceed total_cap.")
        pair = (action_type, target)
        if pair in action_pairs:
            raise RoadmapPackError("Roadmap pack revenue_sprint action policy contains duplicate actions.")
        action_pairs.add(pair)
        clean_actions.append(
            {
                "action_type": action_type,
                "target": target,
                "daily_cap": daily_cap,
                "total_cap": total_cap,
            }
        )

    purchase_present = any(action["action_type"] == "purchase" for action in clean_actions)
    purchase_cap_fields = {"daily_purchase_cap_usd", "total_purchase_cap_usd"}
    purchase_caps_present = purchase_cap_fields & set(action_policy)
    clean_policy: dict[str, Any] = {
        "revision": revision,
        "require_owner_confirmation": True,
        "allowed_external_actions": clean_actions,
    }
    if purchase_present:
        if purchase_caps_present != purchase_cap_fields:
            raise RoadmapPackError(
                "Purchase authorization requires both daily_purchase_cap_usd and total_purchase_cap_usd."
            )
        daily_purchase_cap = _pack_positive_number(
            action_policy.get("daily_purchase_cap_usd"),
            "revenue_sprint.action_policy.daily_purchase_cap_usd",
        )
        total_purchase_cap = _pack_positive_number(
            action_policy.get("total_purchase_cap_usd"),
            "revenue_sprint.action_policy.total_purchase_cap_usd",
        )
        if daily_purchase_cap > total_purchase_cap:
            raise RoadmapPackError("The daily purchase cap cannot exceed the total purchase cap.")
        clean_policy["daily_purchase_cap_usd"] = _money(daily_purchase_cap)
        clean_policy["total_purchase_cap_usd"] = _money(total_purchase_cap)
    elif purchase_caps_present:
        raise RoadmapPackError("Purchase caps are not permitted when no purchase action is authorized.")

    return {
        "id": sprint_id,
        "product": {"id": product_id, "name": product_name, "url": product_url},
        "channel": {"id": channel_id},
        "total_ai_budget_usd": _money(total_budget),
        "daily_ai_budget_usd": _money(daily_budget),
        "daily_budget_includes_emergency_reserve": True,
        "run_days": run_days,
        "checkpoint_thresholds": {
            "day_5_meaningful_interest": {
                "run_day": 5,
                "minimum_meaningful_interactions": day_5_minimum,
            },
            "day_15_sale_or_strong_intent": {
                "run_day": 15,
                "minimum_sales": day_15_sales,
                "minimum_strong_intent_signals": day_15_intent,
                "satisfy": "any",
            },
            "day_20_unconditional_stop": {"run_day": 20, "unconditional_stop": True},
            "max_consecutive_no_progress_days": no_progress_days,
            "trailing_window_days": trailing_days,
            "minimum_gross_revenue_usd_per_day": _money(minimum_per_day),
            "minimum_trailing_gross_revenue_usd": _money(minimum_trailing),
            "require_nonnegative_contribution": True,
        },
        "action_policy": clean_policy,
    }


def _prepare_revenue_sprint_item_action(
    raw: Any,
    *,
    field: str,
    policy_revision: str,
    allowed_pairs: set[tuple[str, str]],
) -> dict[str, str]:
    action = _pack_mapping(raw, field, _REVENUE_SPRINT_ITEM_ACTION_FIELDS)
    if set(action) != _REVENUE_SPRINT_ITEM_ACTION_FIELDS:
        raise RoadmapPackError(
            f"Roadmap pack field {field!r} must contain exactly action_type, target, and policy_revision."
        )
    action_type = _pack_text(action.get("action_type"), f"{field}.action_type", maximum=40).lower()
    target = _pack_text(action.get("target"), f"{field}.target", maximum=240)
    item_revision = _pack_text(
        action.get("policy_revision"), f"{field}.policy_revision", maximum=120
    )
    if item_revision != policy_revision or (action_type, target) not in allowed_pairs:
        raise RoadmapPackError(
            f"Roadmap pack field {field!r} does not exactly match the revision-bound action policy."
        )
    return {
        "action_type": action_type,
        "target": target,
        "policy_revision": item_revision,
    }


def _roadmap_pack_records_are_intact(
    state: Mapping[str, Any],
    receipt: Mapping[str, Any],
    *,
    manifest_id: str,
    revision: str,
    project_id: str,
    goal_record: Mapping[str, Any],
    item_records: list[Mapping[str, Any]],
) -> bool:
    goal_id = str(goal_record.get("id") or "")
    item_ids = [str(item.get("id") or "") for item in item_records]
    record_hash_version = receipt.get("record_hash_version", 1)
    if record_hash_version not in {1, 2}:
        return False
    goal_hash_fields = (
        _ROADMAP_PACK_GOAL_HASH_FIELDS_V2
        if record_hash_version == 2
        else _ROADMAP_PACK_GOAL_HASH_FIELDS
    )
    item_hash_fields = (
        _ROADMAP_PACK_ITEM_HASH_FIELDS_V2
        if record_hash_version == 2
        else _ROADMAP_PACK_ITEM_HASH_FIELDS
    )
    receipt_item_ids = receipt.get("roadmap_item_ids")
    receipt_item_hashes = receipt.get("roadmap_item_hashes")
    if not isinstance(receipt_item_ids, list) or not isinstance(
        receipt_item_hashes, Mapping
    ):
        return False
    goal_hash = _roadmap_pack_record_hash(
        _roadmap_pack_record_projection(goal_record, goal_hash_fields)
    )
    item_hashes = {
        str(item.get("id") or ""): _roadmap_pack_record_hash(
            _roadmap_pack_record_projection(item, item_hash_fields)
        )
        for item in item_records
    }
    if (
        str(receipt.get("manifest_revision") or "") != revision
        or str(receipt.get("project_id") or "") != project_id
        or str(receipt.get("goal_id") or "") != goal_id
        or receipt_item_ids != item_ids
        or str(receipt.get("goal_record_hash") or "") != goal_hash
        or dict(receipt_item_hashes) != item_hashes
    ):
        return False
    if record_hash_version == 2:
        sprint = goal_record.get("revenue_sprint")
        if not isinstance(sprint, Mapping):
            return False
        sprint_hash = _roadmap_pack_record_hash(sprint)
        if (
            receipt.get("revenue_sprint") != sprint
            or str(receipt.get("revenue_sprint_hash") or "") != sprint_hash
        ):
            return False
    projects = [
        project
        for project in state.get("projects", []) or []
        if isinstance(project, Mapping)
        and str(project.get("id") or "").strip() == project_id
    ]
    if len(projects) != 1:
        return False
    project = projects[0]
    goals = [
        goal
        for goal in project.get("goals", []) or []
        if isinstance(goal, Mapping)
        and str(goal.get("id") or "").strip() == goal_id
        and str(goal.get("source_manifest_id") or "") == manifest_id
        and str(goal.get("source_manifest_revision") or "") == revision
    ]
    if len(goals) != 1:
        return False
    persisted_goal = goals[0]
    if (
        str(persisted_goal.get("source_manifest_record_hash") or "") != goal_hash
        or _roadmap_pack_record_hash(
            _roadmap_pack_record_projection(
                persisted_goal, goal_hash_fields
            )
        )
        != goal_hash
    ):
        return False
    for item_id, expected_hash in item_hashes.items():
        matches = [
            (candidate_project, item)
            for candidate_project in state.get("projects", []) or []
            if isinstance(candidate_project, Mapping)
            for item in candidate_project.get("roadmap_items", []) or []
            if isinstance(item, Mapping)
            and str(item.get("id") or "").strip() == item_id
        ]
        if len(matches) != 1:
            return False
        candidate_project, persisted_item = matches[0]
        if (
            str(candidate_project.get("id") or "").strip() != project_id
            or str(persisted_item.get("source_manifest_id") or "") != manifest_id
            or str(persisted_item.get("source_manifest_revision") or "") != revision
            or str(persisted_item.get("source_manifest_record_hash") or "")
            != expected_hash
            or _roadmap_pack_record_hash(
                _roadmap_pack_record_projection(
                    persisted_item, item_hash_fields
                )
            )
            != expected_hash
        ):
            return False
    return True


def _prepare_roadmap_pack(
    state: Mapping[str, Any],
    manifest: Mapping[str, Any],
    revision: str,
    *,
    queued_at: datetime,
    approval_source: str,
    backup_filename: Optional[str] = None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Validate a pack and build an additive candidate state without mutating input."""

    active_run = (state.get("run_control", {}) or {}).get("active_run")
    if active_run:
        run_id = str(active_run.get("run_id") or "unknown")
        raise RoadmapPackError(
            f"Roadmap queueing is unavailable while autonomous run {run_id!r} is active."
        )
    unknown_top = set(manifest) - _ROADMAP_PACK_TOP_LEVEL_FIELDS
    if unknown_top:
        raise RoadmapPackError(
            f"Roadmap pack contains unsupported top-level fields: {', '.join(sorted(unknown_top))}."
        )
    if manifest.get("schema_version") != 1:
        raise RoadmapPackError("Roadmap pack schema_version must be 1.")
    manifest_id = _pack_text(manifest.get("manifest_id"), "manifest_id", maximum=80)
    if not ROADMAP_PACK_ID_RE.fullmatch(manifest_id):
        raise RoadmapPackError("Roadmap pack manifest_id has an invalid format.")
    project_id = _pack_text(
        manifest.get("target_project_id"), "target_project_id", maximum=120
    )
    projects = [
        project
        for project in state.get("projects", []) or []
        if isinstance(project, dict)
        and str(project.get("id") or "").strip() == project_id
    ]
    if len(projects) != 1:
        raise RoadmapPackError(
            f"Target project {project_id!r} is missing or ambiguous; no state was changed."
        )
    project = projects[0]
    project_status = str(project.get("status") or "active").strip().lower()
    if project_status in RETRY_BLOCKED_PROJECT_STATUSES | {"done"}:
        raise RoadmapPackError(
            f"Target project {project_id!r} is {project_status!r}; activate it before queueing."
        )

    revenue_sprint = (
        _prepare_revenue_sprint(manifest.get("revenue_sprint"))
        if "revenue_sprint" in manifest
        else None
    )
    sprint_policy_revision = (
        str(revenue_sprint["action_policy"]["revision"]) if revenue_sprint else ""
    )
    sprint_allowed_action_pairs = (
        {
            (str(action["action_type"]), str(action["target"]))
            for action in revenue_sprint["action_policy"]["allowed_external_actions"]
        }
        if revenue_sprint
        else set()
    )

    raw_goal = manifest.get("goal")
    if not isinstance(raw_goal, Mapping):
        raise RoadmapPackError("Roadmap pack goal must be an object.")
    unknown_goal = set(raw_goal) - _ROADMAP_PACK_GOAL_FIELDS
    if unknown_goal:
        raise RoadmapPackError(
            f"Roadmap pack goal contains unsupported fields: {', '.join(sorted(unknown_goal))}."
        )
    goal_id = _pack_text(raw_goal.get("id"), "goal.id", maximum=120)
    if not _ROADMAP_RECORD_ID_RE.fullmatch(goal_id):
        raise RoadmapPackError("Roadmap pack goal.id has an invalid format.")
    goal_title = _pack_text(raw_goal.get("title"), "goal.title", maximum=240)
    goal_description = _pack_text(
        raw_goal.get("description"), "goal.description", maximum=4000
    )
    goal_status = str(raw_goal.get("status") or "active").strip().lower()
    if goal_status != "active":
        raise RoadmapPackError("A newly queued roadmap-pack goal must have status 'active'.")
    clean_goal_record = {
        "id": goal_id,
        "title": goal_title,
        "description": goal_description,
        "status": "active",
    }
    if revenue_sprint is not None:
        clean_goal_record["revenue_sprint"] = deepcopy(revenue_sprint)

    raw_items = manifest.get("roadmap_items")
    if not isinstance(raw_items, list) or not 1 <= len(raw_items) <= ROADMAP_PACK_MAX_ITEMS:
        raise RoadmapPackError(
            f"Roadmap pack roadmap_items must contain 1 to {ROADMAP_PACK_MAX_ITEMS} items."
        )
    if revenue_sprint is not None and len(raw_items) != REVENUE_SPRINT_RUN_DAYS:
        raise RoadmapPackError(
            f"A revenue sprint roadmap pack must contain exactly {REVENUE_SPRINT_RUN_DAYS} items."
        )
    clean_items: list[dict[str, Any]] = []
    item_ids: list[str] = []
    authorization_levels: set[str] = set()
    sprint_run_days: set[int] = set()
    for index, raw_item in enumerate(raw_items):
        prefix = f"roadmap_items[{index}]"
        if not isinstance(raw_item, Mapping):
            raise RoadmapPackError(f"Roadmap pack {prefix} must be an object.")
        unknown_item = set(raw_item) - _ROADMAP_PACK_ITEM_FIELDS
        if unknown_item:
            raise RoadmapPackError(
                f"Roadmap pack {prefix} contains unsupported fields: "
                f"{', '.join(sorted(unknown_item))}."
            )
        item_id = _pack_text(raw_item.get("id"), f"{prefix}.id", maximum=120)
        if not _ROADMAP_RECORD_ID_RE.fullmatch(item_id):
            raise RoadmapPackError(f"Roadmap pack item ID {item_id!r} has an invalid format.")
        item_goal_id = _pack_text(
            raw_item.get("goal_id"), f"{prefix}.goal_id", maximum=120
        )
        if item_goal_id != goal_id:
            raise RoadmapPackError(
                f"Roadmap pack item {item_id!r} must reference goal {goal_id!r}."
            )
        status = str(raw_item.get("status") or "").strip().lower()
        if status != "ready":
            raise RoadmapPackError(
                f"Roadmap pack item {item_id!r} must begin with status 'ready'."
            )
        authorization = str(raw_item.get("authorization_level") or "").strip().lower()
        allowed_authorizations = {
            AuthorizationLevel.OBSERVE.value,
            AuthorizationLevel.PROPOSE.value,
        }
        if revenue_sprint is not None:
            allowed_authorizations.add(AuthorizationLevel.EXTERNAL_ACTION.value)
        if authorization not in allowed_authorizations:
            if revenue_sprint is None:
                raise RoadmapPackError(
                    f"Roadmap pack item {item_id!r} exceeds the observe/propose authorization limit."
                )
            raise RoadmapPackError(
                f"Roadmap pack item {item_id!r} exceeds the revenue-sprint authorization policy."
            )
        sprint_run_day: Optional[int] = None
        clean_external_action: Optional[dict[str, str]] = None
        if revenue_sprint is None:
            if "revenue_sprint_run_day" in raw_item or "external_action" in raw_item:
                raise RoadmapPackError(
                    f"Non-sprint roadmap item {item_id!r} cannot contain revenue-sprint fields."
                )
        else:
            sprint_run_day = _pack_positive_int(
                raw_item.get("revenue_sprint_run_day"),
                f"{prefix}.revenue_sprint_run_day",
            )
            if sprint_run_day > REVENUE_SPRINT_RUN_DAYS:
                raise RoadmapPackError(
                    f"Roadmap pack item {item_id!r} has a revenue_sprint_run_day outside 1..{REVENUE_SPRINT_RUN_DAYS}."
                )
            if sprint_run_day in sprint_run_days:
                raise RoadmapPackError("Revenue sprint roadmap run days must be unique.")
            sprint_run_days.add(sprint_run_day)
            if authorization == AuthorizationLevel.EXTERNAL_ACTION.value:
                if "external_action" not in raw_item:
                    raise RoadmapPackError(
                        f"External-action roadmap item {item_id!r} must declare an exact external_action."
                    )
                clean_external_action = _prepare_revenue_sprint_item_action(
                    raw_item.get("external_action"),
                    field=f"{prefix}.external_action",
                    policy_revision=sprint_policy_revision,
                    allowed_pairs=sprint_allowed_action_pairs,
                )
            elif "external_action" in raw_item:
                raise RoadmapPackError(
                    f"Observe/propose roadmap item {item_id!r} cannot declare an external_action."
                )
        if raw_item.get("blockers") != []:
            raise RoadmapPackError(
                f"Roadmap pack item {item_id!r} must not begin with blockers."
            )
        if raw_item.get("previous_attempts") != [] or raw_item.get("previous_models") != []:
            raise RoadmapPackError(
                f"Roadmap pack item {item_id!r} must not contain prior execution history."
            )
        if raw_item.get("human_decision_required") is not False or str(
            raw_item.get("human_action") or ""
        ).strip():
            raise RoadmapPackError(
                f"Roadmap pack item {item_id!r} must not begin with an owner blocker."
            )
        dependencies = _pack_string_list(
            raw_item.get("dependencies"), f"{prefix}.dependencies", maximum_items=25, maximum_chars=120
        )
        criteria = _pack_string_list(
            raw_item.get("acceptance_criteria"),
            f"{prefix}.acceptance_criteria",
            require_values=True,
            maximum_items=12,
            maximum_chars=2000,
        )
        capabilities = _pack_string_list(
            raw_item.get("required_capabilities"),
            f"{prefix}.required_capabilities",
            require_values=True,
            maximum_items=20,
            maximum_chars=80,
        )
        for token_field, default in (
            ("estimated_input_tokens", None),
            ("estimated_cached_input_tokens", 0),
            ("estimated_output_tokens", None),
        ):
            raw_value = raw_item.get(token_field, default)
            if (
                isinstance(raw_value, bool)
                or not isinstance(raw_value, int)
                or raw_value < (0 if token_field == "estimated_cached_input_tokens" else 1)
                or raw_value > 250000
            ):
                raise RoadmapPackError(
                    f"Roadmap pack item {item_id!r} has an invalid {token_field}."
                )
        priority = raw_item.get("priority")
        if (
            isinstance(priority, bool)
            or not isinstance(priority, (int, float))
            or not math.isfinite(float(priority))
        ):
            raise RoadmapPackError(
                f"Roadmap pack item {item_id!r} has an invalid priority."
            )
        estimated_cost = raw_item.get("estimated_ai_cost_usd", 0.0)
        if (
            isinstance(estimated_cost, bool)
            or not isinstance(estimated_cost, (int, float))
            or not math.isfinite(float(estimated_cost))
            or float(estimated_cost) < 0
        ):
            raise RoadmapPackError(
                f"Roadmap pack item {item_id!r} has an invalid estimated_ai_cost_usd."
            )
        requires_evidence = raw_item.get("requires_recent_run_evidence", False)
        if not isinstance(requires_evidence, bool):
            raise RoadmapPackError(
                f"Roadmap pack item {item_id!r} has a non-boolean requires_recent_run_evidence."
            )
        complexity = str(raw_item.get("complexity") or "").strip().lower()
        if complexity not in {"lightweight", "standard", "advanced"}:
            raise RoadmapPackError(
                f"Roadmap pack item {item_id!r} has an unsupported complexity."
            )
        risk = str(raw_item.get("risk") or "").strip().lower()
        if risk not in {"low", "medium", "high", "critical"}:
            raise RoadmapPackError(
                f"Roadmap pack item {item_id!r} has an unsupported risk."
            )
        clean_item = {
            "id": item_id,
            "goal_id": item_goal_id,
            "title": _pack_text(raw_item.get("title"), f"{prefix}.title", maximum=240),
            "description": _pack_text(
                raw_item.get("description"), f"{prefix}.description", maximum=4000
            ),
            "priority": priority,
            "status": status,
            "dependencies": dependencies,
            "blockers": [],
            "acceptance_criteria": criteria,
            "agent_owner": _pack_text(
                raw_item.get("agent_owner"), f"{prefix}.agent_owner", maximum=80
            ),
            "task_type": _pack_text(
                raw_item.get("task_type"), f"{prefix}.task_type", maximum=80
            ),
            "complexity": complexity,
            "risk": risk,
            "required_capabilities": capabilities,
            "authorization_level": authorization,
            "estimated_input_tokens": int(raw_item["estimated_input_tokens"]),
            "estimated_cached_input_tokens": int(
                raw_item.get("estimated_cached_input_tokens", 0)
            ),
            "estimated_output_tokens": int(raw_item["estimated_output_tokens"]),
            "estimated_ai_cost_usd": _money(estimated_cost),
            "previous_attempts": [],
            "previous_models": [],
            "human_resolution_history": [],
            "human_decision_required": False,
            "human_action": "",
            "requires_recent_run_evidence": requires_evidence,
        }
        if revenue_sprint is not None:
            clean_item.update(
                {
                    "revenue_sprint_id": revenue_sprint["id"],
                    "revenue_sprint_run_day": sprint_run_day,
                    "action_policy_revision": sprint_policy_revision,
                }
            )
            if clean_external_action is not None:
                clean_item["external_action"] = clean_external_action
        clean_items.append(clean_item)
        item_ids.append(item_id)
        authorization_levels.add(authorization)

    if len(item_ids) != len(set(item_ids)):
        raise RoadmapPackError("Roadmap pack roadmap item IDs must be unique.")
    if revenue_sprint is not None and sprint_run_days != set(
        range(1, REVENUE_SPRINT_RUN_DAYS + 1)
    ):
        raise RoadmapPackError(
            f"Revenue sprint roadmap items must cover every run day from 1 through {REVENUE_SPRINT_RUN_DAYS}."
        )
    existing_item_counts: dict[str, int] = {}
    for candidate_project in state.get("projects", []) or []:
        if not isinstance(candidate_project, Mapping):
            continue
        for existing_item in candidate_project.get("roadmap_items", []) or []:
            if not isinstance(existing_item, Mapping):
                continue
            existing_id = str(existing_item.get("id") or "").strip()
            if existing_id:
                existing_item_counts[existing_id] = existing_item_counts.get(existing_id, 0) + 1

    receipts = [
        receipt
        for receipt in state.get("roadmap_pack_history", []) or []
        if isinstance(receipt, Mapping)
        and str(receipt.get("manifest_id") or "").strip() == manifest_id
    ]
    source_records_exist = any(
        str(goal.get("source_manifest_id") or "").strip() == manifest_id
        for candidate_project in state.get("projects", []) or []
        if isinstance(candidate_project, Mapping)
        for goal in candidate_project.get("goals", []) or []
        if isinstance(goal, Mapping)
    ) or any(
        str(existing_item.get("source_manifest_id") or "").strip() == manifest_id
        for candidate_project in state.get("projects", []) or []
        if isinstance(candidate_project, Mapping)
        for existing_item in candidate_project.get("roadmap_items", []) or []
        if isinstance(existing_item, Mapping)
    )
    if source_records_exist and not receipts:
        raise RoadmapPackError(
            f"Roadmap pack {manifest_id!r} has imported records but no persisted receipt; "
            "no state was changed."
        )
    preview = {
        "manifest_id": manifest_id,
        "manifest_revision": revision,
        "project_id": project_id,
        "project_name": str(project.get("name") or project_id),
        "goal_id": goal_id,
        "goal_title": goal_title,
        "item_count": len(clean_items),
        "roadmap_item_ids": item_ids,
        "authorization_levels": sorted(authorization_levels),
        "already_queued": False,
    }
    if revenue_sprint is not None:
        preview["revenue_sprint"] = deepcopy(revenue_sprint)
    if receipts:
        if len(receipts) != 1 or not _roadmap_pack_records_are_intact(
            state,
            receipts[0],
            manifest_id=manifest_id,
            revision=revision,
            project_id=project_id,
            goal_record=clean_goal_record,
            item_records=clean_items,
        ):
            raise RoadmapPackError(
                f"Roadmap pack {manifest_id!r} conflicts with its persisted receipt or "
                "queued records; no state was changed."
            )
        preview["already_queued"] = True
        return deepcopy(dict(state)), redact_secrets(preview), True

    if any(existing_item_counts.get(item_id, 0) for item_id in item_ids):
        collisions = [item_id for item_id in item_ids if existing_item_counts.get(item_id, 0)]
        raise RoadmapPackError(
            f"Roadmap item IDs already exist: {', '.join(collisions)}; no state was changed."
        )
    existing_goals = [
        goal
        for goal in project.get("goals", []) or []
        if isinstance(goal, Mapping)
        and str(goal.get("id") or "").strip() == goal_id
    ]
    if existing_goals:
        raise RoadmapPackError(
            f"Goal ID {goal_id!r} already exists in project {project_id!r}; no state was changed."
        )
    known_item_ids = set(existing_item_counts) | set(item_ids)
    for clean_item in clean_items:
        for dependency in clean_item["dependencies"]:
            if dependency == clean_item["id"]:
                raise RoadmapPackError(
                    f"Roadmap item {clean_item['id']!r} cannot depend on itself."
                )
            if dependency not in known_item_ids:
                raise RoadmapPackError(
                    f"Roadmap item {clean_item['id']!r} references missing dependency "
                    f"{dependency!r}."
                )
            if existing_item_counts.get(dependency, 0) > 1:
                raise RoadmapPackError(
                    f"Roadmap dependency {dependency!r} is ambiguous in persistent state."
                )
    dependency_graph: dict[str, list[str]] = {}
    for candidate_project in state.get("projects", []) or []:
        if not isinstance(candidate_project, Mapping):
            continue
        for existing_item in candidate_project.get("roadmap_items", []) or []:
            if not isinstance(existing_item, Mapping):
                continue
            existing_id = str(existing_item.get("id") or "").strip()
            if existing_id and existing_item_counts.get(existing_id) == 1:
                dependency_graph[existing_id] = _dependency_ids(existing_item)
    for clean_item in clean_items:
        dependency_graph[clean_item["id"]] = list(clean_item["dependencies"])
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str) -> None:
        if item_id in visiting:
            raise RoadmapPackError("Roadmap pack dependencies contain a cycle.")
        if item_id in visited:
            return
        visiting.add(item_id)
        for dependency in dependency_graph.get(item_id, []):
            if existing_item_counts.get(dependency, 0) > 1:
                raise RoadmapPackError(
                    f"Roadmap dependency {dependency!r} is ambiguous in persistent state."
                )
            if dependency in dependency_graph:
                visit(dependency)
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in item_ids:
        visit(item_id)

    candidate = deepcopy(dict(state))
    candidate_projects = [
        candidate_project
        for candidate_project in candidate.get("projects", []) or []
        if str(candidate_project.get("id") or "").strip() == project_id
    ]
    if len(candidate_projects) != 1:
        raise RoadmapPackError("Target project changed during roadmap-pack preparation.")
    candidate_project = candidate_projects[0]
    timestamp = _aware_utc(queued_at).isoformat()
    source = _redact_text(str(approval_source or "owner_confirmation")).strip()[:200]
    goal_hash_fields = (
        _ROADMAP_PACK_GOAL_HASH_FIELDS_V2
        if revenue_sprint is not None
        else _ROADMAP_PACK_GOAL_HASH_FIELDS
    )
    item_hash_fields = (
        _ROADMAP_PACK_ITEM_HASH_FIELDS_V2
        if revenue_sprint is not None
        else _ROADMAP_PACK_ITEM_HASH_FIELDS
    )
    goal_record_hash = _roadmap_pack_record_hash(
        _roadmap_pack_record_projection(
            clean_goal_record, goal_hash_fields
        )
    )
    item_record_hashes = {
        clean_item["id"]: _roadmap_pack_record_hash(
            _roadmap_pack_record_projection(
                clean_item, item_hash_fields
            )
        )
        for clean_item in clean_items
    }
    clean_goal = {
        **clean_goal_record,
        "created_at": timestamp,
        "updated_at": timestamp,
        "queued_at": timestamp,
        "approval_source": source,
        "source_manifest_id": manifest_id,
        "source_manifest_revision": revision,
        "source_manifest_record_hash": goal_record_hash,
    }
    for clean_item in clean_items:
        clean_item.update({
            "created_at": timestamp,
            "updated_at": timestamp,
            "queued_at": timestamp,
            "approval_source": source,
            "source_manifest_id": manifest_id,
            "source_manifest_revision": revision,
            "source_manifest_record_hash": item_record_hashes[clean_item["id"]],
        })
    candidate_project.setdefault("goals", []).append(clean_goal)
    candidate_project.setdefault("roadmap_items", []).extend(clean_items)
    receipt = {
        "manifest_id": manifest_id,
        "manifest_revision": revision,
        "project_id": project_id,
        "goal_id": goal_id,
        "roadmap_item_ids": item_ids,
        "goal_record_hash": goal_record_hash,
        "roadmap_item_hashes": item_record_hashes,
        "queued_at": timestamp,
        "approval_source": source,
    }
    if revenue_sprint is not None:
        receipt.update(
            {
                "record_hash_version": 2,
                "revenue_sprint": deepcopy(revenue_sprint),
                "revenue_sprint_hash": _roadmap_pack_record_hash(revenue_sprint),
            }
        )
    if backup_filename:
        receipt["backup_filename"] = str(backup_filename)
    candidate.setdefault("roadmap_pack_history", []).append(receipt)
    return _normalize_state(candidate), redact_secrets(preview), False


class AutonomyStateStore:
    """File-locked, atomic, versioned JSON state store."""

    def __init__(self, path: Path, seed_path: Path, lock_timeout_seconds: float = 5.0):
        self.path = Path(path)
        self.seed_path = Path(seed_path)
        self.recovery_path = self.path.with_name(f"{self.path.name}.recovery-required")
        self.lock = FileLock(str(self.path) + ".lock", timeout=lock_timeout_seconds)

    def _read_seed(self) -> dict[str, Any]:
        if self.seed_path.exists():
            with self.seed_path.open("r", encoding="utf-8") as stream:
                return _normalize_state(json.load(stream))
        return _default_state()

    def _quarantine(self) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        quarantine = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        # Write the durable stop marker before moving the bad primary file. If a
        # process crashes between these operations, the next run still refuses to
        # reseed and repeat previously completed or paid work.
        _atomic_write_json(
            self.recovery_path,
            {
                "status": "recovery_required",
                "detected_at": datetime.now(timezone.utc).isoformat(),
                "quarantine_path": str(quarantine),
            },
        )
        os.replace(self.path, quarantine)
        return quarantine

    def _pending_recovery(self) -> Optional[Path]:
        if not self.recovery_path.exists():
            return None
        try:
            with self.recovery_path.open("r", encoding="utf-8") as stream:
                marker = json.load(stream)
            value = marker.get("quarantine_path") if isinstance(marker, dict) else None
            return Path(str(value)) if value else self.recovery_path
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return self.recovery_path

    def _load_unlocked(self) -> dict[str, Any]:
        """Load normalized state while the caller holds ``self.lock``."""

        pending_recovery = self._pending_recovery()
        if pending_recovery is not None:
            raise CorruptAutonomyStateError(
                self.path, pending_recovery, self.recovery_path
            )
        if not self.path.exists():
            seeded = self._read_seed()
            _atomic_write_json(self.path, seeded)
            return deepcopy(seeded)
        try:
            with self.path.open("r", encoding="utf-8") as stream:
                return _normalize_state(json.load(stream))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, TypeError) as exc:
            quarantine = self._quarantine()
            raise CorruptAutonomyStateError(
                self.path, quarantine, self.recovery_path
            ) from exc

    def load(self) -> dict[str, Any]:
        with self.lock:
            return self._load_unlocked()

    def save(self, state: Mapping[str, Any]) -> None:
        normalized = _normalize_state(dict(state))
        with self.lock:
            pending_recovery = self._pending_recovery()
            if pending_recovery is not None:
                raise CorruptAutonomyStateError(
                    self.path, pending_recovery, self.recovery_path
                )
            _atomic_write_json(self.path, normalized)

    def _inspect_roadmap_pack(
        self,
        manifest: Mapping[str, Any],
        *,
        revision: str,
        inspected_at: datetime,
    ) -> dict[str, Any]:
        """Build a deterministic, read-only preview while holding the state lock."""

        with self.lock:
            state = self._load_unlocked()
            _, preview, _ = _prepare_roadmap_pack(
                state,
                manifest,
                revision,
                queued_at=inspected_at,
                approval_source="preview_only",
            )
            return preview

    def _queue_roadmap_pack(
        self,
        manifest: Mapping[str, Any],
        *,
        revision: str,
        expected_revision: str,
        queued_at: datetime,
        approval_source: str,
    ) -> tuple[dict[str, Any], bool, Optional[Path]]:
        """Atomically append a revalidated pack and preserve an on-volume backup."""

        if not expected_revision or revision != str(expected_revision):
            raise RoadmapPackError(
                "The roadmap pack changed after approval was staged; preview and approve it again."
            )
        with self.lock:
            state = self._load_unlocked()
            # First determine idempotency without creating an unnecessary backup.
            _, preview, already_queued = _prepare_roadmap_pack(
                state,
                manifest,
                revision,
                queued_at=queued_at,
                approval_source=approval_source,
            )
            if already_queued:
                return preview, True, None
            manifest_id = str(manifest.get("manifest_id") or "roadmap-pack")
            timestamp = _aware_utc(queued_at).strftime("%Y%m%dT%H%M%S%fZ")
            backup_path = self.path.with_name(
                f"{self.path.name}.before-{manifest_id}-{timestamp}-{uuid.uuid4().hex[:8]}.json"
            )
            candidate, preview, _ = _prepare_roadmap_pack(
                state,
                manifest,
                revision,
                queued_at=queued_at,
                approval_source=approval_source,
                backup_filename=backup_path.name,
            )
            _atomic_write_json(backup_path, state)
            _atomic_write_json(self.path, candidate)
            return preview, False, backup_path

    def _inspect_idea_promotion(
        self,
        idea_id: str,
        *,
        project_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Build a deterministic promotion preview without changing persistent state."""

        with self.lock:
            state = self._load_unlocked()
            idea, project, roadmap_item = _build_idea_promotion(
                state, idea_id, project_id
            )
            return redact_secrets({
                "idea_id": str(idea.get("id")),
                "idea": str(idea.get("idea") or ""),
                "problem_addressed": str(idea.get("problem_addressed") or ""),
                "expected_value": str(idea.get("expected_value") or ""),
                "project_id": str(project.get("id") or ""),
                "project_name": str(project.get("name") or project.get("id") or ""),
                "goal_id": roadmap_item.get("goal_id"),
                "roadmap_item_id": roadmap_item["id"],
                "title": roadmap_item["title"],
                "status": roadmap_item["status"],
                "authorization_level": roadmap_item["authorization_level"],
                "requires_recent_run_evidence": roadmap_item[
                    "requires_recent_run_evidence"
                ],
                "acceptance_criteria": deepcopy(roadmap_item["acceptance_criteria"]),
                "estimated_ai_cost_usd": roadmap_item["estimated_ai_cost_usd"],
                "proposal_revision": _idea_promotion_revision(idea),
            })

    def _promote_idea(
        self,
        idea_id: str,
        *,
        project_id: str,
        expected_revision: str,
        expected_roadmap_item_id: str,
        expected_goal_id: Optional[str],
        promoted_at: datetime,
        approval_source: str,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        """Atomically promote one revalidated idea and preserve its provenance."""

        with self.lock:
            state = self._load_unlocked()
            idea = _find_unique_idea(state, idea_id)
            status = str(idea.get("status") or "proposed").strip().lower()
            if status == "promoted":
                linked_id = str(idea.get("promoted_roadmap_item_id") or "").strip()
                linked = [
                    (project, item)
                    for project in state.get("projects", []) or []
                    if isinstance(project, dict)
                    for item in project.get("roadmap_items", []) or []
                    if isinstance(item, dict)
                    and str(item.get("id") or "").strip() == linked_id
                    and str(item.get("source_idea_id") or "").strip() == str(idea_id).strip()
                ]
                if len(linked) == 1:
                    project, item = linked[0]
                    linked_project_id = str(project.get("id") or "").strip()
                    linked_revision = str(item.get("proposal_revision") or "").strip()
                    if (
                        linked_id != str(expected_roadmap_item_id)
                        or linked_project_id != str(project_id)
                        or linked_revision != str(expected_revision)
                        or item.get("goal_id") != expected_goal_id
                    ):
                        raise IdeaPromotionError(
                            f"Idea {str(idea_id)!r} was already promoted through a "
                            "different reviewed destination; no duplicate was created."
                        )
                    return deepcopy(item), deepcopy(project), True
                raise IdeaPromotionError(
                    f"Idea {str(idea_id)!r} is marked promoted but its linked roadmap "
                    "item is missing or ambiguous; no state was changed."
                )
            idea, project, roadmap_item = _build_idea_promotion(
                state,
                idea_id,
                project_id,
                expected_revision=expected_revision,
                expected_roadmap_item_id=expected_roadmap_item_id,
                expected_goal_id=expected_goal_id,
            )
            timestamp = _aware_utc(promoted_at).isoformat()
            roadmap_item["created_at"] = timestamp
            roadmap_item["updated_at"] = timestamp
            roadmap_item["promoted_at"] = timestamp
            roadmap_item["approval_source"] = str(approval_source or "owner_confirmation")
            project.setdefault("roadmap_items", []).append(roadmap_item)
            idea["status"] = "promoted"
            idea["promoted_at"] = timestamp
            idea["promoted_project_id"] = str(project.get("id") or "")
            idea["promoted_goal_id"] = roadmap_item.get("goal_id")
            idea["promoted_roadmap_item_id"] = roadmap_item["id"]
            idea["promotion_approval_source"] = roadmap_item["approval_source"]
            _atomic_write_json(self.path, state)
            return deepcopy(roadmap_item), deepcopy(project), False

    def _reset_item_for_retry(self, item_id: str, *, reset_at: datetime) -> str:
        """Atomically reset one unambiguous owner-retryable item to ``ready``.

        The workflow-level caller also holds the autonomous run lock.  Checking the
        persisted run claim here closes the crash/restart gap without weakening the
        existing state-file locking boundary.
        """

        target_id = str(item_id or "").strip()
        if not target_id:
            raise RoadmapItemRetryError("A non-empty roadmap item ID is required for retry.")

        with self.lock:
            state = self._load_unlocked()
            active_run = state["run_control"].get("active_run")
            if active_run:
                run_id = str(active_run.get("run_id") or "unknown")
                raise RoadmapItemRetryError(
                    f"Roadmap item {target_id!r} was not reset because autonomous run "
                    f"{run_id!r} is active."
                )

            matches = []
            for project in state.get("projects", []):
                for candidate in project.get("roadmap_items", []):
                    if str(candidate.get("id") or "").strip() == target_id:
                        matches.append((project, candidate))

            if not matches:
                raise RoadmapItemRetryError(f"Roadmap item {target_id!r} was not found.")
            if len(matches) != 1:
                raise RoadmapItemRetryError(
                    f"Roadmap item ID {target_id!r} is ambiguous: {len(matches)} items match; "
                    "no state was changed."
                )

            project, roadmap_item = matches[0]
            previous_status = str(roadmap_item.get("status") or "planned").strip().lower()
            if previous_status not in RETRYABLE_ITEM_STATUSES:
                raise RoadmapItemRetryError(
                    f"Roadmap item {target_id!r} is {previous_status!r}; only 'needs_human', "
                    "'blocked', or 'deferred' items can be reset to 'ready'."
                )
            project_status = str(project.get("status") or "active").strip().lower()
            if project_status in RETRY_BLOCKED_PROJECT_STATUSES:
                raise RoadmapItemRetryError(
                    f"Roadmap item {target_id!r} was not reset because parent project "
                    f"{str(project.get('id') or project.get('name') or 'unknown')!r} is "
                    f"{project_status!r}."
                )
            if _has_unresolved_blockers(roadmap_item):
                raise RoadmapItemRetryError(
                    f"Roadmap item {target_id!r} still has unresolved blockers; resolve them "
                    "before retrying. No state was changed."
                )
            acceptance_criteria = [
                str(value).strip()
                for value in roadmap_item.get("acceptance_criteria", [])
                if str(value).strip()
            ]
            if not acceptance_criteria:
                raise RoadmapItemRetryError(
                    f"Roadmap item {target_id!r} has no acceptance criteria; add explicit "
                    "criteria before retrying. No state was changed."
                )

            reset_timestamp = _aware_utc(reset_at).isoformat()
            roadmap_item["human_resolution_history"].append({
                "action": "retry",
                "reset_at": reset_timestamp,
                "from_status": previous_status,
                "to_status": "ready",
            })
            roadmap_item["human_resolution_history"] = roadmap_item[
                "human_resolution_history"
            ][-HUMAN_RESOLUTION_HISTORY_LIMIT:]
            roadmap_item["status"] = "ready"
            roadmap_item["human_decision_required"] = False
            roadmap_item["human_action"] = ""
            roadmap_item.pop("human_decisions_required", None)
            for field in RETRY_RESET_FIELDS:
                roadmap_item.pop(field, None)
            roadmap_item["updated_at"] = reset_timestamp
            _atomic_write_json(self.path, state)

            return (
                f"Roadmap item {target_id!r} reset from {previous_status!r} to 'ready'; "
                "previous attempts were preserved. No model was invoked. "
                "Run /autorun dry-run to inspect selection before /autorun live."
            )


def _iter_project_items(state: Mapping[str, Any]):
    for project_index, project in enumerate(state.get("projects", [])):
        if not isinstance(project, dict):
            continue
        project_status = str(project.get("status", "active")).lower()
        if project_status in {"paused", "archived", "cancelled", "complete", "completed"}:
            continue
        for item_index, item in enumerate(project.get("roadmap_items", project.get("roadmap", []))):
            if isinstance(item, dict):
                yield project_index, item_index, project, item


def _dependency_ids(item: Mapping[str, Any]) -> list[str]:
    result = []
    for dependency in item.get("dependencies", []) or []:
        if isinstance(dependency, dict):
            dependency = dependency.get("id") or dependency.get("item_id")
        if dependency:
            result.append(str(dependency))
    return result


def _has_unresolved_blockers(item: Mapping[str, Any]) -> bool:
    blockers = item.get("blockers", []) or []
    if isinstance(blockers, str):
        return bool(blockers.strip())
    for blocker in blockers:
        if isinstance(blocker, dict):
            if blocker.get("resolved") is True or str(blocker.get("status", "")).lower() in {"resolved", "done", "closed"}:
                continue
            return True
        if str(blocker).strip():
            return True
    return False


def _needs_human_decision(item: Mapping[str, Any]) -> bool:
    required = item.get("human_decision_required", item.get("human_decisions_required", False))
    if isinstance(required, (list, tuple, dict, set)):
        return bool(required)
    if isinstance(required, str):
        return required.strip().lower() not in {"", "false", "no", "none", "resolved"}
    return bool(required)


def _priority_value(value: Any) -> float:
    labels = {"critical": 1000.0, "highest": 900.0, "high": 700.0, "medium": 400.0, "low": 100.0}
    if isinstance(value, str) and value.strip().lower() in labels:
        return labels[value.strip().lower()]
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _requires_recent_run_evidence(item: Mapping[str, Any]) -> bool:
    explicit = item.get("requires_recent_run_evidence")
    if isinstance(explicit, bool):
        return explicit
    if isinstance(explicit, str):
        explicit_text = explicit.strip().lower()
        if explicit_text in {"true", "yes", "1"}:
            return True
        if explicit_text in {"false", "no", "0"}:
            return False
    searchable = " ".join(
        [
            str(item.get("title") or ""),
            str(item.get("description") or ""),
            *[
                str(value)
                for value in (item.get("acceptance_criteria", []) or [])
                if value is not None
            ],
        ]
    ).lower()
    return bool(
        re.search(
            r"\b(?:last|most\s+recent|recent|prior|previous)\s+"
            r"(?:(?:one|two|three|four|five|six|seven|eight|nine|ten|\d+)\s+)?"
            r"(?:[a-z0-9]+(?:-[a-z0-9]+)?\s+){0,3}runs?\b",
            searchable,
        )
        or re.search(
            r"\b(?:autonomous\s+)?run\s+"
            r"(?:history|reports?|records?|results?|summaries|evidence)\b",
            searchable,
        )
    )


def _select_project_and_item(
    state: Mapping[str, Any],
    excluded_item_ids: Optional[set[str]] = None,
    eligible_item_ids: Optional[set[str]] = None,
) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
    excluded = {str(value) for value in (excluded_item_ids or set())}
    eligible = (
        None
        if eligible_item_ids is None
        else {str(value) for value in eligible_item_ids}
    )
    all_items = {
        str(item.get("id")): item
        for _, _, _, item in _iter_project_items(state)
        if item.get("id") is not None
    }
    candidates = []
    for project_index, item_index, project, item in _iter_project_items(state):
        item_id = str(item.get("id"))
        if item_id in excluded or (eligible is not None and item_id not in eligible):
            continue
        status = str(item.get("status", "planned")).strip().lower()
        if status not in ACTIONABLE_TASK_STATUSES:
            continue
        if _has_unresolved_blockers(item) or _needs_human_decision(item):
            continue
        dependencies = _dependency_ids(item)
        if any(
            dependency not in all_items
            or str(all_items[dependency].get("status", "")).strip().lower() not in TERMINAL_TASK_STATUSES
            for dependency in dependencies
        ):
            continue
        # max() makes a larger numeric priority more important.  Negative indexes
        # retain source-file order when two items have equal priority.
        rank = (
            _priority_value(item.get("priority", 0)),
            _priority_value(project.get("priority", 0)),
            -project_index,
            -item_index,
        )
        candidates.append((rank, project, item))
    if not candidates:
        return None
    _, project, item = max(candidates, key=lambda candidate: candidate[0])
    return project, item


def select_actionable_item(state: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Return the highest-priority actionable roadmap item, or ``None``."""

    selected = _select_project_and_item(state)
    return selected[1] if selected else None


select_actionable_roadmap_item = select_actionable_item


def classify_failure(error: Any) -> str:
    text = str(error or "").lower()
    if any(marker in text for marker in ("permission denied", "forbidden", "not authorized", "unauthorized")):
        return "permission_denied"
    if any(marker in text for marker in ("missing access", "credential", "api key", "token required", "sign in")):
        return "missing_access"
    if any(marker in text for marker in ("tool unavailable", "tool not found", "unsupported tool", "not installed")):
        return "unavailable_tool"
    if any(marker in text for marker in ("budget", "insufficient funds", "cost limit")):
        return "budget_exhausted"
    if any(marker in text for marker in ("need more information", "ambiguous", "decision required", "approval required")):
        return "decision_required"
    if any(marker in text for marker in ("timeout", "temporarily", "rate limit", "connection reset", "503")):
        return "transient"
    return "technical"


def format_escalation(
    project: Mapping[str, Any] | str,
    task: Mapping[str, Any] | str,
    attempted: str,
    reason: str,
    category: str,
    action_required: str,
    other_work_can_continue: bool = True,
) -> str:
    """Build one conversational, actionable Telegram escalation."""

    project_name = project if isinstance(project, str) else project.get("name") or project.get("title") or project.get("id")
    project_name = _chat_excerpt(project_name or "this project", 100)
    task_name = _task_chat_reference(task)
    attempted_text = _chat_sentence(_chat_excerpt(attempted or "No execution was started", 360))
    reason_text = _chat_sentence(
        _chat_excerpt(reason or str(category or "an unresolved issue").replace("_", " "), 220)
    )
    category_text = _chat_excerpt(str(category or "").replace("_", " "), 80)
    action_text = _chat_sentence(
        _chat_excerpt(action_required or "Review the task and provide direction", 700)
    )
    continuation = (
        "I stopped retries for this item, but unrelated work can continue."
        if other_work_can_continue
        else "I stopped here because the rest of this work is blocked too."
    )
    reason_section = (
        (f"This is a {category_text} issue. " if category_text else "")
        + f"We had to stop. {reason_text}"
    )
    return _compose_chat_sections(
        [f"Tyler, I need your help with {task_name} in {project_name}."],
        [f"We tried to move it forward. {attempted_text}"],
        [continuation],
        TELEGRAM_CHAT_RECAP_LIMIT,
        suffix=[reason_section, f"Please do this next:\n{action_text}"],
    )


def _money(value: Any) -> float:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return 0.0


def _summary_human_review_needed(report: Mapping[str, Any]) -> bool:
    """Return whether the run summary should direct the owner's attention."""

    final_status = str(report.get("final_status") or report.get("status") or "").lower()
    if final_status in {"blocked", "needs_human"}:
        return True
    return bool(report.get("human_actions") or report.get("escalations"))


def _chat_compact(value: Any) -> str:
    """Return secret-redacted text suitable for a compact chat sentence."""

    return re.sub(r"\s+", " ", _redact_text(str(value or "")).strip())


def _chat_excerpt(value: Any, limit: int) -> str:
    """Bound chat copy at a word boundary without changing the persisted value."""

    text = _chat_compact(value)
    if len(text) <= limit:
        return text
    marker = "..."
    cut = text[: max(0, limit - len(marker))].rstrip()
    boundary = cut.rfind(" ")
    if boundary >= max(0, len(cut) - 60):
        cut = cut[:boundary].rstrip()
    return cut + marker


def _bounded_chat_message(value: Any, limit: int) -> str:
    """Bound display copy while preserving intentional chat paragraphs."""

    text = _redact_text(str(value or "")).strip()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) <= limit:
        return text
    marker = "\n\n...more details are saved in the run record."
    cut = text[: max(0, limit - len(marker))].rstrip()
    boundary = max(cut.rfind("\n"), cut.rfind(" "))
    if boundary >= max(0, len(cut) - 80):
        cut = cut[:boundary].rstrip()
    return cut + marker


def _compose_chat_sections(
    priority: list[str],
    optional: list[str],
    footer: list[str],
    limit: int,
    *,
    suffix: Optional[list[str]] = None,
) -> str:
    """Fit optional details without ever dropping the priority text or footer."""

    chosen = list(priority)
    trailing = list(suffix or [])
    for section in optional:
        candidate = "\n\n".join(chosen + [section] + trailing + footer)
        if len(candidate) <= limit:
            chosen.append(section)
            continue
        note = "More task details are saved in the run record."
        if len("\n\n".join(chosen + [note] + trailing + footer)) <= limit:
            chosen.append(note)
        break
    return _bounded_chat_message("\n\n".join(chosen + trailing + footer), limit)


def _chat_sentence(value: Any) -> str:
    text = _chat_compact(value).strip()
    if text and text[-1] not in ".!?":
        text += "."
    return text


def _lower_sentence_start(value: str) -> str:
    if not value:
        return value
    if value.startswith("I ") or (len(value) > 1 and value[:2].isupper()):
        return value
    return value[:1].lower() + value[1:]


def _upper_sentence_start(value: str) -> str:
    """Capitalize ordinary prose without changing a leading path or identifier."""

    if not value or not value[:1].islower():
        return value
    first_token = value.split(maxsplit=1)[0]
    if any(marker in first_token for marker in ("/", "\\", "_", "=", ".")):
        return value
    return value[:1].upper() + value[1:]


def _task_chat_reference(task: Mapping[str, Any] | str) -> str:
    """Render a stable task reference without form-style field labels."""

    if isinstance(task, str):
        return f'"{_chat_excerpt(task, 120) or "the task"}"'
    task_id = _chat_excerpt(task.get("id"), 80)
    title = _chat_excerpt(task.get("title"), 120)
    if task_id and title and task_id.lower() not in title.lower():
        return f'"{task_id} - {title}"'
    return f'"{title or task_id or "the task"}"'


def _list_chat_items(values: list[Any], *, limit: int = 3) -> list[str]:
    rendered = [_chat_excerpt(value, 180) for value in values if _chat_compact(value)]
    visible = rendered[:limit]
    if len(rendered) > limit:
        visible.append(f"...and {len(rendered) - limit} more in the run record")
    return visible


def _chat_protocol_detail(value: Any, limit: int = 480) -> str:
    """Remove internal response markers without changing identifiers or commands."""

    text = _redact_text(str(value or "")).strip()
    text = re.sub(
        r"^\s*(?:FINAL\s+ANSWER\s*)?(?:APPROVED|REVISIONS\s+REQUIRED|BLOCKED\s*-\s*NEEDS\s+HUMAN\s+REVIEW)\s*(?:[-:\u2014]\s*)?",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"^\s*FINAL\s+ANSWER\s*[:\u2014-]?\s*", "", text, count=1, flags=re.IGNORECASE)
    text = re.sub(r"\bRESULT\s*:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\bFILES_CHANGED\s*:\s*(?:none|n/?a)\b[.;]?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"^\s*(MISSING_ACCESS|PERMISSION_DENIED|UNAVAILABLE_TOOL|DECISION_REQUIRED)\s*[:\u2014-]?\s*",
        lambda match: match.group(1).replace("_", " ").capitalize() + ". ",
        text,
        count=1,
        flags=re.IGNORECASE,
    )
    return _chat_excerpt(text, limit)


def _review_chat_detail(value: Any, limit: int = 420) -> str:
    """Remove reviewer protocol markers from the human-facing chat projection."""

    text = _chat_protocol_detail(value, limit)
    text = re.sub(r"^\s*(?:\d+[.)]|[-*])\s*", "", text, count=1)
    return _chat_excerpt(_upper_sentence_start(text), limit)


def _result_chat_takeaway(value: Any) -> str:
    """Show short natural outcomes, never a long report or internal instruction."""

    raw = _redact_text(str(value or "")).strip()
    compact = _chat_compact(raw)
    forbidden = (
        "AUTONOMY_HELP_REQUEST",
        "Acceptance criteria:",
        "Allowlisted runtime autonomy configuration",
        "OWNER ACTION NEEDED",
    )
    if (
        not compact
        or len(compact) > TELEGRAM_RESULT_PREVIEW_CHARS
        or raw.count("\n") > 3
        or any(marker.lower() in compact.lower() for marker in forbidden)
    ):
        return ""
    return _chat_protocol_detail(compact, TELEGRAM_RESULT_PREVIEW_CHARS)


def format_team_chat_message(kind: str, **fields: Any) -> str:
    """Project one real team transition into natural Telegram conversation.

    Model instructions stay internal; results, model reasons, and accounting remain in
    task/run state.
    This deterministic formatter never invokes a model.
    """

    message_kind = str(kind or "").strip().lower()
    recipient = _chat_excerpt(fields.get("recipient"), 60)
    task_ref = _task_chat_reference(fields.get("task") or "the task")
    model = _chat_excerpt(fields.get("model"), 80)
    failure = _chat_excerpt(fields.get("failure"), 80).replace("_", " ")
    detail = _chat_protocol_detail(fields.get("detail"), 480)
    review_detail = _review_chat_detail(fields.get("detail"), 480)

    if message_kind == "assignment":
        text = f"{recipient or 'Team'}, please take {task_ref}."
        if model:
            text += f"\n\nI'm using {model} for this pass."
        text += " Send Vera the evidence when it's ready."
    elif message_kind == "review_assignment":
        text = f"{recipient or 'Vera'}, please review the completed work against its acceptance criteria."
        if model:
            text += f"\n\nI'm using {model} for this review pass."
        text += " Let me know whether it is approved, needs one revision, or needs Tyler."
    elif message_kind == "retry":
        text = (
            f"{recipient or 'Team'}, the last pass hit {failure or 'a technical issue'}. "
            f"Please try {task_ref} once more"
        )
        if model:
            text += f" with {model}"
        text += ". This is still inside the bounded retry limit."
    elif message_kind == "help_request":
        question = _chat_excerpt(fields.get("question"), 300)
        reason = _chat_excerpt(fields.get("reason"), 220)
        text = f"{recipient or 'Team'}, can you help me with one part of {task_ref}? {question}"
        if reason:
            text += (
                f" {reason}"
                if reason.startswith("I ")
                else f" I need the check because {_lower_sentence_start(reason)}"
            )
    elif message_kind == "help_route":
        requester = _chat_excerpt(fields.get("requester"), 60)
        text = f"{recipient or 'Team'}, please take that focused check for {requester or 'the team'}."
        if model:
            text += f" I'm using {model} for the assist."
    elif message_kind == "help_response":
        text = f"{recipient or 'Team'}, I checked it."
        if detail:
            text += f" {detail}"
        if fields.get("detail_truncated"):
            text += " I saved the full response with the task."
    elif message_kind == "ready_for_review":
        takeaway = _result_chat_takeaway(fields.get("detail"))
        text = (
            f"{recipient or 'Vera'}, I finished {task_ref} and saved the full result. "
        )
        if takeaway:
            text += f"{takeaway} "
        text += "It's ready for your acceptance-criteria review."
    elif message_kind == "review_approved":
        text = f"I checked {task_ref} against the acceptance criteria. Approved."
        if review_detail:
            text += f" {_chat_sentence(review_detail)}"
        text += "\n\nMiles, you can mark it complete."
    elif message_kind == "review_revision":
        text = f"{recipient or 'Team'}, I need one change before I can approve {task_ref}."
        if review_detail:
            text += f" {_chat_sentence(review_detail)}"
        text += " Send it back when it's ready."
    elif message_kind == "review_blocked":
        text = f"Miles, I can't approve {task_ref} yet."
        if review_detail:
            text += f" {_chat_sentence(review_detail)}"
        text += " I've stopped the review loop so Tyler can resolve it."
    elif message_kind == "worker_blocked":
        text = f"Miles, I'm blocked on {task_ref}."
        if detail:
            text += f" {detail}"
        text += " I've stopped retrying inside the current limits."
    elif message_kind == "budget_stopped":
        text = (
            f"Miles, I stopped {task_ref} before another request could exceed its budget. "
            "It can resume after the budget resets, and unrelated work can continue."
        )
    else:
        raise ValueError(f"Unknown team chat message kind: {kind}")

    max_chars = _safe_int(
        fields.get("max_chars"),
        TELEGRAM_CHAT_TRANSITION_LIMIT,
        minimum=160,
        maximum=TELEGRAM_MESSAGE_LIMIT,
    )
    return _bounded_chat_message(text, max_chars)


def format_telegram_summary(report: Mapping[str, Any]) -> str:
    """Format one conversational Miles recap without another model call."""

    tasks = report.get("tasks_selected", []) or []
    completed = [task for task in tasks if str(task.get("status", "")).lower() in TERMINAL_TASK_STATUSES]
    changed = []
    changed_keys = set()
    for value in list(report.get("files_changed", []) or []) + list(report.get("artifacts", []) or []):
        display = _chat_compact(value)
        key = re.sub(r"^file:\s*", "", display, flags=re.IGNORECASE).lower()
        if not display or key in changed_keys:
            continue
        changed_keys.add(key)
        changed.append(display)
    idea_proposals = [
        idea for idea in (report.get("idea_proposals", []) or []) if isinstance(idea, Mapping)
    ]
    idea_titles = []
    for idea in idea_proposals:
        idea_id = str(idea.get("id") or "").strip()
        title = re.sub(r"\s+", " ", str(idea.get("idea") or "Untitled idea")).strip()
        if len(title) > 120:
            title = title[:117].rstrip() + "..."
        idea_titles.append(f"{idea_id}: {title}" if idea_id else title)
    if not idea_titles:
        idea_titles = [str(value) for value in (report.get("ideas_added", []) or [])]
    deferred = report.get("deferred", []) or []
    blockers = report.get("blockers", []) or []
    actions = report.get("human_actions", []) or []
    budget = report.get("budget", {}) or {}
    used = _money(budget.get("spent_after_usd", report.get("actual_cost_usd", 0.0)))
    limit = _money(budget.get("daily_budget_usd", 0.0))
    remaining = _money(budget.get("remaining_usd", max(0.0, limit - used)))
    estimate_label = " (estimated where exact usage was unavailable)" if report.get("cost_is_estimated") else ""
    final_status = str(report.get("final_status") or report.get("status") or "unknown").lower()
    human_review = _summary_human_review_needed(report)
    dry_run = bool(report.get("dry_run")) or final_status == "dry_run"

    if dry_run:
        if tasks:
            opening = f"Dry run complete. If this were live, I'd start with {_task_chat_reference(tasks[0])}."
        else:
            opening = "Dry run complete. There isn't a ready roadmap item to start."
        opening += " Nothing was executed or changed."
    elif final_status == "ideas_proposed":
        opening = (
            f"The roadmap is clear for now, so Lumen added {len(idea_titles)} "
            f"idea{'s' if len(idea_titles) != 1 else ''} to the backlog. Nothing was started automatically."
        )
    elif human_review:
        attention_count = max(
            len(list(actions)),
            len(list(report.get("escalations", []) or [])),
            1,
        )
        item_label = "one item" if attention_count == 1 else f"{attention_count} items"
        verb = "needs" if attention_count == 1 else "need"
        loop_note = (
            "I stopped its retry loop."
            if attention_count == 1
            else "I stopped their retry loops."
        )
        opening = f"I paused this session because {item_label} {verb} you. {loop_note}"
    elif final_status in {"budget_deferred", "deferred"}:
        opening = "I stopped this session before starting work that could not fit safely inside the current limits."
    elif completed:
        opening = (
            f"We're done for this session. The team completed {len(completed)} of "
            f"{len(tasks)} planned roadmap item{'s' if len(tasks) != 1 else ''}."
        )
    else:
        opening = "We're done for this session. There wasn't any ready roadmap work to start."

    trigger = str(report.get("trigger_source") or "").strip().lower()
    trigger_text = {
        "telegram": "You started this run from Telegram.",
        "scheduled": "This was the scheduled run.",
        "manual": "This was a manual run.",
        "session_continuation": "This continued the same autonomous session.",
    }.get(trigger, "")

    priority_sections = [opening]
    if trigger_text:
        priority_sections.append(trigger_text)
    if actions:
        action_values = [_chat_compact(value) for value in list(actions) if _chat_compact(value)]
        if len(action_values) == 1:
            action_text = _chat_excerpt(action_values[0], 700)
            priority_sections.append(f"Tyler, here's what I need from you next:\n{action_text}")
        else:
            visible_actions = [_chat_excerpt(value, 320) for value in action_values[:2]]
            if len(action_values) > 2:
                visible_actions.append(f"...and {len(action_values) - 2} more in the run record")
            priority_sections.append(
                "Tyler, here's what I need from you next:\n"
                + "\n".join(f"- {value}" for value in visible_actions)
            )
    optional_sections = []
    if completed:
        completed_lines = "\n".join(f"- {_task_chat_reference(task).strip(chr(34))}" for task in completed[:4])
        if len(completed) > 4:
            completed_lines += f"\n- ...and {len(completed) - 4} more in the run record"
        optional_sections.append(f"Completed:\n{completed_lines}")
    if changed:
        optional_sections.append("Changed:\n" + "\n".join(f"- {value}" for value in _list_chat_items(changed)))
    if idea_titles and final_status != "ideas_proposed":
        optional_sections.append("Ideas waiting in the backlog:\n" + "\n".join(f"- {value}" for value in _list_chat_items(idea_titles)))
    if deferred:
        optional_sections.append("Left for later:\n" + "\n".join(f"- {value}" for value in _list_chat_items(list(deferred))))
    if blockers:
        optional_sections.append("Still blocked:\n" + "\n".join(f"- {value}" for value in _list_chat_items(list(blockers))))
    stop_reason = str(report.get("stop_reason") or "").strip().lower()
    routine_stops = {
        "",
        "completed",
        "dry_run_complete",
        "ideas_proposed",
        "needs_human",
        "no_actionable_work",
    }
    if stop_reason not in routine_stops:
        stop_explanations = {
            "budget_floor": "the remaining ordinary budget could not cover another complete item",
            "budget_date_changed": "the budget day changed while the session was running",
            "max_tasks_reached": "the session reached its task limit",
            "max_session_time_reached": "the session reached its time limit",
            "overlap_prevented": "another run already held the execution lock",
        }
        optional_sections.append(
            "I stopped before another item because "
            + _chat_sentence(stop_explanations.get(stop_reason, stop_reason.replace("_", " ")))
        )
    footer_sections = [
        f"Budget today: ${used:.4f} used of ${limit:.2f}; ${remaining:.4f} available after reserve{estimate_label}."
    ]
    if not actions and human_review:
        footer_sections.append("Tyler, check the preceding blocker message for the exact action I need from you.")
    elif not human_review:
        footer_sections.append("Nothing needs your attention right now.")

    return _compose_chat_sections(
        priority_sections,
        optional_sections,
        footer_sections,
        TELEGRAM_CHAT_RECAP_LIMIT,
    )


def format_telegram_idea_plan(report: Mapping[str, Any]) -> str:
    """Render controlled Lumen proposals as a short teammate message."""

    proposals = [
        idea for idea in (report.get("idea_proposals", []) or []) if isinstance(idea, Mapping)
    ]
    if not proposals:
        return ""
    lines = [
        f"I found {len(proposals)} idea{'s' if len(proposals) != 1 else ''} worth considering. "
        "I only added them to the backlog; nothing was started.",
        "",
    ]
    for index, idea in enumerate(proposals, 1):
        idea_id = str(idea.get("id") or "unknown-id").strip()
        title = re.sub(r"\s+", " ", str(idea.get("idea") or "Untitled idea")).strip()
        if len(title) > 120:
            title = title[:117].rstrip() + "..."
        lines.append(f"{index}. {title} ({idea_id})")
    lines.extend([
        "",
        "If you want one moved onto the roadmap, send /autorun promote <idea-id>.",
    ])
    rendered = _redact_text("\n".join(lines))
    if len(rendered) <= TELEGRAM_MESSAGE_LIMIT:
        return rendered
    marker = "\n[idea plan truncated; see the persisted run report]"
    return rendered[: TELEGRAM_MESSAGE_LIMIT - len(marker)].rstrip() + marker


def format_telegram_deliverable(report: Mapping[str, Any]) -> str:
    """Format a concise completion update while retaining the full report on disk."""

    tasks = report.get("tasks_selected", []) or []
    completed = [task for task in tasks if str(task.get("status", "")).lower() in TERMINAL_TASK_STATUSES]
    result_text = _redact_text(str(report.get("result_text") or "")).strip()
    if not completed or not result_text:
        return format_telegram_idea_plan(report)
    task = completed[0]
    approved = any(
        str(value or "").strip().upper().startswith("APPROVED")
        for value in (report.get("review_outcomes", []) or [])
    ) or str(report.get("review_outcome") or "").strip().lower() == "approved"
    lines = [f"I finished {_task_chat_reference(task)}."]
    if approved:
        lines.append("Vera approved it against the acceptance criteria.")
    changed = list(report.get("files_changed", []) or [])
    if changed:
        visible = _list_chat_items(changed, limit=2)
        lines.append("I changed " + ", ".join(visible) + ".")
    takeaway = _result_chat_takeaway(result_text)
    if takeaway:
        lines.append(takeaway)
    storage_note = (
        " The saved result reached its configured storage limit."
        if report.get("result_truncated")
        else ""
    )
    lines.append(
        (
            "I saved the captured result in the run record instead of pasting the report into the chat."
            if report.get("result_truncated")
            else "I saved the full result in the run record instead of pasting the report into the chat."
        )
        + storage_note
    )
    return _bounded_chat_message("\n\n".join(lines), TELEGRAM_CHAT_TRANSITION_LIMIT)


format_daily_summary = format_telegram_summary


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_datetime(value: Any) -> Optional[datetime]:
    try:
        return _aware_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00")))
    except (TypeError, ValueError):
        return None


def _decision_field(decision: Any, name: str, default: Any = None) -> Any:
    if isinstance(decision, Mapping):
        return decision.get(name, default)
    return getattr(decision, name, default)


def _idea_fingerprint(idea: Mapping[str, Any]) -> str:
    source = str(idea.get("idea") or idea.get("title") or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", " ", source).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _find_item(state: Mapping[str, Any], item_id: str) -> Optional[dict[str, Any]]:
    for _, _, _, item in _iter_project_items(state):
        if str(item.get("id")) == str(item_id):
            return item
    return None


class AutonomousWorkflow:
    """Single-cycle primitive plus bounded sequential daily-session coordinator."""

    def __init__(
        self,
        config: Optional[AutonomyConfig] = None,
        *,
        state_path: Optional[Path] = None,
        seed_path: Optional[Path] = None,
        executor: Optional[Callable[[dict[str, Any], dict[str, Any], Any, str], Any]] = None,
        idea_generator: Optional[Callable[[dict[str, Any], int], Any]] = None,
        budget_provider: Optional[Callable[[], Mapping[str, Any]]] = None,
        router: Any = None,
        now_provider: Callable[[], datetime] = _utc_now,
    ):
        self.config = config or AutonomyConfig.from_env()
        self.base_dir = Path(state_path).parent if state_path is not None else Path(self.config.data_dir)
        self.state_path = Path(state_path) if state_path is not None else self.base_dir / "autonomy_state.json"
        self.seed_path = Path(seed_path) if seed_path is not None else Path(self.config.roadmap_seed_path)
        configured_pack_dir = Path(self.config.roadmap_pack_dir)
        self.roadmap_pack_dir = (
            configured_pack_dir
            if configured_pack_dir.is_absolute()
            else BASE_DIR / configured_pack_dir
        )
        self.report_dir = self.base_dir / "autonomous_runs"
        self.run_lock_path = self.base_dir / "autonomy_run.lock"
        self.store = AutonomyStateStore(self.state_path, self.seed_path)
        self.executor = executor
        self.idea_generator = idea_generator
        self.budget_provider = budget_provider
        self.router = router
        self.now_provider = now_provider

    def load_state(self) -> dict[str, Any]:
        return self.store.load()

    def _recent_run_evidence(
        self,
        state: Mapping[str, Any],
        project_id: str,
        limit: int = RECENT_RUN_EVIDENCE_LIMIT,
    ) -> list[dict[str, Any]]:
        """Return bounded global run facts plus project-scoped task details.

        Agents cannot read the control plane's state directory directly.  This
        snapshot gives evidence-based roadmap tasks the operational facts they need
        without exposing raw reports, arbitrary paths, secrets, private result text,
        or other projects' task text. Missing/corrupt reports fall back to the
        explicitly global thin recent-run index.
        """

        run_control = state.get("run_control", {}) or {}
        indexed_runs = run_control.get("recent_runs", []) or []
        if not isinstance(indexed_runs, list):
            return []
        bounded_limit = max(0, min(RECENT_RUN_EVIDENCE_LIMIT, int(limit or 0)))
        if bounded_limit == 0:
            return []

        active_run = run_control.get("active_run") or {}
        active_run_id = str(active_run.get("run_id") or "").strip()
        valid_indexed_runs: list[Mapping[str, Any]] = []
        for indexed in reversed(indexed_runs):
            if not isinstance(indexed, Mapping):
                continue
            run_id = str(indexed.get("run_id") or "").strip()
            if (
                run_id == active_run_id
                or not re.fullmatch(r"run_[A-Za-z0-9_-]{1,120}", run_id)
            ):
                continue
            valid_indexed_runs.append(indexed)
            if len(valid_indexed_runs) >= bounded_limit:
                break

        evidence: list[dict[str, Any]] = []
        for indexed in reversed(valid_indexed_runs):
            run_id = str(indexed.get("run_id") or "").strip()
            report: Mapping[str, Any] = {}
            report_available = False
            try:
                report_path = self.report_dir / f"{run_id}.json"
                if report_path.is_symlink():
                    raise OSError("Recent-run report symlinks are not accepted")
                report_root = self.report_dir.resolve()
                resolved_report = report_path.resolve(strict=True)
                resolved_report.relative_to(report_root)
                with resolved_report.open("rb") as stream:
                    raw_report = stream.read(RECENT_RUN_REPORT_MAX_BYTES + 1)
                if len(raw_report) > RECENT_RUN_REPORT_MAX_BYTES:
                    raise ValueError("Recent-run report exceeds the evidence read limit")
                loaded = json.loads(raw_report.decode("utf-8"))
                if (
                    isinstance(loaded, Mapping)
                    and str(loaded.get("run_id") or "").strip() == run_id
                    and bool(loaded.get("finish_time") or loaded.get("finished_at"))
                    and bool(loaded.get("final_status"))
                ):
                    report = loaded
                    report_available = True
            except (OSError, ValueError, TypeError, RecursionError, RuntimeError):
                report = {}

            source = report if report_available else indexed

            def bounded_text(value: Any, size: int = RECENT_RUN_EVIDENCE_TEXT_CHARS) -> str:
                return _redact_text(str(value or "")).strip()[:size]

            task_outcomes = []
            raw_tasks = source.get("tasks_selected", []) or []
            if isinstance(raw_tasks, list):
                for raw_task in raw_tasks:
                    if not isinstance(raw_task, Mapping):
                        continue
                    if str(raw_task.get("project_id") or "").strip() != project_id:
                        continue
                    task_outcomes.append({
                        "id": bounded_text(raw_task.get("id"), 100),
                        "title": bounded_text(raw_task.get("title")),
                        "status": bounded_text(raw_task.get("status"), 40),
                        "failure_classification": bounded_text(
                            raw_task.get("failure_classification"), 40
                        ),
                    })
                    if len(task_outcomes) >= 5:
                        break

            plans = [
                outcome["title"]
                for outcome in task_outcomes[:3]
                if outcome.get("title")
            ]

            trigger = bounded_text(
                source.get("trigger_source") or indexed.get("trigger_source"), 40
            )
            final_status = bounded_text(
                source.get("final_status") or indexed.get("final_status"), 40
            )
            human_actions = source.get("human_actions", []) or []
            escalations = source.get("escalations", []) or []
            blockers = source.get("blockers", []) or []
            deferred = source.get("deferred", []) or []
            files_changed = source.get("files_changed", []) or []
            project_needs_human = any(
                outcome.get("status") in {"blocked", "needs_human"}
                for outcome in task_outcomes
            )
            global_human_review_required = bool(
                human_actions
                or escalations
                or final_status in {"blocked", "needs_human"}
            )
            planned_label = plans[0] if plans else "none for this project"
            summary_line = bounded_text(
                f"global trigger={trigger or 'unknown'}; "
                f"global final={final_status or 'unknown'}; "
                f"global human_review={'yes' if global_human_review_required else 'no'}; "
                f"project planned={planned_label or 'none'}; "
                f"project human_review={'yes' if project_needs_human else 'no'}",
                320,
            )
            evidence.append({
                "run_id": run_id,
                "scope": "global_report" if report_available else "global_index",
                "global_started_at": bounded_text(
                    source.get("start_time")
                    or source.get("started_at")
                    or indexed.get("started_at"),
                    60,
                ),
                "global_finished_at": bounded_text(
                    source.get("finish_time")
                    or source.get("finished_at")
                    or indexed.get("finished_at"),
                    60,
                ),
                "global_trigger_source": trigger,
                "global_status": bounded_text(source.get("status"), 40),
                "global_final_status": final_status,
                "global_stop_reason": bounded_text(source.get("stop_reason"), 80),
                "project_plans": plans,
                "project_task_outcomes": task_outcomes,
                "project_task_count": len(task_outcomes),
                "project_human_review_required": project_needs_human,
                "global_deferred_count": len(deferred) if isinstance(deferred, list) else 0,
                "global_blocker_count": len(blockers) if isinstance(blockers, list) else 0,
                "global_human_action_count": (
                    len(human_actions) if isinstance(human_actions, list) else 0
                ),
                "global_files_changed_count": (
                    len(files_changed) if isinstance(files_changed, list) else 0
                ),
                "global_human_review_required": global_human_review_required,
                "report_available": report_available,
                "summary_line": summary_line,
            })
        return evidence

    def preview_roadmap_pack(
        self,
        manifest_id: str,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Preview one repository-owned roadmap pack without changing state."""

        run_lock = FileLock(
            str(self.run_lock_path), timeout=self.config.lock_timeout_seconds
        )
        try:
            with run_lock:
                manifest, revision = _load_roadmap_pack(
                    self.roadmap_pack_dir, manifest_id
                )
                preview = self.store._inspect_roadmap_pack(
                    manifest,
                    revision=revision,
                    inspected_at=self._now(),
                )
        except FileLockTimeout:
            return False, _redact_text(
                "The roadmap pack was not staged because another autonomous run holds "
                "the persistent run lock."
            )
        except RoadmapPackError as exc:
            return False, _redact_text(str(exc))
        return True, preview

    def queue_roadmap_pack(
        self,
        manifest_id: str,
        *,
        expected_revision: str,
        approval_source: str = "telegram_owner_confirmation",
    ) -> tuple[bool, str]:
        """Atomically append one owner-approved pack without invoking a model."""

        run_lock = FileLock(
            str(self.run_lock_path), timeout=self.config.lock_timeout_seconds
        )
        try:
            with run_lock:
                # Reload inside the run lock so confirmation is bound to the reviewed
                # file revision and cannot race an autonomous task claim.
                manifest, revision = _load_roadmap_pack(
                    self.roadmap_pack_dir, manifest_id
                )
                preview, already_queued, backup_path = self.store._queue_roadmap_pack(
                    manifest,
                    revision=revision,
                    expected_revision=expected_revision,
                    queued_at=self._now(),
                    approval_source=approval_source,
                )
        except FileLockTimeout:
            return False, _redact_text(
                "The roadmap pack was not queued because another autonomous run holds "
                "the persistent run lock. The approval can be retried after it finishes."
            )
        except RoadmapPackError as exc:
            return False, _redact_text(str(exc))
        if already_queued:
            return True, _redact_text(
                f"Roadmap pack {preview['manifest_id']!r} was already queued with all "
                f"{preview['item_count']} records intact; no duplicate was created."
            )
        backup_note = f" Backup: {backup_path.name}." if backup_path else ""
        return True, _redact_text(
            f"Queued roadmap pack {preview['manifest_id']!r}: goal "
            f"{preview['goal_id']!r} and {preview['item_count']} ready items in project "
            f"{preview['project_id']!r}.{backup_note} No model was invoked and no "
            "autonomous run was started. Run /autorun dry-run to inspect selection "
            "before /autorun live."
        )

    def preview_idea_promotion(
        self,
        idea_id: str,
        project_id: Optional[str] = None,
    ) -> tuple[bool, dict[str, Any] | str]:
        """Inspect one owner-gated idea promotion without writing or invoking a model."""

        run_lock = FileLock(
            str(self.run_lock_path), timeout=self.config.lock_timeout_seconds
        )
        try:
            with run_lock:
                preview = self.store._inspect_idea_promotion(
                    idea_id, project_id=project_id
                )
        except FileLockTimeout:
            return False, _redact_text(
                "Idea promotion was not staged because another autonomous run holds "
                "the persistent run lock."
            )
        except IdeaPromotionError as exc:
            return False, _redact_text(str(exc))
        return True, preview

    def promote_idea(
        self,
        idea_id: str,
        *,
        project_id: str,
        expected_revision: str,
        expected_roadmap_item_id: str,
        expected_goal_id: Optional[str],
        approval_source: str = "telegram_owner_confirmation",
    ) -> tuple[bool, str]:
        """Atomically queue one approved proposal without starting autonomous work."""

        run_lock = FileLock(
            str(self.run_lock_path), timeout=self.config.lock_timeout_seconds
        )
        try:
            with run_lock:
                roadmap_item, project, already_promoted = self.store._promote_idea(
                    idea_id,
                    project_id=project_id,
                    expected_revision=expected_revision,
                    expected_roadmap_item_id=expected_roadmap_item_id,
                    expected_goal_id=expected_goal_id,
                    promoted_at=self._now(),
                    approval_source=approval_source,
                )
        except FileLockTimeout:
            return False, _redact_text(
                "Idea promotion did not run because another autonomous run holds the "
                "persistent run lock. The approval can be retried after that run finishes."
            )
        except IdeaPromotionError as exc:
            return False, _redact_text(str(exc))
        project_id_text = str(project.get("id") or project.get("name") or "unknown")
        item_id = str(roadmap_item.get("id") or "unknown")
        if already_promoted:
            return True, _redact_text(
                f"Idea {str(idea_id)!r} was already promoted to roadmap item {item_id!r} "
                f"in project {project_id_text!r}; no duplicate was created."
            )
        return True, _redact_text(
            f"Promoted idea {str(idea_id)!r} to ready roadmap item {item_id!r} in "
            f"project {project_id_text!r}. No model was invoked and no autonomous run "
            "was started. Run /autorun dry-run to inspect selection before /autorun live."
        )

    def retry_item(self, item_id: str) -> tuple[bool, str]:
        """Safely make one owner-retryable item eligible for a future run.

        Expected owner-correctable rejections return ``(False, message)`` so a
        Telegram handler can display the message directly. Persistent-state I/O or
        corruption errors still raise; callers must not mistake those for a normal
        validation rejection.
        """

        run_lock = FileLock(
            str(self.run_lock_path), timeout=self.config.lock_timeout_seconds
        )
        try:
            with run_lock:
                message = self.store._reset_item_for_retry(item_id, reset_at=self._now())
        except FileLockTimeout:
            message = (
                f"Roadmap item {str(item_id or '').strip()!r} was not reset because another "
                "autonomous run holds the persistent run lock."
            )
            return False, _redact_text(message)
        except RoadmapItemRetryError as exc:
            return False, _redact_text(str(exc))
        return True, _redact_text(message)

    def select_actionable_item(self, state: Optional[Mapping[str, Any]] = None) -> Optional[dict[str, Any]]:
        return select_actionable_item(state or self.store.load())

    def _now(self) -> datetime:
        return _aware_utc(self.now_provider())

    def _local_date(self, now: datetime) -> str:
        return now.astimezone(ZoneInfo(self.config.timezone)).date().isoformat()

    def _new_report(self, trigger_source: str, dry_run: bool, scheduled_date: Optional[str]) -> dict[str, Any]:
        now = self._now()
        run_id = f"run_{now.strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
        report_path = self.report_dir / f"{run_id}.json"
        return {
            "schema_version": 1,
            "run_id": run_id,
            "start_time": now.isoformat(),
            "started_at": now.isoformat(),
            "finish_time": None,
            "finished_at": None,
            "trigger_source": trigger_source,
            "scheduled_date": scheduled_date,
            "dry_run": dry_run,
            "status": "running",
            "final_status": "running",
            "daily_plan": [],
            "tasks_selected": [],
            "agents_involved": [],
            "models_selected": [],
            "model_selection_reasons": [],
            "collaborations": [],
            "token_usage": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "estimated_cost_usd": 0.0,
            "actual_cost_usd": 0.0,
            "cost_is_estimated": False,
            "costs": {"by_project": {}, "by_task": {}, "by_agent": {}, "by_model": {}},
            "budget": {
                "budget_date": self._local_date(now),
                "budget_timezone": self.config.timezone,
                "daily_budget_usd": self.config.daily_budget_usd,
                "emergency_reserve_usd": self.config.emergency_reserve_usd,
                "spent_before_usd": 0.0,
                "spent_after_usd": 0.0,
                "reserved_usd": 0.0,
                "remaining_usd": max(0.0, self.config.daily_budget_usd - self.config.emergency_reserve_usd),
            },
            "review_outcomes": [],
            "result_text": "",
            "result_task_id": None,
            "result_agent": None,
            "result_truncated": False,
            "retry_count": 0,
            "blockers": [],
            "deferred": [],
            "escalations": [],
            "human_actions": [],
            "files_changed": [],
            "tests_executed": [],
            "artifacts": [],
            "ideas_added": [],
            "idea_proposals": [],
            "stale_recoveries": [],
            "errors": [],
            "stop_reason": "",
            "report_path": str(report_path),
        }

    def _finish_report(self, report: dict[str, Any], status: str, final_status: Optional[str] = None) -> dict[str, Any]:
        finished = self._now().isoformat()
        report["finish_time"] = finished
        report["finished_at"] = finished
        report["status"] = status
        report["final_status"] = final_status or status
        report["telegram_summary"] = format_telegram_summary(report)
        return report

    def _persist_report(self, report: Mapping[str, Any]) -> None:
        _atomic_write_json(Path(str(report["report_path"])), report)

    def _persist_and_return(self, report: dict[str, Any], status: str, final_status: Optional[str] = None) -> dict[str, Any]:
        self._finish_report(report, status, final_status)
        self._persist_report(report)
        return redact_secrets(report)

    def _reset_budget_day(self, state: dict[str, Any], local_date: str) -> float:
        tracking = state["budget_tracking"]
        if tracking.get("date") != local_date:
            tracking.update(
                date=local_date,
                actual_or_reconciled_cost_usd=0.0,
                cost_is_estimated=True,
            )
        return _money(tracking.get("actual_or_reconciled_cost_usd", 0.0))

    def _recover_stale_run(self, state: dict[str, Any], report: dict[str, Any], now: datetime) -> bool:
        active = state["run_control"].get("active_run")
        if not active:
            return True
        started = _parse_datetime(active.get("started_at"))
        stale = started is None or now - started >= timedelta(minutes=self.config.stale_run_minutes)
        if not stale:
            report["blockers"].append(f"Run {active.get('run_id', 'unknown')} is already active.")
            return False
        recovery = {
            "run_id": active.get("run_id"),
            "recovered_at": now.isoformat(),
            "reason": "stale_running_recovery",
        }
        item_id = active.get("item_id")
        if item_id:
            item = _find_item(state, str(item_id))
            if item and str(item.get("status", "")).lower() == "in_progress":
                item["status"] = "ready"
                item.setdefault("previous_attempts", []).append(
                    {
                        "run_id": active.get("run_id"),
                        "status": "abandoned",
                        "failure_classification": "stale_running_recovery",
                        "finished_at": now.isoformat(),
                    }
                )
        state["run_control"].setdefault("stale_recoveries", []).append(recovery)
        state["run_control"]["stale_recoveries"] = state["run_control"]["stale_recoveries"][-50:]
        state["run_control"]["active_run"] = None
        report["stale_recoveries"].append(recovery)
        return True

    def _available_model_budget(self, spent_before: float) -> float:
        return max(0.0, self.config.daily_budget_usd - self.config.emergency_reserve_usd - spent_before)

    def _apply_budget_snapshot(self, state: dict[str, Any], report: dict[str, Any], *, before: bool) -> float:
        """Read the shared Company Mode ledger when an integration supplies it.

        The workflow's own budget field remains a restart-safe audit fallback.  In the
        Telegram runtime, the provider is authoritative so reservations held by other
        work are included in the route decision and the end report.
        """

        if self.budget_provider is None:
            spent = _money(state["budget_tracking"].get("actual_or_reconciled_cost_usd", 0.0))
            remaining = self._available_model_budget(spent)
            if before:
                report["budget"]["spent_before_usd"] = spent
            report["budget"]["spent_after_usd"] = spent
            report["budget"]["remaining_usd"] = remaining
            return remaining

        snapshot = self.budget_provider()
        if not isinstance(snapshot, Mapping):
            raise RuntimeError("The configured budget provider returned no structured snapshot")
        daily = _money(snapshot.get("daily_budget_usd", self.config.daily_budget_usd))
        emergency = _money(snapshot.get("emergency_reserve_usd", self.config.emergency_reserve_usd))
        spent = _money(snapshot.get("spent_today_usd", snapshot.get("spent_usd", 0.0)))
        reserved = _money(snapshot.get("reserved_today_usd", snapshot.get("reserved_usd", 0.0)))
        remaining = _money(snapshot.get(
            "remaining_usd",
            max(0.0, daily - emergency - spent - reserved),
        ))
        report["budget"].update(
            budget_date=snapshot.get("budget_date", report["budget"].get("budget_date")),
            budget_timezone=snapshot.get("budget_timezone", report["budget"].get("budget_timezone")),
            daily_budget_usd=daily,
            emergency_reserve_usd=emergency,
            spent_after_usd=spent,
            reserved_usd=reserved,
            remaining_usd=remaining,
        )
        if before:
            report["budget"]["spent_before_usd"] = spent
        state["budget_tracking"]["actual_or_reconciled_cost_usd"] = spent
        state["budget_tracking"]["cost_is_estimated"] = bool(snapshot.get("cost_is_estimated", False))
        return remaining

    def _routing_request(self, item: Mapping[str, Any], remaining_budget: float) -> Any:
        values = {
            "task_type": str(item.get("task_type", "general")),
            "complexity": str(item.get("complexity", "standard")),
            "risk": str(item.get("risk", "low")),
            "required_capabilities": list(item.get("required_capabilities", []) or []),
            "estimated_input_tokens": _safe_int(item.get("estimated_input_tokens"), 2000),
            "estimated_cached_input_tokens": _safe_int(item.get("estimated_cached_input_tokens"), 0),
            "estimated_output_tokens": _safe_int(item.get("estimated_output_tokens"), 600),
            "remaining_budget_usd": remaining_budget,
            "previous_failures": sum(
                1
                for attempt in item.get("previous_attempts", [])
                if isinstance(attempt, dict)
                and attempt.get("model_invoked", True)
                and str(attempt.get("failure_classification") or "").lower()
                in {"technical", "transient", "no_progress", "model_quality"}
            ),
            "previous_models": list(item.get("previous_models", []) or []),
        }
        try:
            import model_router  # Local import keeps offline state tests independent.

            request_type = getattr(model_router, "RoutingRequest", None)
            return request_type(**values) if request_type else values
        except (ImportError, TypeError, ValueError):
            return values

    def _route(self, item: Mapping[str, Any], remaining_budget: float) -> Any:
        request = self._routing_request(item, remaining_budget)
        if self.router is not None:
            if hasattr(self.router, "route"):
                return self.router.route(request)
            return self.router(request)
        try:
            import model_router

            router_type = getattr(model_router, "ModelRouter", None)
            if router_type is not None:
                loader = getattr(model_router, "load_catalog", None) or getattr(model_router, "load_model_catalog", None)
                catalog = loader() if loader else None
                router = router_type(catalog) if catalog is not None else router_type()
                return router.route(request)
            route_task = getattr(model_router, "route_task")
            parameters = inspect.signature(route_task).parameters
            if "request" in parameters:
                return route_task(request=request)
            return route_task(request)
        except Exception as exc:  # Routing must fail closed without breaking dry-run inspection.
            estimate = _money(item.get("estimated_ai_cost_usd", 0.05))
            return {
                "model_id": None,
                "level": str(item.get("complexity", "standard")),
                "estimated_cost_usd": estimate,
                "reason": f"Model catalog unavailable; executor must use its configured safe default ({type(exc).__name__}).",
                "deferred": estimate > remaining_budget,
                "deferral_reason": "Estimated task cost exceeds ordinary remaining budget." if estimate > remaining_budget else "",
            }

    def _record_route(self, report: dict[str, Any], decision: Any) -> None:
        model_id = _decision_field(decision, "model_id") or _decision_field(decision, "model")
        reason = str(_decision_field(decision, "reason", "No routing reason supplied."))
        estimate = _money(_decision_field(decision, "estimated_cost_usd", 0.0))
        report["estimated_cost_usd"] = estimate
        if model_id:
            report["models_selected"].append(str(model_id))
        report["model_selection_reasons"].append(reason)

    def _add_ideas(self, state: dict[str, Any], candidates: Any, report: dict[str, Any]) -> None:
        if isinstance(candidates, Mapping):
            candidates = [candidates]
        if not isinstance(candidates, (list, tuple)):
            return
        backlog = state.setdefault("idea_backlog", [])
        available_slots = max(0, self.config.idea_backlog_limit - len(backlog))
        addition_limit = min(self.config.max_ideas_per_run, available_slots)
        fingerprints = {
            str(idea.get("fingerprint") or _idea_fingerprint(idea))
            for idea in backlog
            if isinstance(idea, dict)
        }
        used_ids = {
            str(idea.get("id") or "").strip()
            for idea in backlog
            if isinstance(idea, dict) and str(idea.get("id") or "").strip()
        }
        added = 0
        for candidate in candidates:
            if added >= addition_limit or not isinstance(candidate, Mapping):
                break
            idea_text = str(candidate.get("idea") or candidate.get("title") or "").strip()
            if not idea_text:
                continue
            raw_risks = candidate.get("risks", []) or []
            if isinstance(raw_risks, str):
                raw_risks = [raw_risks]
            idea_id = str(candidate.get("id") or "").strip()
            if not idea_id or idea_id in used_ids:
                idea_id = f"idea_{uuid.uuid4().hex[:10]}"
                while idea_id in used_ids:
                    idea_id = f"idea_{uuid.uuid4().hex[:10]}"
            idea = {
                "id": idea_id,
                "idea": idea_text,
                "problem_addressed": str(candidate.get("problem_addressed", "")),
                "expected_value": str(candidate.get("expected_value", "")),
                "target_user": str(candidate.get("target_user", "")),
                "estimated_effort": str(candidate.get("estimated_effort", "unknown")),
                "estimated_ai_cost_usd": _money(candidate.get("estimated_ai_cost_usd", 0.0)),
                "risks": list(raw_risks),
                "relationship_to_current_goals": str(candidate.get("relationship_to_current_goals", "")),
                "recommended_next_validation_step": str(candidate.get("recommended_next_validation_step", "")),
                "status": "proposed",
                "authorization_level": AuthorizationLevel.PROPOSE.value,
                "created_at": self._now().isoformat(),
                "source_run_id": report["run_id"],
            }
            target_project_id = str(candidate.get("target_project_id") or "").strip()
            target_goal_id = str(candidate.get("target_goal_id") or "").strip()
            if target_project_id:
                idea["target_project_id"] = target_project_id
            if target_goal_id:
                idea["target_goal_id"] = target_goal_id
            fingerprint = _idea_fingerprint(idea)
            if fingerprint in fingerprints:
                continue
            idea["fingerprint"] = fingerprint
            safe_idea = redact_secrets(idea)
            backlog.append(safe_idea)
            fingerprints.add(fingerprint)
            used_ids.add(idea_id)
            report["ideas_added"].append(idea["id"])
            report.setdefault("idea_proposals", []).append(deepcopy(safe_idea))
            added += 1
        if len(backlog) > self.config.idea_backlog_limit:
            # Keep the oldest accepted ideas stable; a full backlog requires owner
            # triage rather than silently evicting prior proposals.
            del backlog[self.config.idea_backlog_limit :]

    def _record_idea_generation(
        self,
        state: dict[str, Any],
        report: dict[str, Any],
        metadata: Mapping[str, Any],
        spent_before: float,
    ) -> Optional[str]:
        """Attach a metered creative callback to the same run-level audit trail.

        Integrations may return ``{"ideas": [...], ...metering fields...}`` while
        simple/offline generators may continue returning a plain list.  A string
        result classifies a deferral so non-budget control-plane conditions are not
        mislabeled as exhausted spend.
        """

        model_id = str(metadata.get("model") or metadata.get("model_id") or "").strip()
        model_reason = str(metadata.get("model_reason") or metadata.get("reason") or "").strip()
        estimate = _money(metadata.get("estimated_cost_usd", 0.0))
        report["estimated_cost_usd"] = estimate
        if model_id and model_id not in report["models_selected"]:
            report["models_selected"].append(model_id)
        if model_reason:
            report["model_selection_reasons"].append(model_reason)

        deferred = bool(metadata.get("deferred", False))
        if deferred:
            reason = str(metadata.get("deferral_reason") or "Creative work was deferred by the model router.")
            report["deferred"].append(reason)
            report["creative_deferral_reason"] = reason
            normalized = reason.lower()
            if any(marker in normalized for marker in ("budget", "reserve", "remaining", "cost")):
                return "budget"
            return "non_budget"

        actual_supplied = metadata.get("actual_cost_usd") is not None
        actual = _money(metadata.get("actual_cost_usd") if actual_supplied else estimate)
        report["actual_cost_usd"] = actual
        report["cost_is_estimated"] = not actual_supplied or bool(metadata.get("cost_is_estimated", False))
        report["token_usage"] = self._result_usage(metadata)

        agents = [str(value) for value in (metadata.get("agents") or [metadata.get("agent") or "creative"]) if value]
        models = [str(value) for value in (metadata.get("models") or ([model_id] if model_id else [])) if value]
        for agent in agents:
            if agent not in report["agents_involved"]:
                report["agents_involved"].append(agent)
        for model in models:
            if model not in report["models_selected"]:
                report["models_selected"].append(model)

        nested_costs = metadata.get("costs")
        if isinstance(nested_costs, Mapping):
            for dimension in ("by_project", "by_task", "by_agent", "by_model"):
                values = nested_costs.get(dimension, {})
                if isinstance(values, Mapping):
                    report["costs"][dimension] = {
                        str(key): _money(value) for key, value in values.items()
                    }
        else:
            project_id = str(metadata.get("project_id") or "idea_backlog")
            task_id = str(metadata.get("task_id") or "controlled-idle-ideation")
            agent_id = agents[0] if agents else "creative"
            model_key = models[0] if models else "unrecorded"
            for dimension, key in (
                ("by_project", project_id),
                ("by_task", task_id),
                ("by_agent", agent_id),
                ("by_model", model_key),
            ):
                report["costs"][dimension][key] = actual

        state["budget_tracking"]["actual_or_reconciled_cost_usd"] = _money(spent_before + actual)
        state["budget_tracking"]["cost_is_estimated"] = report["cost_is_estimated"]
        if self.budget_provider is None:
            report["budget"]["spent_after_usd"] = state["budget_tracking"]["actual_or_reconciled_cost_usd"]
            report["budget"]["remaining_usd"] = max(
                0.0,
                self.config.daily_budget_usd
                - self.config.emergency_reserve_usd
                - report["budget"]["spent_after_usd"],
            )
        else:
            try:
                self._apply_budget_snapshot(state, report, before=False)
            except Exception as exc:
                report["errors"].append(f"Shared budget refresh failed after idea generation: {exc}")
                report["budget"]["spent_after_usd"] = state["budget_tracking"]["actual_or_reconciled_cost_usd"]
        return None

    def _authorization_allowed(self, item: Mapping[str, Any]) -> bool:
        requested = _authorization(item.get("authorization_level"), AuthorizationLevel.PROPOSE)
        return _AUTHORIZATION_RANK[requested] <= _AUTHORIZATION_RANK[self.config.max_authorization]

    def _result_usage(self, result: Mapping[str, Any]) -> dict[str, int]:
        raw = result.get("token_usage") or result.get("usage") or {}
        input_tokens = _safe_int(raw.get("input_tokens", raw.get("prompt_tokens", 0)), 0)
        cached_tokens = _safe_int(raw.get("cached_input_tokens", raw.get("cached_tokens", 0)), 0)
        output_tokens = _safe_int(raw.get("output_tokens", raw.get("completion_tokens", 0)), 0)
        total_tokens = _safe_int(raw.get("total_tokens"), input_tokens + output_tokens)
        return {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        }

    def _execute(
        self,
        state: dict[str, Any],
        project: dict[str, Any],
        item: dict[str, Any],
        decision: Any,
        report: dict[str, Any],
        spent_before: float,
    ) -> None:
        item["status"] = "in_progress"
        item["updated_at"] = self._now().isoformat()
        state["run_control"]["active_run"]["item_id"] = item.get("id")
        self.store.save(state)
        attempt = {
            "run_id": report["run_id"],
            "started_at": self._now().isoformat(),
            "model": _decision_field(decision, "model_id") or _decision_field(decision, "model"),
            "estimated_cost_usd": report["estimated_cost_usd"],
        }
        execution_item = deepcopy(item)
        recent_run_evidence = []
        if _requires_recent_run_evidence(item):
            recent_run_evidence = self._recent_run_evidence(
                state,
                str(project.get("id") or "").strip(),
            )
        if recent_run_evidence:
            execution_item["recent_run_evidence"] = recent_run_evidence
        context_run_ids = [entry["run_id"] for entry in recent_run_evidence]
        attempt["context_run_ids"] = context_run_ids
        report["tasks_selected"][0]["context_run_ids"] = context_run_ids
        try:
            if self.executor is None:
                raise RuntimeError("No autonomous executor is configured")
            raw_result = self.executor(project, execution_item, decision, report["run_id"])
            if isinstance(raw_result, str):
                result = {"status": "completed", "result": raw_result}
            elif isinstance(raw_result, Mapping):
                result = dict(raw_result)
            else:
                raise RuntimeError("Autonomous executor returned no structured result")
        except Exception as exc:
            result = {"status": "failed", "error": str(exc), "failure_classification": classify_failure(exc)}

        status = str(result.get("status", "failed")).strip().lower()
        failure = str(result.get("failure_classification") or classify_failure(result.get("error") or result.get("reason")))
        usage = self._result_usage(result)
        report["token_usage"] = usage
        if result.get("estimated_cost_usd") is not None:
            report["estimated_cost_usd"] = _money(result.get("estimated_cost_usd"))
        actual_supplied = result.get("actual_cost_usd") is not None
        reconciled_cost = _money(result.get("actual_cost_usd") if actual_supplied else report["estimated_cost_usd"])
        report["actual_cost_usd"] = reconciled_cost
        report["cost_is_estimated"] = not actual_supplied or bool(result.get("cost_is_estimated", False))
        model_id = str(result.get("model") or attempt.get("model") or "unrecorded")
        project_id = str(project.get("id") or project.get("name") or "unknown")
        item_id = str(item.get("id") or "unknown")
        agent = str(item.get("agent_owner") or "manager")
        nested_costs = result.get("costs")
        if isinstance(nested_costs, Mapping):
            for dimension in ("by_project", "by_task", "by_agent", "by_model"):
                values = nested_costs.get(dimension, {})
                if isinstance(values, Mapping):
                    report["costs"][dimension] = {
                        str(key): _money(value) for key, value in values.items()
                    }
        else:
            for dimension, key in (("by_project", project_id), ("by_task", item_id), ("by_agent", agent), ("by_model", model_id)):
                report["costs"][dimension][key] = reconciled_cost
        for selected_model in result.get("models", []) or []:
            selected_model = str(selected_model)
            if selected_model and selected_model not in report["models_selected"]:
                report["models_selected"].append(selected_model)
        for involved_agent in result.get("agents", []) or []:
            involved_agent = str(involved_agent)
            if involved_agent and involved_agent not in report["agents_involved"]:
                report["agents_involved"].append(involved_agent)
        for routing_reason in result.get("model_selection_reasons", []) or []:
            routing_reason = str(routing_reason).strip()
            if routing_reason and routing_reason not in report["model_selection_reasons"]:
                report["model_selection_reasons"].append(routing_reason)
        report["collaborations"] = [
            dict(value)
            for value in (result.get("collaborations", []) or [])
            if isinstance(value, Mapping)
        ][:30]
        state["budget_tracking"]["actual_or_reconciled_cost_usd"] = _money(spent_before + reconciled_cost)
        state["budget_tracking"]["cost_is_estimated"] = report["cost_is_estimated"]
        if self.budget_provider is None:
            report["budget"]["spent_after_usd"] = state["budget_tracking"]["actual_or_reconciled_cost_usd"]
            report["budget"]["remaining_usd"] = max(
                0.0,
                self.config.daily_budget_usd
                - self.config.emergency_reserve_usd
                - report["budget"]["spent_after_usd"],
            )
        else:
            try:
                self._apply_budget_snapshot(state, report, before=False)
            except Exception as exc:
                # Execution already occurred, so retain the measured callback cost and
                # make the reconciliation gap visible instead of losing the run report.
                report["errors"].append(f"Shared budget refresh failed after execution: {exc}")
                report["budget"]["spent_after_usd"] = state["budget_tracking"]["actual_or_reconciled_cost_usd"]
                report["budget"]["remaining_usd"] = max(
                    0.0,
                    report["budget"]["daily_budget_usd"]
                    - report["budget"]["emergency_reserve_usd"]
                    - report["budget"].get("reserved_usd", 0.0)
                    - report["budget"]["spent_after_usd"],
                )
        report["review_outcomes"] = list(result.get("review_outcomes", []) or [])
        report["team_handoff_failed"] = bool(result.get("team_handoff_failed", False))
        if result.get("review_outcome"):
            report["review_outcomes"].append(result["review_outcome"])
        report["retry_count"] = _safe_int(result.get("retry_count", result.get("retries", 0)), 0)
        report["files_changed"] = list(result.get("files_changed", []) or [])
        report["tests_executed"] = list(result.get("tests_executed", []) or [])
        report["artifacts"] = list(result.get("artifacts", []) or [])
        raw_result_text = str(
            result.get("result_text") or result.get("result") or result.get("summary") or ""
        )
        result_limit = _safe_int(os.environ.get("MAX_TASK_RESULT_CHARS"), 5000, 1, 20000)
        report["result_text"] = _redact_text(raw_result_text).strip()[:result_limit]
        report["result_truncated"] = bool(result.get("result_truncated")) or (
            len(_redact_text(raw_result_text).strip()) > result_limit
        )
        if report["result_text"]:
            report["result_task_id"] = result.get("result_task_id") or item_id
            report["result_agent"] = result.get("result_agent") or agent
            report["tasks_selected"][0]["result_summary"] = report["result_text"][:1000]
            report["tasks_selected"][0]["result_task_id"] = report["result_task_id"]
            report["tasks_selected"][0]["result_agent"] = report["result_agent"]
            report["tasks_selected"][0]["result_truncated"] = report["result_truncated"]
        attempt.update(
            finished_at=self._now().isoformat(),
            status=status,
            model_invoked=bool(result.get("model_invoked", status != "deferred")),
            actual_or_reconciled_cost_usd=reconciled_cost,
            cost_is_estimated=report["cost_is_estimated"],
            token_usage=usage,
            result_summary=report["result_text"][:1000],
        )
        if status in {"completed", "complete", "done", "approved", "shipped", "success"}:
            item["status"] = "completed"
            report["tasks_selected"][0]["status"] = "completed"
        elif status == "deferred":
            item["status"] = "deferred"
            message = str(result.get("reason") or "Execution deferred.")
            report["deferred"].append(message)
            report["tasks_selected"][0]["status"] = "deferred"
            report["tasks_selected"][0]["failure_classification"] = failure
            attempt["failure_classification"] = failure
            action = str(result.get("human_action") or "").strip()
            if action:
                if action not in report["human_actions"]:
                    report["human_actions"].append(action)
                report["escalations"].append(
                    format_escalation(
                        project,
                        item,
                        str(
                            result.get("attempted")
                            or "Checked execution preconditions; no task execution started."
                        ),
                        message,
                        failure,
                        action,
                        bool(result.get("other_work_can_continue", True)),
                    )
                )
        else:
            attempt["failure_classification"] = failure
            prior_attempts = len(item.get("previous_attempts", [])) + 1
            terminal_failure = failure in {
                "permission_denied",
                "missing_access",
                "missing_information",
                "unavailable_tool",
                "budget_exhausted",
                "decision_required",
                "no_progress",
            } or prior_attempts >= self.config.max_execution_attempts or status in {"blocked", "needs_human"}
            item["status"] = "needs_human" if terminal_failure else "ready"
            report["tasks_selected"][0]["status"] = item["status"]
            report["tasks_selected"][0]["failure_classification"] = failure
            reason = str(result.get("error") or result.get("reason") or "Execution did not complete.")
            report["blockers"].append(reason)
            if terminal_failure:
                action = str(result.get("human_action") or "Review the failure, provide missing access or direction, then mark the task resolved.")
                item["human_decision_required"] = True
                item["human_action"] = action
                escalation = format_escalation(
                    project,
                    item,
                    str(result.get("attempted") or "The assigned agent attempted the roadmap task."),
                    reason,
                    failure,
                    action,
                    True,
                )
                report["escalations"].append(escalation)
                report["human_actions"].append(action)
        item.setdefault("previous_attempts", []).append(redact_secrets(attempt))
        if (
            attempt.get("model_invoked")
            and attempt.get("model")
            and attempt["model"] not in item.setdefault("previous_models", [])
        ):
            item["previous_models"].append(attempt["model"])
        item["updated_at"] = self._now().isoformat()

    def _close_run_claim(self, state: dict[str, Any], report: dict[str, Any]) -> None:
        state["run_control"]["active_run"] = None
        scheduled_date = report.get("scheduled_date")
        if scheduled_date and scheduled_date in state["run_control"].get("scheduled_dates", {}):
            state["run_control"]["scheduled_dates"][scheduled_date]["status"] = report.get("final_status", report.get("status"))
            state["run_control"]["scheduled_dates"][scheduled_date]["finished_at"] = report.get("finish_time")
        state["run_control"].setdefault("recent_runs", []).append(
            {
                "run_id": report["run_id"],
                "started_at": report["start_time"],
                "finished_at": report.get("finish_time"),
                "trigger_source": report["trigger_source"],
                "final_status": report.get("final_status"),
            }
        )
        state["run_control"]["recent_runs"] = state["run_control"]["recent_runs"][-100:]
        self.store.save(state)

    def _run_locked(
        self,
        report: dict[str, Any],
        excluded_item_ids: Optional[set[str]] = None,
        eligible_item_ids: Optional[set[str]] = None,
        allow_ideation: bool = True,
    ) -> dict[str, Any]:
        try:
            state = self.store.load()
        except CorruptAutonomyStateError as exc:
            report["blockers"].append("Persistent autonomy state was corrupt; execution stopped conservatively.")
            marker = exc.recovery_path.name if exc.recovery_path else "the recovery marker"
            action = (
                f"Inspect {exc.quarantine_path.name}, restore a verified autonomy_state.json, "
                f"then remove {marker} and retry in dry-run mode."
            )
            report["human_actions"].append(action)
            report["escalations"].append(
                format_escalation("Autonomous assistant", "Load roadmap state", "Read the persistent state file", str(exc), "technical", action, False)
            )
            return self._persist_and_return(report, "blocked", "needs_human")

        now = self._now()
        if not self._recover_stale_run(state, report, now):
            return self._persist_and_return(report, "skipped", "overlap_prevented")

        scheduled_date = report.get("scheduled_date")
        if scheduled_date and scheduled_date in state["run_control"].get("scheduled_dates", {}):
            prior = state["run_control"]["scheduled_dates"][scheduled_date]
            report["deferred"].append(f"Scheduled run for {scheduled_date} already claimed by {prior.get('run_id', 'another run')}.")
            return self._persist_and_return(report, "skipped", "idempotent_skip")

        active_claim = {"run_id": report["run_id"], "started_at": report["start_time"], "item_id": None}
        state["run_control"]["active_run"] = active_claim
        if scheduled_date:
            state["run_control"].setdefault("scheduled_dates", {})[scheduled_date] = {
                "run_id": report["run_id"],
                "claimed_at": report["start_time"],
                "status": "running",
            }
        local_date = self._local_date(now)
        spent_before = self._reset_budget_day(state, local_date)
        try:
            remaining_budget = self._apply_budget_snapshot(state, report, before=True)
        except Exception as exc:
            report["errors"].append(str(exc))
            report["blockers"].append("The shared budget ledger could not be read; no work was started.")
            action = "Restore or repair the Company Mode budget state, then retry in dry-run mode."
            report["human_actions"].append(action)
            report["escalations"].append(
                format_escalation(
                    "Autonomous assistant",
                    "Daily budget preflight",
                    "Claimed the run but invoked no model or task executor",
                    str(exc),
                    "technical",
                    action,
                    True,
                )
            )
            self._finish_report(report, "blocked", "needs_human")
            self._close_run_claim(state, report)
            self._persist_report(report)
            return redact_secrets(report)
        spent_before = report["budget"]["spent_before_usd"]
        self.store.save(state)

        try:
            selected = _select_project_and_item(
                state,
                excluded_item_ids,
                eligible_item_ids,
            )
            if selected is None:
                report["daily_plan"].append(
                    "No actionable roadmap work; consider one controlled Lumen idea batch."
                )
                if report["dry_run"]:
                    report["deferred"].append("Creative callback skipped because this is a dry run.")
                    self._finish_report(report, "dry_run", "idle_dry_run")
                elif not allow_ideation:
                    report["deferred"].append("Creative work was disabled by the session policy.")
                    self._finish_report(report, "completed", "idle")
                elif remaining_budget <= 0:
                    report["deferred"].append("Creative work was deferred because no ordinary daily budget remains.")
                    self._finish_report(report, "deferred", "budget_deferred")
                elif self.idea_generator is not None and self.config.max_ideas_per_run > 0:
                    try:
                        generated = self.idea_generator(deepcopy(state), self.config.max_ideas_per_run)
                        metadata = generated if isinstance(generated, Mapping) and "ideas" in generated else None
                        candidates = metadata.get("ideas", []) if metadata is not None else generated
                        self._add_ideas(state, candidates, report)
                        deferral_kind = (
                            self._record_idea_generation(state, report, metadata, spent_before)
                            if metadata else None
                        )
                        if deferral_kind == "budget":
                            self._finish_report(report, "deferred", "budget_deferred")
                        elif deferral_kind:
                            self._finish_report(report, "completed", "idle")
                        else:
                            self._finish_report(report, "completed", "ideas_proposed" if report["ideas_added"] else "idle")
                    except Exception as exc:
                        report["errors"].append(str(exc))
                        report["blockers"].append("Creative idea generation failed; no roadmap work was affected.")
                        self._finish_report(report, "completed", "idle")
                else:
                    self._finish_report(report, "completed", "idle")
                self._close_run_claim(state, report)
                self._persist_report(report)
                return redact_secrets(report)

            project, item = selected
            task_record = {
                "project_id": project.get("id"),
                "id": item.get("id"),
                "title": item.get("title"),
                "status": "selected",
                "priority": item.get("priority"),
                "agent_owner": item.get("agent_owner", "manager"),
                "authorization_level": item.get("authorization_level", "propose"),
                "acceptance_criteria": list(item.get("acceptance_criteria", []) or []),
            }
            report["tasks_selected"].append(task_record)
            report["daily_plan"].append(f"{project.get('name', project.get('id'))}: {item.get('title', item.get('id'))}")
            report["agents_involved"].append(str(item.get("agent_owner", "manager")))
            state["run_control"]["active_run"]["item_id"] = item.get("id")

            if not task_record["acceptance_criteria"]:
                reason = "The selected roadmap item has no explicit acceptance criteria."
                action = (
                    f"Add at least one verifiable acceptance criterion to {item.get('id')}, "
                    "mark it ready, and retry in dry-run mode."
                )
                task_record["status"] = "needs_human"
                task_record["failure_classification"] = "decision_required"
                report["blockers"].append("missing_acceptance_criteria")
                report["human_actions"].append(action)
                report["escalations"].append(
                    format_escalation(
                        project,
                        item,
                        "Selected the roadmap item; no model or task executor was invoked.",
                        reason,
                        "decision_required",
                        action,
                        True,
                    )
                )
                if not report["dry_run"]:
                    item["status"] = "needs_human"
                    item["human_decision_required"] = True
                    item["human_action"] = action
                self._finish_report(
                    report,
                    "blocked",
                    "needs_human" if not report["dry_run"] else "dry_run",
                )
                self._close_run_claim(state, report)
                self._persist_report(report)
                return redact_secrets(report)

            decision = self._route(item, remaining_budget)
            self._record_route(report, decision)

            if not self._authorization_allowed(item):
                requested = _authorization(item.get("authorization_level"), AuthorizationLevel.PROPOSE).value
                reason = f"Task requires {requested}; autonomous ceiling is {self.config.max_authorization.value}."
                action = f"Approve this task or lower its authorization requirement before retrying {item.get('id')}."
                escalation = format_escalation(project, item, "Selected and authorization-checked the task", reason, "decision_required", action, True)
                report["blockers"].append(reason)
                report["escalations"].append(escalation)
                report["human_actions"].append(action)
                task_record["status"] = "needs_human"
                task_record["failure_classification"] = "decision_required"
                if not report["dry_run"]:
                    item["status"] = "needs_human"
                    item["human_decision_required"] = True
                    item["human_action"] = action
                self._finish_report(report, "blocked", "needs_human" if not report["dry_run"] else "dry_run")
            elif bool(_decision_field(decision, "deferred", False)):
                deferral_code = str(_decision_field(decision, "deferral_reason", "routing_unavailable"))
                reason = str(_decision_field(decision, "reason", deferral_code))
                report["deferred"].append(reason)
                if deferral_code == "insufficient_budget":
                    report["blockers"].append("budget_exhausted")
                    task_record["status"] = "deferred"
                    task_record["failure_classification"] = "budget_exhausted"
                    if not report["dry_run"]:
                        item["status"] = "deferred"
                    self._finish_report(report, "deferred", "budget_deferred")
                else:
                    failure = "no_progress" if deferral_code == "no_stronger_model" else "unavailable_tool"
                    action = (
                        "Review the model catalog/capability requirements or reduce the task context, "
                        f"then mark {item.get('id')} ready and retry."
                    )
                    report["blockers"].append(deferral_code)
                    report["human_actions"].append(action)
                    report["escalations"].append(
                        format_escalation(
                            project,
                            item,
                            "Evaluated the configured model catalog; no model call was made.",
                            reason,
                            failure,
                            action,
                            True,
                        )
                    )
                    task_record["status"] = "needs_human"
                    task_record["failure_classification"] = failure
                    if not report["dry_run"]:
                        item["status"] = "needs_human"
                        item["human_decision_required"] = True
                        item["human_action"] = action
                    self._finish_report(
                        report,
                        "blocked" if not report["dry_run"] else "dry_run",
                        "needs_human" if not report["dry_run"] else "dry_run",
                    )
            elif report["dry_run"]:
                task_record["status"] = "planned"
                self._finish_report(report, "dry_run", "dry_run")
            else:
                self._execute(state, project, item, decision, report, spent_before)
                final = "completed" if item.get("status") == "completed" else item.get("status", "failed")
                self._finish_report(report, "completed" if final == "completed" else "blocked", str(final))
        except Exception as exc:
            report["errors"].append(str(exc))
            report["blockers"].append("Unexpected coordinator failure; no automatic retry was attempted.")
            action = "Inspect the run report, correct the coordinator error, then retry manually in dry-run mode."
            report["human_actions"].append(action)
            report["escalations"].append(
                format_escalation("Autonomous assistant", "Daily run", "Selected roadmap work", str(exc), classify_failure(exc), action, True)
            )
            self._finish_report(report, "failed", "needs_human")

        self._close_run_claim(state, report)
        self._persist_report(report)
        return redact_secrets(report)

    @staticmethod
    def _append_unique(target: list[Any], values: Any) -> None:
        for value in values or []:
            if value not in target:
                target.append(deepcopy(value))

    def _merge_session_cycle(
        self,
        session: dict[str, Any],
        cycle: Mapping[str, Any],
    ) -> None:
        """Fold one persisted single-cycle report into a bounded session report."""

        session["cycle_reports"].append(deepcopy(dict(cycle)))
        session["daily_plan"].extend(deepcopy(list(cycle.get("daily_plan", []) or [])))
        session["tasks_selected"].extend(deepcopy(list(cycle.get("tasks_selected", []) or [])))
        for field in (
            "agents_involved",
            "models_selected",
            "model_selection_reasons",
            "collaborations",
            "review_outcomes",
            "blockers",
            "deferred",
            "escalations",
            "human_actions",
            "files_changed",
            "tests_executed",
            "artifacts",
            "ideas_added",
            "idea_proposals",
            "stale_recoveries",
            "errors",
        ):
            self._append_unique(session[field], cycle.get(field, []))

        for token_field in ("input_tokens", "cached_input_tokens", "output_tokens", "total_tokens"):
            session["token_usage"][token_field] = _safe_int(
                session["token_usage"].get(token_field), 0
            ) + _safe_int((cycle.get("token_usage", {}) or {}).get(token_field), 0)
        session["estimated_cost_usd"] = _money(
            session.get("estimated_cost_usd", 0.0) + _money(cycle.get("estimated_cost_usd", 0.0))
        )
        session["actual_cost_usd"] = _money(
            session.get("actual_cost_usd", 0.0) + _money(cycle.get("actual_cost_usd", 0.0))
        )
        session["cost_is_estimated"] = bool(session.get("cost_is_estimated")) or bool(
            cycle.get("cost_is_estimated")
        )
        for dimension in ("by_project", "by_task", "by_agent", "by_model"):
            for key, amount in ((cycle.get("costs", {}) or {}).get(dimension, {}) or {}).items():
                session["costs"][dimension][str(key)] = _money(
                    session["costs"][dimension].get(str(key), 0.0) + _money(amount)
                )
        session["retry_count"] = _safe_int(session.get("retry_count"), 0) + _safe_int(
            cycle.get("retry_count"), 0
        )

        cycle_budget = cycle.get("budget", {}) or {}
        if len(session["cycle_reports"]) == 1:
            session["budget"] = deepcopy(cycle_budget)
        else:
            initial_spend = session["budget"].get("spent_before_usd", 0.0)
            session["budget"].update(deepcopy(cycle_budget))
            session["budget"]["spent_before_usd"] = initial_spend

        if cycle.get("result_text"):
            session["result_text"] = str(cycle.get("result_text") or "")
            session["result_task_id"] = cycle.get("result_task_id")
            session["result_agent"] = cycle.get("result_agent")
            session["result_truncated"] = bool(cycle.get("result_truncated"))

    @staticmethod
    def _cycle_is_global_stop(cycle: Mapping[str, Any]) -> bool:
        final = str(cycle.get("final_status") or "").lower()
        tasks = cycle.get("tasks_selected", []) or []
        if cycle.get("errors"):
            return True
        if final in {"disabled", "overlap_prevented", "idempotent_skip"}:
            return True
        if not tasks and final not in {"idle", "ideas_proposed", "idle_dry_run", "dry_run"}:
            return True
        if tasks:
            task = tasks[0]
            return (
                str(task.get("status") or "").lower() == "deferred"
                and str(task.get("failure_classification") or "").lower()
                == "decision_required"
            )
        return False

    def _finish_session(
        self,
        report: dict[str, Any],
        stop_reason: str,
    ) -> dict[str, Any]:
        report["stop_reason"] = stop_reason
        completed = any(
            str(task.get("status") or "").lower() in TERMINAL_TASK_STATUSES
            for task in report.get("tasks_selected", [])
        )
        needs_human = any(
            str(task.get("status") or "").lower() in {"blocked", "needs_human"}
            for task in report.get("tasks_selected", [])
        )
        if report.get("dry_run"):
            status, final = "dry_run", "dry_run"
        elif stop_reason == "disabled":
            status, final = "skipped", "disabled"
        elif stop_reason == "overlap_prevented":
            status, final = "skipped", "overlap_prevented"
        elif stop_reason == "scheduled_date_already_claimed":
            status, final = "skipped", "idempotent_skip"
        elif needs_human or report.get("human_actions"):
            status, final = "blocked", "needs_human"
        elif completed:
            status, final = "completed", "completed"
        elif report.get("ideas_added"):
            status, final = "completed", "ideas_proposed"
        elif stop_reason in {"budget_floor", "budget_deferred"}:
            status, final = "deferred", "budget_deferred"
        else:
            status, final = "completed", "idle"
        self._finish_report(report, status, final)

        scheduled_date = report.get("scheduled_date")
        if stop_reason in {
            "disabled",
            "overlap_prevented",
            "scheduled_date_already_claimed",
        }:
            self._persist_report(report)
            return redact_secrets(report)
        try:
            state = self.store.load()
            if scheduled_date:
                claim = state["run_control"].get("scheduled_dates", {}).get(scheduled_date)
                if claim and claim.get("run_id") == report.get("run_id"):
                    claim["status"] = report["final_status"]
                    claim["finished_at"] = report["finish_time"]
                    claim["cycle_count"] = len(report.get("cycle_reports", []))
            for recent in state["run_control"].get("recent_runs", []):
                if recent.get("run_id") == report.get("run_id"):
                    recent["finished_at"] = report["finish_time"]
                    recent["final_status"] = report["final_status"]
                    recent["session"] = True
                    recent["cycle_count"] = len(report.get("cycle_reports", []))
                    break
            self.store.save(state)
        except CorruptAutonomyStateError:
            # The child report already contains the fail-closed recovery action.
            pass
        self._persist_report(report)
        return redact_secrets(report)

    def _run_session_locked(
        self,
        report: dict[str, Any],
        *,
        eligible_item_ids: Optional[set[str]],
        max_selected_items: int,
        allow_ideation: bool,
    ) -> dict[str, Any]:
        attempted_item_ids: set[str] = set()
        task_attempts = 0
        creative_attempted = False
        blocked_task_seen = False
        started_monotonic = time.monotonic()
        stop_reason = "no_actionable_work"

        while True:
            if task_attempts >= max_selected_items:
                stop_reason = (
                    "session_policy_item_cap"
                    if max_selected_items < self.config.max_tasks_per_run
                    else "max_tasks_reached"
                )
                break
            if time.monotonic() - started_monotonic >= self.config.max_session_minutes * 60:
                stop_reason = "max_session_time_reached"
                break
            if report["cycle_reports"]:
                budget_date = str(report["budget"].get("budget_date") or "")
                if budget_date and self._local_date(self._now()) != budget_date:
                    stop_reason = "budget_date_changed"
                    break
            if report["cycle_reports"] and _money(report["budget"].get("remaining_usd")) < self.config.min_task_reservation_usd:
                stop_reason = "budget_floor"
                break

            try:
                current_state = self.store.load()
                next_selected = _select_project_and_item(
                    current_state,
                    attempted_item_ids,
                    eligible_item_ids,
                )
            except CorruptAutonomyStateError:
                next_selected = None
            if next_selected is None:
                if blocked_task_seen:
                    stop_reason = "needs_human"
                    break
                if creative_attempted:
                    stop_reason = "no_actionable_work"
                    break

            cycle = self._new_report(
                report["trigger_source"] if not report["cycle_reports"] else "session_continuation",
                bool(report["dry_run"]),
                report.get("scheduled_date") if not report["cycle_reports"] else None,
            )
            cycle["session_parent_run_id"] = report["run_id"]
            cycle["session_policy"] = deepcopy(report.get("session_policy", {}))
            cycle["report_metadata"] = deepcopy(report.get("report_metadata", {}))
            if not report["cycle_reports"]:
                cycle["run_id"] = report["run_id"]
                cycle["start_time"] = report["start_time"]
                cycle["started_at"] = report["started_at"]
                cycle["report_path"] = report["report_path"]
            cycle = self._run_locked(
                cycle,
                attempted_item_ids,
                eligible_item_ids,
                allow_ideation,
            )
            cycle["cycle_index"] = len(report["cycle_reports"]) + 1
            self._merge_session_cycle(report, cycle)

            selected_tasks = cycle.get("tasks_selected", []) or []
            cycle_final = str(cycle.get("final_status") or "").lower()
            if cycle_final in {"disabled", "overlap_prevented", "idempotent_skip"} or (
                not selected_tasks
                and cycle_final not in {
                    "idle",
                    "ideas_proposed",
                    "idle_dry_run",
                    "dry_run",
                    "budget_deferred",
                }
            ):
                stop_reason = (
                    "scheduled_date_already_claimed"
                    if cycle_final == "idempotent_skip"
                    else "overlap_prevented"
                    if cycle_final == "overlap_prevented"
                    else "global_deferral"
                )
                break
            if selected_tasks:
                item_id = str(selected_tasks[0].get("id") or "").strip()
                if not item_id or item_id in attempted_item_ids:
                    stop_reason = "no_progress"
                    break
                attempted_item_ids.add(item_id)
                task_attempts += 1
                if str(selected_tasks[0].get("status") or "").lower() in {
                    "blocked",
                    "needs_human",
                }:
                    blocked_task_seen = True
            else:
                creative_attempted = True
                if cycle.get("ideas_added"):
                    stop_reason = "ideas_proposed"
                elif cycle.get("creative_deferral_reason"):
                    stop_reason = "idea_generation_deferred"
                elif str(cycle.get("final_status") or "") == "budget_deferred":
                    stop_reason = "budget_deferred"
                else:
                    stop_reason = "no_actionable_work"
                break

            if report["dry_run"]:
                stop_reason = "dry_run_complete"
                break
            if self._cycle_is_global_stop(cycle):
                final = str(cycle.get("final_status") or "")
                stop_reason = (
                    "scheduled_date_already_claimed"
                    if final == "idempotent_skip"
                    else "overlap_prevented"
                    if final == "overlap_prevented"
                    else "global_deferral"
                )
                break

        return self._finish_session(report, stop_reason)

    def run_session(
        self,
        trigger_source: str = "manual",
        *,
        dry_run: Optional[bool] = None,
        scheduled_date: Optional[str] = None,
        eligible_item_ids: Optional[Iterable[str]] = None,
        max_selected_items: Optional[int] = None,
        allow_ideation: bool = True,
        report_metadata: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any]:
        """Run a bounded multi-item session under one persistent overlap lock."""

        if eligible_item_ids is None:
            normalized_eligible_ids: Optional[set[str]] = None
        else:
            if isinstance(eligible_item_ids, (str, bytes)):
                raise ValueError("eligible_item_ids must be an iterable of non-empty text IDs.")
            normalized_eligible_ids = set()
            for raw_item_id in eligible_item_ids:
                if not isinstance(raw_item_id, str) or not raw_item_id.strip():
                    raise ValueError("eligible_item_ids must contain only non-empty text IDs.")
                normalized_eligible_ids.add(raw_item_id.strip())
        if max_selected_items is None:
            effective_max_selected_items = self.config.max_tasks_per_run
        else:
            if (
                isinstance(max_selected_items, bool)
                or not isinstance(max_selected_items, int)
                or max_selected_items <= 0
            ):
                raise ValueError("max_selected_items must be a positive integer.")
            effective_max_selected_items = min(
                max_selected_items,
                self.config.max_tasks_per_run,
            )
        if not isinstance(allow_ideation, bool):
            raise ValueError("allow_ideation must be a boolean.")
        if report_metadata is None:
            normalized_report_metadata: dict[str, Any] = {}
        else:
            if not isinstance(report_metadata, Mapping):
                raise ValueError("report_metadata must be a JSON-serializable object.")
            try:
                redacted_metadata = redact_secrets(deepcopy(dict(report_metadata)))
                encoded_metadata = json.dumps(
                    redacted_metadata,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
                if len(encoded_metadata) > SESSION_REPORT_METADATA_MAX_BYTES:
                    raise ValueError(
                        f"report_metadata exceeds the {SESSION_REPORT_METADATA_MAX_BYTES}-byte limit."
                    )
                normalized_report_metadata = json.loads(encoded_metadata.decode("utf-8"))
            except (TypeError, ValueError) as exc:
                if isinstance(exc, ValueError) and str(exc).startswith("report_metadata exceeds"):
                    raise
                raise ValueError("report_metadata must contain only finite JSON values.") from exc

        effective_dry_run = self.config.dry_run if dry_run is None else bool(dry_run)
        normalized_trigger = str(trigger_source or "manual").strip().lower()
        now = self._now()
        if scheduled_date is None and normalized_trigger in {"scheduled", "scheduler", "daily"}:
            scheduled_date = self._local_date(now)
        report = self._new_report(normalized_trigger, effective_dry_run, scheduled_date)
        report.update(
            session=True,
            cycle_reports=[],
            max_tasks_per_run=self.config.max_tasks_per_run,
            max_session_minutes=self.config.max_session_minutes,
            session_policy={
                "eligible_item_ids": (
                    None
                    if normalized_eligible_ids is None
                    else sorted(normalized_eligible_ids)
                ),
                "requested_max_selected_items": max_selected_items,
                "effective_max_selected_items": effective_max_selected_items,
                "allow_ideation": allow_ideation,
            },
            report_metadata=normalized_report_metadata,
        )
        if normalized_trigger in {"scheduled", "scheduler", "daily"} and not self.config.enabled:
            report["deferred"].append("Scheduled autonomy is disabled by configuration.")
            return self._finish_session(report, "disabled")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        run_lock = FileLock(str(self.run_lock_path), timeout=self.config.lock_timeout_seconds)
        try:
            with run_lock:
                return self._run_session_locked(
                    report,
                    eligible_item_ids=normalized_eligible_ids,
                    max_selected_items=effective_max_selected_items,
                    allow_ideation=allow_ideation,
                )
        except FileLockTimeout:
            report["blockers"].append("Another autonomous run holds the persistent run lock.")
            return self._finish_session(report, "overlap_prevented")

    def run(
        self,
        trigger_source: str = "manual",
        *,
        dry_run: Optional[bool] = None,
        scheduled_date: Optional[str] = None,
    ) -> dict[str, Any]:
        """Run one safe planning/execution cycle and return its structured report."""

        effective_dry_run = self.config.dry_run if dry_run is None else bool(dry_run)
        normalized_trigger = str(trigger_source or "manual").strip().lower()
        now = self._now()
        if scheduled_date is None and normalized_trigger in {"scheduled", "scheduler", "daily"}:
            scheduled_date = self._local_date(now)
        report = self._new_report(normalized_trigger, effective_dry_run, scheduled_date)
        if normalized_trigger in {"scheduled", "scheduler", "daily"} and not self.config.enabled:
            report["deferred"].append("Scheduled autonomy is disabled by configuration.")
            return self._persist_and_return(report, "skipped", "disabled")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        run_lock = FileLock(str(self.run_lock_path), timeout=self.config.lock_timeout_seconds)
        try:
            with run_lock:
                return self._run_locked(report)
        except FileLockTimeout:
            report["blockers"].append("Another autonomous run holds the persistent run lock.")
            return self._persist_and_return(report, "skipped", "overlap_prevented")

    def register_scheduler(self, scheduler: Any):
        return register_scheduler(
            scheduler,
            lambda: self.run_session(trigger_source="scheduled"),
            config=self.config,
        )


def build_cron_trigger(config: Optional[AutonomyConfig] = None):
    from apscheduler.triggers.cron import CronTrigger

    resolved = config or AutonomyConfig.from_env()
    return CronTrigger(
        day_of_week=resolved.schedule_days,
        hour=resolved.schedule_hour,
        minute=resolved.schedule_minute,
        timezone=ZoneInfo(resolved.timezone),
    )


def register_scheduler(scheduler: Any, callback: Callable[..., Any], config: Optional[AutonomyConfig] = None):
    """Register the weekday job on an existing APScheduler instance."""

    resolved = config or AutonomyConfig.from_env()
    if not resolved.enabled:
        return None
    trigger = build_cron_trigger(resolved)
    return scheduler.add_job(
        callback,
        trigger=trigger,
        id="autonomous-daily-run",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )


def run_daily(
    *,
    trigger_source: str = "manual",
    dry_run: Optional[bool] = None,
    config: Optional[AutonomyConfig] = None,
    executor: Optional[Callable[..., Any]] = None,
    idea_generator: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """Run one bounded daily session without requiring a long-lived workflow object."""

    return AutonomousWorkflow(config, executor=executor, idea_generator=idea_generator).run_session(
        trigger_source=trigger_source,
        dry_run=dry_run,
    )


def _cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect the next autonomous roadmap action safely.")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Plan and write an audit report without invoking callbacks (default).")
    parser.add_argument("--scheduled-date", help="Optional YYYY-MM-DD idempotency key for scheduler testing.")
    parser.add_argument("--json", action="store_true", help="Print the complete structured report.")
    args = parser.parse_args(argv)
    config = replace(AutonomyConfig.from_env(), dry_run=True)
    report = AutonomousWorkflow(config).run(
        trigger_source="manual",
        dry_run=True,
        scheduled_date=args.scheduled_date,
    )
    print(json.dumps(report, indent=2) if args.json else report["telegram_summary"])
    print(f"Report: {report['report_path']}")
    return 0 if report.get("status") not in {"failed"} else 1


if __name__ == "__main__":
    raise SystemExit(_cli())

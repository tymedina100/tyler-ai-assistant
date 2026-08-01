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
import os
import re
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from zoneinfo import ZoneInfo

from filelock import FileLock, Timeout as FileLockTimeout


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_ROADMAP_FILE = BASE_DIR / "config" / "autonomous-roadmap.json"
STATE_VERSION = 1
TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_RESULT_PREVIEW_CHARS = 360
TERMINAL_TASK_STATUSES = {"complete", "completed", "done", "approved", "shipped"}
ACTIONABLE_TASK_STATUSES = {"planned", "ready", "pending", "todo", "deferred", "retry"}
RETRYABLE_HUMAN_STATUSES = {"blocked", "needs_human"}
RETRY_BLOCKED_PROJECT_STATUSES = {
    "paused",
    "archived",
    "cancelled",
    "complete",
    "completed",
}
HUMAN_RESOLUTION_HISTORY_LIMIT = 50
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
    if not isinstance(state["projects"], list) or not isinstance(state["idea_backlog"], list):
        raise ValueError("Autonomy projects and idea_backlog must be lists")
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

    def _reset_item_for_retry(self, item_id: str, *, reset_at: datetime) -> str:
        """Atomically reset one unambiguous human-blocked item to ``ready``.

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
            if previous_status not in RETRYABLE_HUMAN_STATUSES:
                raise RoadmapItemRetryError(
                    f"Roadmap item {target_id!r} is {previous_status!r}; only 'needs_human' "
                    "or 'blocked' items can be reset to 'ready'."
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


def _select_project_and_item(
    state: Mapping[str, Any],
    excluded_item_ids: Optional[set[str]] = None,
) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
    excluded = {str(value) for value in (excluded_item_ids or set())}
    all_items = {
        str(item.get("id")): item
        for _, _, _, item in _iter_project_items(state)
        if item.get("id") is not None
    }
    candidates = []
    for project_index, item_index, project, item in _iter_project_items(state):
        if str(item.get("id")) in excluded:
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
    """Build a concise, actionable Telegram escalation."""

    project_name = project if isinstance(project, str) else project.get("name") or project.get("title") or project.get("id")
    task_name = task if isinstance(task, str) else task.get("title") or task.get("id")
    message = (
        "OWNER ACTION NEEDED\n"
        f"Project: {project_name or 'Unknown'}\n"
        f"Task: {task_name or 'Unknown'}\n"
        f"Attempted: {attempted or 'No execution started.'}\n"
        f"Blocked by: {category or 'unknown'} - {reason}\n"
        f"Action: {action_required or 'Review the task and provide direction.'}\n"
        f"Other work: {'can continue' if other_work_can_continue else 'is also blocked'}"
    )
    return _redact_text(message)


def _money(value: Any) -> float:
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return 0.0


def format_telegram_summary(report: Mapping[str, Any]) -> str:
    """Format the required end-of-run fields without another model call."""

    tasks = report.get("tasks_selected", []) or []
    completed = [task for task in tasks if str(task.get("status", "")).lower() in TERMINAL_TASK_STATUSES]
    planned = ", ".join(str(task.get("title") or task.get("id")) for task in tasks) or "No roadmap task"
    completed_text = ", ".join(str(task.get("title") or task.get("id")) for task in completed) or "None"
    changed = list(report.get("files_changed", []) or []) + list(report.get("artifacts", []) or [])
    idea_proposals = [
        idea for idea in (report.get("idea_proposals", []) or []) if isinstance(idea, Mapping)
    ]
    idea_titles = [str(idea.get("idea") or idea.get("id") or "Untitled idea") for idea in idea_proposals]
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
    report_label = "session" if report.get("session") else "run"
    lines = [
        f"Autonomous {report_label}: {report.get('final_status') or report.get('status') or 'unknown'}",
        f"Planned: {planned}",
        f"Completed: {completed_text}",
        f"Changed: {', '.join(map(str, changed)) if changed else 'None'}",
        f"Ideas: {', '.join(idea_titles) if idea_titles else 'None'}",
        f"Deferred: {', '.join(map(str, deferred)) if deferred else 'None'}",
        f"Blocked: {', '.join(map(str, blockers)) if blockers else 'None'}",
        f"Budget: ${used:.4f} used of ${limit:.2f}; ${remaining:.4f} remaining{estimate_label}",
        f"Your action: {'; '.join(map(str, actions)) if actions else 'None'}",
    ]
    stop_reason = str(report.get("stop_reason") or "").strip()
    if stop_reason:
        lines.insert(-2, f"Stop: {stop_reason.replace('_', ' ')}")
    result_text = _redact_text(str(report.get("result_text") or "")).strip()
    if completed and result_text:
        compact_result = re.sub(r"\s+", " ", result_text)
        base_text = _redact_text("\n".join(lines))
        available = max(0, TELEGRAM_MESSAGE_LIMIT - len(base_text) - len("\nResult: "))
        preview_limit = min(TELEGRAM_RESULT_PREVIEW_CHARS, available)
        if preview_limit > 0:
            marker = " [preview truncated]"
            if len(compact_result) > preview_limit:
                if preview_limit <= len(marker):
                    compact_result = marker[:preview_limit]
                else:
                    keep = preview_limit - len(marker)
                    compact_result = compact_result[:keep].rstrip() + marker
            lines.insert(3, f"Result: {compact_result}")
    return _redact_text("\n".join(lines))[:TELEGRAM_MESSAGE_LIMIT]


def format_telegram_idea_plan(report: Mapping[str, Any]) -> str:
    """Render controlled Lumen proposals without another model call."""

    proposals = [
        idea for idea in (report.get("idea_proposals", []) or []) if isinstance(idea, Mapping)
    ]
    if not proposals:
        return ""
    lines = ["Lumen idea plan"]
    for index, idea in enumerate(proposals, 1):
        lines.extend([
            "",
            f"{index}. {idea.get('idea') or 'Untitled idea'}",
            f"Problem: {idea.get('problem_addressed') or 'Not specified'}",
            f"Expected value: {idea.get('expected_value') or 'Not specified'}",
            f"Effort: {idea.get('estimated_effort') or 'unknown'}; estimated AI cost: ${_money(idea.get('estimated_ai_cost_usd')):.4f}",
            f"Next validation: {idea.get('recommended_next_validation_step') or 'Owner review'}",
        ])
    rendered = _redact_text("\n".join(lines))
    if len(rendered) <= TELEGRAM_MESSAGE_LIMIT:
        return rendered
    marker = "\n[idea plan truncated; see the persisted run report]"
    return rendered[: TELEGRAM_MESSAGE_LIMIT - len(marker)].rstrip() + marker


def format_telegram_deliverable(report: Mapping[str, Any]) -> str:
    """Format the substantive completed result for chunked Telegram delivery."""

    tasks = report.get("tasks_selected", []) or []
    completed = [task for task in tasks if str(task.get("status", "")).lower() in TERMINAL_TASK_STATUSES]
    result_text = _redact_text(str(report.get("result_text") or "")).strip()
    if not completed or not result_text:
        return format_telegram_idea_plan(report)
    task = completed[0]
    lines = [
        "Autonomous deliverable",
        f"Task: {task.get('title') or task.get('id') or 'Unknown'}",
        f"Agent: {report.get('result_agent') or task.get('agent_owner') or 'unrecorded'}",
        "",
        result_text,
    ]
    if report.get("result_truncated"):
        lines.extend(["", "Note: the captured agent result reached the configured storage limit."])
    return _redact_text("\n".join(lines))


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

    def retry_item(self, item_id: str) -> tuple[bool, str]:
        """Safely make one human-blocked item eligible for a future run.

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
            idea = {
                "id": str(candidate.get("id") or f"idea_{uuid.uuid4().hex[:10]}"),
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
            fingerprint = _idea_fingerprint(idea)
            if fingerprint in fingerprints:
                continue
            idea["fingerprint"] = fingerprint
            safe_idea = redact_secrets(idea)
            backlog.append(safe_idea)
            fingerprints.add(fingerprint)
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
        try:
            if self.executor is None:
                raise RuntimeError("No autonomous executor is configured")
            raw_result = self.executor(project, item, decision, report["run_id"])
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
            selected = _select_project_and_item(state, excluded_item_ids)
            if selected is None:
                report["daily_plan"].append(
                    "No actionable roadmap work; consider one controlled Lumen idea batch."
                )
                if report["dry_run"]:
                    report["deferred"].append("Creative callback skipped because this is a dry run.")
                    self._finish_report(report, "dry_run", "idle_dry_run")
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
        if report.get("dry_run"):
            status, final = "dry_run", "dry_run"
        elif stop_reason == "disabled":
            status, final = "skipped", "disabled"
        elif stop_reason == "overlap_prevented":
            status, final = "skipped", "overlap_prevented"
        elif stop_reason == "scheduled_date_already_claimed":
            status, final = "skipped", "idempotent_skip"
        elif completed:
            status, final = "completed", "completed"
        elif report.get("ideas_added"):
            status, final = "completed", "ideas_proposed"
        elif stop_reason in {"budget_floor", "budget_deferred"}:
            status, final = "deferred", "budget_deferred"
        elif report.get("human_actions") or report.get("errors"):
            status, final = "blocked", "needs_human"
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

    def _run_session_locked(self, report: dict[str, Any]) -> dict[str, Any]:
        attempted_item_ids: set[str] = set()
        task_attempts = 0
        creative_attempted = False
        started_monotonic = time.monotonic()
        stop_reason = "no_actionable_work"

        while True:
            if task_attempts >= self.config.max_tasks_per_run:
                stop_reason = "max_tasks_reached"
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
                next_selected = _select_project_and_item(current_state, attempted_item_ids)
            except CorruptAutonomyStateError:
                next_selected = None
            if next_selected is None and creative_attempted:
                stop_reason = "no_actionable_work"
                break

            cycle = self._new_report(
                report["trigger_source"] if not report["cycle_reports"] else "session_continuation",
                bool(report["dry_run"]),
                report.get("scheduled_date") if not report["cycle_reports"] else None,
            )
            if not report["cycle_reports"]:
                cycle["run_id"] = report["run_id"]
                cycle["start_time"] = report["start_time"]
                cycle["started_at"] = report["started_at"]
                cycle["report_path"] = report["report_path"]
            cycle = self._run_locked(cycle, attempted_item_ids)
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
    ) -> dict[str, Any]:
        """Run a bounded multi-item session under one persistent overlap lock."""

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
        )
        if normalized_trigger in {"scheduled", "scheduler", "daily"} and not self.config.enabled:
            report["deferred"].append("Scheduled autonomy is disabled by configuration.")
            return self._finish_session(report, "disabled")
        self.base_dir.mkdir(parents=True, exist_ok=True)
        run_lock = FileLock(str(self.run_lock_path), timeout=self.config.lock_timeout_seconds)
        try:
            with run_lock:
                return self._run_session_locked(report)
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

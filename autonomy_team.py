"""Pure adapter helpers between the autonomous control plane and Company Mode.

Telegram owns the async/runtime boundary.  This module keeps the authorization
matrix, per-roadmap task plan, and final Company Mode aggregation deterministic and
unit-testable without bot credentials or paid API calls.
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterable, Mapping

import model_router


READ_ONLY_TOOLS = frozenset({
    "read_file",
    "search_the_web",
    "get_company_status",
    "get_revenue_report",
    "list_deploy_projects",
    "check_deploy",
    "railway_list_projects",
    "railway_get_project",
    "railway_list_vars",
    "railway_deploy_status",
    "github_list_files",
    "github_read_file",
    "code_list_files",
    "code_read_file",
    "code_read_pr",
    "project_list",
    "project_current",
    "linear_list_issues",
    "linear_search_issues",
    "linear_get_issue",
    "linear_list_teams",
    "linear_list_projects",
})

# The current helpers named code_edit/code_propose create remote GitHub state and
# run_python is network-capable, so none of them satisfy a truthful "local only"
# boundary.  Modify-local roadmap items are escalated by the Telegram bridge until
# a real isolated checkout executor exists.
LOCAL_MODIFICATION_TOOLS = frozenset()


def normalize_authorization(value: Any) -> str:
    normalized = str(value or "propose").strip().lower()
    return {"modify_locally": "modify_local"}.get(normalized, normalized)


def allowed_tool_names(profile_tool_names: Iterable[str], authorization_level: Any) -> set[str]:
    """Intersect an agent's least-privilege profile with roadmap authorization."""

    profile = {str(name) for name in profile_tool_names}
    authorization = normalize_authorization(authorization_level)
    allowed = set(READ_ONLY_TOOLS)
    if authorization == "modify_local":
        allowed.update(LOCAL_MODIFICATION_TOOLS)
    # External actions never become automatic merely because a roadmap item says
    # external_action.  The runtime escalates those tasks before invoking a model.
    return profile & allowed


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _money(value: Any) -> float:
    try:
        return round(max(0.0, float(value)), 6)
    except (TypeError, ValueError):
        return 0.0


def _configured_number(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


def reservation_estimate(decision: Any) -> float:
    """Conservatively expand a one-call route estimate for bounded tool loops."""

    multiplier = _configured_number("AUTONOMY_COST_ESTIMATE_MULTIPLIER", 4.0, 1.0)
    minimum = _configured_number("AUTONOMY_MIN_TASK_RESERVATION_USD", 0.05, 0.001)
    return _money(max(minimum, _money(_field(decision, "estimated_cost_usd", 0.0)) * multiplier))


def _decision_record(decision: Any) -> dict[str, Any]:
    return {
        "model": _field(decision, "model_id") or _field(decision, "model"),
        "estimated_cost_usd": _money(_field(decision, "estimated_cost_usd", 0.0)),
        "reason": str(_field(decision, "reason", "No routing reason supplied.")),
        "deferred": bool(_field(decision, "deferred", False)),
        "deferral_reason": _field(decision, "deferral_reason"),
    }


def build_company_plan(
    item: Mapping[str, Any],
    worker_decision: Any,
    remaining_budget_usd: float,
    *,
    router: model_router.ModelRouter | None = None,
) -> dict[str, Any]:
    """Create one bounded worker task plus an acceptance-criteria review task."""

    worker_route = _decision_record(worker_decision)
    if worker_route["deferred"] or not worker_route["model"]:
        return {
            "deferred": True,
            "reason": worker_route["deferral_reason"] or worker_route["reason"],
            "deferral_reason": worker_route["deferral_reason"],
            "tasks": [],
            "decisions": [worker_route],
        }

    criteria = [
        str(value).strip()
        for value in item.get("acceptance_criteria", []) or []
        if str(value).strip()
    ]
    if not criteria:
        return {
            "deferred": True,
            "reason": "The roadmap item has no explicit acceptance criteria.",
            "deferral_reason": "missing_acceptance_criteria",
            "tasks": [],
            "decisions": [worker_route],
        }
    authorization = normalize_authorization(item.get("authorization_level"))
    requested_owner = str(item.get("agent_owner") or "manager")
    # Miles owns selection and prioritization, but he is not a worker persona in
    # main.SPECIALISTS. Delegate a manager-owned roadmap item to Robin's general
    # worker path so execution and cost attribution do not falsely claim Miles ran it.
    owner = "general" if requested_owner == "manager" else requested_owner
    common = {
        "acceptance_criteria": criteria,
        "authorization_level": authorization,
        "enforce_authorization": True,
        "roadmap_item_id": str(item.get("id") or ""),
        "task_type": str(item.get("task_type") or "general"),
        "complexity": str(item.get("complexity") or "standard"),
        "risk": str(item.get("risk") or "low"),
        "required_capabilities": list(item.get("required_capabilities", []) or []),
        "estimated_input_tokens": int(item.get("estimated_input_tokens", 2000) or 2000),
        "estimated_output_tokens": int(item.get("estimated_output_tokens", 600) or 600),
    }
    worker_estimate = reservation_estimate(worker_decision)
    worker_task = {
        **common,
        "owner": owner,
        "title": str(item.get("title") or item.get("id") or "Complete roadmap item"),
        "estimate_usd": worker_estimate,
        "model": worker_route["model"],
        "model_reason": worker_route["reason"],
    }
    tasks = [worker_task]
    decisions = [worker_route]

    if owner != "editor":
        review_risk = str(item.get("risk") or "low").strip().lower()
        review_type = "important_review" if review_risk in {"high", "critical", "important"} else "review"
        review_budget = max(0.0, float(remaining_budget_usd) - worker_estimate)
        review_request = model_router.RoutingRequest(
            task_type=review_type,
            complexity="advanced" if review_type == "important_review" else "standard",
            risk=review_risk,
            required_capabilities=("text", "review"),
            estimated_input_tokens=max(2000, int(common["estimated_input_tokens"])),
            estimated_output_tokens=max(500, int(common["estimated_output_tokens"])),
            remaining_budget_usd=review_budget,
        )
        review_decision = (router or model_router.ModelRouter()).route(review_request)
        review_route = _decision_record(review_decision)
        decisions.append(review_route)
        if review_route["deferred"] or not review_route["model"]:
            return {
                "deferred": True,
                "reason": (
                    "A bounded acceptance-criteria review could not be funded or routed: "
                    f"{review_route['deferral_reason'] or review_route['reason']}"
                ),
                "deferral_reason": review_route["deferral_reason"],
                "tasks": [],
                "decisions": decisions,
            }
        tasks.append({
            **common,
            "owner": "editor",
            "title": (
                "Review the completed result against every explicit acceptance criterion; "
                "respond APPROVED, REVISIONS REQUIRED, or BLOCKED - NEEDS HUMAN REVIEW."
            ),
            "task_type": review_type,
            "complexity": "advanced" if review_type == "important_review" else "standard",
            "authorization_level": "observe",
            "estimate_usd": reservation_estimate(review_decision),
            "model": review_route["model"],
            "model_reason": review_route["reason"],
        })

    total = _money(sum(float(task["estimate_usd"]) for task in tasks))
    if total > float(remaining_budget_usd) + 1e-9:
        return {
            "deferred": True,
            "reason": (
                f"The worker and required review need about ${total:.4f}, above the "
                f"${float(remaining_budget_usd):.4f} ordinary budget remaining."
            ),
            "deferral_reason": "insufficient_budget",
            "tasks": [],
            "decisions": decisions,
        }
    return {
        "deferred": False,
        "reason": "",
        "deferral_reason": None,
        "tasks": tasks,
        "decisions": decisions,
        "estimated_cost_usd": total,
    }


_FAILURE_MAP = {
    "permission": "permission_denied",
    "budget": "budget_exhausted",
    "decision": "decision_required",
    "missing_information": "decision_required",
}


def workflow_failure(classification: Any) -> str:
    value = str(classification or "technical").strip().lower()
    return _FAILURE_MAP.get(value, value or "technical")


def aggregate_company_result(
    state: Mapping[str, Any],
    project_id: str,
    *,
    fallback_model: str | None = None,
) -> dict[str, Any]:
    """Convert persisted Company Mode evidence into one workflow callback result."""

    project = next((value for value in state.get("projects", []) if value.get("id") == project_id), {})
    tasks = [value for value in state.get("tasks", []) if value.get("project_id") == project_id]
    blocked = [task for task in tasks if task.get("status") in {"blocked", "needs_human"}]
    editor_tasks = [task for task in tasks if task.get("owner") == "editor"]
    review_outcomes = [str(task.get("result") or "") for task in editor_tasks if task.get("result")]
    approved = project.get("editor_verdict") == "approved" or (not editor_tasks and project.get("status") == "completed")

    if project.get("status") == "completed" and approved:
        status = "completed"
    elif blocked or project.get("status") == "blocked":
        status = "needs_human"
    elif any(task.get("status") == "planned" for task in tasks):
        status = "deferred"
    else:
        status = "failed"

    usage_records = [
        usage
        for task in tasks
        for usage in task.get("usage_records", []) or []
        if isinstance(usage, Mapping)
    ]
    token_usage = {
        "input_tokens": sum(int(value.get("input_tokens", 0) or 0) for value in usage_records),
        "cached_input_tokens": sum(int(value.get("cached_input_tokens", 0) or 0) for value in usage_records),
        "output_tokens": sum(int(value.get("output_tokens", 0) or 0) for value in usage_records),
    }
    token_usage["total_tokens"] = token_usage["input_tokens"] + token_usage["output_tokens"]
    artifacts = list(dict.fromkeys(
        str(value)
        for task in tasks
        for value in task.get("artifacts", []) or []
        if str(value).strip()
    ))
    files_changed = []
    tests_executed = []
    for artifact in artifacts:
        if artifact.startswith("file: "):
            files_changed.append(artifact[len("file: "):])
        elif artifact.startswith("github: "):
            files_changed.append(artifact[len("github: "):])
        elif artifact.startswith("test: "):
            tests_executed.append(artifact[len("test: "):])

    task_ids = {str(task.get("id")) for task in tasks}
    relevant_costs = [
        entry
        for entry in state.get("cost_entries", []) or []
        if entry.get("project_id") == project_id or str(entry.get("task_id")) in task_ids
    ]
    spent = _money(sum(float(task.get("spent_usd", 0.0) or 0.0) for task in tasks))
    estimated = _money(sum(float(task.get("estimate_usd", 0.0) or 0.0) for task in tasks))
    cost_is_estimated = any(entry.get("cost_basis") == "estimated" for entry in relevant_costs)
    models = list(dict.fromkeys(
        str(value)
        for value in (
            [task.get("model") for task in tasks]
            + [usage.get("model") for usage in usage_records]
        )
        if str(value or "").strip()
    ))
    agents = list(dict.fromkeys(
        str(task.get("owner")) for task in tasks if str(task.get("owner") or "").strip()
    ))
    by_task = {
        str(task.get("id")): _money(task.get("spent_usd", 0.0))
        for task in tasks
        if task.get("id")
    }
    by_agent = {}
    by_model = {}
    for task in tasks:
        amount = _money(task.get("spent_usd", 0.0))
        agent_name = str(task.get("owner") or "unrecorded")
        by_agent[agent_name] = _money(by_agent.get(agent_name, 0.0) + amount)
        attributed = 0.0
        for usage in task.get("usage_records", []) or []:
            if not isinstance(usage, Mapping) or not usage.get("model"):
                continue
            usage_cost = _money(usage.get("cost_usd", 0.0))
            model_name = str(usage["model"])
            by_model[model_name] = _money(by_model.get(model_name, 0.0) + usage_cost)
            attributed = _money(attributed + usage_cost)
        residual = _money(max(0.0, amount - attributed))
        if residual or not task.get("usage_records"):
            model_name = str(task.get("model") or "unrecorded")
            by_model[model_name] = _money(by_model.get(model_name, 0.0) + residual)
    failure = workflow_failure(
        next(
            (task.get("failure_classification") for task in blocked if task.get("failure_classification")),
            project.get("failure_classification") or "technical",
        )
    )
    reasons = [str(task.get("result") or task.get("failure_classification") or "Task blocked") for task in blocked]
    reason = "; ".join(reasons) or (
        "Company Mode did not reach a terminal approved result." if status != "completed" else ""
    )
    human_actions = {
        "missing_access": "Provide the named credential or access, then mark the roadmap item ready and retry.",
        "permission_denied": "Grant only the required permission or choose a lower-impact alternative, then retry.",
        "unavailable_tool": "Configure the required tool/integration or rescope the task to available capabilities.",
        "budget_exhausted": "Increase today's budget or defer the roadmap item to another day.",
        "decision_required": "Provide the missing information or owner decision, then mark the item ready.",
        "no_progress": "Review the repeated feedback and decide whether to rescope, accept, or stop the task.",
    }
    # The reviewer verdict is evidence about the deliverable, not the deliverable
    # itself.  Prefer the latest completed worker result so proposal/observe tasks
    # remain useful after intermediate Company Mode messages are suppressed.
    result_task = next(
        (
            task
            for task in reversed(tasks)
            if task.get("owner") != "editor"
            and task.get("status") in {"done", "completed", "complete", "approved"}
            and str(task.get("result") or "").strip()
        ),
        None,
    )
    if result_task is None:
        result_task = next(
            (task for task in reversed(tasks) if str(task.get("result") or "").strip()),
            None,
        )
    result_text = str((result_task or {}).get("result") or "").strip()
    result_limit = int(_configured_number("MAX_TASK_RESULT_CHARS", 5000, 1.0))
    result_truncated = bool((result_task or {}).get("result_truncated")) or (
        bool(result_text) and len(result_text) >= result_limit
    )
    return {
        "status": status,
        "result": result_text[:1000],
        "result_text": result_text,
        "result_task_id": (result_task or {}).get("id"),
        "result_agent": (result_task or {}).get("owner"),
        "result_truncated": result_truncated,
        "reason": reason[:1500],
        "failure_classification": failure,
        "human_action": human_actions.get(failure, "Inspect the run report, correct the failure, then retry in dry-run mode."),
        "attempted": "; ".join(str(task.get("title") or task.get("id")) for task in tasks),
        "actual_cost_usd": spent,
        "estimated_cost_usd": estimated,
        "cost_is_estimated": cost_is_estimated,
        "model_invoked": any(int(task.get("execution_attempts", 0) or 0) > 0 for task in tasks),
        "model": fallback_model or (models[0] if models else None),
        "models": models,
        "agents": agents,
        "costs": {
            "by_project": {project_id: spent},
            "by_task": by_task,
            "by_agent": by_agent,
            "by_model": by_model,
        },
        "token_usage": token_usage,
        "review_outcomes": review_outcomes,
        "review_outcome": project.get("editor_verdict"),
        "retry_count": sum(max(0, int(task.get("execution_attempts", 0) or 0) - 1) for task in tasks)
        + int(project.get("revision_round", 0) or 0),
        "files_changed": list(dict.fromkeys(files_changed)),
        "tests_executed": list(dict.fromkeys(tests_executed)),
        "artifacts": artifacts,
    }


def idea_project_context(state: Mapping[str, Any]) -> str:
    """Serialize bounded planning context for the no-tools creative callback.

    Existing idea titles and recent outcomes are included so Lumen can plan around
    what the team has already proposed or attempted.  Descriptions, artifacts, task
    results, blocker text, and arbitrary state fields stay outside this prompt
    boundary because they may contain private or unnecessarily large content.
    """

    projects = []
    for project in state.get("projects", []) or []:
        projects.append({
            "id": project.get("id"),
            "name": project.get("name"),
            "status": project.get("status"),
            "goals": [
                {"id": goal.get("id"), "title": goal.get("title"), "status": goal.get("status")}
                for goal in project.get("goals", []) or []
                if isinstance(goal, Mapping)
            ],
            "roadmap": [
                {"id": item.get("id"), "title": item.get("title"), "status": item.get("status")}
                for item in project.get("roadmap_items", []) or []
                if isinstance(item, Mapping)
            ],
        })
    existing_ideas = [
        {
            "id": idea.get("id"),
            "idea": idea.get("idea"),
            "status": idea.get("status"),
            "relationship_to_current_goals": idea.get(
                "relationship_to_current_goals"
            ),
        }
        for idea in (state.get("idea_backlog", []) or [])[-20:]
        if isinstance(idea, Mapping)
    ]
    recent_runs = [
        {
            "run_id": run.get("run_id"),
            "final_status": run.get("final_status"),
            "trigger_source": run.get("trigger_source"),
        }
        for run in (
            (state.get("run_control", {}) or {}).get("recent_runs", []) or []
        )[-10:]
        if isinstance(run, Mapping)
    ]
    return json.dumps(
        {
            "projects": projects,
            "existing_ideas": existing_ideas,
            "recent_runs": recent_runs,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )


__all__ = [
    "LOCAL_MODIFICATION_TOOLS",
    "READ_ONLY_TOOLS",
    "aggregate_company_result",
    "allowed_tool_names",
    "build_company_plan",
    "idea_project_context",
    "normalize_authorization",
    "reservation_estimate",
    "workflow_failure",
]

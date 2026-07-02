import json
import os
import re
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).parent


def _data_dir():
    """Directory for persistent state. Set DATA_DIR (e.g. a mounted volume on Railway)
    so company_state.json survives redeploys; defaults to the project dir locally."""
    return Path(os.environ.get("DATA_DIR") or BASE_DIR)


COMPANY_STATE_FILE = _data_dir() / "company_state.json"

# Fresh state starts on a $5/day budget so a brand-new deploy can run a small plan
# without needing /setbudget first. Change it live any time with /setbudget <amount>.
DEFAULT_DAILY_BUDGET_USD = 5.0
# Per-task budget *reservation* (a soft pre-check hold). Kept small so a 4-task plan
# fits inside the $5 default; the real metered token cost replaces it as tasks finish.
DEFAULT_TASK_ESTIMATE_USD = 1.0
DEFAULT_ASSIGN_TASKS = [
    ("research", "Validate demand, competitors, and buyer pain for this goal."),
    ("code", "Identify the smallest buildable asset or PR that moves this goal forward."),
    ("write", "Draft the offer, positioning, landing-page copy, or sales collateral."),
    ("tasks", "Turn the plan into an operational checklist and keep follow-up tasks tidy."),
]

COMPANY_COMMANDS = {
    "/company",
    "/setbudget",
    "/assign",
    "/approve",
    "/cancel",
    "/publish",
    "/status",
    "/dailyreport",
    "/pausecompany",
    "/resumecompany",
}


def _now():
    return datetime.now().replace(microsecond=0)


def today_key():
    return _now().date().isoformat()


def new_state():
    today = today_key()
    return {
        "company": {
            "mode": "running",
            "budget_date": today,
            "daily_budget_usd": DEFAULT_DAILY_BUDGET_USD,
            "reserved_today_usd": 0.0,
            "spent_today_usd": 0.0,
            "active_project_id": None,
        },
        "projects": [],
        "tasks": [],
        "events": [],
    }


def normalize_state(state):
    base = new_state()
    if not isinstance(state, dict):
        return base

    normalized = deepcopy(base)
    normalized.update({k: state.get(k, normalized[k]) for k in ("projects", "tasks", "events")})
    normalized["company"].update(state.get("company", {}))

    if normalized["company"].get("budget_date") != today_key():
        normalized["company"]["budget_date"] = today_key()
        normalized["company"]["reserved_today_usd"] = 0.0
        normalized["company"]["spent_today_usd"] = 0.0

    return normalized


def load_state(path=COMPANY_STATE_FILE):
    if not path.exists():
        return new_state()
    try:
        return normalize_state(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return new_state()


def save_state(state, path=COMPANY_STATE_FILE):
    path.write_text(json.dumps(normalize_state(state), indent=2), encoding="utf-8")


def _money(value):
    return round(float(value), 2)


def remaining_budget(state):
    company = normalize_state(state)["company"]
    return _money(company["daily_budget_usd"] - company["reserved_today_usd"] - company["spent_today_usd"])


def set_daily_budget(amount_usd, path=COMPANY_STATE_FILE):
    amount = _money(amount_usd)
    if amount < 0:
        return "Budget must be zero or greater."

    state = load_state(path)
    state["company"]["daily_budget_usd"] = amount
    state["company"]["budget_date"] = today_key()
    save_state(state, path)
    return f"Company budget set to ${amount:.2f} for today. Remaining: ${remaining_budget(state):.2f}."


def pause_company(path=COMPANY_STATE_FILE):
    state = load_state(path)
    state["company"]["mode"] = "paused"
    add_event(state, "company_paused", "Company Mode paused.")
    save_state(state, path)
    return "Company Mode paused. I will still answer status/report commands, but I will not start new assigned work."


def resume_company(path=COMPANY_STATE_FILE):
    state = load_state(path)
    state["company"]["mode"] = "running"
    add_event(state, "company_resumed", "Company Mode resumed.")
    save_state(state, path)
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


def assign_goal(goal, configured_agent_keys, specialist_keys=None, path=COMPANY_STATE_FILE, tasks=None):
    # `tasks` is an optional dynamic work plan: a list of (owner, title) tuples chosen
    # for THIS goal (see main.plan_company_goal). When None, fall back to the fixed
    # DEFAULT_ASSIGN_TASKS so behavior is unchanged.
    goal = goal.strip()
    if not goal:
        return "Usage: /assign <goal>"

    state = load_state(path)
    if state["company"]["mode"] == "paused":
        return "Company Mode is paused. Use /resumecompany before assigning new work."

    tasks_to_create = []
    total_estimate = 0.0
    for preferred_owner, title in (tasks or DEFAULT_ASSIGN_TASKS):
        owner, delivery = _owner_for(preferred_owner, configured_agent_keys)
        estimate = DEFAULT_TASK_ESTIMATE_USD
        tasks_to_create.append((owner, delivery, title, estimate))
        total_estimate += estimate

    if remaining_budget(state) < total_estimate:
        return (
            f"Blocked: assigning this goal reserves about ${total_estimate:.2f}, "
            f"but only ${remaining_budget(state):.2f} remains today. Raise /setbudget, "
            f"rescope the goal, or defer it."
        )

    project_id = f"proj_{uuid.uuid4().hex[:8]}"
    # v2: a fresh project starts "proposed" - it doesn't run until the user /approve's
    # it (checkpointed autonomy). /assign only plans and reserves budget.
    project = {
        "id": project_id,
        "title": _project_title(goal),
        "goal": goal,
        "status": "proposed",
        "created_at": _now().isoformat(),
        "task_ids": [],
        "artifacts": [],
        "notes": [],
    }
    state["projects"].append(project)
    state["company"]["active_project_id"] = project_id

    for owner, delivery, title, estimate in tasks_to_create:
        task_id = f"task_{uuid.uuid4().hex[:8]}"
        task = {
            "id": task_id,
            "project_id": project_id,
            "title": title,
            "owner": owner,
            "delivery": delivery,
            "status": "planned",
            "estimate_usd": _money(estimate),
            "reserved_usd": _money(estimate),
            "spent_usd": 0.0,
            "created_at": _now().isoformat(),
            "updated_at": _now().isoformat(),
            "result": "",
            "artifacts": [],
            "notes": [],
        }
        state["tasks"].append(task)
        project["task_ids"].append(task_id)

    state["company"]["reserved_today_usd"] = _money(state["company"]["reserved_today_usd"] + total_estimate)
    add_event(state, "goal_assigned", f"Assigned company goal: {goal}", project_id=project_id, amount_usd=total_estimate)
    save_state(state, path)

    return render_assignment(project, tasks_to_create, state, specialist_keys or [])


def update_task_status(task_id, status, result="", artifacts=None, spent_usd=None, path=COMPANY_STATE_FILE):
    state = load_state(path)
    for task in state["tasks"]:
        if task["id"] == task_id:
            previous_status = task["status"]
            task["status"] = status
            task["updated_at"] = _now().isoformat()
            if result:
                task["result"] = result
            if artifacts:
                task["artifacts"].extend(artifacts)
                for project in state.get("projects", []):
                    if project["id"] == task["project_id"]:
                        project.setdefault("artifacts", []).extend(artifacts)
                        break
            if status in {"done", "shipped", "blocked"} and previous_status not in {"done", "shipped", "blocked"}:
                spend = task["reserved_usd"] if spent_usd is None else _money(spent_usd)
                task["spent_usd"] = spend
                state["company"]["reserved_today_usd"] = _money(max(0, state["company"]["reserved_today_usd"] - task["reserved_usd"]))
                state["company"]["spent_today_usd"] = _money(state["company"]["spent_today_usd"] + spend)
                task["reserved_usd"] = 0.0
            add_event(state, "task_updated", f"{task_id} moved to {status}.", task_id=task_id, amount_usd=task["spent_usd"])
            save_state(state, path)
            return f"{task_id} updated to {status}."
    return f"Task not found: {task_id}"


def mark_task_blocked(task_id, reason, spent_usd=None, artifacts=None, path=COMPANY_STATE_FILE):
    """Park a task that needs your approval to proceed (a gated action was staged).
    Records any real spend/artifacts produced so far and releases its reserve."""
    return update_task_status(
        task_id, "blocked", result=reason, artifacts=artifacts, spent_usd=spent_usd, path=path
    )


def next_planned_task(state, project_id):
    """The first not-yet-worked task of a project, in creation order, or None."""
    for task in project_tasks(state, project_id):
        if task["status"] == "planned":
            return task
    return None


def approve_project(path=COMPANY_STATE_FILE):
    """Flip the active project from 'proposed' to 'active' so the engine may run it.
    Returns (message, project_id_or_None) - the caller starts the runner if an id
    comes back."""
    state = load_state(path)
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
    add_event(state, "project_approved", f"Approved project: {project['title']}", project_id=project["id"])
    save_state(state, path)
    return f"Approved: {project['title']}. Starting the work plan now.", project["id"]


def cancel_project(path=COMPANY_STATE_FILE):
    """Cancel the active project and release any budget still reserved for its open
    tasks. Returns a status message."""
    state = load_state(path)
    project = active_project(state)
    if not project:
        return "No active project to cancel."

    released = 0.0
    for task in project_tasks(state, project["id"]):
        if task["status"] in {"planned", "in_progress", "blocked"} and task["reserved_usd"]:
            released += task["reserved_usd"]
            task["reserved_usd"] = 0.0
            task["status"] = "cancelled"

    project["status"] = "cancelled"
    state["company"]["reserved_today_usd"] = _money(max(0.0, state["company"]["reserved_today_usd"] - released))
    state["company"]["active_project_id"] = None
    add_event(state, "project_cancelled", f"Cancelled project: {project['title']}",
              project_id=project["id"], amount_usd=released)
    save_state(state, path)
    return f"Cancelled {project['title']} and released ${released:.2f} of reserved budget."


def record_delegation(owner, request_text, answer_text, path=COMPANY_STATE_FILE,
                      spent_usd=None, artifacts=None):
    # New kwargs go AFTER path so the existing positional-path callers still work.
    state = load_state(path)
    project = active_project(state)
    spend = _money(spent_usd) if spent_usd else 0.0
    artifacts = list(artifacts or [])
    if not project:
        add_event(state, "delegation", f"{owner} handled delegated work without an active project.",
                  amount_usd=spend or None)
        if spend:
            # Even without a project, real spend still counts against today's budget.
            state["company"]["spent_today_usd"] = _money(state["company"]["spent_today_usd"] + spend)
        save_state(state, path)
        return None

    task_id = f"task_{uuid.uuid4().hex[:8]}"
    task = {
        "id": task_id,
        "project_id": project["id"],
        "title": request_text.strip()[:120] or "Delegated work",
        "owner": owner,
        "delivery": "direct_bot",
        "status": "done",
        "estimate_usd": 0.0,
        "reserved_usd": 0.0,
        "spent_usd": spend,
        "created_at": _now().isoformat(),
        "updated_at": _now().isoformat(),
        "result": answer_text[:1000],
        "artifacts": artifacts,
        "notes": ["Recorded from Miles delegation."],
    }
    state["tasks"].append(task)
    project["task_ids"].append(task_id)
    if spend:
        state["company"]["spent_today_usd"] = _money(state["company"]["spent_today_usd"] + spend)
    if artifacts:
        project.setdefault("artifacts", []).extend(artifacts)
    add_event(state, "delegation", f"{owner} handled delegated work: {task['title']}",
              project_id=project["id"], task_id=task_id, amount_usd=spend or None)
    save_state(state, path)
    return task_id


def mark_project_published(path=COMPANY_STATE_FILE):
    """Mark the active project as published after the user approves the publish
    package. This is the supervised gate - the actual upload to Gumroad is done by
    the user (no upload API exists)."""
    state = load_state(path)
    project = active_project(state)
    if not project:
        return "No active project to publish."
    project["status"] = "published"
    add_event(state, "project_published", f"Approved publishing: {project['title']}", project_id=project["id"])
    save_state(state, path)
    return f"Marked '{project['title']}' as published. Nice work shipping it."


def record_adhoc_spend(spent_usd, artifacts=None, path=COMPANY_STATE_FILE):
    """Count real spend from an ad-hoc chat turn (e.g. delegating to Miles in the
    group) against today's budget, and attach any produced artifacts to the active
    project. Keeps the daily ledger honest for work that happens outside the
    autonomous engine. A no-op when there's nothing to record."""
    spend = _money(spent_usd) if spent_usd else 0.0
    artifacts = list(artifacts or [])
    if not spend and not artifacts:
        return

    state = load_state(path)
    if spend:
        state["company"]["spent_today_usd"] = _money(state["company"]["spent_today_usd"] + spend)
    project = active_project(state)
    if project and artifacts:
        project.setdefault("artifacts", []).extend(artifacts)
    add_event(state, "adhoc_spend", "Ad-hoc chat spend.", amount_usd=spend or None)
    save_state(state, path)


def active_project(state):
    project_id = state["company"].get("active_project_id")
    for project in state.get("projects", []):
        if project["id"] == project_id:
            return project
    return None


def project_tasks(state, project_id):
    return [task for task in state.get("tasks", []) if task.get("project_id") == project_id]


def prior_work_summary(state, project_id, current_task_id, limit_chars=1500):
    """A compact summary of what earlier tasks in this project already produced, so
    the next agent can build on it instead of duplicating it. Includes each completed
    task's owner, title, result (truncated), and any deliverables. Excludes the task
    being run now. Returns "" when nothing is done yet."""
    lines = []
    for task in project_tasks(state, project_id):
        if task["id"] == current_task_id or task["status"] not in {"done", "shipped"}:
            continue
        result = (task.get("result") or "").strip()
        if len(result) > limit_chars:
            result = result[:limit_chars] + " ...[truncated]"
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
    if prior_work:
        prompt += (
            "\nYour teammates have ALREADY completed earlier steps on this project. Build "
            "on their work - do NOT redo it or produce a duplicate:\n"
            f"{prior_work}\n"
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
    prompt += (
        "\nIf you produce a file or open a pull request, do it now with your tools - that "
        "saved output is recorded as this task's deliverable. Focus only on YOUR task, keep "
        "it tight and in scope, and don't repeat what a teammate already delivered."
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
        return "\n".join(lines)

    tasks = project_tasks(state, project["id"])
    open_tasks = [task for task in tasks if task["status"] not in {"done", "shipped", "cancelled"}]
    lines.append(f"Active project: {project['title']} ({project['id']}) - {project['status']}")
    lines.append(f"Open tasks: {len(open_tasks)}/{len(tasks)}")
    for task in open_tasks[:6]:
        suffix = " via Miles" if task["delivery"] == "via_miles" else ""
        lines.append(f"- {task['id']} [{task['status']}] {task['owner']}{suffix}: {task['title']} (${task['estimate_usd']:.2f})")

    artifacts = [a for task in tasks for a in task.get("artifacts", [])]
    if artifacts:
        lines.append(f"Artifacts so far: {len(artifacts)} (see /dailyreport)")
    if project["status"] == "proposed":
        lines.append("Reply /approve to start the work plan, or /cancel to drop it.")
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
        return cancel_project(path)
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

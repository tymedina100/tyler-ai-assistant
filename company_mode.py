import json
import os
import re
import uuid
from copy import deepcopy
from datetime import datetime
from pathlib import Path


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


def _fire(hook, *args):
    """Call a sync hook if set, never letting a hook error break a state operation."""
    if hook is None:
        return
    try:
        hook(*args)
    except Exception:  # noqa: BLE001 - a tracker glitch must not corrupt company state
        pass


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
        "products": [],
    }


def normalize_state(state):
    base = new_state()
    if not isinstance(state, dict):
        return base

    normalized = deepcopy(base)
    normalized.update({k: state.get(k, normalized[k]) for k in ("projects", "tasks", "events", "products")})
    normalized["company"].update(state.get("company", {}))

    for task in normalized["tasks"]:
        if isinstance(task, dict) and task.get("owner") in LEGACY_OWNER_MAP:
            task["owner"] = LEGACY_OWNER_MAP[task["owner"]]

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


def _new_task(project_id, owner, delivery, title, estimate=DEFAULT_TASK_ESTIMATE_USD):
    return {
        "id": f"task_{uuid.uuid4().hex[:8]}",
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
        # Linear mirror (populated by the company_linear bridge once the project is
        # approved). Empty when Linear isn't configured or the task isn't mirrored yet.
        "linear_issue_id": "",
        "linear_identifier": "",
        "linear_url": "",
    }


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

    existing = active_project(state)
    if existing and existing["status"] in {"proposed", "active"}:
        return (
            f"Blocked: '{existing['title']}' ({existing['id']}) is still {existing['status']}. "
            f"/approve or /cancel it before assigning something new - otherwise its reserved "
            f"budget would be orphaned."
        )

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
        task = _new_task(project_id, owner, delivery, title, estimate)
        state["tasks"].append(task)
        project["task_ids"].append(task["id"])

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
            _fire(on_task_status_change, task_id, status, previous_status)
            return f"{task_id} updated to {status}."
    return f"Task not found: {task_id}"


def set_task_linear(task_id, issue_id, identifier, url, path=COMPANY_STATE_FILE):
    """Persist a mirrored Linear issue's ids onto a task. Used by the company_linear
    bridge after it creates the issue. Does NOT fire hooks (avoids re-entrancy)."""
    state = load_state(path)
    for task in state["tasks"]:
        if task["id"] == task_id:
            task["linear_issue_id"] = issue_id or ""
            task["linear_identifier"] = identifier or ""
            task["linear_url"] = url or ""
            save_state(state, path)
            return True
    return False


def set_project_source_issue(project_id, issue, path=COMPANY_STATE_FILE):
    """Tag a project with the Linear issue it was created from (via /linear do). The
    company_linear bridge then tracks THAT issue as the project's umbrella - moving it
    to In Progress on start and Done/revisions on completion - instead of creating a
    new issue per task. `issue` is a dict with id/identifier/url."""
    state = load_state(path)
    for project in state["projects"]:
        if project["id"] == project_id:
            project["source_linear_issue"] = {
                "id": issue.get("id", ""),
                "identifier": issue.get("identifier", ""),
                "url": issue.get("url", ""),
            }
            save_state(state, path)
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
    # Now the project is really running: mirror its tasks into the tracker (Linear).
    _fire(on_project_activated, project["id"])
    return f"Approved: {project['title']}. Starting the work plan now.", project["id"]


def cancel_project(path=COMPANY_STATE_FILE, project_id=None):
    """Cancel a project and release any budget still reserved for its open tasks.
    Defaults to the active project; pass project_id to reach one that's no longer
    tracked as active (e.g. an older project a later /assign superseded without
    ever being approved or cancelled - see open_projects)."""
    state = load_state(path)
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
        if task["status"] in {"planned", "in_progress", "blocked"} and task["reserved_usd"]:
            released += task["reserved_usd"]
            task["reserved_usd"] = 0.0
            task["status"] = "cancelled"

    project["status"] = "cancelled"
    state["company"]["reserved_today_usd"] = _money(max(0.0, state["company"]["reserved_today_usd"] - released))
    if state["company"].get("active_project_id") == project["id"]:
        state["company"]["active_project_id"] = None
    add_event(state, "project_cancelled", f"Cancelled project: {project['title']}",
              project_id=project["id"], amount_usd=released)
    save_state(state, path)
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


def set_project_revision_flag(project_id, editor_answer, path=COMPANY_STATE_FILE):
    """Record whether the Managing Editor approved a project's deliverables. Looked
    for explicitly (an "APPROVED" prefix) rather than assumed, so a missing or
    garbled verdict is treated as NOT approved rather than silently passing. Keeps
    her full feedback (unlike the 1000-char task result) so a revision round can
    hand her exact required changes back to the agents who need to act on them."""
    state = load_state(path)
    approved = editor_answer.strip().upper().startswith("APPROVED")
    for project in state["projects"]:
        if project["id"] == project_id:
            project["needs_revision"] = not approved
            project["last_editor_feedback"] = editor_answer.strip()
            break
    save_state(state, path)


def start_revision_round(project_id, configured_agent_keys, path=COMPANY_STATE_FILE):
    """Queue another pass at a project the Managing Editor rejected: one task per
    original non-editor owner (in their first-appearance order) to address her
    feedback, followed by a fresh editor re-review. Called in a loop by the
    runner until she approves or the budget can't cover another round. Returns
    (created, message) - created=False means the caller should complete the
    project as-is instead of looping."""
    state = load_state(path)
    project = next((p for p in state["projects"] if p["id"] == project_id), None)
    if not project:
        return False, "Project not found."

    owners_in_order = []
    for task in project_tasks(state, project_id):
        if task["owner"] != "editor" and task["owner"] not in owners_in_order:
            owners_in_order.append(task["owner"])
    if not owners_in_order:
        return False, "No revisable owners found in this project."

    round_number = project.get("revision_round", 0) + 1
    specs = [
        (owner, f"Revision round {round_number}: address the Managing Editor's required "
                f"changes in your part of the shared deliverable.")
        for owner in owners_in_order
    ]
    specs.append((
        "editor",
        f"Re-review the round {round_number} revisions against the original goal; approve, "
        f"or list any remaining required changes.",
    ))

    total_estimate = DEFAULT_TASK_ESTIMATE_USD * len(specs)
    if remaining_budget(state) < total_estimate:
        return False, (
            f"not enough budget left for another revision round "
            f"(needs ~${total_estimate:.2f}, ${remaining_budget(state):.2f} remaining)"
        )

    for preferred_owner, title in specs:
        owner, delivery = _owner_for(preferred_owner, configured_agent_keys)
        task = _new_task(project_id, owner, delivery, title)
        state["tasks"].append(task)
        project["task_ids"].append(task["id"])

    project["revision_round"] = round_number
    state["company"]["reserved_today_usd"] = _money(state["company"]["reserved_today_usd"] + total_estimate)
    add_event(state, "revision_round_started", f"Started revision round {round_number} for {project['title']}",
              project_id=project_id, amount_usd=total_estimate)
    save_state(state, path)
    # Mirror the freshly queued revision tasks into the tracker too (idempotent).
    _fire(on_project_activated, project_id)
    return True, f"Editor requires revisions - starting round {round_number} (${total_estimate:.2f} reserved)."


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

    state = load_state(path)
    project = _latest_project(state)
    if not project:
        return "No project to link yet. /assign and build something first."

    for product in state["products"]:
        if product["project_id"] == project["id"]:
            product["gumroad_url"] = gumroad_url
            save_state(state, path)
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
    save_state(state, path)
    return f"Linked '{project['title']}' to {gumroad_url}. Run /revenue to pull its sales."


def sync_revenue(gumroad_products, path=COMPANY_STATE_FILE):
    """Update the product registry from a list of Gumroad products (dicts with
    short_url, id, sales_count, sales_usd_cents). Matches on the linked short_url.
    Pure - the caller fetches from Gumroad and passes the data in."""
    state = load_state(path)
    by_url = {p.get("short_url", "").rstrip("/"): p for p in (gumroad_products or []) if p.get("short_url")}
    now = _now().isoformat()

    for product in state["products"]:
        match = by_url.get((product.get("gumroad_url") or "").rstrip("/"))
        if not match:
            continue
        product["gumroad_product_id"] = match.get("id")
        product["sales_count"] = match.get("sales_count", 0) or 0
        product["revenue_usd"] = _money((match.get("sales_usd_cents", 0) or 0) / 100.0)
        product["last_synced"] = now

    save_state(state, path)
    return state


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

    The editor's own task result is stored truncated to 1000 chars (see
    _run_one_task), which can cut off her numbered revision list - so for her task
    specifically, this pulls the untruncated last_editor_feedback the project keeps
    instead, with a larger cap, so a revision round gets her complete feedback."""
    project = next((p for p in state.get("projects", []) if p["id"] == project_id), None)
    lines = []
    for task in project_tasks(state, project_id):
        if task["id"] == current_task_id or task["status"] not in {"done", "shipped"}:
            continue
        if task["owner"] == "editor" and project and project.get("last_editor_feedback"):
            result, block_limit = project["last_editor_feedback"], max(limit_chars, 3000)
        else:
            result, block_limit = (task.get("result") or "").strip(), limit_chars
        if len(result) > block_limit:
            result = result[:block_limit] + " ...[truncated]"
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

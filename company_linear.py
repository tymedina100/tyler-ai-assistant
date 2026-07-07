"""Bridge between Company Mode and Linear.

Company Mode (company_mode.py) is a pure, offline state machine. This module is the
thin layer that mirrors its work into Linear so Linear becomes the company's live
project tracker:

- When a project is approved to run, each of its tasks becomes a Linear issue
  (one issue per task).
- As the engine works a task, the issue's workflow state follows along
  (Todo -> In Progress -> Done), and the agent's result / PR link is posted as a
  comment when the task finishes.

It plugs in via company_mode's optional hooks (see register()), so company_mode
never imports the network layer and stays fully testable. Everything here is a
safe no-op when Linear isn't configured (LINEAR_API_KEY unset), and every failure
is swallowed with a log line - a tracker glitch must never stop the company engine.

Issue creation happens at /approve (an explicit user action), consistent with the
assistant's "writes need explicit intent" rule.
"""
import logging

import company_mode
import linear_helpers

logger = logging.getLogger("assistant")

# Company task status -> Linear workflow state name. Names follow Linear's default
# workflow; update_issue_status resolves them per-team and fails soft if a team
# renamed or lacks one, so a non-standard workflow just skips the state move.
_STATUS_TO_LINEAR_STATE = {
    "in_progress": "In Progress",
    "done": "Done",
    "shipped": "Done",
    "cancelled": "Canceled",
    # "blocked" has no universal state; we comment instead (see sync_task_status).
}


def is_enabled():
    return linear_helpers.is_configured()


def _issue_body(project, task):
    """Description for a task's mirror issue: what it is, its goal, and its owner."""
    return (
        f"{task['title']}\n\n"
        f"Owner (agent): {task['owner']}\n"
        f"Company goal: {project.get('goal', '')}\n"
        f"Company project: {project.get('title', '')} ({project['id']})\n"
        f"Company task id: {task['id']}"
    )


def _active_project_key():
    """The selected assistant project key, if any, so mirror issues can be tagged to
    the right repo/Linear project. Imported lazily to avoid load-time coupling."""
    try:
        import projects
        key, _ = projects.get_active_project()
        return key
    except Exception:
        return None


def ensure_issues_for_project(project_id, path=company_mode.COMPANY_STATE_FILE):
    """Create a Linear issue for each of the project's tasks that isn't mirrored yet,
    and persist the issue ids back onto the tasks. Idempotent - skips tasks that
    already have a linear_issue_id, and cancelled tasks. Returns a list of
    (identifier, url) for the issues created this call (empty if none / not enabled)."""
    if not is_enabled():
        return []

    state = company_mode.load_state(path)
    project = next((p for p in state.get("projects", []) if p["id"] == project_id), None)
    if not project:
        return []

    # A /linear do project tracks an EXISTING source issue as its umbrella instead of
    # creating one issue per task. Move that issue to In Progress and skip per-task
    # mirroring entirely (avoids cluttering Linear with sub-issues for one issue's work).
    source = project.get("source_linear_issue")
    if source and source.get("id"):
        _, err = _safe(linear_helpers.update_issue_status, source["id"], "In Progress")
        if err:
            logger.info(f"company_linear: source issue In Progress skipped: {err}")
        _safe(linear_helpers.add_comment, source["id"],
              f"🏭 Company project {project_id} started working this issue.")
        return []

    project_key = _active_project_key()
    created = []
    for task in company_mode.project_tasks(state, project_id):
        if task.get("linear_issue_id") or task["status"] == "cancelled":
            continue

        title = f"{task['owner']}: {task['title']}"
        body = _issue_body(project, task)
        try:
            if project_key:
                issue, err = linear_helpers.create_project_issue(project_key, title, body)
            else:
                issue, err = linear_helpers.create_issue(title, body)
        except Exception as e:  # noqa: BLE001
            logger.error(f"company_linear: create issue failed for {task['id']}: {e}")
            continue

        if err or not issue:
            logger.error(f"company_linear: create issue error for {task['id']}: {err}")
            continue

        company_mode.set_task_linear(
            task["id"], issue.get("id", ""), issue.get("identifier", ""), issue.get("url", ""), path
        )
        created.append((issue.get("identifier", "?"), issue.get("url", "")))

    return created


def sync_task_status(task_id, status, previous_status, path=company_mode.COMPANY_STATE_FILE):
    """Mirror a task's status change onto its Linear issue: move the workflow state and,
    when the task finishes or blocks, post the result/PR link as a comment. Safe no-op
    when Linear isn't enabled or the task isn't mirrored."""
    if not is_enabled():
        return

    state = company_mode.load_state(path)
    task = next((t for t in state.get("tasks", []) if t["id"] == task_id), None)
    if not task or not task.get("linear_issue_id"):
        return

    issue_id = task["linear_issue_id"]

    # Move the workflow state where there's a sensible mapping.
    target_state = _STATUS_TO_LINEAR_STATE.get(status)
    if target_state:
        _, err = _safe(linear_helpers.update_issue_status, issue_id, target_state)
        if err:
            logger.info(f"company_linear: status move to '{target_state}' skipped for {task_id}: {err}")

    # Post a comment on the outcomes worth capturing.
    comment = None
    if status in {"done", "shipped"}:
        comment = _completion_comment(task)
    elif status == "blocked":
        reason = (task.get("result") or "").strip() or "Blocked."
        comment = f"⛔ Blocked: {reason}"

    if comment:
        _, err = _safe(linear_helpers.add_comment, issue_id, comment)
        if err:
            logger.info(f"company_linear: comment failed for {task_id}: {err}")


def finalize_source_issue(project_id, path=company_mode.COMPANY_STATE_FILE):
    """Close out a /linear do project's source issue when the project finishes. If the
    Managing Editor approved (project not flagged needs_revision), move the issue to
    Done with a summary of the deliverables; if revisions are required, leave it In
    Progress and comment the editor's required changes. Safe no-op when disabled or the
    project has no source issue."""
    if not is_enabled():
        return

    state = company_mode.load_state(path)
    project = next((p for p in state.get("projects", []) if p["id"] == project_id), None)
    if not project:
        return
    source = project.get("source_linear_issue")
    if not source or not source.get("id"):
        return
    issue_id = source["id"]

    artifacts = [a for t in company_mode.project_tasks(state, project_id) for a in t.get("artifacts", [])]

    if project.get("needs_revision"):
        feedback = (project.get("last_editor_feedback") or "Revisions required.").strip()
        _safe(linear_helpers.add_comment, issue_id,
              f"📝 Editor requires revisions before this ships:\n\n{feedback[:1500]}")
        return

    parts = ["✅ Company project complete and approved by the editor."]
    if artifacts:
        parts.append("Deliverables:\n" + "\n".join(f"- {a}" for a in artifacts))
    _safe(linear_helpers.add_comment, issue_id, "\n\n".join(parts))
    _, err = _safe(linear_helpers.update_issue_status, issue_id, "Done")
    if err:
        logger.info(f"company_linear: source issue Done skipped: {err}")


def _completion_comment(task):
    parts = ["✅ Completed by the company engine."]
    result = (task.get("result") or "").strip()
    if result:
        parts.append(result[:1500])
    artifacts = task.get("artifacts") or []
    if artifacts:
        parts.append("Deliverables:\n" + "\n".join(f"- {a}" for a in artifacts))
    return "\n\n".join(parts)


def _safe(fn, *args):
    """Call a linear_helpers write, turning any exception into a (None, error) tuple so
    a network blip never propagates into the company engine."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        return None, str(e)


def register():
    """Wire this bridge into company_mode's hooks. Call once at startup."""
    company_mode.on_project_activated = ensure_issues_for_project
    company_mode.on_task_status_change = sync_task_status

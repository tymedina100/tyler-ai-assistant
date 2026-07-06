"""Linear integration for the assistant (used by the Linear specialist and the
/linear commands).

Talks to Linear's GraphQL API directly (https://api.linear.app/graphql) with a
personal API key (LINEAR_API_KEY) - single authenticated HTTPS requests, no SDK,
so it works from the headless container. Config is read at call time (not import
time) so a value from a .env file loaded after import still works.

Every public function returns a (data, error) tuple: (result, None) on success or
(None, "friendly message") on failure. If LINEAR_API_KEY isn't set the functions
return a clear "not configured" message instead of crashing.

Env vars:
  LINEAR_API_KEY             - personal API key (required to do anything)
  LINEAR_TEAM_ID             - default team new issues land in
  LINEAR_WORKSPACE_ID        - optional, informational
  LINEAR_DEFAULT_PROJECT_ID  - optional default Linear project id
Per-assistant-project Linear project ids can be set with
  LINEAR_PROJECT_ID_<KEY>    - e.g. LINEAR_PROJECT_ID_VANTAGE
"""
import os

import requests

API_URL = "https://api.linear.app/graphql"

NOT_CONFIGURED = "Linear isn't configured (set LINEAR_API_KEY)."


def _api_key():
    return os.environ.get("LINEAR_API_KEY", "")


def _default_team_id():
    return os.environ.get("LINEAR_TEAM_ID", "")


def is_configured():
    return bool(_api_key())


def _headers():
    # Linear personal API keys are sent as the raw Authorization header value.
    return {"Authorization": _api_key(), "Content-Type": "application/json"}


def _graphql(query, variables=None):
    """Run a GraphQL request. Returns (data_dict, None) or (None, error_str)."""
    if not is_configured():
        return None, NOT_CONFIGURED
    try:
        resp = requests.post(
            API_URL,
            headers=_headers(),
            json={"query": query, "variables": variables or {}},
            timeout=15,
        )
    except Exception as e:
        return None, f"Couldn't reach Linear ({e})."

    if resp.status_code == 401:
        return None, "Linear rejected the API key (401). Check LINEAR_API_KEY."
    if resp.status_code != 200:
        return None, f"Linear API error {resp.status_code}: {resp.text[:200]}"

    try:
        body = resp.json()
    except ValueError:
        return None, "Linear returned a non-JSON response."

    if body.get("errors"):
        messages = "; ".join(e.get("message", "?") for e in body["errors"])
        return None, f"Linear query failed: {messages}"

    return body.get("data", {}), None


# --------------------------------------------------------------------------- #
# Reads (always safe)
# --------------------------------------------------------------------------- #

def list_teams():
    """Return (list_of_{id,name,key}, None) or (None, error)."""
    data, err = _graphql("{ teams { nodes { id name key } } }")
    if err:
        return None, err
    return data.get("teams", {}).get("nodes", []), None


def list_projects():
    """Return (list_of_{id,name,state}, None) or (None, error)."""
    data, err = _graphql("{ projects { nodes { id name state } } }")
    if err:
        return None, err
    return data.get("projects", {}).get("nodes", []), None


def list_issues(limit=20):
    """Return (list_of recent issues, None) or (None, error)."""
    query = """
    query Recent($first: Int!) {
      issues(first: $first, orderBy: updatedAt) {
        nodes { id identifier title url state { name } }
      }
    }
    """
    data, err = _graphql(query, {"first": limit})
    if err:
        return None, err
    return data.get("issues", {}).get("nodes", []), None


def search_issues(query, limit=20):
    """Full-text search issues by text. Returns (list, None) or (None, error)."""
    gql = """
    query Search($term: String!, $first: Int!) {
      issueSearch(query: $term, first: $first) {
        nodes { id identifier title url state { name } }
      }
    }
    """
    data, err = _graphql(gql, {"term": query, "first": limit})
    if err:
        return None, err
    return data.get("issueSearch", {}).get("nodes", []), None


# --------------------------------------------------------------------------- #
# Writes (explicit intent only)
# --------------------------------------------------------------------------- #

def create_issue(title, description="", team_id=None, project_id=None, labels=None):
    """Create a Linear issue. Returns ({id,identifier,title,url}, None) or
    (None, error). Falls back to LINEAR_TEAM_ID when team_id is omitted."""
    if not is_configured():
        return None, NOT_CONFIGURED
    team = team_id or _default_team_id()
    if not team:
        return None, "No Linear team to create the issue in (set LINEAR_TEAM_ID or pass team_id)."

    issue_input = {"title": title, "teamId": team}
    if description:
        issue_input["description"] = description
    project = project_id or os.environ.get("LINEAR_DEFAULT_PROJECT_ID", "")
    if project:
        issue_input["projectId"] = project
    if labels:  # only if caller resolved real label ids
        issue_input["labelIds"] = labels

    mutation = """
    mutation Create($input: IssueCreateInput!) {
      issueCreate(input: $input) {
        success
        issue { id identifier title url }
      }
    }
    """
    data, err = _graphql(mutation, {"input": issue_input})
    if err:
        return None, err
    result = data.get("issueCreate", {})
    if not result.get("success"):
        return None, "Linear reported the issue wasn't created."
    return result.get("issue", {}), None


def _project_footer(project_key):
    """Standard description footer tying the issue to an assistant project."""
    import projects  # local import to avoid a circular import at module load

    profile = projects.get_project(project_key) or {}
    repo = profile.get("repo", "unknown")
    return (
        "\n\n---\n"
        "Created by Tyler AI Assistant\n"
        f"Project key: {project_key}\n"
        f"GitHub repo: {repo}"
    )


def create_project_issue(project_key, title, description="", labels=None):
    """Create an issue tied to an assistant project. Uses a per-project Linear
    project id if configured (LINEAR_PROJECT_ID_<KEY> or LINEAR_DEFAULT_PROJECT_ID);
    otherwise creates in the default team and tags the description with the project
    key + repo footer so it's still traceable."""
    env_key = f"LINEAR_PROJECT_ID_{project_key.upper().replace('-', '_')}"
    project_id = os.environ.get(env_key, "") or os.environ.get("LINEAR_DEFAULT_PROJECT_ID", "")
    body = (description or "") + _project_footer(project_key)
    return create_issue(title, description=body, project_id=project_id or None, labels=labels)


def _workflow_state_id(team_id, state_name):
    """Resolve a workflow state name (e.g. 'In Progress') to its id for a team."""
    query = """
    query States($teamId: String!) {
      team(id: $teamId) { states { nodes { id name } } }
    }
    """
    data, err = _graphql(query, {"teamId": team_id})
    if err:
        return None, err
    states = data.get("team", {}).get("states", {}).get("nodes", [])
    for state in states:
        if state["name"].lower() == state_name.lower():
            return state["id"], None
    names = ", ".join(s["name"] for s in states) or "(none)"
    return None, f"No workflow state named '{state_name}'. Available: {names}."


def update_issue_status(issue_id, state_name, team_id=None):
    """Move an issue to a workflow state by name. Returns (issue, None) or
    (None, error). Low-risk; exposed via explicit tools only."""
    team = team_id or _default_team_id()
    if not team:
        return None, "Need a team to resolve the workflow state (set LINEAR_TEAM_ID)."
    state_id, err = _workflow_state_id(team, state_name)
    if err:
        return None, err
    mutation = """
    mutation Update($id: String!, $stateId: String!) {
      issueUpdate(id: $id, input: { stateId: $stateId }) {
        success
        issue { id identifier title url state { name } }
      }
    }
    """
    data, err = _graphql(mutation, {"id": issue_id, "stateId": state_id})
    if err:
        return None, err
    result = data.get("issueUpdate", {})
    if not result.get("success"):
        return None, "Linear reported the update didn't apply."
    return result.get("issue", {}), None


def add_comment(issue_id, body):
    """Add a comment to an issue. Returns ({id}, None) or (None, error)."""
    mutation = """
    mutation Comment($input: CommentCreateInput!) {
      commentCreate(input: $input) {
        success
        comment { id url }
      }
    }
    """
    data, err = _graphql(mutation, {"input": {"issueId": issue_id, "body": body}})
    if err:
        return None, err
    result = data.get("commentCreate", {})
    if not result.get("success"):
        return None, "Linear reported the comment wasn't added."
    return result.get("comment", {}), None

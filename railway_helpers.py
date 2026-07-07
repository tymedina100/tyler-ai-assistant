"""Railway integration for the assistant (used by Patch).

Lets the company inspect and (with confirmation) change its Railway infrastructure:
list services, read env-var names, check deployment status/domains, and - gated behind
/confirm at the tool layer - set a variable or trigger a redeploy. Talks to Railway's
public GraphQL API directly with a token (RAILWAY_TOKEN) - single authenticated HTTPS
requests, no CLI. Config is read at call time (not import time) so a value from a .env
loaded after import still works.

Every public function returns a (data, error) tuple: (result, None) on success or
(None, "friendly message") on failure. If RAILWAY_TOKEN isn't set the functions return
a clear "not configured" message instead of crashing.

SECRETS: Railway variables often hold secrets (DB URLs, API keys). `list_variables`
returns NAMES only. `get_variable` returns one value on purpose - call it only for a
specific var you need to check (e.g. a non-secret EXPO_PUBLIC_* var), never to dump
everything.

Env vars:
  RAILWAY_TOKEN  - Railway account/team API token (Account Settings -> Tokens)
"""
import os

import requests

API_URL = "https://backboard.railway.app/graphql/v2"

NOT_CONFIGURED = "Railway isn't configured (set RAILWAY_TOKEN)."


def _token():
    return os.environ.get("RAILWAY_TOKEN", "")


def is_configured():
    return bool(_token())


def _headers():
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


def _graphql(query, variables=None):
    """Run a Railway GraphQL request. Returns (data_dict, None) or (None, error_str)."""
    if not is_configured():
        return None, NOT_CONFIGURED
    try:
        resp = requests.post(
            API_URL, headers=_headers(),
            json={"query": query, "variables": variables or {}}, timeout=20,
        )
    except Exception as e:
        return None, f"Couldn't reach Railway ({e})."

    if resp.status_code == 401:
        return None, "Railway rejected the token (401). Check RAILWAY_TOKEN."
    if resp.status_code != 200:
        return None, f"Railway API error {resp.status_code}: {resp.text[:200]}"

    try:
        body = resp.json()
    except ValueError:
        return None, "Railway returned a non-JSON response."

    if body.get("errors"):
        messages = "; ".join(e.get("message", "?") for e in body["errors"])
        return None, f"Railway query failed: {messages}"
    return body.get("data", {}), None


def _nodes(connection):
    """Flatten a Relay-style {edges: [{node: {...}}]} connection to a list of nodes."""
    if not isinstance(connection, dict):
        return []
    return [edge.get("node", {}) for edge in connection.get("edges", []) if isinstance(edge, dict)]


# --------------------------------------------------------------------------- #
# Reads (safe)
# --------------------------------------------------------------------------- #

def list_projects():
    """Return (list of {id,name}, None) or (None, error)."""
    data, err = _graphql("{ projects { edges { node { id name } } } }")
    if err:
        return None, err
    return _nodes(data.get("projects")), None


def get_project(project_id):
    """Return ({id, name, services:[...], environments:[...]}, None) or (None, error)."""
    query = """
    query Project($id: String!) {
      project(id: $id) {
        id
        name
        services { edges { node { id name } } }
        environments { edges { node { id name } } }
      }
    }
    """
    data, err = _graphql(query, {"id": project_id})
    if err:
        return None, err
    project = data.get("project")
    if not project:
        return None, f"Railway project not found: {project_id}"
    return {
        "id": project.get("id"),
        "name": project.get("name"),
        "services": _nodes(project.get("services")),
        "environments": _nodes(project.get("environments")),
    }, None


def _variables(project_id, environment_id, service_id):
    query = """
    query Vars($projectId: String!, $environmentId: String!, $serviceId: String) {
      variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId)
    }
    """
    data, err = _graphql(query, {
        "projectId": project_id, "environmentId": environment_id, "serviceId": service_id,
    })
    if err:
        return None, err
    return data.get("variables") or {}, None


def list_variables(project_id, environment_id, service_id=None):
    """Return (sorted list of variable NAMES, None) or (None, error). Values are never
    returned here - use get_variable for one specific value."""
    variables, err = _variables(project_id, environment_id, service_id)
    if err:
        return None, err
    return sorted(variables.keys()), None


def get_variable(project_id, environment_id, name, service_id=None):
    """Return (value_str, None) for one variable, or (None, error). Returns a real value
    on purpose - only call it for a specific var you need to verify."""
    variables, err = _variables(project_id, environment_id, service_id)
    if err:
        return None, err
    if name not in variables:
        return None, f"Variable '{name}' not found in that service/environment."
    return variables[name], None


def latest_deployment(project_id, environment_id, service_id):
    """Return ({id, status, url, createdAt}, None) for the most recent deployment."""
    query = """
    query Deploys($input: DeploymentListInput!) {
      deployments(input: $input, first: 1) {
        edges { node { id status createdAt staticUrl url } }
      }
    }
    """
    data, err = _graphql(query, {"input": {
        "projectId": project_id, "environmentId": environment_id, "serviceId": service_id,
    }})
    if err:
        return None, err
    nodes = _nodes(data.get("deployments"))
    if not nodes:
        return None, "No deployments found for that service/environment."
    node = nodes[0]
    url = node.get("staticUrl") or node.get("url") or ""
    return {
        "id": node.get("id", ""),
        "status": node.get("status", "UNKNOWN"),
        "url": f"https://{url}" if url and not url.startswith("http") else url,
        "createdAt": node.get("createdAt", ""),
    }, None


# --------------------------------------------------------------------------- #
# Writes (gated behind /confirm at the tool layer)
# --------------------------------------------------------------------------- #

def set_variable(project_id, environment_id, service_id, name, value):
    """Upsert a Railway variable. Returns (True, None) or (None, error)."""
    mutation = """
    mutation Upsert($input: VariableUpsertInput!) {
      variableUpsert(input: $input)
    }
    """
    data, err = _graphql(mutation, {"input": {
        "projectId": project_id, "environmentId": environment_id,
        "serviceId": service_id, "name": name, "value": value,
    }})
    if err:
        return None, err
    return bool(data.get("variableUpsert", True)), None


def redeploy(service_id, environment_id):
    """Trigger a redeploy of a service in an environment. Returns (True, None) or
    (None, error)."""
    mutation = """
    mutation Redeploy($serviceId: String!, $environmentId: String!) {
      serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId)
    }
    """
    data, err = _graphql(mutation, {"serviceId": service_id, "environmentId": environment_id})
    if err:
        return None, err
    return bool(data.get("serviceInstanceRedeploy", True)), None

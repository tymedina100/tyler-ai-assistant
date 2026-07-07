"""Vercel deploy integration for the assistant (used by Patch and Sway).

Lets the company put a landing page LIVE: trigger a Vercel deployment of a Git-linked
project's branch and get back a real URL. Talks to the Vercel REST API directly with a
token (VERCEL_TOKEN) - single authenticated HTTPS requests, no CLI, so it works from the
headless container. Config is read at call time (not import time) so a value from a .env
loaded after import still works.

Every public function returns a (data, error) tuple: (result, None) on success or
(None, "friendly message") on failure. If VERCEL_TOKEN isn't set the functions return a
clear "not configured" message instead of crashing.

Env vars:
  VERCEL_TOKEN    - personal access token (required)
  VERCEL_TEAM_ID  - optional; scope requests to a team (else your personal scope)

Deploying is outward-facing: PREVIEW deploys are throwaway URLs (safe to run freely);
PRODUCTION deploys touch the live domain and are gated behind /confirm in main.execute_tool.
This module just performs whatever it's asked - the gate lives at the tool layer.
"""
import os

import requests

API_ROOT = "https://api.vercel.com"

NOT_CONFIGURED = "Vercel isn't configured (set VERCEL_TOKEN)."


def _token():
    return os.environ.get("VERCEL_TOKEN", "")


def _team_id():
    return os.environ.get("VERCEL_TEAM_ID", "")


def is_configured():
    return bool(_token())


def _headers():
    return {"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"}


def _team_params(extra=None):
    params = dict(extra or {})
    team = _team_id()
    if team:
        params["teamId"] = team
    return params


def _request(method, path, json_body=None, params=None):
    """One Vercel API call. Returns (parsed_json, None) or (None, error_str)."""
    if not is_configured():
        return None, NOT_CONFIGURED
    try:
        resp = requests.request(
            method, f"{API_ROOT}{path}",
            headers=_headers(), json=json_body, params=_team_params(params), timeout=20,
        )
    except Exception as e:
        return None, f"Couldn't reach Vercel ({e})."

    if resp.status_code == 401:
        return None, "Vercel rejected the token (401). Check VERCEL_TOKEN."
    if resp.status_code == 403:
        return None, "Vercel denied access (403). Check the token scope / VERCEL_TEAM_ID."
    if resp.status_code == 404:
        return None, "Vercel resource not found (404)."
    if resp.status_code not in (200, 201, 202):
        return None, f"Vercel API error {resp.status_code}: {resp.text[:200]}"

    try:
        return resp.json(), None
    except ValueError:
        return None, "Vercel returned a non-JSON response."


# --------------------------------------------------------------------------- #
# Reads (safe)
# --------------------------------------------------------------------------- #

def list_projects(limit=50):
    """Return (list of {id,name,link}, None) or (None, error)."""
    data, err = _request("GET", "/v9/projects", params={"limit": limit})
    if err:
        return None, err
    return data.get("projects", []), None


def get_project(project):
    """Fetch one project by name or id. Returns (project_dict, None) or (None, error)."""
    return _request("GET", f"/v9/projects/{project}")


def deployment_status(deployment_id):
    """Return ({readyState, url}, None) for a deployment, or (None, error)."""
    data, err = _request("GET", f"/v13/deployments/{deployment_id}")
    if err:
        return None, err
    url = data.get("url", "")
    return {
        "id": data.get("id", deployment_id),
        "readyState": data.get("readyState") or data.get("status") or "UNKNOWN",
        "url": f"https://{url}" if url else "",
    }, None


# --------------------------------------------------------------------------- #
# Deploy (preview is free; production is gated at the tool layer)
# --------------------------------------------------------------------------- #

def deploy(project, ref="main", target="preview"):
    """Trigger a deployment of `project`'s `ref` branch. `target` is "preview" or
    "production". Returns ({url, id, readyState, target}, None) or (None, error).

    The Vercel project must be linked to a Git repo (we deploy from that repo's branch).
    """
    if not is_configured():
        return None, NOT_CONFIGURED

    ref = (ref or "main").strip() or "main"
    target = "production" if str(target).lower() == "production" else "preview"

    project_data, err = get_project(project)
    if err:
        return None, f"Couldn't load Vercel project '{project}': {err}"

    link = project_data.get("link") or {}
    repo_id = link.get("repoId")
    link_type = link.get("type")
    if not repo_id or not link_type:
        return None, (
            f"Vercel project '{project}' isn't connected to a Git repo, so there's "
            f"nothing to deploy from. Link it to the GitHub repo in the Vercel dashboard first."
        )

    body = {
        "name": project_data.get("name", project),
        "project": project_data.get("id", project),
        "gitSource": {"type": link_type, "repoId": repo_id, "ref": ref},
    }
    if target == "production":
        body["target"] = "production"

    data, err = _request("POST", "/v13/deployments", json_body=body)
    if err:
        return None, f"Vercel deploy failed: {err}"

    url = data.get("url", "")
    return {
        "url": f"https://{url}" if url else "",
        "id": data.get("id", ""),
        "readyState": data.get("readyState") or "QUEUED",
        "target": target,
    }, None

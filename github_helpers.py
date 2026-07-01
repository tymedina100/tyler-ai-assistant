"""Optional GitHub mirroring for files the assistant writes (Patch, Quill, Sage).

If GITHUB_TOKEN and GITHUB_REPO are set, push_file() commits a copy of each saved
file to that repo via the GitHub REST "contents" API - a single authenticated HTTPS
request, no git binary needed, so it works from the headless container. The upshot:
files survive redeploys and are reachable from any machine (git pull, or github.com),
which is how a cloud-run bot can get code onto your PC.

If the env vars aren't set, push_file() is a no-op (returns None) and the assistant
just writes locally as before. Config is read at call time (not import time) so it
still works when the values come from a .env file loaded after import.
"""
import base64
import os

import requests

API_ROOT = "https://api.github.com"


def _config():
    return (
        os.environ.get("GITHUB_TOKEN", ""),
        os.environ.get("GITHUB_REPO", ""),      # "owner/repo"
        os.environ.get("GITHUB_BRANCH", "main"),
    )


def is_configured():
    token, repo, _ = _config()
    return bool(token and repo)


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def push_file(filename, content):
    """Commit `content` to GITHUB_REPO at path `filename`. Returns a short status
    string (including the file's URL) on success, an error note on failure, or None
    if GitHub mirroring isn't configured."""
    token, repo, branch = _config()

    if not (token and repo):
        return None

    url = f"{API_ROOT}/repos/{repo}/contents/{filename}"
    headers = _headers(token)

    try:
        # Updating an existing file requires its current blob SHA; creating a new one
        # must omit it. So look it up first (404 = new file).
        get_resp = requests.get(url, headers=headers, params={"ref": branch}, timeout=10)
        sha = get_resp.json().get("sha") if get_resp.status_code == 200 else None

        payload = {
            "message": f"Update {filename}" if sha else f"Add {filename}",
            "content": base64.b64encode(content.encode("utf-8")).decode(),
            "branch": branch,
        }
        if sha:
            payload["sha"] = sha

        put_resp = requests.put(url, headers=headers, json=payload, timeout=10)

        if put_resp.status_code in (200, 201):
            html_url = put_resp.json().get("content", {}).get("html_url", f"{repo}/{filename}")
            return f"Also pushed to GitHub: {html_url}"

        return f"(Saved locally, but the GitHub push failed: {put_resp.status_code} {put_resp.text[:200]})"

    except Exception as e:
        return f"(Saved locally, but the GitHub push failed: {e})"

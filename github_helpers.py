"""GitHub repo access for the assistant (used by Patch, and by write_file mirroring).

Two roles:
- push_file() mirrors every write_file save to the repo (see main.write_file).
- Patch also gets first-class tools (list/read/save/delete) so it can work on the
  repo like a developer - see main.py's github_* tools.

Everything goes through the GitHub REST "contents" API with a Personal Access Token
(GITHUB_TOKEN) against GITHUB_REPO ("owner/repo") on GITHUB_BRANCH (default "main") -
single authenticated HTTPS requests, no git binary, so it works from the headless
container. Config is read at call time (not import time) so a value from a .env file
loaded after import still works. If GITHUB_TOKEN/GITHUB_REPO aren't set, push_file()
is a silent no-op and the explicit tools return a friendly "not configured" message.
"""
import base64
import os

import requests

API_ROOT = "https://api.github.com"

NOT_CONFIGURED = "GitHub isn't configured (set GITHUB_TOKEN and GITHUB_REPO)."


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


def _content_url(repo, path):
    return f"{API_ROOT}/repos/{repo}/contents/{path}"


def _commit(path, content, token, repo, branch):
    """Create or update a file. Returns (html_url, None) on success or
    (None, error_str) on failure. Assumes config is already validated."""
    url = _content_url(repo, path)
    headers = _headers(token)

    # Updating an existing file needs its current blob SHA; creating omits it.
    get_resp = requests.get(url, headers=headers, params={"ref": branch}, timeout=10)
    sha = get_resp.json().get("sha") if get_resp.status_code == 200 else None

    payload = {
        "message": f"Update {path}" if sha else f"Add {path}",
        "content": base64.b64encode(content.encode("utf-8")).decode(),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    put_resp = requests.put(url, headers=headers, json=payload, timeout=10)
    if put_resp.status_code in (200, 201):
        return put_resp.json().get("content", {}).get("html_url", f"{repo}/{path}"), None
    return None, f"{put_resp.status_code} {put_resp.text[:200]}"


def push_file(filename, content):
    """Mirror a locally-saved file to the repo. Returns a short status string (with
    the file URL), an error note, or None if GitHub mirroring isn't configured."""
    token, repo, branch = _config()
    if not (token and repo):
        return None

    try:
        html_url, err = _commit(filename, content, token, repo, branch)
    except Exception as e:
        return f"(Saved locally, but the GitHub push failed: {e})"

    if html_url:
        return f"Also pushed to GitHub: {html_url}"
    return f"(Saved locally, but the GitHub push failed: {err})"


def save_file(path, content):
    """Explicit tool: create/update a file directly in the repo at `path`."""
    token, repo, branch = _config()
    if not (token and repo):
        return NOT_CONFIGURED

    try:
        html_url, err = _commit(path, content, token, repo, branch)
    except Exception as e:
        return f"Sorry, couldn't save {path} to GitHub ({e})."

    if html_url:
        return f"Saved {path} to GitHub: {html_url}"
    return f"Couldn't save {path}: {err}"


def list_files(path=""):
    """List the repo contents at `path` (or the repo root if empty)."""
    token, repo, branch = _config()
    if not (token and repo):
        return NOT_CONFIGURED

    try:
        resp = requests.get(_content_url(repo, path), headers=_headers(token),
                            params={"ref": branch}, timeout=10)
        if resp.status_code == 404:
            return f"Path not found in {repo}: {path or '(root)'}"
        if resp.status_code != 200:
            return f"Couldn't list {path or '(root)'}: {resp.status_code} {resp.text[:200]}"

        items = resp.json()
        if isinstance(items, dict):  # path pointed at a file, not a directory
            return f"{items['path']} (file, {items.get('size', '?')} bytes)"
        if not items:
            return "(empty)"

        return "\n".join(f"- {it['name']}{'/' if it['type'] == 'dir' else ''}" for it in items)

    except Exception as e:
        return f"Sorry, couldn't list the repo ({e})."


def read_file(path):
    """Read a file's contents from the repo."""
    token, repo, branch = _config()
    if not (token and repo):
        return NOT_CONFIGURED

    try:
        resp = requests.get(_content_url(repo, path), headers=_headers(token),
                            params={"ref": branch}, timeout=10)
        if resp.status_code == 404:
            return f"File not found in {repo}: {path}"
        if resp.status_code != 200:
            return f"Couldn't read {path}: {resp.status_code} {resp.text[:200]}"

        data = resp.json()
        if data.get("type") != "file" or "content" not in data:
            return f"{path} isn't a readable file (maybe a directory - try github_list_files)."

        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")[:8000]

    except Exception as e:
        return f"Sorry, couldn't read {path} ({e})."


def delete_file(path):
    """Delete a file from the repo. (Still recoverable from git history.)"""
    token, repo, branch = _config()
    if not (token and repo):
        return NOT_CONFIGURED

    try:
        url = _content_url(repo, path)
        get_resp = requests.get(url, headers=_headers(token), params={"ref": branch}, timeout=10)
        if get_resp.status_code == 404:
            return f"Nothing to delete - {path} isn't in {repo}."

        sha = get_resp.json().get("sha")
        if not sha:
            return f"Couldn't find {path} to delete."

        resp = requests.delete(url, headers=_headers(token), json={
            "message": f"Delete {path}", "sha": sha, "branch": branch,
        }, timeout=10)

        if resp.status_code == 200:
            return f"Deleted {path} from GitHub (still recoverable from commit history)."
        return f"Couldn't delete {path}: {resp.status_code} {resp.text[:200]}"

    except Exception as e:
        return f"Sorry, couldn't delete {path} ({e})."

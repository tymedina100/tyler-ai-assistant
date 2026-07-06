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

# How much of a file read returns. Generous so the coding agent can see whole
# source files (main.py etc.) when it needs full context, not just the first page.
MAX_READ_CHARS = 100_000


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


def _list(repo, ref, token, path):
    """List contents of `path` in `repo` at `ref`. Returns a formatted string."""
    resp = requests.get(_content_url(repo, path), headers=_headers(token),
                        params={"ref": ref}, timeout=10)
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


def _read(repo, ref, token, path):
    """Read `path` from `repo` at `ref`. Returns the text (truncated) or a message."""
    resp = requests.get(_content_url(repo, path), headers=_headers(token),
                        params={"ref": ref}, timeout=10)
    if resp.status_code == 404:
        return f"File not found in {repo}: {path}"
    if resp.status_code != 200:
        return f"Couldn't read {path}: {resp.status_code} {resp.text[:200]}"

    data = resp.json()
    if data.get("type") != "file" or "content" not in data:
        return f"{path} isn't a readable file (maybe a directory - list it first)."

    return base64.b64decode(data["content"]).decode("utf-8", errors="replace")[:MAX_READ_CHARS]


def list_files(path=""):
    """List the workspace repo's contents at `path` (or root)."""
    token, repo, branch = _config()
    if not (token and repo):
        return NOT_CONFIGURED
    try:
        return _list(repo, branch, token, path)
    except Exception as e:
        return f"Sorry, couldn't list the repo ({e})."


def read_file(path):
    """Read a file's contents from the workspace repo."""
    token, repo, branch = _config()
    if not (token and repo):
        return NOT_CONFIGURED
    try:
        return _read(repo, branch, token, path)
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


# --------------------------------------------------------------------------- #
# Code-repo workflow: let Patch propose changes to the assistant's OWN codebase
# (or any project repo) as pull requests the user reviews - never straight to the
# base branch. Operates on GITHUB_CODE_REPO (falls back to GITHUB_REPO) against
# GITHUB_CODE_BASE (default "main"). Needs a token with, on that repo, Contents +
# Pull requests: Read and write.
# --------------------------------------------------------------------------- #

# When a project is selected (see projects.py), the code-repo tools target that
# project's repo/branch instead of the env vars. These are set via
# set_active_code_repo() and cleared via clear_active_code_repo(); when both are
# None the resolution falls back to the original env-var behavior so nothing
# breaks when no project is selected.
_ACTIVE_CODE_REPO = None
_ACTIVE_CODE_BASE = None


def set_active_code_repo(repo, base):
    """Point the code-repo tools (code_list/read/propose/edit) at `repo`@`base`.
    Called by projects.set_active_project(). Pass a falsy repo to clear."""
    global _ACTIVE_CODE_REPO, _ACTIVE_CODE_BASE
    if not repo:
        _ACTIVE_CODE_REPO = None
        _ACTIVE_CODE_BASE = None
        return
    _ACTIVE_CODE_REPO = repo
    _ACTIVE_CODE_BASE = base or "main"


def clear_active_code_repo():
    """Forget any active project override; fall back to env vars."""
    set_active_code_repo(None, None)


def active_code_repo():
    """Return (repo, base) if a project override is active, else None."""
    if _ACTIVE_CODE_REPO:
        return _ACTIVE_CODE_REPO, _ACTIVE_CODE_BASE
    return None


def _code_config():
    # An active project (if any) wins; otherwise the original env-var behavior.
    if _ACTIVE_CODE_REPO:
        token, _, _ = _config()
        return token, _ACTIVE_CODE_REPO, _ACTIVE_CODE_BASE
    token, repo, _ = _config()
    code_repo = os.environ.get("GITHUB_CODE_REPO", "") or repo
    base = os.environ.get("GITHUB_CODE_BASE", "main")
    return token, code_repo, base


def branch_name(project_key, task):
    """Build a feature-branch name: ai/<project-key>/<short-task-slug>."""
    def _slug(text, max_len):
        slug = "".join(c if c.isalnum() else "-" for c in (text or "").lower())
        slug = "-".join(part for part in slug.split("-") if part)  # collapse dashes
        return slug[:max_len].strip("-")

    key = _slug(project_key, 30) or "project"
    name = _slug(task, 40) or "task"
    return f"ai/{key}/{name}"


def code_list_files(path=""):
    """List files/folders in the code repo."""
    token, repo, base = _code_config()
    if not (token and repo):
        return NOT_CONFIGURED
    try:
        return _list(repo, base, token, path)
    except Exception as e:
        return f"Sorry, couldn't list the code repo ({e})."


def code_read_file(path):
    """Read a file from the code repo."""
    token, repo, base = _code_config()
    if not (token and repo):
        return NOT_CONFIGURED
    try:
        return _read(repo, base, token, path)
    except Exception as e:
        return f"Sorry, couldn't read {path} from the code repo ({e})."


def _branch_sha(repo, branch, token):
    resp = requests.get(f"{API_ROOT}/repos/{repo}/git/ref/heads/{branch}",
                        headers=_headers(token), timeout=10)
    return resp.json()["object"]["sha"] if resp.status_code == 200 else None


def _ensure_branch(repo, branch, base, token):
    """Create `branch` off `base` if it doesn't exist. Returns None on success or an
    error string."""
    if _branch_sha(repo, branch, token):
        return None  # already exists - reuse it (multi-file PRs land on one branch)

    base_sha = _branch_sha(repo, base, token)
    if not base_sha:
        return f"Base branch '{base}' not found in {repo}."

    resp = requests.post(f"{API_ROOT}/repos/{repo}/git/refs", headers=_headers(token),
                        json={"ref": f"refs/heads/{branch}", "sha": base_sha}, timeout=10)
    if resp.status_code not in (200, 201):
        return f"Couldn't create branch {branch}: {resp.status_code} {resp.text[:200]}"
    return None


def _existing_pr_url(repo, branch, token):
    owner = repo.split("/")[0]
    resp = requests.get(f"{API_ROOT}/repos/{repo}/pulls", headers=_headers(token),
                        params={"head": f"{owner}:{branch}", "state": "open"}, timeout=10)
    if resp.status_code == 200 and resp.json():
        return resp.json()[0]["html_url"]
    return None


def _get_content(repo, ref, token, path):
    """Return a file's raw text at `ref`, or None if it isn't a readable file there."""
    resp = requests.get(_content_url(repo, path), headers=_headers(token),
                        params={"ref": ref}, timeout=10)
    if resp.status_code != 200:
        return None
    data = resp.json()
    if data.get("type") != "file" or "content" not in data:
        return None
    return base64.b64decode(data["content"]).decode("utf-8", errors="replace")


def _commit_and_pr(repo, base, branch, path, content, title, body, token):
    """Commit `content` to `branch` and make sure an open PR exists for it. Assumes
    the branch already exists. Returns a status string."""
    _, commit_err = _commit(path, content, token, repo, branch)
    if commit_err:
        return f"Couldn't commit {path} to {branch}: {commit_err}"

    pr_url = _existing_pr_url(repo, branch, token)
    if not pr_url:
        pr_resp = requests.post(
            f"{API_ROOT}/repos/{repo}/pulls", headers=_headers(token),
            json={"title": title, "head": branch, "base": base, "body": body}, timeout=10)
        if pr_resp.status_code in (200, 201):
            pr_url = pr_resp.json()["html_url"]
        else:
            return (f"Committed {path} to '{branch}', but couldn't open the PR: "
                    f"{pr_resp.status_code} {pr_resp.text[:200]}")

    return f"Committed {path} to branch '{branch}'. Pull request (review + merge to ship): {pr_url}"


def code_propose_change(branch, path, content, title, body=""):
    """Commit whole-file `content` to `path` on `branch` and ensure a PR exists. Best
    for NEW files; to change an existing file prefer code_edit_file (you don't have to
    reproduce the whole file). Reuse the same branch name across a multi-file change;
    the PR is created once and reused. Nothing ships until the user merges."""
    token, repo, base = _code_config()
    if not (token and repo):
        return NOT_CONFIGURED

    try:
        err = _ensure_branch(repo, branch, base, token)
        if err:
            return err
        return _commit_and_pr(repo, base, branch, path, content, title, body, token)
    except Exception as e:
        return f"Sorry, the change proposal failed ({e})."


def code_edit_file(branch, path, old_snippet, new_snippet, title, body=""):
    """Make a targeted edit to an existing file in the code repo: replace `old_snippet`
    with `new_snippet` (the snippet must appear exactly once), then commit to `branch`
    and ensure a PR. You only supply the small changed snippet, not the whole file - so
    this is the reliable way to edit large files. Edits stack on the branch, so call it
    repeatedly (same branch) for several changes in one PR. Nothing ships until merge."""
    token, repo, base = _code_config()
    if not (token and repo):
        return NOT_CONFIGURED

    try:
        err = _ensure_branch(repo, branch, base, token)
        if err:
            return err

        # Edit relative to the branch's current state so repeated edits accumulate.
        current = _get_content(repo, branch, token, path)
        if current is None:
            return f"Couldn't read {path} from {repo}@{branch} to edit it (does it exist?)."

        occurrences = current.count(old_snippet)
        if occurrences == 0:
            return (f"The snippet to replace wasn't found in {path}. Read the file "
                    f"(code_read_file) and copy an exact, unique snippet to replace.")
        if occurrences > 1:
            return (f"That snippet appears {occurrences} times in {path} - include more "
                    f"surrounding context so it matches exactly once.")

        updated = current.replace(old_snippet, new_snippet, 1)
        return _commit_and_pr(repo, base, branch, path, updated, title, body, token)

    except Exception as e:
        return f"Sorry, the edit failed ({e})."

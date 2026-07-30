"""Multi-project registry + active-project selection.

The assistant works across several repos (see projects.json). This module loads
that registry, tracks which project is "active" for the running process, and
points the GitHub code-repo tools (code_list/read/propose/edit) at the active
project's repo/branch. When no project is selected everything falls back to the
env vars (GITHUB_CODE_REPO / GITHUB_CODE_BASE), so existing behavior is preserved.

The active selection is persisted to active_project.json in DATA_DIR (same
pattern as company_state.json) so it survives a restart of the long-running
Telegram process.
"""
import contextvars
import json
import os
from pathlib import Path

import github_helpers


BASE_DIR = Path(__file__).parent
_SCOPED_PROJECT_KEY = contextvars.ContextVar("scoped_project_key", default=None)


def _data_dir():
    """Directory for persistent state. Set DATA_DIR (e.g. a mounted volume on
    Railway) so the active-project choice survives redeploys. Honor Railway's
    mounted-volume hint too, matching Company Mode and Google state."""
    return Path(
        os.environ.get("DATA_DIR")
        or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
        or BASE_DIR
    )


def _registry_path():
    """Prefer a DATA_DIR copy of projects.json if present, else the bundled one."""
    override = _data_dir() / "projects.json"
    if override.exists():
        return override
    return BASE_DIR / "projects.json"


def _active_file():
    return _data_dir() / "active_project.json"


def load_registry():
    """Return the project registry dict (key -> profile). Empty dict if missing."""
    path = _registry_path()
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def list_projects():
    """Return a list of (key, profile) pairs in registry order."""
    return list(load_registry().items())


def get_project(key):
    """Return the profile dict for `key`, or None if unknown."""
    if not key:
        return None
    return load_registry().get(key)


def project_repo(key):
    """Return the repo ('owner/name') for a project key, or None."""
    profile = get_project(key) or {}
    return profile.get("repo") or None


def find_by_linear_project(linear_project_name):
    """Map a Linear project name (e.g. 'Worthlane') to the assistant project key that
    owns it, so /linear do can target the right repo automatically. Matches on the
    profile's `linear_project` field first, then falls back to the profile `name`.
    Case-insensitive. Returns the key or None."""
    name = (linear_project_name or "").strip().lower()
    if not name:
        return None
    for key, profile in load_registry().items():
        linked = (profile.get("linear_project") or "").strip().lower()
        if linked and linked == name:
            return key
    # Fallback: match against the human name (handles "Worthlane / Vantage" vs "Worthlane").
    for key, profile in load_registry().items():
        human = (profile.get("name") or "").strip().lower()
        if human and (name in human or human in name):
            return key
    return None


def _read_active_key():
    path = _active_file()
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        key = data.get("active")
        # Only honor keys that still exist in the registry.
        return key if key and key in load_registry() else None
    except (json.JSONDecodeError, OSError):
        return None


def _write_active_key(key):
    path = _active_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"active": key}, f)
    except OSError:
        pass  # Persistence is best-effort; the in-process choice still applies.


def _apply_to_github(key):
    """Point the GitHub code tools at `key`'s repo, or clear the override."""
    profile = get_project(key) if key else None
    if profile and profile.get("repo"):
        github_helpers.set_active_code_repo(profile["repo"], profile.get("default_branch", "main"))
    else:
        github_helpers.clear_active_code_repo()


def get_active_project():
    """Return (key, profile) for the active project, or (None, None).

    Re-applies the persisted selection to github_helpers so a fresh process
    (which lost the in-memory override on restart) targets the right repo.
    """
    scoped_key = _SCOPED_PROJECT_KEY.get()
    key = scoped_key or _read_active_key()
    if not key:
        return None, None
    if not scoped_key and github_helpers.active_code_repo() is None:
        _apply_to_github(key)
    return key, get_project(key)


def begin_scoped_project(key):
    """Select a project only for this execution context.

    Returns ``(profile, error, token_pair)``.  The caller must pass the token pair
    to :func:`end_scoped_project` in ``finally``.  Unlike ``set_active_project``,
    this never rewrites the user's persisted project selection.
    """
    profile = get_project(key)
    if profile is None:
        known = ", ".join(k for k, _ in list_projects()) or "(none configured)"
        return None, f"Unknown project '{key}'. Known projects: {known}.", None
    if not profile.get("repo"):
        return None, f"Project '{key}' has no repo configured in projects.json.", None
    project_token = _SCOPED_PROJECT_KEY.set(key)
    code_token = github_helpers.set_scoped_code_repo(
        profile["repo"], profile.get("default_branch", "main")
    )
    return profile, None, (project_token, code_token)


def end_scoped_project(tokens):
    if not tokens:
        return
    project_token, code_token = tokens
    github_helpers.reset_scoped_code_repo(code_token)
    _SCOPED_PROJECT_KEY.reset(project_token)


def set_active_project(key):
    """Select `key` as the active project. Returns (profile, None) on success or
    (None, error_message) if the key is unknown."""
    profile = get_project(key)
    if profile is None:
        known = ", ".join(k for k, _ in list_projects()) or "(none configured)"
        return None, f"Unknown project '{key}'. Known projects: {known}."
    if not profile.get("repo"):
        return None, f"Project '{key}' has no repo configured in projects.json."
    _write_active_key(key)
    _apply_to_github(key)
    return profile, None


def clear_active_project():
    """Deselect the active project and fall back to env-var config."""
    _write_active_key(None)
    github_helpers.clear_active_code_repo()


def active_code_target():
    """Return (repo, base) for the active project, or None if none selected."""
    key, profile = get_active_project()
    if profile and profile.get("repo"):
        return profile["repo"], profile.get("default_branch", "main")
    return None


def describe_active():
    """One-line human summary of the active project (for tool responses)."""
    key, profile = get_active_project()
    if not profile:
        return "No active project selected (using env-var GitHub config)."
    return f"Active project: {profile.get('name', key)} ({key}) -> repo {profile.get('repo')}"


# Re-apply any persisted selection at import so the code tools are pointed at the
# right repo as soon as the process starts.
get_active_project()

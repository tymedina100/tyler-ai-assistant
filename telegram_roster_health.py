"""Deterministic health checks for the multi-bot Telegram roster.

This module deliberately performs no network or environment access.  The Telegram
runtime supplies its roster metadata, token values, ``get_me`` results, and group
membership results.  Keeping evaluation pure makes startup policy easy to test and
ensures secrets never become part of a health report.
"""

from dataclasses import dataclass
from typing import Any, Iterable, Mapping


READY = "ready"
MISSING_TOKEN = "missing_token"
INVALID_IDENTITY = "invalid_identity"
PRIVACY_ENABLED = "privacy_enabled"
NOT_IN_GROUP = "not_in_group"
CHECK_UNAVAILABLE = "check_unavailable"

_ACTIVE_MEMBER_STATUSES = frozenset({"administrator", "creator", "member"})


@dataclass(frozen=True)
class RosterIssue:
    """One actionable reason an expected bot is not ready."""

    code: str
    detail: str

    def to_dict(self):
        return {"code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class AgentRosterHealth:
    """Secret-free health state for one expected agent."""

    key: str
    label: str
    env_var: str
    username: str
    issues: tuple[RosterIssue, ...]

    @property
    def ready(self):
        return not self.issues

    @property
    def status(self):
        return READY if self.ready else self.issues[0].code

    @property
    def issue_codes(self):
        return tuple(issue.code for issue in self.issues)

    def to_dict(self):
        return {
            "key": self.key,
            "label": self.label,
            "env_var": self.env_var,
            "username": self.username,
            "ready": self.ready,
            "status": self.status,
            "issues": [issue.to_dict() for issue in self.issues],
        }


@dataclass(frozen=True)
class RosterHealth:
    """Aggregate health for the complete expected roster."""

    agents: tuple[AgentRosterHealth, ...]

    @property
    def complete(self):
        return all(agent.ready for agent in self.agents)

    @property
    def expected_keys(self):
        return tuple(agent.key for agent in self.agents)

    @property
    def ready_keys(self):
        return tuple(agent.key for agent in self.agents if agent.ready)

    @property
    def issue_count(self):
        return sum(len(agent.issues) for agent in self.agents)

    def to_dict(self):
        return {
            "complete": self.complete,
            "expected_count": len(self.agents),
            "ready_count": len(self.ready_keys),
            "issue_count": self.issue_count,
            "agents": [agent.to_dict() for agent in self.agents],
        }


def expected_roster_keys(specialist_keys: Iterable[str]):
    """Return Manager + every specialist + General in stable, unique order."""

    keys = ["manager"]
    seen = {"manager", "general"}
    for raw_key in specialist_keys:
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ValueError("specialist keys must be non-empty strings")
        key = raw_key.strip()
        if key not in seen:
            keys.append(key)
            seen.add(key)
    keys.append("general")
    return tuple(keys)


def _identity_error(identity: Any):
    if not isinstance(identity, Mapping):
        return "Telegram did not return a bot identity."
    if identity.get("check_unavailable") is True:
        return "Telegram bot identity could not be verified after bounded retries."
    bot_id = identity.get("id")
    if not isinstance(bot_id, int) or isinstance(bot_id, bool) or bot_id <= 0:
        return "Telegram returned an identity without a valid bot id."
    if identity.get("is_bot") is not True:
        return "The configured token does not identify a Telegram bot."
    if not str(identity.get("username") or "").strip():
        return "Telegram returned a bot identity without a username."
    return ""


def _privacy_is_enabled(identity: Mapping[str, Any]):
    if "privacy_enabled" in identity:
        return identity.get("privacy_enabled") is not False
    # Telegram only guarantees can_read_all_group_messages when privacy mode is
    # disabled.  Missing/False therefore fails closed as privacy enabled.
    return identity.get("can_read_all_group_messages") is not True


def _is_group_member(membership: Any):
    if isinstance(membership, bool):
        return membership
    if isinstance(membership, str):
        return membership.strip().lower() in _ACTIVE_MEMBER_STATUSES
    if not isinstance(membership, Mapping):
        return False
    status = str(membership.get("status") or "").strip().lower()
    if status in _ACTIVE_MEMBER_STATUSES:
        return True
    if status == "restricted":
        return membership.get("is_member") is True
    return False


def _membership_check_unavailable(membership: Any):
    return (
        isinstance(membership, Mapping)
        and membership.get("check_unavailable") is True
    )


def evaluate_roster(
    *,
    specialist_keys: Iterable[str],
    agent_info: Mapping[str, Mapping[str, Any]],
    token_values: Mapping[str, Any],
    identities: Mapping[str, Any],
    group_memberships: Mapping[str, Any],
):
    """Evaluate caller-supplied Telegram evidence for the complete roster.

    ``token_values`` is keyed by the environment-variable names in ``agent_info``.
    Tokens are checked only for non-blank presence and are never copied into the
    returned objects.  ``identities`` and ``group_memberships`` are keyed by agent
    key and should contain normalized Telegram API results.
    """

    expected_keys = expected_roster_keys(specialist_keys)
    token_owners: dict[str, list[str]] = {}
    for key in expected_keys:
        info = agent_info.get(key)
        if not isinstance(info, Mapping):
            continue
        env_var = str(info.get("env_var") or "").strip()
        token = token_values.get(env_var) if env_var else None
        if isinstance(token, str) and token.strip():
            token_owners.setdefault(token.strip(), []).append(key)

    duplicate_token_keys = {
        key
        for owners in token_owners.values()
        if len(owners) > 1
        for key in owners
    }

    eligible_identity_keys = []
    for key in expected_keys:
        info = agent_info.get(key)
        if not isinstance(info, Mapping):
            continue
        env_var = str(info.get("env_var") or "").strip()
        token = token_values.get(env_var) if env_var else None
        if isinstance(token, str) and token.strip() and key not in duplicate_token_keys:
            eligible_identity_keys.append(key)

    identity_owners: dict[tuple[int, str], list[str]] = {}
    for key in eligible_identity_keys:
        identity = identities.get(key)
        if _identity_error(identity):
            continue
        identity_key = (
            identity["id"],
            str(identity["username"]).strip().casefold(),
        )
        identity_owners.setdefault(identity_key, []).append(key)

    duplicate_identity_keys = {
        key
        for owners in identity_owners.values()
        if len(owners) > 1
        for key in owners
    }

    agents = []
    for key in expected_keys:
        info = agent_info.get(key)
        label = key.replace("_", " ").title()
        env_var = ""
        username = ""
        issues = []

        if not isinstance(info, Mapping):
            issues.append(RosterIssue(
                INVALID_IDENTITY,
                "No AGENT_INFO entry is configured for this expected agent.",
            ))
        else:
            label = str(info.get("label") or label).strip()
            env_var = str(info.get("env_var") or "").strip()
            if not env_var:
                issues.append(RosterIssue(
                    INVALID_IDENTITY,
                    "The AGENT_INFO entry does not name a token environment variable.",
                ))
            else:
                token = token_values.get(env_var)
                if not isinstance(token, str) or not token.strip():
                    issues.append(RosterIssue(
                        MISSING_TOKEN,
                        f"Set {env_var} to this agent's BotFather token.",
                    ))
                elif key in duplicate_token_keys:
                    issues.append(RosterIssue(
                        INVALID_IDENTITY,
                        "This token is reused by another expected agent; each agent needs a distinct bot.",
                    ))
                else:
                    identity = identities.get(key)
                    identity_error = _identity_error(identity)
                    if identity_error:
                        issues.append(RosterIssue(INVALID_IDENTITY, identity_error))
                    elif key in duplicate_identity_keys:
                        username = str(identity["username"]).strip()
                        issues.append(RosterIssue(
                            INVALID_IDENTITY,
                            "This Telegram identity is reused by another expected agent.",
                        ))
                    else:
                        username = str(identity["username"]).strip()
                        if _privacy_is_enabled(identity):
                            issues.append(RosterIssue(
                                PRIVACY_ENABLED,
                                "Disable Group Privacy for this bot in BotFather.",
                            ))
                        membership = group_memberships.get(key)
                        if _membership_check_unavailable(membership):
                            issues.append(RosterIssue(
                                CHECK_UNAVAILABLE,
                                "Telegram group membership could not be verified after bounded retries.",
                            ))
                        elif not _is_group_member(membership):
                            issues.append(RosterIssue(
                                NOT_IN_GROUP,
                                "Add this bot to the configured Telegram group.",
                            ))

        agents.append(AgentRosterHealth(
            key=key,
            label=label,
            env_var=env_var,
            username=username,
            issues=tuple(issues),
        ))

    return RosterHealth(tuple(agents))


def render_roster_summary(health: RosterHealth, *, include_ready=False):
    """Render a concise, actionable Telegram-safe roster summary."""

    total = len(health.agents)
    ready = len(health.ready_keys)
    state = "COMPLETE" if health.complete else "INCOMPLETE"
    lines = [f"Telegram roster: {state} ({ready}/{total} ready)"]
    for agent in health.agents:
        if agent.ready:
            if include_ready:
                identity = f"@{agent.username}" if agent.username else "identity verified"
                lines.append(f"- {agent.label} [{agent.key}]: ready ({identity})")
            continue
        codes = ", ".join(agent.issue_codes)
        details = " ".join(issue.detail for issue in agent.issues)
        lines.append(f"- {agent.label} [{agent.key}]: {codes} - {details}")
    return "\n".join(lines)

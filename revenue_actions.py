"""Fail-closed external actions for an owner-approved revenue campaign.

This module is intentionally separate from the assistant's ordinary confirmation
machinery.  A caller must install a verified, action-specific capability in the
current execution context.  The persistent Company Mode claim is still the
authoritative authorization: it is written before provider I/O and completed after
the outcome is known.  A replayed claim is never executed again.

Provider credentials are read from environment variables at call time and are never
included in action metadata, results, or logs.
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlparse

import requests


logger = logging.getLogger(__name__)

OUTREACH_ACTION = "outreach"
DEPLOY_ACTION = "deploy"
PUBLISH_ACTION = "publish"
PURCHASE_ACTION = "purchase"

CAMPAIGN_SEND_OUTREACH_EMAIL_TOOL = "campaign_send_outreach_email"
CAMPAIGN_DEPLOY_VERCEL_TOOL = "campaign_deploy_vercel"
CAMPAIGN_PUBLISH_WEBHOOK_TOOL = "campaign_publish_webhook"
CAMPAIGN_PUBLISH_BLUESKY_TOOL = "campaign_publish_bluesky"
CAMPAIGN_PURCHASE_WEBHOOK_TOOL = "campaign_purchase_webhook"

CAMPAIGN_TOOL_ACTIONS = {
    CAMPAIGN_SEND_OUTREACH_EMAIL_TOOL: OUTREACH_ACTION,
    CAMPAIGN_DEPLOY_VERCEL_TOOL: DEPLOY_ACTION,
    CAMPAIGN_PUBLISH_WEBHOOK_TOOL: PUBLISH_ACTION,
    CAMPAIGN_PUBLISH_BLUESKY_TOOL: PUBLISH_ACTION,
    CAMPAIGN_PURCHASE_WEBHOOK_TOOL: PURCHASE_ACTION,
}
CAMPAIGN_TOOL_NAMES = frozenset(CAMPAIGN_TOOL_ACTIONS)
CAMPAIGN_DRAFT_JSON_PREFIX = "CAMPAIGN_DRAFT_JSON:"
CAMPAIGN_DRAFT_KEYS = frozenset({"action_type", "target", "text", "url"})
CAMPAIGN_DRAFT_SCHEMAS = {
    "publish_bluesky": CAMPAIGN_DRAFT_KEYS,
    "publish_webhook": frozenset({"action_type", "target", "payload"}),
    OUTREACH_ACTION: frozenset(
        {"action_type", "target", "recipient", "subject", "body"}
    ),
    DEPLOY_ACTION: frozenset(
        {"action_type", "target", "account_id", "project", "ref"}
    ),
    PURCHASE_ACTION: frozenset(
        {"action_type", "target", "amount_usd", "payload"}
    ),
}

_ACTION_RESULT_LIMIT = 500
_WEBHOOK_PAYLOAD_LIMIT = 20_000
_WEBHOOK_RESPONSE_LIMIT = 64_000
_MAX_SIGNALS_PER_RESPONSE = 10
_SIGNAL_EVIDENCE_LIMIT = 500
_SIGNAL_TYPES = frozenset({
    "bounce",
    "checkout_started",
    "click",
    "lead",
    "purchase_commitment",
    "reply",
    "sale",
    "signup",
    "strong_intent",
    "unsubscribe",
    "wishlist",
})
_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:authorization|cookie|credential|password|secret|token|api[_-]?key|"
    r"private[_-]?key|client[_-]?secret|card(?:_number)?|cvv|cvc)"
)
_SAFE_RECEIPT_RE = re.compile(r"[^A-Za-z0-9._:@/-]+")
_SECRET_TEXT_RE = re.compile(
    r"(?i)(?:"
    r"\bsk-[A-Za-z0-9_-]{16,}\b|"
    r"\bgithub_pat_[A-Za-z0-9_]{16,}\b|"
    r"\bgh[pousr]_[A-Za-z0-9]{16,}\b|"
    r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b|"
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b|"
    r"\b(?:token|secret|password|api[_-]?key|authorization)\s*[:=]\s*\S{6,}"
    r")"
)
_NONPUBLIC_MARKER_RE = re.compile(
    r"(?i)(?:\[(?:private|internal|confidential)\]|"
    r"\b(?:internal only|do not publish|not for public release)\b)"
)
_BLUESKY_BOOTSTRAP_MESSAGE = (
    "NEEDS HUMAN: create a dedicated company-owned Bluesky account manually if needed, "
    "label the profile as automated, complete provider verification, generate an app "
    "password, set REVENUE_COMPANY_ACTION_TARGETS, REVENUE_BLUESKY_HANDLE, and "
    "REVENUE_BLUESKY_APP_PASSWORD, then redeploy. Automated signup and account creation "
    "are disabled."
)


class RevenueActionDenied(RuntimeError):
    """The requested external action is outside the active campaign capability."""


@dataclass(frozen=True)
class CampaignActionContext:
    capability: Mapping[str, Any]
    run_id: str
    dry_run: bool = False
    approved_payload_digest: str = ""

    @property
    def campaign_id(self) -> str:
        return str(self.capability.get("campaign_id") or "")


@dataclass(frozen=True)
class ProviderOutcome:
    status: str
    result: str
    actual_purchase_usd: Optional[float] = None
    response_json: Optional[Mapping[str, Any]] = None
    provider_receipt: Optional[Mapping[str, str]] = None


_active_context: contextvars.ContextVar[Optional[CampaignActionContext]] = (
    contextvars.ContextVar("revenue_campaign_action_context", default=None)
)


def tool_names_for_capability(capability: Any) -> set[str]:
    """Return the one tool authorized by a verified action-specific capability.

    This is deliberately structural and narrow.  The live Company Mode policy is
    rechecked immediately before the claim, so a stale capability cannot execute.
    """
    if not isinstance(capability, Mapping):
        return set()
    if capability.get("allowed") is not True:
        return set()
    if str(capability.get("campaign_status") or "").lower() != "active":
        return set()
    if not str(capability.get("campaign_id") or "").strip():
        return set()
    if not str(capability.get("target") or "").strip():
        return set()
    policy_revision = str(capability.get("policy_revision") or "").strip()
    requested_revision = str(capability.get("requested_policy_revision") or "").strip()
    if not policy_revision or requested_revision != policy_revision:
        return set()
    action_type = str(capability.get("action_type") or "").lower()
    target = str(capability.get("target") or "")
    if action_type == PUBLISH_ACTION:
        return {
            CAMPAIGN_PUBLISH_BLUESKY_TOOL
            if target.startswith("bluesky:")
            else CAMPAIGN_PUBLISH_WEBHOOK_TOOL
        }
    tool_name = {
        OUTREACH_ACTION: CAMPAIGN_SEND_OUTREACH_EMAIL_TOOL,
        DEPLOY_ACTION: CAMPAIGN_DEPLOY_VERCEL_TOOL,
        PURCHASE_ACTION: CAMPAIGN_PURCHASE_WEBHOOK_TOOL,
    }.get(action_type)
    return {tool_name} if tool_name else set()


def set_campaign_action_context(
    capability: Mapping[str, Any],
    run_id: str,
    *,
    dry_run: bool = False,
    approved_payload_digest: Optional[str] = None,
):
    """Install one verified capability and return a ContextVar reset token."""
    if not tool_names_for_capability(capability):
        raise RevenueActionDenied("The revenue campaign capability is not active and allowed.")
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        raise RevenueActionDenied("A persisted autonomous run ID is required for an external action.")
    normalized_digest = str(approved_payload_digest or "").strip().lower()
    if normalized_digest and not re.fullmatch(r"[a-f0-9]{64}", normalized_digest):
        raise RevenueActionDenied("An approved SHA-256 campaign payload digest is required.")
    return _active_context.set(
        CampaignActionContext(
            dict(capability),
            normalized_run_id,
            bool(dry_run),
            normalized_digest,
        )
    )


def reset_campaign_action_context(token) -> None:
    _active_context.reset(token)


def current_campaign_action_context() -> Optional[CampaignActionContext]:
    return _active_context.get()


def _money(value: Any) -> float:
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        raise RevenueActionDenied("The purchase amount must be a valid non-negative number.")
    if amount < 0:
        raise RevenueActionDenied("The purchase amount must be non-negative.")
    return float(amount)


def _purchase_hard_cap() -> float:
    try:
        return max(0.0, float(os.environ.get("REVENUE_PURCHASE_HARD_CAP_USD", "0")))
    except (TypeError, ValueError):
        return 0.0


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise RevenueActionDenied("The external-action payload must be valid JSON.") from exc


def _payload_digest(payload: Any) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _contains_sensitive_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if _SENSITIVE_KEY_RE.search(str(key)):
                return True
            if _contains_sensitive_key(child):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_sensitive_key(child) for child in value)
    return False


def _luhn_valid(candidate: str) -> bool:
    digits = [int(char) for char in candidate if char.isdigit()]
    if len(digits) < 13 or len(digits) > 19 or len(set(digits)) == 1:
        return False
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def _contains_sensitive_text(value: Any) -> bool:
    text = str(value or "")
    if _SECRET_TEXT_RE.search(text):
        return True
    for candidate in re.findall(r"(?:\d[ -]?){13,19}", text):
        if _luhn_valid(candidate):
            return True
    for name, secret in os.environ.items():
        if not _SENSITIVE_KEY_RE.search(name) or len(secret) < 8:
            continue
        if secret in text:
            return True
    return False


def _assert_public_content(value: Any, label: str) -> None:
    if _contains_sensitive_text(value):
        raise RevenueActionDenied(
            f"The {label} appears to contain a credential, secret, or payment-card number."
        )


def _assert_public_campaign_text(value: str) -> None:
    if any(ord(char) < 32 and char not in {"\n", "\t"} for char in value):
        raise RevenueActionDenied(
            "The campaign draft contains unsupported control characters."
        )
    if _NONPUBLIC_MARKER_RE.search(value):
        raise RevenueActionDenied(
            "The campaign draft is explicitly marked as non-public content."
        )
    _assert_public_content(value, "campaign draft")


def _assert_public_https_url(value: str) -> None:
    parsed = urlparse(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RevenueActionDenied("The campaign product URL must be one exact public HTTPS URL.")
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise RevenueActionDenied("The campaign product URL must be publicly reachable.")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise RevenueActionDenied("The campaign product URL must be publicly reachable.")
    _assert_public_content(value, "campaign product URL")


def _validated_webhook_endpoint(prefix: str, *, require_allowed_host: bool) -> str:
    url = os.environ.get(f"REVENUE_{prefix}_WEBHOOK_URL", "").strip()
    parsed = urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise RevenueActionDenied("The revenue webhook URL has an invalid port.") from exc
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or hostname == "localhost"
        or hostname.endswith((".localhost", ".local", ".internal"))
    ):
        raise RevenueActionDenied(
            "The revenue webhook must use one public HTTPS host without userinfo or a custom port."
        )
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise RevenueActionDenied("The revenue webhook may not target a private address.")
    allowed_host = os.environ.get(
        f"REVENUE_{prefix}_WEBHOOK_ALLOWED_HOST", ""
    ).strip().rstrip(".").lower()
    if require_allowed_host and (not allowed_host or allowed_host != hostname):
        raise RevenueActionDenied(
            f"NEEDS HUMAN: set REVENUE_{prefix}_WEBHOOK_ALLOWED_HOST to the exact public webhook host."
        )
    if allowed_host and allowed_host != hostname:
        raise RevenueActionDenied("The revenue webhook URL does not match its exact allowed host.")
    _assert_public_content(url, "revenue webhook URL")
    return url


def _campaign_draft_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RevenueActionDenied(f"The campaign draft repeats the JSON key {key!r}.")
        result[key] = value
    return result


def _canonical_bluesky_campaign_payload(
    payload: Any,
    *,
    expected_target: str,
    expected_product_url: Optional[str] = None,
) -> dict[str, str]:
    if not isinstance(payload, Mapping):
        raise RevenueActionDenied("The campaign draft JSON must be one object.")
    if set(payload) != CAMPAIGN_DRAFT_KEYS:
        raise RevenueActionDenied(
            "The campaign draft must contain only action_type, target, text, and url."
        )
    if any(not isinstance(payload[key], str) for key in CAMPAIGN_DRAFT_KEYS):
        raise RevenueActionDenied("Every campaign draft field must be a string.")
    if payload["action_type"] != PUBLISH_ACTION:
        raise RevenueActionDenied("The campaign draft action_type must be exactly 'publish'.")

    if (
        not isinstance(expected_target, str)
        or expected_target != expected_target.strip()
    ):
        raise RevenueActionDenied("The expected campaign target must be one exact string.")
    exact_target = _normalize_target(expected_target)
    if not exact_target.startswith("bluesky:"):
        raise RevenueActionDenied("The campaign target must use bluesky:<handle>.")
    if payload["target"] != exact_target:
        raise RevenueActionDenied(
            "The campaign draft target does not exactly match the approved target."
        )

    text = payload["text"]
    if (
        text != text.strip()
        or not text
        or len(text) > 300
        or len(text.encode("utf-8")) > 1200
    ):
        raise RevenueActionDenied(
            "The campaign draft text must contain exactly 1-300 bounded characters."
        )
    _assert_public_campaign_text(text)

    url = payload["url"]
    if url != url.strip():
        raise RevenueActionDenied("The campaign product URL may not contain surrounding whitespace.")
    if expected_product_url is not None:
        if not isinstance(expected_product_url, str):
            raise RevenueActionDenied("The expected campaign product URL must be a string.")
        if expected_product_url != expected_product_url.strip():
            raise RevenueActionDenied("The expected campaign product URL must be exact.")
        if url != expected_product_url:
            raise RevenueActionDenied(
                "The campaign draft URL does not exactly match the approved product URL."
            )
    if url:
        _assert_public_https_url(url)
        if text.count(url) != 1:
            raise RevenueActionDenied(
                "The exact campaign product URL must appear once in the campaign text."
            )

    return {
        "action_type": PUBLISH_ACTION,
        "target": exact_target,
        "text": text,
        "url": url,
    }


def _exact_campaign_target(action_type: str, expected_target: str, payload: Mapping[str, Any]) -> str:
    if (
        not isinstance(expected_target, str)
        or expected_target != expected_target.strip()
    ):
        raise RevenueActionDenied("The expected campaign target must be one exact string.")
    exact_target = _normalize_target(expected_target)
    if payload.get("action_type") != action_type:
        raise RevenueActionDenied(
            f"The campaign draft action_type must be exactly {action_type!r}."
        )
    if payload.get("target") != exact_target:
        raise RevenueActionDenied(
            "The campaign draft target does not exactly match the approved target."
        )
    return exact_target


def _canonical_publish_webhook_payload(
    payload: Any,
    *,
    expected_target: str,
    expected_product_url: Optional[str] = None,
) -> dict[str, Any]:
    keys = CAMPAIGN_DRAFT_SCHEMAS["publish_webhook"]
    if not isinstance(payload, Mapping) or set(payload) != keys:
        raise RevenueActionDenied(
            "The publish draft must contain only action_type, target, and payload."
        )
    exact_target = _exact_campaign_target(PUBLISH_ACTION, expected_target, payload)
    if exact_target.startswith("bluesky:"):
        raise RevenueActionDenied("Bluesky publishes require the text and url draft schema.")
    safe_payload = _validated_payload(payload["payload"])
    if expected_product_url:
        if not isinstance(expected_product_url, str):
            raise RevenueActionDenied("The expected campaign product URL must be a string.")
        exact_url = expected_product_url.strip()
        if exact_url != expected_product_url:
            raise RevenueActionDenied("The expected campaign product URL must be exact.")
        _assert_public_https_url(exact_url)
        if _canonical_json(safe_payload).count(exact_url) != 1:
            raise RevenueActionDenied(
                "The exact campaign product URL must appear once in the publish payload."
            )
    return {
        "action_type": PUBLISH_ACTION,
        "target": exact_target,
        "payload": safe_payload,
    }


def _canonical_outreach_payload(
    payload: Any,
    *,
    expected_target: str,
    expected_product_url: Optional[str] = None,
) -> dict[str, str]:
    keys = CAMPAIGN_DRAFT_SCHEMAS[OUTREACH_ACTION]
    if not isinstance(payload, Mapping) or set(payload) != keys:
        raise RevenueActionDenied(
            "The outreach draft must contain only action_type, target, recipient, subject, and body."
        )
    if any(not isinstance(payload[key], str) for key in keys):
        raise RevenueActionDenied("Every outreach draft field must be a string.")
    exact_target = _exact_campaign_target(OUTREACH_ACTION, expected_target, payload)
    company_target = _company_target_config(OUTREACH_ACTION, exact_target)
    configured_recipient = company_target.get("recipient")
    if not isinstance(configured_recipient, str) or configured_recipient != configured_recipient.strip():
        raise RevenueActionDenied(
            "The company outreach target must contain one exact recipient field."
        )
    recipient = _email_target(configured_recipient)
    if payload["recipient"] != recipient:
        raise RevenueActionDenied(
            "The outreach recipient does not exactly match the company target mapping."
        )
    subject = payload["subject"]
    body = payload["body"]
    if subject != subject.strip() or not subject or len(subject) > 300:
        raise RevenueActionDenied(
            "The outreach subject must contain exactly 1-300 bounded characters."
        )
    if body != body.strip() or not body or len(body) > 20_000:
        raise RevenueActionDenied(
            "The outreach body must contain exactly 1-20,000 bounded characters."
        )
    _assert_public_content(subject, "outreach subject")
    _assert_public_content(body, "outreach body")
    if expected_product_url:
        if not isinstance(expected_product_url, str):
            raise RevenueActionDenied("The expected campaign product URL must be a string.")
        exact_url = expected_product_url.strip()
        if exact_url != expected_product_url:
            raise RevenueActionDenied("The expected campaign product URL must be exact.")
        _assert_public_https_url(exact_url)
        if body.count(exact_url) != 1:
            raise RevenueActionDenied(
                "The exact campaign product URL must appear once in the outreach body."
            )
    return {
        "action_type": OUTREACH_ACTION,
        "target": exact_target,
        "recipient": recipient,
        "subject": subject,
        "body": body,
    }


def _strict_deploy_binding(target: str) -> dict[str, str]:
    company_target = _company_target_config(DEPLOY_ACTION, target)
    account_id = str(company_target.get("account_id") or "").strip()
    project = str(company_target.get("project") or "").strip()
    ref = str(company_target.get("ref") or "").strip()
    if os.environ.get("REVENUE_DEPLOY_CREDENTIAL_OWNERSHIP", "").strip() != "company_service":
        raise RevenueActionDenied(
            "NEEDS HUMAN: set REVENUE_DEPLOY_CREDENTIAL_OWNERSHIP=company_service for a company-owned deploy credential."
        )
    if not account_id or not project or not re.fullmatch(r"[0-9a-f]{40}", ref):
        raise RevenueActionDenied(
            "The company deploy target must pin one account, project, and immutable lowercase 40-hex ref."
        )
    if (
        os.environ.get("REVENUE_DEPLOY_VERCEL_ACCOUNT_ID", "").strip() != account_id
        or os.environ.get("VERCEL_TEAM_ID", "").strip() != account_id
    ):
        raise RevenueActionDenied(
            "NEEDS HUMAN: the deploy target must match the exact company-owned Vercel team."
        )
    return {"account_id": account_id, "project": project, "ref": ref}


def _canonical_deploy_payload(
    payload: Any,
    *,
    expected_target: str,
) -> dict[str, str]:
    keys = CAMPAIGN_DRAFT_SCHEMAS[DEPLOY_ACTION]
    if not isinstance(payload, Mapping) or set(payload) != keys:
        raise RevenueActionDenied(
            "The deploy draft must contain only action_type, target, account_id, project, and ref."
        )
    if any(not isinstance(payload[key], str) for key in keys):
        raise RevenueActionDenied("Every deploy draft field must be a string.")
    exact_target = _exact_campaign_target(DEPLOY_ACTION, expected_target, payload)
    binding = _strict_deploy_binding(exact_target)
    for key in ("account_id", "project", "ref"):
        if payload[key] != binding[key]:
            raise RevenueActionDenied(
                f"The deploy {key} does not exactly match the company target mapping."
            )
    return {
        "action_type": DEPLOY_ACTION,
        "target": exact_target,
        **binding,
    }


def _canonical_purchase_payload(
    payload: Any,
    *,
    expected_target: str,
    expected_amount_usd: Optional[Any] = None,
) -> dict[str, Any]:
    keys = CAMPAIGN_DRAFT_SCHEMAS[PURCHASE_ACTION]
    if not isinstance(payload, Mapping) or set(payload) != keys:
        raise RevenueActionDenied(
            "The purchase draft must contain only action_type, target, amount_usd, and payload."
        )
    exact_target = _exact_campaign_target(PURCHASE_ACTION, expected_target, payload)
    amount = _money(payload["amount_usd"])
    if amount <= 0:
        raise RevenueActionDenied("The purchase draft requires a positive amount_usd.")
    company_target = _company_target_config(PURCHASE_ACTION, exact_target)
    configured_amount = _money(company_target.get("amount_usd", 0.0))
    if configured_amount <= 0 or abs(amount - configured_amount) > 0.000001:
        raise RevenueActionDenied(
            "The purchase amount does not exactly match the owner-configured company target."
        )
    if expected_amount_usd is not None and abs(amount - _money(expected_amount_usd)) > 0.000001:
        raise RevenueActionDenied(
            "The purchase amount does not exactly match the approved capability."
        )
    return {
        "action_type": PURCHASE_ACTION,
        "target": exact_target,
        "amount_usd": amount,
        "payload": _validated_payload(payload["payload"]),
    }


def _canonical_campaign_payload(
    payload: Any,
    *,
    action_type: str,
    expected_target: str,
    expected_product_url: Optional[str] = None,
    expected_purchase_amount_usd: Optional[Any] = None,
) -> dict[str, Any]:
    normalized_type = str(action_type or "").strip().lower()
    if normalized_type == PUBLISH_ACTION and str(expected_target).startswith("bluesky:"):
        return _canonical_bluesky_campaign_payload(
            payload,
            expected_target=expected_target,
            expected_product_url=expected_product_url,
        )
    if normalized_type == PUBLISH_ACTION:
        return _canonical_publish_webhook_payload(
            payload,
            expected_target=expected_target,
            expected_product_url=expected_product_url,
        )
    if normalized_type == OUTREACH_ACTION:
        return _canonical_outreach_payload(
            payload,
            expected_target=expected_target,
            expected_product_url=expected_product_url,
        )
    if normalized_type == DEPLOY_ACTION:
        return _canonical_deploy_payload(payload, expected_target=expected_target)
    if normalized_type == PURCHASE_ACTION:
        return _canonical_purchase_payload(
            payload,
            expected_target=expected_target,
            expected_amount_usd=expected_purchase_amount_usd,
        )
    raise RevenueActionDenied("The campaign draft uses an unsupported action type.")


def parse_campaign_draft(
    text: Any,
    *,
    action_type: str,
    target: str,
    product_url: Optional[str] = None,
) -> dict[str, Any]:
    """Parse one reviewable worker envelope into one exact canonical action.

    The envelope may have surrounding whitespace, but it may not contain prose or
    any JSON fields beyond the values that reach the selected adapter.  The returned
    digest is SHA-256 over deterministic canonical JSON.
    """
    if not isinstance(text, str):
        raise RevenueActionDenied("The campaign draft worker output must be text.")
    envelope = text.strip()
    if not envelope.startswith(CAMPAIGN_DRAFT_JSON_PREFIX):
        raise RevenueActionDenied(
            f"The campaign draft must start with {CAMPAIGN_DRAFT_JSON_PREFIX}"
        )
    encoded = envelope[len(CAMPAIGN_DRAFT_JSON_PREFIX):].strip()
    if not encoded:
        raise RevenueActionDenied("The campaign draft JSON object is missing.")
    try:
        parsed = json.loads(encoded, object_pairs_hook=_campaign_draft_object)
    except RevenueActionDenied:
        raise
    except (TypeError, ValueError) as exc:
        raise RevenueActionDenied(
            "The campaign draft envelope contains invalid JSON."
        ) from exc
    canonical = _canonical_campaign_payload(
        parsed,
        action_type=action_type,
        expected_target=target,
        expected_product_url=product_url,
    )
    return {
        "payload": canonical,
        "payload_digest": _payload_digest(canonical),
    }


def execute_approved_campaign_draft(
    capability: Mapping[str, Any],
    run_id: str,
    parsed: Mapping[str, Any],
    *,
    dry_run: bool = False,
) -> str:
    """Execute one already-reviewed canonical payload under a verified capability."""
    authorized_tools = tool_names_for_capability(capability)
    if len(authorized_tools) != 1:
        raise RevenueActionDenied("The capability does not authorize one exact adapter.")
    if not isinstance(parsed, Mapping) or set(parsed) != {"payload", "payload_digest"}:
        raise RevenueActionDenied("The approved campaign draft is not canonical.")
    action_type = str(capability.get("action_type") or "").strip().lower()
    expected_target = str(capability.get("target") or "")
    payload = _canonical_campaign_payload(
        parsed["payload"],
        action_type=action_type,
        expected_target=expected_target,
        expected_purchase_amount_usd=(
            capability.get("purchase_requested_usd")
            if action_type == PURCHASE_ACTION
            else None
        ),
    )
    supplied_digest = parsed["payload_digest"]
    actual_digest = _payload_digest(payload)
    if (
        not isinstance(supplied_digest, str)
        or not re.fullmatch(r"[a-f0-9]{64}", supplied_digest)
        or not hmac.compare_digest(supplied_digest, actual_digest)
    ):
        raise RevenueActionDenied(
            "The approved campaign draft digest does not match its payload."
        )
    token = set_campaign_action_context(
        capability,
        run_id,
        dry_run=dry_run,
        approved_payload_digest=supplied_digest,
    )
    try:
        if authorized_tools == {CAMPAIGN_PUBLISH_BLUESKY_TOOL}:
            return publish_bluesky(
                payload["target"],
                payload["text"],
                payload["url"] or None,
                dry_run=dry_run,
            )
        if authorized_tools == {CAMPAIGN_PUBLISH_WEBHOOK_TOOL}:
            return publish_webhook(
                payload["target"], payload["payload"], dry_run=dry_run
            )
        if authorized_tools == {CAMPAIGN_SEND_OUTREACH_EMAIL_TOOL}:
            return send_outreach_email(
                payload["recipient"],
                payload["subject"],
                payload["body"],
                dry_run=dry_run,
                campaign_target=payload["target"],
            )
        if authorized_tools == {CAMPAIGN_DEPLOY_VERCEL_TOOL}:
            return deploy_vercel(payload["target"], dry_run=dry_run)
        if authorized_tools == {CAMPAIGN_PURCHASE_WEBHOOK_TOOL}:
            return purchase_webhook(
                payload["target"],
                payload["amount_usd"],
                payload["payload"],
                dry_run=dry_run,
            )
        raise RevenueActionDenied("The approved campaign adapter is unsupported.")
    finally:
        reset_campaign_action_context(token)


def _validated_payload(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise RevenueActionDenied("The webhook payload must be a JSON object.")
    result = dict(payload)
    if _contains_sensitive_key(result):
        raise RevenueActionDenied(
            "Webhook payloads may not contain credentials, payment-card data, or secrets."
        )
    rendered = _canonical_json(result)
    _assert_public_content(rendered, "webhook payload")
    if len(rendered.encode("utf-8")) > _WEBHOOK_PAYLOAD_LIMIT:
        raise RevenueActionDenied("The webhook payload exceeds the 20 KB safety limit.")
    return result


def _safe_receipt(value: Any) -> str:
    text = str(value or "").strip()[:200]
    if _contains_sensitive_text(text):
        # Provider-controlled receipt fields are never trusted as safe log text.
        # Keep a correlation handle without persisting the suspicious value.
        return "redacted-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return _SAFE_RECEIPT_RE.sub("_", text)


def _sanitized_provider_receipt(
    receipt: Optional[Mapping[str, Any]],
) -> Optional[dict[str, str]]:
    if not isinstance(receipt, Mapping):
        return None
    safe: dict[str, str] = {}
    for raw_key, raw_value in list(receipt.items())[:10]:
        key = _SAFE_RECEIPT_RE.sub("_", str(raw_key or "").strip())[:50]
        if not key or _SENSITIVE_KEY_RE.search(key):
            continue
        value = _safe_receipt(raw_value)
        if value:
            safe[key] = value
    return safe or None


def _bounded_response_json(response: Any) -> Optional[Mapping[str, Any]]:
    """Parse at most the bounded response body from a streamed provider response."""

    chunks: list[bytes] = []
    size = 0
    iterator = getattr(response, "iter_content", None)
    if callable(iterator):
        for chunk in iterator(chunk_size=8192):
            if not chunk:
                continue
            encoded = bytes(chunk)
            size += len(encoded)
            if size > _WEBHOOK_RESPONSE_LIMIT:
                return None
            chunks.append(encoded)
        body = b"".join(chunks)
    else:
        body = bytes(getattr(response, "content", b"") or b"")
        if len(body) > _WEBHOOK_RESPONSE_LIMIT:
            return None
    if not body:
        return None
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _close_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _redact_evidence(value: Any) -> str:
    text = str(value or "").strip()
    try:
        import autonomous_workflow

        text = str(autonomous_workflow.redact_secrets(text))
    except Exception:
        # Do not make signal recording capable of breaking an already-completed
        # action.  This fallback removes common credential-shaped assignments.
        text = re.sub(
            r"(?i)(token|secret|password|api[_-]?key)\s*[:=]\s*\S+",
            r"\1=[REDACTED]",
            text,
        )
    return text[:_SIGNAL_EVIDENCE_LIMIT]


def _normalize_target(value: Any) -> str:
    target = str(value or "").strip()
    if not target or len(target) > 500:
        raise RevenueActionDenied("An exact, bounded campaign target is required.")
    return target


def _email_target(value: Any) -> str:
    target = _normalize_target(value)
    if target.count("@") != 1 or any(char.isspace() for char in target):
        raise RevenueActionDenied("The outreach target must be one exact email address.")
    return target


def _active_for(action_type: str, target: str, purchase_amount_usd: float) -> CampaignActionContext:
    context = current_campaign_action_context()
    if context is None:
        raise RevenueActionDenied(
            "No owner-approved revenue campaign capability is active for this task."
        )
    capability = context.capability
    if str(capability.get("action_type") or "").lower() != action_type:
        raise RevenueActionDenied("The active campaign capability is for a different action type.")
    if str(capability.get("target") or "") != target:
        raise RevenueActionDenied("The requested target does not exactly match the campaign capability.")
    if action_type == PURCHASE_ACTION:
        requested = _money(capability.get("purchase_requested_usd", 0.0))
        if abs(requested - purchase_amount_usd) > 0.000001:
            raise RevenueActionDenied("The purchase amount does not match the approved capability.")
    return context


def _company_target_config(action_type: str, target: str) -> dict[str, Any]:
    """Resolve an exact action/target to an explicitly company-owned account.

    Example (stored as one Railway secret):
    ``{"publish:web:freelancer-cold-email-site":{"account_id":"company-web"}}``.
    There is deliberately no personal-account or provider-default fallback.
    """
    raw = os.environ.get("REVENUE_COMPANY_ACTION_TARGETS", "").strip()
    try:
        configured = json.loads(raw) if raw else {}
    except (TypeError, ValueError) as exc:
        raise RevenueActionDenied(
            "REVENUE_COMPANY_ACTION_TARGETS is not valid JSON."
        ) from exc
    if not isinstance(configured, Mapping):
        raise RevenueActionDenied("Company action targets must be a JSON object.")
    record = configured.get(f"{action_type}:{target}")
    if not isinstance(record, Mapping) or not str(record.get("account_id") or "").strip():
        raise RevenueActionDenied(
            "The exact campaign action/target is not mapped to a company-owned account."
        )
    return dict(record)


def _provider_ready(
    action_type: str,
    target: str,
    *,
    require_review_binding: Optional[bool] = None,
) -> dict[str, Any]:
    if require_review_binding is None:
        context = current_campaign_action_context()
        require_review_binding = bool(
            context is not None and context.approved_payload_digest
        )
    try:
        company_target = _company_target_config(action_type, target)
    except RevenueActionDenied as exc:
        if action_type == PUBLISH_ACTION and target.startswith("bluesky:"):
            raise RevenueActionDenied(_BLUESKY_BOOTSTRAP_MESSAGE) from exc
        raise
    account_id = str(company_target["account_id"]).strip()
    if action_type == OUTREACH_ACTION:
        import google_helpers

        recipient = company_target.get("recipient")
        if require_review_binding and (
            not isinstance(recipient, str)
            or recipient != recipient.strip()
            or _email_target(recipient) != recipient
        ):
            raise RevenueActionDenied(
                "The company outreach target must contain one exact recipient field."
            )
        configured_sender = os.environ.get("REVENUE_OUTREACH_GMAIL_ACCOUNT", "").strip().lower()
        company_accounts = {
            value.strip().lower()
            for value in os.environ.get("REVENUE_COMPANY_GMAIL_ACCOUNTS", "").split(",")
            if value.strip()
        }
        if not configured_sender or configured_sender != account_id.lower() or configured_sender not in company_accounts:
            raise RevenueActionDenied(
                "NEEDS HUMAN: configure and verify an explicitly allowlisted company-owned Gmail account."
            )
        if not google_helpers.TOKEN_FILE.exists() and not os.environ.get("GOOGLE_TOKEN_JSON"):
            raise RevenueActionDenied(
                "NEEDS HUMAN: connect the verified company-owned Gmail account for campaign outreach."
            )
        return company_target
    if action_type == DEPLOY_ACTION:
        import deploy_helpers

        company_team = os.environ.get("REVENUE_DEPLOY_VERCEL_ACCOUNT_ID", "").strip()
        if not company_team or company_team != account_id or os.environ.get("VERCEL_TEAM_ID", "").strip() != company_team:
            raise RevenueActionDenied(
                "NEEDS HUMAN: configure and verify the exact company-owned Vercel team account."
            )
        if not str(company_target.get("project") or "").strip() or not str(company_target.get("ref") or "").strip():
            raise RevenueActionDenied("The company deploy target must pin one Vercel project and ref.")
        if require_review_binding:
            _strict_deploy_binding(target)
        if not deploy_helpers.is_configured():
            raise RevenueActionDenied(
                "NEEDS HUMAN: connect the verified company-owned Vercel account for campaign deploys."
            )
        return company_target
    if action_type == PUBLISH_ACTION and target.startswith("bluesky:"):
        expected_handle = target.split(":", 1)[1].strip().lower()
        configured_handle = os.environ.get("REVENUE_BLUESKY_HANDLE", "").strip().lower()
        app_password = os.environ.get("REVENUE_BLUESKY_APP_PASSWORD", "")
        if (
            not expected_handle
            or configured_handle != expected_handle
            or account_id.lower() != expected_handle
            or not app_password
        ):
            raise RevenueActionDenied(_BLUESKY_BOOTSTRAP_MESSAGE)
        return company_target
    prefix = "PUBLISH" if action_type == PUBLISH_ACTION else "PURCHASE"
    try:
        _validated_webhook_endpoint(
            prefix, require_allowed_host=bool(require_review_binding)
        )
    except RevenueActionDenied as exc:
        raise RevenueActionDenied(
            f"NEEDS HUMAN: the {action_type} adapter webhook is not safely configured."
        ) from exc
    secret = os.environ.get(f"REVENUE_{prefix}_WEBHOOK_SECRET", "")
    configured_account = os.environ.get(f"REVENUE_{prefix}_WEBHOOK_ACCOUNT_ID", "").strip()
    if (
        not secret
        or not configured_account
        or configured_account != account_id
    ):
        raise RevenueActionDenied(
            f"NEEDS HUMAN: the {action_type} adapter requires an HTTPS URL, signing "
            "secret, and matching company-owned account ID."
        )
    if action_type == PURCHASE_ACTION:
        configured_amount = _money(company_target.get("amount_usd", 0.0))
        hard_cap = _purchase_hard_cap()
        if configured_amount <= 0:
            raise RevenueActionDenied(
                "NEEDS HUMAN: the purchase target requires one exact positive amount_usd."
            )
        if hard_cap <= 0 or configured_amount > hard_cap:
            raise RevenueActionDenied(
                "NEEDS HUMAN: the exact purchase target exceeds the independent operator hard cap."
            )
    return company_target


def revenue_action_target_readiness(
    action_type: str,
    target: str,
    *,
    verify_identity: bool = False,
) -> dict[str, Any]:
    """Preflight one exact company target without publishing or writing a claim.

    ``verify_identity=True`` performs a bounded read-only identity check for native
    Gmail, Vercel, or Bluesky adapters. It never creates an account, content, or a
    deployment. Webhook adapters instead prove the configured company account in
    their signed mutation receipt. The returned record is deliberately secret-free
    and suitable for an owner-facing queue confirmation.
    """

    normalized_type = str(action_type or "").strip().lower()
    try:
        normalized_target = _normalize_target(target)
    except RevenueActionDenied as exc:
        return {
            "ready": False,
            "needs_human": True,
            "action_type": normalized_type,
            "target": str(target or "")[:500],
            "identity_verified": False,
            "reason": str(exc),
        }
    if normalized_type not in {
        OUTREACH_ACTION,
        DEPLOY_ACTION,
        PUBLISH_ACTION,
        PURCHASE_ACTION,
    }:
        return {
            "ready": False,
            "needs_human": True,
            "action_type": normalized_type,
            "target": normalized_target,
            "identity_verified": False,
            "reason": "NEEDS HUMAN: choose one supported external action type.",
        }
    try:
        company_target = _provider_ready(
            normalized_type,
            normalized_target,
            require_review_binding=True,
        )
    except RevenueActionDenied as exc:
        reason = (
            _BLUESKY_BOOTSTRAP_MESSAGE
            if normalized_type == PUBLISH_ACTION
            and normalized_target.startswith("bluesky:")
            else str(exc)
        )
        return {
            "ready": False,
            "needs_human": True,
            "action_type": normalized_type,
            "target": normalized_target,
            "identity_verified": False,
            "reason": reason,
        }

    result = {
        "ready": True,
        "needs_human": False,
        "action_type": normalized_type,
        "target": normalized_target,
        "account_id": str(company_target["account_id"]),
        "identity_verified": False,
        "reason": "Exact company-owned target configuration is ready.",
    }
    if normalized_type == OUTREACH_ACTION:
        result["draft_binding"] = {
            "action_type": normalized_type,
            "target": normalized_target,
            "recipient": str(company_target["recipient"]),
        }
    elif normalized_type == DEPLOY_ACTION:
        result["draft_binding"] = {
            "action_type": normalized_type,
            "target": normalized_target,
            "account_id": str(company_target["account_id"]),
            "project": str(company_target["project"]),
            "ref": str(company_target["ref"]),
        }
    elif normalized_type == PURCHASE_ACTION:
        result["draft_binding"] = {
            "action_type": normalized_type,
            "target": normalized_target,
            "amount_usd": _money(company_target["amount_usd"]),
        }
    else:
        result["draft_binding"] = {
            "action_type": normalized_type,
            "target": normalized_target,
        }
    if not verify_identity:
        return result
    if normalized_type == OUTREACH_ACTION:
        try:
            import google_helpers

            profile = (
                google_helpers._gmail_service()
                .users()
                .getProfile(userId="me")
                .execute()
            )
        except Exception as exc:
            result.update(
                ready=False,
                needs_human=True,
                reason=(
                    "NEEDS HUMAN: the company Gmail identity could not be verified "
                    f"({type(exc).__name__})."
                ),
            )
            return result
        actual = str((profile or {}).get("emailAddress") or "").strip().lower()
        expected = str(company_target["account_id"]).strip().lower()
        if actual != expected:
            result.update(
                ready=False,
                needs_human=True,
                reason="NEEDS HUMAN: connected Gmail identity is not the company-owned sender.",
            )
            return result
        result.update(
            identity_verified=True,
            reason="Exact company-owned Gmail sender identity is verified; no message was sent.",
        )
        return result
    if normalized_type == DEPLOY_ACTION:
        try:
            import deploy_helpers

            project, error = deploy_helpers.get_project(str(company_target["project"]))
        except Exception as exc:
            project, error = None, type(exc).__name__
        if error or not isinstance(project, Mapping):
            result.update(
                ready=False,
                needs_human=True,
                reason="NEEDS HUMAN: the company Vercel project identity could not be verified.",
            )
            return result
        actual_account = str(
            project.get("accountId") or project.get("teamId") or ""
        ).strip()
        expected_project = str(company_target["project"]).strip()
        actual_projects = {
            str(project.get("id") or "").strip(),
            str(project.get("name") or "").strip(),
        }
        if (
            actual_account != str(company_target["account_id"]).strip()
            or expected_project not in actual_projects
        ):
            result.update(
                ready=False,
                needs_human=True,
                reason="NEEDS HUMAN: Vercel project identity is not the reviewed company target.",
            )
            return result
        result.update(
            identity_verified=True,
            reason="Exact company-owned Vercel project identity is verified; no deploy was started.",
        )
        return result
    if not (
        normalized_type == PUBLISH_ACTION
        and normalized_target.startswith("bluesky:")
    ):
        result["reason"] = (
            "Exact company-owned webhook configuration is ready; a signed exact "
            "provider receipt is required before success can be persisted."
        )
        return result

    handle = str(company_target["account_id"]).strip().lower()
    try:
        response = requests.post(
            "https://bsky.social/xrpc/com.atproto.server.createSession",
            json={
                "identifier": handle,
                "password": os.environ["REVENUE_BLUESKY_APP_PASSWORD"],
            },
            timeout=20,
            allow_redirects=False,
            stream=True,
        )
        try:
            if not (200 <= int(response.status_code) < 300):
                raise RevenueActionDenied(
                    f"{_BLUESKY_BOOTSTRAP_MESSAGE} Identity verification returned "
                    f"HTTP {response.status_code}."
                )
            session = _bounded_response_json(response)
        finally:
            _close_response(response)
    except RevenueActionDenied as exc:
        result.update(ready=False, needs_human=True, reason=str(exc))
        return result
    except Exception as exc:
        result.update(
            ready=False,
            needs_human=True,
            reason=(
                f"{_BLUESKY_BOOTSTRAP_MESSAGE} Identity verification failed "
                f"({type(exc).__name__})."
            ),
        )
        return result
    actual_handle = str((session or {}).get("handle") or "").strip().lower()
    if actual_handle != handle:
        result.update(
            ready=False,
            needs_human=True,
            reason=f"{_BLUESKY_BOOTSTRAP_MESSAGE} Connected identity did not match.",
        )
        return result
    result.update(
        identity_verified=True,
        reason="Exact company-owned Bluesky identity is verified; no content was published.",
    )
    return result


def _live_capability(
    context: CampaignActionContext,
    action_type: str,
    target: str,
    purchase_amount_usd: float,
) -> Mapping[str, Any]:
    import company_mode

    capability = company_mode.revenue_action_capability(
        action_type,
        target,
        sprint_id=context.campaign_id,
        purchase_amount_usd=purchase_amount_usd,
        policy_revision=str(context.capability.get("policy_revision") or ""),
    )
    if not isinstance(capability, Mapping) or capability.get("allowed") is not True:
        reason = capability.get("reason") if isinstance(capability, Mapping) else "invalid capability"
        raise RevenueActionDenied(f"Campaign policy denied the action: {str(reason)[:300]}")
    if str(capability.get("campaign_id") or "") != context.campaign_id:
        raise RevenueActionDenied("The live campaign no longer matches the task capability.")
    if str(capability.get("campaign_status") or "").lower() != "active":
        raise RevenueActionDenied("The live campaign is no longer active.")
    if str(capability.get("action_type") or "").lower() != action_type:
        raise RevenueActionDenied("The live campaign returned a different action type.")
    if str(capability.get("target") or "") != target:
        raise RevenueActionDenied("The live campaign returned a different exact target.")
    policy_revision = str(context.capability.get("policy_revision") or "")
    if (
        str(capability.get("policy_revision") or "") != policy_revision
        or str(capability.get("requested_policy_revision") or "") != policy_revision
    ):
        raise RevenueActionDenied("The live campaign policy revision changed before execution.")
    return capability


def _idempotency_key(
    context: CampaignActionContext,
    action_type: str,
    target: str,
    purchase_amount_usd: float,
) -> str:
    # One sprint run may perform at most one external action for its exact
    # action/target/amount grant.  The payload digest is journal metadata, not part
    # of this key: changing content during the same run must replay the first claim
    # rather than silently creating a second outbound action.
    material = "\n".join((
        context.campaign_id,
        context.run_id,
        action_type,
        target,
        str(context.capability.get("policy_revision") or ""),
        f"{purchase_amount_usd:.6f}",
    ))
    return "rev_" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _claim(
    context: CampaignActionContext,
    action_type: str,
    target: str,
    payload_digest: str,
    purchase_amount_usd: float,
) -> Mapping[str, Any]:
    import company_mode

    key = _idempotency_key(context, action_type, target, purchase_amount_usd)
    return company_mode.claim_revenue_action(
        action_type,
        target,
        context.run_id,
        sprint_id=context.campaign_id,
        purchase_amount_usd=purchase_amount_usd,
        policy_revision=str(context.capability.get("policy_revision") or ""),
        approved_payload_digest=context.approved_payload_digest,
        idempotency_key=key,
        metadata={
            "payload_digest": payload_digest,
            "executor": "revenue_actions.v1",
        },
    )


def _complete(
    context: CampaignActionContext,
    claim: Mapping[str, Any],
    outcome: ProviderOutcome,
) -> Mapping[str, Any]:
    import company_mode

    result = str(outcome.result or "")[:_ACTION_RESULT_LIMIT]
    return company_mode.complete_revenue_action(
        str(claim["id"]),
        outcome.status,
        sprint_id=context.campaign_id,
        actual_purchase_usd=outcome.actual_purchase_usd,
        result=result,
        provider_receipt=_sanitized_provider_receipt(outcome.provider_receipt),
    )


def _record_webhook_signals(
    context: CampaignActionContext,
    response_json: Optional[Mapping[str, Any]],
) -> int:
    """Record a bounded set of provider metrics after action completion.

    Malformed signals are skipped.  Signal ingestion is deliberately independent
    from action completion so a metrics issue cannot turn a successful external
    action into an ambiguous outcome.
    """
    if not isinstance(response_json, Mapping):
        return 0
    raw_signals = response_json.get("signals")
    if not isinstance(raw_signals, list) or len(raw_signals) > _MAX_SIGNALS_PER_RESPONSE:
        return 0

    import company_mode

    recorded = 0
    for raw in raw_signals:
        if not isinstance(raw, Mapping):
            continue
        signal_type = str(raw.get("type") or "").strip().lower()
        if signal_type not in _SIGNAL_TYPES:
            continue
        try:
            count = int(raw.get("count", 1))
            value_usd = _money(raw.get("value_usd", 0.0))
        except (RevenueActionDenied, TypeError, ValueError):
            continue
        if count < 0 or count > 10_000 or value_usd > 1_000_000:
            continue
        try:
            company_mode.record_revenue_signal(
                signal_type,
                sprint_id=context.campaign_id,
                run_id=context.run_id,
                count=count,
                value_usd=value_usd,
                evidence=_redact_evidence(raw.get("evidence", "")),
            )
        except Exception as exc:
            logger.warning(
                "Revenue signal recording failed after completed action (%s).",
                type(exc).__name__,
            )
            continue
        recorded += 1
    return recorded


def _execute(
    action_type: str,
    target: str,
    payload: Mapping[str, Any],
    provider_call: Callable[[Mapping[str, Any], Mapping[str, Any]], ProviderOutcome],
    *,
    purchase_amount_usd: float = 0.0,
    dry_run: Optional[bool] = None,
) -> str:
    target = _normalize_target(target)
    amount = _money(purchase_amount_usd)
    context = _active_for(action_type, target, amount)

    if action_type == PURCHASE_ACTION:
        hard_cap = _purchase_hard_cap()
        if hard_cap <= 0:
            raise RevenueActionDenied(
                "Automated purchasing is disabled by the zero-dollar hard cap."
            )
        if amount <= 0 or amount > hard_cap:
            raise RevenueActionDenied(
                f"The purchase amount exceeds the configured ${hard_cap:.2f} hard cap."
            )

    digest = _payload_digest(payload)
    if context.approved_payload_digest and not hmac.compare_digest(
        context.approved_payload_digest, digest
    ):
        raise RevenueActionDenied(
            "The adapter payload does not match the exact reviewed campaign draft."
        )
    company_target = _provider_ready(action_type, target)
    _live_capability(context, action_type, target, amount)
    # A tool argument can make a live context safer, but can never turn a dry-run
    # context live.  This prevents model-supplied dry_run=False from bypassing the
    # runtime's safety mode.
    effective_dry_run = bool(context.dry_run or dry_run)
    if effective_dry_run:
        return (
            f"DRY RUN: campaign policy allows {action_type} for exact target {target}; "
            "no claim was written and no external request was made."
        )

    try:
        claim = _claim(context, action_type, target, digest, amount)
    except Exception as exc:
        raise RevenueActionDenied(
            f"Campaign action claim was denied ({type(exc).__name__})."
        ) from exc
    if not isinstance(claim, Mapping):
        raise RevenueActionDenied("Campaign action claim returned an invalid record.")
    if claim.get("idempotent_replay"):
        status = str(claim.get("status") or "unknown")
        return (
            f"Campaign action already has a persisted {status} claim; no external "
            "request was repeated."
        )
    if str(claim.get("status") or "") != "claimed" or not claim.get("id"):
        raise RevenueActionDenied("Campaign action did not receive a valid persistent claim.")

    try:
        outcome = provider_call(claim, company_target)
        if outcome.status not in {"succeeded", "failed", "uncertain"}:
            outcome = ProviderOutcome(
                "uncertain",
                "Provider returned an invalid outcome; manual inspection required.",
                actual_purchase_usd=amount if action_type == PURCHASE_ACTION else None,
            )
    except Exception as exc:
        logger.warning(
            "Campaign provider outcome is uncertain (%s; action=%s).",
            type(exc).__name__,
            action_type,
        )
        outcome = ProviderOutcome(
            "uncertain",
            f"Provider outcome uncertain ({type(exc).__name__}); do not retry blindly.",
            actual_purchase_usd=amount if action_type == PURCHASE_ACTION else None,
        )

    try:
        _complete(context, claim, outcome)
    except Exception as exc:
        logger.error(
            "Campaign action completion journal failed (%s; action=%s).",
            type(exc).__name__,
            action_type,
        )
        return (
            f"External {action_type} outcome is {outcome.status}, but journal completion "
            "failed. Treat the claim as uncertain and do not retry."
        )

    signal_count = 0
    if outcome.status == "succeeded":
        signal_count = _record_webhook_signals(context, outcome.response_json)
    suffix = f" Recorded {signal_count} revenue signal(s)." if signal_count else ""
    return f"Campaign {action_type} {outcome.status}: {outcome.result}{suffix}"


def send_outreach_email(
    to: str,
    subject: str,
    body: str,
    *,
    dry_run: Optional[bool] = None,
    campaign_target: Optional[str] = None,
) -> str:
    """Send one policy-allowlisted Gmail message after a persistent claim."""
    recipient = _email_target(to)
    target = _normalize_target(campaign_target) if campaign_target is not None else recipient
    subject_text = str(subject or "").strip()
    body_text = str(body or "").strip()
    if not subject_text or len(subject_text) > 300:
        raise RevenueActionDenied("Outreach subject is required and limited to 300 characters.")
    if not body_text or len(body_text) > 20_000:
        raise RevenueActionDenied("Outreach body is required and limited to 20,000 characters.")
    _assert_public_content(subject_text, "outreach subject")
    _assert_public_content(body_text, "outreach body")
    payload = {
        "action_type": OUTREACH_ACTION,
        "target": target,
        "recipient": recipient,
        "subject": subject_text,
        "body": body_text,
    }

    def provider_call(
        _claim: Mapping[str, Any], company_target: Mapping[str, Any]
    ) -> ProviderOutcome:
        import google_helpers

        expected_sender = str(company_target["account_id"]).strip().lower()
        try:
            service = google_helpers._gmail_service()
            profile = service.users().getProfile(userId="me").execute()
        except Exception:
            return ProviderOutcome(
                "uncertain",
                "Could not verify the company Gmail identity; no send was attempted.",
            )
        actual_sender = str((profile or {}).get("emailAddress") or "").strip().lower()
        if actual_sender != expected_sender:
            return ProviderOutcome(
                "failed",
                "NEEDS HUMAN: connected Gmail identity does not match the company-owned account.",
            )
        configured_recipient = company_target.get("recipient")
        if configured_recipient is not None and configured_recipient != recipient:
            return ProviderOutcome(
                "failed",
                "NEEDS HUMAN: the reviewed recipient no longer matches the company target mapping.",
            )
        result = str(
            google_helpers.send_email(
                recipient,
                subject_text,
                body_text,
                service=service,
            )
            or ""
        )
        if result.startswith("Email sent to "):
            match = re.search(r"\(id ([^)]+)\)", result)
            receipt = _safe_receipt(match.group(1) if match else "accepted")
            return ProviderOutcome(
                "succeeded",
                f"Gmail accepted message {receipt}.",
                provider_receipt={"message_id": receipt},
            )
        if "isn't connected" in result or "credentials are invalid" in result:
            return ProviderOutcome("failed", "Gmail rejected the request before sending.")
        return ProviderOutcome(
            "uncertain",
            "Gmail did not return a definitive send receipt; do not retry blindly.",
        )

    return _execute(
        OUTREACH_ACTION, target, payload, provider_call, dry_run=dry_run
    )


def deploy_vercel(
    target: str,
    *,
    dry_run: Optional[bool] = None,
) -> str:
    """Deploy the company-owned Vercel project/ref pinned to one logical target."""
    exact_target = _normalize_target(target)
    context = current_campaign_action_context()
    if context is not None and context.approved_payload_digest:
        binding = _strict_deploy_binding(exact_target)
    else:
        configured = _company_target_config(DEPLOY_ACTION, exact_target)
        binding = {
            "account_id": str(configured.get("account_id") or "").strip(),
            "project": str(configured.get("project") or "").strip(),
            "ref": str(configured.get("ref") or "").strip(),
        }
    payload = {
        "action_type": DEPLOY_ACTION,
        "target": exact_target,
        **binding,
    }

    def provider_call(
        _claim: Mapping[str, Any], company_target: Mapping[str, Any]
    ) -> ProviderOutcome:
        import deploy_helpers

        project_name = str(company_target["project"]).strip()
        ref_name = str(company_target["ref"]).strip()
        account_id = str(company_target["account_id"]).strip()
        if context is not None and context.approved_payload_digest:
            project_record, identity_error = deploy_helpers.get_project(project_name)
            if identity_error or not isinstance(project_record, Mapping):
                return ProviderOutcome(
                    "failed",
                    "NEEDS HUMAN: Vercel project identity could not be verified; no deploy was attempted.",
                )
            actual_project_ids = {
                str(project_record.get("id") or "").strip(),
                str(project_record.get("name") or "").strip(),
            }
            actual_account_id = str(
                project_record.get("accountId")
                or project_record.get("teamId")
                or ""
            ).strip()
            if project_name not in actual_project_ids or actual_account_id != account_id:
                return ProviderOutcome(
                    "failed",
                    "NEEDS HUMAN: Vercel project identity did not match the reviewed company account/project.",
                )
        result, err = deploy_helpers.deploy(project_name, ref_name, "production")
        if err:
            # deploy_helpers performs both preflight reads and the deployment POST
            # behind one call.  It cannot prove which stage failed, so after the
            # persistent claim we conservatively forbid an automatic replay.
            return ProviderOutcome(
                "uncertain",
                "Vercel outcome is uncertain; inspect deployments before retrying.",
            )
        raw_receipt = (result or {}).get("id") if isinstance(result, Mapping) else None
        receipt = _safe_receipt(raw_receipt)
        if (
            not isinstance(raw_receipt, str)
            or not raw_receipt
            or receipt != raw_receipt
        ):
            return ProviderOutcome(
                "uncertain",
                "Vercel returned no verifiable deployment ID; inspect deployments before retrying.",
            )
        url = str((result or {}).get("url") or "")
        parsed_url = urlparse(url)
        safe_url = (
            url
            if parsed_url.scheme == "https"
            and parsed_url.hostname
            and parsed_url.username is None
            and parsed_url.password is None
            and not parsed_url.query
            and not parsed_url.fragment
            else ""
        )
        provider_receipt = {"deployment_id": receipt}
        if safe_url:
            provider_receipt["url"] = safe_url
        return ProviderOutcome(
            "succeeded",
            f"Vercel queued deployment {receipt}{f' at {safe_url}' if safe_url else ''}.",
            provider_receipt=provider_receipt,
        )

    return _execute(DEPLOY_ACTION, exact_target, payload, provider_call, dry_run=dry_run)


def _bluesky_link_facet(text: str, url: Optional[str]) -> list[dict[str, Any]]:
    if not url:
        return []
    link = str(url).strip()
    parsed = urlparse(link)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RevenueActionDenied("The Bluesky link must be one exact HTTPS URL.")
    if text.count(link) != 1:
        raise RevenueActionDenied("The Bluesky link must appear exactly once in the post text.")
    start = text.index(link)
    byte_start = len(text[:start].encode("utf-8"))
    byte_end = byte_start + len(link.encode("utf-8"))
    return [{
        "index": {"byteStart": byte_start, "byteEnd": byte_end},
        "features": [{"$type": "app.bsky.richtext.facet#link", "uri": link}],
    }]


def publish_bluesky(
    target: str,
    text: str,
    url: Optional[str] = None,
    *,
    dry_run: Optional[bool] = None,
) -> str:
    """Create one standalone post on an exact company-owned Bluesky account.

    This adapter cannot create accounts, reply, like, follow, or send messages.  The
    app password and session JWT remain in memory only; the journal stores only the
    payload digest and the returned URI/CID.
    """
    exact_target = _normalize_target(target)
    if not exact_target.startswith("bluesky:"):
        raise RevenueActionDenied("The Bluesky target must use bluesky:<handle>.")
    post_text = str(text or "").strip()
    if not post_text or len(post_text) > 300 or len(post_text.encode("utf-8")) > 1200:
        raise RevenueActionDenied("A Bluesky post must contain 1-300 bounded characters.")
    if any(ord(char) < 32 and char not in {"\n", "\t"} for char in post_text):
        raise RevenueActionDenied("The Bluesky post contains unsupported control characters.")
    _assert_public_content(post_text, "Bluesky post")
    if url:
        _assert_public_content(url, "Bluesky link")
    facets = _bluesky_link_facet(post_text, url)
    payload = {
        "action_type": PUBLISH_ACTION,
        "target": exact_target,
        "text": post_text,
        "url": str(url or ""),
    }

    def provider_call(
        _claim: Mapping[str, Any], company_target: Mapping[str, Any]
    ) -> ProviderOutcome:
        handle = str(company_target["account_id"]).strip().lower()
        session_response = requests.post(
            "https://bsky.social/xrpc/com.atproto.server.createSession",
            json={
                "identifier": handle,
                "password": os.environ["REVENUE_BLUESKY_APP_PASSWORD"],
            },
            timeout=20,
            allow_redirects=False,
            stream=True,
        )
        try:
            if not (200 <= int(session_response.status_code) < 300):
                return ProviderOutcome(
                    "failed",
                    f"Bluesky rejected the company account session with HTTP {session_response.status_code}.",
                )
            session = _bounded_response_json(session_response)
        finally:
            _close_response(session_response)
        if not isinstance(session, Mapping):
            return ProviderOutcome("failed", "Bluesky returned an invalid session response.")
        actual_handle = str(session.get("handle") or "").strip().lower()
        access_jwt = str(session.get("accessJwt") or "")
        did = str(session.get("did") or "")
        if actual_handle != handle or not access_jwt or not did:
            return ProviderOutcome(
                "failed",
                "NEEDS HUMAN: Bluesky session identity did not match the company-owned account.",
            )

        record: dict[str, Any] = {
            "$type": "app.bsky.feed.post",
            "text": post_text,
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if facets:
            record["facets"] = facets
        post_response = requests.post(
            "https://bsky.social/xrpc/com.atproto.repo.createRecord",
            headers={
                "Authorization": f"Bearer {access_jwt}",
                "Content-Type": "application/json",
            },
            json={
                "repo": did,
                "collection": "app.bsky.feed.post",
                "record": record,
            },
            timeout=20,
            allow_redirects=False,
            stream=True,
        )
        try:
            if not (200 <= int(post_response.status_code) < 300):
                return ProviderOutcome(
                    "uncertain",
                    f"Bluesky feed-post outcome is uncertain after HTTP {post_response.status_code}; do not retry blindly.",
                )
            posted = _bounded_response_json(post_response)
        finally:
            _close_response(post_response)
        if not isinstance(posted, Mapping):
            return ProviderOutcome(
                "uncertain",
                "Bluesky accepted the request but returned no verifiable post receipt.",
            )
        raw_uri = posted.get("uri")
        raw_cid = posted.get("cid")
        uri_prefix = f"at://{did}/app.bsky.feed.post/"
        receipt_is_exact = (
            isinstance(raw_uri, str)
            and isinstance(raw_cid, str)
            and raw_uri.startswith(uri_prefix)
            and bool(raw_uri[len(uri_prefix):])
            and bool(raw_cid)
            and "/" not in raw_uri[len(uri_prefix):]
            and "?" not in raw_uri[len(uri_prefix):]
            and "#" not in raw_uri[len(uri_prefix):]
            and _safe_receipt(raw_uri) == raw_uri
            and _safe_receipt(raw_cid) == raw_cid
        )
        if not receipt_is_exact:
            return ProviderOutcome(
                "uncertain",
                "Bluesky accepted the request but returned a post receipt that did "
                "not match the authenticated company repository and feed collection.",
            )
        uri = raw_uri
        cid = raw_cid
        return ProviderOutcome(
            "succeeded",
            f"Bluesky created uri={uri} cid={cid}.",
            provider_receipt={"uri": uri, "cid": cid},
        )

    return _execute(
        PUBLISH_ACTION,
        exact_target,
        payload,
        provider_call,
        dry_run=dry_run,
    )


def fetch_bluesky_engagement(actions: Any) -> list[dict[str, Any]]:
    """Fetch public engagement counts for exact persisted Bluesky receipts.

    This read-only adapter accepts only successful Bluesky journal actions and
    matches every public response back to both its URI and CID.  It deliberately
    uses the public endpoint without credentials and fails closed on any partial,
    duplicated, malformed, or identity-mismatched response.
    """

    if isinstance(actions, (str, bytes, bytearray, Mapping)):
        raise RevenueActionDenied(
            "Bluesky engagement actions must be a bounded iterable of action mappings."
        )
    try:
        iterator = iter(actions)
    except TypeError as exc:
        raise RevenueActionDenied(
            "Bluesky engagement actions must be a bounded iterable of action mappings."
        ) from exc

    requested: list[dict[str, str]] = []
    action_ids: set[str] = set()
    uris: set[str] = set()
    for raw in iterator:
        if len(requested) >= 20:
            raise RevenueActionDenied(
                "Bluesky engagement fetches are limited to 20 actions."
            )
        if not isinstance(raw, Mapping):
            raise RevenueActionDenied(
                "Every Bluesky engagement action must be a mapping."
            )
        receipt = raw.get("provider_receipt")
        action_id = raw.get("id")
        uri = receipt.get("uri") if isinstance(receipt, Mapping) else None
        cid = receipt.get("cid") if isinstance(receipt, Mapping) else None
        if (
            raw.get("status") != "succeeded"
            or raw.get("action_type") != PUBLISH_ACTION
            or not isinstance(raw.get("target"), str)
            or not raw["target"].startswith("bluesky:")
            or not isinstance(action_id, str)
            or not isinstance(uri, str)
            or not isinstance(cid, str)
            or not action_id
            or not uri.startswith("at://")
            or not cid
            or _safe_receipt(action_id) != action_id
            or _safe_receipt(uri) != uri
            or _safe_receipt(cid) != cid
        ):
            raise RevenueActionDenied(
                "Each engagement lookup requires one successful Bluesky action with an exact URI/CID receipt."
            )
        if action_id in action_ids or uri in uris:
            raise RevenueActionDenied(
                "Bluesky engagement actions must have unique action IDs and post URIs."
            )
        action_ids.add(action_id)
        uris.add(uri)
        requested.append({"action_id": action_id, "uri": uri, "cid": cid})

    if not requested:
        return []

    try:
        response = requests.get(
            "https://public.api.bsky.app/xrpc/app.bsky.feed.getPosts",
            params=[("uris", entry["uri"]) for entry in requested],
            timeout=20,
            allow_redirects=False,
            stream=True,
        )
    except Exception as exc:
        raise RevenueActionDenied(
            f"Bluesky engagement request failed ({type(exc).__name__})."
        ) from None

    try:
        try:
            status_code = int(response.status_code)
        except (TypeError, ValueError) as exc:
            raise RevenueActionDenied(
                "Bluesky engagement response had an invalid HTTP status."
            ) from exc
        if not 200 <= status_code < 300:
            raise RevenueActionDenied(
                f"Bluesky engagement request failed with HTTP {status_code}."
            )
        payload = _bounded_response_json(response)
    finally:
        _close_response(response)

    posts = payload.get("posts") if isinstance(payload, Mapping) else None
    if not isinstance(posts, list) or len(posts) != len(requested):
        raise RevenueActionDenied(
            "Bluesky engagement response did not contain one exact post per requested URI."
        )

    expected_by_uri = {entry["uri"]: entry for entry in requested}
    returned_by_uri: dict[str, dict[str, Any]] = {}
    count_fields = {
        "like_count": "likeCount",
        "reply_count": "replyCount",
        "repost_count": "repostCount",
        "quote_count": "quoteCount",
    }
    for post in posts:
        if not isinstance(post, Mapping):
            raise RevenueActionDenied("Bluesky engagement response contains a malformed post.")
        uri = post.get("uri")
        cid = post.get("cid")
        if (
            not isinstance(uri, str)
            or not isinstance(cid, str)
            or uri not in expected_by_uri
            or uri in returned_by_uri
            or cid != expected_by_uri[uri]["cid"]
        ):
            raise RevenueActionDenied(
                "Bluesky engagement response did not match the requested URI/CID receipts."
            )
        normalized_counts: dict[str, int] = {}
        for output_name, provider_name in count_fields.items():
            value = post.get(provider_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RevenueActionDenied(
                    "Bluesky engagement response contains an invalid count."
                )
            normalized_counts[output_name] = value
        returned_by_uri[uri] = {
            "action_id": expected_by_uri[uri]["action_id"],
            "uri": uri,
            "cid": cid,
            **normalized_counts,
        }

    if set(returned_by_uri) != set(expected_by_uri):
        raise RevenueActionDenied(
            "Bluesky engagement response omitted a requested post."
        )
    return [returned_by_uri[entry["uri"]] for entry in requested]


def _webhook_provider(
    action_type: str,
    target: str,
    payload: Mapping[str, Any],
    purchase_amount_usd: float,
) -> Callable[[Mapping[str, Any], Mapping[str, Any]], ProviderOutcome]:
    prefix = "PUBLISH" if action_type == PUBLISH_ACTION else "PURCHASE"

    def provider_call(
        claim: Mapping[str, Any], company_target: Mapping[str, Any]
    ) -> ProviderOutcome:
        context = current_campaign_action_context()
        url = _validated_webhook_endpoint(
            prefix,
            require_allowed_host=bool(
                context is not None and context.approved_payload_digest
            ),
        )
        secret = os.environ[f"REVENUE_{prefix}_WEBHOOK_SECRET"].encode("utf-8")
        timestamp = str(int(time.time()))
        claim_id = str(claim["id"])
        idempotency_key = str(claim.get("idempotency_key") or "")
        reviewed_payload = {
            "action_type": action_type,
            "target": target,
            **(
                {"amount_usd": purchase_amount_usd}
                if action_type == PURCHASE_ACTION
                else {}
            ),
            "payload": payload,
        }
        claim_digest = str(
            ((claim.get("metadata") or {}).get("payload_digest") or "")
        ).strip().lower()
        payload_digest = claim_digest or _payload_digest(reviewed_payload)
        envelope = {
            "action_id": claim_id,
            "idempotency_key": idempotency_key,
            "action_type": action_type,
            "target": target,
            "provider_account_id": str(company_target["account_id"]),
            "amount_usd": purchase_amount_usd,
            "payload_digest": payload_digest,
            "payload": payload,
        }
        body = _canonical_json(envelope).encode("utf-8")
        signature = hmac.new(secret, timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()
        response = requests.post(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Revenue-Action-Id": claim_id,
                "X-Idempotency-Key": idempotency_key,
                "X-Revenue-Timestamp": timestamp,
                "X-Revenue-Signature": f"sha256={signature}",
            },
            timeout=20,
            allow_redirects=False,
            stream=True,
        )
        try:
            if not (200 <= int(response.status_code) < 300):
                return ProviderOutcome(
                    "uncertain",
                    f"{action_type.title()} webhook outcome is uncertain after HTTP {response.status_code}; do not retry blindly.",
                    actual_purchase_usd=(
                        purchase_amount_usd if action_type == PURCHASE_ACTION else None
                    ),
                )
            response_json = _bounded_response_json(response)
            response_headers = getattr(response, "headers", {})
        finally:
            _close_response(response)
        reviewed = bool(context is not None and context.approved_payload_digest)
        if reviewed:
            expected_account = str(company_target["account_id"])
            try:
                exact_response = (
                    isinstance(response_json, Mapping)
                    and response_json.get("status") == "succeeded"
                    and response_json.get("action_id") == claim_id
                    and response_json.get("idempotency_key") == idempotency_key
                    and response_json.get("payload_digest") == payload_digest
                    and response_json.get("provider_account_id") == expected_account
                    and abs(
                        _money(response_json.get("amount_usd", -1))
                        - purchase_amount_usd
                    )
                    <= 0.000001
                )
            except RevenueActionDenied:
                exact_response = False
            raw_receipt = (
                response_json.get("receipt_id")
                if isinstance(response_json, Mapping)
                else None
            )
            exact_receipt = (
                isinstance(raw_receipt, str)
                and bool(raw_receipt)
                and _safe_receipt(raw_receipt) == raw_receipt
            )
            response_signature = str(
                response_headers.get("X-Revenue-Response-Signature", "")
                if isinstance(response_headers, Mapping)
                else ""
            )
            signed_body = (
                _canonical_json(response_json).encode("utf-8")
                if isinstance(response_json, Mapping)
                else b""
            )
            expected_signature = "sha256=" + hmac.new(
                secret,
                timestamp.encode("ascii") + b"." + signed_body,
                hashlib.sha256,
            ).hexdigest()
            if (
                not exact_response
                or not exact_receipt
                or not hmac.compare_digest(response_signature, expected_signature)
            ):
                return ProviderOutcome(
                    "uncertain",
                    f"{action_type.title()} webhook returned no exact signed receipt; do not retry blindly.",
                    actual_purchase_usd=(
                        purchase_amount_usd
                        if action_type == PURCHASE_ACTION
                        else None
                    ),
                )
        receipt = "accepted"
        if response_json:
            receipt = _safe_receipt(
                response_json.get("receipt_id") or response_json.get("id") or "accepted"
            )
        return ProviderOutcome(
            "succeeded",
            f"{action_type.title()} webhook accepted receipt {receipt}.",
            actual_purchase_usd=(
                purchase_amount_usd if action_type == PURCHASE_ACTION else None
            ),
            response_json=response_json,
            provider_receipt={"receipt_id": receipt},
        )

    return provider_call


def publish_webhook(
    target: str,
    payload: Mapping[str, Any],
    *,
    dry_run: Optional[bool] = None,
) -> str:
    """Invoke the configured HMAC-signed publish adapter for one exact target."""
    exact_target = _normalize_target(target)
    safe_payload = _validated_payload(payload)
    reviewed_payload = {
        "action_type": PUBLISH_ACTION,
        "target": exact_target,
        "payload": safe_payload,
    }
    return _execute(
        PUBLISH_ACTION,
        exact_target,
        reviewed_payload,
        _webhook_provider(PUBLISH_ACTION, exact_target, safe_payload, 0.0),
        dry_run=dry_run,
    )


def purchase_webhook(
    target: str,
    amount_usd: Any,
    payload: Mapping[str, Any],
    *,
    dry_run: Optional[bool] = None,
) -> str:
    """Invoke a fixed-target purchase adapter inside both campaign and hard caps."""
    exact_target = _normalize_target(target)
    amount = _money(amount_usd)
    safe_payload = _validated_payload(payload)
    reviewed_payload = {
        "action_type": PURCHASE_ACTION,
        "target": exact_target,
        "amount_usd": amount,
        "payload": safe_payload,
    }
    return _execute(
        PURCHASE_ACTION,
        exact_target,
        reviewed_payload,
        _webhook_provider(PURCHASE_ACTION, exact_target, safe_payload, amount),
        purchase_amount_usd=amount,
        dry_run=dry_run,
    )


__all__ = [
    "CAMPAIGN_DRAFT_JSON_PREFIX",
    "CAMPAIGN_TOOL_ACTIONS",
    "CAMPAIGN_TOOL_NAMES",
    "CampaignActionContext",
    "RevenueActionDenied",
    "current_campaign_action_context",
    "deploy_vercel",
    "execute_approved_campaign_draft",
    "fetch_bluesky_engagement",
    "parse_campaign_draft",
    "publish_bluesky",
    "publish_webhook",
    "purchase_webhook",
    "revenue_action_target_readiness",
    "reset_campaign_action_context",
    "send_outreach_email",
    "set_campaign_action_context",
    "tool_names_for_capability",
]

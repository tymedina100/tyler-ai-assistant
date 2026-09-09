"""Opt-in private Codex analysis; never an API-key or automatic retry fallback.

Uses documented codex exec/auth controls. Not enabled in the production poller.
Caller supplies explicit model/effort and freshly observed account quota.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class QuotaEvidence:
    observed_at: float
    used_percent: float
    exhausted: bool = False


class SubscriptionUnavailable(RuntimeError):
    pass


def validate_quota(quota: QuotaEvidence) -> None:
    if (type(quota.observed_at) not in (int, float) or not math.isfinite(quota.observed_at)
            or type(quota.used_percent) not in (int, float) or not math.isfinite(quota.used_percent)
            or type(quota.exhausted) is not bool):
        raise SubscriptionUnavailable("Invalid quota evidence.")
    age = time.time() - quota.observed_at
    if not 0 <= age <= 300 or not 0 <= quota.used_percent <= 90 or quota.exhausted:
        raise SubscriptionUnavailable("Fresh quota with at least 10% reserve is required.")


def analyze(prompt: str, *, binary: str, model: str, effort: str,
            quota: QuotaEvidence | None = None, enabled: bool = False, timeout: int = 90, response_kind: str = "summary") -> dict:
    if response_kind not in {"summary", "miles_judgment"}:
        raise ValueError("Unknown structured analysis kind.")
    if not enabled:
        raise SubscriptionUnavailable("Subscription execution is not enabled.")
    if quota is None:
        from codex_quota import read_quota
        quota = read_quota(binary)
    validate_quota(quota)
    if not model or effort not in {"low", "medium", "high"}:
        raise ValueError("An explicit model and supported effort are required.")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000:
        raise ValueError("Supply 1 to 20000 characters of analysis context.")
    if not 1 <= timeout <= 180:
        raise ValueError("Timeout must be 1 to 180 seconds.")
    if not Path(binary).is_absolute():
        raise ValueError("Use an absolute trusted CLI path.")
    # Allow only login/runtime essentials. Do not inherit API keys or service secrets.
    env = {k: os.environ[k] for k in ("HOME", "PATH", "TMPDIR", "CODEX_HOME", "LANG") if k in os.environ}
    auth = subprocess.run([binary, "login", "status"], env=env, capture_output=True,
                          text=True, timeout=10, check=False)
    if auth.returncode or "Logged in using ChatGPT" not in auth.stdout + auth.stderr:
        raise SubscriptionUnavailable("CLI must already be signed in with ChatGPT.")
    with tempfile.TemporaryDirectory(prefix="tyleros-analysis-") as directory:
        root = Path(directory)
        schema = root / "schema.json"
        output = root / "result.json"
        properties = {"summary": {"type": "string"}}
        if response_kind == "miles_judgment":
            properties.update({key: {"type": "array", "items": {"type": "string"}} for key in ("priorities", "needsTyler", "watch")})
        schema.write_text(json.dumps({"type": "object", "properties": properties,
                                      "required": list(properties), "additionalProperties": False}))
        command = [binary, "exec", "--ignore-user-config", "--strict-config", "--ephemeral",
                   "--skip-git-repo-check", "--sandbox", "read-only", "--cd", directory,
                   "--model", model, "--config", f'model_reasoning_effort="{effort}"',
                   "--config", 'forced_login_method="chatgpt"', "--config", 'model_provider="openai"',
                   "--config", 'web_search="disabled"', "--config", 'approval_policy="never"',
                   "--disable", "shell_tool", "--disable", "apps", "--disable", "plugins",
                   "--disable", "multi_agent", "--output-schema", str(schema),
                   "--output-last-message", str(output), "--json", "-"]
        result = subprocess.run(command, input=prompt, env=env, capture_output=True,
                                text=True, timeout=timeout, check=False)
        if result.returncode:
            raise SubscriptionUnavailable("Codex run failed; no retry or provider fallback was attempted.")
        if not output.exists() or output.stat().st_size > 20000:
            raise SubscriptionUnavailable("Missing or oversized structured result.")
        data = json.loads(output.read_text())
        if not isinstance(data, dict) or set(data) != set(properties) or not isinstance(data["summary"], str) or not 1 <= len(data["summary"]) <= 4000:
            raise SubscriptionUnavailable("Invalid structured result.")
        if response_kind == "miles_judgment" and (len(data["summary"]) > 400 or any(not isinstance(data[key], list) or len(data[key]) > 5 or any(not isinstance(value, str) or not 1 <= len(value.strip()) <= 160 for value in data[key]) for key in ("priorities", "needsTyler", "watch"))):
            raise SubscriptionUnavailable("Invalid Miles judgment.")
        events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        if any(event.get("item", {}).get("type") in {"command_execution", "mcp_tool_call", "web_search", "file_change"} for event in events):
            raise SubscriptionUnavailable("Analysis unexpectedly attempted a tool action.")
        completed = [event for event in events if event.get("type") == "turn.completed"]
        if len(completed) != 1 or any(event.get("type") in {"turn.failed", "error"} for event in events):
            raise SubscriptionUnavailable("Run did not finish exactly one successful turn.")
        return {**({"judgment": data} if response_kind == "miles_judgment" else {"summary": data["summary"]}), "provider": "openai", "product": "codex_chatgpt",
                "model": model, "usage": completed[0].get("usage"), "automaticRetry": False}

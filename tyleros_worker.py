#!/usr/bin/env python3
"""Poll TylerOS as Miles on the Python runtime.

This is not the Telegram group bot and not a specialist framework. It claims
jobs assigned to the Miles role as a named Python *instance* (home-desktop-python,
backup-python, …). Prefer TYLEROS_RUNTIME_CREDENTIAL so TylerOS derives identity
from the credential rather than a kind header. RUNTIME_TOKEN still ticks schedules.

Long-running mode also POSTs a scheduler tick. The worker is a clock, not the
source of truth for when a Miles briefing should exist.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

ROLE = "miles"
RUNTIME_KIND = "python"
EMPTY_TODAY_SUMMARY = "No material Today items."
MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
MATERIAL_ITEM_KEYS = ("overdue", "dueToday", "needsTriage", "upcoming")


def format_briefing_date(iso: str) -> str:
    year, month, day = iso.split("-")
    return f"{int(day)} {MONTHS[int(month) - 1]} {year}"


def today_has_material(context: dict[str, Any]) -> bool:
    for key in MATERIAL_ITEM_KEYS:
        items = context.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and str(item.get("title") or "").strip():
                return True
    food = context.get("expiringSoon")
    if not isinstance(food, list):
        return False
    return any(isinstance(item, dict) and str(item.get("name") or "").strip() for item in food)


def format_today_briefing(context: dict[str, Any]) -> tuple[str, str]:
    today = str(context.get("today") or "")
    title = f"Today briefing — {format_briefing_date(today)}" if today else "Today briefing"

    sections: list[str] = []
    _append_item_section(sections, "Overdue", context.get("overdue"))
    _append_item_section(sections, "Due today", context.get("dueToday"))
    _append_item_section(sections, "Needs triage", context.get("needsTriage"), include_due=False)
    _append_item_section(sections, "Next 7 days", context.get("upcoming"))
    _append_food_section(sections, context.get("expiringSoon"))

    body = f"{title}\n\n" + "\n\n".join(sections) if sections else title
    return title, body


def _append_item_section(
    sections: list[str],
    heading: str,
    items: Any,
    *,
    include_due: bool = True,
) -> None:
    if not isinstance(items, list) or len(items) == 0:
        return
    lines = [f"## {heading}"]
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("title") or "").strip()
        if not name:
            continue
        due = item.get("dueOn") if include_due else None
        lines.append(f"- {name} (due {due})" if due else f"- {name}")
    if len(lines) > 1:
        sections.append("\n".join(lines))


def _append_food_section(sections: list[str], items: Any) -> None:
    if not isinstance(items, list) or len(items) == 0:
        return
    lines = ["## Use soon"]
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        expires = item.get("expiresOn")
        location = item.get("location")
        detail = []
        if location:
            detail.append(str(location))
        if expires:
            detail.append(f"by {expires}")
        suffix = f" ({', '.join(detail)})" if detail else ""
        lines.append(f"- {name}{suffix}")
    if len(lines) > 1:
        sections.append("\n".join(lines))


def identity_headers(environ: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ if environ is None else environ
    headers = {"X-TylerOS-Role": ROLE}
    if env.get("TYLEROS_RUNTIME_CREDENTIAL", "").strip() == "":
        headers["X-TylerOS-Runtime-Kind"] = RUNTIME_KIND
    return headers


def work_and_tick_tokens(environ: dict[str, str] | None = None) -> tuple[str, str]:
    env = os.environ if environ is None else environ
    instance = env.get("TYLEROS_RUNTIME_CREDENTIAL", "").strip()
    system = env.get("RUNTIME_TOKEN", "").strip()
    return instance or system, system


def request_json(
    method: str,
    url: str,
    token: str,
    *,
    payload: dict[str, Any] | None = None,
    identity: bool = True,
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    if identity:
        headers.update(identity_headers())
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
            if not raw:
                return None
            return json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: HTTP {error.code} {body}") from error


def tick_once(base_url: str, token: str) -> Any:
    return request_json(
        "POST",
        f"{base_url}/api/runtime/schedules/tick",
        token,
        payload={},
        identity=False,
    )


def process_once(base_url: str, token: str) -> bool:
    claimed = request_json("GET", f"{base_url}/api/runtime/jobs/next", token)
    job = None if not isinstance(claimed, dict) else claimed.get("job")
    run = None if not isinstance(claimed, dict) else claimed.get("run")
    if not job or not run:
        return False

    run_id = run.get("id")
    context = request_json("GET", f"{base_url}/api/runtime/context/today", token)
    if not isinstance(context, dict):
        raise RuntimeError("Today context was not an object.")

    usage = {"provider": "none", "model": "deterministic"}
    if not today_has_material(context):
        request_json(
            "POST",
            f"{base_url}/api/runtime/runs/{run_id}/complete",
            token,
            payload={
                "status": "succeeded",
                "resultSummary": EMPTY_TODAY_SUMMARY,
                "usage": usage,
            },
        )
        print(f"Quiet success for job {job.get('id')}: {EMPTY_TODAY_SUMMARY}")
        return True

    title, body = format_today_briefing(context)
    request_json(
        "POST",
        f"{base_url}/api/runtime/runs/{run_id}/complete",
        token,
        payload={
            "status": "succeeded",
            "resultSummary": "Drafted today's briefing.",
            "proposal": {"kind": "create_note", "title": title, "body": body},
            "usage": usage,
        },
    )
    print(f"Proposed note for job {job.get('id')}: {title}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="tick once, claim at most one job, exit")
    args = parser.parse_args(argv)

    base_url = os.environ.get("TYLEROS_URL", "http://localhost:3000").rstrip("/")
    work_token, tick_token = work_and_tick_tokens()
    if len(work_token) < 32:
        print(
            "Set TYLEROS_RUNTIME_CREDENTIAL (preferred) or RUNTIME_TOKEN to a TylerOS instance or system token.",
            file=sys.stderr,
        )
        return 1

    poll_seconds = float(os.environ.get("TYLEROS_POLL_SECONDS", "5"))
    tick_seconds = float(os.environ.get("TYLEROS_TICK_SECONDS", "60"))

    if args.once:
        if tick_token:
            tick_once(base_url, tick_token)
        process_once(base_url, work_token)
        return 0

    print(
        f"Polling {base_url} as Miles on a Python runtime instance; ticking schedules every {tick_seconds:.0f}s."
    )
    last_tick = 0.0
    while True:
        try:
            now = time.time()
            if tick_token and now - last_tick >= tick_seconds:
                tick_once(base_url, tick_token)
                last_tick = now
            process_once(base_url, work_token)
        except Exception as error:  # noqa: BLE001 — keep the poller alive
            print(f"poll failed: {error}", file=sys.stderr)
        time.sleep(poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())

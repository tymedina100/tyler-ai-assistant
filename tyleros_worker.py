#!/usr/bin/env python3
"""Poll TylerOS as Miles on the Python runtime.

This is not the Telegram group bot and not a specialist framework. It claims
jobs assigned to the Miles role, reads Today's titles and dates, and proposes
a note. TylerOS writes the note only if Tyler accepts it.
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


def format_briefing_date(iso: str) -> str:
    year, month, day = iso.split("-")
    return f"{int(day)} {MONTHS[int(month) - 1]} {year}"


def format_today_briefing(context: dict[str, Any]) -> tuple[str, str]:
    today = str(context.get("today") or "")
    title = f"Today briefing — {format_briefing_date(today)}" if today else "Today briefing"

    sections: list[str] = []
    _append_item_section(sections, "Overdue", context.get("overdue"))
    _append_item_section(sections, "Due today", context.get("dueToday"))
    _append_item_section(sections, "Needs triage", context.get("needsTriage"), include_due=False)
    _append_item_section(sections, "Next 7 days", context.get("upcoming"))
    _append_food_section(sections, context.get("expiringSoon"))

    if not sections:
        body = f"{title}\n\nNothing needs you right now."
    else:
        body = f"{title}\n\n" + "\n\n".join(sections)

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


def request_json(
    method: str,
    url: str,
    token: str,
    *,
    payload: dict[str, Any] | None = None,
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-TylerOS-Runtime-Kind": RUNTIME_KIND,
        "X-TylerOS-Role": ROLE,
        "Accept": "application/json",
    }
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

    title, body = format_today_briefing(context)
    request_json(
        "POST",
        f"{base_url}/api/runtime/runs/{run_id}/complete",
        token,
        payload={
            "status": "succeeded",
            "resultSummary": "Drafted today's briefing.",
            "proposal": {"kind": "create_note", "title": title, "body": body},
            "usage": {"provider": "none", "model": "deterministic"},
        },
    )
    print(f"Proposed note for job {job.get('id')}: {title}")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="claim at most one job and exit")
    args = parser.parse_args(argv)

    base_url = os.environ.get("TYLEROS_URL", "http://localhost:3000").rstrip("/")
    token = os.environ.get("RUNTIME_TOKEN", "").strip()
    if len(token) < 32:
        print("RUNTIME_TOKEN is missing or too short. Set it to the same value as TylerOS.", file=sys.stderr)
        return 1

    interval = float(os.environ.get("TYLEROS_POLL_SECONDS", "5"))
    if args.once:
        process_once(base_url, token)
        return 0

    print(f"Polling {base_url} as Miles on the Python runtime.")
    while True:
        try:
            process_once(base_url, token)
        except Exception as error:  # noqa: BLE001 — keep the poller alive
            print(f"poll failed: {error}", file=sys.stderr)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())

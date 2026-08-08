"""Send one bounded, zero-model Telegram team transport check from Railway.

This script initializes Telegram clients only long enough to call ``sendMessage``.
It never starts polling, calls an AI model, reserves budget, or mutates roadmap
state. The resulting secret-redacted report is persisted beside autonomous runs.
"""

import asyncio
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import group_bot  # noqa: E402


def _exit_code(report):
    """Only a complete, verified direct roster is automation-successful."""

    return 0 if report.get("status") == "passed" else 1


async def _main():
    try:
        report = await group_bot.run_team_transport_smoke_one_shot()
    except group_bot.TeamExecutionOverlapError:
        print(json.dumps({
            "status": "overlap_prevented",
            "message": (
                "Another autonomous, Company Mode, or team-check run holds the "
                "persistent execution gate; no smoke messages were sent."
            ),
        }, indent=2, sort_keys=True))
        return 1
    print(json.dumps({
        "smoke_id": report.get("smoke_id"),
        "status": report.get("status"),
        "expected_count": report.get("expected_count"),
        "direct_count": report.get("direct_count"),
        "relayed_count": report.get("relayed_count"),
        "failed_count": report.get("failed_count"),
        "model_calls": report.get("model_calls"),
        "actual_or_reconciled_cost_usd": report.get(
            "actual_or_reconciled_cost_usd"
        ),
        "report_path": report.get("report_path"),
    }, indent=2, sort_keys=True))
    return _exit_code(report)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))

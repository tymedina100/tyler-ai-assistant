"""Preview or atomically queue a repository-owned autonomous roadmap pack.

This command never invokes a model and never starts an autonomous run. Applying a
pack requires both ``--apply`` and a recorded approval source.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import autonomous_workflow


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely preview or queue one autonomous roadmap pack."
    )
    parser.add_argument("manifest_id", help="Pack filename without the .json suffix")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist the pack after revalidating the previewed revision",
    )
    parser.add_argument(
        "--approval-source",
        help="Audit label for the explicit owner approval; required with --apply",
    )
    args = parser.parse_args()

    if args.apply and not str(args.approval_source or "").strip():
        parser.error("--approval-source is required with --apply")

    workflow = autonomous_workflow.AutonomousWorkflow()
    success, preview_or_message = workflow.preview_roadmap_pack(args.manifest_id)
    if not success:
        print(str(preview_or_message))
        return 1
    preview = preview_or_message
    if not args.apply:
        print(json.dumps(autonomous_workflow.redact_secrets(preview), indent=2, sort_keys=True))
        return 0

    success, message = workflow.queue_roadmap_pack(
        args.manifest_id,
        expected_revision=str(preview["manifest_revision"]),
        approval_source=str(args.approval_source),
    )
    print(message)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())

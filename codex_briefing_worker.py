"""Explicit one-shot subscription briefing consumer. No scheduling or API fallback."""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from codex_subscription import QuotaEvidence, SubscriptionUnavailable, validate_quota
from codex_quota import read_quota
from subscription_attempts import analyze_once, inspect_attempts, assert_ledger_available
from tyleros_worker import request_json, work_and_tick_tokens


def resume_run(base_url, token, run_id, *, ledger_path, binary, quota=None, enabled=False, delivery_only=False):
    if not enabled:
        raise SubscriptionUnavailable("Subscription execution is not enabled.")
    run_id = str(UUID(run_id))
    if delivery_only:
        attempts = inspect_attempts(ledger_path, attempt_id=run_id)
        if not attempts or attempts[0]["status"] != "succeeded":
            raise SubscriptionUnavailable("Delivery-only requires a completed local receipt; no inference started.")
    endpoint = f"{base_url}/api/runtime/runs/{run_id}/codex"
    prepared = request_json("POST", endpoint + "/prepare", token, payload={})
    if prepared["status"] == "succeeded":
        return prepared
    request = prepared["request"]
    if request["runId"] != run_id:
        raise SubscriptionUnavailable("Prepared context belongs to another attempt.")
    receipt = analyze_once(request["prompt"], attempt_id=run_id, ledger_path=ledger_path,
                           binary=binary, model=request["model"], effort=request["effort"],
                           quota=quota, enabled=enabled, delivery_only=delivery_only, response_kind="miles_judgment")
    usage = receipt.get("usage") or {}
    return request_json("POST", endpoint + "/complete", token, payload={
        "judgment": receipt["judgment"], "usage": {
            "inputTokens": usage.get("input_tokens"),
            "cachedInputTokens": usage.get("cached_input_tokens"),
            "outputTokens": usage.get("output_tokens")}})


def process_once(base_url, token, **options):
    if not options.get("enabled"):
        raise SubscriptionUnavailable("Subscription execution is not enabled.")
    options = dict(options)
    if options.get("quota") is None:
        options["quota"] = read_quota(options["binary"])
    validate_quota(options["quota"])
    assert_ledger_available(options["ledger_path"])
    claimed = request_json("GET", base_url + "/api/runtime/jobs/next?kind=today_briefing_codex", token)
    if not claimed or not claimed.get("job"):
        return None
    if claimed["job"]["kind"] != "today_briefing_codex":
        raise SubscriptionUnavailable("Unexpected job kind; no inference started.")
    return resume_run(base_url, token, claimed["run"]["id"], **options)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-subscription", action="store_true")
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--binary")
    parser.add_argument("--quota-file", help="Optional fresh quota evidence override; otherwise read supported app-server limits")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--resume-run")
    modes.add_argument("--deliver-run", help="Deliver an existing receipt only; never run inference")
    modes.add_argument("--inspect-ledger", action="store_true", help="Read recent status without credentials, network or model calls")
    args = parser.parse_args()
    if args.inspect_ledger:
        print(json.dumps(inspect_attempts(args.ledger)))
        return
    if not args.binary:
        parser.error("--binary is required to match the original execution intent.")
    if not args.enable_subscription:
        parser.error("Explicit --enable-subscription is required.")
    base_url = os.environ.get("TYLEROS_URL", "").rstrip("/")
    url = urlsplit(base_url)
    if not url.hostname or url.username or url.password or (url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"})):
        parser.error("Use an HTTPS TylerOS origin or isolated loopback server.")
    token, _ = work_and_tick_tokens()
    if len(token) < 32:
        parser.error("Configure the existing runtime credential.")
    quota = QuotaEvidence(**json.loads(Path(args.quota_file).read_text())) if args.quota_file else None
    options = dict(ledger_path=args.ledger, binary=args.binary, quota=quota, enabled=True)
    run_id = args.deliver_run or args.resume_run
    result = resume_run(base_url, token, run_id, delivery_only=bool(args.deliver_run), **options) if run_id else process_once(base_url, token, **options)
    print(json.dumps(result))


if __name__ == "__main__":
    main()

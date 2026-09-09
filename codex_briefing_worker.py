"""Explicit one-shot subscription briefing consumer. No scheduling or API fallback."""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from codex_subscription import QuotaEvidence, SubscriptionUnavailable, validate_quota
from codex_quota import read_quota
from subscription_attempts import analyze_once
from tyleros_worker import request_json, work_and_tick_tokens


def resume_run(base_url, token, run_id, *, ledger_path, binary, quota=None, enabled=False):
    if not enabled:
        raise SubscriptionUnavailable("Subscription execution is not enabled.")
    run_id = str(UUID(run_id))
    endpoint = f"{base_url}/api/runtime/runs/{run_id}/codex"
    prepared = request_json("POST", endpoint + "/prepare", token, payload={})
    if prepared["status"] == "succeeded":
        return prepared
    request = prepared["request"]
    if request["runId"] != run_id:
        raise SubscriptionUnavailable("Prepared context belongs to another attempt.")
    receipt = analyze_once(request["prompt"], attempt_id=run_id, ledger_path=ledger_path,
                           binary=binary, model=request["model"], effort=request["effort"],
                           quota=quota, enabled=enabled, response_kind="miles_judgment")
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
    parser.add_argument("--binary", required=True)
    parser.add_argument("--quota-file", help="Optional fresh quota evidence override; otherwise read supported app-server limits")
    parser.add_argument("--resume-run")
    args = parser.parse_args()
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
    result = resume_run(base_url, token, args.resume_run, **options) if args.resume_run else process_once(base_url, token, **options)
    print(json.dumps(result))


if __name__ == "__main__":
    main()

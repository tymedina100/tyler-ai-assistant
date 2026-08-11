import hashlib
import hmac
import json
import os
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, call, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import autonomy_team
import company_mode
import main
import revenue_actions


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.content = (
            json.dumps(payload).encode("utf-8") if payload is not None else b""
        )

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


def capability(action_type, target, *, amount=0.0, allowed=True):
    return {
        "allowed": allowed,
        "reason": "allowed" if allowed else "denied for test",
        "campaign_id": "rev-sprint-1",
        "campaign_status": "active",
        "campaign_date": "2026-08-10",
        "action_type": action_type,
        "target": target,
        "policy_revision": "policy-rev-1",
        "requested_policy_revision": "policy-rev-1",
        "daily_count": 0,
        "daily_cap": 2,
        "total_count": 0,
        "total_cap": 20,
        "purchase_requested_usd": amount,
        "purchase_committed_today_usd": 0.0,
        "purchase_daily_cap_usd": 0.0,
        "purchase_committed_total_usd": 0.0,
        "purchase_total_cap_usd": 0.0,
    }


class RevenueActionsTests(unittest.TestCase):
    def setUp(self):
        self.claim = patch.object(company_mode, "claim_revenue_action").start()
        self.complete = patch.object(company_mode, "complete_revenue_action").start()
        self.live_capability = patch.object(
            company_mode, "revenue_action_capability"
        ).start()
        self.record_signal = patch.object(company_mode, "record_revenue_signal").start()
        self.addCleanup(patch.stopall)

        def claim_record(action_type, target, run_id, **kwargs):
            return {
                "id": "action-1",
                "campaign_id": kwargs.get("sprint_id"),
                "run_id": run_id,
                "action_type": action_type,
                "target": target,
                "status": "claimed",
                "idempotency_key": kwargs.get("idempotency_key"),
                "reserved_purchase_usd": kwargs.get("purchase_amount_usd", 0.0),
                "metadata": kwargs.get("metadata", {}),
            }

        self.claim.side_effect = claim_record
        self.complete.return_value = {"status": "succeeded"}

    @contextmanager
    def active(self, cap, *, dry_run=False, approved_payload_digest=None):
        token = revenue_actions.set_campaign_action_context(
            cap,
            "campaign-run-1",
            dry_run=dry_run,
            approved_payload_digest=approved_payload_digest,
        )
        try:
            yield
        finally:
            revenue_actions.reset_campaign_action_context(token)

    def configure_target(self, action_type, target, **record):
        value = {"account_id": record.pop("account_id", "company-account"), **record}
        return patch.dict(
            os.environ,
            {
                "REVENUE_COMPANY_ACTION_TARGETS": json.dumps(
                    {f"{action_type}:{target}": value}
                )
            },
            clear=False,
        )

    def test_provider_receipt_that_looks_sensitive_is_hashed_before_persistence(self):
        raw = "token=provider-controlled-secret-value"

        safe = revenue_actions._safe_receipt(raw)

        self.assertTrue(safe.startswith("redacted-"))
        self.assertNotIn("secret", safe)
        self.assertNotIn(raw, safe)

    def test_capability_exposes_only_one_exact_campaign_tool(self):
        target = "bluesky:company.example"
        cap = capability("publish", target)
        profile = {
            "read_file",
            "campaign_publish_bluesky",
            "campaign_publish_webhook",
            "campaign_purchase_webhook",
        }

        self.assertEqual(
            autonomy_team.allowed_tool_names(profile, "external_action", cap),
            {"read_file", "campaign_publish_bluesky"},
        )
        self.assertEqual(
            autonomy_team.allowed_tool_names(profile, "external_action"),
            {"read_file"},
        )
        bad_revision = dict(cap, requested_policy_revision="different")
        self.assertNotIn(
            "campaign_publish_bluesky",
            autonomy_team.allowed_tool_names(profile, "external_action", bad_revision),
        )
        missing_revision = dict(cap)
        missing_revision.pop("requested_policy_revision")
        self.assertNotIn(
            "campaign_publish_bluesky",
            autonomy_team.allowed_tool_names(
                profile, "external_action", missing_revision
            ),
        )
        self.assertNotIn(
            "campaign_publish_bluesky",
            autonomy_team.allowed_tool_names(profile, "observe", cap),
        )

    def test_ordinary_reactive_tools_hide_every_campaign_action(self):
        ordinary_names = {
            tool["name"] for tool in main._allowed_tools(main.TOOLS)
        }
        self.assertTrue(main.CAMPAIGN_EXTERNAL_TOOL_NAMES.isdisjoint(ordinary_names))
        explicit_names = {
            tool["name"]
            for tool in main._allowed_tools(
                main.TOOLS, {"read_file", "campaign_publish_bluesky"}
            )
        }
        self.assertEqual(explicit_names, {"read_file", "campaign_publish_bluesky"})

    def test_bluesky_publish_claims_before_io_and_persists_only_receipt(self):
        target = "bluesky:company.example"
        cap = capability("publish", target)
        self.live_capability.return_value = cap
        session = FakeResponse(200, {
            "handle": "company.example",
            "did": "did:plc:company",
            "accessJwt": "session-secret-token",
        })
        posted = FakeResponse(200, {
            "uri": "at://did:plc:company/app.bsky.feed.post/abc",
            "cid": "bafy-post-cid",
        })
        text = "New guide: https://company.example/guide"
        approved_payload = {
            "action_type": "publish",
            "target": target,
            "text": text,
            "url": "https://company.example/guide",
        }
        approved_digest = hashlib.sha256(
            json.dumps(
                approved_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        env = {
            "REVENUE_BLUESKY_HANDLE": "company.example",
            "REVENUE_BLUESKY_APP_PASSWORD": "app-password-secret",
        }

        with self.configure_target(
            "publish", target, account_id="company.example"
        ), patch.dict(os.environ, env, clear=False), patch.object(
            revenue_actions.requests, "post", side_effect=[session, posted]
        ) as post, self.active(cap, approved_payload_digest=approved_digest):
            result = revenue_actions.publish_bluesky(
                target, text, "https://company.example/guide"
            )

        self.assertIn("succeeded", result)
        self.claim.assert_called_once()
        post.assert_has_calls([
            call(
                "https://bsky.social/xrpc/com.atproto.server.createSession",
                json={
                    "identifier": "company.example",
                    "password": "app-password-secret",
                },
                timeout=20,
                allow_redirects=False,
                stream=True,
            ),
            call(
                "https://bsky.social/xrpc/com.atproto.repo.createRecord",
                headers={
                    "Authorization": "Bearer session-secret-token",
                    "Content-Type": "application/json",
                },
                json=ANY,
                timeout=20,
                allow_redirects=False,
                stream=True,
            ),
        ])
        record = post.call_args_list[1].kwargs["json"]["record"]
        self.assertEqual(record["$type"], "app.bsky.feed.post")
        self.assertIn("createdAt", record)
        facet = record["facets"][0]
        start = text.index("https://")
        self.assertEqual(facet["index"]["byteStart"], len(text[:start].encode("utf-8")))

        claim_kwargs = self.claim.call_args.kwargs
        self.assertEqual(claim_kwargs["approved_payload_digest"], approved_digest)
        serialized_claim = json.dumps(claim_kwargs)
        self.assertNotIn("app-password-secret", serialized_claim)
        self.assertNotIn("session-secret-token", serialized_claim)
        self.assertNotIn(text, serialized_claim)
        self.assertRegex(claim_kwargs["metadata"]["payload_digest"], r"^[a-f0-9]{64}$")
        completion = self.complete.call_args
        persisted_result = completion.kwargs["result"]
        self.assertIn("at://did:plc:company", persisted_result)
        self.assertNotIn("session-secret-token", persisted_result)

    def test_bluesky_never_falls_back_to_unmapped_or_personal_account(self):
        target = "bluesky:company.example"
        cap = capability("publish", target)
        self.live_capability.return_value = cap
        with patch.dict(
            os.environ,
            {
                "REVENUE_COMPANY_ACTION_TARGETS": "{}",
                "REVENUE_BLUESKY_HANDLE": "personal.example",
                "REVENUE_BLUESKY_APP_PASSWORD": "secret",
            },
            clear=False,
        ), patch.object(revenue_actions.requests, "post") as post, self.active(cap):
            with self.assertRaises(revenue_actions.RevenueActionDenied) as denied:
                revenue_actions.publish_bluesky(target, "A bounded post")
        self.assertIn("NEEDS HUMAN", str(denied.exception))
        self.assertIn("Automated signup", str(denied.exception))
        self.claim.assert_not_called()
        post.assert_not_called()

    def test_generic_publish_webhook_is_signed_idempotent_and_records_bounded_signals(self):
        target = "web:freelancer-cold-email-site"
        cap = capability("publish", target)
        self.live_capability.return_value = cap
        response = FakeResponse(200, {
            "receipt_id": "publish-123",
            "signals": [
                {"type": "click", "count": 3, "value_usd": 0, "evidence": "campaign click"},
                {"type": "not-allowed", "count": 999},
                {"type": "sale", "count": 1, "value_usd": 9, "evidence": "token=secret-value"},
            ],
        })
        env = {
            "REVENUE_PUBLISH_WEBHOOK_URL": "https://company-actions.example/publish",
            "REVENUE_PUBLISH_WEBHOOK_SECRET": "webhook-signing-secret",
            "REVENUE_PUBLISH_WEBHOOK_ACCOUNT_ID": "company-web",
        }
        with self.configure_target(
            "publish", target, account_id="company-web"
        ), patch.dict(os.environ, env, clear=False), patch.object(
            revenue_actions.requests, "post", return_value=response
        ) as post, patch.object(revenue_actions.time, "time", return_value=1234), self.active(cap):
            result = revenue_actions.publish_webhook(
                target, {"artifact_url": "https://company.example/product"}
            )

        self.assertIn("Recorded 2 revenue signal", result)
        request = post.call_args
        body = request.kwargs["data"]
        expected = hmac.new(
            b"webhook-signing-secret", b"1234." + body, hashlib.sha256
        ).hexdigest()
        self.assertEqual(
            request.kwargs["headers"]["X-Revenue-Signature"], f"sha256={expected}"
        )
        self.assertEqual(
            request.kwargs["headers"]["X-Idempotency-Key"],
            self.claim.call_args.kwargs["idempotency_key"],
        )
        self.assertEqual(self.record_signal.call_count, 2)
        evidence = self.record_signal.call_args_list[1].kwargs["evidence"]
        self.assertNotIn("secret-value", evidence)
        self.assertEqual(self.complete.call_args.args[1], "succeeded")

    def test_idempotent_replay_never_repeats_provider_io(self):
        target = "web:freelancer-cold-email-site"
        cap = capability("publish", target)
        self.live_capability.return_value = cap
        self.claim.side_effect = None
        self.claim.return_value = {
            "id": "existing",
            "status": "uncertain",
            "idempotent_replay": True,
        }
        env = {
            "REVENUE_PUBLISH_WEBHOOK_URL": "https://company-actions.example/publish",
            "REVENUE_PUBLISH_WEBHOOK_SECRET": "secret",
            "REVENUE_PUBLISH_WEBHOOK_ACCOUNT_ID": "company-web",
        }
        with self.configure_target(
            "publish", target, account_id="company-web"
        ), patch.dict(os.environ, env, clear=False), patch.object(
            revenue_actions.requests, "post"
        ) as post, self.active(cap):
            result = revenue_actions.publish_webhook(target, {"version": 1})

        self.assertIn("no external request was repeated", result)
        post.assert_not_called()
        self.complete.assert_not_called()

    def test_live_policy_denial_prevents_claim_and_network(self):
        target = "web:freelancer-cold-email-site"
        cap = capability("publish", target)
        self.live_capability.return_value = capability("publish", target, allowed=False)
        env = {
            "REVENUE_PUBLISH_WEBHOOK_URL": "https://company-actions.example/publish",
            "REVENUE_PUBLISH_WEBHOOK_SECRET": "secret",
            "REVENUE_PUBLISH_WEBHOOK_ACCOUNT_ID": "company-web",
        }
        with self.configure_target(
            "publish", target, account_id="company-web"
        ), patch.dict(os.environ, env, clear=False), patch.object(
            revenue_actions.requests, "post"
        ) as post, self.active(cap):
            with self.assertRaises(revenue_actions.RevenueActionDenied):
                revenue_actions.publish_webhook(target, {"version": 1})
        self.claim.assert_not_called()
        post.assert_not_called()

    def test_live_capability_drift_prevents_claim_and_network(self):
        target = "web:freelancer-cold-email-site"
        cap = capability("publish", target)
        self.live_capability.return_value = capability("publish", "web:different")
        env = {
            "REVENUE_PUBLISH_WEBHOOK_URL": "https://company-actions.example/publish",
            "REVENUE_PUBLISH_WEBHOOK_SECRET": "secret",
            "REVENUE_PUBLISH_WEBHOOK_ACCOUNT_ID": "company-web",
        }
        with self.configure_target(
            "publish", target, account_id="company-web"
        ), patch.dict(os.environ, env, clear=False), patch.object(
            revenue_actions.requests, "post"
        ) as post, self.active(cap):
            with self.assertRaises(revenue_actions.RevenueActionDenied):
                revenue_actions.publish_webhook(target, {"version": 1})
        self.claim.assert_not_called()
        post.assert_not_called()

    def test_dry_run_checks_policy_but_neither_claims_nor_calls_provider(self):
        target = "web:freelancer-cold-email-site"
        cap = capability("publish", target)
        self.live_capability.return_value = cap
        env = {
            "REVENUE_PUBLISH_WEBHOOK_URL": "https://company-actions.example/publish",
            "REVENUE_PUBLISH_WEBHOOK_SECRET": "secret",
            "REVENUE_PUBLISH_WEBHOOK_ACCOUNT_ID": "company-web",
        }
        with self.configure_target(
            "publish", target, account_id="company-web"
        ), patch.dict(os.environ, env, clear=False), patch.object(
            revenue_actions.requests, "post"
        ) as post, self.active(cap, dry_run=True):
            result = revenue_actions.publish_webhook(target, {"version": 1})

        self.assertIn("DRY RUN", result)
        self.live_capability.assert_called_once()
        self.claim.assert_not_called()
        self.complete.assert_not_called()
        post.assert_not_called()

    def test_purchase_is_separate_and_defaults_fail_closed_at_zero(self):
        target = "vendor:fixed-sku"
        cap = capability("purchase", target, amount=1.0)
        self.live_capability.return_value = cap
        env = {
            "REVENUE_PURCHASE_WEBHOOK_URL": "https://company-actions.example/purchase",
            "REVENUE_PURCHASE_WEBHOOK_SECRET": "secret",
            "REVENUE_PURCHASE_WEBHOOK_ACCOUNT_ID": "company-buying",
            "REVENUE_PURCHASE_HARD_CAP_USD": "0",
        }
        with self.configure_target(
            "purchase", target, account_id="company-buying"
        ), patch.dict(os.environ, env, clear=False), patch.object(
            revenue_actions.requests, "post"
        ) as post, self.active(cap):
            with self.assertRaises(revenue_actions.RevenueActionDenied):
                revenue_actions.purchase_webhook(target, 1.0, {"sku": "fixed-sku"})
        self.claim.assert_not_called()
        post.assert_not_called()

    def test_purchase_success_reconciles_exact_claimed_amount(self):
        target = "vendor:fixed-sku"
        cap = capability("purchase", target, amount=1.25)
        self.live_capability.return_value = cap
        env = {
            "REVENUE_PURCHASE_WEBHOOK_URL": "https://company-actions.example/purchase",
            "REVENUE_PURCHASE_WEBHOOK_SECRET": "secret",
            "REVENUE_PURCHASE_WEBHOOK_ACCOUNT_ID": "company-buying",
            "REVENUE_PURCHASE_HARD_CAP_USD": "2.00",
        }
        with self.configure_target(
            "purchase", target, account_id="company-buying"
        ), patch.dict(os.environ, env, clear=False), patch.object(
            revenue_actions.requests,
            "post",
            return_value=FakeResponse(200, {"receipt_id": "order-1"}),
        ), self.active(cap):
            result = revenue_actions.purchase_webhook(
                target, 1.25, {"sku": "fixed-sku"}
            )

        self.assertIn("succeeded", result)
        self.assertEqual(self.claim.call_args.kwargs["purchase_amount_usd"], 1.25)
        self.assertEqual(self.complete.call_args.kwargs["actual_purchase_usd"], 1.25)

    def test_purchase_http_failure_is_uncertain_and_keeps_reserved_amount(self):
        target = "vendor:fixed-sku"
        cap = capability("purchase", target, amount=1.25)
        self.live_capability.return_value = cap
        env = {
            "REVENUE_PURCHASE_WEBHOOK_URL": "https://company-actions.example/purchase",
            "REVENUE_PURCHASE_WEBHOOK_SECRET": "secret",
            "REVENUE_PURCHASE_WEBHOOK_ACCOUNT_ID": "company-buying",
            "REVENUE_PURCHASE_HARD_CAP_USD": "2.00",
        }
        with self.configure_target(
            "purchase", target, account_id="company-buying"
        ), patch.dict(os.environ, env, clear=False), patch.object(
            revenue_actions.requests,
            "post",
            return_value=FakeResponse(503, {"error": "unknown"}),
        ), self.active(cap):
            result = revenue_actions.purchase_webhook(
                target, 1.25, {"sku": "fixed-sku"}
            )

        self.assertIn("uncertain", result)
        self.assertEqual(self.complete.call_args.args[1], "uncertain")
        self.assertEqual(self.complete.call_args.kwargs["actual_purchase_usd"], 1.25)

    def test_company_gmail_identity_is_verified_after_claim_before_send(self):
        target = "prospect@example.com"
        cap = capability("outreach", target)
        self.live_capability.return_value = cap
        profile_execute = MagicMock(return_value={"emailAddress": "sales@company.example"})
        gmail_service = SimpleNamespace(
            users=lambda: SimpleNamespace(
                getProfile=lambda **_kwargs: SimpleNamespace(execute=profile_execute)
            )
        )
        env = {
            "REVENUE_OUTREACH_GMAIL_ACCOUNT": "sales@company.example",
            "REVENUE_COMPANY_GMAIL_ACCOUNTS": "sales@company.example",
            "GOOGLE_TOKEN_JSON": "{}",
        }
        with self.configure_target(
            "outreach", target, account_id="sales@company.example"
        ), patch.dict(os.environ, env, clear=False), patch(
            "google_helpers._gmail_service", return_value=gmail_service
        ), patch(
            "google_helpers.send_email",
            return_value="Email sent to prospect@example.com, subject 'Hi' (id msg-1).",
        ) as send, self.active(cap):
            result = revenue_actions.send_outreach_email(target, "Hi", "A concise note")

        self.assertIn("succeeded", result)
        self.claim.assert_called_once()
        profile_execute.assert_called_once()
        send.assert_called_once()

    def test_vercel_uses_only_project_and_ref_pinned_to_company_team(self):
        target = "web:freelancer-cold-email-site"
        cap = capability("deploy", target)
        self.live_capability.return_value = cap
        env = {
            "REVENUE_DEPLOY_VERCEL_ACCOUNT_ID": "team-company",
            "VERCEL_TEAM_ID": "team-company",
            "VERCEL_TOKEN": "provider-token",
        }
        with self.configure_target(
            "deploy",
            target,
            account_id="team-company",
            project="company-site",
            ref="release-campaign",
        ), patch.dict(os.environ, env, clear=False), patch(
            "deploy_helpers.deploy",
            return_value=({"id": "dpl-1", "url": "https://company.vercel.app"}, None),
        ) as deploy, self.active(cap):
            result = revenue_actions.deploy_vercel(target)

        self.assertIn("succeeded", result)
        deploy.assert_called_once_with("company-site", "release-campaign", "production")

    def test_payload_with_secret_or_card_data_is_denied_before_claim(self):
        target = "web:freelancer-cold-email-site"
        cap = capability("publish", target)
        with self.active(cap):
            with self.assertRaises(revenue_actions.RevenueActionDenied):
                revenue_actions.publish_webhook(target, {"api_key": "do-not-send"})
        self.claim.assert_not_called()

    def test_campaign_draft_is_parsed_to_exact_payload_and_digest(self):
        target = "bluesky:company.example"
        product_url = "https://company.example/product"
        payload = {
            "action_type": "publish",
            "target": target,
            "text": f"Ship smarter: {product_url}",
            "url": product_url,
        }

        parsed = revenue_actions.parse_campaign_draft(
            f"CAMPAIGN_DRAFT_JSON: {json.dumps(payload)}",
            action_type="publish",
            target=target,
            product_url=product_url,
        )

        canonical_json = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        self.assertEqual(parsed["payload"], payload)
        self.assertEqual(
            parsed["payload_digest"],
            hashlib.sha256(canonical_json.encode("utf-8")).hexdigest(),
        )

    def test_campaign_draft_rejects_noncanonical_envelopes_and_schema(self):
        target = "bluesky:company.example"
        base = {
            "action_type": "publish",
            "target": target,
            "text": "A public launch note",
            "url": "",
        }
        invalid_envelopes = [
            json.dumps(base),
            f"CAMPAIGN_DRAFT_JSON: {json.dumps(base)} trailing prose",
            f"CAMPAIGN_DRAFT_JSON: {json.dumps(dict(base, extra='no'))}",
            f"CAMPAIGN_DRAFT_JSON: {json.dumps({key: value for key, value in base.items() if key != 'url'})}",
            (
                'CAMPAIGN_DRAFT_JSON: {"action_type":"publish",'
                '"target":"bluesky:company.example",'
                '"target":"bluesky:other.example",'
                '"text":"A public launch note","url":""}'
            ),
        ]

        for envelope in invalid_envelopes:
            with self.subTest(envelope=envelope):
                with self.assertRaises(revenue_actions.RevenueActionDenied):
                    revenue_actions.parse_campaign_draft(
                        envelope,
                        action_type="publish",
                        target=target,
                        product_url="",
                    )

    def test_campaign_draft_requires_exact_target_bounded_text_and_product_url(self):
        target = "bluesky:company.example"
        product_url = "https://company.example/product"
        invalid_payloads = [
            {
                "action_type": "publish",
                "target": "bluesky:other.example",
                "text": f"Launch: {product_url}",
                "url": product_url,
            },
            {
                "action_type": "publish",
                "target": target,
                "text": "x" * 301,
                "url": "",
            },
            {
                "action_type": "publish",
                "target": target,
                "text": f"Launch: {product_url}",
                "url": "https://company.example/different",
            },
            {
                "action_type": "publish",
                "target": target,
                "text": "Launch: http://company.example/product",
                "url": "http://company.example/product",
            },
        ]

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(revenue_actions.RevenueActionDenied):
                    revenue_actions.parse_campaign_draft(
                        f"CAMPAIGN_DRAFT_JSON: {json.dumps(payload)}",
                        action_type="publish",
                        target=target,
                        product_url=product_url if payload["url"] else "",
                    )

    def test_campaign_draft_rejects_secret_and_explicitly_nonpublic_content(self):
        target = "bluesky:company.example"
        for text in (
            "Launch note api_key=super-secret-value",
            "[INTERNAL] launch copy pending approval",
        ):
            payload = {
                "action_type": "publish",
                "target": target,
                "text": text,
                "url": "",
            }
            with self.subTest(text=text):
                with self.assertRaises(revenue_actions.RevenueActionDenied):
                    revenue_actions.parse_campaign_draft(
                        f"CAMPAIGN_DRAFT_JSON: {json.dumps(payload)}",
                        action_type="publish",
                        target=target,
                        product_url="",
                    )

    def test_approved_campaign_draft_executes_with_context_then_resets_it(self):
        target = "bluesky:company.example"
        cap = capability("publish", target)
        payload = {
            "action_type": "publish",
            "target": target,
            "text": "A bounded public launch note",
            "url": "",
        }
        parsed = revenue_actions.parse_campaign_draft(
            f"CAMPAIGN_DRAFT_JSON: {json.dumps(payload)}",
            action_type="publish",
            target=target,
            product_url="",
        )
        observed = {}

        def publish(target_arg, text_arg, url_arg, *, dry_run=None):
            observed["context"] = revenue_actions.current_campaign_action_context()
            return f"published:{target_arg}:{text_arg}:{url_arg}:{dry_run}"

        with patch.object(revenue_actions, "publish_bluesky", side_effect=publish) as adapter:
            result = revenue_actions.execute_approved_campaign_draft(
                cap, "campaign-run-reviewed", parsed, dry_run=True
            )

        self.assertIn("published", result)
        adapter.assert_called_once_with(
            target, payload["text"], None, dry_run=True
        )
        self.assertEqual(observed["context"].run_id, "campaign-run-reviewed")
        self.assertTrue(observed["context"].dry_run)
        self.assertEqual(
            observed["context"].approved_payload_digest,
            parsed["payload_digest"],
        )
        self.assertIsNone(revenue_actions.current_campaign_action_context())

    def test_approved_campaign_draft_rejects_tampering_and_resets_after_failure(self):
        target = "bluesky:company.example"
        cap = capability("publish", target)
        payload = {
            "action_type": "publish",
            "target": target,
            "text": "A bounded public launch note",
            "url": "",
        }
        parsed = revenue_actions.parse_campaign_draft(
            f"CAMPAIGN_DRAFT_JSON: {json.dumps(payload)}",
            action_type="publish",
            target=target,
            product_url="",
        )
        tampered = {**parsed, "payload": {**parsed["payload"], "text": "Changed after review"}}

        with patch.object(revenue_actions, "publish_bluesky") as adapter:
            with self.assertRaises(revenue_actions.RevenueActionDenied):
                revenue_actions.execute_approved_campaign_draft(
                    cap, "campaign-run-reviewed", tampered
                )
        adapter.assert_not_called()
        self.assertIsNone(revenue_actions.current_campaign_action_context())

        with patch.object(
            revenue_actions, "publish_bluesky", side_effect=RuntimeError("provider failed")
        ):
            with self.assertRaisesRegex(RuntimeError, "provider failed"):
                revenue_actions.execute_approved_campaign_draft(
                    cap, "campaign-run-reviewed", parsed
                )
        self.assertIsNone(revenue_actions.current_campaign_action_context())


if __name__ == "__main__":
    unittest.main()

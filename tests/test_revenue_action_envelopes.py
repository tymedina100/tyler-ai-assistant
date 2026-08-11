import hashlib
import hmac
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import revenue_actions


REF = "a" * 40


class FakeResponse:
    def __init__(self, payload, *, signature):
        self.status_code = 200
        self.content = json.dumps(payload).encode("utf-8")
        self.headers = {"X-Revenue-Response-Signature": signature}
        self.closed = False

    def close(self):
        self.closed = True


def capability(action_type, target, *, amount=0.0):
    return {
        "allowed": True,
        "campaign_id": "campaign-1",
        "campaign_status": "active",
        "action_type": action_type,
        "target": target,
        "policy_revision": "policy-1",
        "requested_policy_revision": "policy-1",
        "purchase_requested_usd": amount,
    }


def envelope(payload):
    return "CAMPAIGN_DRAFT_JSON: " + json.dumps(payload)


class RevenueActionEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.base_env = {
            "REVENUE_COMPANY_ACTION_TARGETS": json.dumps({
                "purchase:vendor:sku-1": {
                    "account_id": "company-buying",
                    "amount_usd": 1.25,
                }
            }),
            "REVENUE_PURCHASE_HARD_CAP_USD": "5",
        }

    def parse(self, payload, **kwargs):
        return revenue_actions.parse_campaign_draft(
            envelope(payload),
            action_type=payload["action_type"],
            target=payload["target"],
            **kwargs,
        )

    def assert_digest(self, parsed):
        canonical = json.dumps(
            parsed["payload"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        self.assertEqual(
            parsed["payload_digest"],
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )

    def test_all_generic_schemas_parse_to_exact_canonical_payloads(self):
        outreach_target = "lead:one"
        deploy_target = "site:company"
        targets = {
            f"outreach:{outreach_target}": {
                "account_id": "sales@company.example",
                "recipient": "buyer@example.com",
            },
            f"deploy:{deploy_target}": {
                "account_id": "team-company",
                "project": "company-site",
                "ref": REF,
            },
            "purchase:vendor:sku-1": {
                "account_id": "company-buying",
                "amount_usd": 1.25,
            },
        }
        env = {
            **self.base_env,
            "REVENUE_COMPANY_ACTION_TARGETS": json.dumps(targets),
            "REVENUE_DEPLOY_CREDENTIAL_OWNERSHIP": "company_service",
            "REVENUE_DEPLOY_VERCEL_ACCOUNT_ID": "team-company",
            "VERCEL_TEAM_ID": "team-company",
        }
        payloads = [
            {
                "action_type": "publish",
                "target": "web:offer",
                "payload": {"headline": "A public offer"},
            },
            {
                "action_type": "outreach",
                "target": outreach_target,
                "recipient": "buyer@example.com",
                "subject": "A relevant offer",
                "body": "A concise company-owned outreach message.",
            },
            {
                "action_type": "deploy",
                "target": deploy_target,
                "account_id": "team-company",
                "project": "company-site",
                "ref": REF,
            },
            {
                "action_type": "purchase",
                "target": "vendor:sku-1",
                "amount_usd": 1.25,
                "payload": {"sku": "sku-1"},
            },
        ]
        with patch.dict(os.environ, env, clear=False):
            for payload in payloads:
                with self.subTest(action_type=payload["action_type"]):
                    parsed = self.parse(payload)
                    self.assertEqual(parsed["payload"], payload)
                    self.assert_digest(parsed)

    def test_outreach_and_deploy_bindings_fail_closed_on_drift(self):
        targets = {
            "outreach:lead:one": {
                "account_id": "sales@company.example",
                "recipient": "approved@example.com",
            },
            "deploy:site:company": {
                "account_id": "team-company",
                "project": "company-site",
                "ref": REF,
            },
            "purchase:vendor:sku-1": {
                "account_id": "company-buying",
                "amount_usd": 1.25,
            },
        }
        env = {
            **self.base_env,
            "REVENUE_COMPANY_ACTION_TARGETS": json.dumps(targets),
            "REVENUE_DEPLOY_CREDENTIAL_OWNERSHIP": "company_service",
            "REVENUE_DEPLOY_VERCEL_ACCOUNT_ID": "team-company",
            "VERCEL_TEAM_ID": "team-company",
        }
        invalid = [
            {
                "action_type": "outreach",
                "target": "lead:one",
                "recipient": "other@example.com",
                "subject": "Hello",
                "body": "A bounded note",
            },
            {
                "action_type": "deploy",
                "target": "site:company",
                "account_id": "team-company",
                "project": "company-site",
                "ref": "main",
            },
            {
                "action_type": "purchase",
                "target": "vendor:sku-1",
                "amount_usd": 1.26,
                "payload": {"sku": "sku-1"},
            },
        ]
        with patch.dict(os.environ, env, clear=False):
            for payload in invalid:
                with self.subTest(action_type=payload["action_type"]):
                    with self.assertRaises(revenue_actions.RevenueActionDenied):
                        self.parse(payload)

    def test_executor_dispatches_each_reviewed_generic_action(self):
        targets = {
            "outreach:lead:one": {
                "account_id": "sales@company.example",
                "recipient": "buyer@example.com",
            },
            "deploy:site:company": {
                "account_id": "team-company",
                "project": "company-site",
                "ref": REF,
            },
            "purchase:vendor:sku-1": {
                "account_id": "company-buying",
                "amount_usd": 1.25,
            },
        }
        env = {
            **self.base_env,
            "REVENUE_COMPANY_ACTION_TARGETS": json.dumps(targets),
            "REVENUE_DEPLOY_CREDENTIAL_OWNERSHIP": "company_service",
            "REVENUE_DEPLOY_VERCEL_ACCOUNT_ID": "team-company",
            "VERCEL_TEAM_ID": "team-company",
        }
        cases = [
            (
                {
                    "action_type": "publish",
                    "target": "web:offer",
                    "payload": {"headline": "Offer"},
                },
                "publish_webhook",
                ("web:offer", {"headline": "Offer"}),
            ),
            (
                {
                    "action_type": "outreach",
                    "target": "lead:one",
                    "recipient": "buyer@example.com",
                    "subject": "Hello",
                    "body": "A bounded note",
                },
                "send_outreach_email",
                ("buyer@example.com", "Hello", "A bounded note"),
            ),
            (
                {
                    "action_type": "deploy",
                    "target": "site:company",
                    "account_id": "team-company",
                    "project": "company-site",
                    "ref": REF,
                },
                "deploy_vercel",
                ("site:company",),
            ),
            (
                {
                    "action_type": "purchase",
                    "target": "vendor:sku-1",
                    "amount_usd": 1.25,
                    "payload": {"sku": "sku-1"},
                },
                "purchase_webhook",
                ("vendor:sku-1", 1.25, {"sku": "sku-1"}),
            ),
        ]
        with patch.dict(os.environ, env, clear=False):
            for payload, adapter_name, args in cases:
                with self.subTest(action_type=payload["action_type"]):
                    parsed = self.parse(payload)
                    cap = capability(
                        payload["action_type"],
                        payload["target"],
                        amount=payload.get("amount_usd", 0),
                    )
                    with patch.object(
                        revenue_actions, adapter_name, return_value="executed"
                    ) as adapter:
                        result = revenue_actions.execute_approved_campaign_draft(
                            cap, "run-1", parsed, dry_run=True
                        )
                    self.assertEqual(result, "executed")
                    adapter.assert_called_once()
                    self.assertEqual(adapter.call_args.args, args)
                    self.assertTrue(adapter.call_args.kwargs["dry_run"])
                    if adapter_name == "send_outreach_email":
                        self.assertEqual(
                            adapter.call_args.kwargs["campaign_target"], "lead:one"
                        )
                    self.assertIsNone(
                        revenue_actions.current_campaign_action_context()
                    )

    def test_purchase_claim_digest_binds_amount_and_nested_payload(self):
        payload = {
            "action_type": "purchase",
            "target": "vendor:sku-1",
            "amount_usd": 1.25,
            "payload": {"sku": "sku-1"},
        }
        with patch.dict(os.environ, self.base_env, clear=False):
            parsed = self.parse(payload)
        cap = capability("purchase", "vendor:sku-1", amount=1.25)
        provider = lambda _claim, _target: revenue_actions.ProviderOutcome(
            "succeeded", "accepted", actual_purchase_usd=1.25
        )
        claim = MagicMock(
            return_value={
                "id": "action-1",
                "status": "claimed",
                "idempotency_key": "rev-1",
            }
        )
        with patch.dict(os.environ, self.base_env, clear=False), patch.object(
            revenue_actions, "_provider_ready", return_value={"account_id": "buyer"}
        ), patch.object(
            revenue_actions, "_live_capability", return_value=cap
        ), patch.object(
            revenue_actions, "_claim", claim
        ), patch.object(
            revenue_actions, "_complete", return_value={"status": "succeeded"}
        ), patch.object(
            revenue_actions, "_webhook_provider", return_value=provider
        ):
            result = revenue_actions.execute_approved_campaign_draft(
                cap, "run-1", parsed
            )

        self.assertIn("succeeded", result)
        self.assertEqual(claim.call_args.args[3], parsed["payload_digest"])
        self.assertEqual(claim.call_args.args[4], 1.25)

    def test_reviewed_webhook_requires_exact_public_allowed_host(self):
        payload = {
            "action_type": "publish",
            "target": "web:offer",
            "payload": {"headline": "Offer"},
        }
        parsed = self.parse(payload)
        cap = capability("publish", "web:offer")
        target_map = {
            "publish:web:offer": {"account_id": "company-web"}
        }
        env = {
            **self.base_env,
            "REVENUE_COMPANY_ACTION_TARGETS": json.dumps(target_map),
            "REVENUE_PUBLISH_WEBHOOK_URL": "https://127.0.0.1/publish",
            "REVENUE_PUBLISH_WEBHOOK_ALLOWED_HOST": "127.0.0.1",
            "REVENUE_PUBLISH_WEBHOOK_SECRET": "signing-secret",
            "REVENUE_PUBLISH_WEBHOOK_ACCOUNT_ID": "company-web",
        }
        with patch.dict(os.environ, env, clear=False), patch.object(
            revenue_actions.requests, "post"
        ) as post:
            with self.assertRaises(revenue_actions.RevenueActionDenied):
                revenue_actions.execute_approved_campaign_draft(
                    cap, "run-1", parsed
                )
        post.assert_not_called()

    def test_reviewed_webhook_requires_exact_signed_receipt(self):
        payload = {
            "action_type": "publish",
            "target": "web:offer",
            "payload": {"headline": "Offer"},
        }
        parsed = self.parse(payload)
        cap = capability("publish", "web:offer")
        target_map = {"publish:web:offer": {"account_id": "company-web"}}
        env = {
            **self.base_env,
            "REVENUE_COMPANY_ACTION_TARGETS": json.dumps(target_map),
            "REVENUE_PUBLISH_WEBHOOK_URL": "https://actions.company.example/publish",
            "REVENUE_PUBLISH_WEBHOOK_ALLOWED_HOST": "actions.company.example",
            "REVENUE_PUBLISH_WEBHOOK_SECRET": "signing-secret",
            "REVENUE_PUBLISH_WEBHOOK_ACCOUNT_ID": "company-web",
        }
        receipt = {
            "status": "succeeded",
            "action_id": "action-1",
            "idempotency_key": "rev-1",
            "payload_digest": parsed["payload_digest"],
            "provider_account_id": "company-web",
            "amount_usd": 0.0,
            "receipt_id": "receipt-1",
        }
        canonical = json.dumps(
            receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        signature = "sha256=" + hmac.new(
            b"signing-secret", b"1234." + canonical, hashlib.sha256
        ).hexdigest()
        response = FakeResponse(receipt, signature=signature)
        claim = {
            "id": "action-1",
            "status": "claimed",
            "idempotency_key": "rev-1",
            "metadata": {"payload_digest": parsed["payload_digest"]},
        }
        with patch.dict(os.environ, env, clear=False), patch.object(
            revenue_actions.time, "time", return_value=1234
        ), patch.object(
            revenue_actions.requests, "post", return_value=response
        ), patch.object(
            revenue_actions, "_live_capability", return_value=cap
        ), patch.object(
            revenue_actions, "_claim", return_value=claim
        ), patch.object(
            revenue_actions, "_complete", return_value={"status": "succeeded"}
        ) as complete:
            result = revenue_actions.execute_approved_campaign_draft(
                cap, "run-1", parsed
            )

        self.assertIn("succeeded", result)
        outcome = complete.call_args.args[2]
        self.assertEqual(outcome.provider_receipt, {"receipt_id": "receipt-1"})

        bad_response = FakeResponse(receipt, signature="sha256=wrong")
        with patch.dict(os.environ, env, clear=False), patch.object(
            revenue_actions.time, "time", return_value=1234
        ), patch.object(
            revenue_actions.requests, "post", return_value=bad_response
        ), patch.object(
            revenue_actions, "_live_capability", return_value=cap
        ), patch.object(
            revenue_actions, "_claim", return_value=claim
        ), patch.object(
            revenue_actions, "_complete", return_value={"status": "uncertain"}
        ) as complete:
            result = revenue_actions.execute_approved_campaign_draft(
                cap, "run-1", parsed
            )

        self.assertIn("uncertain", result)
        self.assertEqual(complete.call_args.args[2].status, "uncertain")

    def test_readiness_returns_only_safe_exact_draft_bindings(self):
        target = "site:company"
        target_map = {
            f"deploy:{target}": {
                "account_id": "team-company",
                "project": "company-site",
                "ref": REF,
            }
        }
        env = {
            **self.base_env,
            "REVENUE_COMPANY_ACTION_TARGETS": json.dumps(target_map),
            "REVENUE_DEPLOY_CREDENTIAL_OWNERSHIP": "company_service",
            "REVENUE_DEPLOY_VERCEL_ACCOUNT_ID": "team-company",
            "VERCEL_TEAM_ID": "team-company",
            "VERCEL_TOKEN": "provider-token",
        }
        with patch.dict(os.environ, env, clear=False):
            result = revenue_actions.revenue_action_target_readiness(
                "deploy", target
            )

        self.assertTrue(result["ready"])
        self.assertEqual(
            result["draft_binding"],
            {
                "action_type": "deploy",
                "target": target,
                "account_id": "team-company",
                "project": "company-site",
                "ref": REF,
            },
        )
        self.assertNotIn("VERCEL_TOKEN", json.dumps(result))
        self.assertNotIn("provider-token", json.dumps(result))

    def test_reviewed_deploy_verifies_provider_project_before_mutation(self):
        target = "site:company"
        target_map = {
            f"deploy:{target}": {
                "account_id": "team-company",
                "project": "company-site",
                "ref": REF,
            }
        }
        env = {
            **self.base_env,
            "REVENUE_COMPANY_ACTION_TARGETS": json.dumps(target_map),
            "REVENUE_DEPLOY_CREDENTIAL_OWNERSHIP": "company_service",
            "REVENUE_DEPLOY_VERCEL_ACCOUNT_ID": "team-company",
            "VERCEL_TEAM_ID": "team-company",
            "VERCEL_TOKEN": "provider-token",
        }
        payload = {
            "action_type": "deploy",
            "target": target,
            "account_id": "team-company",
            "project": "company-site",
            "ref": REF,
        }
        with patch.dict(os.environ, env, clear=False):
            parsed = self.parse(payload)
            cap = capability("deploy", target)
            with patch.object(
                revenue_actions, "_live_capability", return_value=cap
            ), patch.object(
                revenue_actions,
                "_claim",
                return_value={"id": "action-1", "status": "claimed"},
            ), patch.object(
                revenue_actions, "_complete", return_value={"status": "failed"}
            ) as complete, patch(
                "deploy_helpers.get_project",
                return_value=({"id": "other", "name": "other", "accountId": "personal"}, None),
            ), patch("deploy_helpers.deploy") as deploy:
                result = revenue_actions.execute_approved_campaign_draft(
                    cap, "run-1", parsed
                )

        self.assertIn("failed", result)
        deploy.assert_not_called()
        self.assertEqual(complete.call_args.args[2].status, "failed")

    def test_reviewed_deploy_requires_exact_provider_deployment_id(self):
        target = "site:company"
        target_map = {
            f"deploy:{target}": {
                "account_id": "team-company",
                "project": "company-site",
                "ref": REF,
            }
        }
        env = {
            **self.base_env,
            "REVENUE_COMPANY_ACTION_TARGETS": json.dumps(target_map),
            "REVENUE_DEPLOY_CREDENTIAL_OWNERSHIP": "company_service",
            "REVENUE_DEPLOY_VERCEL_ACCOUNT_ID": "team-company",
            "VERCEL_TEAM_ID": "team-company",
            "VERCEL_TOKEN": "provider-token",
        }
        payload = {
            "action_type": "deploy",
            "target": target,
            "account_id": "team-company",
            "project": "company-site",
            "ref": REF,
        }
        with patch.dict(os.environ, env, clear=False):
            parsed = self.parse(payload)
        cap = capability("deploy", target)
        claim = {"id": "action-1", "status": "claimed"}
        identity = {
            "id": "company-site",
            "name": "company-site",
            "accountId": "team-company",
        }

        with patch.dict(os.environ, env, clear=False), patch.object(
            revenue_actions, "_live_capability", return_value=cap
        ), patch.object(
            revenue_actions, "_claim", return_value=claim
        ), patch(
            "deploy_helpers.get_project", return_value=(identity, None)
        ), patch(
            "deploy_helpers.deploy",
            return_value=({"id": "dpl_company_1", "url": "https://company.vercel.app"}, None),
        ), patch.object(
            revenue_actions, "_complete", return_value={"status": "succeeded"}
        ) as complete:
            result = revenue_actions.execute_approved_campaign_draft(
                cap, "run-1", parsed
            )

        self.assertIn("succeeded", result)
        outcome = complete.call_args.args[2]
        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(
            outcome.provider_receipt,
            {
                "deployment_id": "dpl_company_1",
                "url": "https://company.vercel.app",
            },
        )

        for invalid_result in ({}, {"id": 123}, {"id": "token=provider-secret"}):
            with self.subTest(invalid_result=invalid_result), patch.dict(
                os.environ, env, clear=False
            ), patch.object(
                revenue_actions, "_live_capability", return_value=cap
            ), patch.object(
                revenue_actions, "_claim", return_value=claim
            ), patch(
                "deploy_helpers.get_project", return_value=(identity, None)
            ), patch(
                "deploy_helpers.deploy", return_value=(invalid_result, None)
            ), patch.object(
                revenue_actions, "_complete", return_value={"status": "uncertain"}
            ) as uncertain_complete:
                result = revenue_actions.execute_approved_campaign_draft(
                    cap, "run-1", parsed
                )

            self.assertIn("uncertain", result)
            uncertain_outcome = uncertain_complete.call_args.args[2]
            self.assertEqual(uncertain_outcome.status, "uncertain")
            self.assertIsNone(uncertain_outcome.provider_receipt)


if __name__ == "__main__":
    unittest.main()

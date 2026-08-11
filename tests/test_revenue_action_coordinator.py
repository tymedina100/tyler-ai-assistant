import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import autonomy_team
import company_mode
from tests.test_group_autonomy import import_group_bot_with_stub_main


class RevenueActionPromptTests(unittest.TestCase):
    def _prompt_payload(self, action, *, binding=None, product_url=""):
        prompt = company_mode.build_task_prompt(
            {"goal": "Run one bounded, reviewed company experiment."},
            {
                "owner": "marketing",
                "title": "Draft one exact company action",
                "authorization_level": "propose",
                "enforce_authorization": True,
                "campaign_external_action": action,
                "campaign_action_binding": binding or {},
                "campaign_product_url": product_url,
            },
        )
        encoded = prompt.split("CAMPAIGN_DRAFT_JSON:\n", 1)[1]
        payload, _end = json.JSONDecoder().raw_decode(encoded)
        return prompt, payload

    def test_worker_prompts_use_one_strict_schema_per_action_adapter(self):
        product_url = "https://company.gumroad.com/l/kit"
        cases = [
            (
                {"action_type": "publish", "target": "bluesky:company.example"},
                {},
                {"action_type", "target", "text", "url"},
            ),
            (
                {"action_type": "publish", "target": "web:company-site"},
                {},
                {"action_type", "target", "payload"},
            ),
            (
                {"action_type": "outreach", "target": "email:launch-list"},
                {"recipient": "buyer@example.com"},
                {"action_type", "target", "recipient", "subject", "body"},
            ),
            (
                {"action_type": "deploy", "target": "vercel:company-site"},
                {"account_id": "team-1", "project": "site", "ref": "a" * 40},
                {"action_type", "target", "account_id", "project", "ref"},
            ),
            (
                {"action_type": "purchase", "target": "vendor:company-plan"},
                {"amount_usd": 1.25},
                {"action_type", "target", "amount_usd", "payload"},
            ),
        ]
        for action, binding, expected_keys in cases:
            with self.subTest(action=action):
                prompt, payload = self._prompt_payload(
                    action, binding=binding, product_url=product_url
                )
                self.assertEqual(set(payload), expected_keys)
                self.assertEqual(payload["action_type"], action["action_type"])
                self.assertEqual(payload["target"], action["target"])
                self.assertIn("do not execute an external action", prompt)

        _prompt, bluesky = self._prompt_payload(
            cases[0][0], product_url=product_url
        )
        self.assertEqual(bluesky["url"], product_url)
        _prompt, outreach = self._prompt_payload(cases[2][0], binding=cases[2][1])
        self.assertEqual(outreach["recipient"], "buyer@example.com")
        _prompt, deploy = self._prompt_payload(cases[3][0], binding=cases[3][1])
        self.assertEqual(
            (deploy["account_id"], deploy["project"], deploy["ref"]),
            ("team-1", "site", "a" * 40),
        )
        _prompt, purchase = self._prompt_payload(
            cases[4][0], binding=cases[4][1]
        )
        self.assertEqual(purchase["amount_usd"], 1.25)

    def test_reviewer_is_told_to_review_not_execute_the_exact_binding(self):
        prompt = company_mode.build_task_prompt(
            {"goal": "Review one action."},
            {
                "owner": "editor",
                "title": "Review",
                "authorization_level": "observe",
                "enforce_authorization": True,
                "campaign_external_action": {
                    "action_type": "deploy",
                    "target": "vercel:company-site",
                },
                "campaign_action_binding": {
                    "account_id": "team-1",
                    "project": "site",
                    "ref": "main",
                },
            },
            prior_work="- LATEST REVIEW CANDIDATE: exact deploy envelope",
        )
        self.assertIn("do not call a campaign tool or execute it", prompt)
        self.assertIn("immutable action binding", prompt)
        self.assertIn("provider receipt", prompt)

    def test_plan_and_revision_preserve_safe_action_binding(self):
        action = {
            "action_type": "deploy",
            "target": "vercel:company-site",
            "policy_revision": "policy-1",
        }
        binding = {"account_id": "team-1", "project": "site", "ref": "main"}
        worker_decision = types.SimpleNamespace(
            model_id="gpt-5.4-mini",
            estimated_cost_usd=0.01,
            reason="standard worker",
            deferred=False,
            deferral_reason=None,
        )
        review_router = Mock()
        review_router.route.return_value = types.SimpleNamespace(
            model_id="gpt-5.4-mini",
            estimated_cost_usd=0.01,
            reason="bounded review",
            deferred=False,
            deferral_reason=None,
        )
        plan = autonomy_team.build_company_plan(
            {
                "id": "AUTO-DEPLOY",
                "title": "Deploy one approved ref",
                "agent_owner": "code",
                "authorization_level": "external_action",
                "revenue_sprint_id": "sprint-1",
                "external_action": action,
                "campaign_action_binding": binding,
                "acceptance_criteria": ["The exact project and ref are preserved."],
            },
            worker_decision,
            5.0,
            router=review_router,
        )
        self.assertFalse(plan["deferred"])
        self.assertTrue(all(
            task["campaign_action_binding"] == binding for task in plan["tasks"]
        ))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "company.json"
            company_mode.set_daily_budget(5.0, path)
            company_mode.assign_goal(
                "Draft and review one exact deploy action",
                ["code", "editor"],
                ["code", "editor"],
                path,
                tasks=plan["tasks"],
            )
            _message, project_id = company_mode.approve_project(
                path, notify_hooks=False
            )
            company_mode.set_project_revision_flag(
                project_id,
                "REVISIONS REQUIRED: clarify the evidence without changing the ref.",
                path,
            )
            created, _note = company_mode.start_revision_round(
                project_id, ["code", "editor"], path
            )
            self.assertTrue(created)
            state = company_mode.load_state(path)
            revised = company_mode.project_tasks(state, project_id)[-2:]
            self.assertTrue(all(
                task["campaign_action_binding"] == binding for task in revised
            ))


class RevenueActionCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.group, cls.fake_main = import_group_bot_with_stub_main()

    @classmethod
    def tearDownClass(cls):
        import sys

        sys.modules.pop("group_bot", None)

    async def test_non_promotional_day_six_action_has_no_fake_copy_control(self):
        sprint = {
            "checkpoint_results": [
                {"day": 5, "decision": "continue", "evidence": {"sales": 1}}
            ]
        }
        purchase = {
            "revenue_sprint_run_day": 6,
            "external_action": {"action_type": "purchase"},
        }
        self.assertEqual(
            self.group._campaign_experiment_control(purchase, sprint), {}
        )

    async def test_readiness_binding_reaches_the_company_plan(self):
        action = {
            "action_type": "deploy",
            "target": "vercel:company-site",
            "policy_revision": "policy-1",
        }
        item = {
            "id": "AUTO-DEPLOY",
            "title": "Deploy",
            "authorization_level": "external_action",
            "revenue_sprint_id": "sprint-1",
            "external_action": action,
            "acceptance_criteria": ["Use the exact reviewed ref."],
        }
        sprint = {
            "id": "sprint-1",
            "status": "active",
            "product": {"gumroad_url": "https://company.gumroad.com/l/kit"},
            "automation_policy": {
                "revision": "policy-1",
                "allowed_action_types": ["deploy"],
                "allowed_targets": {"deploy": ["vercel:company-site"]},
            },
        }
        observed = {}

        def build_plan(value, *_args, **_kwargs):
            observed.update(value)
            return {
                "deferred": True,
                "reason": "No ordinary budget remains.",
                "deferral_reason": "insufficient_budget",
                "decisions": [
                    {"deferred": True, "deferral_reason": "insufficient_budget"}
                ],
            }

        with patch.object(
            self.group.company_mode, "load_state", return_value={"revenue_sprints": [sprint]}
        ), patch.object(
            self.group.company_mode, "active_revenue_sprint", return_value=sprint
        ), patch.object(
            self.group, "_autonomy_runtime_deferral", new=AsyncMock(return_value=None)
        ), patch.object(
            self.group.revenue_actions,
            "revenue_action_target_readiness",
            return_value={
                "ready": True,
                "draft_binding": {
                    "schema": "deploy",
                    "fixed_fields": {
                        "account_id": "team-1",
                        "project": "site",
                        "ref": "main",
                    },
                },
            },
        ), patch.object(
            self.group, "_campaign_experiment_control", return_value={}
        ), patch.object(
            self.group, "_revenue_sprint_evidence_snapshot", return_value={}
        ), patch.object(
            self.group, "_company_budget_snapshot", return_value={"remaining_usd": 0.0}
        ), patch.object(
            self.group.autonomy_team, "build_company_plan", side_effect=build_plan
        ):
            result = await self.group._execute_autonomy_item(
                {"id": "assistant"},
                item,
                types.SimpleNamespace(model_id="gpt-5.4-mini"),
                "run-1",
            )

        self.assertEqual(result["status"], "deferred")
        self.assertEqual(
            observed["campaign_action_binding"],
            {"account_id": "team-1", "project": "site", "ref": "main"},
        )

    async def test_purchase_uses_reviewed_amount_for_capability_and_exact_receipt(self):
        digest = "a" * 64
        project_id = "proj-purchase"
        campaign_id = "sprint-1"
        run_id = "run-1"
        target = "vendor:company-plan"
        project = {
            "id": project_id,
            "status": "active",
            "campaign_id": campaign_id,
            "revenue_sprint_run_id": run_id,
            "editor_verdict": "approved",
            "revision_round": 0,
            "external_action": {
                "action_type": "purchase",
                "target": target,
                "policy_revision": "policy-1",
            },
        }
        initial = {
            "projects": [project],
            "tasks": [
                {
                    "id": "worker-1",
                    "project_id": project_id,
                    "owner": "finance",
                    "status": "done",
                    "revision_round": 0,
                    "result": "CAMPAIGN_DRAFT_JSON: {}",
                },
                {
                    "id": "review-1",
                    "project_id": project_id,
                    "owner": "editor",
                    "status": "done",
                    "revision_round": 0,
                    "result": "APPROVED",
                },
            ],
            "revenue_sprints": [
                {
                    "id": campaign_id,
                    "status": "active",
                    "product": {"gumroad_url": "https://company.gumroad.com/l/kit"},
                    "action_journal": [],
                }
            ],
        }
        final = json.loads(json.dumps(initial))
        final["revenue_sprints"][0]["action_journal"] = [
            {
                "id": "action-1",
                "run_id": run_id,
                "action_type": "purchase",
                "target": target,
                "status": "succeeded",
                "metadata": {"payload_digest": digest},
                "provider_receipt": {"receipt_id": "purchase-1"},
            }
        ]
        parsed = {
            "payload": {
                "action_type": "purchase",
                "target": target,
                "amount_usd": 1.25,
                "payload": {"item": "company plan"},
            },
            "payload_digest": digest,
        }
        capability = {
            "allowed": True,
            "action_type": "purchase",
            "target": target,
        }
        with patch.object(
            self.group.company_mode, "load_state", side_effect=[initial, final]
        ), patch.object(
            self.group.revenue_actions, "parse_campaign_draft", return_value=parsed
        ), patch.object(
            self.group.company_mode,
            "bind_approved_revenue_action",
            return_value={"payload_digest": digest},
        ), patch.object(
            self.group.company_mode,
            "revenue_action_capability",
            return_value=capability,
        ) as capability_call, patch.object(
            self.group.revenue_actions,
            "execute_approved_campaign_draft",
            return_value="Campaign purchase succeeded: receipt.",
        ) as execute:
            result = await self.group._execute_approved_campaign_action(project_id)

        self.assertEqual(result["action_id"], "action-1")
        self.assertEqual(result["action_type"], "purchase")
        self.assertEqual(
            capability_call.call_args.kwargs["purchase_amount_usd"], 1.25
        )
        execute.assert_called_once_with(
            capability, run_id, parsed, dry_run=False
        )


if __name__ == "__main__":
    unittest.main()

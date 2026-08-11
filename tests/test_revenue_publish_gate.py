import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import company_mode
import revenue_actions
from tests.test_group_autonomy import import_group_bot_with_stub_main


PHOENIX = ZoneInfo("America/Phoenix")
TARGET = "bluesky:company.example"
PRODUCT_URL = "https://company.gumroad.com/l/outreach-kit"
POLICY_REVISION = "owner-policy-r1"


class RevenuePublishGateTests(unittest.IsolatedAsyncioTestCase):
    """Integration coverage for draft -> Vera -> deterministic provider I/O."""

    @classmethod
    def setUpClass(cls):
        cls.group, cls.fake_main = import_group_bot_with_stub_main()

    @classmethod
    def tearDownClass(cls):
        import sys

        sys.modules.pop("group_bot", None)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "company_state.json"
        self.moment = datetime(2026, 8, 10, 8, tzinfo=PHOENIX)
        self.run_id = "revenue-run-1"
        self.provider_call = Mock(
            return_value=revenue_actions.ProviderOutcome(
                "succeeded", "Mock provider accepted one company post."
            )
        )
        self.provider_payloads = []
        self.fake_main.pending_actions.clear()

        state = company_mode.load_state(self.path)
        state["products"].append({
            "project_id": "product-project",
            "title": "Company Outreach Kit",
            "gumroad_url": PRODUCT_URL,
            "gumroad_product_id": "gumroad-product-1",
            "sales_count": 0,
            "revenue_usd": 0.0,
            "last_synced": None,
        })
        company_mode.save_state(state, self.path)
        self.sprint = company_mode.start_revenue_sprint(
            {
                "project_id": "product-project",
                "title": "Company Outreach Kit",
                "gumroad_url": PRODUCT_URL,
                "gumroad_product_id": "gumroad-product-1",
                "ownership": "company_owned",
                "personal_fallback_allowed": False,
            },
            {
                "type": "social",
                "account_id": "company.example",
                "destination_scope": TARGET,
                "name": "Company Bluesky",
                "ownership": "company_owned",
                "personal_fallback_allowed": False,
            },
            {
                "revision": POLICY_REVISION,
                "allowed_action_types": ["publish"],
                "allowed_targets": {"publish": [TARGET]},
                "daily_action_caps": {"publish": 1},
                "total_action_caps": {"publish": 20},
                "purchase_daily_cap_usd": 0.0,
                "purchase_total_cap_usd": 0.0,
                "approved_at": "2026-08-02T12:00:00-07:00",
                "approved_by": "company-owner",
            },
            self.path,
            sprint_id="revenue-sprint-1",
            max_consecutive_no_progress_days=30,
        )
        company_mode.claim_revenue_sprint_run(
            self.run_id,
            {
                "id": "experiment-1",
                "hypothesis": "One bounded company post can generate measurable interest.",
                "metric": "qualified visits",
                "success_threshold": ">= 1 qualified visit",
                "action_type": "publish",
            },
            self.path,
            sprint_id=self.sprint["id"],
            at=self.moment,
        )
        company_mode.assign_goal(
            "Publish one review-gated company post",
            ["marketing", "editor"],
            specialist_keys=["marketing", "editor"],
            path=self.path,
            tasks=[
                {
                    "owner": "marketing",
                    "title": "Draft the exact bounded company post",
                    "estimate_usd": 0.01,
                    "authorization_level": "propose",
                    "enforce_authorization": True,
                    "campaign_external_action": {
                        "action_type": "publish",
                        "target": TARGET,
                        "policy_revision": POLICY_REVISION,
                    },
                    "campaign_product_url": PRODUCT_URL,
                },
                {
                    "owner": "editor",
                    "title": "Review the exact campaign draft",
                    "estimate_usd": 0.01,
                    "authorization_level": "observe",
                    "enforce_authorization": True,
                },
            ],
            project_metadata={
                "source": "test",
                "autonomous_run_id": self.run_id,
                "authorization_level": "external_action",
                "campaign_id": self.sprint["id"],
                "revenue_sprint_run_id": self.run_id,
                "external_action": {
                    "action_type": "publish",
                    "target": TARGET,
                    "policy_revision": POLICY_REVISION,
                },
            },
        )
        _, self.project_id = company_mode.approve_project(
            self.path, notify_hooks=False
        )

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def draft(copy):
        return revenue_actions.CAMPAIGN_DRAFT_JSON_PREFIX + json.dumps(
            {
                "action_type": "publish",
                "target": TARGET,
                "text": f"{copy} {PRODUCT_URL}",
                "url": PRODUCT_URL,
            },
            sort_keys=True,
        )

    def _project_and_tasks(self):
        state = company_mode.load_state(self.path)
        project = next(
            row for row in state["projects"] if row["id"] == self.project_id
        )
        tasks = company_mode.project_tasks(state, self.project_id)
        return project, tasks

    def _finish_current_round(self, worker_result, editor_result):
        project, tasks = self._project_and_tasks()
        current_round = int(project.get("revision_round", 0) or 0)
        worker = next(
            row
            for row in tasks
            if row["owner"] != "editor"
            and row["status"] == "planned"
            and int(row.get("revision_round", 0) or 0) == current_round
        )
        editor = next(
            row
            for row in tasks
            if row["owner"] == "editor"
            and row["status"] == "planned"
            and int(row.get("revision_round", 0) or 0) == current_round
        )
        company_mode.update_task_status(
            worker["id"], "done", worker_result, [], 0.0, self.path
        )
        company_mode.update_task_status(
            editor["id"],
            "done",
            editor_result,
            [],
            0.0,
            self.path,
            feedback=editor_result,
        )
        verdict = company_mode.set_project_revision_flag(
            self.project_id, editor_result, self.path
        )
        return worker, editor, verdict

    @contextmanager
    def _temporary_state_defaults(self):
        """Route production functions with import-time default paths to this test ledger."""

        real_load = company_mode.load_state
        real_bind = company_mode.bind_approved_revenue_action
        real_capability = company_mode.revenue_action_capability
        real_claim = company_mode.claim_revenue_action
        real_complete = company_mode.complete_revenue_action

        with ExitStack() as stack:
            stack.enter_context(patch.object(
                company_mode,
                "load_state",
                side_effect=lambda *_args, **_kwargs: real_load(self.path),
            ))
            stack.enter_context(patch.object(
                company_mode,
                "bind_approved_revenue_action",
                side_effect=lambda project_id, worker_id, digest, *_args, **_kwargs: real_bind(
                    project_id, worker_id, digest, self.path
                ),
            ))
            stack.enter_context(patch.object(
                company_mode,
                "revenue_action_capability",
                side_effect=lambda action_type, target, *_args, **kwargs: real_capability(
                    action_type,
                    target,
                    self.path,
                    sprint_id=kwargs.get("sprint_id"),
                    purchase_amount_usd=kwargs.get("purchase_amount_usd", 0.0),
                    policy_revision=kwargs.get("policy_revision"),
                    at=self.moment,
                ),
            ))
            stack.enter_context(patch.object(
                company_mode,
                "claim_revenue_action",
                side_effect=lambda action_type, target, run_id, *_args, **kwargs: real_claim(
                    action_type,
                    target,
                    run_id,
                    self.path,
                    sprint_id=kwargs.get("sprint_id"),
                    purchase_amount_usd=kwargs.get("purchase_amount_usd", 0.0),
                    policy_revision=kwargs.get("policy_revision"),
                    approved_payload_digest=kwargs.get("approved_payload_digest"),
                    idempotency_key=kwargs.get("idempotency_key"),
                    metadata=kwargs.get("metadata"),
                    at=self.moment,
                ),
            ))
            stack.enter_context(patch.object(
                company_mode,
                "complete_revenue_action",
                side_effect=lambda action_id, status, *_args, **kwargs: real_complete(
                    action_id,
                    status,
                    self.path,
                    sprint_id=kwargs.get("sprint_id"),
                    actual_purchase_usd=kwargs.get("actual_purchase_usd"),
                    result=kwargs.get("result", ""),
                    at=self.moment,
                ),
            ))
            yield

    def _mock_publish_adapter(self, target, text, url=None, *, dry_run=None):
        payload = {
            "action_type": "publish",
            "target": target,
            "text": text,
            "url": str(url or ""),
        }
        self.provider_payloads.append(payload)
        return revenue_actions._execute(
            "publish",
            target,
            payload,
            self.provider_call,
            dry_run=dry_run,
        )

    @contextmanager
    def _mocked_provider(self):
        with patch.object(
            revenue_actions,
            "_provider_ready",
            return_value={"account_id": "company.example"},
        ), patch.object(
            revenue_actions,
            "publish_bluesky",
            side_effect=self._mock_publish_adapter,
        ):
            yield

    async def test_revisions_required_review_yields_zero_provider_calls(self):
        _, _, verdict = self._finish_current_round(
            self.draft("Initial reviewed campaign copy."),
            "REVISIONS REQUIRED: make the value proposition more specific.",
        )
        self.assertEqual(verdict, "revise")

        with self._temporary_state_defaults(), self._mocked_provider():
            with self.assertRaises(company_mode.RevenueActionError):
                await self.group._publish_approved_campaign_draft(self.project_id)

        self.provider_call.assert_not_called()
        project, _ = self._project_and_tasks()
        self.assertFalse(
            (project.get("approved_revenue_action") or {}).get("payload_digest")
        )
        self.assertEqual(
            company_mode.revenue_sprint_status(
                self.path, sprint_id=self.sprint["id"], at=self.moment
            )["action_journal"],
            [],
        )

    async def test_approved_exact_draft_persists_one_digest_bound_receipt(self):
        worker, _, verdict = self._finish_current_round(
            self.draft("A concise approved company offer."),
            "APPROVED: the exact draft meets every acceptance criterion.",
        )
        self.assertEqual(verdict, "approved")

        with self._temporary_state_defaults(), self._mocked_provider():
            result = await self.group._publish_approved_campaign_draft(
                self.project_id
            )

        self.provider_call.assert_called_once()
        project, _ = self._project_and_tasks()
        sprint = company_mode.revenue_sprint_status(
            self.path, sprint_id=self.sprint["id"], at=self.moment
        )
        receipts = sprint["action_journal"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["status"], "succeeded")
        self.assertEqual(result["worker_task_id"], worker["id"])
        self.assertEqual(result["action_id"], receipts[0]["id"])
        self.assertEqual(
            receipts[0]["metadata"]["payload_digest"],
            project["approved_revenue_action"]["payload_digest"],
        )
        self.assertEqual(
            result["payload_digest"],
            project["approved_revenue_action"]["payload_digest"],
        )

    async def test_revision_publishes_only_latest_approved_draft(self):
        original = self.draft("Original copy that Vera rejected.")
        _, _, first_verdict = self._finish_current_round(
            original,
            "REVISIONS REQUIRED: state the buyer outcome explicitly.",
        )
        self.assertEqual(first_verdict, "revise")
        created, _ = company_mode.start_revision_round(
            self.project_id, ["marketing", "editor"], self.path
        )
        self.assertTrue(created)
        revised = self.draft("Revised copy states the buyer outcome clearly.")
        revised_worker, _, final_verdict = self._finish_current_round(
            revised,
            "APPROVED: the latest revision meets every explicit criterion.",
        )
        self.assertEqual(final_verdict, "approved")

        with self._temporary_state_defaults(), self._mocked_provider():
            result = await self.group._publish_approved_campaign_draft(
                self.project_id
            )

        self.provider_call.assert_called_once()
        self.assertEqual(len(self.provider_payloads), 1)
        published_payload = self.provider_payloads[0]
        self.assertIn("Revised copy", published_payload["text"])
        self.assertNotIn("Original copy", published_payload["text"])
        self.assertEqual(result["worker_task_id"], revised_worker["id"])
        old_digest = revenue_actions.parse_campaign_draft(
            original,
            action_type="publish",
            target=TARGET,
            product_url=PRODUCT_URL,
        )["payload_digest"]
        project, _ = self._project_and_tasks()
        self.assertNotEqual(
            project["approved_revenue_action"]["payload_digest"], old_digest
        )

    async def test_direct_model_external_action_is_blocked_before_model_or_tool(self):
        task = {
            "id": "direct-external-action",
            "owner": "marketing",
            "title": "Attempt direct publish",
            "authorization_level": "external_action",
            "execution_attempts": 0,
            "attempt_history": [],
        }
        project = {"id": "project-direct", "title": "Direct", "goal": "Do not run"}
        sink = {
            "cost_usd": 0.0,
            "artifacts": [],
            "usage_records": [],
            "context": "test",
        }
        with patch.object(
            self.group.company_mode, "update_task_status", return_value="ok"
        ) as update, patch.object(
            self.group, "_company_task_route"
        ) as route, patch.object(
            self.group, "_task_allowed_tools"
        ) as tools, patch.object(
            self.group, "_invoke_company_agent", new=AsyncMock()
        ) as invoke, patch.object(
            self.group, "post_to_group", new=AsyncMock()
        ):
            outcome = await self.group._execute_routed_task(
                project, task, "marketing", "prompt", sink
            )

        self.assertEqual(outcome, "blocked")
        route.assert_not_called()
        tools.assert_not_called()
        invoke.assert_not_awaited()
        terminal = update.call_args
        self.assertEqual(terminal.args[1], "needs_human")
        self.assertEqual(terminal.kwargs["failure_classification"], "permission")

    async def test_dry_run_invokes_no_provider_and_writes_no_action_claim(self):
        parsed = revenue_actions.parse_campaign_draft(
            self.draft("Dry-run company copy."),
            action_type="publish",
            target=TARGET,
            product_url=PRODUCT_URL,
        )
        capability = company_mode.revenue_action_capability(
            "publish",
            TARGET,
            self.path,
            sprint_id=self.sprint["id"],
            policy_revision=POLICY_REVISION,
            at=self.moment,
        )
        self.assertTrue(capability["allowed"])

        with self._temporary_state_defaults(), self._mocked_provider():
            result = revenue_actions.execute_approved_campaign_draft(
                capability,
                self.run_id,
                parsed,
                dry_run=True,
            )

        self.assertIn("DRY RUN", result)
        self.provider_call.assert_not_called()
        sprint = company_mode.revenue_sprint_status(
            self.path, sprint_id=self.sprint["id"], at=self.moment
        )
        self.assertEqual(sprint["action_journal"], [])


if __name__ == "__main__":
    unittest.main()

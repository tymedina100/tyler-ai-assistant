import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import autonomous_workflow as autonomy


CHANNEL_ID = "bluesky:freelanceremailkit.bsky.social"
POLICY_REVISION = "bluesky-publish-v1"


def state_with_items(items):
    return {
        "version": 1,
        "schema_version": 1,
        "projects": [
            {
                "id": "project-a",
                "name": "Project A",
                "status": "active",
                "priority": 10,
                "goals": [{"id": "goal-a", "title": "Ship safely", "status": "active"}],
                "roadmap_items": items,
            }
        ],
        "idea_backlog": [],
        "run_control": {
            "active_run": None,
            "scheduled_dates": {},
            "stale_recoveries": [],
            "recent_runs": [],
        },
        "budget_tracking": {
            "date": None,
            "actual_or_reconciled_cost_usd": 0.0,
            "cost_is_estimated": True,
        },
    }


def ordinary_item(item_id, priority=10):
    return {
        "id": item_id,
        "goal_id": "goal-a",
        "title": f"Task {item_id}",
        "description": "Produce one bounded result.",
        "priority": priority,
        "status": "ready",
        "dependencies": [],
        "blockers": [],
        "acceptance_criteria": ["The bounded result exists."],
        "agent_owner": "manager",
        "task_type": "status_update",
        "complexity": "lightweight",
        "risk": "low",
        "required_capabilities": ["text"],
        "authorization_level": "observe",
        "previous_attempts": [],
        "previous_models": [],
        "human_decision_required": False,
        "human_action": "",
    }


def pack_item(day, *, action=None):
    item_id = f"SPRINT-{day:02d}"
    value = {
        "id": item_id,
        "goal_id": "goal-sprint",
        "title": f"Run-day {day} validation",
        "description": f"Complete the bounded validation planned for run day {day}.",
        "priority": 200 - day,
        "status": "ready",
        "dependencies": [],
        "blockers": [],
        "acceptance_criteria": [f"Run day {day} produces one evidence-backed result."],
        "agent_owner": "manager",
        "task_type": "status_update",
        "complexity": "lightweight",
        "risk": "low",
        "required_capabilities": ["text"],
        "authorization_level": "propose",
        "estimated_input_tokens": 1000,
        "estimated_cached_input_tokens": 0,
        "estimated_output_tokens": 250,
        "estimated_ai_cost_usd": 0.02,
        "requires_recent_run_evidence": False,
        "previous_attempts": [],
        "previous_models": [],
        "human_decision_required": False,
        "human_action": "",
        "revenue_sprint_run_day": day,
    }
    if action is not None:
        value["authorization_level"] = "external_action"
        value["external_action"] = {
            "action_type": action["action_type"],
            "target": action["target"],
            "policy_revision": POLICY_REVISION,
        }
    return value


def revenue_sprint(actions=None):
    allowed_actions = actions or [
        {
            "action_type": "publish",
            "target": CHANNEL_ID,
            "daily_cap": 1,
            "total_cap": 7,
        }
    ]
    policy = {
        "revision": POLICY_REVISION,
        "require_owner_confirmation": True,
        "allowed_external_actions": deepcopy(allowed_actions),
    }
    if any(action["action_type"] == "purchase" for action in allowed_actions):
        policy.update(daily_purchase_cap_usd=2.0, total_purchase_cap_usd=10.0)
    return {
        "id": "freelancer-cold-email-20d",
        "product": {
            "id": "freelancer-cold-email",
            "name": "Freelancer Cold-Email Starter Pack",
            "url": "https://tymedina.gumroad.com/l/freelancer-cold-email",
        },
        "channel": {"id": CHANNEL_ID},
        "total_ai_budget_usd": 100.0,
        "daily_ai_budget_usd": 5.0,
        "daily_budget_includes_emergency_reserve": True,
        "run_days": 20,
        "checkpoint_thresholds": {
            "day_5_meaningful_interest": {
                "run_day": 5,
                "minimum_meaningful_interactions": 1,
            },
            "day_15_sale_or_strong_intent": {
                "run_day": 15,
                "minimum_sales": 1,
                "minimum_strong_intent_signals": 1,
                "satisfy": "any",
            },
            "day_20_unconditional_stop": {
                "run_day": 20,
                "unconditional_stop": True,
            },
            "max_consecutive_no_progress_days": 3,
            "trailing_window_days": 7,
            "minimum_gross_revenue_usd_per_day": 5.0,
            "minimum_trailing_gross_revenue_usd": 35.0,
            "require_nonnegative_contribution": True,
        },
        "action_policy": policy,
    }


def sprint_pack(actions=None):
    allowed_actions = actions or [
        {
            "action_type": "publish",
            "target": CHANNEL_ID,
            "daily_cap": 1,
            "total_cap": 7,
        }
    ]
    first_action = allowed_actions[0]
    return {
        "schema_version": 1,
        "manifest_id": "revenue-sprint-test",
        "summary": "Queue one bounded 20-day revenue validation sprint.",
        "target_project_id": "project-a",
        "goal": {
            "id": "goal-sprint",
            "title": "Validate one product and one acquisition channel",
            "description": "Stop on checkpoint failure and never exceed the action policy.",
            "status": "active",
        },
        "roadmap_items": [
            pack_item(day, action=first_action if day == 1 else None)
            for day in range(1, 21)
        ],
        "revenue_sprint": revenue_sprint(allowed_actions),
    }


def ordinary_pack():
    value = sprint_pack()
    value["manifest_id"] = "ordinary-pack"
    value["goal"]["id"] = "goal-ordinary"
    value["roadmap_items"] = [pack_item(1)]
    value["roadmap_items"][0].update(id="ORDINARY-01", goal_id="goal-ordinary")
    value["roadmap_items"][0].pop("revenue_sprint_run_day")
    value.pop("revenue_sprint")
    return value


class RevenueSprintWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.seed = self.root / "seed.json"
        self.state_path = self.root / "autonomy_state.json"
        self.pack_dir = self.root / "packs"
        self.pack_dir.mkdir()
        self.config = autonomy.AutonomyConfig(
            enabled=True,
            dry_run=True,
            data_dir=self.root,
            roadmap_seed_path=self.seed,
            roadmap_pack_dir=self.pack_dir,
            max_authorization=autonomy.AuthorizationLevel.EXTERNAL_ACTION,
            max_tasks_per_run=10,
        )

    def tearDown(self):
        self.temp.cleanup()

    def workflow(self, items=None, **kwargs):
        self.seed.write_text(json.dumps(state_with_items(items or [])), encoding="utf-8")
        return autonomy.AutonomousWorkflow(
            self.config,
            state_path=self.state_path,
            seed_path=self.seed,
            **kwargs,
        )

    def write_pack(self, value):
        path = self.pack_dir / f"{value['manifest_id']}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    @staticmethod
    def decision():
        return SimpleNamespace(
            model_id="test-model",
            estimated_cost_usd=0.02,
            reason="Lowest-cost capable test model.",
            deferred=False,
            deferral_reason="",
        )

    def test_valid_sprint_queues_v2_receipt_and_detects_metadata_tampering(self):
        pack = sprint_pack()
        self.write_pack(pack)
        workflow = self.workflow()

        success, preview = workflow.preview_roadmap_pack(pack["manifest_id"])
        self.assertTrue(success)
        self.assertEqual(preview["item_count"], 20)
        self.assertEqual(preview["revenue_sprint"]["channel"]["id"], CHANNEL_ID)
        success, _ = workflow.queue_roadmap_pack(
            pack["manifest_id"],
            expected_revision=preview["manifest_revision"],
            approval_source="unit_test_owner",
        )
        self.assertTrue(success)

        state = workflow.load_state()
        receipt = state["roadmap_pack_history"][0]
        self.assertEqual(receipt["record_hash_version"], 2)
        self.assertEqual(receipt["revenue_sprint"]["run_days"], 20)
        self.assertEqual(
            state["projects"][0]["roadmap_items"][0]["external_action"]["target"],
            CHANNEL_ID,
        )

        state["projects"][0]["goals"][1]["revenue_sprint"]["channel"]["id"] = (
            "bluesky:tampered.bsky.social"
        )
        workflow.store.save(state)
        intact, message = workflow.preview_roadmap_pack(pack["manifest_id"])
        self.assertFalse(intact)
        self.assertIn("conflicts with its persisted receipt", message)

    def test_sprint_schema_rejects_ambiguous_or_unsafe_contracts(self):
        base = sprint_pack()
        mutations = {
            "multiple_products": lambda value: value["revenue_sprint"].update(
                product=[value["revenue_sprint"]["product"], value["revenue_sprint"]["product"]]
            ),
            "multiple_channels": lambda value: value["revenue_sprint"].update(
                channel=[{"id": CHANNEL_ID}, {"id": "bluesky:second.bsky.social"}]
            ),
            "wildcard_channel": lambda value: value["revenue_sprint"].update(
                channel={"id": "bluesky:*"}
            ),
            "wrong_total_budget": lambda value: value["revenue_sprint"].update(
                total_ai_budget_usd=99.0
            ),
            "wrong_run_days": lambda value: value["revenue_sprint"].update(run_days=21),
            "wrong_checkpoint": lambda value: value["revenue_sprint"][
                "checkpoint_thresholds"
            ]["day_5_meaningful_interest"].update(run_day=6),
            "unknown_action": lambda value: value["revenue_sprint"]["action_policy"][
                "allowed_external_actions"
            ][0].update(action_type="email_everyone"),
            "broad_action_target": lambda value: value["revenue_sprint"]["action_policy"][
                "allowed_external_actions"
            ][0].update(target="bluesky:*"),
            "daily_action_cap_over_total": lambda value: value["revenue_sprint"][
                "action_policy"
            ]["allowed_external_actions"][0].update(daily_cap=8, total_cap=7),
            "purchase_caps_without_purchase": lambda value: value["revenue_sprint"][
                "action_policy"
            ].update(daily_purchase_cap_usd=1.0, total_purchase_cap_usd=2.0),
        }
        workflow = self.workflow()
        workflow.load_state()
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                candidate = deepcopy(base)
                mutate(candidate)
                self.write_pack(candidate)
                success, _message = workflow.preview_roadmap_pack(candidate["manifest_id"])
                self.assertFalse(success)

        purchase = deepcopy(base)
        purchase["revenue_sprint"]["action_policy"]["allowed_external_actions"].append(
            {
                "action_type": "purchase",
                "target": "gumroad:freelancer-cold-email",
                "daily_cap": 1,
                "total_cap": 2,
            }
        )
        self.write_pack(purchase)
        success, message = workflow.preview_roadmap_pack(purchase["manifest_id"])
        self.assertFalse(success)
        self.assertIn("requires both", message)

    def test_all_action_types_are_supported_but_items_must_match_exact_policy(self):
        actions = [
            {"action_type": "publish", "target": CHANNEL_ID, "daily_cap": 1, "total_cap": 4},
            {"action_type": "outreach", "target": CHANNEL_ID, "daily_cap": 2, "total_cap": 8},
            {
                "action_type": "purchase",
                "target": "vendor:campaign-assets",
                "daily_cap": 1,
                "total_cap": 2,
            },
            {
                "action_type": "deploy",
                "target": "railway:freelancer-cold-email-site",
                "daily_cap": 1,
                "total_cap": 4,
            },
        ]
        pack = sprint_pack(actions)
        pack["roadmap_items"] = [
            pack_item(day, action=actions[day - 1] if day <= len(actions) else None)
            for day in range(1, 21)
        ]
        self.write_pack(pack)
        workflow = self.workflow()

        success, _preview = workflow.preview_roadmap_pack(pack["manifest_id"])
        self.assertTrue(success)

        mismatch = deepcopy(pack)
        mismatch["roadmap_items"][0]["external_action"]["target"] = (
            "bluesky:other.bsky.social"
        )
        self.write_pack(mismatch)
        success, message = workflow.preview_roadmap_pack(mismatch["manifest_id"])
        self.assertFalse(success)
        self.assertIn("does not exactly match", message)

        stale_revision = deepcopy(pack)
        stale_revision["roadmap_items"][0]["external_action"]["policy_revision"] = (
            "bluesky-publish-stale"
        )
        self.write_pack(stale_revision)
        success, message = workflow.preview_roadmap_pack(stale_revision["manifest_id"])
        self.assertFalse(success)
        self.assertIn("does not exactly match", message)

    def test_ordinary_pack_remains_v1_and_external_actions_remain_rejected(self):
        pack = ordinary_pack()
        self.write_pack(pack)
        workflow = self.workflow()
        success, preview = workflow.preview_roadmap_pack(pack["manifest_id"])
        self.assertTrue(success)
        success, _ = workflow.queue_roadmap_pack(
            pack["manifest_id"],
            expected_revision=preview["manifest_revision"],
            approval_source="unit_test_owner",
        )
        self.assertTrue(success)
        self.assertNotIn("record_hash_version", workflow.load_state()["roadmap_pack_history"][0])

        unsafe = ordinary_pack()
        unsafe["roadmap_items"][0]["authorization_level"] = "external_action"
        self.write_pack(unsafe)
        fresh = self.workflow()
        success, message = fresh.preview_roadmap_pack(unsafe["manifest_id"])
        self.assertFalse(success)
        self.assertIn("observe/propose", message)

    def test_session_policy_limits_a_sprint_to_one_eligible_item_and_persists_metadata(self):
        pack = sprint_pack()
        self.write_pack(pack)
        executor = Mock(
            return_value={
                "status": "completed",
                "actual_cost_usd": 0.01,
                "model": "test-model",
                "result_text": "One bounded campaign step completed.",
            }
        )
        ideas = Mock()
        workflow = self.workflow(
            executor=executor,
            idea_generator=ideas,
            router=SimpleNamespace(route=Mock(return_value=self.decision())),
        )
        success, preview = workflow.preview_roadmap_pack(pack["manifest_id"])
        self.assertTrue(success)
        success, _ = workflow.queue_roadmap_pack(
            pack["manifest_id"],
            expected_revision=preview["manifest_revision"],
            approval_source="unit_test_owner",
        )
        self.assertTrue(success)

        report = workflow.run_session(
            dry_run=False,
            eligible_item_ids={"SPRINT-01", "SPRINT-02"},
            max_selected_items=1,
            allow_ideation=False,
            report_metadata={"campaign_id": "freelancer-cold-email-20d", "api_key": "must-not-persist"},
        )

        self.assertEqual(executor.call_count, 1)
        self.assertEqual([task["id"] for task in report["tasks_selected"]], ["SPRINT-01"])
        self.assertEqual(report["stop_reason"], "session_policy_item_cap")
        self.assertEqual(report["report_metadata"]["api_key"], "[REDACTED]")
        self.assertFalse(report["session_policy"]["allow_ideation"])
        ideas.assert_not_called()
        state = workflow.load_state()
        items = {item["id"]: item for item in state["projects"][0]["roadmap_items"]}
        self.assertEqual(items["SPRINT-01"]["status"], "completed")
        self.assertEqual(items["SPRINT-02"]["status"], "ready")
        persisted = json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))
        self.assertEqual(persisted["report_metadata"]["api_key"], "[REDACTED]")

    def test_eligibility_filter_and_empty_policy_never_fall_through_to_other_work(self):
        executor = Mock(return_value={"status": "completed", "actual_cost_usd": 0.01})
        ideas = Mock()
        workflow = self.workflow(
            [ordinary_item("high", priority=50), ordinary_item("allowed", priority=10)],
            executor=executor,
            idea_generator=ideas,
            router=SimpleNamespace(route=Mock(return_value=self.decision())),
        )

        report = workflow.run_session(
            dry_run=False,
            eligible_item_ids={"allowed"},
            max_selected_items=1,
            allow_ideation=False,
        )
        self.assertEqual([task["id"] for task in report["tasks_selected"]], ["allowed"])
        self.assertEqual(executor.call_count, 1)

        empty = workflow.run_session(
            dry_run=False,
            eligible_item_ids=set(),
            max_selected_items=1,
            allow_ideation=False,
        )
        self.assertEqual(empty["tasks_selected"], [])
        self.assertEqual(executor.call_count, 1)
        ideas.assert_not_called()

    def test_dry_run_with_campaign_policy_invokes_no_paid_or_external_callback(self):
        executor = Mock()
        ideas = Mock()
        workflow = self.workflow(
            [ordinary_item("campaign-day")],
            executor=executor,
            idea_generator=ideas,
        )

        report = workflow.run_session(
            dry_run=True,
            eligible_item_ids={"campaign-day"},
            max_selected_items=1,
            allow_ideation=False,
        )

        self.assertEqual(report["final_status"], "dry_run")
        self.assertEqual([task["id"] for task in report["tasks_selected"]], ["campaign-day"])
        executor.assert_not_called()
        ideas.assert_not_called()


if __name__ == "__main__":
    unittest.main()

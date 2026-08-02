import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from filelock import FileLock

import autonomous_workflow as autonomy


def roadmap_state(items):
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
        "run_control": {"active_run": None, "scheduled_dates": {}, "stale_recoveries": [], "recent_runs": []},
        "budget_tracking": {"date": None, "actual_or_reconciled_cost_usd": 0.0, "cost_is_estimated": True},
    }


def item(item_id="task-1", **overrides):
    value = {
        "id": item_id,
        "goal_id": "goal-a",
        "title": f"Task {item_id}",
        "description": "Produce one bounded result.",
        "priority": 10,
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
    value.update(overrides)
    return value


def idea_record(idea_id="idea-1", **overrides):
    value = {
        "id": idea_id,
        "idea": "Add a concise autonomous run summary",
        "problem_addressed": "Run outcomes are hard to scan.",
        "expected_value": "Operators can understand outcomes in under 30 seconds.",
        "target_user": "Owner",
        "estimated_effort": "small",
        "estimated_ai_cost_usd": 0.01,
        "risks": ["The summary could omit important evidence."],
        "relationship_to_current_goals": "Improves safe autonomous operations.",
        "recommended_next_validation_step": "Draft three examples and compare scan time.",
        "status": "proposed",
        "authorization_level": "propose",
        "source_run_id": "run-ideas",
    }
    value["fingerprint"] = autonomy._idea_fingerprint(value)
    value.update(overrides)
    return value


def roadmap_pack(manifest_id="test-roadmap-pack", **overrides):
    value = {
        "schema_version": 1,
        "manifest_id": manifest_id,
        "summary": "Queue one bounded test initiative.",
        "target_project_id": "project-a",
        "goal": {
            "id": "goal-pack",
            "title": "Validate roadmap pack queueing",
            "description": "Exercise the owner-approved additive queue path.",
            "status": "active",
        },
        "roadmap_items": [
            {
                "id": "PACK-001",
                "goal_id": "goal-pack",
                "title": "Inspect the queue result",
                "description": "Produce one read-only inspection result.",
                "priority": 100,
                "status": "ready",
                "dependencies": [],
                "blockers": [],
                "acceptance_criteria": ["The inspection is source-backed."],
                "agent_owner": "code",
                "task_type": "review",
                "complexity": "standard",
                "risk": "medium",
                "required_capabilities": ["text", "review"],
                "authorization_level": "observe",
                "estimated_input_tokens": 3000,
                "estimated_output_tokens": 600,
                "requires_recent_run_evidence": False,
                "previous_attempts": [],
                "previous_models": [],
                "human_decision_required": False,
                "human_action": "",
            }
        ],
    }
    value.update(overrides)
    return value


class FakeScheduler:
    def __init__(self):
        self.calls = []

    def add_job(self, callback, trigger, **kwargs):
        record = {"callback": callback, "trigger": trigger, **kwargs}
        self.calls.append(record)
        return record


class AutonomousWorkflowTests(unittest.TestCase):
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
            max_authorization=autonomy.AuthorizationLevel.PROPOSE,
        )

    def tearDown(self):
        self.temp.cleanup()

    def write_seed(self, items):
        self.seed.write_text(json.dumps(roadmap_state(items)), encoding="utf-8")

    def workflow(self, items, **kwargs):
        self.write_seed(items)
        return autonomy.AutonomousWorkflow(
            self.config,
            state_path=self.state_path,
            seed_path=self.seed,
            **kwargs,
        )

    def test_config_defaults_are_safe_and_typed(self):
        with patch.dict(os.environ, {}, clear=True):
            config = autonomy.AutonomyConfig.from_env()
        self.assertFalse(config.enabled)
        self.assertTrue(config.dry_run)
        self.assertEqual(config.schedule_time, "08:00")
        self.assertEqual(config.schedule_days, "mon-fri")
        self.assertEqual(config.timezone, "America/Phoenix")
        self.assertEqual(config.daily_budget_usd, 5.0)
        self.assertEqual(config.max_tasks_per_run, 10)
        self.assertEqual(config.max_ideas_per_run, 3)
        self.assertEqual(config.max_session_minutes, 120)
        self.assertEqual(config.min_task_reservation_usd, 0.05)
        self.assertEqual(config.max_authorization, autonomy.AuthorizationLevel.PROPOSE)
        self.assertEqual(config.roadmap_pack_dir, autonomy.DEFAULT_ROADMAP_PACK_DIR)

    def test_session_limits_are_loaded_from_env_and_bounded(self):
        with patch.dict(os.environ, {
            "AUTONOMY_MAX_TASKS_PER_RUN": "12",
            "AUTONOMY_MAX_IDEAS_PER_RUN": "4",
            "AUTONOMY_MAX_SESSION_MINUTES": "90",
            "AUTONOMY_PROJECT_PACK_DIR": "config/custom-autonomous-projects",
        }, clear=True):
            config = autonomy.AutonomyConfig.from_env()
        self.assertEqual(config.max_tasks_per_run, 12)
        self.assertEqual(config.max_ideas_per_run, 4)
        self.assertEqual(config.max_session_minutes, 90)
        self.assertEqual(config.roadmap_pack_dir, Path("config/custom-autonomous-projects"))

        with patch.dict(os.environ, {
            "AUTONOMY_MAX_TASKS_PER_RUN": "51",
            "AUTONOMY_MAX_SESSION_MINUTES": "0",
        }, clear=True):
            config = autonomy.AutonomyConfig.from_env()
        self.assertEqual(config.max_tasks_per_run, 10)
        self.assertEqual(config.max_session_minutes, 120)

    def write_pack(self, value, manifest_id=None):
        pack_id = manifest_id or value["manifest_id"]
        path = self.pack_dir / f"{pack_id}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_scheduler_registers_weekday_daily_callback(self):
        scheduler = FakeScheduler()
        callback = Mock()
        result = autonomy.register_scheduler(scheduler, callback, self.config)
        self.assertIs(result, scheduler.calls[0])
        self.assertEqual(result["id"], "autonomous-daily-run")
        self.assertEqual(result["max_instances"], 1)
        trigger_text = str(result["trigger"])
        self.assertIn("day_of_week='mon-fri'", trigger_text)
        self.assertIn("hour='8'", trigger_text)
        self.assertIn("minute='0'", trigger_text)

    def test_scheduler_is_not_registered_when_disabled(self):
        scheduler = FakeScheduler()
        disabled = replace(self.config, enabled=False)
        self.assertIsNone(autonomy.register_scheduler(scheduler, Mock(), disabled))
        self.assertEqual(scheduler.calls, [])

    def test_persistent_lock_prevents_overlapping_run(self):
        executor = Mock()
        workflow = self.workflow([item()], executor=executor)
        workflow.base_dir.mkdir(parents=True, exist_ok=True)
        held = FileLock(str(workflow.run_lock_path))
        held.acquire()
        try:
            report = workflow.run(dry_run=False)
        finally:
            held.release()
        self.assertEqual(report["final_status"], "overlap_prevented")
        executor.assert_not_called()

    def test_retry_reset_clears_terminal_fields_and_preserves_attempt_history(self):
        previous_attempts = [
            {
                "run_id": "run-before-access",
                "status": "needs_human",
                "failure_classification": "missing_access",
            }
        ]
        workflow = self.workflow([
            item(
                status="needs_human",
                blockers=[{"status": "resolved", "reason": "Repository access was granted."}],
                human_decision_required=True,
                human_decisions_required=["Grant repository access."],
                human_action="Grant repository access, then retry.",
                failure_classification="missing_access",
                failure_reason="Repository access is missing.",
                last_error="403 Forbidden",
                previous_attempts=previous_attempts,
                previous_models=["worker-model"],
            ),
            item(
                "other-blocked",
                status="blocked",
                blockers=["A separate owner decision is pending."],
                human_decision_required=True,
                human_action="Resolve the separate decision.",
            ),
        ])

        success, message = workflow.retry_item("task-1")

        self.assertTrue(success)
        self.assertIn("reset from 'needs_human' to 'ready'", message)
        self.assertIn("previous attempts were preserved", message)
        self.assertIn("No model was invoked", message)
        self.assertIn("/autorun dry-run", message)
        self.assertIn("/autorun live", message)
        persisted = workflow.load_state()["projects"][0]["roadmap_items"][0]
        self.assertEqual(persisted["status"], "ready")
        self.assertFalse(persisted["human_decision_required"])
        self.assertEqual(persisted["human_action"], "")
        self.assertEqual(
            persisted["blockers"],
            [{"status": "resolved", "reason": "Repository access was granted."}],
        )
        self.assertNotIn("human_decisions_required", persisted)
        self.assertNotIn("failure_classification", persisted)
        self.assertNotIn("failure_reason", persisted)
        self.assertNotIn("last_error", persisted)
        self.assertEqual(persisted["previous_attempts"], previous_attempts)
        self.assertEqual(persisted["previous_models"], ["worker-model"])
        self.assertEqual(
            persisted["human_resolution_history"],
            [{
                "action": "retry",
                "reset_at": persisted["updated_at"],
                "from_status": "needs_human",
                "to_status": "ready",
            }],
        )
        other = workflow.load_state()["projects"][0]["roadmap_items"][1]
        self.assertEqual(other["status"], "blocked")
        self.assertTrue(other["human_decision_required"])
        self.assertEqual(other["blockers"], ["A separate owner decision is pending."])
        self.assertIsNotNone(autonomy.select_actionable_item(workflow.load_state()))

    def test_retry_reset_accepts_blocked_status(self):
        workflow = self.workflow([
            item(
                status="blocked",
                blockers=[{"status": "closed", "reason": "Owner decision supplied"}],
                human_decision_required=True,
                human_action="Choose a direction.",
            )
        ])

        success, message = workflow.retry_item("task-1")

        self.assertTrue(success)
        self.assertIn("reset from 'blocked' to 'ready'", message)
        persisted = workflow.load_state()["projects"][0]["roadmap_items"][0]
        self.assertEqual(persisted["status"], "ready")
        self.assertEqual(
            persisted["blockers"],
            [{"status": "closed", "reason": "Owner decision supplied"}],
        )
        self.assertEqual(persisted["human_resolution_history"][0]["action"], "retry")
        self.assertEqual(
            persisted["human_resolution_history"][0]["from_status"], "blocked"
        )

    def test_retry_reset_rejects_unresolved_blockers_and_missing_acceptance_criteria(self):
        blocked_workflow = self.workflow([
            item(
                status="needs_human",
                blockers=["Repository access is still missing."],
                human_decision_required=True,
                human_action="Grant repository access.",
            )
        ])
        blocked_workflow.load_state()
        before = self.state_path.read_text(encoding="utf-8")

        success, message = blocked_workflow.retry_item("task-1")

        self.assertFalse(success)
        self.assertIn("still has unresolved blockers", message)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

        criteria_seed = self.root / "criteria-seed.json"
        criteria_state = self.root / "criteria-state.json"
        criteria_seed.write_text(
            json.dumps(roadmap_state([
                item(
                    status="needs_human",
                    acceptance_criteria=[],
                    human_decision_required=True,
                )
            ])),
            encoding="utf-8",
        )
        criteria_workflow = autonomy.AutonomousWorkflow(
            self.config, state_path=criteria_state, seed_path=criteria_seed
        )
        criteria_workflow.load_state()
        before = criteria_state.read_text(encoding="utf-8")

        success, message = criteria_workflow.retry_item("task-1")

        self.assertFalse(success)
        self.assertIn("has no acceptance criteria", message)
        self.assertEqual(criteria_state.read_text(encoding="utf-8"), before)

    def test_retry_reset_rejects_inactive_parent_project(self):
        for project_status in ("paused", "archived", "cancelled", "completed"):
            with self.subTest(project_status=project_status):
                seed_path = self.root / f"{project_status}-seed.json"
                state_path = self.root / f"{project_status}-state.json"
                state = roadmap_state([
                    item(status="needs_human", human_decision_required=True)
                ])
                state["projects"][0]["status"] = project_status
                seed_path.write_text(json.dumps(state), encoding="utf-8")
                workflow = autonomy.AutonomousWorkflow(
                    self.config, state_path=state_path, seed_path=seed_path
                )
                workflow.load_state()
                before = state_path.read_text(encoding="utf-8")

                success, message = workflow.retry_item("task-1")

                self.assertFalse(success)
                self.assertIn(f"is '{project_status}'", message)
                self.assertEqual(state_path.read_text(encoding="utf-8"), before)

    def test_retry_resolution_history_is_normalized_and_capped(self):
        history = [
            {
                "action": "retry",
                "reset_at": f"2026-07-{index + 1:02d}T00:00:00+00:00",
                "from_status": "needs_human",
                "to_status": "ready",
                "sequence": index,
            }
            for index in range(55)
        ]
        workflow = self.workflow([
            item(
                status="needs_human",
                human_decision_required=True,
                human_resolution_history=history,
            )
        ])

        success, _message = workflow.retry_item("task-1")

        self.assertTrue(success)
        persisted = workflow.load_state()["projects"][0]["roadmap_items"][0]
        self.assertEqual(len(persisted["human_resolution_history"]), 50)
        self.assertEqual(persisted["human_resolution_history"][0]["sequence"], 6)
        self.assertEqual(persisted["human_resolution_history"][-1]["action"], "retry")
        self.assertEqual(persisted["human_resolution_history"][-1]["to_status"], "ready")

    def test_retry_reset_rejects_unknown_nonterminal_and_duplicate_ids(self):
        workflow = self.workflow([item("ready-item")])
        workflow.load_state()

        success, message = workflow.retry_item("missing-item")
        self.assertFalse(success)
        self.assertIn("was not found", message)
        success, message = workflow.retry_item("ready-item")
        self.assertFalse(success)
        self.assertIn("only 'needs_human' or 'blocked'", message)

        duplicate_seed = self.root / "duplicate-seed.json"
        duplicate_state = self.root / "duplicate-state.json"
        duplicate_seed.write_text(
            json.dumps(roadmap_state([
                item("duplicate", status="needs_human"),
                item("duplicate", status="blocked"),
            ])),
            encoding="utf-8",
        )
        duplicate_workflow = autonomy.AutonomousWorkflow(
            self.config, state_path=duplicate_state, seed_path=duplicate_seed
        )
        success, message = duplicate_workflow.retry_item("duplicate")
        self.assertFalse(success)
        self.assertIn("ambiguous: 2 items match", message)
        statuses = [
            value["status"]
            for value in duplicate_workflow.load_state()["projects"][0]["roadmap_items"]
        ]
        self.assertEqual(statuses, ["needs_human", "blocked"])

    def test_retry_reset_rejects_active_run_claim_and_held_run_lock(self):
        workflow = self.workflow([item(status="needs_human", human_decision_required=True)])
        state = workflow.load_state()
        state["run_control"]["active_run"] = {
            "run_id": "run-active",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "item_id": "task-1",
        }
        workflow.store.save(state)

        success, message = workflow.retry_item("task-1")
        self.assertFalse(success)
        self.assertIn("run-active", message)
        self.assertIn("is active", message)
        self.assertEqual(
            workflow.load_state()["projects"][0]["roadmap_items"][0]["status"],
            "needs_human",
        )

        state = workflow.load_state()
        state["run_control"]["active_run"] = None
        workflow.store.save(state)
        held = FileLock(str(workflow.run_lock_path))
        held.acquire()
        try:
            success, message = workflow.retry_item("task-1")
        finally:
            held.release()
        self.assertFalse(success)
        self.assertIn("persistent run lock", message)
        self.assertEqual(
            workflow.load_state()["projects"][0]["roadmap_items"][0]["status"],
            "needs_human",
        )

    def test_roadmap_pack_preview_apply_and_idempotency_are_additive(self):
        self.write_pack(roadmap_pack())
        workflow = self.workflow([])
        state = workflow.load_state()
        state["idea_backlog"] = [idea_record()]
        state["budget_tracking"].update(
            date="2026-08-02",
            actual_or_reconciled_cost_usd=1.25,
            cost_is_estimated=False,
        )
        state["run_control"]["recent_runs"] = [{"run_id": "run-before-pack"}]
        workflow.store.save(state)
        before = self.state_path.read_text(encoding="utf-8")

        success, preview = workflow.preview_roadmap_pack("test-roadmap-pack")

        self.assertTrue(success)
        self.assertFalse(preview["already_queued"])
        self.assertEqual(preview["roadmap_item_ids"], ["PACK-001"])
        self.assertEqual(preview["authorization_levels"], ["observe"])
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

        success, message = workflow.queue_roadmap_pack(
            "test-roadmap-pack",
            expected_revision=preview["manifest_revision"],
            approval_source="unit_test_owner",
        )

        self.assertTrue(success)
        self.assertIn("No model was invoked", message)
        persisted = workflow.load_state()
        project = persisted["projects"][0]
        self.assertEqual(len(project["goals"]), 2)
        self.assertEqual(len(project["roadmap_items"]), 1)
        self.assertEqual(project["roadmap_items"][0]["source_manifest_id"], "test-roadmap-pack")
        self.assertEqual(persisted["idea_backlog"][0]["id"], "idea-1")
        self.assertEqual(persisted["budget_tracking"]["actual_or_reconciled_cost_usd"], 1.25)
        self.assertEqual(persisted["run_control"]["recent_runs"][0]["run_id"], "run-before-pack")
        self.assertEqual(len(persisted["roadmap_pack_history"]), 1)
        self.assertEqual(autonomy.select_actionable_item(persisted)["id"], "PACK-001")
        backups = list(self.root.glob("autonomy_state.json.before-test-roadmap-pack-*.json"))
        self.assertEqual(len(backups), 1)

        again, again_message = workflow.queue_roadmap_pack(
            "test-roadmap-pack",
            expected_revision=preview["manifest_revision"],
            approval_source="unit_test_owner",
        )
        self.assertTrue(again)
        self.assertIn("already queued", again_message)
        self.assertEqual(len(workflow.load_state()["projects"][0]["roadmap_items"]), 1)
        self.assertEqual(
            len(list(self.root.glob("autonomy_state.json.before-test-roadmap-pack-*.json"))),
            1,
        )

        altered = workflow.load_state()
        altered_item = altered["projects"][0]["roadmap_items"][0]
        altered_item["acceptance_criteria"] = []
        workflow.store.save(altered)
        intact, mismatch = workflow.preview_roadmap_pack("test-roadmap-pack")
        self.assertFalse(intact)
        self.assertIn("conflicts with its persisted receipt", mismatch)

        altered_item["acceptance_criteria"] = ["The inspection is source-backed."]
        duplicate_project = json.loads(json.dumps(altered["projects"][0]))
        duplicate_project["id"] = "project-duplicate"
        duplicate_project["name"] = "Duplicate Project"
        duplicate_project["goals"] = []
        duplicate_project["roadmap_items"] = [json.loads(json.dumps(altered_item))]
        altered["projects"].append(duplicate_project)
        workflow.store.save(altered)
        intact, mismatch = workflow.preview_roadmap_pack("test-roadmap-pack")
        self.assertFalse(intact)
        self.assertIn("conflicts with its persisted receipt", mismatch)

    def test_roadmap_pack_revalidates_revision_and_rejects_unsafe_records(self):
        pack = roadmap_pack()
        pack_path = self.write_pack(pack)
        workflow = self.workflow([])
        workflow.load_state()
        success, preview = workflow.preview_roadmap_pack("test-roadmap-pack")
        self.assertTrue(success)

        changed = roadmap_pack()
        changed["roadmap_items"][0]["title"] = "Changed after owner review"
        pack_path.write_text(json.dumps(changed), encoding="utf-8")
        before = self.state_path.read_text(encoding="utf-8")
        success, message = workflow.queue_roadmap_pack(
            "test-roadmap-pack",
            expected_revision=preview["manifest_revision"],
            approval_source="unit_test_owner",
        )
        self.assertFalse(success)
        self.assertIn("changed after approval", message)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

        unsafe = roadmap_pack()
        unsafe["roadmap_items"][0]["authorization_level"] = "external_action"
        pack_path.write_text(json.dumps(unsafe), encoding="utf-8")
        success, message = workflow.preview_roadmap_pack("test-roadmap-pack")
        self.assertFalse(success)
        self.assertIn("observe/propose", message)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

        cyclic = roadmap_pack()
        second = dict(cyclic["roadmap_items"][0])
        second["id"] = "PACK-002"
        second["title"] = "Inspect the second result"
        cyclic["roadmap_items"][0]["dependencies"] = ["PACK-002"]
        second["dependencies"] = ["PACK-001"]
        cyclic["roadmap_items"].append(second)
        pack_path.write_text(json.dumps(cyclic), encoding="utf-8")
        success, message = workflow.preview_roadmap_pack("test-roadmap-pack")
        self.assertFalse(success)
        self.assertIn("contain a cycle", message)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

        state = workflow.load_state()
        state["projects"][0]["roadmap_items"] = [
            item("OLD-ITEM", dependencies=["PACK-001"])
        ]
        workflow.store.save(state)
        cross_cycle = roadmap_pack()
        cross_cycle["roadmap_items"][0]["dependencies"] = ["OLD-ITEM"]
        pack_path.write_text(json.dumps(cross_cycle), encoding="utf-8")
        before_cycle_preview = self.state_path.read_text(encoding="utf-8")
        success, message = workflow.preview_roadmap_pack("test-roadmap-pack")
        self.assertFalse(success)
        self.assertIn("contain a cycle", message)
        self.assertEqual(
            self.state_path.read_text(encoding="utf-8"), before_cycle_preview
        )

    def test_roadmap_pack_rejects_receiptless_manifest_reuse(self):
        self.write_pack(roadmap_pack())
        workflow = self.workflow([])
        success, preview = workflow.preview_roadmap_pack("test-roadmap-pack")
        self.assertTrue(success)
        success, _ = workflow.queue_roadmap_pack(
            "test-roadmap-pack",
            expected_revision=preview["manifest_revision"],
            approval_source="unit_test_owner",
        )
        self.assertTrue(success)
        state = workflow.load_state()
        state["roadmap_pack_history"][0]["roadmap_item_hashes"] = [1]
        workflow.store.save(state)
        success, message = workflow.preview_roadmap_pack("test-roadmap-pack")
        self.assertFalse(success)
        self.assertIn("conflicts with its persisted receipt", message)

        state = workflow.load_state()
        state["roadmap_pack_history"] = []
        workflow.store.save(state)
        before = self.state_path.read_text(encoding="utf-8")

        success, message = workflow.preview_roadmap_pack("test-roadmap-pack")

        self.assertFalse(success)
        self.assertIn("imported records but no persisted receipt", message)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

    def test_roadmap_pack_respects_active_claim_and_run_lock(self):
        self.write_pack(roadmap_pack())
        workflow = self.workflow([])
        state = workflow.load_state()
        state["run_control"]["active_run"] = {"run_id": "run-active-pack"}
        workflow.store.save(state)
        success, message = workflow.preview_roadmap_pack("test-roadmap-pack")
        self.assertFalse(success)
        self.assertIn("run-active-pack", message)

        state["run_control"]["active_run"] = None
        workflow.store.save(state)
        held = FileLock(str(workflow.run_lock_path))
        held.acquire()
        try:
            success, message = workflow.preview_roadmap_pack("test-roadmap-pack")
        finally:
            held.release()
        self.assertFalse(success)
        self.assertIn("persistent run lock", message)

    def test_roadmap_pack_primary_write_failure_preserves_state_and_backup(self):
        self.write_pack(roadmap_pack())
        workflow = self.workflow([])
        workflow.load_state()
        success, preview = workflow.preview_roadmap_pack("test-roadmap-pack")
        self.assertTrue(success)
        before = self.state_path.read_text(encoding="utf-8")
        real_atomic_write = autonomy._atomic_write_json

        def fail_primary(path, value):
            if Path(path) == self.state_path:
                raise OSError("simulated primary write failure")
            return real_atomic_write(path, value)

        with patch.object(autonomy, "_atomic_write_json", side_effect=fail_primary):
            with self.assertRaises(OSError):
                workflow.queue_roadmap_pack(
                    "test-roadmap-pack",
                    expected_revision=preview["manifest_revision"],
                    approval_source="unit_test_owner",
                )

        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)
        backups = list(self.root.glob("autonomy_state.json.before-test-roadmap-pack-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(
            json.loads(backups[0].read_text(encoding="utf-8")),
            json.loads(before),
        )

    def test_production_readiness_pack_is_valid_and_selects_highest_priority_item(self):
        source_pack_dir = Path(__file__).resolve().parents[1] / "config" / "autonomous-projects"
        state = roadmap_state([])
        state["projects"][0].update(id="assistant", name="Tyler AI Assistant")
        self.seed.write_text(json.dumps(state), encoding="utf-8")
        workflow = autonomy.AutonomousWorkflow(
            replace(self.config, roadmap_pack_dir=source_pack_dir),
            state_path=self.state_path,
            seed_path=self.seed,
        )

        success, preview = workflow.preview_roadmap_pack(
            "assistant-production-readiness-202608"
        )
        self.assertTrue(success)
        self.assertEqual(preview["item_count"], 7)
        self.assertEqual(preview["authorization_levels"], ["observe"])
        success, _ = workflow.queue_roadmap_pack(
            "assistant-production-readiness-202608",
            expected_revision=preview["manifest_revision"],
            approval_source="unit_test_owner",
        )
        self.assertTrue(success)
        self.assertEqual(
            workflow.select_actionable_item()["id"],
            "AUTO-PROD-202608-001",
        )

    def test_idea_promotion_preview_is_read_only_and_builds_explicit_criteria(self):
        workflow = self.workflow([])
        state = workflow.load_state()
        state["idea_backlog"] = [idea_record()]
        workflow.store.save(state)
        before = self.state_path.read_text(encoding="utf-8")

        success, preview = workflow.preview_idea_promotion("idea-1")

        self.assertTrue(success)
        self.assertEqual(preview["idea_id"], "idea-1")
        self.assertEqual(preview["project_id"], "project-a")
        self.assertEqual(preview["roadmap_item_id"], "AUTO-IDEA-1")
        self.assertEqual(preview["status"], "ready")
        self.assertEqual(preview["authorization_level"], "propose")
        self.assertEqual(len(preview["acceptance_criteria"]), 4)
        self.assertIn("Draft three examples", preview["acceptance_criteria"][0])
        self.assertIn("under 30 seconds", preview["acceptance_criteria"][1])
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

    def test_idea_promotion_persists_typed_recent_run_context_requirement(self):
        workflow = self.workflow([])
        state = workflow.load_state()
        state["idea_backlog"] = [idea_record(
            recommended_next_validation_step=(
                "Compare the most recent Telegram-triggered runs."
            ),
        )]
        workflow.store.save(state)

        success, preview = workflow.preview_idea_promotion("idea-1")

        self.assertTrue(success)
        self.assertTrue(preview["requires_recent_run_evidence"])

    def test_idea_promotion_is_atomic_actionable_and_idempotent(self):
        workflow = self.workflow([])
        state = workflow.load_state()
        state["idea_backlog"] = [idea_record()]
        workflow.store.save(state)
        budget_before = workflow.load_state()["budget_tracking"]
        success, preview = workflow.preview_idea_promotion("idea-1")
        self.assertTrue(success)

        promoted, message = workflow.promote_idea(
            "idea-1",
            project_id=preview["project_id"],
            expected_revision=preview["proposal_revision"],
            expected_roadmap_item_id=preview["roadmap_item_id"],
            expected_goal_id=preview["goal_id"],
        )

        self.assertTrue(promoted)
        self.assertIn("No model was invoked", message)
        self.assertIn("no autonomous run was started", message)
        self.assertIn("/autorun dry-run", message)
        persisted = workflow.load_state()
        proposal = persisted["idea_backlog"][0]
        roadmap = persisted["projects"][0]["roadmap_items"]
        self.assertEqual(proposal["status"], "promoted")
        self.assertEqual(proposal["promoted_roadmap_item_id"], "AUTO-IDEA-1")
        self.assertEqual(len(roadmap), 1)
        self.assertEqual(roadmap[0]["status"], "ready")
        self.assertEqual(roadmap[0]["source_idea_id"], "idea-1")
        self.assertEqual(roadmap[0]["authorization_level"], "propose")
        self.assertEqual(persisted["budget_tracking"], budget_before)
        self.assertEqual(autonomy.select_actionable_item(persisted)["id"], "AUTO-IDEA-1")

        again, again_message = workflow.promote_idea(
            "idea-1",
            project_id=preview["project_id"],
            expected_revision=preview["proposal_revision"],
            expected_roadmap_item_id=preview["roadmap_item_id"],
            expected_goal_id=preview["goal_id"],
        )
        self.assertTrue(again)
        self.assertIn("already promoted", again_message)
        self.assertIn("no duplicate", again_message)
        self.assertEqual(
            len(workflow.load_state()["projects"][0]["roadmap_items"]), 1
        )

        mismatch, mismatch_message = workflow.promote_idea(
            "idea-1",
            project_id="different-project",
            expected_revision=preview["proposal_revision"],
            expected_roadmap_item_id=preview["roadmap_item_id"],
            expected_goal_id=preview["goal_id"],
        )
        self.assertFalse(mismatch)
        self.assertIn("different reviewed destination", mismatch_message)
        self.assertEqual(
            len(workflow.load_state()["projects"][0]["roadmap_items"]), 1
        )

    def test_idea_promotion_rejections_do_not_mutate_state(self):
        workflow = self.workflow([])
        state = workflow.load_state()
        state["idea_backlog"] = [idea_record(), idea_record()]
        workflow.store.save(state)
        before = self.state_path.read_text(encoding="utf-8")

        success, message = workflow.preview_idea_promotion("idea-1")
        self.assertFalse(success)
        self.assertIn("ambiguous: 2", message)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

        success, message = workflow.preview_idea_promotion("missing")
        self.assertFalse(success)
        self.assertIn("was not found", message)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

        state["idea_backlog"] = [idea_record(recommended_next_validation_step="")]
        workflow.store.save(state)
        before = self.state_path.read_text(encoding="utf-8")
        success, message = workflow.preview_idea_promotion("idea-1")
        self.assertFalse(success)
        self.assertIn("no recommended validation step", message)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

        state["idea_backlog"] = [idea_record(target_goal_id="missing-goal")]
        workflow.store.save(state)
        before = self.state_path.read_text(encoding="utf-8")
        success, message = workflow.preview_idea_promotion("idea-1")
        self.assertFalse(success)
        self.assertIn("Goal 'missing-goal' is missing", message)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

    def test_idea_promotion_requires_unambiguous_active_project(self):
        workflow = self.workflow([])
        state = workflow.load_state()
        state["idea_backlog"] = [idea_record()]
        second = json.loads(json.dumps(state["projects"][0]))
        second["id"] = "project-b"
        second["name"] = "Project B"
        second["roadmap_items"] = []
        state["projects"].append(second)
        workflow.store.save(state)
        before = self.state_path.read_text(encoding="utf-8")

        success, message = workflow.preview_idea_promotion("idea-1")
        self.assertFalse(success)
        self.assertIn("Several active projects", message)
        self.assertIn("project-a", message)
        self.assertIn("project-b", message)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

        success, preview = workflow.preview_idea_promotion("idea-1", "project-b")
        self.assertTrue(success)
        self.assertEqual(preview["project_id"], "project-b")

        state = workflow.load_state()
        for project in state["projects"]:
            project["status"] = "paused"
        workflow.store.save(state)
        success, message = workflow.preview_idea_promotion("idea-1")
        self.assertFalse(success)
        self.assertIn("No active project", message)

    def test_idea_promotion_revalidates_revision_and_respects_run_lock(self):
        workflow = self.workflow([])
        state = workflow.load_state()
        state["idea_backlog"] = [idea_record()]
        workflow.store.save(state)
        success, preview = workflow.preview_idea_promotion("idea-1")
        self.assertTrue(success)

        state = workflow.load_state()
        state["idea_backlog"][0]["expected_value"] = "A materially different outcome."
        workflow.store.save(state)
        before = self.state_path.read_text(encoding="utf-8")
        success, message = workflow.promote_idea(
            "idea-1",
            project_id=preview["project_id"],
            expected_revision=preview["proposal_revision"],
            expected_roadmap_item_id=preview["roadmap_item_id"],
            expected_goal_id=preview["goal_id"],
        )
        self.assertFalse(success)
        self.assertIn("changed after approval", message)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

        state = workflow.load_state()
        state["idea_backlog"][0] = idea_record()
        workflow.store.save(state)
        success, goal_preview = workflow.preview_idea_promotion("idea-1")
        self.assertTrue(success)
        state = workflow.load_state()
        state["projects"][0]["goals"][0]["id"] = "goal-b"
        workflow.store.save(state)
        before = self.state_path.read_text(encoding="utf-8")
        success, message = workflow.promote_idea(
            "idea-1",
            project_id=goal_preview["project_id"],
            expected_revision=goal_preview["proposal_revision"],
            expected_roadmap_item_id=goal_preview["roadmap_item_id"],
            expected_goal_id=goal_preview["goal_id"],
        )
        self.assertFalse(success)
        self.assertIn("reviewed roadmap goal changed", message)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

        state = workflow.load_state()
        state["run_control"]["active_run"] = {"run_id": "active-promotion-run"}
        workflow.store.save(state)
        success, message = workflow.preview_idea_promotion("idea-1")
        self.assertFalse(success)
        self.assertIn("active-promotion-run", message)

        state["run_control"]["active_run"] = None
        workflow.store.save(state)
        held = FileLock(str(workflow.run_lock_path))
        held.acquire()
        try:
            success, message = workflow.preview_idea_promotion("idea-1")
        finally:
            held.release()
        self.assertFalse(success)
        self.assertIn("persistent run lock", message)

    def test_idea_promotion_collision_and_write_failure_leave_state_unchanged(self):
        workflow = self.workflow([item("AUTO-IDEA-1")])
        state = workflow.load_state()
        state["idea_backlog"] = [idea_record()]
        workflow.store.save(state)
        before = self.state_path.read_text(encoding="utf-8")

        success, message = workflow.preview_idea_promotion("idea-1")
        self.assertFalse(success)
        self.assertIn("already exists", message)
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

        state = workflow.load_state()
        state["projects"][0]["roadmap_items"] = []
        workflow.store.save(state)
        success, preview = workflow.preview_idea_promotion("idea-1")
        self.assertTrue(success)
        before = self.state_path.read_text(encoding="utf-8")
        with patch.object(
            autonomy, "_atomic_write_json", side_effect=OSError("simulated write failure")
        ):
            with self.assertRaises(OSError):
                workflow.promote_idea(
                    "idea-1",
                    project_id=preview["project_id"],
                    expected_revision=preview["proposal_revision"],
                    expected_roadmap_item_id=preview["roadmap_item_id"],
                    expected_goal_id=preview["goal_id"],
                )
        self.assertEqual(self.state_path.read_text(encoding="utf-8"), before)

    def test_idea_plan_and_summary_expose_every_stable_id_before_verbose_details(self):
        proposals = [
            idea_record("idea-one", idea="A" * 5000),
            idea_record("idea-two", idea="Second idea"),
            idea_record("idea-three", idea="Third idea"),
        ]
        report = {
            "idea_proposals": proposals,
            "tasks_selected": [],
            "final_status": "ideas_proposed",
            "budget": {"daily_budget_usd": 5, "remaining_usd": 5},
        }

        plan = autonomy.format_telegram_idea_plan(report)
        summary = autonomy.format_telegram_summary(report)

        self.assertLessEqual(len(plan), autonomy.TELEGRAM_MESSAGE_LIMIT)
        for idea_id in ("idea-one", "idea-two", "idea-three"):
            self.assertIn(idea_id, plan)
            self.assertIn(idea_id, summary)
        self.assertIn("/autorun promote <idea-id>", plan)

    def test_selects_highest_priority_actionable_item_and_skips_blockers(self):
        items = [
            item("blocked-high", priority=100, blockers=["Needs credentials"]),
            item("human-high", priority=90, human_decision_required=True),
            item("actionable", priority=80),
            item("lower", priority=10),
        ]
        selected = autonomy.select_actionable_item(roadmap_state(items))
        self.assertEqual(selected["id"], "actionable")

    def test_unmet_dependency_is_skipped_but_unrelated_work_continues(self):
        items = [
            item("waiting", priority=100, dependencies=["foundation"]),
            item("foundation", priority=90, status="blocked", blockers=["Human decision"]),
            item("unrelated", priority=50),
        ]
        selected = autonomy.select_actionable_item(roadmap_state(items))
        self.assertEqual(selected["id"], "unrelated")

    def test_completed_dependency_unlocks_item(self):
        items = [
            item("foundation", priority=5, status="completed"),
            item("dependent", priority=100, dependencies=["foundation"]),
            item("other", priority=50),
        ]
        self.assertEqual(autonomy.select_actionable_item(roadmap_state(items))["id"], "dependent")

    def test_dry_run_invokes_no_execution_or_creative_callback(self):
        executor = Mock()
        ideas = Mock()
        workflow = self.workflow([item()], executor=executor, idea_generator=ideas)
        report = workflow.run(dry_run=True)
        self.assertEqual(report["final_status"], "dry_run")
        executor.assert_not_called()
        ideas.assert_not_called()
        self.assertEqual(report["actual_cost_usd"], 0.0)
        self.assertEqual(report["result_text"], "")
        self.assertEqual(report["artifacts"], [])
        self.assertEqual(report["files_changed"], [])
        self.assertEqual(autonomy.format_telegram_deliverable(report), "")
        self.assertEqual(workflow.load_state()["projects"][0]["roadmap_items"][0]["status"], "ready")

    def test_telegram_summary_includes_at_a_glance_after_heading(self):
        workflow = self.workflow([item()])

        report = workflow.run(trigger_source="telegram", dry_run=True)

        self.assertEqual(
            report["telegram_summary"].splitlines()[:2],
            [
                "Autonomous run: dry_run",
                "trigger=telegram | final=dry_run | human_review=no",
            ],
        )

    def test_telegram_summary_flags_only_owner_attention_as_human_review(self):
        base_report = {
            "trigger_source": "scheduled",
            "status": "completed",
            "final_status": "completed",
            "tasks_selected": [],
            "human_actions": [],
            "escalations": [],
            "blockers": [],
        }
        action_summary = autonomy.format_telegram_summary({
            **base_report,
            "human_actions": ["Approve the production change."],
        })
        escalation_summary = autonomy.format_telegram_summary({
            **base_report,
            "escalations": ["OWNER ACTION NEEDED"],
        })
        terminal_summary = autonomy.format_telegram_summary({
            **base_report,
            "status": "blocked",
            "final_status": "needs_human",
        })
        informational_blocker = autonomy.format_telegram_summary({
            **base_report,
            "blockers": ["Creative generation was unavailable."],
        })

        expected = "trigger=scheduled | final=completed | human_review=yes"
        self.assertEqual(action_summary.splitlines()[1], expected)
        self.assertEqual(escalation_summary.splitlines()[1], expected)
        self.assertEqual(
            terminal_summary.splitlines()[1],
            "trigger=scheduled | final=needs_human | human_review=yes",
        )
        self.assertEqual(
            informational_blocker.splitlines()[1],
            "trigger=scheduled | final=completed | human_review=no",
        )

    def test_dry_run_at_a_glance_surfaces_discovered_owner_action(self):
        workflow = self.workflow([item(acceptance_criteria=[])])

        report = workflow.run(trigger_source="manual", dry_run=True)

        self.assertEqual(report["final_status"], "dry_run")
        self.assertTrue(report["human_actions"])
        self.assertEqual(
            report["telegram_summary"].splitlines()[1],
            "trigger=manual | final=dry_run | human_review=yes",
        )

    def test_execution_receives_bounded_redacted_recent_run_evidence(self):
        captured = {}

        def execute(_project, selected, _decision, _run_id):
            captured["item"] = selected
            return {"status": "completed", "actual_cost_usd": 0.01}

        workflow = self.workflow(
            [item(acceptance_criteria=["Compare the last five runs."])],
            executor=execute,
        )
        workflow.report_dir.mkdir(parents=True, exist_ok=True)
        state = workflow.load_state()
        recent = []
        for index in range(6):
            run_id = f"run_2026073{index}T120000_evidence{index}"
            recent.append({
                "run_id": run_id,
                "started_at": f"2026-07-3{index}T12:00:00+00:00",
                "finished_at": f"2026-07-3{index}T12:01:00+00:00",
                "trigger_source": "telegram" if index % 2 else "scheduled",
                "final_status": "needs_human" if index in {4, 5} else "completed",
            })
            report = {
                "run_id": run_id,
                "start_time": f"2026-07-3{index}T12:00:00+00:00",
                "finish_time": f"2026-07-3{index}T12:01:00+00:00",
                "trigger_source": "telegram" if index % 2 else "scheduled",
                "status": "blocked" if index in {4, 5} else "completed",
                "final_status": "needs_human" if index in {4, 5} else "completed",
                "stop_reason": (
                    "needs_human" if index in {4, 5} else "no_actionable_work"
                ),
                "daily_plan": [
                    "OPENAI_API_KEY=do-not-leak" if index == 5 else f"Plan {index}"
                ],
                "tasks_selected": [
                    {
                        "project_id": "project-b",
                        "id": f"private-task-{index}",
                        "title": "Private Project B roadmap title",
                        "status": "needs_human" if index == 4 else "completed",
                        "failure_classification": (
                            "missing_access" if index == 4 else ""
                        ),
                    },
                    {
                        "project_id": "project-a",
                        "id": f"task-{index}",
                        "title": (
                            "OPENAI_API_KEY=do-not-leak"
                            if index == 5
                            else f"Task {index}"
                        ),
                        "status": "needs_human" if index == 5 else "completed",
                        "failure_classification": "missing_access" if index == 5 else "",
                    },
                ],
                "blockers": ["blocked"] if index in {4, 5} else [],
                "human_actions": ["owner action"] if index in {4, 5} else [],
                "escalations": ["escalated"] if index in {4, 5} else [],
                "deferred": [],
                "files_changed": [],
                "private_result": "must never be copied",
            }
            if index == 1:
                report["run_id"] = "run_mismatched_identity"
            report_path = workflow.report_dir / f"{run_id}.json"
            if index == 2:
                report_path.write_text("{ malformed", encoding="utf-8")
            elif index == 3:
                report["padding"] = "x" * autonomy.RECENT_RUN_REPORT_MAX_BYTES
                report_path.write_text(json.dumps(report), encoding="utf-8")
            else:
                report_path.write_text(json.dumps(report), encoding="utf-8")
        # A malformed/path-like identifier is ignored rather than used as a filename.
        recent.append({"run_id": "run_../../outside", "final_status": "completed"})
        state["run_control"]["recent_runs"] = recent
        workflow.store.save(state)

        report = workflow.run(dry_run=False)

        evidence = captured["item"]["recent_run_evidence"]
        self.assertLessEqual(len(evidence), autonomy.RECENT_RUN_EVIDENCE_LIMIT)
        self.assertEqual([entry["run_id"] for entry in evidence], [
            "run_20260731T120000_evidence1",
            "run_20260732T120000_evidence2",
            "run_20260733T120000_evidence3",
            "run_20260734T120000_evidence4",
            "run_20260735T120000_evidence5",
        ])
        self.assertFalse(evidence[0]["report_available"])
        self.assertFalse(evidence[1]["report_available"])
        self.assertFalse(evidence[2]["report_available"])
        self.assertTrue(evidence[-1]["global_human_review_required"])
        self.assertTrue(evidence[-1]["project_human_review_required"])
        self.assertTrue(evidence[3]["global_human_review_required"])
        self.assertFalse(evidence[3]["project_human_review_required"])
        self.assertIn("[REDACTED]", evidence[-1]["project_plans"][0])
        self.assertNotIn("private_result", json.dumps(evidence))
        self.assertNotIn("Private Project B", json.dumps(evidence))
        self.assertNotIn("do-not-leak", json.dumps(evidence))
        self.assertEqual(
            report["tasks_selected"][0]["context_run_ids"],
            [entry["run_id"] for entry in evidence],
        )
        persisted_item = workflow.load_state()["projects"][0]["roadmap_items"][0]
        self.assertNotIn("recent_run_evidence", persisted_item)

        state_with_active_duplicate = workflow.load_state()
        state_with_active_duplicate["run_control"]["active_run"] = {
            "run_id": "run_20260735T120000_evidence5"
        }
        active_filtered = workflow._recent_run_evidence(
            state_with_active_duplicate,
            "project-a",
        )
        self.assertNotIn(
            "run_20260735T120000_evidence5",
            [entry["run_id"] for entry in active_filtered],
        )

    def test_unrelated_task_does_not_receive_recent_run_context(self):
        captured = {}

        def execute(_project, selected, _decision, _run_id):
            captured["item"] = selected
            return {"status": "completed", "actual_cost_usd": 0.01}

        workflow = self.workflow([item()], executor=execute)
        workflow.run(dry_run=False)

        self.assertNotIn("recent_run_evidence", captured["item"])

    def test_recent_run_context_detection_supports_qualified_phrases_and_opt_out(self):
        self.assertTrue(autonomy._requires_recent_run_evidence({
            "acceptance_criteria": ["Inspect the last 5 Telegram-triggered runs."],
        }))
        self.assertTrue(autonomy._requires_recent_run_evidence({
            "description": "Compare the most recent Telegram runs.",
        }))
        self.assertFalse(autonomy._requires_recent_run_evidence({
            "requires_recent_run_evidence": "false",
            "acceptance_criteria": ["Inspect the last five runs."],
        }))

    def test_missing_information_is_terminal_without_an_execution_retry(self):
        workflow = self.workflow(
            [item()],
            executor=lambda *_args: {
                "status": "failed",
                "failure_classification": "missing_information",
                "reason": "The owner must provide the required decision.",
                "human_action": "Provide the required decision.",
                "actual_cost_usd": 0.01,
            },
        )

        report = workflow.run(dry_run=False)

        self.assertEqual(report["final_status"], "needs_human")
        self.assertEqual(report["tasks_selected"][0]["status"], "needs_human")
        self.assertEqual(
            report["tasks_selected"][0]["failure_classification"],
            "missing_information",
        )

    def test_creative_agent_runs_only_when_no_actionable_work_exists(self):
        ideas = Mock(
            return_value=[
                {
                    "idea": "Add a deployment health digest",
                    "problem_addressed": "Silent deployment failures",
                    "expected_value": "Faster owner response",
                    "target_user": "Owner",
                    "estimated_effort": "small",
                    "estimated_ai_cost_usd": 0.03,
                    "risks": ["Alert fatigue"],
                    "relationship_to_current_goals": "Improves reliability",
                    "recommended_next_validation_step": "Review three recent incidents",
                }
            ]
        )
        workflow = self.workflow([item(status="completed")], idea_generator=ideas)
        report = workflow.run(dry_run=False)
        ideas.assert_called_once()
        self.assertEqual(report["final_status"], "ideas_proposed")
        state = workflow.load_state()
        self.assertEqual(len(state["idea_backlog"]), 1)
        self.assertEqual(state["idea_backlog"][0]["status"], "proposed")
        self.assertEqual(state["projects"][0]["roadmap_items"][0]["status"], "completed")

    def test_nonfatal_idle_ideation_failure_does_not_request_owner_action(self):
        def fail_ideas(_state, _limit):
            raise RuntimeError("creative provider unavailable")

        workflow = self.workflow(
            [item(status="completed")],
            idea_generator=fail_ideas,
        )

        report = workflow.run_session(dry_run=False)

        self.assertEqual(report["final_status"], "idle")
        self.assertEqual(report["human_actions"], [])
        self.assertTrue(report["errors"])
        self.assertIn("no roadmap work was affected", report["blockers"][0].lower())

    def test_completed_roadmap_work_remains_complete_if_idle_ideation_fails(self):
        def fail_ideas(_state, _limit):
            raise RuntimeError("creative provider unavailable")

        workflow = self.workflow(
            [item()],
            executor=lambda *_args: {
                "status": "completed",
                "actual_cost_usd": 0.01,
            },
            idea_generator=fail_ideas,
        )

        report = workflow.run_session(dry_run=False)

        self.assertEqual(report["final_status"], "completed")
        self.assertEqual(report["tasks_selected"][0]["status"], "completed")
        self.assertEqual(report["human_actions"], [])
        self.assertTrue(report["errors"])

    def test_creative_ideas_are_deduplicated_and_never_auto_built(self):
        executor = Mock()
        proposal = {"idea": "Add a deployment health digest"}
        ideas = Mock(return_value=[proposal, proposal])
        workflow = self.workflow([item(status="completed")], executor=executor, idea_generator=ideas)
        first = workflow.run(dry_run=False)
        second = workflow.run(dry_run=False)
        self.assertEqual(len(first["ideas_added"]), 1)
        self.assertEqual(second["ideas_added"], [])
        self.assertEqual(len(workflow.load_state()["idea_backlog"]), 1)
        executor.assert_not_called()

    def test_metered_creative_result_is_attributed_and_refreshes_shared_budget(self):
        before = {
            "daily_budget_usd": 1.0,
            "emergency_reserve_usd": 0.25,
            "spent_today_usd": 0.10,
            "reserved_today_usd": 0.0,
            "remaining_usd": 0.65,
        }
        after = {
            **before,
            "spent_today_usd": 0.14,
            "remaining_usd": 0.61,
            "cost_is_estimated": False,
        }
        provider = Mock(side_effect=[before, after])
        ideas = Mock(return_value={
            "ideas": [{"idea": "Add a deployment health digest"}],
            "model": "creative-model",
            "model_reason": "Standard ideation benefits from a balanced model.",
            "estimated_cost_usd": 0.05,
            "actual_cost_usd": 0.04,
            "cost_is_estimated": False,
            "token_usage": {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
            "agent": "creative",
            "project_id": "idea_backlog",
            "task_id": "controlled-idle-ideation",
        })
        workflow = self.workflow(
            [item(status="completed")],
            idea_generator=ideas,
            budget_provider=provider,
        )

        report = workflow.run(dry_run=False)

        self.assertEqual(report["final_status"], "ideas_proposed")
        self.assertEqual(report["actual_cost_usd"], 0.04)
        self.assertFalse(report["cost_is_estimated"])
        self.assertEqual(report["token_usage"]["total_tokens"], 100)
        self.assertEqual(report["agents_involved"], ["creative"])
        self.assertEqual(report["models_selected"], ["creative-model"])
        self.assertEqual(report["costs"]["by_agent"]["creative"], 0.04)
        self.assertEqual(report["costs"]["by_model"]["creative-model"], 0.04)
        self.assertEqual(report["budget"]["spent_after_usd"], 0.14)
        self.assertEqual(report["budget"]["remaining_usd"], 0.61)
        self.assertEqual(provider.call_count, 2)

    def test_creative_callback_is_skipped_when_no_ordinary_budget_remains(self):
        provider = Mock(return_value={
            "daily_budget_usd": 1.0,
            "emergency_reserve_usd": 0.25,
            "spent_today_usd": 0.75,
            "reserved_today_usd": 0.0,
            "remaining_usd": 0.0,
        })
        ideas = Mock(return_value=[{"idea": "Should not be generated"}])
        workflow = self.workflow(
            [item(status="completed")],
            idea_generator=ideas,
            budget_provider=provider,
        )

        report = workflow.run(dry_run=False)

        self.assertEqual(report["final_status"], "budget_deferred")
        ideas.assert_not_called()
        self.assertEqual(workflow.load_state()["idea_backlog"], [])

    def test_actionable_work_prevents_creative_callback(self):
        executor = Mock(return_value={"status": "completed", "actual_cost_usd": 0.01})
        ideas = Mock(return_value=[{"idea": "Speculative idea"}])
        workflow = self.workflow([item()], executor=executor, idea_generator=ideas)
        report = workflow.run(dry_run=False)
        self.assertEqual(report["final_status"], "completed")
        executor.assert_called_once()
        ideas.assert_not_called()

    def test_completed_result_is_persisted_redacted_and_telegram_safe(self):
        result_text = (
            "Production checklist begins. OPENAI_API_KEY=top-secret. "
            + "Verify overlap, idempotency, redaction, and budget reporting. " * 120
        )
        executor = Mock(return_value={
            "status": "completed",
            "result_text": result_text,
            "result_task_id": "worker-1",
            "result_agent": "general",
            "review_outcomes": ["APPROVED: every criterion is satisfied."],
            "actual_cost_usd": 0.01,
            "artifacts": ["file: files/config-check.md"],
            "files_changed": ["files/config-check.md"],
        })
        workflow = self.workflow([item()], executor=executor)

        with patch.dict(os.environ, {"MAX_TASK_RESULT_CHARS": "5000"}):
            report = workflow.run(dry_run=False)

        self.assertEqual(report["final_status"], "completed")
        self.assertEqual(report["result_task_id"], "worker-1")
        self.assertEqual(report["result_agent"], "general")
        self.assertTrue(report["result_truncated"])
        self.assertLessEqual(len(report["result_text"]), 5000)
        self.assertIn("Production checklist begins", report["result_text"])
        self.assertIn("[REDACTED]", report["result_text"])
        self.assertNotIn("top-secret", json.dumps(report))
        self.assertEqual(
            report["tasks_selected"][0]["result_summary"],
            report["result_text"][:1000],
        )
        self.assertIn("Result:", report["telegram_summary"])
        self.assertIn("[preview truncated]", report["telegram_summary"])
        self.assertIn("Budget:", report["telegram_summary"])
        self.assertIn("Your action:", report["telegram_summary"])
        self.assertEqual(
            report["telegram_summary"].splitlines()[1],
            "trigger=manual | final=completed | human_review=no",
        )
        self.assertLessEqual(len(report["telegram_summary"]), autonomy.TELEGRAM_MESSAGE_LIMIT)
        deliverable = autonomy.format_telegram_deliverable(report)
        self.assertIn("Autonomous deliverable", deliverable)
        self.assertIn("Agent: general", deliverable)
        self.assertIn("configured storage limit", deliverable)
        persisted = json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))
        self.assertEqual(persisted["result_text"], report["result_text"])
        self.assertNotIn("top-secret", json.dumps(persisted))

    def test_shared_budget_provider_and_nested_execution_attribution_drive_report(self):
        before = {
            "daily_budget_usd": 2.0,
            "emergency_reserve_usd": 0.25,
            "spent_today_usd": 0.50,
            "reserved_today_usd": 0.85,
            "remaining_usd": 0.40,
        }
        after = {**before, "spent_today_usd": 0.60, "reserved_today_usd": 0.0, "remaining_usd": 1.15}
        provider = Mock(side_effect=[before, after])
        decision = SimpleNamespace(
            model_id="worker-model",
            estimated_cost_usd=0.02,
            reason="Selected from shared remaining budget.",
            deferred=False,
            deferral_reason="",
        )
        router = SimpleNamespace(route=Mock(return_value=decision))
        executor = Mock(return_value={
            "status": "completed",
            "actual_cost_usd": 0.10,
            "model": "worker-model",
            "models": ["worker-model", "review-model"],
            "agents": ["manager", "editor"],
            "costs": {
                "by_project": {"company-project": 0.10},
                "by_task": {"worker": 0.04, "review": 0.06},
                "by_agent": {"manager": 0.04, "editor": 0.06},
                "by_model": {"worker-model": 0.04, "review-model": 0.06},
            },
        })
        workflow = self.workflow(
            [item()], executor=executor, router=router, budget_provider=provider
        )
        report = workflow.run(dry_run=False)

        routed_request = router.route.call_args.args[0]
        self.assertEqual(routed_request.remaining_budget_usd, 0.40)
        self.assertEqual(report["budget"]["daily_budget_usd"], 2.0)
        self.assertEqual(report["budget"]["spent_before_usd"], 0.50)
        self.assertEqual(report["budget"]["spent_after_usd"], 0.60)
        self.assertEqual(report["budget"]["remaining_usd"], 1.15)
        self.assertEqual(report["models_selected"], ["worker-model", "review-model"])
        self.assertEqual(report["agents_involved"], ["manager", "editor"])
        self.assertEqual(report["costs"]["by_model"]["review-model"], 0.06)
        self.assertEqual(provider.call_count, 2)

    def test_shared_budget_provider_failure_starts_no_work_and_releases_run_claim(self):
        executor = Mock()
        provider = Mock(side_effect=RuntimeError("company ledger unavailable"))
        workflow = self.workflow([item()], executor=executor, budget_provider=provider)

        report = workflow.run(dry_run=False)

        self.assertEqual(report["final_status"], "needs_human")
        self.assertIn("no work was started", report["blockers"][0].lower())
        executor.assert_not_called()
        self.assertIsNone(workflow.load_state()["run_control"]["active_run"])

    def test_real_router_selects_lightweight_model_for_lightweight_item(self):
        try:
            import model_router
        except ImportError:
            self.skipTest("model_router is being implemented in parallel")
        workflow = self.workflow([item()])
        decision = workflow._route(item(), remaining_budget=4.75)
        self.assertIsInstance(decision, model_router.RoutingDecision)
        self.assertFalse(decision.deferred)
        self.assertEqual(decision.model_id, "gpt-5.4-nano")

    def test_access_and_decision_failures_do_not_promote_model_strength(self):
        workflow = self.workflow([item()])
        blocked = item(previous_attempts=[{
            "status": "blocked",
            "failure_classification": "missing_access",
            "model_invoked": True,
        }], previous_models=["gpt-5.4-nano"])
        technical = item(previous_attempts=[{
            "status": "failed",
            "failure_classification": "technical",
            "model_invoked": True,
        }], previous_models=["gpt-5.4-nano"])

        self.assertEqual(workflow._routing_request(blocked, 1.0).previous_failures, 0)
        self.assertEqual(workflow._routing_request(technical, 1.0).previous_failures, 1)

    def test_report_contains_required_fields_and_actionable_summary(self):
        router = SimpleNamespace(
            route=Mock(
                return_value=SimpleNamespace(
                    model_id="test-light",
                    estimated_cost_usd=0.02,
                    reason="Lightweight status task fits the least-cost capable model.",
                    deferred=False,
                    deferral_reason="",
                )
            )
        )
        executor = Mock(
            return_value={
                "status": "blocked",
                "error": "Permission denied while opening the repository",
                "human_action": "Grant read access to the repository.",
                "actual_cost_usd": 0.01,
                "token_usage": {"input_tokens": 100, "output_tokens": 20},
                "tests_executed": ["python -m unittest"],
            }
        )
        workflow = self.workflow([item()], executor=executor, router=router)
        report = workflow.run(dry_run=False)
        required = {
            "run_id",
            "start_time",
            "finish_time",
            "trigger_source",
            "daily_plan",
            "tasks_selected",
            "agents_involved",
            "models_selected",
            "model_selection_reasons",
            "token_usage",
            "estimated_cost_usd",
            "actual_cost_usd",
            "review_outcomes",
            "retry_count",
            "blockers",
            "escalations",
            "files_changed",
            "tests_executed",
            "final_status",
        }
        self.assertTrue(required.issubset(report))
        self.assertEqual(report["final_status"], "needs_human")
        self.assertIn("Grant read access", report["telegram_summary"])
        self.assertIn("Budget:", report["telegram_summary"])
        persisted = json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))
        self.assertEqual(persisted["run_id"], report["run_id"])

    def test_secret_redaction_covers_keys_and_embedded_values(self):
        redacted = autonomy.redact_secrets(
            {
                "api_key": "top-secret",
                "GITHUB_TOKEN": "ghp_abcdefghijklmnopqrstuvwxyz123456",
                "token_usage": {"input_tokens": 5},
                "message": (
                    "OPENAI_API_KEY=top-secret sk-abcdefghijk and Bearer xyz123 "
                    "plus ghp_abcdefghijklmnopqrstuvwxyz123456"
                ),
            }
        )
        serialized = json.dumps(redacted)
        self.assertNotIn("top-secret", serialized)
        self.assertNotIn("sk-abcdefghijk", serialized)
        self.assertNotIn("xyz123", serialized)
        self.assertNotIn("ghp_", serialized)
        self.assertEqual(redacted["token_usage"]["input_tokens"], 5)
        self.assertIn("[REDACTED]", serialized)

    def test_scheduled_date_is_idempotent(self):
        executor = Mock()
        workflow = self.workflow([item()], executor=executor)
        first = workflow.run(trigger_source="scheduled", dry_run=True, scheduled_date="2026-07-27")
        second = workflow.run(trigger_source="scheduled", dry_run=True, scheduled_date="2026-07-27")
        self.assertEqual(first["final_status"], "dry_run")
        self.assertEqual(second["final_status"], "idempotent_skip")
        executor.assert_not_called()

    def test_stale_running_state_is_recovered(self):
        now = datetime(2026, 7, 27, 15, 0, tzinfo=timezone.utc)
        workflow = self.workflow([item(status="in_progress")], now_provider=lambda: now)
        state = workflow.load_state()
        state["run_control"]["active_run"] = {
            "run_id": "old-run",
            "started_at": (now - timedelta(hours=5)).isoformat(),
            "item_id": "task-1",
        }
        workflow.store.save(state)
        report = workflow.run(dry_run=True)
        self.assertEqual(report["final_status"], "dry_run")
        self.assertEqual(report["stale_recoveries"][0]["run_id"], "old-run")
        attempts = workflow.load_state()["projects"][0]["roadmap_items"][0]["previous_attempts"]
        self.assertEqual(attempts[0]["failure_classification"], "stale_running_recovery")

    def test_corrupt_state_is_quarantined_and_run_stops(self):
        self.write_seed([item()])
        self.state_path.write_text("{not valid json", encoding="utf-8")
        workflow = autonomy.AutonomousWorkflow(self.config, state_path=self.state_path, seed_path=self.seed)
        first = workflow.run(dry_run=True)
        second = workflow.run(dry_run=True)
        self.assertEqual(first["final_status"], "needs_human")
        self.assertEqual(second["final_status"], "needs_human")
        self.assertEqual(second["tasks_selected"], [])
        self.assertFalse(self.state_path.exists())
        self.assertEqual(len(list(self.root.glob("autonomy_state.json.corrupt-*"))), 1)
        self.assertTrue((self.root / "autonomy_state.json.recovery-required").exists())

    def test_valid_json_with_wrong_nested_shape_is_quarantined(self):
        self.write_seed([item()])
        self.state_path.write_text(
            json.dumps({**roadmap_state([item()]), "run_control": []}),
            encoding="utf-8",
        )
        workflow = autonomy.AutonomousWorkflow(
            self.config, state_path=self.state_path, seed_path=self.seed
        )

        report = workflow.run(dry_run=True)

        self.assertEqual(report["final_status"], "needs_human")
        self.assertFalse(self.state_path.exists())
        self.assertEqual(len(list(self.root.glob("autonomy_state.json.corrupt-*"))), 1)

    def test_non_budget_router_deferral_becomes_actionable_human_blocker(self):
        decision = SimpleNamespace(
            model_id=None,
            estimated_cost_usd=0.0,
            reason="No configured model supports quantum_hardware_control.",
            deferred=True,
            deferral_reason="missing_capability",
        )
        router = SimpleNamespace(route=Mock(return_value=decision))
        executor = Mock()
        workflow = self.workflow([item(required_capabilities=["quantum_hardware_control"])], router=router, executor=executor)

        report = workflow.run(dry_run=False)

        self.assertEqual(report["final_status"], "needs_human")
        self.assertIn("missing_capability", report["blockers"])
        self.assertTrue(report["escalations"])
        executor.assert_not_called()

    def test_no_model_preflight_deferral_does_not_poison_model_history(self):
        decision = SimpleNamespace(
            model_id="worker-model",
            estimated_cost_usd=0.01,
            reason="Selected capable model.",
            deferred=False,
            deferral_reason="",
        )
        router = SimpleNamespace(route=Mock(return_value=decision))
        executor = Mock(return_value={
            "status": "deferred",
            "failure_classification": "decision_required",
            "reason": "A supervised Company Mode plan is already running.",
            "attempted": "Checked the persisted Company Mode ledger before task execution.",
            "human_action": "Run /company, resolve the open project, then retry /autorun live.",
            "other_work_can_continue": True,
            "actual_cost_usd": 0.0,
            "model_invoked": False,
        })
        workflow = self.workflow([item()], router=router, executor=executor)

        report = workflow.run(dry_run=False)

        self.assertEqual(report["final_status"], "deferred")
        persisted_item = workflow.load_state()["projects"][0]["roadmap_items"][0]
        self.assertEqual(persisted_item["status"], "deferred")
        self.assertFalse(persisted_item["human_decision_required"])
        self.assertEqual(persisted_item["previous_models"], [])
        self.assertFalse(persisted_item["previous_attempts"][0]["model_invoked"])
        self.assertEqual(report["actual_cost_usd"], 0.0)
        self.assertIn("/company", report["human_actions"][0])
        self.assertIn("/autorun live", report["telegram_summary"])
        self.assertNotIn("Your action: None", report["telegram_summary"])
        self.assertEqual(len(report["escalations"]), 1)
        self.assertIn("OWNER ACTION NEEDED", report["escalations"][0])
        self.assertIn("decision_required", report["escalations"][0])
        self.assertIn("Checked the persisted Company Mode ledger", report["escalations"][0])

    def test_authorization_above_ceiling_escalates_without_execution(self):
        executor = Mock()
        workflow = self.workflow(
            [item(authorization_level=autonomy.AuthorizationLevel.MODIFY_LOCAL.value)],
            executor=executor,
        )
        report = workflow.run(dry_run=False)
        self.assertEqual(report["final_status"], "needs_human")
        self.assertIn("Approve this task", report["telegram_summary"])
        executor.assert_not_called()

    def test_missing_acceptance_criteria_escalates_before_routing_or_execution(self):
        router = SimpleNamespace(route=Mock())
        executor = Mock()
        workflow = self.workflow(
            [item(acceptance_criteria=[])], router=router, executor=executor
        )

        report = workflow.run(dry_run=False)

        self.assertEqual(report["final_status"], "needs_human")
        self.assertIn("missing_acceptance_criteria", report["blockers"])
        self.assertIn("Add at least one", report["telegram_summary"])
        router.route.assert_not_called()
        executor.assert_not_called()

    def test_technical_failures_stop_at_the_configured_cross_run_attempt_cap(self):
        self.config = replace(self.config, max_execution_attempts=2)
        executor = Mock(return_value={
            "status": "failed",
            "reason": "Temporary provider failure.",
            "failure_classification": "technical",
            "actual_cost_usd": 0.0,
            "model_invoked": True,
        })
        workflow = self.workflow([item()], executor=executor)

        first = workflow.run(dry_run=False)
        second = workflow.run(dry_run=False)
        third = workflow.run(dry_run=False)

        self.assertEqual(first["tasks_selected"][0]["status"], "ready")
        self.assertEqual(second["final_status"], "needs_human")
        self.assertIn("Review the failure", second["telegram_summary"])
        self.assertIn(third["final_status"], {"idle", "ideas_proposed"})
        self.assertEqual(executor.call_count, 2)
        persisted = workflow.load_state()["projects"][0]["roadmap_items"][0]
        self.assertEqual(persisted["status"], "needs_human")
        self.assertEqual(len(persisted["previous_attempts"]), 2)

    def test_missing_access_reaches_needs_human_and_the_owner_summary(self):
        executor = Mock(return_value={
            "status": "needs_human",
            "reason": "The repository cannot be read without access.",
            "failure_classification": "missing_access",
            "human_action": "Grant read access to the repository, then retry.",
            "attempted": "Tried the configured read-only repository tool.",
            "actual_cost_usd": 0.0,
            "model_invoked": True,
        })
        workflow = self.workflow([item()], executor=executor)

        report = workflow.run(dry_run=False)

        self.assertEqual(report["final_status"], "needs_human")
        self.assertEqual(report["tasks_selected"][0]["status"], "needs_human")
        self.assertIn("missing_access", report["escalations"][0])
        self.assertIn("Grant read access", report["telegram_summary"])
        persisted = workflow.load_state()["projects"][0]["roadmap_items"][0]
        self.assertTrue(persisted["human_decision_required"])
        self.assertEqual(
            persisted["human_action"],
            "Grant read access to the repository, then retry.",
        )

    def test_session_executes_priority_order_and_aggregates_totals(self):
        self.config = replace(self.config, max_tasks_per_run=10)
        calls = []

        def execute(_project, selected, _decision, _run_id):
            calls.append(selected["id"])
            return {
                "status": "completed",
                "actual_cost_usd": 0.10,
                "model": "worker-model",
                "token_usage": {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
                "result_text": f"Completed {selected['id']}",
            }

        decision = SimpleNamespace(
            model_id="worker-model", estimated_cost_usd=0.05,
            reason="Lowest capable model.", deferred=False, deferral_reason="",
        )
        workflow = self.workflow(
            [item("low", priority=10), item("high", priority=30), item("middle", priority=20)],
            executor=execute,
            router=SimpleNamespace(route=Mock(return_value=decision)),
        )

        report = workflow.run_session(dry_run=False)

        self.assertEqual(calls, ["high", "middle", "low"])
        self.assertEqual(report["final_status"], "completed")
        self.assertEqual(report["stop_reason"], "no_actionable_work")
        self.assertEqual(len(report["cycle_reports"]), 4)
        self.assertAlmostEqual(report["actual_cost_usd"], 0.30)
        self.assertEqual(report["token_usage"]["total_tokens"], 300)
        self.assertEqual(report["costs"]["by_task"], {
            "high": 0.10, "middle": 0.10, "low": 0.10,
        })
        self.assertIn("Task high, Task middle, Task low", report["telegram_summary"])
        persisted = json.loads(Path(report["report_path"]).read_text(encoding="utf-8"))
        self.assertEqual(len(persisted["cycle_reports"]), 4)

    def test_session_holds_lock_and_scheduled_date_is_claimed_once(self):
        observed_lock = []
        workflow = self.workflow([item()])

        def execute(_project, _selected, _decision, _run_id):
            contender = FileLock(str(workflow.run_lock_path), timeout=0)
            try:
                contender.acquire()
            except Exception:
                observed_lock.append(True)
            else:
                contender.release()
                observed_lock.append(False)
            return {"status": "completed", "actual_cost_usd": 0.01}

        workflow.executor = execute
        first = workflow.run_session(
            trigger_source="scheduled", dry_run=False, scheduled_date="2026-07-31"
        )
        second = workflow.run_session(
            trigger_source="scheduled", dry_run=False, scheduled_date="2026-07-31"
        )

        self.assertEqual(observed_lock, [True])
        self.assertEqual(first["final_status"], "completed")
        self.assertEqual(second["final_status"], "idempotent_skip")
        self.assertEqual(second["stop_reason"], "scheduled_date_already_claimed")

    def test_overlapping_session_does_not_touch_persistent_state(self):
        workflow = self.workflow([item()], executor=Mock())
        workflow.base_dir.mkdir(parents=True, exist_ok=True)
        held = FileLock(str(workflow.run_lock_path))
        held.acquire()
        try:
            with patch.object(workflow.store, "load", side_effect=AssertionError("must not read")), \
                    patch.object(workflow.store, "save", side_effect=AssertionError("must not write")):
                report = workflow.run_session(dry_run=False)
        finally:
            held.release()

        self.assertEqual(report["final_status"], "overlap_prevented")
        self.assertEqual(report["stop_reason"], "overlap_prevented")
        workflow.executor.assert_not_called()

    def test_session_continues_after_needs_human_to_unrelated_work(self):
        calls = []
        ideas = Mock(return_value=[{"idea": "Should not run after a blocker"}])

        def execute(_project, selected, _decision, _run_id):
            calls.append(selected["id"])
            if selected["id"] == "blocked-high":
                return {
                    "status": "needs_human",
                    "reason": "Repository access is missing.",
                    "failure_classification": "missing_access",
                    "human_action": "Grant repository access.",
                    "actual_cost_usd": 0.01,
                }
            return {"status": "completed", "actual_cost_usd": 0.02}

        workflow = self.workflow([
            item("blocked-high", priority=20), item("unrelated", priority=10)
        ], executor=execute, idea_generator=ideas)

        report = workflow.run_session(dry_run=False)

        self.assertEqual(calls, ["blocked-high", "unrelated"])
        self.assertEqual(report["final_status"], "needs_human")
        self.assertEqual(report["stop_reason"], "needs_human")
        self.assertEqual([task["status"] for task in report["tasks_selected"]], [
            "needs_human", "completed",
        ])
        self.assertIn("Grant repository access.", report["human_actions"])
        ideas.assert_not_called()
        self.assertEqual(report["ideas_added"], [])

    def test_session_attempts_budget_deferred_item_once_then_continues(self):
        routed_types = []

        def route(request):
            routed_types.append(request.task_type)
            if request.task_type == "architecture":
                return SimpleNamespace(
                    model_id=None, estimated_cost_usd=9.0,
                    reason="Task estimate does not fit.", deferred=True,
                    deferral_reason="insufficient_budget",
                )
            return SimpleNamespace(
                model_id="worker-model", estimated_cost_usd=0.05,
                reason="Fits remaining budget.", deferred=False, deferral_reason="",
            )

        executor = Mock(return_value={"status": "completed", "actual_cost_usd": 0.02})
        workflow = self.workflow([
            item("too-large", priority=20, task_type="architecture"),
            item("small", priority=10, task_type="status_update"),
        ], executor=executor, router=SimpleNamespace(route=route))

        report = workflow.run_session(dry_run=False)

        self.assertEqual(routed_types, ["architecture", "status_update"])
        self.assertEqual([task["id"] for task in report["tasks_selected"]], ["too-large", "small"])
        self.assertEqual(report["tasks_selected"][0]["status"], "deferred")
        self.assertEqual(report["tasks_selected"][1]["status"], "completed")
        executor.assert_called_once()

    def test_session_stops_at_budget_floor_and_preserves_emergency_reserve(self):
        executor = Mock(return_value={"status": "completed", "actual_cost_usd": 4.72})
        decision = SimpleNamespace(
            model_id="worker-model", estimated_cost_usd=0.05,
            reason="Fits ordinary budget.", deferred=False, deferral_reason="",
        )
        workflow = self.workflow(
            [item("first", priority=20), item("second", priority=10)],
            executor=executor,
            router=SimpleNamespace(route=Mock(return_value=decision)),
        )

        report = workflow.run_session(dry_run=False)

        self.assertEqual(report["stop_reason"], "budget_floor")
        self.assertEqual([task["id"] for task in report["tasks_selected"]], ["first"])
        self.assertAlmostEqual(report["budget"]["remaining_usd"], 0.03)
        self.assertEqual(workflow.load_state()["projects"][0]["roadmap_items"][1]["status"], "ready")

    def test_session_dry_run_plans_once_without_execution_or_ideas(self):
        executor = Mock()
        ideas = Mock()
        workflow = self.workflow(
            [item("high", priority=20), item("low", priority=10)],
            executor=executor,
            idea_generator=ideas,
        )

        report = workflow.run_session(dry_run=True)

        self.assertEqual(report["final_status"], "dry_run")
        self.assertEqual(report["stop_reason"], "dry_run_complete")
        self.assertEqual(len(report["cycle_reports"]), 1)
        self.assertEqual([task["id"] for task in report["tasks_selected"]], ["high"])
        executor.assert_not_called()
        ideas.assert_not_called()

    def test_session_runs_creative_once_and_never_executes_proposals(self):
        executor = Mock()
        ideas = Mock(return_value={
            "ideas": [
                {
                    "idea": "Deployment health digest",
                    "problem_addressed": "Silent failures",
                    "expected_value": "Faster recovery",
                    "estimated_effort": "small",
                    "estimated_ai_cost_usd": 0.03,
                    "recommended_next_validation_step": "Review three incidents",
                },
                {"idea": "Roadmap aging report"},
            ],
            "model": "creative-model",
            "model_reason": "A balanced ideation model is sufficient.",
            "estimated_cost_usd": 0.04,
            "actual_cost_usd": 0.03,
            "token_usage": {"input_tokens": 90, "output_tokens": 10, "total_tokens": 100},
            "agent": "creative",
        })
        workflow = self.workflow(
            [item(status="completed")], executor=executor, idea_generator=ideas
        )

        report = workflow.run_session(dry_run=False)

        ideas.assert_called_once()
        executor.assert_not_called()
        self.assertEqual(report["final_status"], "ideas_proposed")
        self.assertEqual(report["stop_reason"], "ideas_proposed")
        self.assertEqual(len(report["idea_proposals"]), 2)
        self.assertIn("Deployment health digest", autonomy.format_telegram_deliverable(report))
        backlog = workflow.load_state()["idea_backlog"]
        self.assertEqual([idea["status"] for idea in backlog], ["proposed", "proposed"])

    def test_creative_report_never_claims_more_than_backlog_capacity(self):
        self.config = replace(self.config, idea_backlog_limit=2, max_ideas_per_run=3)
        workflow = self.workflow(
            [item(status="completed")],
            idea_generator=Mock(return_value=[
                {"idea": "Only available slot"},
                {"idea": "Must not be reported"},
                {"idea": "Also must not be reported"},
            ]),
        )
        state = workflow.load_state()
        state["idea_backlog"] = [{
            "id": "existing", "idea": "Existing idea", "status": "proposed",
            "fingerprint": "existing-fingerprint",
        }]
        workflow.store.save(state)

        report = workflow.run_session(dry_run=False)

        self.assertEqual(len(report["idea_proposals"]), 1)
        self.assertEqual(report["idea_proposals"][0]["idea"], "Only available slot")
        self.assertEqual(len(workflow.load_state()["idea_backlog"]), 2)

    def test_session_stops_before_work_on_a_new_budget_date(self):
        clock = {"now": datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc)}
        calls = []

        def execute(_project, selected, _decision, _run_id):
            calls.append(selected["id"])
            clock["now"] = clock["now"] + timedelta(days=1)
            return {"status": "completed", "actual_cost_usd": 0.01}

        self.write_seed([item("first", priority=20), item("second", priority=10)])
        workflow = autonomy.AutonomousWorkflow(
            self.config,
            state_path=self.state_path,
            seed_path=self.seed,
            executor=execute,
            now_provider=lambda: clock["now"],
        )

        report = workflow.run_session(dry_run=False)

        self.assertEqual(calls, ["first"])
        self.assertEqual(report["stop_reason"], "budget_date_changed")
        self.assertIn("Stop: budget date changed", report["telegram_summary"])
        self.assertEqual(workflow.load_state()["projects"][0]["roadmap_items"][1]["status"], "ready")

    def test_session_honors_task_and_elapsed_time_caps(self):
        self.config = replace(self.config, max_tasks_per_run=2)
        executor = Mock(return_value={"status": "completed", "actual_cost_usd": 0.01})
        workflow = self.workflow([
            item("one", priority=30), item("two", priority=20), item("three", priority=10)
        ], executor=executor)

        report = workflow.run_session(dry_run=False)
        self.assertEqual(report["stop_reason"], "max_tasks_reached")
        self.assertEqual(executor.call_count, 2)

        timed = self.workflow([item("later")], executor=Mock())
        with patch.object(autonomy.time, "monotonic", side_effect=[0.0, 7200.0]):
            timed_report = timed.run_session(dry_run=False)
        self.assertEqual(timed_report["stop_reason"], "max_session_time_reached")
        timed.executor.assert_not_called()


if __name__ == "__main__":
    unittest.main()

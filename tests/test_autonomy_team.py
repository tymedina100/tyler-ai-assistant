import unittest

import autonomy_team
import model_router


class AutonomyTeamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.router = model_router.ModelRouter(model_router.load_model_catalog())

    def worker_decision(self):
        return self.router.route(model_router.RoutingRequest(
            task_type="status_update",
            complexity="lightweight",
            risk="low",
            required_capabilities=("text",),
            estimated_input_tokens=1200,
            estimated_output_tokens=300,
            remaining_budget_usd=5.0,
        ))

    def item(self, **overrides):
        value = {
            "id": "AUTO-1",
            "title": "Validate the schedule",
            "agent_owner": "manager",
            "task_type": "status_update",
            "complexity": "lightweight",
            "risk": "low",
            "required_capabilities": ["text"],
            "authorization_level": "observe",
            "estimated_input_tokens": 1200,
            "estimated_output_tokens": 300,
            "acceptance_criteria": ["Report the configured timezone", "Expose no secrets"],
        }
        value.update(overrides)
        return value

    def test_observe_and_propose_authorization_exclude_mutating_tools(self):
        profile = {
            "read_file", "search_the_web", "write_file", "code_edit_file",
            "send_email", "deploy_site", "linear_create_issue",
        }
        for level in ("observe", "propose"):
            with self.subTest(level=level):
                allowed = autonomy_team.allowed_tool_names(profile, level)
                self.assertEqual(allowed, {"read_file", "search_the_web"})

    def test_modify_local_does_not_relabel_network_or_github_writes_as_local(self):
        profile = {
            "code_read_file", "code_edit_file", "code_propose_change", "run_python",
            "github_save_file", "write_file", "deploy_site", "send_email",
        }
        allowed = autonomy_team.allowed_tool_names(profile, "modify_local")
        self.assertEqual(
            allowed,
            {"code_read_file"},
        )

    def test_read_only_autonomy_excludes_secret_value_tools(self):
        allowed = autonomy_team.allowed_tool_names(
            {"railway_list_vars", "railway_get_var", "railway_deploy_status"},
            "observe",
        )
        self.assertEqual(allowed, {"railway_list_vars", "railway_deploy_status"})

    def test_project_autonomy_uses_scoped_code_repo_not_file_mirror(self):
        allowed = autonomy_team.allowed_tool_names(
            {
                "github_list_files",
                "github_read_file",
                "code_list_files",
                "code_read_file",
            },
            "observe",
        )
        self.assertEqual(allowed, {"code_list_files", "code_read_file"})

    def test_company_plan_adds_required_review_and_preserves_acceptance_criteria(self):
        plan = autonomy_team.build_company_plan(
            self.item(), self.worker_decision(), 5.0, router=self.router
        )
        self.assertFalse(plan["deferred"])
        self.assertEqual([task["owner"] for task in plan["tasks"]], ["general", "editor"])
        self.assertEqual(plan["tasks"][0]["model"], "gpt-5.4-nano")
        self.assertEqual(plan["tasks"][1]["model"], "gpt-5.4-mini")
        self.assertEqual(plan["tasks"][1]["authorization_level"], "observe")
        self.assertEqual(
            plan["estimated_cost_usd"],
            sum(task["estimate_usd"] for task in plan["tasks"]),
        )
        self.assertTrue(all(task["enforce_authorization"] for task in plan["tasks"]))
        self.assertEqual(
            plan["tasks"][1]["acceptance_criteria"],
            ["Report the configured timezone", "Expose no secrets"],
        )

    def test_campaign_plan_is_draft_then_review_with_exact_metadata(self):
        action = {
            "action_type": "publish",
            "target": "bluesky:company.example",
            "policy_revision": "owner-policy-r1",
        }
        plan = autonomy_team.build_company_plan(
            self.item(
                agent_owner="marketing",
                authorization_level="external_action",
                revenue_sprint_id="sprint-1",
                external_action=action,
                campaign_product_url="https://company.example/product",
                campaign_changed_variable="call_to_action",
                campaign_evidence_basis="Day-5 decision=pivot",
            ),
            self.worker_decision(),
            5.0,
            router=self.router,
        )

        self.assertFalse(plan["deferred"])
        worker, editor = plan["tasks"]
        self.assertEqual((worker["owner"], worker["authorization_level"]), ("marketing", "propose"))
        self.assertEqual((editor["owner"], editor["authorization_level"]), ("editor", "observe"))
        self.assertEqual(worker["campaign_external_action"], action)
        self.assertEqual(editor["campaign_external_action"], action)
        self.assertEqual(worker["campaign_product_url"], "https://company.example/product")
        self.assertEqual(editor["campaign_product_url"], "https://company.example/product")
        self.assertEqual(worker["campaign_changed_variable"], "call_to_action")
        self.assertEqual(editor["campaign_evidence_basis"], "Day-5 decision=pivot")
        self.assertNotIn(
            "campaign_publish_bluesky",
            autonomy_team.allowed_tool_names(
                {"read_file", "campaign_publish_bluesky"},
                worker["authorization_level"],
            ),
        )

    def test_company_plan_requires_explicit_acceptance_criteria(self):
        plan = autonomy_team.build_company_plan(
            self.item(acceptance_criteria=[]), self.worker_decision(), 5.0, router=self.router
        )
        self.assertTrue(plan["deferred"])
        self.assertEqual(plan["deferral_reason"], "missing_acceptance_criteria")
        self.assertEqual(plan["tasks"], [])

    def test_company_plan_defers_when_required_review_cannot_be_reserved(self):
        plan = autonomy_team.build_company_plan(
            self.item(), self.worker_decision(), 0.05, router=self.router
        )
        self.assertTrue(plan["deferred"])
        self.assertEqual(plan["tasks"], [])
        self.assertIn("review", plan["reason"].lower())

    def test_completed_company_result_aggregates_evidence(self):
        state = {
            "projects": [{
                "id": "proj-1", "status": "completed", "editor_verdict": "approved",
                "revision_round": 0, "last_editor_feedback": "APPROVED: criteria met.",
            }],
            "tasks": [
                {
                    "id": "t1", "project_id": "proj-1", "owner": "manager", "status": "done",
                    "title": "Inspect", "spent_usd": 0.01, "model": "gpt-5.4-nano",
                    "execution_attempts": 1,
                    "result": "Schedule verified: mon-fri at 08:00 America/Phoenix.",
                    "artifacts": ["file: files/report.md"],
                    "usage_records": [{"input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 20}],
                },
                {
                    "id": "t2", "project_id": "proj-1", "owner": "editor", "status": "done",
                    "title": "Review", "result": "APPROVED: criteria met.", "spent_usd": 0.02,
                    "model": "gpt-5.4-mini", "execution_attempts": 1,
                    "artifacts": [],
                    "usage_records": [{"input_tokens": 200, "output_tokens": 30}],
                },
            ],
            "cost_entries": [
                {"project_id": "proj-1", "task_id": "t1", "cost_basis": "actual"},
                {"project_id": "proj-1", "task_id": "t2", "cost_basis": "actual"},
            ],
        }
        result = autonomy_team.aggregate_company_result(state, "proj-1")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["actual_cost_usd"], 0.03)
        self.assertEqual(result["estimated_cost_usd"], 0.0)
        self.assertTrue(result["model_invoked"])
        self.assertEqual(result["token_usage"]["total_tokens"], 350)
        self.assertEqual(result["files_changed"], ["files/report.md"])
        self.assertEqual(result["review_outcome"], "approved")
        self.assertEqual(result["review_outcomes"], ["APPROVED: criteria met."])
        self.assertEqual(
            result["result_text"],
            "Schedule verified: mon-fri at 08:00 America/Phoenix.",
        )
        self.assertEqual(result["result"], result["result_text"])
        self.assertEqual(result["result_task_id"], "t1")
        self.assertEqual(result["result_agent"], "manager")
        self.assertFalse(result["result_truncated"])
        self.assertEqual(result["agents"], ["manager", "editor"])
        self.assertEqual(result["models"], ["gpt-5.4-nano", "gpt-5.4-mini"])
        self.assertEqual(result["costs"]["by_model"]["gpt-5.4-mini"], 0.02)

    def test_blocked_company_result_maps_permission_for_workflow_escalation(self):
        state = {
            "projects": [{"id": "proj-1", "status": "blocked"}],
            "tasks": [{
                "id": "t1", "project_id": "proj-1", "owner": "code", "status": "needs_human",
                "title": "Inspect protected repo", "result": "403 permission denied",
                "failure_classification": "permission", "spent_usd": 0.0,
            }],
            "cost_entries": [],
        }
        result = autonomy_team.aggregate_company_result(state, "proj-1")
        self.assertEqual(result["status"], "needs_human")
        self.assertEqual(result["failure_classification"], "permission_denied")
        self.assertIn("permission", result["human_action"].lower())

    def test_company_result_attributes_helper_model_agent_cost_and_reason(self):
        state = {
            "projects": [{
                "id": "proj-help", "status": "completed", "editor_verdict": "approved",
            }],
            "tasks": [{
                "id": "worker", "project_id": "proj-help", "owner": "code",
                "status": "done", "title": "Validate", "result": "Validated.",
                "spent_usd": 0.03, "model": "worker-model", "execution_attempts": 1,
                "attempt_history": [{
                    "model": "worker-model", "model_reason": "Standard coding route."
                }],
                "usage_records": [
                    {"model": "worker-model", "agent": "code", "input_tokens": 100,
                     "output_tokens": 20, "cost_usd": 0.02},
                    {"model": "helper-model", "agent": "research", "input_tokens": 50,
                     "output_tokens": 10, "cost_usd": 0.01},
                ],
                "team_help_events": [{
                    "requesting_agent": "code", "helper_agent": "research",
                    "question": "Is the claim supported?", "reason": "Need a source check.",
                    "response": "Yes.", "helper_model": "helper-model",
                    "model_reason": "Lightweight no-tool research help.",
                    "task_type": "classification", "complexity": "lightweight",
                    "risk": "low", "status": "completed", "cost_usd": 0.01,
                }],
            }],
            "cost_entries": [{
                "project_id": "proj-help", "task_id": "worker", "cost_basis": "actual",
            }],
        }

        result = autonomy_team.aggregate_company_result(state, "proj-help")

        self.assertEqual(result["agents"], ["code", "research"])
        self.assertEqual(result["models"], ["worker-model", "helper-model"])
        self.assertEqual(result["costs"]["by_agent"], {"code": 0.02, "research": 0.01})
        self.assertEqual(result["costs"]["by_model"]["helper-model"], 0.01)
        self.assertEqual(result["collaborations"][0]["helper_agent"], "research")
        self.assertTrue(any(
            "help code -> research / helper-model" in reason
            for reason in result["model_selection_reasons"]
        ))

    def test_budget_only_company_block_is_deferred_without_owner_action(self):
        state = {
            "projects": [{"id": "proj-1", "status": "blocked"}],
            "tasks": [{
                "id": "t1", "project_id": "proj-1", "owner": "code",
                "status": "needs_human", "title": "Large audit",
                "result": "The next request cannot fit inside today's ordinary budget.",
                "failure_classification": "budget", "spent_usd": 0.4,
            }],
            "cost_entries": [],
        }

        result = autonomy_team.aggregate_company_result(state, "proj-1")

        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["failure_classification"], "budget_exhausted")
        self.assertEqual(result["human_action"], "")

    def test_idea_context_includes_bounded_history_but_excludes_private_fields(self):
        context = autonomy_team.idea_project_context({
            "projects": [{
                "id": "p1", "name": "Project", "status": "active", "secret": "TOKEN-123",
                "goals": [{"id": "g1", "title": "Goal", "status": "active", "private": "hide"}],
                "roadmap_items": [{"id": "i1", "title": "Item", "status": "done", "description": "hide"}],
            }],
            "idea_backlog": [{
                "id": "idea-1", "idea": "Deployment health digest", "status": "proposed",
                "relationship_to_current_goals": "Improves reliability", "risks": "hide",
            }],
            "run_control": {"recent_runs": [{
                "run_id": "run-1", "final_status": "completed", "trigger_source": "scheduled",
                "private_result": "hide",
            }]},
        })
        self.assertIn("Project", context)
        self.assertIn("Deployment health digest", context)
        self.assertIn("run-1", context)
        self.assertNotIn("TOKEN-123", context)
        self.assertNotIn("private", context)
        self.assertNotIn("description", context)
        self.assertNotIn("risks", context)


if __name__ == "__main__":
    unittest.main()

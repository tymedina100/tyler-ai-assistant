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
                    "execution_attempts": 1, "artifacts": [],
                    "usage_records": [{"input_tokens": 100, "cached_input_tokens": 10, "output_tokens": 20}],
                },
                {
                    "id": "t2", "project_id": "proj-1", "owner": "editor", "status": "done",
                    "title": "Review", "result": "APPROVED: criteria met.", "spent_usd": 0.02,
                    "model": "gpt-5.4-mini", "execution_attempts": 1,
                    "artifacts": ["file: files/report.md"],
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

    def test_idea_context_excludes_descriptions_and_unrelated_fields(self):
        context = autonomy_team.idea_project_context({"projects": [{
            "id": "p1", "name": "Project", "status": "active", "secret": "TOKEN-123",
            "goals": [{"id": "g1", "title": "Goal", "status": "active", "private": "hide"}],
            "roadmap_items": [{"id": "i1", "title": "Item", "status": "done", "description": "hide"}],
        }]})
        self.assertIn("Project", context)
        self.assertNotIn("TOKEN-123", context)
        self.assertNotIn("private", context)
        self.assertNotIn("description", context)


if __name__ == "__main__":
    unittest.main()

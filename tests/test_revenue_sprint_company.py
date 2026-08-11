import hashlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import company_mode


PHOENIX = ZoneInfo("America/Phoenix")


def business_days(count, start=datetime(2026, 8, 3, 8, tzinfo=PHOENIX)):
    values = []
    current = start
    while len(values) < count:
        if current.weekday() < 5:
            values.append(current)
        current += timedelta(days=1)
    return values


class RevenueSprintCompanyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "company_state.json"
        state = company_mode.load_state(self.path)
        state["products"].append({
            "project_id": "product-project",
            "title": "Company Outreach Kit",
            "gumroad_url": "https://company.gumroad.com/l/outreach-kit",
            "gumroad_product_id": "gumroad-product-1",
            "sales_count": 0,
            "revenue_usd": 0.0,
            "last_synced": None,
        })
        company_mode.save_state(state, self.path)
        self.product = {
            "project_id": "product-project",
            "title": "Company Outreach Kit",
            "gumroad_url": "https://company.gumroad.com/l/outreach-kit",
            "gumroad_product_id": "gumroad-product-1",
            "ownership": "company_owned",
            "personal_fallback_allowed": False,
        }
        self.channel = {
            "type": "web",
            "account_id": "company-sales",
            "destination_scope": "web:freelancer-cold-email-site",
            "name": "Freelancer cold-email site",
            "ownership": "company_owned",
            "personal_fallback_allowed": False,
        }

    def tearDown(self):
        self.temp.cleanup()

    def policy(self, *, all_actions=False):
        allowed = ["outreach", "purchase"] if all_actions else ["outreach"]
        targets = {"outreach": ["web:freelancer-cold-email-site"]}
        daily = {"outreach": 2}
        total = {"outreach": 20}
        if all_actions:
            targets["purchase"] = ["vendor:company-ad-credit"]
            daily["purchase"] = 2
            total["purchase"] = 3
        return {
            "revision": "owner-policy-r1",
            "allowed_action_types": allowed,
            "allowed_targets": targets,
            "daily_action_caps": daily,
            "total_action_caps": total,
            "purchase_daily_cap_usd": 3.0 if all_actions else 0.0,
            "purchase_total_cap_usd": 5.0 if all_actions else 0.0,
            "approved_at": "2026-08-02T12:00:00-07:00",
            "approved_by": "company-owner",
        }

    def start(self, **kwargs):
        return company_mode.start_revenue_sprint(
            self.product,
            self.channel,
            kwargs.pop("automation_policy", self.policy()),
            self.path,
            max_consecutive_no_progress_days=kwargs.pop(
                "max_consecutive_no_progress_days", 30
            ),
            **kwargs,
        )

    @staticmethod
    def experiment(index, action_type="outreach"):
        return {
            "id": f"experiment-{index}",
            "hypothesis": f"Company experiment {index} will produce a measurable response.",
            "metric": "qualified replies",
            "success_threshold": ">= 1 qualified reply",
            "action_type": action_type,
        }

    @staticmethod
    def gumroad(sales_count, revenue_usd):
        return [{
            "id": "gumroad-product-1",
            "name": "Company Outreach Kit",
            "short_url": "https://company.gumroad.com/l/outreach-kit",
            "sales_count": sales_count,
            "sales_usd_cents": int(round(revenue_usd * 100)),
            "published": True,
        }]

    def approve_action_payload(
        self,
        sprint_id,
        run_id,
        action_type,
        target,
        payload_digest,
        *,
        policy_revision="owner-policy-r1",
    ):
        company_mode.assign_goal(
            f"Review one exact {action_type} campaign payload",
            ["general", "editor"],
            ["general", "editor"],
            self.path,
            tasks=[
                {"owner": "general", "title": "Draft", "estimate_usd": 0.0},
                {"owner": "editor", "title": "Review", "estimate_usd": 0.0},
            ],
            project_metadata={
                "campaign_id": sprint_id,
                "revenue_sprint_run_id": run_id,
                "external_action": {
                    "action_type": action_type,
                    "target": target,
                    "policy_revision": policy_revision,
                },
            },
        )
        _message, project_id = company_mode.approve_project(
            self.path, notify_hooks=False
        )
        state = company_mode.load_state(self.path)
        tasks = company_mode.project_tasks(state, project_id)
        worker = next(task for task in tasks if task["owner"] != "editor")
        reviewer = next(task for task in tasks if task["owner"] == "editor")
        company_mode.update_task_status(
            worker["id"],
            "done",
            result="CAMPAIGN_DRAFT_JSON: exact reviewed candidate",
            path=self.path,
        )
        review = "APPROVED: the exact campaign payload satisfies the criteria."
        company_mode.update_task_status(
            reviewer["id"], "done", result=review, path=self.path
        )
        company_mode.set_project_revision_flag(project_id, review, self.path)
        return company_mode.bind_approved_revenue_action(
            project_id, worker["id"], payload_digest, self.path
        )

    def test_start_persists_exact_scope_defaults_and_one_active_campaign(self):
        sprint = self.start(sprint_id="revenue-20-day")
        state = company_mode.load_state(self.path)

        self.assertEqual(state["company"]["active_revenue_sprint_id"], sprint["id"])
        self.assertEqual(len(state["revenue_sprints"]), 1)
        self.assertEqual(sprint["product"], self.product)
        self.assertEqual(sprint["channel"]["destination_scope"], "web:freelancer-cold-email-site")
        self.assertEqual(sprint["automation_policy"]["revision"], "owner-policy-r1")
        self.assertEqual(sprint["total_ai_budget_usd"], 100.0)
        self.assertEqual(sprint["daily_ai_budget_usd"], 5.0)
        self.assertEqual(sprint["max_run_days"], 20)
        self.assertEqual(sprint["checkpoint_policy"]["trailing_window_days"], 7)
        self.assertEqual(sprint["checkpoint_policy"]["minimum_trailing_gross_revenue_usd"], 35.0)
        with self.assertRaises(company_mode.RevenueSprintError):
            self.start(sprint_id="second")

    def test_start_rejects_unregistered_product_wildcard_scope_and_unrevisioned_policy(self):
        wrong = {**self.product, "gumroad_url": "https://company.gumroad.com/l/missing"}
        with self.assertRaises(company_mode.RevenueSprintError):
            company_mode.start_revenue_sprint(
                wrong, self.channel, self.policy(), self.path
            )
        with self.assertRaises(company_mode.RevenueSprintError):
            company_mode.start_revenue_sprint(
                self.product,
                {**self.channel, "destination_scope": "web:*"},
                self.policy(),
                self.path,
            )
        without_company_ownership = dict(self.channel)
        without_company_ownership.pop("ownership")
        with self.assertRaises(company_mode.RevenueSprintError):
            company_mode.start_revenue_sprint(
                self.product, without_company_ownership, self.policy(), self.path
            )
        with self.assertRaises(company_mode.RevenueSprintError):
            company_mode.start_revenue_sprint(
                self.product,
                {**self.channel, "personal_fallback_allowed": True},
                self.policy(),
                self.path,
            )
        without_product_ownership = dict(self.product)
        without_product_ownership.pop("ownership")
        with self.assertRaises(company_mode.RevenueSprintError):
            company_mode.start_revenue_sprint(
                without_product_ownership, self.channel, self.policy(), self.path
            )
        with self.assertRaises(company_mode.RevenueSprintError):
            company_mode.start_revenue_sprint(
                {**self.product, "personal_fallback_allowed": True},
                self.channel,
                self.policy(),
                self.path,
            )
        unrevisioned = self.policy()
        unrevisioned["revision"] = ""
        with self.assertRaises(company_mode.RevenueSprintError):
            company_mode.start_revenue_sprint(
                self.product, self.channel, unrevisioned, self.path
            )

    def test_five_dollar_daily_cap_includes_the_emergency_reserve(self):
        # The campaign reserve remains inside its own $5 ceiling even when the
        # surrounding Company day has much more ordinary budget available.
        company_mode.set_daily_budget(10.0, self.path)
        sprint = self.start()
        initial = company_mode.revenue_sprint_status(
            self.path, sprint_id=sprint["id"]
        )["budget"]
        self.assertEqual(initial["remaining_today_usd"], 5.0)
        self.assertEqual(initial["ordinary_remaining_today_usd"], 4.75)
        self.assertEqual(initial["emergency_reserve_usd"], 0.25)
        ordinary = company_mode.reserve_budget(
            4.75, self.path, campaign_id=sprint["id"], context="task"
        )
        with self.assertRaises(company_mode.BudgetExceededError):
            company_mode.reserve_budget(
                0.01, self.path, campaign_id=sprint["id"], context="task"
            )
        emergency = company_mode.reserve_budget(
            0.25,
            self.path,
            campaign_id=sprint["id"],
            context="summary",
            allow_emergency=True,
        )
        snapshot = company_mode.revenue_sprint_status(
            self.path, sprint_id=sprint["id"]
        )["budget"]
        self.assertEqual(snapshot["reserved_today_usd"], 5.0)
        self.assertEqual(snapshot["remaining_today_usd"], 0.0)
        self.assertTrue(emergency["uses_emergency_reserve"])
        company_mode.release_budget(ordinary["id"], self.path)
        company_mode.release_budget(emergency["id"], self.path)

    def test_concurrent_campaign_admission_cannot_oversubscribe_day(self):
        company_mode.set_daily_budget(20.0, self.path)
        sprint = self.start()

        def reserve(index):
            try:
                return company_mode.reserve_budget(
                    0.5,
                    self.path,
                    campaign_id=sprint["id"],
                    task_id=f"task-{index}",
                )
            except company_mode.BudgetExceededError:
                return None

        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(reserve, range(20)))

        # Nine 50-cent ordinary claims fit under the $4.75 ordinary ceiling.
        # The remaining 25 cents of ordinary capacity plus the 25-cent reserve
        # can then fund one bounded summary claim.
        self.assertEqual(sum(value is not None for value in results), 9)
        emergency = company_mode.reserve_budget(
            0.5,
            self.path,
            campaign_id=sprint["id"],
            context="summary",
            allow_emergency=True,
        )
        self.assertTrue(emergency["uses_emergency_reserve"])
        snapshot = company_mode.revenue_sprint_status(
            self.path, sprint_id=sprint["id"]
        )["budget"]
        self.assertEqual(snapshot["reserved_today_usd"], 5.0)
        self.assertEqual(snapshot["remaining_today_usd"], 0.0)

    def test_total_cap_and_reconciliation_are_campaign_attributed_and_terminal(self):
        company_mode.set_daily_budget(20.0, self.path)
        sprint = self.start(total_ai_budget_usd=1.0, daily_ai_budget_usd=1.0)
        reservation = company_mode.reserve_budget(
            1.0,
            self.path,
            campaign_id=sprint["id"],
            project_id="campaign-project",
            task_id="campaign-task",
            context="summary",
            allow_emergency=True,
        )
        cost = company_mode.reconcile_budget(reservation["id"], 1.0, self.path)
        status = company_mode.revenue_sprint_status(self.path, sprint_id=sprint["id"])

        self.assertEqual(cost["campaign_id"], sprint["id"])
        self.assertEqual(cost["campaign_date"], reservation["campaign_date"])
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(status["stop_reason"], "campaign_ai_budget_exhausted")
        with self.assertRaises(company_mode.BudgetExceededError):
            company_mode.reserve_budget(0.01, self.path, campaign_id=sprint["id"])

    def test_reservation_idempotency_cannot_change_campaign_attribution(self):
        company_mode.set_daily_budget(20.0, self.path)
        sprint = self.start()
        first = company_mode.reserve_budget(
            0.5,
            self.path,
            campaign_id=sprint["id"],
            task_id="exact-task",
            reservation_id="exact-reservation",
        )
        replay = company_mode.reserve_budget(
            0.5,
            self.path,
            campaign_id=sprint["id"],
            task_id="exact-task",
            reservation_id="exact-reservation",
        )
        self.assertEqual(first["id"], replay["id"])
        with self.assertRaises(ValueError):
            company_mode.reserve_budget(
                0.5,
                self.path,
                campaign_id=sprint["id"],
                task_id="different-task",
                reservation_id="exact-reservation",
            )
        with self.assertRaises(ValueError):
            company_mode.reconcile_budget(
                first["id"], 0.1, self.path, campaign_id="different-campaign"
            )

    def test_assign_and_expansion_use_same_campaign_envelope(self):
        company_mode.set_daily_budget(20.0, self.path)
        sprint = self.start(total_ai_budget_usd=10.0, daily_ai_budget_usd=1.25)
        result = company_mode.assign_goal(
            "Run one company acquisition experiment",
            ["general", "editor"],
            ["general", "editor"],
            self.path,
            tasks=[
                {"owner": "general", "title": "Execute", "estimate_usd": 0.2},
                {"owner": "editor", "title": "Review", "estimate_usd": 0.2},
            ],
            project_metadata={"campaign_id": sprint["id"]},
        )
        self.assertIn("Company goal accepted", result)
        state = company_mode.load_state(self.path)
        first, second = state["tasks"]
        expanded = company_mode.expand_task_budget_reservation(
            first["id"], 0.8, 0.8, self.path
        )
        denied = company_mode.expand_task_budget_reservation(
            second["id"], 0.3, 0.3, self.path
        )

        self.assertEqual(first["campaign_id"], sprint["id"])
        self.assertEqual(expanded["amount_usd"], 0.8)
        self.assertFalse(denied["expanded"])
        self.assertEqual(denied["reason"], "insufficient_ordinary_budget")
        company_mode.update_task_status(
            first["id"], "done", spent_usd=0.7, path=self.path
        )
        cost = company_mode.load_state(self.path)["cost_entries"][-1]
        self.assertEqual(cost["campaign_id"], sprint["id"])

    def test_assign_preserves_only_exact_campaign_execution_metadata(self):
        company_mode.set_daily_budget(20.0, self.path)
        sprint = self.start()
        day = business_days(1)[0]
        run_id = "metadata-run"
        company_mode.claim_revenue_sprint_run(
            run_id,
            self.experiment("metadata"),
            self.path,
            sprint_id=sprint["id"],
            at=day,
        )
        company_mode.assign_goal(
            "Run one exact company outreach action",
            ["general"],
            ["general"],
            self.path,
            tasks=[{"owner": "general", "title": "Execute", "estimate_usd": 0.1}],
            project_metadata={
                "campaign_id": sprint["id"],
                "revenue_sprint_run_id": run_id,
                "external_action": {
                    "action_type": "outreach",
                    "target": "web:freelancer-cold-email-site",
                    "policy_revision": "owner-policy-r1",
                },
            },
        )
        project = company_mode.active_project(company_mode.load_state(self.path))
        self.assertEqual(project["revenue_sprint_run_id"], run_id)
        self.assertEqual(project["external_action"], {
            "action_type": "outreach",
            "target": "web:freelancer-cold-email-site",
            "policy_revision": "owner-policy-r1",
        })
        with self.assertRaises(company_mode.RevenueSprintError):
            company_mode.assign_goal(
                "Unsafe passthrough",
                ["general"],
                ["general"],
                self.path,
                tasks=[{"owner": "general", "title": "Execute", "estimate_usd": 0.1}],
                project_metadata={
                    "campaign_id": sprint["id"],
                    "revenue_sprint_run_id": run_id,
                    "external_action": {
                        "action_type": "outreach",
                        "target": "web:freelancer-cold-email-site",
                        "policy_revision": "owner-policy-r1",
                        "unreviewed_payload": "must not persist",
                    },
                },
            )

    def test_campaign_revision_preserves_draft_metadata_and_reservation_attribution(self):
        company_mode.set_daily_budget(20.0, self.path)
        sprint = self.start()
        action = {
            "action_type": "outreach",
            "target": "web:freelancer-cold-email-site",
            "policy_revision": "owner-policy-r1",
        }
        company_mode.assign_goal(
            "Draft and review one exact company outreach action",
            ["general", "editor"],
            ["general", "editor"],
            self.path,
            tasks=[
                {
                    "owner": "general",
                    "title": "Draft the outreach",
                    "estimate_usd": 0.2,
                    "authorization_level": "propose",
                    "enforce_authorization": True,
                    "campaign_external_action": action,
                    "campaign_product_url": self.product["gumroad_url"],
                    "campaign_changed_variable": "call_to_action",
                    "campaign_evidence_basis": "Day-5 decision=pivot",
                },
                {
                    "owner": "editor",
                    "title": "Review the draft",
                    "estimate_usd": 0.2,
                    "authorization_level": "observe",
                    "enforce_authorization": True,
                    "campaign_external_action": action,
                    "campaign_product_url": self.product["gumroad_url"],
                    "campaign_changed_variable": "call_to_action",
                    "campaign_evidence_basis": "Day-5 decision=pivot",
                },
            ],
            project_metadata={"campaign_id": sprint["id"]},
        )
        state = company_mode.load_state(self.path)
        project_id = state["company"]["active_project_id"]
        company_mode.set_project_revision_flag(
            project_id,
            "REVISIONS REQUIRED: make the offer and destination explicit.",
            self.path,
        )

        created, _note = company_mode.start_revision_round(
            project_id, ["general", "editor"], self.path
        )

        self.assertTrue(created)
        state = company_mode.load_state(self.path)
        revised = company_mode.project_tasks(state, project_id)[-2:]
        self.assertEqual(
            [(task["owner"], task["authorization_level"]) for task in revised],
            [("general", "propose"), ("editor", "observe")],
        )
        self.assertTrue(all(task["campaign_external_action"] == action for task in revised))
        self.assertTrue(all(
            task["campaign_product_url"] == self.product["gumroad_url"]
            for task in revised
        ))
        self.assertTrue(all(
            task["campaign_changed_variable"] == "call_to_action"
            and task["campaign_evidence_basis"] == "Day-5 decision=pivot"
            for task in revised
        ))
        revised_reservations = {
            reservation["task_id"]: reservation
            for reservation in state["budget_reservations"]
            if reservation.get("task_id") in {task["id"] for task in revised}
        }
        self.assertEqual(set(revised_reservations), {task["id"] for task in revised})
        self.assertTrue(all(
            reservation["campaign_id"] == sprint["id"]
            for reservation in revised_reservations.values()
        ))

    def test_campaign_revision_is_denied_by_campaign_cap_while_global_budget_remains(self):
        company_mode.set_daily_budget(20.0, self.path)
        sprint = self.start(total_ai_budget_usd=10.0, daily_ai_budget_usd=1.0)
        company_mode.assign_goal(
            "Draft and review one campaign action",
            ["general", "editor"],
            ["general", "editor"],
            self.path,
            tasks=[
                {"owner": "general", "title": "Draft", "estimate_usd": 0.2},
                {"owner": "editor", "title": "Review", "estimate_usd": 0.2},
            ],
            project_metadata={"campaign_id": sprint["id"]},
        )
        before = company_mode.load_state(self.path)
        project_id = before["company"]["active_project_id"]
        original_task_count = len(company_mode.project_tasks(before, project_id))
        original_reservation_count = len(before["budget_reservations"])
        self.assertGreater(company_mode.remaining_budget(before), 10.0)
        self.assertEqual(
            company_mode.revenue_sprint_status(
                self.path, sprint_id=sprint["id"]
            )["budget"]["ordinary_remaining_today_usd"],
            0.35,
        )
        company_mode.set_project_revision_flag(
            project_id, "REVISIONS REQUIRED: tighten the evidence.", self.path
        )

        created, note = company_mode.start_revision_round(
            project_id, ["general", "editor"], self.path
        )

        self.assertFalse(created)
        self.assertIn("not enough budget", note)
        after = company_mode.load_state(self.path)
        self.assertEqual(len(company_mode.project_tasks(after, project_id)), original_task_count)
        self.assertEqual(len(after["budget_reservations"]), original_reservation_count)
        self.assertGreater(company_mode.remaining_budget(after), 10.0)

    def test_run_claims_are_weekday_date_unique_and_day5_requires_pivot(self):
        sprint = self.start()
        days = business_days(6)
        weekend = datetime(2026, 8, 8, 8, tzinfo=PHOENIX)
        with self.assertRaises(company_mode.RevenueSprintError):
            company_mode.claim_revenue_sprint_run(
                "weekend", self.experiment("weekend"), self.path,
                sprint_id=sprint["id"], at=weekend,
            )
        for index, day in enumerate(days[:5], 1):
            claimed = company_mode.claim_revenue_sprint_run(
                f"run-{index}", self.experiment(index), self.path,
                sprint_id=sprint["id"], at=day,
            )
            if index == 1:
                replay = company_mode.claim_revenue_sprint_run(
                    "run-1", self.experiment(1), self.path,
                    sprint_id=sprint["id"], at=day,
                )
                self.assertEqual(replay["run_id"], claimed["run_id"])
                self.assertTrue(replay["idempotent_replay"])
                changed = self.experiment(1)
                changed["hypothesis"] = "Different parameters must not replay."
                with self.assertRaises(company_mode.RevenueSprintError):
                    company_mode.claim_revenue_sprint_run(
                        "run-1", changed, self.path,
                        sprint_id=sprint["id"], at=day,
                    )
            with self.assertRaises(company_mode.RevenueSprintError):
                company_mode.claim_revenue_sprint_run(
                    f"other-{index}", self.experiment(f"other-{index}"), self.path,
                    sprint_id=sprint["id"], at=day,
                )
            completed = company_mode.complete_revenue_sprint_run(
                f"run-{index}", "succeeded", self.path,
                sprint_id=sprint["id"], progress=True, at=day,
            )
            if index == 1:
                replay = company_mode.complete_revenue_sprint_run(
                    "run-1", "succeeded", self.path,
                    sprint_id=sprint["id"], progress=True, at=day,
                )
                self.assertTrue(replay["idempotent_replay"])
                with self.assertRaises(company_mode.RevenueSprintError):
                    company_mode.complete_revenue_sprint_run(
                        "run-1", "succeeded", self.path,
                        sprint_id=sprint["id"], progress=False, at=day,
                    )
        self.assertTrue(completed["pivot_required"])
        with self.assertRaises(company_mode.RevenueSprintError):
            company_mode.claim_revenue_sprint_run(
                "run-6", self.experiment(6), self.path,
                sprint_id=sprint["id"], at=days[5],
            )
        company_mode.record_revenue_sprint_pivot(
            "Keep the same company channel but narrow the offer to one buyer segment.",
            self.path,
            sprint_id=sprint["id"],
            run_id="run-5",
            at=days[4],
        )
        claimed = company_mode.claim_revenue_sprint_run(
            "run-6", self.experiment(6), self.path,
            sprint_id=sprint["id"], at=days[5],
        )
        self.assertEqual(claimed["ordinal"], 6)

    def test_day15_stops_without_sale_or_strong_intent(self):
        sprint = self.start()
        days = business_days(15)
        for index, day in enumerate(days, 1):
            run_id = f"run-{index}"
            company_mode.claim_revenue_sprint_run(
                run_id, self.experiment(index), self.path,
                sprint_id=sprint["id"], at=day,
            )
            if index == 1:
                company_mode.record_revenue_signal(
                    "click", self.path, sprint_id=sprint["id"], run_id=run_id,
                    evidence="Anonymous company-site click aggregate.", at=day,
                )
            completed = company_mode.complete_revenue_sprint_run(
                run_id, "succeeded", self.path, sprint_id=sprint["id"],
                progress=True, at=day,
            )
        self.assertEqual(completed["campaign_status"], "stopped")
        self.assertEqual(completed["stop_reason"], "day15_no_sale_or_strong_intent")

    def test_snapshots_create_sale_signals_day20_stops_and_economic_scope_is_honest(self):
        sprint = self.start()
        days = business_days(20)
        cumulative = 0.0
        for index, day in enumerate(days, 1):
            run_id = f"run-{index}"
            company_mode.claim_revenue_sprint_run(
                run_id, self.experiment(index), self.path,
                sprint_id=sprint["id"], at=day,
            )
            if index == 1:
                with self.assertRaises(company_mode.RevenueSprintError):
                    company_mode.record_revenue_snapshot(
                        self.gumroad(index, cumulative + 5.0), "after", run_id, self.path,
                        sprint_id=sprint["id"], at=day,
                    )
                wrong_product = self.gumroad(index - 1, cumulative)
                wrong_product[0]["short_url"] = "https://company.gumroad.com/l/different"
                with self.assertRaises(company_mode.RevenueSprintError):
                    company_mode.record_revenue_snapshot(
                        wrong_product, "before", run_id, self.path,
                        sprint_id=sprint["id"], at=day,
                    )
            before = company_mode.record_revenue_snapshot(
                self.gumroad(index - 1, cumulative), "before", run_id, self.path,
                sprint_id=sprint["id"], at=day,
            )
            cumulative += 5.0
            after = company_mode.record_revenue_snapshot(
                self.gumroad(index, cumulative), "after", run_id, self.path,
                sprint_id=sprint["id"], at=day,
            )
            self.assertNotEqual(before["id"], after["id"])
            completed = company_mode.complete_revenue_sprint_run(
                run_id, "succeeded", self.path, sprint_id=sprint["id"], at=day,
            )

        status = company_mode.revenue_sprint_status(
            self.path, sprint_id=sprint["id"], at=days[-1]
        )
        verdict = status["economic_verdict"]
        self.assertEqual(completed["stop_reason"], "day20_limit_reached")
        self.assertEqual(status["status"], "stopped")
        self.assertEqual(verdict["trailing_gross_revenue_usd"], 35.0)
        self.assertEqual(verdict["trailing_gross_revenue_usd_per_day"], 5.0)
        self.assertTrue(verdict["target_demonstrated"])
        self.assertFalse(verdict["self_sustaining_verified"])
        self.assertFalse(verdict["fee_data_available"])
        self.assertEqual(verdict["scope"], "before_unavailable_gumroad_and_infrastructure_fees")
        persisted = company_mode.load_state(self.path)["revenue_sprints"][0]
        self.assertEqual(len([s for s in persisted["signals"] if s["type"] == "sale"]), 20)
        day20 = next(entry for entry in persisted["checkpoint_results"] if entry["day"] == 20)
        self.assertIn("economic_verdict", day20["evidence"])

    def test_sale_first_seen_next_morning_is_attributed_to_prior_run_window(self):
        sprint = self.start()
        day1, day2 = business_days(2)
        company_mode.claim_revenue_sprint_run(
            "run-1", self.experiment(1), self.path,
            sprint_id=sprint["id"], at=day1,
        )
        company_mode.record_revenue_snapshot(
            self.gumroad(0, 0.0), "before", "run-1", self.path,
            sprint_id=sprint["id"], at=day1,
        )
        company_mode.record_revenue_snapshot(
            self.gumroad(0, 0.0), "after", "run-1", self.path,
            sprint_id=sprint["id"], at=day1,
        )
        company_mode.complete_revenue_sprint_run(
            "run-1", "succeeded", self.path,
            sprint_id=sprint["id"], progress=True, at=day1,
        )
        company_mode.claim_revenue_sprint_run(
            "run-2", self.experiment(2), self.path,
            sprint_id=sprint["id"], at=day2,
        )
        company_mode.record_revenue_snapshot(
            self.gumroad(1, 5.0), "before", "run-2", self.path,
            sprint_id=sprint["id"], at=day2,
        )

        persisted = company_mode.load_state(self.path)["revenue_sprints"][0]
        sale = next(entry for entry in persisted["signals"] if entry["type"] == "sale")
        self.assertEqual(sale["run_id"], "run-1")
        self.assertEqual(sale["count"], 1)
        self.assertEqual(sale["value_usd"], 5.0)

    def test_repeated_no_progress_stops(self):
        sprint = self.start(max_consecutive_no_progress_days=2)
        days = business_days(2)
        for index, day in enumerate(days, 1):
            run_id = f"run-{index}"
            company_mode.claim_revenue_sprint_run(
                run_id, self.experiment(index), self.path,
                sprint_id=sprint["id"], at=day,
            )
            completed = company_mode.complete_revenue_sprint_run(
                run_id, "failed", self.path, sprint_id=sprint["id"],
                progress=False, at=day,
            )
        self.assertEqual(completed["campaign_status"], "stopped")
        self.assertEqual(completed["stop_reason"], "repeated_no_progress")

    def test_successful_execution_without_commercial_signal_is_no_progress(self):
        sprint = self.start(max_consecutive_no_progress_days=2)
        days = business_days(2)
        for index, day in enumerate(days, 1):
            run_id = f"receipt-only-{index}"
            company_mode.claim_revenue_sprint_run(
                run_id, self.experiment(index), self.path,
                sprint_id=sprint["id"], at=day,
            )
            company_mode.record_revenue_snapshot(
                self.gumroad(0, 0.0), "before", run_id, self.path,
                sprint_id=sprint["id"], at=day,
            )
            company_mode.record_revenue_snapshot(
                self.gumroad(0, 0.0), "after", run_id, self.path,
                sprint_id=sprint["id"], at=day,
            )
            completed = company_mode.complete_revenue_sprint_run(
                run_id, "succeeded", self.path, sprint_id=sprint["id"],
                progress=None, at=day,
            )

        self.assertFalse(completed["progress"])
        self.assertEqual(completed["campaign_status"], "stopped")
        self.assertEqual(completed["stop_reason"], "repeated_no_progress")

    def test_action_claim_is_atomically_bound_to_the_approved_payload(self):
        sprint = self.start()
        day = business_days(1)[0]
        run_id = "approval-bound-run"
        target = "web:freelancer-cold-email-site"
        company_mode.claim_revenue_sprint_run(
            run_id,
            self.experiment("approval-bound"),
            self.path,
            sprint_id=sprint["id"],
            at=day,
        )
        payload_a = hashlib.sha256(b"reviewed payload A").hexdigest()
        payload_b = hashlib.sha256(b"unreviewed payload B").hexdigest()
        approval = self.approve_action_payload(
            sprint["id"], run_id, "outreach", target, payload_a
        )
        self.assertEqual(approval["payload_digest"], payload_a)

        with self.assertRaisesRegex(
            company_mode.RevenueActionError, "does not match the persisted final approval"
        ):
            company_mode.claim_revenue_action(
                "outreach",
                target,
                run_id,
                self.path,
                sprint_id=sprint["id"],
                policy_revision="owner-policy-r1",
                approved_payload_digest=payload_b,
                idempotency_key="payload-b",
                metadata={"payload_digest": payload_b},
                at=day,
            )
        self.assertEqual(
            company_mode.revenue_sprint_status(
                self.path, sprint_id=sprint["id"]
            )["action_journal"],
            [],
        )

        claimed = company_mode.claim_revenue_action(
            "outreach",
            target,
            run_id,
            self.path,
            sprint_id=sprint["id"],
            policy_revision="owner-policy-r1",
            approved_payload_digest=payload_a,
            idempotency_key="payload-a",
            metadata={"payload_digest": payload_a},
            at=day,
        )
        self.assertEqual(claimed["approved_payload_digest"], payload_a)
        self.assertEqual(claimed["metadata"]["payload_digest"], payload_a)

    def test_action_journal_enforces_revision_exact_targets_counts_and_purchase_caps(self):
        sprint = self.start(automation_policy=self.policy(all_actions=True))
        day = business_days(1)[0]
        run_id = "action-run"
        company_mode.claim_revenue_sprint_run(
            run_id, self.experiment("actions"), self.path,
            sprint_id=sprint["id"], at=day,
        )
        wrong_revision = company_mode.revenue_action_capability(
            "outreach", "web:freelancer-cold-email-site", self.path,
            sprint_id=sprint["id"], policy_revision="wrong", at=day,
        )
        self.assertFalse(wrong_revision["allowed"])
        with self.assertRaises(company_mode.RevenueActionError):
            company_mode.claim_revenue_action(
                "outreach", "web:freelancer-cold-email-site", run_id, self.path,
                sprint_id=sprint["id"], policy_revision="wrong", at=day,
            )
        capability = company_mode.revenue_action_capability(
            "outreach", "web:freelancer-cold-email-site", self.path,
            sprint_id=sprint["id"], policy_revision="owner-policy-r1", at=day,
        )
        self.assertTrue(capability["allowed"])
        outreach_digest = hashlib.sha256(b"approved outreach payload").hexdigest()
        self.approve_action_payload(
            sprint["id"],
            run_id,
            "outreach",
            "web:freelancer-cold-email-site",
            outreach_digest,
        )
        action = company_mode.claim_revenue_action(
            "outreach", "web:freelancer-cold-email-site", run_id, self.path,
            sprint_id=sprint["id"], policy_revision="owner-policy-r1",
            approved_payload_digest=outreach_digest,
            idempotency_key="outreach-once",
            metadata={"payload_digest": outreach_digest},
            at=day,
        )
        replay = company_mode.claim_revenue_action(
            "outreach", "web:freelancer-cold-email-site", run_id, self.path,
            sprint_id=sprint["id"], policy_revision="owner-policy-r1",
            approved_payload_digest=outreach_digest,
            idempotency_key="outreach-once",
            metadata={"payload_digest": outreach_digest},
            at=day,
        )
        self.assertEqual(action["id"], replay["id"])
        self.assertTrue(replay["idempotent_replay"])
        with self.assertRaises(company_mode.RevenueActionError):
            company_mode.claim_revenue_action(
                "outreach", "web:freelancer-cold-email-site", run_id, self.path,
                sprint_id=sprint["id"], policy_revision="owner-policy-r1",
                approved_payload_digest=outreach_digest,
                idempotency_key="outreach-once",
                metadata={"payload_digest": "changed"}, at=day,
            )
        completed_action = company_mode.complete_revenue_action(
            action["id"], "succeeded", self.path, sprint_id=sprint["id"],
            result="Company message sent.", at=day,
        )
        replay_completion = company_mode.complete_revenue_action(
            action["id"], "succeeded", self.path, sprint_id=sprint["id"],
            result="Company message sent.", at=day,
        )
        self.assertTrue(replay_completion["idempotent_replay"])
        with self.assertRaises(company_mode.RevenueActionError):
            company_mode.complete_revenue_action(
                action["id"], "succeeded", self.path, sprint_id=sprint["id"],
                result="Different completion.", at=day,
            )

        outreach_project = next(
            project
            for project in company_mode.load_state(self.path)["projects"]
            if project.get("revenue_sprint_run_id") == run_id
        )
        company_mode.complete_project(outreach_project["id"], self.path)

        with self.assertRaises(company_mode.RevenueActionError):
            company_mode.claim_revenue_action(
                "purchase", "vendor:company-ad-credit", run_id, self.path,
                sprint_id=sprint["id"], policy_revision="owner-policy-r1",
                approved_payload_digest=outreach_digest,
                purchase_amount_usd=2.0, idempotency_key="wrong-experiment-type",
                metadata={"payload_digest": outreach_digest},
                at=day,
            )
        company_mode.complete_revenue_sprint_run(
            run_id, "succeeded", self.path, sprint_id=sprint["id"],
            progress=True, at=day,
        )
        purchase_day = business_days(2)[1]
        purchase_run_id = "purchase-run"
        company_mode.claim_revenue_sprint_run(
            purchase_run_id,
            self.experiment("purchases", action_type="purchase"),
            self.path,
            sprint_id=sprint["id"],
            at=purchase_day,
        )
        purchase_digest = hashlib.sha256(b"approved purchase payload").hexdigest()
        self.approve_action_payload(
            sprint["id"],
            purchase_run_id,
            "purchase",
            "vendor:company-ad-credit",
            purchase_digest,
        )
        purchase = company_mode.claim_revenue_action(
            "purchase", "vendor:company-ad-credit", purchase_run_id, self.path,
            sprint_id=sprint["id"], policy_revision="owner-policy-r1",
            approved_payload_digest=purchase_digest,
            purchase_amount_usd=2.0,
            idempotency_key="purchase-one",
            metadata={"payload_digest": purchase_digest},
            at=purchase_day,
        )
        with self.assertRaises(company_mode.RevenueActionError):
            company_mode.claim_revenue_action(
                "purchase", "vendor:company-ad-credit", purchase_run_id, self.path,
                sprint_id=sprint["id"], policy_revision="owner-policy-r1",
                approved_payload_digest=purchase_digest,
                purchase_amount_usd=2.0, idempotency_key="purchase-too-large",
                metadata={"payload_digest": purchase_digest},
                at=purchase_day,
            )
        completed_purchase = company_mode.complete_revenue_action(
            purchase["id"], "succeeded", self.path, sprint_id=sprint["id"],
            actual_purchase_usd=1.0, at=purchase_day,
        )
        self.assertEqual(completed_purchase["actual_purchase_usd"], 1.0)
        second_purchase = company_mode.claim_revenue_action(
            "purchase", "vendor:company-ad-credit", purchase_run_id, self.path,
            sprint_id=sprint["id"], policy_revision="owner-policy-r1",
            approved_payload_digest=purchase_digest,
            purchase_amount_usd=2.0,
            idempotency_key="purchase-two",
            metadata={"payload_digest": purchase_digest},
            at=purchase_day,
        )
        uncertain = company_mode.complete_revenue_action(
            second_purchase["id"], "uncertain", self.path, sprint_id=sprint["id"],
            at=purchase_day,
        )
        self.assertEqual(uncertain["campaign_status"], "stopped")
        self.assertEqual(
            company_mode.revenue_sprint_status(self.path, sprint_id=sprint["id"])["stop_reason"],
            "external_action_outcome_uncertain",
        )
        verdict = company_mode.revenue_sprint_status(
            self.path, sprint_id=sprint["id"]
        )["economic_verdict"]
        self.assertEqual(verdict["campaign_purchase_spend_usd"], 3.0)
        self.assertEqual(verdict["observed_contribution_before_fees_usd"], -3.0)


if __name__ == "__main__":
    unittest.main()

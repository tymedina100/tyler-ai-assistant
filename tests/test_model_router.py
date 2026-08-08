import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from model_router import (
    CapabilityLevel,
    ModelRouter,
    RoutingRequest,
    estimate_cost,
    load_model_catalog,
)


class ModelRouterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_model_catalog()
        cls.router = ModelRouter(cls.catalog)

    def test_lightweight_task_selects_lowest_cost_capable_model(self):
        decision = self.router.route(
            RoutingRequest(
                task_type="classification",
                complexity="simple",
                risk="low",
                estimated_input_tokens=2_000,
                estimated_output_tokens=100,
                remaining_budget_usd=1.0,
            )
        )

        self.assertFalse(decision.deferred)
        self.assertEqual(decision.model_id, "gpt-5.4-nano")
        self.assertEqual(decision.model_level, CapabilityLevel.LIGHTWEIGHT)
        self.assertIn("lowest estimated-cost", decision.reason)

    def test_high_risk_architecture_selects_advanced_model(self):
        decision = self.router.route(
            RoutingRequest(
                task_type="architecture_decision",
                complexity="advanced",
                risk="high",
                required_capabilities=("architecture",),
                remaining_budget_usd=5.0,
            )
        )

        self.assertFalse(decision.deferred)
        self.assertEqual(decision.model_id, "gpt-5.6-sol")
        self.assertEqual(decision.required_level, CapabilityLevel.ADVANCED)
        self.assertIn("risk=high", decision.reason)

    def test_failed_cheaper_model_escalates_to_stronger_tier(self):
        decision = self.router.route(
            RoutingRequest(
                task_type="summarization",
                complexity="lightweight",
                previous_failures=1,
                previous_models=("gpt-5.4-nano",),
                remaining_budget_usd=5.0,
            )
        )

        self.assertFalse(decision.deferred)
        self.assertEqual(decision.model_id, "gpt-5.4-mini")
        self.assertIn("stronger than gpt-5.4-nano", decision.reason)

    def test_standard_failure_ladder_uses_luna_then_terra(self):
        luna = self.router.route(
            RoutingRequest(
                task_type="coding",
                complexity="standard",
                required_capabilities=("tool_use",),
                previous_failures=1,
                previous_models=("gpt-5.4-mini",),
                remaining_budget_usd=5.0,
            )
        )
        terra = self.router.route(
            RoutingRequest(
                task_type="coding",
                complexity="standard",
                required_capabilities=("tool_use",),
                previous_failures=2,
                previous_models=("gpt-5.4-mini", "gpt-5.6-luna"),
                remaining_budget_usd=5.0,
            )
        )

        self.assertEqual(luna.model_id, "gpt-5.6-luna")
        self.assertEqual(terra.model_id, "gpt-5.6-terra")

    def test_generic_advanced_debugging_uses_terra(self):
        decision = self.router.route(
            RoutingRequest(
                task_type="complex_debugging",
                complexity="advanced",
                risk="medium",
                required_capabilities=("debugging",),
                remaining_budget_usd=5.0,
            )
        )

        self.assertFalse(decision.deferred)
        self.assertEqual(decision.model_id, "gpt-5.6-terra")
        self.assertEqual(decision.model_level, CapabilityLevel.ADVANCED)

    def test_required_capability_filters_out_lightweight_model(self):
        decision = self.router.route(
            RoutingRequest(
                task_type="classification",
                complexity="lightweight",
                required_capabilities=("tool_use",),
                remaining_budget_usd=5.0,
            )
        )

        self.assertEqual(decision.model_id, "gpt-5.4-mini")
        self.assertEqual(decision.model_level, CapabilityLevel.STANDARD)

    def test_context_limit_filters_out_smaller_context_model(self):
        decision = self.router.route(
            RoutingRequest(
                task_type="summarization",
                complexity="lightweight",
                estimated_input_tokens=63_500,
                estimated_output_tokens=1_000,
                remaining_budget_usd=5.0,
            )
        )

        self.assertEqual(decision.model_id, "gpt-5.4-mini")

    def test_context_or_capability_gap_is_explicitly_deferred(self):
        decision = self.router.route(
            RoutingRequest(
                task_type="classification",
                required_capabilities=("quantum_hardware_control",),
                remaining_budget_usd=5.0,
            )
        )

        self.assertTrue(decision.deferred)
        self.assertIsNone(decision.model_id)
        self.assertEqual(decision.deferral_reason, "missing_capability")
        self.assertIn("deferred", decision.reason)

    def test_cost_estimate_splits_fresh_cached_and_output_tokens(self):
        model = self.catalog.require("gpt-5.4-mini")

        cost = estimate_cost(
            model,
            input_tokens=1_000_000,
            cached_input_tokens=250_000,
            output_tokens=100_000,
        )

        # 750k * $0.75/M + 250k * $0.075/M + 100k * $4.50/M.
        self.assertAlmostEqual(cost, 1.03125, places=8)

    def test_oversized_context_defers_without_selecting_model(self):
        decision = self.router.route(
            RoutingRequest(
                task_type="classification",
                complexity="lightweight",
                estimated_input_tokens=1_000_000,
                estimated_output_tokens=1_000_000,
                remaining_budget_usd=0.10,
            )
        )

        # Context filtering promotes this large request, then the cheapest fit is
        # still above budget. No provider API is involved in the decision.
        self.assertTrue(decision.deferred)
        self.assertIsNone(decision.model_id)
        self.assertEqual(decision.deferral_reason, "context_limit")

    def test_budget_deferral_reports_minimum_estimated_cost(self):
        decision = self.router.route(
            RoutingRequest(
                task_type="classification",
                complexity="lightweight",
                estimated_input_tokens=20_000,
                estimated_output_tokens=5_000,
                remaining_budget_usd=0.0001,
            )
        )

        self.assertTrue(decision.deferred)
        self.assertEqual(decision.deferral_reason, "insufficient_budget")
        self.assertGreater(decision.estimated_cost_usd, 0.0001)
        self.assertIn("above the remaining", decision.reason)

    def test_model_catalog_file_environment_override(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            catalog_path = Path(tmpdir) / "catalog.json"
            catalog_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "id": "local-test-model",
                                "level": "lightweight",
                                "capabilities": ["classification"],
                                "context_limit_tokens": 1000,
                                "input_usd_per_million": 1,
                                "cached_input_usd_per_million": 0.1,
                                "output_usd_per_million": 2,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"MODEL_CATALOG_FILE": str(catalog_path)}):
                catalog = load_model_catalog()

        self.assertEqual(catalog.models[0].model_id, "local-test-model")


if __name__ == "__main__":
    unittest.main()

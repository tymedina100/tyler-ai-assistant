import unittest

import telegram_roster_health as roster_health


SPECIALISTS = ("code", "research", "linear")
AGENT_INFO = {
    "manager": {"env_var": "TELEGRAM_MANAGER_BOT_TOKEN", "label": "Miles (Manager)"},
    "code": {"env_var": "TELEGRAM_CODE_BOT_TOKEN", "label": "Patch (Engineer)"},
    "research": {"env_var": "TELEGRAM_RESEARCH_BOT_TOKEN", "label": "Scout (Researcher)"},
    "linear": {"env_var": "TELEGRAM_LINEAR_BOT_TOKEN", "label": "Linear (Planner)"},
    "general": {"env_var": "TELEGRAM_GENERAL_BOT_TOKEN", "label": "Robin (General)"},
}


def complete_inputs():
    tokens = {}
    identities = {}
    memberships = {}
    for index, key in enumerate(("manager", *SPECIALISTS, "general"), start=1):
        tokens[AGENT_INFO[key]["env_var"]] = f"token-{index}"
        identities[key] = {
            "id": index,
            "is_bot": True,
            "username": f"team_{key}_bot",
            "can_read_all_group_messages": True,
        }
        memberships[key] = {"status": "member"}
    return tokens, identities, memberships


class TelegramRosterHealthTests(unittest.TestCase):
    def test_complete_roster_uses_manager_all_specialists_and_general(self):
        tokens, identities, memberships = complete_inputs()

        result = roster_health.evaluate_roster(
            specialist_keys=SPECIALISTS,
            agent_info=AGENT_INFO,
            token_values=tokens,
            identities=identities,
            group_memberships=memberships,
        )

        self.assertTrue(result.complete)
        self.assertEqual(result.expected_keys, ("manager", "code", "research", "linear", "general"))
        self.assertEqual(result.issue_count, 0)
        self.assertEqual(
            roster_health.render_roster_summary(result),
            "Telegram roster: COMPLETE (5/5 ready)",
        )

    def test_missing_token_is_actionable_without_claiming_unchecked_failures(self):
        tokens, identities, memberships = complete_inputs()
        tokens["TELEGRAM_LINEAR_BOT_TOKEN"] = "  "
        # Stale caller evidence for an unconfigured bot must not poison Miles's
        # otherwise-valid identity through duplicate detection.
        identities["linear"] = dict(identities["manager"])

        result = roster_health.evaluate_roster(
            specialist_keys=SPECIALISTS,
            agent_info=AGENT_INFO,
            token_values=tokens,
            identities=identities,
            group_memberships=memberships,
        )
        linear = next(agent for agent in result.agents if agent.key == "linear")
        manager = next(agent for agent in result.agents if agent.key == "manager")

        self.assertEqual(linear.issue_codes, (roster_health.MISSING_TOKEN,))
        self.assertTrue(manager.ready)
        summary = roster_health.render_roster_summary(result)
        self.assertIn("Linear (Planner) [linear]: missing_token", summary)
        self.assertIn("TELEGRAM_LINEAR_BOT_TOKEN", summary)
        self.assertNotIn("token-", summary)

    def test_invalid_identity_fails_closed(self):
        tokens, identities, memberships = complete_inputs()
        identities["research"] = {"id": 3, "is_bot": False, "username": "not_a_bot"}

        result = roster_health.evaluate_roster(
            specialist_keys=SPECIALISTS,
            agent_info=AGENT_INFO,
            token_values=tokens,
            identities=identities,
            group_memberships=memberships,
        )
        research = next(agent for agent in result.agents if agent.key == "research")

        self.assertEqual(research.issue_codes, (roster_health.INVALID_IDENTITY,))
        self.assertIn("does not identify a Telegram bot", research.issues[0].detail)

    def test_privacy_and_membership_are_reported_independently(self):
        tokens, identities, memberships = complete_inputs()
        identities["code"]["can_read_all_group_messages"] = False
        memberships["code"] = {"status": "left"}

        result = roster_health.evaluate_roster(
            specialist_keys=SPECIALISTS,
            agent_info=AGENT_INFO,
            token_values=tokens,
            identities=identities,
            group_memberships=memberships,
        )
        code = next(agent for agent in result.agents if agent.key == "code")

        self.assertEqual(
            code.issue_codes,
            (roster_health.PRIVACY_ENABLED, roster_health.NOT_IN_GROUP),
        )
        self.assertIn("BotFather", roster_health.render_roster_summary(result))

    def test_membership_check_unavailable_is_not_reported_as_not_in_group(self):
        tokens, identities, memberships = complete_inputs()
        memberships["code"] = {"check_unavailable": True}

        result = roster_health.evaluate_roster(
            specialist_keys=SPECIALISTS,
            agent_info=AGENT_INFO,
            token_values=tokens,
            identities=identities,
            group_memberships=memberships,
        )
        code = next(agent for agent in result.agents if agent.key == "code")

        self.assertEqual(code.issue_codes, (roster_health.CHECK_UNAVAILABLE,))
        summary = roster_health.render_roster_summary(result)
        self.assertIn("could not be verified after bounded retries", summary)
        self.assertNotIn("Add this bot to the configured Telegram group", summary)

    def test_restricted_member_is_in_group_only_when_is_member(self):
        tokens, identities, memberships = complete_inputs()
        memberships["linear"] = {"status": "restricted", "is_member": True}

        healthy = roster_health.evaluate_roster(
            specialist_keys=SPECIALISTS,
            agent_info=AGENT_INFO,
            token_values=tokens,
            identities=identities,
            group_memberships=memberships,
        )
        self.assertTrue(healthy.complete)

        memberships["linear"] = {"status": "restricted", "is_member": False}
        unhealthy = roster_health.evaluate_roster(
            specialist_keys=SPECIALISTS,
            agent_info=AGENT_INFO,
            token_values=tokens,
            identities=identities,
            group_memberships=memberships,
        )
        linear = next(agent for agent in unhealthy.agents if agent.key == "linear")
        self.assertEqual(linear.issue_codes, (roster_health.NOT_IN_GROUP,))

    def test_duplicate_tokens_or_identities_are_invalid_without_exposing_secrets(self):
        tokens, identities, memberships = complete_inputs()
        tokens["TELEGRAM_LINEAR_BOT_TOKEN"] = tokens["TELEGRAM_CODE_BOT_TOKEN"]
        identities["research"] = dict(identities["manager"])

        result = roster_health.evaluate_roster(
            specialist_keys=SPECIALISTS,
            agent_info=AGENT_INFO,
            token_values=tokens,
            identities=identities,
            group_memberships=memberships,
        )

        states = {agent.key: agent.issue_codes for agent in result.agents}
        self.assertEqual(states["code"], (roster_health.INVALID_IDENTITY,))
        self.assertEqual(states["linear"], (roster_health.INVALID_IDENTITY,))
        self.assertEqual(states["manager"], (roster_health.INVALID_IDENTITY,))
        self.assertEqual(states["research"], (roster_health.INVALID_IDENTITY,))
        rendered = roster_health.render_roster_summary(result)
        self.assertNotIn(tokens["TELEGRAM_CODE_BOT_TOKEN"], rendered)
        self.assertNotIn("team_manager_bot", rendered)

    def test_missing_agent_info_is_invalid_identity(self):
        tokens, identities, memberships = complete_inputs()
        agent_info = dict(AGENT_INFO)
        del agent_info["linear"]

        result = roster_health.evaluate_roster(
            specialist_keys=SPECIALISTS,
            agent_info=agent_info,
            token_values=tokens,
            identities=identities,
            group_memberships=memberships,
        )
        linear = next(agent for agent in result.agents if agent.key == "linear")

        self.assertEqual(linear.issue_codes, (roster_health.INVALID_IDENTITY,))
        self.assertIn("AGENT_INFO", linear.issues[0].detail)

    def test_serialized_health_contains_no_token_values(self):
        tokens, identities, memberships = complete_inputs()
        result = roster_health.evaluate_roster(
            specialist_keys=SPECIALISTS,
            agent_info=AGENT_INFO,
            token_values=tokens,
            identities=identities,
            group_memberships=memberships,
        )

        serialized = repr(result.to_dict())
        for token in tokens.values():
            self.assertNotIn(token, serialized)


if __name__ == "__main__":
    unittest.main()

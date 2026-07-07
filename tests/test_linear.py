import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import linear_helpers


class FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


CREATE_OK = {"data": {"issueCreate": {
    "success": True,
    "issue": {"id": "i1", "identifier": "ENG-1", "title": "Add alerts", "url": "https://l/ENG-1"},
}}}


class LinearHelpersTests(unittest.TestCase):
    def test_not_configured_without_key(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(linear_helpers.is_configured())
            issues, err = linear_helpers.list_issues()
        self.assertIsNone(issues)
        self.assertIn("isn't configured", err)

    def test_create_issue_builds_expected_graphql_payload(self):
        mock_post = MagicMock(return_value=FakeResp(200, CREATE_OK))
        with patch.dict(os.environ, {"LINEAR_API_KEY": "lin_key", "LINEAR_TEAM_ID": "team_123"}, clear=True), \
                patch.object(linear_helpers.requests, "post", mock_post):
            issue, err = linear_helpers.create_issue("Add alerts", "desc")

        self.assertIsNone(err)
        self.assertEqual(issue["identifier"], "ENG-1")

        # Endpoint + auth header.
        self.assertEqual(mock_post.call_args.args[0], linear_helpers.API_URL)
        self.assertEqual(mock_post.call_args.kwargs["headers"]["Authorization"], "lin_key")

        # GraphQL query + variables.
        sent = mock_post.call_args.kwargs["json"]
        self.assertIn("issueCreate", sent["query"])
        variables = sent["variables"]["input"]
        self.assertEqual(variables["title"], "Add alerts")
        self.assertEqual(variables["teamId"], "team_123")
        self.assertEqual(variables["description"], "desc")
        self.assertNotIn("projectId", variables)  # none configured

    def test_create_issue_requires_a_team(self):
        with patch.dict(os.environ, {"LINEAR_API_KEY": "lin_key"}, clear=True):
            issue, err = linear_helpers.create_issue("No team here")
        self.assertIsNone(issue)
        self.assertIn("team", err.lower())

    def test_create_project_issue_appends_footer(self):
        captured = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured["json"] = json
            return FakeResp(200, CREATE_OK)

        with patch.dict(os.environ, {"LINEAR_API_KEY": "k", "LINEAR_TEAM_ID": "team"}, clear=True), \
                patch.object(linear_helpers.requests, "post", fake_post):
            issue, err = linear_helpers.create_project_issue("vantage", "Add widget", "body")

        self.assertIsNone(err)
        desc = captured["json"]["variables"]["input"]["description"]
        self.assertIn("body", desc)
        self.assertIn("Project key: vantage", desc)
        self.assertIn("tymedina100/vantage", desc)

    def test_api_error_is_reported_cleanly(self):
        with patch.dict(os.environ, {"LINEAR_API_KEY": "k"}, clear=True), \
                patch.object(linear_helpers.requests, "post",
                             MagicMock(return_value=FakeResp(500, {"error": "boom"}))):
            teams, err = linear_helpers.list_teams()
        self.assertIsNone(teams)
        self.assertIn("500", err)

    def test_list_teams_parses_nodes(self):
        payload = {"data": {"teams": {"nodes": [
            {"id": "t1", "name": "Product Lab", "key": "VAN"},
        ]}}}
        with patch.dict(os.environ, {"LINEAR_API_KEY": "k"}, clear=True), \
                patch.object(linear_helpers.requests, "post",
                             MagicMock(return_value=FakeResp(200, payload))):
            teams, err = linear_helpers.list_teams()
        self.assertIsNone(err)
        self.assertEqual(teams[0]["key"], "VAN")
        self.assertEqual(teams[0]["id"], "t1")

    def test_list_projects_parses_nodes(self):
        payload = {"data": {"projects": {"nodes": [
            {"id": "p1", "name": "Watchlist", "state": "started"},
        ]}}}
        with patch.dict(os.environ, {"LINEAR_API_KEY": "k"}, clear=True), \
                patch.object(linear_helpers.requests, "post",
                             MagicMock(return_value=FakeResp(200, payload))):
            projects_list, err = linear_helpers.list_projects()
        self.assertIsNone(err)
        self.assertEqual(projects_list[0]["name"], "Watchlist")


if __name__ == "__main__":
    unittest.main()

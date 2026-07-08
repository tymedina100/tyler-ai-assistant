import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import railway_helpers


class FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class RailwayHelpersTests(unittest.TestCase):
    def test_not_configured_without_token(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(railway_helpers.is_configured())
            data, err = railway_helpers.list_projects()
        self.assertIsNone(data)
        self.assertIn("isn't configured", err)

    def test_list_projects_flattens_edges(self):
        payload = {"data": {"projects": {"edges": [
            {"node": {"id": "p1", "name": "worthlane-api"}},
        ]}}}
        with patch.dict(os.environ, {"RAILWAY_TOKEN": "tok"}, clear=True), \
                patch.object(railway_helpers.requests, "post",
                             MagicMock(return_value=FakeResp(200, payload))):
            projects_list, err = railway_helpers.list_projects()
        self.assertIsNone(err)
        self.assertEqual(projects_list[0]["name"], "worthlane-api")

    def test_list_variables_returns_names_only(self):
        payload = {"data": {"variables": {"EXPO_PUBLIC_API_URL": "https://api.x", "SECRET_KEY": "s3cr3t"}}}
        with patch.dict(os.environ, {"RAILWAY_TOKEN": "tok"}, clear=True), \
                patch.object(railway_helpers.requests, "post",
                             MagicMock(return_value=FakeResp(200, payload))):
            names, err = railway_helpers.list_variables("p1", "e1", "s1")
        self.assertIsNone(err)
        self.assertEqual(names, ["EXPO_PUBLIC_API_URL", "SECRET_KEY"])  # sorted names, no values

    def test_get_variable_returns_one_value(self):
        payload = {"data": {"variables": {"EXPO_PUBLIC_API_URL": "https://api.x"}}}
        with patch.dict(os.environ, {"RAILWAY_TOKEN": "tok"}, clear=True), \
                patch.object(railway_helpers.requests, "post",
                             MagicMock(return_value=FakeResp(200, payload))):
            value, err = railway_helpers.get_variable("p1", "e1", "EXPO_PUBLIC_API_URL", "s1")
        self.assertIsNone(err)
        self.assertEqual(value, "https://api.x")

    def test_set_variable_builds_upsert_mutation(self):
        payload = {"data": {"variableUpsert": True}}
        mock_post = MagicMock(return_value=FakeResp(200, payload))
        with patch.dict(os.environ, {"RAILWAY_TOKEN": "tok"}, clear=True), \
                patch.object(railway_helpers.requests, "post", mock_post):
            ok, err = railway_helpers.set_variable("p1", "e1", "s1", "EXPO_PUBLIC_PLAID_ENABLED", "false")
        self.assertIsNone(err)
        self.assertTrue(ok)
        sent = mock_post.call_args.kwargs["json"]
        self.assertIn("variableUpsert", sent["query"])
        self.assertEqual(sent["variables"]["input"]["name"], "EXPO_PUBLIC_PLAID_ENABLED")
        self.assertEqual(sent["variables"]["input"]["value"], "false")
        self.assertEqual(sent["variables"]["input"]["serviceId"], "s1")  # service-scoped

    def test_set_variable_shared_omits_service_id(self):
        payload = {"data": {"variableUpsert": True}}
        mock_post = MagicMock(return_value=FakeResp(200, payload))
        with patch.dict(os.environ, {"RAILWAY_TOKEN": "tok"}, clear=True), \
                patch.object(railway_helpers.requests, "post", mock_post):
            ok, err = railway_helpers.set_variable("p1", "e1", None, "SHARED_VAR", "v")
        self.assertIsNone(err)
        self.assertTrue(ok)
        sent = mock_post.call_args.kwargs["json"]["variables"]["input"]
        self.assertNotIn("serviceId", sent)  # shared (environment-level) scope
        self.assertEqual(sent["environmentId"], "e1")

    def test_graphql_error_reported(self):
        with patch.dict(os.environ, {"RAILWAY_TOKEN": "tok"}, clear=True), \
                patch.object(railway_helpers.requests, "post",
                             MagicMock(return_value=FakeResp(200, {"errors": [{"message": "nope"}]}))):
            data, err = railway_helpers.list_projects()
        self.assertIsNone(data)
        self.assertIn("nope", err)


if __name__ == "__main__":
    unittest.main()

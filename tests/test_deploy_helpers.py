import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import deploy_helpers


class FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class DeployHelpersTests(unittest.TestCase):
    def test_not_configured_without_token(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(deploy_helpers.is_configured())
            data, err = deploy_helpers.deploy("site", "main", "preview")
        self.assertIsNone(data)
        self.assertIn("isn't configured", err)

    def test_deploy_resolves_project_then_posts_deployment(self):
        project_payload = {"id": "prj_1", "name": "landing",
                           "link": {"type": "github", "repoId": 42, "repo": "me/landing"}}
        deploy_payload = {"id": "dpl_9", "url": "landing-abc.vercel.app", "readyState": "QUEUED"}

        def fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            if method == "GET":
                return FakeResp(200, project_payload)
            return FakeResp(200, deploy_payload)  # POST /v13/deployments

        with patch.dict(os.environ, {"VERCEL_TOKEN": "tok"}, clear=True), \
                patch.object(deploy_helpers.requests, "request", side_effect=fake_request) as m:
            result, err = deploy_helpers.deploy("landing", "main", "preview")

        self.assertIsNone(err)
        self.assertEqual(result["url"], "https://landing-abc.vercel.app")
        self.assertEqual(result["target"], "preview")
        # The POST body carried the git source from the resolved project.
        post_call = m.call_args_list[-1]
        body = post_call.kwargs["json"]
        self.assertEqual(body["gitSource"], {"type": "github", "repoId": 42, "ref": "main"})
        self.assertNotIn("target", body)  # preview omits target

    def test_production_deploy_sets_target(self):
        project_payload = {"id": "prj_1", "name": "landing",
                           "link": {"type": "github", "repoId": 42}}
        deploy_payload = {"id": "dpl_9", "url": "landing.vercel.app", "readyState": "QUEUED"}

        def fake_request(method, url, headers=None, json=None, params=None, timeout=None):
            return FakeResp(200, project_payload if method == "GET" else deploy_payload)

        with patch.dict(os.environ, {"VERCEL_TOKEN": "tok"}, clear=True), \
                patch.object(deploy_helpers.requests, "request", side_effect=fake_request) as m:
            result, err = deploy_helpers.deploy("landing", "main", "production")

        self.assertIsNone(err)
        self.assertEqual(m.call_args_list[-1].kwargs["json"]["target"], "production")

    def test_deploy_errors_when_project_not_git_linked(self):
        project_payload = {"id": "prj_1", "name": "landing"}  # no link
        with patch.dict(os.environ, {"VERCEL_TOKEN": "tok"}, clear=True), \
                patch.object(deploy_helpers.requests, "request",
                             MagicMock(return_value=FakeResp(200, project_payload))):
            result, err = deploy_helpers.deploy("landing", "main", "preview")
        self.assertIsNone(result)
        self.assertIn("isn't connected to a Git repo", err)

    def test_team_id_added_to_params(self):
        with patch.dict(os.environ, {"VERCEL_TOKEN": "tok", "VERCEL_TEAM_ID": "team_x"}, clear=True), \
                patch.object(deploy_helpers.requests, "request",
                             MagicMock(return_value=FakeResp(200, {"projects": []}))) as m:
            deploy_helpers.list_projects()
        self.assertEqual(m.call_args.kwargs["params"]["teamId"], "team_x")

    def test_list_projects_parses(self):
        payload = {"projects": [{"id": "p1", "name": "landing", "link": {"repo": "me/landing"}}]}
        with patch.dict(os.environ, {"VERCEL_TOKEN": "tok"}, clear=True), \
                patch.object(deploy_helpers.requests, "request",
                             MagicMock(return_value=FakeResp(200, payload))):
            projects_list, err = deploy_helpers.list_projects()
        self.assertIsNone(err)
        self.assertEqual(projects_list[0]["name"], "landing")


if __name__ == "__main__":
    unittest.main()

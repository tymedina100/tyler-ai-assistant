import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import office_api
import office_state


class OfficeAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"DATA_DIR": self.temp.name}, clear=False)
        self.env.start()
        office_state.configure_agents({"manager": {"name": "Miles", "role": "Manager"}})
        self.server = office_api.build_server("127.0.0.1", 0, "office-test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}{office_api.OFFICE_STATE_PATH}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.env.stop()
        self.temp.cleanup()

    def request(self, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        return urlopen(Request(self.url, headers=headers), timeout=3)

    def test_state_endpoint_requires_a_bearer_token(self):
        with self.assertRaises(HTTPError) as missing:
            self.request()
        self.assertEqual(missing.exception.code, 401)

        with self.assertRaises(HTTPError) as incorrect:
            self.request("wrong-token")
        self.assertEqual(incorrect.exception.code, 403)

    def test_authorized_request_returns_office_state(self):
        with self.request("office-test-token") as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["agents"]["manager"]["name"], "Miles")

    def test_unknown_path_returns_not_found(self):
        url = self.url.rsplit("/", 1)[0] + "/not-found"
        with self.assertRaises(HTTPError) as result:
            urlopen(url, timeout=3)
        self.assertEqual(result.exception.code, 404)


if __name__ == "__main__":
    unittest.main()

import io
import os
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from tyleros_worker import RejectRuntimeRedirects, request_json


class RuntimeTransportTests(unittest.TestCase):
    def test_bypass_is_header_only_and_runtime_identity_retained(self):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"job":null}'
        with patch.dict(os.environ, {"TYLEROS_URL": "https://example.vercel.app", "TYLEROS_VERCEL_PROTECTION_BYPASS": "synthetic-bypass", "TYLEROS_RUNTIME_CREDENTIAL": "synthetic-instance"}), patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertEqual(request_json("GET", "https://example.vercel.app/api/runtime/jobs/next", "synthetic-instance"), {"job": None})
            req = opener.return_value.open.call_args.args[0]
            self.assertEqual(req.get_header("X-vercel-protection-bypass"), "synthetic-bypass")
            self.assertEqual(req.get_header("Authorization"), "Bearer synthetic-instance")
            self.assertEqual(req.get_header("X-tyleros-role"), "miles")
            self.assertNotIn("synthetic-bypass", req.full_url)

    def test_bypass_refuses_wrong_origin_before_network(self):
        with patch.dict(os.environ, {"TYLEROS_URL": "https://example.vercel.app", "TYLEROS_VERCEL_PROTECTION_BYPASS": "synthetic-bypass"}), patch("urllib.request.build_opener") as opener:
            with self.assertRaisesRegex(RuntimeError, "configured HTTPS"):
                request_json("GET", "https://other.example/api/runtime/jobs/next", "fixture")
            opener.assert_not_called()

    def test_error_body_secrets_are_never_reported(self):
        error = urllib.error.HTTPError("https://example.com", 401, "Unauthorized", {}, io.BytesIO(b"synthetic-secret"))
        with patch.dict(os.environ, {}, clear=True), patch("urllib.request.build_opener") as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaisesRegex(RuntimeError, "^Runtime request failed: HTTP 401$"):
                request_json("GET", "https://example.com/api/runtime/jobs/next", "fixture")

    def test_redirect_never_forwards_credentials(self):
        with self.assertRaisesRegex(RuntimeError, "redirected"):
            RejectRuntimeRedirects().redirect_request(None, None, 302, "", {}, "https://other.example")


if __name__ == "__main__":
    unittest.main()

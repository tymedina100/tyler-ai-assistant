"""Authenticated Railway-facing API for the native Virtual Office desktop app."""
import hmac
import json
import os
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import office_state


OFFICE_STATE_PATH = "/api/office-state"


class OfficeAPIRequestHandler(BaseHTTPRequestHandler):
    """Read-only endpoint; the configured token is set by ``build_server``."""

    api_token = ""

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _is_authorized(self):
        authorization = self.headers.get("Authorization", "")
        expected = f"Bearer {self.api_token}"
        return hmac.compare_digest(authorization, expected)

    def do_GET(self):
        if self.path.split("?", 1)[0] != OFFICE_STATE_PATH:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        if not self.headers.get("Authorization"):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Missing office API token"})
            return
        if not self._is_authorized():
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "Invalid office API token"})
            return
        self._send_json(HTTPStatus.OK, office_state.get_state())

    def log_message(self, format, *args):
        """Do not log bearer-authenticated polling URLs or response bodies."""
        return


def build_server(host, port, api_token):
    """Create (but do not start) a token-protected threaded office API server."""
    if not api_token:
        raise ValueError("OFFICE_API_TOKEN is required to start the office API.")

    class ConfiguredOfficeHandler(OfficeAPIRequestHandler):
        pass

    ConfiguredOfficeHandler.api_token = api_token
    server = ThreadingHTTPServer((host, port), ConfiguredOfficeHandler)
    server.daemon_threads = True
    return server


def start_server(api_token, host=None, port=None):
    """Start the API in a daemon thread and return the server for later shutdown."""
    host = host or os.environ.get("OFFICE_API_HOST", "0.0.0.0")
    if port is None:
        try:
            port = int(os.environ.get("PORT", "8080"))
        except ValueError as e:
            raise ValueError("PORT must be an integer for the office API.") from e

    server = build_server(host, port, api_token)
    threading.Thread(target=server.serve_forever, name="office-api", daemon=True).start()
    return server

"""Loopback-only web server for the Tyler AI Assistant virtual office."""
import argparse
import json
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import office_state


OFFICE_DIR = Path(__file__).resolve().parent / "office"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


class OfficeRequestHandler(SimpleHTTPRequestHandler):
    """Serve static office assets plus a tiny read-only state endpoint."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(OFFICE_DIR), **kwargs)

    def do_GET(self):
        if self.path.split("?", 1)[0] == "/api/office-state":
            payload = json.dumps(office_state.get_state(), ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            return
        super().do_GET()

    def log_message(self, format, *args):
        """Keep frequent browser polling out of the terminal."""
        if not any("/api/office-state" in str(arg) for arg in args):
            super().log_message(format, *args)


def main():
    parser = argparse.ArgumentParser(description="Run the local Tyler AI Assistant virtual office.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Loopback port (default: {DEFAULT_PORT})")
    args = parser.parse_args()

    server = ThreadingHTTPServer((DEFAULT_HOST, args.port), OfficeRequestHandler)
    print(f"Virtual office: http://{DEFAULT_HOST}:{args.port}")
    print("Reading office_state.json from the configured DATA_DIR. Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nVirtual office stopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

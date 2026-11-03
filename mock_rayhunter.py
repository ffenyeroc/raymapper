#!/usr/bin/env python3
"""
mock_rayhunter.py — a tiny fake of the Rayhunter REST API for local testing.

Run it on 127.0.0.1:8080, then run the collector with --mock (or set
`rayhunter_host = "127.0.0.1"` in config.toml). It implements just enough of
the API for the collector to poll and build records.

    python3 mock_rayhunter.py [--port 8080]
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests  # noqa: F401  (kept for parity with the real setup)

GPS = {"latitude": 34.0522, "longitude": -118.2437}
CONFIG = {
    "qmdl_store_path": "/data/qmdl",
    "port": 8080,
    "gps_mode": "Api",
    "wifi_enabled": True,
    "ntfy_url": None,
}


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/time":
            self._send({"epoch": 1234567890, "device_time": "2026-01-01T00:00:00Z"})
        elif self.path == "/api/config":
            self._send(CONFIG)
        elif self.path == "/api/gps":
            self._send(GPS)
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/api/gps":
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            GPS.update({k: data[k] for k in ("latitude", "longitude") if k in data})
            self._send({"status": "ok"})
        else:
            self._send({"error": "not found"}, 404)

    def log_message(self, fmt, *args):  # quiet the default stderr logging
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock Rayhunter listening on 127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

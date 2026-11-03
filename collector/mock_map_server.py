#!/usr/bin/env python3
"""
mock_map_server.py — a tiny receiver that prints and records the gzipped JSON
batches sent by the collector. Use it to verify the upload half of the loop.

    python3 mock_map_server.py [--port 9000]

Then point `server_url = "http://127.0.0.1:9000/ingest"` in config.toml.
"""

from __future__ import annotations

import argparse
import gzip
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def _payload_path(self):
        # assume the body is gzip (we set Content-Encoding: gzip on upload)
        return None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        if self.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        try:
            batch = json.loads(raw)
        except json.JSONDecodeError:
            batch = {"_error": "could not parse"}

        with open("/tmp/raymap_ingest.jsonl", "a") as fh:
            fh.write(json.dumps(batch) + "\n")

        print(f"INGEST {self.path} -> device={batch.get('device')} "
              f"count={batch.get('count')} records={len(batch.get('records', []))}")

        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"mock map server listening on 127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

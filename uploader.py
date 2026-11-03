"""
Upload queued status records to the map server as a single gzipped JSON batch.
A simple on-disk queue (JSONL file) survives process restarts.
"""

from __future__ import annotations

import gzip
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("raymap")


class Uploader:
    """Accumulates status records, then uploads a gzipped batch on demand."""

    def __init__(
        self,
        server_url: str,
        state_file: str,
        device_id_hash: str,
    ):
        self.server_url = server_url.rstrip("/")
        self.state_file = Path(state_file)
        self.device_id_hash = device_id_hash
        self._backoff = 0  # current retry delay multiplier

    # -- Queue persistence -------------------------------------------------

    def _read_queue(self) -> List[Dict[str, Any]]:
        if not self.state_file.exists():
            return []
        records: List[Dict[str, Any]] = []
        try:
            with self.state_file.open("r") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            log.warning("corrupt queue file, starting fresh")
            self.state_file.unlink(missing_ok=True)
        return records

    def _write_queue(self, records: List[Dict[str, Any]]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        with self.state_file.open("w") as fh:
            for rec in records:
                fh.write(json.dumps(rec, separators=(",", ":")) + "\n")

    def enqueue(self, record: Dict[str, Any]) -> None:
        """Append a single record to the on-disk queue."""
        records = self._read_queue()
        records.append(record)
        self._write_queue(records)

    # -- Upload logic ------------------------------------------------------

    def _payload(self) -> Optional[bytes]:
        """Read queue, gzip a batch, return payload bytes, or None if empty."""
        records = self._read_queue()
        if not records:
            return None
        batch = {
            "device": self.device_id_hash,
            "count": len(records),
            "records": records,
        }
        return gzip.compress(json.dumps(batch, separators=(",", ":")).encode())

    def upload(self) -> bool:
        """Try to upload the queued records. Returns True on success."""
        payload = self._payload()
        if payload is None:
            return True  # nothing to upload

        if not self.server_url:
            # No server configured — treat dev mode as a sink: accept and
            # clear the queue so it doesn't grow unboundedly during testing.
            self._backoff = 0
            self.state_file.unlink(missing_ok=True)
            return True

        backoff_seconds = min(2 ** self._backoff, 300)
        try:
            resp = requests.post(
                self.server_url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Content-Encoding": "gzip",
                },
                timeout=15,
            )
            if resp.status_code == 200:
                self._backoff = 0
                self.state_file.unlink(missing_ok=True)
                log.info(
                    "uploaded %d record(s) (%.1f KB gzipped)",
                    len(json.loads(gzip.decompress(payload))["records"]),
                    len(payload) / 1024,
                )
                return True
            else:
                log.warning(
                    "upload returned %s, will retry (backoff=%ds)",
                    resp.status_code,
                    backoff_seconds,
                )
        except requests.RequestException as exc:
            log.warning("upload failed: %s (backoff=%ds)", exc, backoff_seconds)

        self._backoff += 1
        return False
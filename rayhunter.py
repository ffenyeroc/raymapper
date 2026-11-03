"""
Thin client for the Rayhunter REST API running locally on the USB-tethered
hotspot (port 8080 by default). Only reads the endpoints the collector needs
for status + GPS; unknown/broken endpoints are handled gracefully.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import requests


class RayhunterClient:
    def __init__(self, base_url: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def get_json(self, path: str) -> Optional[Dict[str, Any]]:
        """GET an endpoint and return parsed JSON, or None on any failure."""
        try:
            resp = self.session.get(
                f"{self.base_url}{path}", timeout=self.timeout
            )
            if resp.status_code != 200:
                return None
            return resp.json()
        except (requests.RequestException, ValueError):
            return None

    def post_json(self, path: str, payload: Dict[str, Any]) -> bool:
        try:
            resp = self.session.post(
                f"{self.base_url}{path}", json=payload, timeout=self.timeout
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False

    # --- Endpoint helpers -------------------------------------------------

    def get_time(self) -> Optional[Dict[str, Any]]:
        return self.get_json("/api/time")

    def get_config(self) -> Optional[Dict[str, Any]]:
        return self.get_json("/api/config")

    def get_gps(self) -> Optional[Dict[str, Any]]:
        """GET /api/gps -> {"latitude": float, "longitude": float} or None."""
        data = self.get_json("/api/gps")
        if data is not None and "latitude" in data and "longitude" in data:
            return data
        return None

    def post_gps(self, latitude: float, longitude: float) -> bool:
        """Push coordinates to the device (requires gps_mode=Api on device)."""
        return self.post_json("/api/gps", {"latitude": latitude, "longitude": longitude})

    def is_alive(self) -> bool:
        return self.get_json("/api/time") is not None

"""
Configuration loading for the raymap collector.

Uses the standard-library `tomllib` (Python 3.11+) to read a TOML config
file, merged over sane defaults, so the only third-party dependency in the
whole project is `requests`.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

# Defaults that are overridden by anything present in the config file.
DEFAULTS: dict = {
    # Where the collector POSTs its gzipped batches. Empty = never upload
    # (just log + queue locally), which is useful during development.
    "server_url": "",
    # Stable local identifier for this hotspot+computer pair. If empty, a
    # persistent random id is generated once and stored in the state file.
    "device_id": "",
    # How often (seconds) to poll the hotspot's local Rayhunter API over USB.
    "poll_interval_s": 60,
    # Maximum time (seconds) between uploads to the map server.
    "upload_interval_s": 900,
    # Upload immediately whenever the polled status changes, even if the
    # upload interval hasn't elapsed yet.
    "upload_on_change": True,
    # Where the map coordinates come from:
    #   "fixed"  -> use gps_fixed_latitude/longitude below (and push them
    #               to the hotspot via POST /api/gps)
    #   "api"    -> read whatever is currently stored on the device
    #   "disabled" -> no location
    "gps_source": "fixed",
    "gps_fixed_latitude": 37.7749,
    "gps_fixed_longitude": -122.4194,
    # The Rayhunter daemon's listening port (default 8080).
    "rayhunter_port": 8080,
    # Optional explicit host to talk to, bypassing USB discovery. Useful for
    # testing against mock_rayhunter.py (e.g. "127.0.0.1").
    "rayhunter_host": "",
    # Paths (defaults are relative to the config file's directory).
    "state_file": "state.jsonl",
    "log_file": "raymap-collector.log",
}


def _default_state_dir() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "raymap"
    else:
        base = Path.home() / ".raymap"
    base.mkdir(parents=True, exist_ok=True)
    return base


def resolve_config(config_path: Path) -> dict:
    """Return the effective configuration dict, using defaults + config file."""
    cfg = dict(DEFAULTS)

    if config_path.exists():
        with config_path.open("rb") as fh:
            file_cfg = tomllib.load(fh)
        # Only copy keys we know about (ignore unknown keys silently).
        for key in file_cfg:
            if key in cfg:
                cfg[key] = file_cfg[key]

    # Resolve relative file paths against the config file's directory so the
    # config can live anywhere while state/logs are written next to it.
    state_file = cfg["state_file"]
    if not Path(state_file).is_absolute():
        cfg["state_file"] = str(config_path.parent / state_file)
    log_file = cfg["log_file"]
    if not Path(log_file).is_absolute():
        cfg["log_file"] = str(config_path.parent / log_file)

    return cfg

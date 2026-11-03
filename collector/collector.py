#!/usr/bin/env python3
"""
raymap-collector  — macOS USB collector for Rayhunter-powered hotspots.

Polls the Rayhunter daemon on a USB-tethered Orbic RC400L at a configurable
interval and uploads status (GPS, detections, device health) to a map server
as a gzipped JSON batch.

Usage (dev):
    ./collector.py --mock             # talk to mock_rayhunter.py
    ./collector.py --config foo.toml  # use a custom config file
    ./collector.py --once             # single poll + upload, then exit
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

import config as cfg
import discovery
import rayhunter
import uploader

log = logging.getLogger("raymap")
_shutdown = False


def _signal_handler(signum: int, _frame) -> None:
    global _shutdown
    log.info("caught signal %d, shutting down...", signum)
    _shutdown = True


def _setup_logging(log_path: str, verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path))
    except OSError as exc:
        log.warning("could not open log file %s: %s", log_path, exc)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def _device_id_hash(raw_id: str) -> str:
    """Stable, privacy-safe hash of the device id sent to the map server."""
    return hashlib.sha256(raw_id.encode()).hexdigest()[:16]


def _ensure_device_id(config: dict) -> str:
    """Return a stable device id, auto-generating if needed and persisting."""
    raw = config.get("device_id", "")
    if raw:
        return raw
    # Auto-generate and persist next to the config file.
    store_dir = Path(config.get("state_file", "state.jsonl")).parent
    id_file = store_dir / ".device_id"
    if id_file.exists():
        raw = id_file.read_text().strip()
    if not raw:
        import uuid
        raw = str(uuid.uuid4())
        id_file.parent.mkdir(parents=True, exist_ok=True)
        id_file.write_text(raw + "\n")
    return raw


def build_record(
    client: rayhunter.RayhunterClient, config: dict
) -> Optional[Dict[str, Any]]:
    """Poll the Rayhunter API and compose a status record, or None on failure."""
    lat = config.get("gps_fixed_latitude")
    lon = config.get("gps_fixed_longitude")
    gps_source = config.get("gps_source", "fixed")
    if gps_source == "api":
        gps = client.get_gps()
        if gps:
            lat, lon = gps["latitude"], gps["longitude"]
    if gps_source == "fixed" and lat is not None and lon is not None:
        client.post_gps(lat, lon)

    return {
        "ts": int(time.time()),
        "lat": lat,
        "lon": lon,
        "gps_source": gps_source,
        "rssi_dbm": None,       # TODO: add confirmed endpoint
        "detection": None,      # TODO: add analysis/event summary
        "network": None,        # TODO: add from config/status
    }
def status_key(record: Optional[Dict[str, Any]]):
    """Fields that count as a status change for upload_on_change."""
    if not record:
        return None
    return (record.get("detection"), record.get("lat"), record.get("lon"))


def should_upload(now, last_upload_ts, interval, changed, upload_on_change):
    """Upload when the interval elapsed or (optionally) the status changed."""
    due = (now - last_upload_ts) >= interval
    return bool(due or (upload_on_change and changed))


def poll_once(
    client: rayhunter.RayhunterClient,
    up: uploader.Uploader,
    config: dict,
) -> tuple[bool, Optional[Dict[str, Any]]]:
    """Poll the device and queue a record. Returns (device_alive, record)."""
    if not client.is_alive():
        log.debug("device not reachable")
        return False, None

    record = build_record(client, config)
    if record is None:
        return False, None

    up.enqueue(record)
    # Never log coordinates: the server stores a cell, and so should the log.
    log.debug(
        "polled device: detection=%s gps_source=%s",
        record.get("detection"),
        record.get("gps_source"),
    )
    return True, record


def _sleep(seconds: float) -> None:
    """Sleep in 1s slices so SIGINT/SIGTERM stop promptly."""
    deadline = time.monotonic() + seconds
    while not _shutdown:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))


def run_loop(config: dict) -> None:
    """Main loop: discover, poll, upload, sleep."""
    device_id_raw = _ensure_device_id(config)
    device_id_hash = _device_id_hash(device_id_raw) if device_id_raw else "unknown"
    log.info("device id (hash sent upstream): %s", device_id_hash)

    up = uploader.Uploader(
        server_url=config.get("server_url", ""),
        state_file=config.get("state_file", ""),
        device_id_hash=device_id_hash,
    )

    explicit_host = config.get("rayhunter_host", "")
    client: Optional[rayhunter.RayhunterClient] = None
    device_ep: Optional[discovery.DeviceEndpoint] = None

    poll_interval = max(1, int(config.get("poll_interval_s", 60)))
    upload_interval = max(1, int(config.get("upload_interval_s", 900)))
    upload_on_change = bool(config.get("upload_on_change", True))
    last_upload_ts = 0.0
    last_key = None

    while not _shutdown:
        # -- Discovery / re-discovery -----------------------------------
        if explicit_host:
            base_url = f"http://{explicit_host}:{config.get('rayhunter_port', 8080)}"
            if client is None:
                client = rayhunter.RayhunterClient(base_url)
                log.info("explicit host: %s", base_url)
        else:
            if device_ep is None:
                device_ep = discovery.discover(config.get("rayhunter_port", 8080))
                if device_ep:
                    client = rayhunter.RayhunterClient(device_ep.base_url)
                    log.info(
                        "discovered device on %s (%s)",
                        device_ep.base_url,
                        device_ep.interface or "?",
                    )
                else:
                    log.info("no tethered device found yet — will retry")

        # -- Poll -------------------------------------------------------
        changed = False
        if client is not None:
            alive, record = poll_once(client, up, config)
            if not alive:
                log.info("device no longer reachable, will rediscover")
                client = None
                device_ep = None
            else:
                key = status_key(record)
                changed = last_key is not None and key != last_key
                last_key = key

        # -- Upload: interval elapsed, or status changed ------------------
        now = time.time()
        if should_upload(now, last_upload_ts, upload_interval, changed, upload_on_change):
            if not up.upload():
                log.info(
                    "upload failed; queued records kept, next attempt in %ss",
                    upload_interval,
                )
            last_upload_ts = now

        _sleep(poll_interval)

    # -- Shutdown flush -------------------------------------------------
    if client is not None:
        up.upload()
    log.info("collector stopped")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="raymap-collector — crowdsource Rayhunter hotspot status"
    )
    parser.add_argument(
        "--config",
        default=None,
        help="path to config.toml (default: ./config.toml)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="single poll + upload cycle, then exit",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="use 127.0.0.1:8080 (for testing with mock_rayhunter.py)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    # Resolve config path ---------------------------------------------------
    config_path = Path(args.config) if args.config else Path.cwd() / "config.toml"
    if not config_path.exists():
        config_path = Path(__file__).parent / "config.toml"

    conf = cfg.resolve_config(config_path)
    conf["__config_path"] = str(config_path)

    if args.mock:
        conf["rayhunter_host"] = "127.0.0.1"

    _setup_logging(conf.get("log_file", "raymap-collector.log"), args.verbose)
    log.info("started (config: %s)", config_path)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    if args.once:
        exit_code = 0
        device_id_raw = _ensure_device_id(conf)
        device_id_hash = _device_id_hash(device_id_raw) if device_id_raw else "unknown"
        up = uploader.Uploader(
            server_url=conf.get("server_url", ""),
            state_file=conf.get("state_file", ""),
            device_id_hash=device_id_hash,
        )
        explicit_host = conf.get("rayhunter_host", "")
        if explicit_host:
            client = rayhunter.RayhunterClient(
                f"http://{explicit_host}:{conf.get('rayhunter_port', 8080)}"
            )
        else:
            ep = discovery.discover(conf.get("rayhunter_port", 8080))
            if not ep:
                print("No tethered device found. Try --mock for testing.")
                sys.exit(1)
            client = rayhunter.RayhunterClient(ep.base_url)
            log.info("discovered device at %s", ep.base_url)

        alive, _record = poll_once(client, up, conf)
        if not alive:
            log.warning("poll cycle completed but device was unreachable")
            exit_code = 2
        elif not up.upload():
            log.warning("upload failed; record stayed queued in %s", conf.get("state_file"))
            exit_code = 2
        sys.exit(exit_code)
    else:
        run_loop(conf)


if __name__ == "__main__":
    main()
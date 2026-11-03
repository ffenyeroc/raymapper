"""
USB-tether device discovery (macOS).

Finds the Rayhunter daemon running on a USB-tethered hotspot by:
  1. Enumerating active network interfaces and their IPv4 addresses.
  2. Collecting candidate gateway IPs (the tethered device is the gateway of
     the subnet the tether interface presents, e.g. 192.168.42.1).
  3. Probing each candidate on the Rayhunter port and confirming the reply is
     the device by checking for a known JSON key in GET /api/config.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from typing import List, Optional

import requests

# Common gateway IPs used by MiFi-style hotspots when USB tethered.
COMMON_GATEWAYS = ["192.168.42.1", "192.168.1.1"]


@dataclass
class DeviceEndpoint:
    interface: str
    local_ip: str
    gateway_ip: str
    port: int

    @property
    def base_url(self) -> str:
        return f"http://{self.gateway_ip}:{self.port}"


def _run(cmd: List[str]) -> str:
    """Run a shell command and return its stdout, or '' on failure."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=5, check=False
        )
        return proc.stdout or ""
    except (subprocess.SubprocessError, OSError):
        return ""


def _active_ipv4_interfaces() -> List[tuple]:
    """Return [(interface, [ipv4, ...]), ...] for interfaces with an IPv4 addr."""
    out = _run(["ifconfig", "-a"])
    interfaces: List[tuple] = []
    current: Optional[str] = None
    for line in out.splitlines():
        m = re.match(r"^([A-Za-z0-9._-]+):", line)
        if m:
            current = m.group(1)
            interfaces.append((current, []))
            continue
        if current is None:
            continue
        m = re.search(r"\binet (\d+\.\d+\.\d+\.\d+)", line)
        if m:
            interfaces[-1][1].append(m.group(1))
    return [(name, addrs) for (name, addrs) in interfaces if addrs]


def _default_gateways() -> List[str]:
    """Return gateway IPs for default routes from netstat -rn."""
    out = _run(["netstat", "-rn", "-f", "inet"])
    gateways: List[str] = []
    for line in out.splitlines():
        # e.g. "default  192.168.1.1  UGScg ..."
        m = re.match(r"^default\s+(\d+\.\d+\.\d+\.\d+)", line)
        if m and m.group(1) not in gateways:
            gateways.append(m.group(1))
    return gateways


def _candidate_gateways() -> List[str]:
    """Combine default gateways, per-interface subnet gateways, and commons."""
    candidates: List[str] = []
    seen = set()

    for gw in COMMON_GATEWAYS:
        if gw not in seen:
            seen.add(gw)
            candidates.append(gw)

    for gw in _default_gateways():
        if gw not in seen:
            seen.add(gw)
            candidates.append(gw)

    # For each interface, guess the gateway at x.y.z.1 (a /24 tether subnet).
    for name, addrs in _active_ipv4_interfaces():
        for ip in addrs:
            prefix = ip.rsplit(".", 1)[0]
            for last in ("1", "2"):
                gw = f"{prefix}.{last}"
                if gw not in seen and gw != ip:
                    seen.add(gw)
                    candidates.append(gw)
    return candidates


def _looks_like_rayhunter(base_url: str, timeout: float = 1.5) -> bool:
    """True if the host answers GET /api/config as the Rayhunter daemon."""
    try:
        resp = requests.get(f"{base_url}/api/config", timeout=timeout)
        if resp.status_code != 200:
            return False
        # /api/config is the serialized Config struct; look for a distinctive key.
        return "qmdl_store_path" in resp.text or "gps_mode" in resp.text
    except requests.RequestException:
        return False


def discover(port: int = 8080) -> Optional[DeviceEndpoint]:
    """Return the first USB-tethered Rayhunter endpoint found, or None."""
    for gw in _candidate_gateways():
        if _looks_like_rayhunter(f"http://{gw}:{port}"):
            # Find the local interface that shares a subnet with this gateway.
            local_ip = ""
            interface = ""
            for name, addrs in _active_ipv4_interfaces():
                for ip in addrs:
                    if ip.rsplit(".", 1)[0] == gw.rsplit(".", 1)[0]:
                        local_ip, interface = ip, name
                        break
                if local_ip:
                    break
            return DeviceEndpoint(
                interface=interface,
                local_ip=local_ip,
                gateway_ip=gw,
                port=port,
            )
    return None

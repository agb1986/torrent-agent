"""VPN state detection.

Primary check is PIA's `piactl get connectionstate`. If piactl is missing or
unresponsive we fall back to looking for a VPN tunnel interface (tun*/wg*/pia*).
The check runs wherever this agent runs, which must be the same machine Deluge
binds to for it to be meaningful.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

_PIACTL_CANDIDATES = ("piactl", "/opt/piavpn/bin/piactl")
_TUNNEL_PREFIXES = ("tun", "wg", "pia")
# Public address used only as a routing probe; nothing is sent to it.
_ROUTE_PROBE_ADDR = "1.1.1.1"


@dataclass
class VpnStatus:
    active: bool
    ip: str | None
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {"active": self.active, "ip": self.ip, "detail": self.detail}


def _piactl_path() -> str | None:
    for cand in _PIACTL_CANDIDATES:
        found = shutil.which(cand) or (cand if Path(cand).is_file() else None)
        if found:
            return found
    return None


def _piactl_get(exe: str, field: str) -> str | None:
    try:
        proc = subprocess.run(
            [exe, "get", field],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    value = proc.stdout.strip()
    return value or None


def _tunnel_interfaces() -> list[str]:
    try:
        names = [p.name for p in Path("/sys/class/net").iterdir()]
    except OSError:
        return []
    return [n for n in names if n.startswith(_TUNNEL_PREFIXES)]


def tunnel_device() -> str | None:
    """Return the interface name currently carrying outbound traffic, if it's a VPN.

    Asks the kernel where traffic to a public address would actually go, which
    is stronger evidence than "a tun* interface exists" — PIA layers
    0.0.0.0/1 + 128.0.0.0/1 over the LAN default route, so the answer flips
    back to the LAN device the moment the tunnel drops.

    Returns the device *name* (tun0, wgpia0). Never an address: the tunnel IP
    changes on every reconnect, and piactl's `vpnip` is the public exit IP,
    which is not on any local interface.
    """
    try:
        proc = subprocess.run(
            ["ip", "route", "get", _ROUTE_PROBE_ADDR],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        proc = None

    if proc is not None and proc.returncode == 0:
        fields = proc.stdout.split()
        if "dev" in fields:
            device = fields[fields.index("dev") + 1]
            if device.startswith(_TUNNEL_PREFIXES):
                return device
            # Route resolved to a non-tunnel device — the VPN is not carrying
            # traffic, even if a stale tun* interface still exists.
            return None

    # `ip` unavailable: fall back to presence of a tunnel interface.
    tunnels = sorted(_tunnel_interfaces())
    return tunnels[0] if tunnels else None


def vpn_status(provider: str = "pia") -> VpnStatus:
    """Return the current VPN status.

    ``provider`` is accepted for forward compatibility; only "pia" has a
    dedicated path today. The interface fallback is provider-agnostic.
    """
    exe = _piactl_path()
    if provider == "pia" and exe:
        state = _piactl_get(exe, "connectionstate")
        if state is not None:
            ip = _piactl_get(exe, "vpnip") if state == "Connected" else None
            return VpnStatus(
                active=state == "Connected",
                ip=ip,
                detail=f"piactl connectionstate={state}",
            )

    # Fallback: a tunnel interface usually means a VPN is up.
    tunnels = _tunnel_interfaces()
    if tunnels:
        return VpnStatus(
            active=True,
            ip=None,
            detail=f"tunnel interface present: {', '.join(sorted(tunnels))}",
        )
    return VpnStatus(active=False, ip=None, detail="no VPN tunnel detected")

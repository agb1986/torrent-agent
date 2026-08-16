"""VPN state detection.

Two providers:

- ``pia`` — the host tunnel, whatever carries it. `piactl get connectionstate`
  when PIA is installed, otherwise the route check below, which is
  provider-agnostic: it covers ProtonVPN (proton0), WireGuard (wg*) and
  anything else that owns the default route. The name is historical; it means
  "the tunnel on this machine". Meaningful only when the agent runs on the same
  box Deluge binds to.
- ``gluetun`` — HTTP to the VPN container's control server. The tunnel lives
  inside a network namespace, so there is nothing on the host to inspect and
  piactl does not exist there.

Both fail closed: anything unknown reports inactive, because the caller uses
this to decide whether adding a torrent is safe.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_PIACTL_CANDIDATES = ("piactl", "/opt/piavpn/bin/piactl")
_TUNNEL_PREFIXES = ("tun", "wg", "pia", "proton")
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


# "pia" was the original name, from when PIA was the only host VPN supported.
# It has meant "the tunnel on this machine, whatever carries it" for a while;
# accepted forever so existing configs keep working.
_HOST_PROVIDERS = {"host", "pia"}


def normalise_provider(provider: str) -> str:
    """Fold legacy provider names onto current ones."""
    return "host" if str(provider or "").lower() in _HOST_PROVIDERS else str(provider)


def binding_is_structural(provider: str) -> bool:
    """True when containment comes from the runtime, not from Deluge's config.

    Under gluetun, Deluge shares the tunnel container's network namespace and
    has no other route out, so there is no `listen_interface` to check and
    nothing for scripts/bind_vpn.py to fix. Under PIA-on-the-host the binding
    is a real, separately-verifiable setting.
    """
    return normalise_provider(provider) == "gluetun"


def _gluetun_get(base_url: str, path: str, api_key: str) -> Any | None:
    """GET a control-server route, returning parsed JSON or None on any failure."""
    req = urllib.request.Request(base_url.rstrip("/") + path)
    if api_key:
        req.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        # Includes 401 from a missing/wrong key, which must NOT read as "up".
        return None


def _gluetun_status(config: dict[str, Any]) -> VpnStatus:
    base = str(config.get("control_url") or "http://127.0.0.1:8000")
    api_key = str(config.get("api_key") or "")

    status = _gluetun_get(base, "/v1/vpn/status", api_key)
    if status is None:
        return VpnStatus(
            active=False,
            ip=None,
            detail=(
                f"gluetun control server at {base} did not answer "
                f"/v1/vpn/status (down, unreachable, or api_key rejected)"
            ),
        )

    state = status.get("status") if isinstance(status, dict) else None
    if state != "running":
        return VpnStatus(
            active=False, ip=None, detail=f"gluetun vpn status={state!r}"
        )

    # Running: the exit IP is proof of which network we are on, and is the
    # gluetun equivalent of piactl's vpnip.
    ip = None
    pub = _gluetun_get(base, "/v1/publicip/ip", api_key)
    if isinstance(pub, dict):
        ip = pub.get("public_ip") or None
    return VpnStatus(active=True, ip=ip, detail="gluetun vpn status=running")


def gluetun_forwarded_port(config: dict[str, Any] | None = None) -> int | None:
    """The port gluetun negotiated with the provider, or None if unavailable.

    Proton's NAT-PMP lease rotates, so this is a moving target rather than a
    one-off setup value — see scripts/sync_pf_port.py. Returns None rather than
    0 on any doubt: writing a bogus port into Deluge is worse than leaving the
    old one, because it takes the daemon off a port that may still work.
    """
    config = config or {}
    base = str(config.get("control_url") or "http://127.0.0.1:8000")
    payload = _gluetun_get(base, "/v1/portforward", str(config.get("api_key") or ""))
    if not isinstance(payload, dict):
        return None

    candidate = payload.get("port")
    if not candidate:
        ports = payload.get("ports")
        candidate = ports[0] if isinstance(ports, list) and ports else None
    try:
        port = int(candidate)
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def vpn_status(
    provider: str = "host", config: dict[str, Any] | None = None
) -> VpnStatus:
    """Return the current VPN status for ``provider``.

    ``config`` is the ``[vpn]`` block; only the gluetun path needs it.
    """
    provider = normalise_provider(provider)
    if provider == "gluetun":
        return _gluetun_status(config or {})

    exe = _piactl_path()
    if provider == "host" and exe:
        state = _piactl_get(exe, "connectionstate")
        if state == "Connected":
            return VpnStatus(
                active=True,
                ip=_piactl_get(exe, "vpnip"),
                detail=f"piactl connectionstate={state}",
            )
        # Anything else means *PIA* is not carrying traffic — not that nothing
        # is. A machine can have PIA installed and idle while ProtonVPN holds
        # the tunnel, so fall through to the route check rather than reporting
        # inactive. Still fail-closed: only a positive answer below flips this
        # to active.

    # Fallback: ask which device is actually carrying traffic, not merely which
    # interfaces exist. A tun* left behind by a dead session still shows up in
    # /sys/class/net, so presence alone would report a leaking machine as safe —
    # the exact mistake scripts/bind_vpn.py exists to prevent.
    device = tunnel_device()
    if device:
        return VpnStatus(
            active=True,
            ip=None,
            detail=f"tunnel device carrying traffic: {device}",
        )
    return VpnStatus(active=False, ip=None, detail="no VPN tunnel carrying traffic")

"""Bind Deluge's peer traffic to the VPN tunnel device.

Without this, the VPN check is only a gate at add-time: PIA leaves the LAN
default route in place underneath its 0.0.0.0/1 + 128.0.0.0/1 split, so if the
tunnel drops mid-download traffic silently continues over the LAN interface.
Binding Deluge's sockets to the tunnel *device* makes them fail closed instead.

Always bind by interface NAME (tun0 / wgpia0), never by IP: the tunnel address
changes on every reconnect, and `piactl get vpnip` reports the public exit IP,
which is not on any local interface.

    python scripts/bind_vpn.py            # detect tunnel, apply binding
    python scripts/bind_vpn.py --check    # report only; exit 1 if not bound
    python scripts/bind_vpn.py -i wgpia0  # force a specific interface
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

# Allow running as a script from any directory.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from torrent_agent import deluge
from torrent_agent.config import load_config
from torrent_agent.deluge import (
    BINDING_KEYS as _KEYS,
    DelugeError,
    core_conf_path,
    deluge_binding,
)
from torrent_agent.vpn import tunnel_device


def _read_core_conf(path: Path) -> tuple[dict, dict]:
    """Parse Deluge's two-object config format: {header}{body}."""
    raw = path.read_text()
    decoder = json.JSONDecoder()
    header, idx = decoder.raw_decode(raw)
    body, _ = decoder.raw_decode(raw[idx:].lstrip())
    return header, body


def _write_core_conf(path: Path, header: dict, body: dict) -> None:
    """Rewrite core.conf, preserving Deluge's format. Backs up first."""
    shutil.copy2(path, path.with_suffix(path.suffix + ".prevpnbind"))
    text = json.dumps(header, indent=4, sort_keys=True) + json.dumps(
        body, indent=4, sort_keys=True
    )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)  # atomic


def _apply_via_rpc(config: dict, interface: str) -> bool:
    """Set the binding on a running daemon. False if it isn't running."""
    try:
        with deluge.connect(config) as client:
            client.call(
                "core.set_config", {key: interface for key in _KEYS}
            )
        return True
    except DelugeError:
        return False


def _apply_via_file(interface: str) -> None:
    path = core_conf_path()
    if not path.is_file():
        raise SystemExit(f"error: {path} not found — run deluged once first.")
    header, body = _read_core_conf(path)
    for key in _KEYS:
        body[key] = interface
    _write_core_conf(path, header, body)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report binding status without changing anything.",
    )
    parser.add_argument(
        "-i",
        "--interface",
        help="Tunnel device to bind to (default: auto-detect).",
    )
    args = parser.parse_args()

    config = load_config()
    device = args.interface or tunnel_device()
    bound = deluge_binding(config)

    if args.check:
        print(f"VPN tunnel device: {device or 'none detected'}")
        print(
            "Deluge bound to:   "
            + (
                bound["listen_interface"] or "(unbound)"
                if bound
                else "(cannot read config)"
            )
        )
        if bound is None:
            print("\nCould not read Deluge's binding.")
            return 1
        if device is None:
            print("\nNo VPN tunnel is up — cannot verify the binding matches.")
            return 1
        matched = all(bound[key] == device for key in _KEYS)
        print(
            f"\n{'OK' if matched else 'MISMATCH'}: Deluge is "
            + (
                f"bound to the live tunnel ({device})."
                if matched
                else f"not bound to {device} — downloads would leak if the VPN drops."
            )
        )
        return 0 if matched else 1

    if device is None:
        print(
            "error: no VPN tunnel interface detected — start the VPN first "
            "(scripts/start.sh), or pass --interface.",
            file=sys.stderr,
        )
        return 1

    if _apply_via_rpc(config, device):
        where = "running daemon (applied live)"
    else:
        _apply_via_file(device)
        where = f"{core_conf_path()} (daemon not running)"
    print(f"Bound Deluge to {device} via {where}.")
    print("  listen_interface  = " + device)
    print("  outgoing_interface = " + device)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

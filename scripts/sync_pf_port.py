"""Keep Deluge's listen port equal to gluetun's forwarded port.

Without this the two numbers never agree and there are **zero inbound peers**,
with nothing in any log to say so. gluetun negotiates a forwarded port with the
provider and firewalls exactly that port; Deluge, shipped with
``random_port: true``, picks its own on every start and listens there instead.
Traffic you initiate still works, which is why the fault reads as "the VPN is
slow" rather than as a misconfiguration.

Proton's NAT-PMP lease also *rotates*, so this is not a one-off fix. Run it
with ``--watch`` alongside the stack, or periodically.

    python scripts/sync_pf_port.py            # sync once
    python scripts/sync_pf_port.py --check    # report only; exit 1 on mismatch
    python scripts/sync_pf_port.py --watch    # resync as the lease rotates

Only meaningful for the gluetun provider — a host VPN has no control server to
ask, and PIA-on-the-host forwards its port through the client instead.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# Allow running as a script from any directory.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

from torrent_agent import deluge
from torrent_agent.config import load_config
from torrent_agent.deluge import DelugeError
from torrent_agent.vpn import gluetun_forwarded_port

# The lease outlives any sane poll interval; this is short enough to close the
# gap after a rotation without hammering the control server.
WATCH_INTERVAL_SECONDS = 300


def forwarded_port(config: dict) -> int | None:
    """The port gluetun currently has forwarded, or None."""
    return gluetun_forwarded_port(config.get("vpn", {}))


def deluge_state(config: dict) -> tuple[list[int], bool] | None:
    """Deluge's (listen_ports, random_port), or None if it cannot be read."""
    try:
        with deluge.connect(config) as client:
            values = client.call(
                "core.get_config_values", ["listen_ports", "random_port"]
            )
    except DelugeError:
        return None
    if not values:
        return None
    ports = values.get("listen_ports") or values.get(b"listen_ports") or []
    random = values.get("random_port")
    if random is None:
        random = values.get(b"random_port")
    return [int(p) for p in ports], bool(random)


def apply_port(config: dict, port: int) -> bool:
    """Point Deluge at ``port``. Returns True if anything actually changed.

    Turning off ``random_port`` is as important as the number itself: leaving
    it on means the next daemon start silently re-randomises and the mismatch
    comes back.
    """
    state = deluge_state(config)
    if state is not None:
        ports, random = state
        if ports == [port, port] and not random:
            return False

    with deluge.connect(config) as client:
        client.call(
            "core.set_config",
            {"random_port": False, "listen_ports": [port, port]},
        )
    return True


def _report(config: dict) -> tuple[int | None, tuple[list[int], bool] | None]:
    port = forwarded_port(config)
    state = deluge_state(config)
    print(f"gluetun forwarded port: {port if port else 'unavailable'}")
    if state is None:
        print("Deluge listen ports:    (cannot read — daemon down?)")
    else:
        ports, random = state
        print(
            f"Deluge listen ports:    {ports}"
            + (" (random_port ON — will drift on restart)" if random else "")
        )
    return port, state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="Report only; exit 1 if Deluge is not on the forwarded port.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Keep running, resyncing when the lease rotates.",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=WATCH_INTERVAL_SECONDS,
        help=f"Seconds between --watch polls (default {WATCH_INTERVAL_SECONDS}).",
    )
    args = parser.parse_args(argv)
    config = load_config()

    if args.check:
        port, state = _report(config)
        # Fail closed, like bind_vpn.py --check: "cannot tell" is not "fine".
        if port is None or state is None:
            print("\nCannot verify the port is forwarded to Deluge.")
            return 1
        ports, random = state
        matched = ports == [port, port] and not random
        print(
            f"\n{'OK' if matched else 'MISMATCH'}: Deluge is "
            + (
                f"listening on the forwarded port ({port})."
                if matched
                else f"not listening on {port} — no inbound peers will connect."
            )
        )
        return 0 if matched else 1

    if not args.watch:
        return _sync_once(config)

    print(f"Watching gluetun's forwarded port every {args.interval}s. Ctrl-C to stop.")
    while True:
        try:
            _sync_once(config)
        except DelugeError as exc:
            print(f"warning: {exc}", file=sys.stderr)
        time.sleep(args.interval)


def _sync_once(config: dict) -> int:
    port = forwarded_port(config)
    if port is None:
        print(
            "error: gluetun has no forwarded port (control server down, api_key "
            "rejected, or the server does not support port forwarding).",
            file=sys.stderr,
        )
        return 1
    try:
        changed = apply_port(config, port)
    except DelugeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Deluge listening on {port}."
        if changed
        else f"Already on {port} — nothing to do."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

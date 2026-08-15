"""Add torrents to Deluge over the daemon RPC (port 58846).

Requires `deluged` to be running. If username/password are not configured we
read the local daemon's auth file (~/.config/deluge/auth), so the default
localclient credentials work without extra setup.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from deluge_client import DelugeRPCClient


class DelugeError(RuntimeError):
    pass


BINDING_KEYS = ("listen_interface", "outgoing_interface")


def core_conf_path() -> Path:
    return Path.home() / ".config" / "deluge" / "core.conf"


def _binding_from_file() -> dict[str, str] | None:
    """Read the interface binding straight out of core.conf.

    The file is two concatenated JSON objects — a {"file","format"} header
    followed by the settings — so it can't be handed to json.load().
    """
    try:
        raw = core_conf_path().read_text()
    except OSError:
        return None
    decoder = json.JSONDecoder()
    try:
        _, idx = decoder.raw_decode(raw)
        body, _ = decoder.raw_decode(raw[idx:].lstrip())
    except ValueError:
        return None
    return {key: str(body.get(key, "")) for key in BINDING_KEYS}


def deluge_binding(config: dict[str, Any]) -> dict[str, str] | None:
    """What Deluge is currently bound to, or None if it can't be determined.

    A running daemon is authoritative — its in-memory config may differ from
    core.conf, which is only rewritten on shutdown. Falls back to the file
    when the daemon is down.
    """
    try:
        with connect(config) as client:
            values = client.call("core.get_config_values", list(BINDING_KEYS))
    except Exception:
        return _binding_from_file()

    out = {}
    for key in BINDING_KEYS:
        value = values.get(key.encode(), values.get(key, ""))
        out[key] = value.decode() if isinstance(value, bytes) else str(value)
    return out


def default_auth_path() -> Path:
    """Where the native, run-as-you deluged keeps its credentials."""
    return Path.home() / ".config" / "deluge" / "auth"


def _read_auth_file(auth_path: Path) -> tuple[str, str] | None:
    """Parse a Deluge auth file. Lines look like ``username:password:level``."""
    try:
        lines = auth_path.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        parts = line.strip().split(":")
        if len(parts) >= 2 and parts[0]:
            return parts[0], parts[1]
    return None


def _resolve_credentials(cfg: dict[str, Any]) -> tuple[str, str]:
    """Credentials for the configured daemon.

    Order: explicit config, then an explicitly configured auth_file, then the
    native daemon's ~/.config/deluge/auth.

    The last step is a convenience for the run-as-you daemon and a trap for
    anything else: a containerised Deluge has its OWN auth file, and silently
    using the host's produces a bare "Bad login" that gives no hint the wrong
    file was read. So set [deluge] auth_file (or username/password) whenever
    the daemon is not the native one.
    """
    username = cfg.get("username") or ""
    password = cfg.get("password") or ""
    if username and password:
        return username, password

    configured = cfg.get("auth_file") or ""
    if configured:
        path = Path(configured).expanduser()
        auth = _read_auth_file(path)
        if auth:
            return auth
        raise DelugeError(
            f"[deluge] auth_file is set to {path} but no credentials could be "
            f"read from it."
        )

    path = default_auth_path()
    auth = _read_auth_file(path)
    if auth:
        return auth
    raise DelugeError(
        f"No Deluge credentials: set username/password or auth_file under "
        f"[deluge] in your config, or ensure {path} exists (created when the "
        f"native deluged first runs)."
    )


@contextmanager
def connect(config: dict[str, Any]) -> Iterator[DelugeRPCClient]:
    """Yield a connected Deluge RPC client, disconnecting on exit."""
    cfg = config.get("deluge", {})
    username, password = _resolve_credentials(cfg)
    client = DelugeRPCClient(
        cfg.get("host", "127.0.0.1"),
        int(cfg.get("port", 58846)),
        username,
        password,
    )
    try:
        client.connect()
    except Exception as exc:  # deluge_client raises bare exceptions
        raise DelugeError(
            f"Could not connect to the Deluge daemon at "
            f"{cfg.get('host')}:{cfg.get('port')} ({exc}). Is `deluged` running "
            f"with the daemon RPC enabled?"
        ) from exc
    try:
        yield client
    finally:
        try:
            client.disconnect()
        except Exception:
            pass


def add_torrent(result_link: str, config: dict[str, Any]) -> str:
    """Add a magnet URI or torrent URL to Deluge. Returns the torrent id."""
    with connect(config) as client:
        try:
            if result_link.startswith("magnet:"):
                tid = client.call("core.add_torrent_magnet", result_link, {})
            else:
                tid = client.call("core.add_torrent_url", result_link, {})
        except Exception as exc:
            raise DelugeError(f"Deluge rejected the torrent: {exc}") from exc

    if tid is None:
        raise DelugeError(
            "Deluge returned no torrent id — it may already be in the session."
        )
    return tid.decode() if isinstance(tid, bytes) else str(tid)

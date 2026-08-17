"""Add torrents to Deluge over the daemon RPC (port 58846).

Requires `deluged` to be running. If username/password are not configured we
read the local daemon's auth file (~/.config/deluge/auth), so the default
localclient credentials work without extra setup.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from deluge_client import DelugeRPCClient

from . import security


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


# How long to wait for a freshly-added torrent's file list before giving up
# on the pre-download check. A .torrent URL add has this instantly — Deluge
# parses the file synchronously. A bare magnet needs to hear from DHT/peers
# first, which can take longer; timing out lets the add through rather than
# blocking on an inconclusive state (the post-download scan in
# torrent_agent.security is the backstop for whatever this misses).
_METADATA_TIMEOUT = 20.0
_METADATA_POLL = 0.5


def _wait_for_files(client: DelugeRPCClient, tid: str) -> list[dict] | None:
    """Poll for the torrent's file list. None if metadata never arrived in time."""
    deadline = time.monotonic() + _METADATA_TIMEOUT
    while time.monotonic() < deadline:
        status = client.call("core.get_torrent_status", tid, ["files"])
        files = status.get(b"files") or status.get("files")
        if files:
            return files
        time.sleep(_METADATA_POLL)
    return None


def add_torrent(result_link: str, config: dict[str, Any]) -> str:
    """Add a magnet URI or torrent URL to Deluge. Returns the torrent id.

    Before returning, waits briefly for the file list and refuses — removing
    what was just added, data included — if it contains an executable
    instead of media. See torrent_agent.security for why this exists and for
    the second check that runs once the download finishes.
    """
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
        tid = tid.decode() if isinstance(tid, bytes) else str(tid)

        files = _wait_for_files(client, tid)
        if files:
            bad = security.flag_dangerous_files(files)
            if bad:
                client.call("core.remove_torrent", tid, True)
                raise DelugeError(
                    "Blocked: release contains executable file(s) instead of "
                    f"media ({', '.join(bad[:5])}). Removed — nothing downloaded."
                )

    return tid


# --- listing ------------------------------------------------------------- #
# What both `scripts/status.py` and the Telegram bot's /status need. Kept here
# rather than in either caller so the two cannot drift into disagreeing about
# what "progress" means.
TORRENT_FIELDS = [
    "name",
    "state",
    "progress",
    "eta",
    "download_payload_rate",
    "total_wanted",
    "num_seeds",
    "total_peers",
    # Deluge's own notion of done, rather than progress >= 100.0. A float
    # comparison would also fire for a torrent that was already complete when
    # it was added, and for one still checking its existing data.
    "is_finished",
    # Where the files actually are — the automated pipeline needs the path,
    # not just the name, and it differs between hosts.
    "save_path",
]

# Active work first, so the interesting rows lead in any view.
_STATE_ORDER = {"Downloading": 0, "Checking": 1, "Queued": 2, "Seeding": 3, "Paused": 4}


def fmt_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def fmt_eta(seconds: int) -> str:
    if seconds <= 0:
        return "-"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _decode(value: Any) -> Any:
    return value.decode() if isinstance(value, bytes) else value


def list_torrents(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Every torrent in the session, sorted with active downloads first.

    Deluge's RPC hands back bytes for both keys and values, hence the decoding
    on the way out — callers should never have to think about it.
    """
    with connect(config) as client:
        try:
            torrents: dict = client.call("core.get_torrents_status", {}, TORRENT_FIELDS)
        except Exception as exc:
            raise DelugeError(f"Failed to list torrents: {exc}") from exc

    rows = []
    for tid, info in (torrents or {}).items():
        info = {_decode(k): v for k, v in info.items()}
        rows.append(
            {
                "id": _decode(tid),
                "name": _decode(info.get("name", "<unknown>")),
                "finished": bool(info.get("is_finished", False)),
                "save_path": _decode(info.get("save_path", "")),
                "state": _decode(info.get("state", "?")),
                "progress": float(info.get("progress", 0.0)),
                "eta": int(info.get("eta", 0)),
                "rate": float(info.get("download_payload_rate", 0)),
                "size": float(info.get("total_wanted", 0)),
                "seeds": int(info.get("num_seeds", 0)),
                "peers": int(info.get("total_peers", 0)),
            }
        )
    rows.sort(key=lambda r: (_STATE_ORDER.get(r["state"], 9), r["name"].lower()))
    return rows

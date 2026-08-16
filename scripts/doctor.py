"""Check whether this machine can actually run the stack, and say what is wrong.

Written from the failures that actually happened rather than from imagination.
Every check here corresponds to something that went wrong and cost real time,
and the pattern in all of them is the same: the component was fine, the wiring
was not, and nothing said so. Deluge listening on a port gluetun was not
forwarding. An API key silently overwritten with a different service's. A
container reporting paths that do not exist on the host. An ISP blocking
Telegram. None of those raise an error anywhere — they just quietly do
nothing useful.

    python scripts/doctor.py           # human-readable
    python scripts/doctor.py --json    # for scripting
    python scripts/doctor.py --quiet   # only problems

Exit 1 if anything failed, 0 otherwise. Warnings do not fail: a missing media
server or search backend is a legitimate setup.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from torrent_agent.config import load_config  # noqa: E402

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
_MARK = {OK: "ok  ", WARN: "warn", FAIL: "FAIL", SKIP: "--  "}
_TIMEOUT = 8


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    fix: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "", fix: str = "") -> Check:
        c = Check(name, status, detail, fix)
        self.checks.append(c)
        return c

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == FAIL]


# Local services must not be reached through a proxy the bot set for Telegram.
# Telegram must be, on networks that block it. So the choice is per call.
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http(
    url: str, headers: dict[str, str] | None = None, direct: bool = True
) -> tuple[int | None, str]:
    """GET a url, retrying once on a server error or a connection failure.

    ``direct`` bypasses any configured proxy, which is right for everything on
    the LAN or loopback. Telegram is the exception and passes direct=False.

    Not on 4xx: a rejected key is a real answer and retrying only slows the
    report down. But a single 500 usually means the service was busy, and a
    diagnostic that reports a transient blip as a failure gets ignored — which
    was demonstrated the first time this ran against a healthy Jellyfin.
    """
    req = urllib.request.Request(url, headers=headers or {})
    last: tuple[int | None, str] = (None, "")
    for attempt in (1, 2):
        try:
            opener = _DIRECT.open if direct else urllib.request.urlopen
            with opener(req, timeout=_TIMEOUT) as resp:
                return resp.status, ""
        except urllib.error.HTTPError as exc:
            last = (exc.code, str(exc))
            if exc.code < 500:
                return last
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = (None, str(exc))
        if attempt == 1:
            time.sleep(1)
    return last


# --- checks ---------------------------------------------------------------


def check_python(r: Report) -> None:
    v = sys.version_info
    try:
        import tomllib  # noqa: F401
        toml = "tomllib (stdlib)"
    except ModuleNotFoundError:
        try:
            import tomli  # noqa: F401
            toml = "tomli"
        except ModuleNotFoundError:
            r.add("python", FAIL, f"{v.major}.{v.minor}, no TOML parser",
                  "pip install -r requirements.txt (tomli is needed below 3.11)")
            return
    r.add("python", OK, f"{v.major}.{v.minor}.{v.micro}, {toml}")


def check_config(r: Report, config_path: str | None) -> dict[str, Any] | None:
    path = config_path or os.environ.get("TORRENT_AGENT_CONFIG") or "config.toml"
    if not (_REPO_ROOT / path).is_file() and not Path(path).is_file():
        r.add("config", WARN, f"{path} not found — using defaults",
              "cp config.example.toml config.toml")
    try:
        config = load_config(config_path)
    except Exception as exc:
        r.add("config", FAIL, f"could not parse: {exc}",
              "TOML rejects duplicate keys — check for a repeated setting")
        return None
    r.add("config", OK, f"loaded ({path})")
    return config


def check_api_key(r: Report) -> None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        r.add("anthropic key", OK, "set")
    else:
        r.add("anthropic key", FAIL, "ANTHROPIC_API_KEY is not set",
              "export it, or put it in .env.bot for the services. Note a bare "
              "'ANTHROPIC_API_KEY=' line there overwrites the shell's copy")


def check_vpn(r: Report, config: dict[str, Any]) -> None:
    from torrent_agent.vpn import normalise_provider, tunnel_device, vpn_status

    vpn_cfg = config.get("vpn", {})
    provider = normalise_provider(vpn_cfg.get("provider", "host"))
    status = vpn_status(provider, vpn_cfg)

    if status.active:
        r.add("vpn", OK, f"{provider}: {status.detail}"
                         + (f", exit {status.ip}" if status.ip else ""))
    else:
        r.add("vpn", FAIL, f"{provider}: {status.detail}",
              "adds are refused while the VPN is down — this is the gate working")

    if provider == "host":
        dev = tunnel_device()
        if dev:
            r.add("vpn binding", OK, f"tunnel device {dev}") if _binding_matches(
                config, dev) else r.add(
                "vpn binding", FAIL, f"Deluge is not bound to {dev}",
                "python scripts/bind_vpn.py — unbound, a VPN drop reroutes "
                "downloads over the LAN silently")
        else:
            r.add("vpn binding", SKIP, "no tunnel to compare against")
    else:
        r.add("vpn binding", SKIP, "structural: Deluge is inside the tunnel's namespace")


def _binding_matches(config: dict[str, Any], device: str) -> bool:
    from torrent_agent.deluge import BINDING_KEYS, deluge_binding

    bound = deluge_binding(config)
    return bool(bound) and all(bound.get(k) == device for k in BINDING_KEYS)


def check_deluge(r: Report, config: dict[str, Any]) -> None:
    from torrent_agent import deluge
    from torrent_agent.deluge import DelugeError

    cfg = config.get("deluge", {})
    where = f"{cfg.get('host')}:{cfg.get('port')}"
    try:
        with deluge.connect(config) as client:
            client.call("core.get_free_space")
        r.add("deluge", OK, f"reachable at {where}")
    except DelugeError as exc:
        r.add("deluge", FAIL, str(exc).split("(")[0].strip(),
              "is deluged running? a containerised daemon needs allow_remote=true "
              "and its own [deluge] auth_file")
        return
    except Exception as exc:
        r.add("deluge", FAIL, f"{where}: {exc}")
        return

    _check_download_path(r, config)


def _check_download_path(r: Report, config: dict[str, Any]) -> None:
    """Deluge's save path must exist from *this* process's point of view."""
    from server.pipeline import host_path
    from torrent_agent.deluge import list_torrents

    try:
        rows = list_torrents(config)
    except Exception:
        r.add("download path", SKIP, "could not list torrents")
        return
    if not rows:
        r.add("download path", SKIP, "no torrents to check against")
        return

    raw = rows[0].get("save_path") or ""
    resolved = host_path(raw, config)
    if Path(resolved).is_dir():
        detail = f"{resolved}" + (f"  (mapped from {raw})" if resolved != raw else "")
        r.add("download path", OK, detail)
    else:
        r.add("download path", FAIL, f"Deluge says {raw!r}, which is not a directory here",
              "a containerised Deluge reports its own paths — map them with "
              "[deluge.path_map], or the pipeline cannot find finished downloads")


def check_forwarded_port(r: Report, config: dict[str, Any]) -> None:
    from torrent_agent.vpn import gluetun_forwarded_port, normalise_provider

    if normalise_provider(config.get("vpn", {}).get("provider", "host")) != "gluetun":
        r.add("forwarded port", SKIP, "not a gluetun stack")
        return

    port = gluetun_forwarded_port(config.get("vpn", {}))
    if not port:
        r.add("forwarded port", WARN, "gluetun has no forwarded port",
              "without one you can download but never seed. Check the key "
              "supports port forwarding (Proton: NAT-PMP must be ticked)")
        return

    try:
        from torrent_agent.deluge import connect
        with connect(config) as client:
            values = client.call("core.get_config_values", ["listen_ports", "random_port"])
        ports = values.get(b"listen_ports") or values.get("listen_ports") or []
        random = values.get(b"random_port", values.get("random_port"))
    except Exception:
        r.add("forwarded port", SKIP, f"gluetun forwards {port}; Deluge unreadable")
        return

    if list(ports) == [port, port] and not random:
        r.add("forwarded port", OK, f"{port}, and Deluge is listening on it")
    else:
        r.add("forwarded port", FAIL,
              f"gluetun forwards {port}, Deluge listens on {list(ports)}"
              + (" with random_port on" if random else ""),
              "python scripts/sync_pf_port.py — mismatched means zero inbound "
              "peers, with nothing in any log to say so")


def check_search(r: Report, config: dict[str, Any]) -> None:
    prowlarr = config.get("search", {}).get("prowlarr", {})
    url, key = prowlarr.get("url"), prowlarr.get("api_key")
    if not url or not key:
        r.add("prowlarr", WARN, "not configured — falling back to apibay only",
              "you lose the good indexers; set [search.prowlarr] url and api_key")
        return

    code, err = _http(f"{url.rstrip('/')}/api/v1/system/status", {"X-Api-Key": key})
    if code == 200:
        n, enabled = _prowlarr_indexers(url, key)
        if enabled == 0:
            r.add("prowlarr", WARN, f"reachable, but 0 of {n} indexers enabled",
                  "add or enable indexers in Prowlarr")
        else:
            r.add("prowlarr", OK, f"reachable, {enabled}/{n} indexers enabled")
    elif code in (401, 403):
        r.add("prowlarr", FAIL, f"{url} rejected the API key (HTTP {code})",
              "check [search.prowlarr] api_key matches Prowlarr's")
    else:
        r.add("prowlarr", FAIL, f"{url} unreachable ({err or code})")


def _prowlarr_indexers(url: str, key: str) -> tuple[int, int]:
    req = urllib.request.Request(f"{url.rstrip('/')}/api/v1/indexer",
                                 headers={"X-Api-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            rows = json.loads(resp.read().decode())
        return len(rows), sum(1 for i in rows if i.get("enable"))
    except Exception:
        return 0, 0


def check_media_server(r: Report, config: dict[str, Any]) -> None:
    import transfer

    kind = transfer.media_server_kind()
    if kind == "none":
        r.add("media server", SKIP, "none configured")
        return
    jf = config.get("jellyfin", {})
    url = jf.get("url")
    key = os.environ.get("JELLYFIN_API_KEY") or jf.get("api_key")
    if not key:
        r.add("media server", WARN, f"{kind} at {url}, but no API key",
              "deliveries land but the library is never told to scan")
        return

    code, err = _http(f"{url.rstrip('/')}/System/Info", {"X-Emby-Token": key})
    if code == 200:
        r.add("media server", OK, f"{kind} at {url}, key accepted")
    elif code in (401, 403):
        r.add("media server", FAIL, f"{kind} rejected the API key (HTTP {code})",
              "a key for a different service will authenticate nowhere — check "
              "[jellyfin] api_key is the media server's, not Prowlarr's")
    else:
        r.add("media server", FAIL, f"{url} unreachable ({err or code})",
              "if this is a .local name, mDNS may be broken — see deploy/host/")


def check_delivery(r: Report, config: dict[str, Any]) -> None:
    import transfer

    dests = config.get("server", {}).get("destinations", {})
    if not dests:
        r.add("delivery", WARN, "no [server.destinations] configured",
              "tidied media has nowhere to go")
        return

    if transfer.server_is_local():
        missing = [f"{k}={v}" for k, v in dests.items() if not Path(v).is_dir()]
        if missing:
            r.add("delivery", FAIL, "local, but these do not exist: " + ", ".join(missing),
                  "create them, or point [server.destinations] elsewhere")
        else:
            r.add("delivery", OK, f"local move into {len(dests)} destination(s)")
        return

    host = config["server"].get("host")
    try:
        socket.getaddrinfo(host, None)
    except OSError as exc:
        r.add("delivery", FAIL, f"cannot resolve {host} ({exc})",
              "if it is a .local name, mDNS may be broken — see deploy/host/")
        return
    r.add("delivery", OK, f"remote: rsync to {config['server'].get('user')}@{host}")


def check_telegram(r: Report) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        r.add("telegram", SKIP, "no bot token in the environment")
        return
    # Deliberately proxied: on a network that blocks Telegram, going direct
    # would report a failure the bot itself does not have.
    code, err = _http(f"https://api.telegram.org/bot{token}/getMe", direct=False)
    if code == 200:
        allowed = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").strip()
        if not allowed:
            r.add("telegram", FAIL, "token works, but the allowlist is empty",
                  "the bot refuses to start rather than run open to anyone")
        else:
            r.add("telegram", OK, f"token accepted, {len(allowed.split())} chat id(s)")
    elif code in (401, 404):
        r.add("telegram", FAIL, "token rejected — revoked or mistyped",
              "get a new one from @BotFather")
    else:
        r.add("telegram", FAIL, f"api.telegram.org unreachable ({err or code})",
              "some ISPs block Telegram. Route it through the VPN: set "
              "HTTPS_PROXY to gluetun's proxy, plus NO_PROXY=127.0.0.1,localhost")


def check_tools(r: Report) -> None:
    missing = [t for t in ("rsync", "ssh", "ip") if not shutil.which(t)]
    if missing:
        r.add("tools", WARN, "not on PATH: " + ", ".join(missing),
              "rsync/ssh are only needed for a remote server; ip for the host VPN check")
    else:
        r.add("tools", OK, "rsync, ssh, ip present")


# --- driver ---------------------------------------------------------------


def run_checks(config_path: str | None = None) -> Report:
    r = Report()
    check_python(r)
    config = check_config(r, config_path)
    if config is None:
        return r
    check_api_key(r)
    check_tools(r)
    for fn in (check_vpn, check_deluge, check_forwarded_port,
               check_search, check_media_server, check_delivery):
        try:
            fn(r, config)
        except Exception as exc:  # a broken check must not hide the others
            r.add(fn.__name__.replace("check_", ""), FAIL, f"check itself failed: {exc}")
    check_telegram(r)
    return r


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("-c", "--config", default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--quiet", action="store_true", help="Only problems.")
    args = parser.parse_args(argv)

    report = run_checks(args.config)

    if args.json:
        print(json.dumps({"checks": [asdict(c) for c in report.checks],
                          "failed": len(report.failed)}, indent=2))
        return 1 if report.failed else 0

    width = max(len(c.name) for c in report.checks)
    for c in report.checks:
        if args.quiet and c.status in (OK, SKIP):
            continue
        print(f"[{_MARK[c.status]}] {c.name:<{width}}  {c.detail}")
        if c.fix and c.status in (WARN, FAIL):
            print(f"{'':>{width + 9}}↳ {c.fix}")

    if report.failed:
        print(f"\n{len(report.failed)} check(s) failed.")
        return 1
    print("\nAll good." if not args.quiet else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

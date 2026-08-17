"""Configuration loading: defaults <- config.toml <- environment."""

from __future__ import annotations

import os
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - depends on interpreter
    # The CasaOS server runs Debian's 3.9, where tomllib is not yet stdlib.
    # tomli is the same library, and is what tomllib was adopted from, so the
    # call sites need no branching beyond this import.
    import tomli as tomllib

from typing import Any

DEFAULTS: dict[str, Any] = {
    "anthropic": {"model": "claude-opus-5"},
    "search": {
        "backend": "auto",
        "prowlarr": {"url": "", "api_key": ""},
    },
    "deluge": {
        "host": "127.0.0.1",
        "port": 58846,
        "username": "",
        "password": "",
        # Empty means the native daemon's ~/.config/deluge/auth. Point this at
        # a containerised daemon's own auth file rather than letting it fall
        # back — the host's file would be read instead, and the only symptom
        # is an unexplained "Bad login".
        "auth_file": "",
    },
    # "host" is the tunnel on this machine, whatever carries it — piactl when
    # PIA is installed and connected, otherwise a route check that covers
    # ProtonVPN, WireGuard and the rest. "gluetun" queries the VPN container's
    # control server, the only option where the tunnel lives in a namespace
    # rather than on the box. "pia" is accepted as a legacy alias for "host".
    "vpn": {
        "provider": "host",
        "control_url": "http://127.0.0.1:8000",
        "api_key": "",
    },
    "preferences": {
        "resolutions": ["1080p", "2160p", "720p"],
        "prefer_codecs": ["x265", "hevc", "h265"],
        "max_episode_size_gb": 20,
        "min_seeders": 1,
    },
    # Where tidied media ends up. Empty host means "this machine": transfer.py
    # then moves files into destinations locally instead of rsyncing over SSH,
    # which is also the right behaviour when there is no separate server at
    # all. Fill these in via config.toml — shipping one person's hostname and
    # ssh key as defaults just sends a stranger's first transfer somewhere
    # that does not exist.
    "server": {
        "user": "",
        "host": "",
        "ssh_key": "",
        "destinations": {},
    },
    # Jellyfin (on the same server) is told to scan after each transfer.
    # path_map translates server paths into the paths the Jellyfin container
    # sees (its bind mounts). Paths with no mapping (books, manga) are skipped.
    # Told to scan after each delivery. Empty url means "no media server":
    # the scan is skipped rather than failing. path_map translates server
    # paths into the paths the media server's container sees — mappings are
    # per-install, so there is no sensible default.
    # "jellyfin", "emby" (same API), or "none". Empty means: infer from
    # whether a url is configured, so existing setups need no change.
    "media_server": {"kind": ""},
    "jellyfin": {
        "url": "",
        "api_key": "",
        "path_map": {},
    },
    # scripts/tmdb_id.py tags tidied media with its TMDB id. The key is
    # optional — without one it resolves ids through Wikidata instead.
    "tmdb": {"api_key": ""},
    # scripts/prune.py reclaims disk by removing torrents that have finished
    # seeding. Off by default, and deliberately: this is the only thing in the
    # repo that deletes a user's media, so it must be opted into rather than
    # arriving switched on with someone else's idea of a sensible ratio.
    "prune": {
        "enabled": False,
        # Do nothing at all while there is this much room. Pruning is for
        # reclaiming space, not for enforcing a seed policy — a half-empty
        # disk has no reason to lose anything.
        "min_free_gb": 200,
        # A torrent is only a candidate once it has met BOTH: seeded this
        # long, and reached this ratio. Either alone is a bad rule — a popular
        # release hits 2.0 in an hour, an unpopular one never does.
        "min_seed_hours": 72,
        "min_ratio": 1.0,
        # The files, not just the entry in Deluge. False frees nothing, which
        # makes the whole thing pointless, but is the safe first run.
        "delete_data": True,
    },
    # scripts/backup.py snapshots the small state that is expensive to lose:
    # the subscription list, and Deluge's own config and torrent list.
    "backup": {
        # Empty means <repo>/tmp/backups. Put it on a different disk than the
        # one it is backing up if you have one.
        "dir": "",
        "keep": 14,
        # Deluge's config directory. Empty derives it: the parent of
        # [deluge] auth_file when that is set (which is how a containerised
        # daemon is already pointed at), else ~/.config/deluge.
        "deluge_config_dir": "",
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _apply_env_overrides(config: dict[str, Any]) -> dict[str, Any]:
    """Overlay environment variables on top of the file config (env wins).

    Lets secrets like the Prowlarr API key stay out of config.toml.
    """
    override: dict[str, Any] = {
        "search": {"prowlarr": {}},
        "jellyfin": {},
        "tmdb": {},
        "vpn": {},
    }
    if key := os.environ.get("GLUETUN_API_KEY"):
        override["vpn"]["api_key"] = key
    if key := os.environ.get("PROWLARR_API_KEY"):
        override["search"]["prowlarr"]["api_key"] = key
    if url := os.environ.get("PROWLARR_URL"):
        override["search"]["prowlarr"]["url"] = url
    if key := os.environ.get("JELLYFIN_API_KEY"):
        override["jellyfin"]["api_key"] = key
    if key := os.environ.get("TMDB_API_KEY"):
        override["tmdb"]["api_key"] = key
    config = _deep_merge(config, override)

    # If a key is set but no URL anywhere, assume a local Prowlarr.
    prowlarr = config["search"]["prowlarr"]
    if prowlarr.get("api_key") and not prowlarr.get("url"):
        config = _deep_merge(
            config, {"search": {"prowlarr": {"url": "http://localhost:9696"}}}
        )
    return config


_REPO_ROOT = Path(__file__).resolve().parent.parent


def default_config_path() -> Path:
    """Which config file to load when the caller doesn't name one.

    Honours ``TORRENT_AGENT_CONFIG`` so a second stack (the containerised
    rehearsal, or the server) can be driven by the same scripts without
    swapping config.toml in and out. A relative value resolves against the
    repo root, not the cwd — the scripts are run from anywhere.
    """
    env = os.environ.get("TORRENT_AGENT_CONFIG")
    if not env:
        return _REPO_ROOT / "config.toml"
    p = Path(env).expanduser()
    return p if p.is_absolute() else _REPO_ROOT / p


def load_config(path: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Load config: defaults <- config file <- environment."""
    config = DEFAULTS
    p = Path(path) if path is not None else default_config_path()
    if p.is_file():
        with p.open("rb") as fh:
            config = _deep_merge(config, tomllib.load(fh))
    return _apply_env_overrides(config)


def anthropic_api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY")

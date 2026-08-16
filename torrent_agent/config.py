"""Configuration loading: defaults <- config.toml <- environment."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
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
    # provider "pia" uses piactl on this machine; "gluetun" queries the VPN
    # container's control server, which is the only option on a host where the
    # tunnel lives in a namespace rather than on the box.
    "vpn": {
        "provider": "pia",
        "control_url": "http://127.0.0.1:8000",
        "api_key": "",
    },
    "preferences": {
        "resolutions": ["1080p", "2160p", "720p"],
        "prefer_codecs": ["x265", "hevc", "h265"],
        "max_episode_size_gb": 20,
        "min_seeders": 1,
    },
    # The CASAOS media server that scripts/transfer.py pushes to.
    "server": {
        "user": "casaos",
        "host": "casaos.local",
        "ssh_key": "~/.ssh/id_rsa_ha",
        "destinations": {
            "film": "/mnt/data/film",
            "tv": "/mnt/data/tv",
            "book": "/media/local/books",
            "manga": "/media/local/manga",
        },
    },
    # Jellyfin (on the same server) is told to scan after each transfer.
    # path_map translates server paths into the paths the Jellyfin container
    # sees (its bind mounts). Paths with no mapping (books, manga) are skipped.
    "jellyfin": {
        "url": "http://casaos.local:8096",
        "api_key": "",
        "path_map": {
            "/mnt/data/tv": "/media/tv",
            "/mnt/data/film": "/media/movies",
        },
    },
    # scripts/tmdb_id.py tags tidied media with its TMDB id. The key is
    # optional — without one it resolves ids through Wikidata instead.
    "tmdb": {"api_key": ""},
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

"""Configuration loading: defaults <- config.toml <- environment."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "anthropic": {"model": "claude-opus-4-8"},
    "search": {
        "backend": "auto",
        "prowlarr": {"url": "", "api_key": ""},
    },
    "deluge": {
        "host": "127.0.0.1",
        "port": 58846,
        "username": "",
        "password": "",
    },
    "vpn": {"provider": "pia"},
    "preferences": {
        "resolutions": ["1080p", "2160p", "720p"],
        "prefer_codecs": ["x265", "hevc", "h265"],
        "max_episode_size_gb": 20,
        "min_seeders": 1,
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
    override: dict[str, Any] = {"search": {"prowlarr": {}}}
    if key := os.environ.get("PROWLARR_API_KEY"):
        override["search"]["prowlarr"]["api_key"] = key
    if url := os.environ.get("PROWLARR_URL"):
        override["search"]["prowlarr"]["url"] = url
    config = _deep_merge(config, override)

    # If a key is set but no URL anywhere, assume a local Prowlarr.
    prowlarr = config["search"]["prowlarr"]
    if prowlarr.get("api_key") and not prowlarr.get("url"):
        config = _deep_merge(
            config, {"search": {"prowlarr": {"url": "http://localhost:9696"}}}
        )
    return config


def load_config(path: str | os.PathLike[str] = "config.toml") -> dict[str, Any]:
    """Load config: defaults <- config.toml <- environment."""
    config = DEFAULTS
    p = Path(path)
    if p.is_file():
        with p.open("rb") as fh:
            config = _deep_merge(config, tomllib.load(fh))
    return _apply_env_overrides(config)


def anthropic_api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY")

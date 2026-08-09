"""Config: deep merge, env overrides, and defaults."""

from torrent_agent.config import DEFAULTS, _deep_merge, load_config


def test_deep_merge_nested_override():
    base = {"a": {"b": 1, "c": 2}, "d": 3}
    override = {"a": {"b": 9}}
    merged = _deep_merge(base, override)
    assert merged == {"a": {"b": 9, "c": 2}, "d": 3}
    assert base["a"]["b"] == 1  # base untouched


def test_defaults_when_no_config_file(tmp_path, monkeypatch):
    for var in ("PROWLARR_API_KEY", "PROWLARR_URL", "JELLYFIN_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    config = load_config(tmp_path / "missing.toml")
    assert config["anthropic"]["model"] == DEFAULTS["anthropic"]["model"]
    assert config["server"]["destinations"]["tv"] == "/mnt/data/tv"


def test_env_overrides_win(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[search.prowlarr]\nurl = "http://file:9696"\napi_key = "from-file"\n'
        '[jellyfin]\napi_key = "from-file"\n'
    )
    monkeypatch.setenv("PROWLARR_API_KEY", "from-env")
    monkeypatch.setenv("JELLYFIN_API_KEY", "jf-from-env")
    config = load_config(cfg)
    assert config["search"]["prowlarr"]["api_key"] == "from-env"
    assert config["search"]["prowlarr"]["url"] == "http://file:9696"
    assert config["jellyfin"]["api_key"] == "jf-from-env"


def test_prowlarr_url_assumed_local_when_key_only(tmp_path, monkeypatch):
    monkeypatch.delenv("PROWLARR_URL", raising=False)
    monkeypatch.setenv("PROWLARR_API_KEY", "some-key")
    config = load_config(tmp_path / "missing.toml")
    assert config["search"]["prowlarr"]["url"] == "http://localhost:9696"

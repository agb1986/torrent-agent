"""Deluge credential resolution.

The hazard these cover: the fallback reads the *native* daemon's auth file at
~/.config/deluge/auth. Against a containerised Deluge that is the wrong
daemon's credentials, and the resulting failure is a bare "Bad login" with
nothing pointing at the cause.
"""

import pytest

from torrent_agent import deluge


def _auth_file(tmp_path, contents):
    p = tmp_path / "auth"
    p.write_text(contents)
    return p


def test_explicit_credentials_win(tmp_path, monkeypatch):
    monkeypatch.setattr(deluge, "default_auth_path", lambda: _auth_file(tmp_path, "native:wrong:10\n"))
    creds = deluge._resolve_credentials({"username": "set", "password": "explicit"})
    assert creds == ("set", "explicit")


def test_auth_file_beats_the_native_fallback(tmp_path, monkeypatch):
    native = _auth_file(tmp_path, "native:wrongpw:10\n")
    container = tmp_path / "container_auth"
    container.write_text("localclient:rightpw:10\n")
    monkeypatch.setattr(deluge, "default_auth_path", lambda: native)

    creds = deluge._resolve_credentials({"auth_file": str(container)})
    assert creds == ("localclient", "rightpw")


def test_native_fallback_used_when_nothing_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deluge, "default_auth_path", lambda: _auth_file(tmp_path, "localclient:pw:10\n")
    )
    assert deluge._resolve_credentials({}) == ("localclient", "pw")


def test_configured_auth_file_that_is_missing_names_the_path(tmp_path):
    missing = tmp_path / "nope" / "auth"
    with pytest.raises(deluge.DelugeError) as exc:
        deluge._resolve_credentials({"auth_file": str(missing)})
    # The whole point is that the error says which file it tried.
    assert str(missing) in str(exc.value)


def test_no_credentials_anywhere_names_the_default_path(tmp_path, monkeypatch):
    monkeypatch.setattr(deluge, "default_auth_path", lambda: tmp_path / "absent")
    with pytest.raises(deluge.DelugeError) as exc:
        deluge._resolve_credentials({})
    assert "absent" in str(exc.value)


def test_comment_and_blank_lines_are_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deluge,
        "default_auth_path",
        lambda: _auth_file(tmp_path, "\n:onlycolons:\nlocalclient:pw:10\n"),
    )
    assert deluge._resolve_credentials({}) == ("localclient", "pw")

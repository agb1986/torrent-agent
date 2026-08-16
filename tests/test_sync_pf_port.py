"""Keeping Deluge's listen port equal to gluetun's forwarded port.

The bug this guards against is silent: gluetun negotiates a port and firewalls
exactly that one, while Deluge with ``random_port: true`` listens somewhere
else entirely. Nothing errors — there are simply never any inbound peers. So
the cases below care as much about *detecting* the mismatch as fixing it.
"""

import json
import urllib.error

import pytest

import sync_pf_port
from torrent_agent import vpn

CFG = {
    "vpn": {"control_url": "http://127.0.0.1:8000", "api_key": "testkey"},
    "deluge": {"host": "127.0.0.1", "port": 58846},
}


class _Resp:
    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode()
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _routes(mapping):
    def fake_urlopen(req, timeout=None):
        path = req.full_url.split("8000", 1)[1]
        if path not in mapping:
            raise urllib.error.HTTPError(req.full_url, 404, "nope", {}, None)
        value = mapping[path]
        if isinstance(value, Exception):
            raise value
        return _Resp(value)

    return fake_urlopen


class _FakeClient:
    """Records core.set_config calls; serves canned config values."""

    def __init__(self, values):
        self.values = dict(values)
        self.calls = []

    def call(self, method, *args):
        self.calls.append((method, args))
        if method == "core.get_config_values":
            return {k: self.values[k] for k in args[0]}
        if method == "core.set_config":
            self.values.update(args[0])
            return None
        raise AssertionError(f"unexpected RPC {method}")


@pytest.fixture
def fake_deluge(monkeypatch):
    """Patch deluge.connect to yield a recording client."""
    import contextlib

    holder = {}

    def _install(values):
        client = _FakeClient(values)
        holder["client"] = client

        @contextlib.contextmanager
        def fake_connect(config):
            yield client

        monkeypatch.setattr(sync_pf_port.deluge, "connect", fake_connect)
        return client

    return _install


# --- reading the forwarded port -------------------------------------------


def test_forwarded_port_read_from_control_server(monkeypatch):
    monkeypatch.setattr(
        vpn.urllib.request, "urlopen", _routes({"/v1/portforward": {"port": 48114}})
    )
    assert vpn.gluetun_forwarded_port(CFG["vpn"]) == 48114


def test_forwarded_port_none_when_control_server_down(monkeypatch):
    monkeypatch.setattr(vpn.urllib.request, "urlopen", _routes({}))
    assert vpn.gluetun_forwarded_port(CFG["vpn"]) is None


def test_forwarded_port_none_when_payload_malformed(monkeypatch):
    # A reply without a usable port must not be coerced into 0 and written to
    # Deluge — that would take the daemon off a working port for a broken one.
    monkeypatch.setattr(
        vpn.urllib.request, "urlopen", _routes({"/v1/portforward": {"port": 0}})
    )
    assert vpn.gluetun_forwarded_port(CFG["vpn"]) is None


def test_forwarded_port_falls_back_to_ports_list(monkeypatch):
    monkeypatch.setattr(
        vpn.urllib.request,
        "urlopen",
        _routes({"/v1/portforward": {"ports": [51820]}}),
    )
    assert vpn.gluetun_forwarded_port(CFG["vpn"]) == 51820


# --- applying it ----------------------------------------------------------


def test_sync_sets_listen_ports_and_disables_random(fake_deluge):
    client = fake_deluge({"listen_ports": [51765, 51765], "random_port": True})

    changed = sync_pf_port.apply_port(CFG, 48114)

    assert changed is True
    assert client.values["listen_ports"] == [48114, 48114]
    # random_port: true is the actual bug — leaving it on means Deluge picks a
    # fresh port on next start and the mismatch silently returns.
    assert client.values["random_port"] is False


def test_sync_is_a_noop_when_already_correct(fake_deluge):
    client = fake_deluge({"listen_ports": [48114, 48114], "random_port": False})

    assert sync_pf_port.apply_port(CFG, 48114) is False
    assert not [c for c in client.calls if c[0] == "core.set_config"]


def test_sync_still_fixes_random_port_when_number_matches(fake_deluge):
    # Right port, but random_port left on: still broken, because the next
    # restart moves it. Must be corrected rather than reported as fine.
    client = fake_deluge({"listen_ports": [48114, 48114], "random_port": True})

    assert sync_pf_port.apply_port(CFG, 48114) is True
    assert client.values["random_port"] is False


# --- check mode -----------------------------------------------------------


def test_check_reports_mismatch(monkeypatch, fake_deluge, capsys):
    fake_deluge({"listen_ports": [51765, 51765], "random_port": True})
    monkeypatch.setattr(sync_pf_port, "forwarded_port", lambda cfg: 48114)

    assert sync_pf_port.main(["--check"]) == 1
    assert "MISMATCH" in capsys.readouterr().out


def test_check_passes_when_aligned(monkeypatch, fake_deluge, capsys):
    fake_deluge({"listen_ports": [48114, 48114], "random_port": False})
    monkeypatch.setattr(sync_pf_port, "forwarded_port", lambda cfg: 48114)

    assert sync_pf_port.main(["--check"]) == 0
    assert "OK" in capsys.readouterr().out


def test_check_fails_closed_when_port_unknown(monkeypatch, fake_deluge, capsys):
    # No forwarded port available: report a problem rather than silently
    # passing, the same rule bind_vpn.py --check follows.
    fake_deluge({"listen_ports": [48114, 48114], "random_port": False})
    monkeypatch.setattr(sync_pf_port, "forwarded_port", lambda cfg: None)

    assert sync_pf_port.main(["--check"]) == 1

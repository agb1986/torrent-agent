"""gluetun provider: control-server status checks.

The governing rule for every case here is fail-closed. This function decides
whether adding a torrent is safe, so anything it cannot positively confirm —
a down container, a rejected key, a malformed reply — must read as "VPN not
active" rather than as an error the caller might shrug off.
"""

import json
import urllib.error

from torrent_agent import vpn

CFG = {"control_url": "http://127.0.0.1:8000", "api_key": "testkey"}


class _Resp:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, payload, status=200):
        self._body = json.dumps(payload).encode()
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _routes(mapping, capture=None):
    """Serve a dict of path -> payload, recording requests if asked."""

    def fake_urlopen(req, timeout=None):
        if capture is not None:
            capture.append(req)
        path = req.full_url.split("8000", 1)[1]
        if path not in mapping:
            raise urllib.error.HTTPError(req.full_url, 404, "nope", {}, None)
        value = mapping[path]
        if isinstance(value, Exception):
            raise value
        return _Resp(value)

    return fake_urlopen


def test_running_tunnel_is_active_and_reports_exit_ip(monkeypatch):
    monkeypatch.setattr(
        vpn.urllib.request,
        "urlopen",
        _routes(
            {
                "/v1/vpn/status": {"status": "running"},
                "/v1/publicip/ip": {"public_ip": "155.2.194.4", "country": "Ireland"},
            }
        ),
    )
    status = vpn.vpn_status("gluetun", CFG)
    assert status.active is True
    assert status.ip == "155.2.194.4"


def test_api_key_is_sent_as_header(monkeypatch):
    seen = []
    monkeypatch.setattr(
        vpn.urllib.request,
        "urlopen",
        _routes(
            {
                "/v1/vpn/status": {"status": "running"},
                "/v1/publicip/ip": {"public_ip": "1.2.3.4"},
            },
            capture=seen,
        ),
    )
    vpn.vpn_status("gluetun", CFG)
    assert seen and seen[0].get_header("X-api-key") == "testkey"


def test_stopped_tunnel_is_inactive(monkeypatch):
    monkeypatch.setattr(
        vpn.urllib.request,
        "urlopen",
        _routes({"/v1/vpn/status": {"status": "stopped"}}),
    )
    status = vpn.vpn_status("gluetun", CFG)
    assert status.active is False
    assert "stopped" in status.detail


def test_rejected_api_key_is_not_treated_as_up(monkeypatch):
    # The dangerous case: a 401 means we learned nothing, not that the VPN is
    # fine. Reading it as "up" would let the agent add torrents blind.
    err = urllib.error.HTTPError(
        "http://127.0.0.1:8000/v1/vpn/status", 401, "Unauthorized", {}, None
    )
    monkeypatch.setattr(
        vpn.urllib.request, "urlopen", _routes({"/v1/vpn/status": err})
    )
    status = vpn.vpn_status("gluetun", CFG)
    assert status.active is False
    assert "api_key" in status.detail


def test_control_server_unreachable_is_inactive(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(vpn.urllib.request, "urlopen", boom)
    status = vpn.vpn_status("gluetun", CFG)
    assert status.active is False
    assert "did not answer" in status.detail


def test_malformed_reply_is_inactive(monkeypatch):
    # A 200 that isn't the shape we expect must not pass either.
    monkeypatch.setattr(
        vpn.urllib.request,
        "urlopen",
        _routes({"/v1/vpn/status": {"unexpected": "shape"}}),
    )
    assert vpn.vpn_status("gluetun", CFG).active is False


def test_running_but_ip_lookup_fails_still_counts_as_active(monkeypatch):
    # The status route is authoritative. A missing exit IP costs us a nicety,
    # not the safety property, so it should not block a legitimate add.
    monkeypatch.setattr(
        vpn.urllib.request,
        "urlopen",
        _routes({"/v1/vpn/status": {"status": "running"}}),
    )
    status = vpn.vpn_status("gluetun", CFG)
    assert status.active is True
    assert status.ip is None


def test_gluetun_never_shells_out_to_piactl(monkeypatch):
    # Guards against the provider branch falling through to the PIA path on a
    # host that happens to have piactl installed — the laptop, during L1.
    monkeypatch.setattr(
        vpn, "_piactl_path", lambda: (_ for _ in ()).throw(AssertionError("piactl"))
    )
    monkeypatch.setattr(
        vpn.urllib.request,
        "urlopen",
        _routes({"/v1/vpn/status": {"status": "running"}}),
    )
    assert vpn.vpn_status("gluetun", CFG).active is True


def test_binding_is_structural_only_for_gluetun():
    # Under gluetun there is no interface binding to check; under PIA there is,
    # and claiming otherwise would silently drop a real safety check.
    assert vpn.binding_is_structural("gluetun") is True
    assert vpn.binding_is_structural("pia") is False

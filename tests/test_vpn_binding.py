"""VPN tunnel detection and Deluge interface-binding checks."""

import json
import subprocess

from torrent_agent import deluge, vpn


class _Proc:
    def __init__(self, stdout, returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _route(dev):
    return _Proc(f"1.1.1.1 via 10.11.0.1 dev {dev} src 10.11.3.195 uid 1000 \ncache")


def test_tunnel_device_returns_name_when_route_uses_tunnel(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _route("tun0"))
    assert vpn.tunnel_device() == "tun0"


def test_tunnel_device_detects_wireguard(monkeypatch):
    # PIA on WireGuard names the device wgpia0, not tun0.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _route("wgpia0"))
    assert vpn.tunnel_device() == "wgpia0"


def test_tunnel_device_none_when_route_leaves_via_lan(monkeypatch):
    # The leak case: VPN down, traffic falls through to the LAN default route.
    # A stale tun* interface must not make this look protected.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _route("wlp1s0"))
    monkeypatch.setattr(vpn, "_tunnel_interfaces", lambda: ["tun0"])
    assert vpn.tunnel_device() is None


def test_tunnel_device_falls_back_when_ip_command_missing(monkeypatch):
    def boom(*a, **k):
        raise OSError("no ip binary")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(vpn, "_tunnel_interfaces", lambda: ["tun0"])
    assert vpn.tunnel_device() == "tun0"


def test_tunnel_device_detects_protonvpn(monkeypatch):
    # ProtonVPN names the device proton0, which matched no prefix before the
    # migration — tunnel_device() returned None and bind_vpn.py refused to run.
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _route("proton0"))
    assert vpn.tunnel_device() == "proton0"


def test_status_uses_live_tunnel_when_pia_is_installed_but_idle(monkeypatch):
    # The migration state: PIA still installed, Proton actually carrying
    # traffic. piactl saying "Disconnected" means PIA is down, not that the
    # machine is unprotected, so this must not report inactive.
    monkeypatch.setattr(vpn, "_piactl_path", lambda: "/opt/piavpn/bin/piactl")
    monkeypatch.setattr(vpn, "_piactl_get", lambda exe, field: "Disconnected")
    monkeypatch.setattr(vpn, "tunnel_device", lambda: "proton0")

    status = vpn.vpn_status("pia")
    assert status.active is True
    assert "proton0" in status.detail


def test_status_inactive_when_pia_idle_and_no_tunnel(monkeypatch):
    # Same path, nothing carrying traffic: must still fail closed.
    monkeypatch.setattr(vpn, "_piactl_path", lambda: "/opt/piavpn/bin/piactl")
    monkeypatch.setattr(vpn, "_piactl_get", lambda exe, field: "Disconnected")
    monkeypatch.setattr(vpn, "tunnel_device", lambda: None)

    assert vpn.vpn_status("pia").active is False


def test_status_ignores_stale_interface_without_a_route(monkeypatch):
    # The hardening: a leftover tun0 in /sys/class/net with the route out over
    # the LAN used to read as "VPN up". It must not.
    monkeypatch.setattr(vpn, "_piactl_path", lambda: None)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _route("wlp1s0"))
    monkeypatch.setattr(vpn, "_tunnel_interfaces", lambda: ["tun0"])

    assert vpn.vpn_status("pia").active is False


def _write_core_conf(path, listen="", outgoing=""):
    header = {"file": 1, "format": 1}
    body = {
        "listen_interface": listen,
        "outgoing_interface": outgoing,
        "cache_size": 512,
    }
    path.write_text(
        json.dumps(header, indent=4, sort_keys=True)
        + json.dumps(body, indent=4, sort_keys=True)
    )


def test_binding_read_from_two_object_config(tmp_path, monkeypatch):
    # core.conf is {header}{body}, not plain JSON — json.load() would fail.
    conf = tmp_path / "core.conf"
    _write_core_conf(conf, "tun0", "tun0")
    monkeypatch.setattr(deluge, "core_conf_path", lambda: conf)
    assert deluge._binding_from_file() == {
        "listen_interface": "tun0",
        "outgoing_interface": "tun0",
    }


def test_binding_reports_unbound(tmp_path, monkeypatch):
    conf = tmp_path / "core.conf"
    _write_core_conf(conf)
    monkeypatch.setattr(deluge, "core_conf_path", lambda: conf)
    assert deluge._binding_from_file() == {
        "listen_interface": "",
        "outgoing_interface": "",
    }


def test_binding_none_when_config_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(deluge, "core_conf_path", lambda: tmp_path / "absent.conf")
    assert deluge._binding_from_file() is None


def test_deluge_binding_falls_back_to_file_when_daemon_down(tmp_path, monkeypatch):
    conf = tmp_path / "core.conf"
    _write_core_conf(conf, "tun0", "tun0")
    monkeypatch.setattr(deluge, "core_conf_path", lambda: conf)

    def no_daemon(_config):
        raise deluge.DelugeError("refused")

    monkeypatch.setattr(deluge, "connect", no_daemon)
    assert deluge.deluge_binding({})["listen_interface"] == "tun0"

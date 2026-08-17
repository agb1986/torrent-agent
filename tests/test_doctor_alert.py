"""scripts/doctor_alert.py: message on change, silence otherwise.

The whole value of the thing is that an unread inbox means healthy. So the
test that matters most is the boring one — an unchanged fault sends nothing.
"""

from doctor import Check
from doctor_alert import compose, load_previous, save_current


def fail(name, detail="broken", fix=""):
    return Check(name, "fail", detail, fix)


def ok(name):
    return Check(name, "ok", "fine")


def test_new_failure_is_announced():
    text, failing = compose([fail("vpn binding", "not bound to proton0")], set(), "box")
    assert "vpn binding" in text and "not bound to proton0" in text
    assert failing == {"vpn binding"}


def test_the_fix_line_is_included():
    text, _ = compose([fail("vpn", "down", "python scripts/bind_vpn.py")], set(), "box")
    assert "python scripts/bind_vpn.py" in text


def test_unchanged_failure_says_nothing():
    """The property the whole design rests on: no news is good news."""
    text, failing = compose([fail("vpn")], {"vpn"}, "box")
    assert text == ""
    assert failing == {"vpn"}


def test_recovery_is_announced():
    text, failing = compose([ok("vpn")], {"vpn"}, "box")
    assert "recovered" in text and "vpn" in text
    assert failing == set()


def test_healthy_and_unchanged_says_nothing():
    text, failing = compose([ok("vpn"), ok("deluge")], set(), "box")
    assert text == "" and failing == set()


def test_a_new_failure_alongside_an_old_one_still_alerts():
    text, failing = compose([fail("vpn"), fail("deluge")], {"vpn"}, "box")
    assert "deluge" in text
    assert "still failing: vpn" in text
    assert failing == {"vpn", "deluge"}


def test_hostname_is_in_the_message():
    """Two boxes have run this stack; a message that does not say which is noise."""
    text, _ = compose([fail("vpn")], set(), "casaos")
    assert "casaos" in text


def test_state_round_trips(tmp_path):
    path = tmp_path / "doctor_alerts.json"
    save_current({"vpn", "deluge"}, path)
    assert load_previous(path) == {"vpn", "deluge"}


def test_missing_state_reads_as_nothing_failing(tmp_path):
    """First run must not announce a recovery for faults it never saw."""
    assert load_previous(tmp_path / "absent.json") == set()


def test_corrupt_state_reads_as_nothing_failing(tmp_path):
    path = tmp_path / "doctor_alerts.json"
    path.write_text("{not json")
    assert load_previous(path) == set()

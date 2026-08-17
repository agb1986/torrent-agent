"""scripts/backup.py: what goes in, what stays out, and rotation.

The two that matter: the Deluge auth file must never end up in an archive
(a backup is a much easier thing to copy around carelessly than the file it
came from), and a half-written archive must not be left looking complete.
"""

import tarfile
from datetime import datetime, timezone

from backup import (backup_dir, collect, deluge_config_dir, existing, rotate,
                    write_backup)


def make_repo(tmp_path):
    state = tmp_path / "tmp"
    state.mkdir()
    (state / "subscriptions.json").write_text('{"tt1": {}}')
    (state / "notified_torrents.json").write_text("[]")
    # Not state: a receipt of something that already happened.
    (state / "removed_seeding_20260101T000000Z.json").write_text("{}")
    return tmp_path


def make_deluge(tmp_path):
    d = tmp_path / "deluge"
    (d / "state").mkdir(parents=True)
    (d / "core.conf").write_text('{"file": 1}{"listen_interface": "proton0"}')
    (d / "state" / "torrents.state").write_bytes(b"\x00")
    (d / "auth").write_text("localclient:notarealpassword:10")
    return d


def config(tmp_path, **over):
    cfg = {"backup": {"dir": str(tmp_path / "backups"), "keep": 3,
                      "deluge_config_dir": str(tmp_path / "deluge")},
           "deluge": {}}
    cfg["backup"].update(over)
    return cfg


# --- what is collected ----------------------------------------------------


def test_collects_state_and_deluge(tmp_path):
    repo = make_repo(tmp_path)
    make_deluge(tmp_path)
    names = {arc for _, arc in collect(config(tmp_path), repo)}
    assert "state/subscriptions.json" in names
    assert "state/notified_torrents.json" in names
    assert "deluge/core.conf" in names
    assert "deluge/state" in names


def test_run_artifacts_are_not_state(tmp_path):
    """tmp/ also collects receipts and run artifacts — those are logs."""
    repo = make_repo(tmp_path)
    names = {arc for _, arc in collect(config(tmp_path), repo)}
    assert not any("removed_seeding" in n for n in names)


def test_missing_pieces_are_skipped_not_fatal(tmp_path):
    """A machine with the bot but no Deluge is a legitimate setup."""
    repo = make_repo(tmp_path)
    assert collect(config(tmp_path), repo)  # no deluge dir exists


def test_nothing_at_all_is_reported_not_written(tmp_path):
    (tmp_path / "tmp").mkdir()
    result = write_backup(config(tmp_path), tmp_path)
    assert result["path"] == "" and "nothing to back up" in result["note"]


# --- the credential -------------------------------------------------------


def test_auth_file_never_reaches_the_archive(tmp_path):
    repo = make_repo(tmp_path)
    make_deluge(tmp_path)
    result = write_backup(config(tmp_path), repo)
    with tarfile.open(result["path"]) as tar:
        names = tar.getnames()
    assert not any(n.endswith("/auth") or n == "auth" for n in names)
    assert "deluge/core.conf" in names


# --- writing and rotation -------------------------------------------------


def test_archive_is_renamed_into_place(tmp_path):
    repo = make_repo(tmp_path)
    result = write_backup(config(tmp_path), repo)
    assert result["path"].endswith(".tar.gz")
    assert not list(backup_dir(config(tmp_path)).glob("*.partial"))


def test_rotation_keeps_the_newest(tmp_path):
    repo = make_repo(tmp_path)
    cfg = config(tmp_path, keep=2)
    for day in (1, 2, 3, 4):
        write_backup(cfg, repo, now=datetime(2026, 1, day, tzinfo=timezone.utc))
    rotate(cfg)
    kept = [p.name for p in existing(cfg)]
    assert len(kept) == 2
    assert "20260104" in kept[0] and "20260103" in kept[1]


def test_keep_zero_rotates_nothing(tmp_path):
    """0 reads as "unset", and deleting every backup is never the intent."""
    repo = make_repo(tmp_path)
    cfg = config(tmp_path, keep=0)
    write_backup(cfg, repo)
    assert rotate(cfg) == []
    assert len(existing(cfg)) == 1


# --- where things live ----------------------------------------------------


def test_deluge_dir_derived_from_auth_file(tmp_path):
    """One setting to get right: auth_file already points at the config dir."""
    auth = tmp_path / "appdata" / "deluge" / "auth"
    auth.parent.mkdir(parents=True)
    auth.write_text("x:y:10")
    cfg = {"backup": {"deluge_config_dir": ""}, "deluge": {"auth_file": str(auth)}}
    assert deluge_config_dir(cfg) == auth.parent


def test_explicit_deluge_dir_wins(tmp_path):
    cfg = {"backup": {"deluge_config_dir": str(tmp_path)},
           "deluge": {"auth_file": "/somewhere/else/auth"}}
    assert deluge_config_dir(cfg) == tmp_path


def test_default_backup_dir_is_under_the_repo():
    assert backup_dir({"backup": {"dir": ""}}).parts[-2:] == ("tmp", "backups")

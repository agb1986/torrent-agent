"""Delivery: choosing local-move vs rsync, and merging rather than nesting.

The merge case is the one that matters in practice. Seasons arrive one at a
time, so the second one lands on a show directory that already exists — and
`shutil.move` onto an existing directory nests it (`Show/Show/Season 02`)
rather than merging, which would split the show into two entries in Jellyfin.
"""

import socket

import transfer


# --- picking a mode -------------------------------------------------------


def test_explicit_local_flag_wins():
    assert transfer.server_is_local({"host": "casaos.local", "local": True}) is True
    assert transfer.server_is_local({"host": "casaos.local", "local": False}) is False


def test_localhost_and_empty_host_are_local():
    for host in ("", "localhost", "127.0.0.1", "::1"):
        assert transfer.server_is_local({"host": host}) is True, host


def test_matching_hostname_is_local(monkeypatch):
    # On the server itself, [server] host = "casaos.local" and the hostname is
    # "CasaOS" — the same machine, in different case.
    monkeypatch.setattr(socket, "gethostname", lambda: "CasaOS")
    assert transfer.server_is_local({"host": "casaos.local"}) is True


def test_a_different_host_is_remote(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "laptop")
    monkeypatch.setattr(transfer, "_local_addresses", lambda: {"127.0.0.1"})
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("192.168.1.52", 0))]
    )
    assert transfer.server_is_local({"host": "casaos.local"}) is False


def test_unresolvable_host_is_not_claimed_as_local(monkeypatch):
    # Failing to resolve is not evidence that it is us. Guessing "local" would
    # move files into a destination on the wrong machine.
    monkeypatch.setattr(socket, "gethostname", lambda: "laptop")

    def boom(*a, **k):
        raise OSError("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert transfer.server_is_local({"host": "nonexistent.invalid"}) is False


# --- moving ---------------------------------------------------------------


def _show(root, season, episodes=("E01", "E02")):
    d = root / "Season 01" if season == 1 else root / f"Season 0{season}"
    d.mkdir(parents=True)
    for e in episodes:
        (d / f"S0{season}{e}.mkv").write_text("x")
    return root


def test_local_move_places_the_directory_inside_the_destination(tmp_path):
    src = _show(tmp_path / "src" / "Toast (2013) [tmdbid-1]", 1)
    dest = tmp_path / "tv"
    dest.mkdir()

    assert transfer.transfer_local(str(src), str(dest)) == 0

    landed = dest / "Toast (2013) [tmdbid-1]" / "Season 01"
    assert sorted(p.name for p in landed.iterdir()) == ["S01E01.mkv", "S01E02.mkv"]
    assert not src.exists(), "a move should not leave the source behind"


def test_second_season_merges_instead_of_nesting(tmp_path):
    dest = tmp_path / "tv"
    existing = _show(dest / "Toast (2013) [tmdbid-1]", 1)
    assert existing.exists()
    src = _show(tmp_path / "src" / "Toast (2013) [tmdbid-1]", 2)

    assert transfer.transfer_local(str(src), str(dest)) == 0

    show = dest / "Toast (2013) [tmdbid-1]"
    assert sorted(p.name for p in show.iterdir()) == ["Season 01", "Season 02"]
    # The nesting bug this guards against.
    assert not (show / "Toast (2013) [tmdbid-1]").exists()
    assert (show / "Season 02" / "S02E01.mkv").exists()
    assert (show / "Season 01" / "S01E01.mkv").exists()


def test_a_single_file_moves_loose_into_the_destination(tmp_path):
    src = tmp_path / "Withnail and I (1987) [tmdbid-2].mkv"
    src.write_text("x")
    dest = tmp_path / "film"
    dest.mkdir()

    assert transfer.transfer_local(str(src), str(dest)) == 0
    assert (dest / src.name).exists()


def test_a_missing_source_reports_failure_rather_than_raising(tmp_path):
    dest = tmp_path / "tv"
    dest.mkdir()
    assert transfer.transfer_local(str(tmp_path / "absent"), str(dest)) == 1

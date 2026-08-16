"""AgentRunner: artifact parsing and subprocess behaviour.

The parsing rule under test is that the run's JSON artifact is the contract and
the printed summary is not. The summary is model-authored prose that will drift
between model versions; the artifact has a fixed shape and carries the
torrent_id the audit log needs.

The subprocess tests spawn real processes rather than mocking Popen — the
failure modes worth covering here (non-zero exit, timeout, missing binary) are
precisely the ones a mock would paper over.
"""

import json
import sys

from server.runner import AgentRunner, DailyCap, RunResult


def make_runner(tmp_path, limit=10, timeout=30):
    cap = DailyCap(limit=limit, state_path=tmp_path / "cap.json")
    return AgentRunner(cap=cap, repo_root=tmp_path, timeout=timeout)


class ScriptRunner(AgentRunner):
    """Runs an inline python snippet instead of the agent."""

    def __init__(self, *args, script="", **kwargs):
        super().__init__(*args, **kwargs)
        self.script = script

    def _command(self, request):
        return [sys.executable, "-c", self.script, request]


# --- artifact parsing -----------------------------------------------------

def test_parse_reads_the_artifact_not_the_prose(tmp_path):
    artifact = tmp_path / "added_thing.json"
    artifact.write_text(json.dumps({
        "request": "the bear",
        "added": [{"title": "The Bear S03", "torrent_id": "deadbeef",
                   "resolution": "1080p"}],
    }))
    out = f"{artifact}\nAdded: The Bear S03 (1080p) — looks like a good release."

    result = AgentRunner._parse(out)

    assert result.ok
    assert result.torrent_ids == ["deadbeef"]
    assert result.artifact == str(artifact)
    # The path line is internal plumbing and must not be relayed to the user.
    assert str(artifact) not in result.summary
    assert result.summary.startswith("Added: The Bear S03")


def test_parse_without_an_artifact_keeps_the_whole_summary(tmp_path):
    out = "No matching torrents found."
    result = AgentRunner._parse(out)
    assert result.ok
    assert result.added == []
    assert result.summary == "No matching torrents found."


def test_parse_ignores_a_json_path_that_does_not_exist(tmp_path):
    # A summary that merely happens to start with something .json-shaped must
    # not be silently swallowed as a path.
    out = "/nope/missing.json\nAdded: something"
    result = AgentRunner._parse(out)
    assert result.artifact is None
    assert result.summary.startswith("/nope/missing.json")


def test_parse_survives_a_corrupt_artifact(tmp_path):
    artifact = tmp_path / "added_bad.json"
    artifact.write_text("{ not json")
    result = AgentRunner._parse(f"{artifact}\nAdded: something")
    assert result.ok and result.added == []
    assert result.summary == "Added: something"


def test_parse_collects_every_torrent_id_from_a_multi_add(tmp_path):
    artifact = tmp_path / "added_many.json"
    artifact.write_text(json.dumps({"added": [
        {"title": "E01", "torrent_id": "a1"},
        {"title": "E02", "torrent_id": "b2"},
    ]}))
    result = AgentRunner._parse(f"{artifact}\nAdded 2 episodes")
    assert result.torrent_ids == ["a1", "b2"]


# --- subprocess behaviour -------------------------------------------------

def test_successful_run_returns_the_summary(tmp_path):
    r = ScriptRunner(
        cap=DailyCap(10, tmp_path / "cap.json"),
        repo_root=tmp_path,
        script="print('No matching torrents found.')",
    )
    result = r.run("anything")
    assert result.ok
    assert result.summary == "No matching torrents found."


def test_non_zero_exit_is_reported_with_the_tail_of_the_output(tmp_path):
    r = ScriptRunner(
        cap=DailyCap(10, tmp_path / "cap.json"),
        repo_root=tmp_path,
        script="import sys; print('DelugeError: could not connect'); sys.exit(1)",
    )
    result = r.run("anything")
    assert not result.ok
    assert "Agent failed" in result.summary
    assert "DelugeError" in result.summary


def test_timeout_is_bounded_and_reported(tmp_path):
    r = ScriptRunner(
        cap=DailyCap(10, tmp_path / "cap.json"),
        repo_root=tmp_path,
        timeout=1,
        script="import time; time.sleep(30)",
    )
    result = r.run("anything")
    assert not result.ok
    assert "Timed out" in result.summary


def test_missing_interpreter_is_an_error_not_a_crash(tmp_path):
    r = make_runner(tmp_path)          # repo_root has no .venv/bin/python
    result = r.run("anything")
    assert not result.ok
    assert "Could not start the agent" in result.summary


# --- the cap sits in front of the spend -----------------------------------

def test_cap_exhausted_means_no_subprocess_at_all(tmp_path):
    r = ScriptRunner(
        cap=DailyCap(1, tmp_path / "cap.json"),
        repo_root=tmp_path,
        script="open('ran.txt','w').write('x')",
    )
    assert r.run("first").ok
    second = r.run("second")

    assert not second.ok
    assert "Daily limit" in second.summary
    # The marker file proves exactly one process was spawned.
    assert (tmp_path / "ran.txt").exists()


def test_config_path_is_passed_through_to_the_agent(tmp_path):
    r = AgentRunner(
        cap=DailyCap(10, tmp_path / "cap.json"),
        config_path="config.rehearsal.toml",
        repo_root=tmp_path,
    )
    cmd = r._command("the bear")
    assert "--config" in cmd
    assert cmd[cmd.index("--config") + 1] == "config.rehearsal.toml"
    assert cmd[-1] == "the bear"


def test_request_is_passed_as_one_argv_entry(tmp_path):
    # Never shell-interpolated: a title containing ; or $() must be inert.
    r = AgentRunner(cap=DailyCap(10, tmp_path / "cap.json"), repo_root=tmp_path)
    cmd = r._command("the bear; rm -rf ~")
    assert cmd[-1] == "the bear; rm -rf ~"


def test_run_result_torrent_ids_tolerates_missing_field():
    result = RunResult(ok=True, summary="x", added=[{"title": "no id here"}])
    assert result.torrent_ids == [""]

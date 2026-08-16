"""Invoking the agent, and the guard rails around doing so unattended.

Two things live here because they are the same concern: an unattended process
holding an API key is the expensive failure mode, so the spend cap sits
directly in front of the only code path that can spend anything.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# The agent picks a release, adds it, and exits. Long, because a cold Prowlarr
# search plus a full model loop can genuinely take minutes; bounded, because a
# wedged subprocess must not hold the queue forever.
RUN_TIMEOUT_SECONDS = 900


@dataclass
class RunResult:
    ok: bool
    summary: str
    added: list[dict[str, Any]] = field(default_factory=list)
    artifact: str | None = None

    @property
    def torrent_ids(self) -> list[str]:
        return [a.get("torrent_id", "") for a in self.added]


class DailyCap:
    """A per-day request ceiling, persisted so a restart cannot reset it.

    Deliberately counts *attempts*, not successes: a run that fails still cost
    tokens, and a crash-looping bot is exactly the case this exists to bound.
    """

    def __init__(self, limit: int, state_path: Path) -> None:
        self.limit = int(limit)
        self._path = Path(state_path)
        self._lock = threading.Lock()

    def _read(self) -> tuple[str, int]:
        try:
            data = json.loads(self._path.read_text())
            return str(data.get("day", "")), int(data.get("count", 0))
        except (OSError, ValueError):
            return "", 0

    def _write(self, day: str, count: int) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"day": day, "count": count}))
        tmp.replace(self._path)

    def remaining(self, today: str | None = None) -> int:
        today = today or date.today().isoformat()
        day, count = self._read()
        return self.limit if day != today else max(0, self.limit - count)

    def claim(self, today: str | None = None) -> bool:
        """Consume one request. False when the cap is already spent."""
        if self.limit <= 0:
            return False
        today = today or date.today().isoformat()
        with self._lock:
            day, count = self._read()
            if day != today:
                count = 0
            if count >= self.limit:
                return False
            self._write(today, count + 1)
            return True


class AgentRunner:
    """Runs one agent request to completion.

    Invocation is isolated here on purpose. The migration plan proposed driving
    the `get-torrents` skill through `claude -p`, but that skill's whole body is
    "run python -m torrent_agent", so going through it would add a second model
    round-trip and its cost, latency and version drift for no behaviour we want
    at this scope. Swapping back later means changing `_command` and nothing
    else.
    """

    def __init__(
        self,
        cap: DailyCap,
        config_path: str | None = None,
        repo_root: Path = REPO_ROOT,
        timeout: int = RUN_TIMEOUT_SECONDS,
    ) -> None:
        self.cap = cap
        self.config_path = config_path
        self.repo_root = Path(repo_root)
        self.timeout = timeout
        self._proc: subprocess.Popen[str] | None = None
        self._proc_lock = threading.Lock()

    def torrents(self) -> list[dict[str, Any]]:
        """What Deluge is currently doing. Raises DelugeError if it can't say.

        Read straight from Deluge rather than tracked in the bot: the bot is
        not the only thing that adds torrents, and a view that only knew about
        its own additions would quietly lie.
        """
        from torrent_agent.config import load_config
        from torrent_agent.deluge import list_torrents

        return list_torrents(load_config(self.config_path))

    def _command(self, request: str) -> list[str]:
        cmd = [str(self.repo_root / ".venv" / "bin" / "python"), "-m", "torrent_agent"]
        if self.config_path:
            cmd += ["--config", self.config_path]
        return cmd + [request]

    def cancel(self) -> bool:
        """Terminate the in-flight run, if any."""
        with self._proc_lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return False
            proc.terminate()
            return True

    def run(self, request: str) -> RunResult:
        if not self.cap.claim():
            return RunResult(
                ok=False,
                summary=(
                    f"Daily limit of {self.cap.limit} requests reached. "
                    f"Nothing was sent to the API. Try again tomorrow."
                ),
            )

        env = dict(os.environ)
        if self.config_path:
            env["TORRENT_AGENT_CONFIG"] = self.config_path

        try:
            with self._proc_lock:
                self._proc = subprocess.Popen(
                    self._command(request),
                    cwd=str(self.repo_root),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
            proc = self._proc
            out, _ = proc.communicate(timeout=self.timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            self.cancel()
            return RunResult(
                ok=False, summary=f"Timed out after {self.timeout // 60} minutes."
            )
        except OSError as exc:
            return RunResult(ok=False, summary=f"Could not start the agent: {exc}")
        finally:
            with self._proc_lock:
                self._proc = None

        out = (out or "").strip()
        if code != 0:
            tail = out.splitlines()[-8:] if out else ["(no output)"]
            return RunResult(ok=False, summary="Agent failed:\n" + "\n".join(tail))

        return self._parse(out)

    @staticmethod
    def _parse(out: str) -> RunResult:
        """Read the run's JSON artifact rather than scraping the prose.

        cli.py prints the artifact path as its first line when it added
        something, then the human summary. The artifact is the contract — the
        summary is model-authored and will drift.
        """
        lines = out.splitlines()
        artifact = None
        if lines and lines[0].strip().endswith(".json"):
            candidate = Path(lines[0].strip())
            if candidate.is_file():
                artifact = str(candidate)
                lines = lines[1:]

        summary = "\n".join(lines).strip() or "(no summary)"
        added: list[dict[str, Any]] = []
        if artifact:
            try:
                added = json.loads(Path(artifact).read_text()).get("added", [])
            except (OSError, ValueError):
                added = []
        return RunResult(ok=True, summary=summary, added=added, artifact=artifact)

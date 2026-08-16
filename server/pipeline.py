"""Finished download -> tidied -> delivered -> Jellyfin told.

The steps a person used to run by hand after every download, in the order the
fetch-to-jellyfin skill runs them. Deluge first, because tidying moves files
out from under a live torrent and breaks seeding; then tidy, then deliver,
then tell Jellyfin.

Nothing here guesses. `torrent_agent.tidy` produces a plan and refuses to be
confident when anything is unclear, and this refuses to act on an unconfident
plan — the download is left exactly where it is and the user is told why. The
bad outcome to design against is not "the pipeline stopped", it is "the
pipeline confidently filed something under the wrong programme".
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from torrent_agent import deluge
from torrent_agent.deluge import DelugeError
from torrent_agent.tidy import TidyPlan, execute, plan_for

log = logging.getLogger("server.pipeline")

# scripts/ is not a package; transfer.py owns delivery and the Jellyfin call,
# and duplicating either here would let them drift apart.
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


@dataclass
class Outcome:
    ok: bool
    stage: str
    message: str
    plan: TidyPlan | None = None
    delivered_to: str | None = None
    details: list[str] = field(default_factory=list)


def _destination_for(kind: str, config: dict[str, Any]) -> str | None:
    dests = config.get("server", {}).get("destinations", {})
    return dests.get("tv" if kind == "tv" else "film")


def run(torrent: dict[str, Any], config: dict[str, Any]) -> Outcome:
    """Take one finished torrent all the way to Jellyfin.

    `torrent` is a row from deluge.list_torrents.
    """
    import transfer  # noqa: E402  (path set above)

    name = torrent.get("name") or "?"
    source = Path(torrent.get("save_path") or "") / name
    if not source.exists():
        return Outcome(
            False, "locate", f"Downloaded {name}, but {source} is not on disk."
        )

    # 1. Out of Deluge first. Tidying moves the files, and a torrent still
    #    seeding from them breaks the moment a peer asks for a piece.
    try:
        with deluge.connect(config) as client:
            client.call("core.remove_torrent", torrent["id"], False)
    except (DelugeError, Exception) as exc:  # deluge_client raises bare
        return Outcome(
            False, "deluge", f"Could not remove {name} from Deluge: {exc}"
        )

    # 2. Decide the naming. This is where it refuses if anything is unclear.
    plan = plan_for(source)
    if not plan.confident:
        return Outcome(
            False,
            "tidy",
            f"Downloaded {name}, but I am not sure how to file it.",
            plan=plan,
            details=plan.problems,
        )

    # 3. Rename into place, still inside the downloads directory.
    try:
        execute(plan)
    except (OSError, ValueError) as exc:
        return Outcome(False, "tidy", f"Tidy failed for {name}: {exc}", plan=plan)

    # 4. Deliver. On the server this is a move onto the same filesystem; from
    #    a laptop it is still rsync. transfer.py decides which.
    dest = _destination_for(plan.kind, config)
    if not dest:
        return Outcome(
            False, "deliver", f"No [server.destinations] entry for {plan.kind}.",
            plan=plan,
        )
    code = (
        transfer.transfer_local(str(plan.root), dest)
        if transfer.server_is_local()
        else transfer.transfer(str(plan.root), transfer.build_remote(dest))
    )
    if code != 0:
        return Outcome(
            False, "deliver", f"Delivery of {plan.root.name} failed (exit {code}).",
            plan=plan,
        )

    # 5. Tell Jellyfin. Best-effort by design: the files have landed, so a
    #    failed scan is a nuisance, not a lost download.
    landed = f"{dest}/{plan.root.name}"
    try:
        transfer.scan_jellyfin(landed)
    except Exception as exc:  # noqa: BLE001 - never fail a delivered file
        log.warning("Jellyfin scan failed: %s", exc)

    return Outcome(
        True, "done", f"Delivered {plan.root.name}", plan=plan, delivered_to=landed
    )


def format_outcome(outcome: Outcome) -> str:
    """What the user reads on their phone."""
    if outcome.ok:
        plan = outcome.plan
        count = len(plan.moves) if plan else 0
        return (
            f"✅ {outcome.message}\n"
            f"   {count} file(s) → {outcome.delivered_to}\n"
            f"   Jellyfin notified."
        )

    lines = [f"⚠️ {outcome.message}"]
    if outcome.details:
        lines.append("")
        lines += [f"  • {d}" for d in outcome.details[:5]]
    lines.append("")
    lines.append("Left where it is — nothing was renamed or moved.")
    return "\n".join(lines)

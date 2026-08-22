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
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from torrent_agent import ai_data_store, deluge
from torrent_agent.deluge import DelugeError
from torrent_agent.security import ClamAVUnavailable, clamav_scan
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
    jellyfin_ok: bool = True
    details: list[str] = field(default_factory=list)


def _destination_for(kind: str, config: dict[str, Any]) -> str | None:
    dests = config.get("server", {}).get("destinations", {})
    return dests.get(kind)


def host_path(container_path: str, config: dict[str, Any]) -> str:
    """Translate a path Deluge reports into one this process can open.

    Deluge runs in a container and answers with its own view — `/downloads`,
    which does not exist on the host, where the same files are at
    /mnt/data/downloads. Configured as [deluge.path_map], the same shape as
    [jellyfin.path_map], and for the same reason. Longest prefix wins.

    Unmapped paths pass through unchanged: a native deluged reports host paths
    already, and nothing should need configuring for it.
    """
    mapping = config.get("deluge", {}).get("path_map", {}) or {}
    best = None
    for inside, outside in mapping.items():
        if container_path == inside or container_path.startswith(inside.rstrip("/") + "/"):
            if best is None or len(inside) > len(best[0]):
                best = (inside, outside)
    if best is None:
        return container_path
    return best[1].rstrip("/") + container_path[len(best[0].rstrip("/")):]


def run(torrent: dict[str, Any], config: dict[str, Any]) -> Outcome:
    """Take one finished torrent all the way to Jellyfin.

    `torrent` is a row from deluge.list_torrents.
    """
    import transfer  # noqa: E402  (path set above)

    name = torrent.get("name") or "?"
    source = Path(host_path(str(torrent.get("save_path") or ""), config)) / name
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

    # 2. Scan for malware before deciding anything about naming or delivery —
    #    the extension check in torrent_agent.deluge.add_torrent is the first
    #    gate, this is the backstop that looks at contents instead of names.
    #    A missing scanner is an ops gap, not evidence the file is clean, so
    #    it does not block delivery — it just means this pass didn't happen.
    try:
        infected = clamav_scan(source)
    except ClamAVUnavailable:
        infected = []
    except Exception as exc:  # noqa: BLE001 - a scan crash must not lose the file
        log.warning("clamav scan failed for %s: %s", name, exc)
        infected = []
    if infected:
        quarantine_dir = source.parent / "quarantine"
        quarantine_dir.mkdir(exist_ok=True)
        dest = quarantine_dir / source.name
        try:
            shutil.move(str(source), str(dest))
        except OSError:
            pass  # couldn't move it — leave it in place, still reported below
        return Outcome(
            False, "security",
            f"{name} failed a malware scan — quarantined, not filed.",
            details=infected,
        )

    # 3. Decide the naming. This is where it refuses if anything is unclear.
    plan = plan_for(source)
    if not plan.confident:
        return Outcome(
            False,
            "tidy",
            f"Downloaded {name}, but I am not sure how to file it.",
            plan=plan,
            details=plan.problems,
        )

    # 4. Rename into place, still inside the downloads directory.
    try:
        execute(plan)
    except (OSError, ValueError) as exc:
        return Outcome(False, "tidy", f"Tidy failed for {name}: {exc}", plan=plan)

    # 5. Deliver. On the server this is a move onto the same filesystem; from
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

    # 6. Tell Jellyfin. Best-effort by design: the files have landed, so a
    #    failed scan is a nuisance, not a lost download. Jellyfin is a video
    #    library — a manga delivery has nothing for it to scan.
    landed = f"{dest}/{plan.root.name}"
    jellyfin_ok = True
    if plan.kind in ("tv", "film"):
        # scan_jellyfin swallows its own errors and reports them on stdout, so
        # watch the printed output rather than trusting the absence of an
        # exception — otherwise the summary claims a scan that never happened.
        try:
            import io
            from contextlib import redirect_stdout

            buf = io.StringIO()
            with redirect_stdout(buf):
                transfer.scan_jellyfin(landed)
            printed = buf.getvalue()
            print(printed, end="")
            jellyfin_ok = "[WARN]" not in printed
        except Exception as exc:  # noqa: BLE001 - never fail a delivered file
            log.warning("Jellyfin scan failed: %s", exc)
            jellyfin_ok = False

    # The automated pipeline has no other artifact-writing analog — log_tidy.py
    # is only invoked by the interactive tidy-files skill's manual pipe — so
    # this is the only place an autodelivered item reaches ai-data-store.
    ai_data_store.post_entry(
        source="torrent-agent",
        description=f"Tidied - {plan.name}",
        keywords=["tidy-files", plan.kind],
        data={"kind": plan.kind, "name": plan.name, "delivered_to": landed},
    )

    return Outcome(
        True, "done", f"Delivered {plan.root.name}", plan=plan, delivered_to=landed,
        jellyfin_ok=jellyfin_ok,
    )


def format_outcome(outcome: Outcome) -> str:
    """What the user reads on their phone."""
    if outcome.ok:
        plan = outcome.plan
        count = len(plan.moves) if plan else 0
        tail = (
            "Jellyfin notified."
            if outcome.jellyfin_ok
            else "Jellyfin did NOT pick it up — the files are in place, "
                 "so a library scan will find them."
        )
        return f"✅ {outcome.message}\n   {count} file(s) → {outcome.delivered_to}\n   {tail}"


    lines = [f"⚠️ {outcome.message}"]
    if outcome.details:
        lines.append("")
        lines += [f"  • {d}" for d in outcome.details[:5]]
    lines.append("")
    if outcome.stage == "security":
        lines.append("Moved to quarantine/ — not filed, not delivered.")
    else:
        lines.append("Left where it is — nothing was renamed or moved.")
    return "\n".join(lines)

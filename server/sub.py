"""Follow a series that is still airing, and fetch each episode as it lands.

Deliberately *not* a cron job that reschedules itself. That is a state machine
whose state lives in crontab, and every failure mode breaks it: a missed run —
box asleep, VPN down, no release yet — leaves nothing scheduled and the
subscription dies silently.

Instead: a list of subscriptions, and a reconciler that asks "which episodes
have aired and are not here yet?" on every tick. Nothing is scheduled, so
nothing can be missed. A tick that never happens costs a delay, not a season.

    /sub https://www.imdb.com/title/tt7311154/   follow
    /sub list                                    what is being followed
    /sub stop tt7311154                          stop following

IMDb links only, never titles. An ambiguous subscription would quietly fetch
the wrong programme for weeks — /get can afford a bad guess, this cannot.

This stops at "added". The notifier already watches Deluge and runs tidy,
delivery and the Jellyfin scan on completion, so subscriptions only decide
*what* to fetch and *when*.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from guessit import guessit

from torrent_agent.imdb import extract_id, show_by_imdb
from torrent_agent.tidy import tvmaze_episode_details

log = logging.getLogger("server.sub")

# A backlog should fill in over a few hours rather than eating the whole daily
# budget in one tick — subscribing to a show mid-season can mean eight episodes.
MAX_ADDS_PER_TICK = 3

# A release does not appear the moment an episode airs, and there is no useful
# signal for when it will. Ask again later, indefinitely.
RETRY_HOURS = 12

# Keep retrying after this, but say something once — an episode that is never
# released should not fail in silence forever.
ALERT_AFTER_ATTEMPTS = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_airstamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def episode_key(season: int, number: int) -> str:
    return f"S{season:02d}E{number:02d}"


@dataclass
class Action:
    """Something the reconciler did, or decided not to do."""

    kind: str          # "added" | "failed" | "capped" | "complete" | "stuck"
    imdb: str
    show: str
    episode: str = ""
    detail: str = ""


class Store:
    """The subscription list. Small, rewritten whole, written atomically."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(self.path)


# --- subscribing ----------------------------------------------------------


def subscribe(link: str, store: Store, now: datetime | None = None) -> tuple[bool, str]:
    """Follow the series behind an IMDb link. Returns (ok, message)."""
    now = now or _now()
    imdb = extract_id(link)
    if not imdb:
        return False, "That is not an IMDb link. Try /sub https://www.imdb.com/title/tt…"

    show = show_by_imdb(imdb)
    if not show:
        return False, f"IMDb {imdb} does not match a series TVmaze knows about."

    episodes = tvmaze_episode_details(int(show["id"]))
    if not episodes:
        return False, f"TVmaze lists no episodes for {show['name']}."

    season = _season_to_follow(show, episodes, now)
    if season is None:
        return False, (
            f"{show['name']} has no upcoming or recent season to follow — "
            f"nothing has aired and nothing is scheduled."
        )

    data = store.load()
    if imdb in data:
        return False, f"Already following {show['name']} (season {data[imdb]['season']})."

    tracked, aired = _seed_episodes(episodes, season, now)
    data[imdb] = {
        "tvmaze_id": int(show["id"]),
        "name": show["name"],
        "season": season,
        "quality": {},
        "episodes": tracked,
        "added_at": now.isoformat(timespec="seconds"),
    }
    store.save(data)

    upcoming = _next_airdate(episodes, season, now)
    msg = [f"Following {show['name']} — season {season}."]
    if aired:
        msg.append(f"{aired} episode(s) already aired; catching up "
                   f"{MAX_ADDS_PER_TICK} at a time.")
    msg.append(
        f"Next episode: {upcoming}" if upcoming else "No further episodes scheduled yet."
    )
    return True, "\n".join(msg)


def _season_to_follow(show: dict, episodes: dict, now: datetime) -> int | None:
    """The season with an unaired episode, else the most recent to have aired."""
    future = [s for (s, _n), meta in episodes.items()
              if (a := _parse_airstamp(meta.get("airstamp"))) and a > now]
    if future:
        return min(future)
    past = [s for (s, _n), meta in episodes.items()
            if (a := _parse_airstamp(meta.get("airstamp"))) and a <= now]
    return max(past) if past else None


def _seed_episodes(episodes: dict, season: int, now: datetime) -> tuple[dict, int]:
    """Every episode of the season, aired ones pending so catch-up just works."""
    tracked: dict[str, Any] = {}
    aired = 0
    for (s, n), meta in sorted(episodes.items()):
        if s != season:
            continue
        stamp = _parse_airstamp(meta.get("airstamp"))
        key = episode_key(s, n)
        has_aired = bool(stamp and stamp <= now)
        tracked[key] = {
            "state": "pending",
            "attempts": 0,
            "airstamp": meta.get("airstamp"),
            "next_attempt": (stamp or now).isoformat(timespec="seconds"),
        }
        aired += 1 if has_aired else 0
    return tracked, aired


def _next_airdate(episodes: dict, season: int, now: datetime) -> str | None:
    upcoming = sorted(
        (a, episode_key(s, n))
        for (s, n), meta in episodes.items()
        if s == season and (a := _parse_airstamp(meta.get("airstamp"))) and a > now
    )
    if not upcoming:
        return None
    when, key = upcoming[0]
    return f"{key} on {when.date().isoformat()}"


def unsubscribe(imdb_or_link: str, store: Store) -> tuple[bool, str]:
    imdb = extract_id(imdb_or_link) or imdb_or_link.strip()
    data = store.load()
    entry = data.pop(imdb, None)
    if entry is None:
        return False, f"Not following {imdb}."
    store.save(data)
    return True, f"Stopped following {entry['name']}."


def format_list(store: Store) -> str:
    data = store.load()
    if not data:
        return "Not following anything.\n\n/sub <imdb link> to start."
    out = []
    for imdb, sub in sorted(data.items(), key=lambda kv: kv[1]["name"].lower()):
        eps = sub.get("episodes", {})
        added = sum(1 for e in eps.values() if e.get("state") == "added")
        pending = [k for k, e in eps.items() if e.get("state") == "pending"]
        line = f"• {sub['name']} — season {sub['season']}: {added}/{len(eps)} fetched"
        if pending:
            line += f"\n   waiting on {', '.join(sorted(pending)[:4])}"
        line += f"\n   {imdb}"
        out.append(line)
    return "Following:\n\n" + "\n".join(out)


# --- reconciling ----------------------------------------------------------


def _quality_hint(sub: dict[str, Any]) -> str:
    q = sub.get("quality") or {}
    bits = [str(q[k]) for k in ("resolution", "group") if q.get(k)]
    return " ".join(bits)


def _learn_quality(sub: dict[str, Any], title: str) -> None:
    """Pin the first episode's shape so a season does not become a patchwork."""
    if sub.get("quality"):
        return
    g = guessit(title)
    group = g.get("release_group")
    sub["quality"] = {
        "resolution": g.get("screen_size"),
        "codec": g.get("video_codec"),
        "group": str(group) if group else None,
    }


def reconcile(
    store: Store,
    runner: Any,
    config: dict[str, Any],
    now: datetime | None = None,
) -> list[Action]:
    """One pass: fetch what has aired and is missing. Returns what happened."""
    now = now or _now()
    data = store.load()
    actions: list[Action] = []
    budget = MAX_ADDS_PER_TICK

    for imdb, sub in list(data.items()):
        # Refresh from TVmaze so episodes added or rescheduled after
        # subscribing are picked up rather than frozen at subscribe time.
        episodes = tvmaze_episode_details(int(sub["tvmaze_id"]))
        if episodes:
            _refresh(sub, episodes, now)

        for key in sorted(sub["episodes"]):
            if budget <= 0:
                break
            ep = sub["episodes"][key]
            if ep.get("state") != "pending":
                continue
            due = _parse_airstamp(ep.get("next_attempt"))
            if due and due > now:
                continue

            if runner.cap.remaining() <= 0:
                actions.append(Action("capped", imdb, sub["name"], key))
                break

            budget -= 1
            result = runner.run(f"{sub['name']} {key} {_quality_hint(sub)}".strip())
            added = getattr(result, "added", None) or []
            if result.ok and added:
                ep["state"] = "added"
                ep["torrent_id"] = added[0].get("torrent_id")
                ep["at"] = now.isoformat(timespec="seconds")
                _learn_quality(sub, str(added[0].get("title", "")))
                actions.append(Action("added", imdb, sub["name"], key,
                                      str(added[0].get("title", ""))))
            else:
                ep["attempts"] = int(ep.get("attempts", 0)) + 1
                ep["next_attempt"] = (
                    now + timedelta(hours=RETRY_HOURS)
                ).isoformat(timespec="seconds")
                if ep["attempts"] == ALERT_AFTER_ATTEMPTS:
                    actions.append(Action("stuck", imdb, sub["name"], key,
                                          f"{ep['attempts']} attempts"))

        if _season_complete(sub):
            actions.append(Action("complete", imdb, sub["name"],
                                  detail=f"season {sub['season']}"))
            data.pop(imdb, None)

    store.save(data)
    return actions


def _refresh(sub: dict[str, Any], episodes: dict, now: datetime) -> None:
    for (s, n), meta in episodes.items():
        if s != sub["season"]:
            continue
        key = episode_key(s, n)
        if key not in sub["episodes"]:
            stamp = _parse_airstamp(meta.get("airstamp"))
            sub["episodes"][key] = {
                "state": "pending",
                "attempts": 0,
                "airstamp": meta.get("airstamp"),
                "next_attempt": (stamp or now).isoformat(timespec="seconds"),
            }


def _season_complete(sub: dict[str, Any]) -> bool:
    eps = sub.get("episodes") or {}
    return bool(eps) and all(e.get("state") == "added" for e in eps.values())


def format_actions(actions: list[Action]) -> list[str]:
    """One message per thing worth telling the user about."""
    out = []
    for a in actions:
        if a.kind == "added":
            out.append(f"📺 {a.show} {a.episode} — added\n   {a.detail}")
        elif a.kind == "complete":
            out.append(
                f"✅ {a.show} — {a.detail} complete. No longer following.\n"
                f"   /sub again when the next season is announced."
            )
        elif a.kind == "capped":
            out.append(
                f"⏸ Daily request limit reached — {a.show} {a.episode} is waiting.\n"
                f"   It will retry tomorrow, or raise TELEGRAM_DAILY_LIMIT."
            )
        elif a.kind == "stuck":
            out.append(
                f"⚠️ {a.show} {a.episode}: still no release after {a.detail}.\n"
                f"   Still trying every {RETRY_HOURS}h."
            )
    return out


# --- the service ----------------------------------------------------------

# Hourly. The retry window is 12h and episodes air weekly, so this is about
# responsiveness after a reboot rather than precision — and it costs one
# TVmaze call per subscription.
TICK_SECONDS = 3600


class SubscriptionWatcher:
    def __init__(
        self,
        client: Any,
        chat_ids: list[int],
        store: Store,
        runner: Any,
        config: dict[str, Any],
        interval: int = TICK_SECONDS,
    ) -> None:
        self.client = client
        self.chat_ids = list(chat_ids)
        self.store = store
        self.runner = runner
        self.config = config
        self.interval = interval
        self._capped_on: str | None = None

    def _say(self, text: str) -> None:
        for chat_id in self.chat_ids:
            try:
                self.client.send_message(chat_id, text)
            except Exception as exc:  # noqa: BLE001 - a send must not stop a tick
                log.warning("could not notify %s: %s", chat_id, exc)

    def tick(self, now: datetime | None = None) -> list[Action]:
        now = now or _now()
        actions = reconcile(self.store, self.runner, self.config, now=now)
        for action, text in zip(actions, format_actions(actions)):
            # The cap is hit once and then hit on every tick until midnight;
            # say so once a day rather than every hour.
            if action.kind == "capped":
                today = now.date().isoformat()
                if self._capped_on == today:
                    continue
                self._capped_on = today
            self._say(text)
        return actions

    def run_forever(self) -> None:
        import time

        while True:
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - one bad show must not stop the rest
                log.exception("subscription tick failed")
            time.sleep(self.interval)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    from .runner import REPO_ROOT, AgentRunner, DailyCap
    from .telegram import TelegramClient
    from torrent_agent.config import load_config

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--once", action="store_true", help="Single tick, then exit.")
    parser.add_argument("--interval", type=int, default=TICK_SECONDS)
    args = parser.parse_args(argv)

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    raw = os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "")
    chat_ids = [int(p) for p in raw.replace(",", " ").split() if p.strip("-").isdigit()]
    if not token or not chat_ids:
        print("TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS must be set", flush=True)
        return 1

    config = load_config(os.environ.get("TORRENT_AGENT_CONFIG"))
    watcher = SubscriptionWatcher(
        client=TelegramClient(token),
        chat_ids=chat_ids,
        store=Store(REPO_ROOT / "tmp" / "subscriptions.json"),
        # Shares the bot's daily budget on purpose: one ceiling on spend, not
        # two that can each be under their own limit.
        runner=AgentRunner(
            cap=DailyCap(
                limit=int(os.environ.get("TELEGRAM_DAILY_LIMIT", "20")),
                state_path=REPO_ROOT / "tmp" / "bot_daily_count.json",
            ),
            config_path=os.environ.get("TORRENT_AGENT_CONFIG"),
        ),
        config=config,
        interval=args.interval,
    )
    if args.once:
        actions = watcher.tick()
        print(f"{len(actions)} action(s)")
        return 0
    watcher.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

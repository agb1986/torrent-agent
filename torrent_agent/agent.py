"""The Claude tool-use agent that drives search -> rank -> VPN check -> Deluge.

Magnet URIs are kept in a server-side registry keyed by a short id; the model
only ever sees the id, which keeps long magnets out of the context window and
removes a class of copy/paste errors.
"""

from __future__ import annotations

import json
from typing import Any

import anthropic

from . import deluge, ranking, search
from .config import anthropic_api_key
from .vpn import binding_is_structural, tunnel_device, vpn_status

_MAX_TURNS = 16
# Cap on results shown to the model. Kept generous so a "grab every episode"
# request isn't silently truncated to whichever few episodes rank highest —
# several releases per episode can otherwise crowd a whole season out of view.
_TOP_N = 40

SYSTEM_PROMPT = """\
You are a torrent-fetching agent. Given a request (usually a TV show or movie),
you find the best matching torrent and add it to the user's Deluge client.

Workflow:
1. Call search_torrents with a clean query and the media_type ("tv", "movie",
   or "any"). Results come back already ranked best-first.
2. Pick the single best candidate. Ranking priorities, in order: resolution
   preference, preferred codec, seeders, recency. Prefer exact season/episode
   matches when the user asked for a specific one. Use judgment on the
   resolution/seeder trade-off: a well-seeded lower resolution beats a nearly
   dead higher one (e.g. 720p with hundreds of seeders over 1080p with 1-2).
3. Before adding, the download must go through the VPN. Call check_vpn. If it is
   not active, DO NOT add the torrent — tell the user to start their VPN (PIA)
   first, then stop. (add_torrent also refuses when the VPN is down, as a guard.)
   If check_vpn reports deluge_bound_to_vpn as false, still proceed, but mention
   in your summary that Deluge is not bound to the tunnel and that running
   scripts/bind_vpn.py would protect downloads if the VPN drops.
4. If the VPN is active, call add_torrent with the chosen result_id.

Be concise. State which release you chose and why (resolution / seeders / age),
then report the outcome. If nothing good is found, say so plainly.
"""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_torrents",
        "description": (
            "Search configured indexers for torrents and return the top "
            "candidates, already ranked best-first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search terms, e.g. 'Severance S02' or a movie title and year.",
                },
                "media_type": {
                    "type": "string",
                    "enum": ["tv", "movie", "any"],
                    "description": "What kind of content this is.",
                },
            },
            "required": ["query", "media_type"],
        },
    },
    {
        "name": "check_vpn",
        "description": (
            "Check whether the VPN tunnel is active, and whether Deluge's "
            "traffic is bound to it (leak protection if the VPN later drops)."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_torrent",
        "description": (
            "Add a previously-searched torrent to Deluge by its result_id. "
            "Refuses if the VPN is not active."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "result_id": {
                    "type": "string",
                    "description": "The id of a result returned by search_torrents.",
                }
            },
            "required": ["result_id"],
        },
    },
]


class TorrentAgent:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.client = anthropic.Anthropic()
        self.model = config.get("anthropic", {}).get("model", "claude-opus-5")
        self._results: dict[str, search.TorrentResult] = {}
        self._result_meta: dict[str, dict] = {}
        self._search_gen = 0
        self.added: list[dict] = []

    # --- tool implementations ------------------------------------------- #
    def _tool_search(self, query: str, media_type: str) -> str:
        try:
            raw = search.search(query, media_type, self.config)
        except search.SearchError as exc:
            return json.dumps({"error": str(exc)})

        scored = ranking.rank(raw, self.config.get("preferences", {}), media_type)
        # Ids are namespaced per search (s1r1, s2r1, ...) and the registry is
        # append-only. Reusing bare r1... across searches meant a stale id from
        # an earlier search silently resolved to a different torrent.
        self._search_gen += 1
        out = []
        for i, s in enumerate(scored[:_TOP_N]):
            rid = f"s{self._search_gen}r{i + 1}"
            self._results[rid] = s.result
            size_gb = (
                round(s.result.size_bytes / (1024**3), 2)
                if s.result.size_bytes
                else None
            )
            meta = {
                "result_id": rid,
                "title": s.result.title,
                "resolution": s.resolution,
                "codec": s.codec,
                "season": s.season,
                "episode": s.episode,
                "seeders": s.result.seeders,
                "size_gb": size_gb,
                "age_days": round(s.age_days, 1) if s.age_days is not None else None,
                "source": s.result.source,
            }
            self._result_meta[rid] = meta
            out.append(meta)
        if not out:
            return json.dumps({"results": [], "note": "No matching torrents found."})
        return json.dumps({"results": out})

    def _tool_check_vpn(self) -> str:
        vpn_cfg = self.config.get("vpn", {})
        provider = vpn_cfg.get("provider", "pia")
        out = vpn_status(provider, vpn_cfg).as_dict()

        # Under gluetun the containment is the runtime's, not Deluge's: Deluge
        # shares the tunnel container's network namespace and has no second
        # route out. There is no listen_interface to inspect, and inspecting
        # one would read the *host's* core.conf — a different daemon entirely.
        if binding_is_structural(provider):
            out["tunnel_device"] = None
            out["deluge_bound_to_vpn"] = True
            out["binding_note"] = (
                "Deluge shares the VPN container's network namespace, so it "
                "cannot reach the network except through the tunnel. No "
                "interface binding is involved."
            )
            return json.dumps(out)

        # Whether Deluge is actually pinned to the tunnel, not just whether the
        # tunnel is up — an unbound Deluge keeps downloading over the LAN if
        # the VPN drops mid-transfer.
        device = tunnel_device()
        binding = deluge.deluge_binding(self.config)
        out["tunnel_device"] = device
        if binding is None:
            out["deluge_bound_to_vpn"] = None
        else:
            out["deluge_bound_to_vpn"] = bool(
                device and all(binding[k] == device for k in deluge.BINDING_KEYS)
            )
            if not out["deluge_bound_to_vpn"]:
                out["binding_warning"] = (
                    "Deluge is not bound to the VPN tunnel — downloads would "
                    "continue over the LAN if the VPN drops. Run "
                    "scripts/bind_vpn.py to fix. This does not block adding."
                )
        return json.dumps(out)

    def _tool_add_torrent(self, result_id: str) -> str:
        result = self._results.get(result_id)
        if result is None:
            return json.dumps(
                {"error": f"Unknown result_id '{result_id}'. Search again first."}
            )
        vpn_cfg = self.config.get("vpn", {})
        provider = vpn_cfg.get("provider", "pia")
        # Pass the config, not just the provider: the gluetun path needs the
        # control-server url and api_key from it. Without them the request is
        # unauthenticated, comes back 401, and fails closed — so every add on
        # the containerised stack was refused with the VPN plainly running.
        status = vpn_status(provider, vpn_cfg)
        if not status.active:
            return json.dumps(
                {
                    "added": False,
                    "reason": "VPN is not active",
                    "detail": status.detail,
                    "action": "Ask the user to start their VPN before downloading.",
                }
            )
        if not result.link:
            return json.dumps({"error": "This result has no magnet or download URL."})
        try:
            tid = deluge.add_torrent(result.link, self.config)
        except deluge.DelugeError as exc:
            return json.dumps({"added": False, "error": str(exc)})
        meta = self._result_meta.get(result_id, {})
        self.added.append(
            {
                "title": result.title,
                "torrent_id": tid,
                "resolution": meta.get("resolution"),
                "codec": meta.get("codec"),
                "season": meta.get("season"),
                "episode": meta.get("episode"),
                "seeders": result.seeders,
                "size_gb": meta.get("size_gb"),
                "source": result.source,
            }
        )
        return json.dumps(
            {"added": True, "torrent_id": tid, "title": result.title}
        )

    def _dispatch(self, name: str, args: dict[str, Any]) -> str:
        if name == "search_torrents":
            return self._tool_search(args["query"], args["media_type"])
        if name == "check_vpn":
            return self._tool_check_vpn()
        if name == "add_torrent":
            return self._tool_add_torrent(args["result_id"])
        return json.dumps({"error": f"Unknown tool '{name}'."})

    # --- main loop ------------------------------------------------------ #
    def run(self, user_request: str) -> str:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_request}
        ]
        final_text = ""
        for _ in range(_MAX_TURNS):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=16000,
                    thinking={"type": "adaptive"},
                    output_config={"effort": "high"},
                    # Auto-cache the prefix: the loop re-sends tools + system +
                    # the whole history every turn, so repeat turns read ~90%
                    # of their input from cache instead of paying full price.
                    cache_control={"type": "ephemeral"},
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=messages,
                )
            except anthropic.APIConnectionError as exc:
                raise RuntimeError(
                    f"Could not reach the Anthropic API: {exc}"
                ) from exc
            except anthropic.RateLimitError as exc:
                raise RuntimeError(
                    "Anthropic API rate limit hit — wait a moment and re-run."
                ) from exc
            except anthropic.APIStatusError as exc:
                raise RuntimeError(
                    f"Anthropic API error ({exc.status_code}): {exc.message}"
                ) from exc
            messages.append({"role": "assistant", "content": response.content})

            text_parts = [b.text for b in response.content if b.type == "text"]
            if text_parts:
                final_text = "\n".join(text_parts)

            if response.stop_reason == "refusal":
                final_text = final_text or (
                    "The model declined this request (safety refusal)."
                )
                break
            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = self._dispatch(block.name, dict(block.input))
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        }
                    )
            messages.append({"role": "user", "content": tool_results})
        return final_text


def build_agent(config: dict[str, Any]) -> TorrentAgent:
    if anthropic_api_key() is None:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Export it before running the agent."
        )
    return TorrentAgent(config)

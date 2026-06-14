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
from .vpn import vpn_status

_MAX_TURNS = 16
_TOP_N = 8

SYSTEM_PROMPT = """\
You are a torrent-fetching agent. Given a request (usually a TV show or movie),
you find the best matching torrent and add it to the user's Deluge client.

Workflow:
1. Call search_torrents with a clean query and the media_type ("tv", "movie",
   or "any"). Results come back already ranked best-first.
2. Pick the single best candidate. Ranking priorities, in order: resolution
   preference, preferred codec, seeders, recency. Prefer exact season/episode
   matches when the user asked for a specific one.
3. Before adding, the download must go through the VPN. Call check_vpn. If it is
   not active, DO NOT add the torrent — tell the user to start their VPN (PIA)
   first, then stop. (add_torrent also refuses when the VPN is down, as a guard.)
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
        "description": "Check whether the VPN tunnel is currently active.",
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
        self.model = config.get("anthropic", {}).get("model", "claude-opus-4-8")
        self._results: dict[str, search.TorrentResult] = {}

    # --- tool implementations ------------------------------------------- #
    def _tool_search(self, query: str, media_type: str) -> str:
        try:
            raw = search.search(query, media_type, self.config)
        except search.SearchError as exc:
            return json.dumps({"error": str(exc)})

        scored = ranking.rank(raw, self.config.get("preferences", {}), media_type)
        self._results.clear()
        out = []
        for i, s in enumerate(scored[:_TOP_N]):
            rid = f"r{i + 1}"
            self._results[rid] = s.result
            size_gb = (
                round(s.result.size_bytes / (1024**3), 2)
                if s.result.size_bytes
                else None
            )
            out.append(
                {
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
            )
        if not out:
            return json.dumps({"results": [], "note": "No matching torrents found."})
        return json.dumps({"results": out})

    def _tool_check_vpn(self) -> str:
        provider = self.config.get("vpn", {}).get("provider", "pia")
        return json.dumps(vpn_status(provider).as_dict())

    def _tool_add_torrent(self, result_id: str) -> str:
        result = self._results.get(result_id)
        if result is None:
            return json.dumps(
                {"error": f"Unknown result_id '{result_id}'. Search again first."}
            )
        provider = self.config.get("vpn", {}).get("provider", "pia")
        status = vpn_status(provider)
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
            response = self.client.messages.create(
                model=self.model,
                max_tokens=16000,
                thinking={"type": "adaptive"},
                output_config={"effort": "high"},
                system=SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            text_parts = [b.text for b in response.content if b.type == "text"]
            if text_parts:
                final_text = "\n".join(text_parts)

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

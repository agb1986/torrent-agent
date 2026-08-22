"""Post an entry to ai-data-store from code that never goes through Claude Code.

`log_artifact_entry.py` (a global Claude Code PostToolUse hook, outside this
repo) is what normally feeds ai-data-store: it watches Bash-tool stdout for a
JSON artifact path and posts it. That only fires for *interactive* Claude Code
sessions — the unattended layer (server/bot.py, server/pipeline.py, driven by
systemd) runs as plain Python subprocesses and never goes through a Bash tool
call, so nothing it does has ever reached the hook. This module is the direct
equivalent for that path: same entry shape, its own HTTP call.

Deliberately its own credential, not `~/.claude.json` (which a systemd
service has no principled reason to depend on): AI_DATA_STORE_URL (the same
`.../sse` URL shape ai-data-store's MCP config uses) and AI_DATA_STORE_TOKEN.
Put them in `.env.bot` only — never in `.env`, which the interactive
Claude-Code-skill path sources. Setting them there would double-post: once
via the hook, once via this module.

Best-effort throughout: a failed post must never fail a delivered file or a
completed fetch, so every error is swallowed and logged, never raised.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("torrent_agent.ai_data_store")

_TIMEOUT = 5


def _entries_url() -> str | None:
    sse_url = os.environ.get("AI_DATA_STORE_URL", "").strip()
    if not sse_url:
        return None
    return sse_url.rsplit("/sse", 1)[0] + "/entries"


def post_entry(
    source: str,
    description: str,
    keywords: list[str],
    data: dict[str, Any],
) -> None:
    """Best-effort POST of one entry. Never raises."""
    url = _entries_url()
    token = os.environ.get("AI_DATA_STORE_TOKEN", "").strip()
    if not url or not token:
        log.debug("AI_DATA_STORE_URL/TOKEN not set — skipping ai-data-store post")
        return

    payload = {
        "source": source,
        "description": description,
        "keywords": keywords,
        "data": data,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            resp.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log.warning("ai-data-store post failed: %s", exc)

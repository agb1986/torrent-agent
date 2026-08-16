"""Minimal Telegram Bot API client.

Just the three calls the bot needs, over `requests`, rather than a framework —
same rule as the rest of the repo. Long polling means we never accept an
inbound connection, which is the whole reason Telegram was chosen over a
webhook or a web UI.
"""

from __future__ import annotations

from typing import Any

import requests

_API = "https://api.telegram.org"

# Long-poll for slightly less than the read timeout, so a quiet period ends
# with Telegram returning an empty list rather than requests raising.
POLL_SECONDS = 50
_READ_TIMEOUT = POLL_SECONDS + 10


class TelegramError(RuntimeError):
    pass


class TelegramClient:
    def __init__(
        self,
        token: str,
        session: requests.Session | None = None,
        api_base: str = _API,
    ) -> None:
        if not token:
            raise TelegramError("No Telegram bot token. Set TELEGRAM_BOT_TOKEN.")
        self._token = token
        self._base = f"{api_base}/bot{token}"
        self._session = session or requests.Session()

    def _scrub(self, text: str) -> str:
        """Strip the bot token out of anything headed for a log or exception.

        The token is part of every request URL, and requests puts the full URL
        in its exception messages. Unscrubbed, a single connection failure
        writes the credential into the systemd journal, where it outlives the
        incident by a long way — which is exactly how this was found.
        """
        return text.replace(self._token, "<token>") if self._token else text

    def _call(
        self, method: str, http_timeout: float, params: dict[str, Any] | None = None
    ) -> Any:
        # params is a dict rather than **kwargs on purpose: getUpdates takes a
        # parameter literally named "timeout", which collides with the HTTP
        # timeout if these share a namespace.
        try:
            resp = self._session.post(
                f"{self._base}/{method}", json=params or {}, timeout=http_timeout
            )
            body = resp.json()
        except (requests.RequestException, ValueError) as exc:
            # `from None`, not `from exc`: a chained cause prints its own
            # unscrubbed message in the traceback, undoing the redaction.
            raise TelegramError(f"{method} failed: {self._scrub(str(exc))}") from None
        if not body.get("ok"):
            raise TelegramError(f"{method} rejected: {body.get('description', body)}")
        return body.get("result")

    def get_updates(self, offset: int | None = None) -> list[dict[str, Any]]:
        """Long-poll for new messages. Returns [] when nothing arrived."""
        params: dict[str, Any] = {"timeout": POLL_SECONDS}
        if offset is not None:
            params["offset"] = offset
        return self._call("getUpdates", _READ_TIMEOUT, params) or []

    def send_message(self, chat_id: int, text: str) -> None:
        # Telegram rejects messages over 4096 characters outright, which would
        # turn a long agent summary into a silent delivery failure.
        if len(text) > 4000:
            text = text[:3990] + "\n[truncated]"
        self._call("sendMessage", 20, {"chat_id": chat_id, "text": text})

    def delete_webhook(self) -> None:
        """Long polling and a webhook are mutually exclusive; drop any webhook.

        Without this, getUpdates returns a 409 forever if a webhook was ever
        registered for this token — a confusing failure for a bot that has
        never been configured with one.
        """
        self._call("deleteWebhook", 20)

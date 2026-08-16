"""Print the chat ids that have messaged this bot.

Setup helper. Telegram does not show you your own numeric chat id anywhere in
the UI, and the allowlist needs exactly that, so:

    1. put TELEGRAM_BOT_TOKEN in .env.bot
    2. send your bot any message
    3. python -m server.chat_id

Then paste what it prints into TELEGRAM_ALLOWED_CHAT_IDS.

Reads only — it never consumes the update queue with an offset, so running it
does not hide messages from the bot proper.
"""

from __future__ import annotations

import os
import sys

from .telegram import TelegramClient, TelegramError


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set (see .env.bot.example)", file=sys.stderr)
        return 1

    client = TelegramClient(token)
    try:
        client.delete_webhook()
        updates = client.get_updates()
    except TelegramError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    seen: dict[int, str] = {}
    for update in updates:
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat") or {}
        if (cid := chat.get("id")) is not None:
            who = message.get("from", {}).get("username") or chat.get("title") or "?"
            seen[cid] = who

    if not seen:
        print("No messages yet. Send your bot something, then run this again.")
        return 1

    print("Chat ids that have messaged this bot:")
    for cid, who in seen.items():
        print(f"  {cid}   ({who})")
    print("\nAdd to .env.bot:")
    print(f"  TELEGRAM_ALLOWED_CHAT_IDS={' '.join(str(c) for c in seen)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Finish Telegram setup: find your chat id, save it to .env, send a test message.

Before running:
  1. In Telegram, message @BotFather: /newbot, pick a name and a username ending in "bot".
  2. Put the token it gives you in .env as TELEGRAM_BOT_TOKEN=...
  3. Open your new bot in Telegram, press Start and send it any message.
Then:
    python scripts/setup_telegram.py
The token is never printed.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

from papertrader.notify import send_telegram  # noqa: E402

ENV = ROOT / ".env"


def api(token: str, method: str) -> dict:
    with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/{method}", timeout=15) as resp:
        return json.load(resp)


def save_env(key: str, value: str) -> None:
    text = ENV.read_text(encoding="utf-8") if ENV.exists() else ""
    line = f"{key}={value}"
    if re.search(rf"^{key}=.*$", text, flags=re.M):
        text = re.sub(rf"^{key}=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + f"\n{line}\n"
    ENV.write_text(text, encoding="utf-8")


def main() -> int:
    load_dotenv(ENV)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN is not set in .env. See the steps at the top of this file.")
        return 1
    try:
        me = api(token, "getMe")["result"]
    except Exception as exc:
        print(f"Telegram rejected the token ({exc}). Copy it again from @BotFather.")
        return 1
    print(f"Token OK: bot @{me['username']}")

    updates = api(token, "getUpdates").get("result", [])
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("first_name") or chat.get("title") or str(chat["id"])
    if not chats:
        print(f"No messages found. Open https://t.me/{me['username']} , press Start, send 'hi', then re-run.")
        return 1
    chat_id, name = list(chats.items())[-1]
    save_env("TELEGRAM_CHAT_ID", str(chat_id))
    print(f"Chat found ({name}); TELEGRAM_CHAT_ID={chat_id} saved to .env")

    ok = send_telegram("PaperTrader is connected. You'll get a summary here after each daily run "
                       "(PAPER MONEY ONLY).", token, str(chat_id))
    print("Test message sent - check Telegram." if ok else "Sending the test message failed.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

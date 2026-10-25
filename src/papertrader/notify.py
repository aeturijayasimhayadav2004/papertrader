"""Run notifications via a Telegram bot (free). Never raises: a failed message must not fail a run.

Setup: talk to @BotFather -> /newbot -> copy the token into TELEGRAM_BOT_TOKEN;
send your bot a message, then open https://api.telegram.org/bot<token>/getUpdates
and copy chat.id into TELEGRAM_CHAT_ID.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Any

log = logging.getLogger(__name__)
MAX_LEN = 3900  # Telegram limit is 4096 characters


def format_summary(summary: Any) -> str:
    lines = [f"PaperTrader (PAPER MONEY) - {summary.status.upper()}",
             f"As of {summary.as_of_date or '-'}; {len(summary.days_processed)} day(s) processed.",
             summary.message.strip()]
    acted = [d for d in summary.decisions if d["action"] in ("BUY", "SELL")]
    for d in acted[:15]:
        lines.append(f"- {d['action']} {d['ticker']} ({d['strategy']}): {d['reason'][:180]}")
    if len(acted) > 15:
        lines.append(f"... and {len(acted) - 15} more orders.")
    for e in getattr(summary, "events", [])[:15]:
        lines.append(f"* {e}")
    return "\n".join(lines)[:MAX_LEN]


def send_telegram(text: str, token: str | None = None, chat_id: str | None = None, timeout: float = 10.0) -> bool:
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False
    body = json.dumps({"chat_id": chat_id, "text": text[:MAX_LEN], "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:  # network errors, bad token, ...
        log.warning("Telegram notification failed: %s", exc)
        return False

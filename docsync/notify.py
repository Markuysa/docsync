"""Notification fan-out to log, Slack incoming webhook and Telegram."""
from __future__ import annotations

import html
import json
import logging
from datetime import datetime, timezone

import httpx

from .config import Settings

log = logging.getLogger("docsync.notify")


class Notifier:
    def __init__(self, s: Settings, telegram=None):
        self.s = s
        self.telegram = telegram          # docsync.telegram.TelegramBot or None

    def send(self, payload: dict) -> dict:
        text = str(payload.get("text", ""))
        out = {"log": True, "slack": None, "telegram": None}
        line = json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **payload}, ensure_ascii=False)
        with open(self.s.data_dir / "notifications.log", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        log.info("NOTIFY %s", line)

        if self.s.slack_webhook_url:
            try:
                r = httpx.post(self.s.slack_webhook_url, json={"text": text}, timeout=15)
                out["slack"] = r.status_code
            except httpx.HTTPError as e:
                out["slack"] = f"error: {e}"

        if self.telegram:
            try:
                buttons = None
                if payload.get("kind") == "proposed" and payload.get("runId"):
                    buttons = self.telegram.approval_keyboard(payload["runId"], payload.get("mrUrl"))
                msg = self.telegram.send(html.escape(text), buttons)
                out["telegram"] = msg.get("message_id")
            except Exception as e:
                out["telegram"] = f"error: {e}"
        return out

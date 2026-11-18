"""Phone pings via a Discord webhook (or any URL that accepts {"content": ...} JSON)."""
from __future__ import annotations

import logging
import os

import requests

LOG = logging.getLogger("clipbot.notify")


def notify(text: str):
    url = os.getenv("NOTIFY_WEBHOOK_URL", "")
    if not url:
        LOG.info("NOTIFY: %s", text)
        return
    try:
        requests.post(url, json={"content": text[:1900]}, timeout=15).raise_for_status()
    except Exception as exc:
        LOG.warning("Notification failed: %s", exc)

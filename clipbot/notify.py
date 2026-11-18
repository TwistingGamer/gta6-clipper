"""Phone pings via a Discord webhook (or any URL that accepts {"content": ...} JSON)."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import requests

LOG = logging.getLogger("clipbot.notify")
LIMIT = 1990  # Discord message limit is 2000 characters


def _url() -> str:
    return os.getenv("NOTIFY_WEBHOOK_URL", "")


def notify(text: str, file: Path | None = None):
    """One message. With `file`, the video is attached when it fits Discord's upload limit
    (NOTIFY_MAX_MB, default 10 MB for servers without boosts)."""
    url = _url()
    if not url:
        LOG.info("NOTIFY: %s", text)
        return
    try:
        if file and file.is_file() and file.stat().st_size <= float(os.getenv("NOTIFY_MAX_MB", "10")) * 1024 * 1024:
            with file.open("rb") as fh:
                r = requests.post(url, data={"payload_json": json.dumps({"content": text[:LIMIT]})},
                                  files={"files[0]": (file.name, fh, "video/mp4")}, timeout=120)
        else:
            if file and file.is_file():
                text += f"\n(video too big to attach: {file.stat().st_size / 1e6:.0f} MB, it's in {file.parent})"
            r = requests.post(url, json={"content": text[:LIMIT]}, timeout=15)
        r.raise_for_status()
    except Exception as exc:
        LOG.warning("Notification failed: %s", exc)


def notify_copy(text: str):
    """Text meant to be copied as-is (a caption): sent alone, so long-press > Copy Text gives exactly it."""
    for i in range(0, len(text), LIMIT):
        notify(text[i:i + LIMIT])

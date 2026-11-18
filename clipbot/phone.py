"""Review clips from your phone through Discord.

Every new clip is sent to your Discord channel with a small copy of the video (under 10 MB, so the
webhook can attach it) that you can watch and save. With a Discord bot token in .env, react to that
message with ✅ to approve or ❌ to reject; the bot checks the reactions every cycle. No extra installs:
it only reads reactions through Discord's normal web API.

  DISCORD_BOT_TOKEN=...      bot token (Discord Developer Portal > your app > Bot > Reset Token)
  DISCORD_USER_ID=...        your own Discord user ID, so only your reactions count
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from urllib.parse import quote

import requests

from . import db
from .config import DATA, FFMPEG, FFPROBE, TMP, env_bool
from .notify import max_upload_bytes, notify

LOG = logging.getLogger("clipbot.phone")
MSG_FILE = DATA / "review_messages.json"   # clip id -> [channel id, message id]
APPROVE, REJECT = "✅", "❌"
API = "https://discord.com/api/v10"


def _load() -> dict:
    try:
        return json.loads(MSG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(data: dict):
    MSG_FILE.parent.mkdir(parents=True, exist_ok=True)
    MSG_FILE.write_text(json.dumps(data), encoding="utf-8")


def bot_ready() -> bool:
    return bool(os.getenv("DISCORD_BOT_TOKEN"))


def phone_copy(src: Path) -> Path | None:
    """720p copy sized to fit the Discord upload limit (the original stays full quality for posting)."""
    if not src.is_file():
        return None
    if src.stat().st_size <= max_upload_bytes():
        return src
    try:
        dur = float(subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
                                   capture_output=True, text=True, check=True).stdout.strip())
    except Exception:
        dur = 60.0
    budget_bits = max_upload_bytes() * 8 * 0.9
    video_k = max(300, int(budget_bits / max(dur, 1) / 1000) - 96)
    TMP.mkdir(parents=True, exist_ok=True)
    out = TMP / f"phone_{src.stem}.mp4"
    try:
        subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(src), "-vf", "scale=720:-2",
                        "-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{video_k}k", "-maxrate", f"{video_k}k",
                        "-bufsize", f"{video_k * 2}k", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out)],
                       check=True)
    except Exception as exc:
        LOG.warning("phone copy failed: %s", exc)
        return None
    return out


def announce_new_clip(item: dict):
    """Ping: what the clip is, the phone copy to watch/save, and how to approve it."""
    if not env_bool("NOTIFY_NEW_CLIPS", True) or item.get("status") != "pending_review":
        return
    reasons = item.get("reasons") if isinstance(item.get("reasons"), dict) else {}
    kind = "🆕 FIRST LOOK" if reasons.get("first_look") else "🎬 New clip"
    how = (f"React {APPROVE} to approve or {REJECT} to reject."
           if bot_ready() else f"Approve: `python -m clipbot approve {item['id']}`  ·  reject: `python -m clipbot reject {item['id']}`")
    text = (f"{kind} · **{item.get('streamer') or item.get('campaign')}** · score {int(min(item.get('score') or 0, 0.99) * 100)}\n"
            f"{item.get('title', '')}\n`{item['id']}` · {item.get('campaign')}\n{how}")
    small = phone_copy(Path(item.get("output") or ""))
    try:
        msg = notify(text, file=small)
    finally:
        if small and small.parent == TMP:
            small.unlink(missing_ok=True)
    if msg and msg.get("id") and msg.get("channel_id"):
        data = _load()
        data[item["id"]] = [msg["channel_id"], msg["id"]]
        _save(data)


def _reacted(channel: str, message: str, emoji: str) -> bool:
    r = requests.get(f"{API}/channels/{channel}/messages/{message}/reactions/{quote(emoji)}",
                     headers={"Authorization": f"Bot {os.getenv('DISCORD_BOT_TOKEN')}"}, timeout=15)
    if r.status_code == 404:
        return False
    r.raise_for_status()
    me = os.getenv("DISCORD_USER_ID", "").strip()
    return any((u.get("id") == me) if me else not u.get("bot") for u in r.json())


def check_reactions() -> int:
    """Apply ✅ / ❌ reactions on new-clip messages. Returns how many clips changed."""
    if not bot_ready():
        return 0
    from .publish import delete_clip_files
    from .sources import load_campaigns
    data, changed = _load(), 0
    for clip_id, (channel, message) in list(data.items()):
        item = db.get_clip(clip_id)
        if not item or item["status"] != "pending_review":
            data.pop(clip_id, None)
            continue
        try:
            if _reacted(channel, message, REJECT):
                db.set_status(clip_id, "rejected")
                delete_clip_files(item)
                notify(f"{REJECT} `{clip_id}` rejected and deleted.")
            elif _reacted(channel, message, APPROVE):
                c = load_campaigns().get(item.get("campaign") or "")
                status = "awaiting_streamer" if c and c.clip_preapproval else "approved"
                db.set_status(clip_id, status)
                notify(f"{APPROVE} `{clip_id}` approved" + (": the campaign must OK it first." if status != "approved"
                                                           else ": it goes out in the next free posting slot."))
            else:
                continue
        except requests.HTTPError as exc:
            LOG.warning("Discord reaction check failed (%s): is the bot in the server with Read Message History?", exc)
            break
        except Exception as exc:
            LOG.warning("Discord reaction check failed: %s", exc)
            break
        data.pop(clip_id, None)
        changed += 1
    _save(data)
    return changed

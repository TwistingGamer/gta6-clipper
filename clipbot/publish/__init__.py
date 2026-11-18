"""Posting scheduler: spreads approved clips across platforms within daily limits."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import db
from ..config import env_bool, env_int, env_list
from ..sources import load_sources

LOG = logging.getLogger("clipbot.publish")
PLATFORMS = ("youtube", "tiktok", "instagram")


def uploader(platform: str):
    if platform == "youtube":
        from . import youtube as mod
    elif platform == "tiktok":
        from . import tiktok as mod
    elif platform == "instagram":
        from . import instagram as mod
    else:
        raise ValueError(f"unknown platform {platform}")
    return mod.post


def still_authorized(item: dict) -> tuple[bool, str]:
    """Re-check permission at post time: it may have been revoked since the clip was made."""
    src = load_sources().get((item.get("streamer") or "").lower())
    if not src:
        return False, f"streamer {item.get('streamer')!r} no longer in sources.yaml"
    return src.authorized()


def post_one(clip_id: str, platform: str) -> str:
    item = db.get_clip(clip_id)
    if not item:
        raise KeyError(clip_id)
    if item["status"] != "approved":
        raise RuntimeError(f"clip {clip_id} is {item['status']}, approve it first")
    ok, why = still_authorized(item)
    if not ok:
        raise PermissionError(f"not posting {clip_id}: {why}")
    if not Path(item["output"]).is_file():
        raise FileNotFoundError(item["output"])
    db.record_post(clip_id, platform, "processing")
    try:
        remote = uploader(platform)(item)
    except Exception as exc:
        db.record_post(clip_id, platform, "failed", error=str(exc)[:500])
        raise
    db.record_post(clip_id, platform, "posted", remote_id=remote)
    LOG.info("Posted %s to %s: %s", clip_id, platform, remote)
    return remote


def enabled_platforms() -> list[str]:
    if not env_bool("AUTO_POST"):
        return []
    return [p for p in env_list("POST_PLATFORMS") if p in PLATFORMS]


def post_due() -> list[tuple[str, str, str]]:
    """Post at most one clip per enabled platform per call, honouring per-day caps and spacing."""
    done = []
    daily = max(1, env_int("POSTS_PER_DAY", 3))
    now = datetime.now(timezone.utc)
    for platform in enabled_platforms():
        count, latest = db.posts_since(platform, (now - timedelta(hours=24)).isoformat())
        if count >= daily:
            continue
        if latest and (now - datetime.fromisoformat(latest)).total_seconds() < 86400 / daily:
            continue
        item = db.next_for_platform(platform)
        if not item:
            continue
        try:
            done.append((item["id"], platform, post_one(item["id"], platform)))
        except PermissionError as exc:
            LOG.warning("%s", exc)
            db.set_status(item["id"], "rejected")
        except Exception:
            LOG.exception("Posting %s to %s failed; will retry later", item["id"], platform)
    return done

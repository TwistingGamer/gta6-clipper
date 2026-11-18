"""Posting: spreads approved clips across platforms within warm-up limits and campaign rules.

Two modes per platform (POST_MODE_<PLATFORM>=manual|api, default manual):
  manual  the clip + caption are copied to output/ready/<platform>/ and you get a ping;
          you post from your phone, then record the link with `python -m clipbot posted`.
          Needed while your accounts are new and the API apps are not yet approved
          (TikTok/YouTube keep unaudited API uploads private).
  api     uploads through the official API.
"""
from __future__ import annotations

import logging
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .. import db
from ..config import OUTPUT, env_bool, env_int, env_list
from ..notify import notify
from ..sources import load_campaigns

import os

LOG = logging.getLogger("clipbot.publish")
PLATFORMS = ("youtube", "tiktok", "instagram", "x")
READY = OUTPUT / "ready"


def mode(platform: str) -> str:
    m = os.getenv(f"POST_MODE_{platform.upper()}", "manual").lower()
    return "api" if m == "api" and platform != "x" else "manual"


def uploader(platform: str):
    if platform == "youtube":
        from . import youtube as mod
    elif platform == "tiktok":
        from . import tiktok as mod
    elif platform == "instagram":
        from . import instagram as mod
    else:
        raise ValueError(f"no API uploader for {platform}")
    return mod.post


def daily_cap() -> int:
    """Warm-up: brand-new accounts start at 1 post/day per platform and ramp up weekly."""
    cap = max(1, env_int("POSTS_PER_DAY", 3))
    started = os.getenv("ACCOUNTS_STARTED", "")
    if started:
        try:
            weeks = max(0, (date.today() - date.fromisoformat(started)).days // 7)
            cap = min(cap, 1 + weeks)
        except ValueError:
            pass
    return cap


def check_clip(item: dict, platform: str):
    campaigns = load_campaigns()
    c = campaigns.get(item.get("campaign") or "")
    if not c:
        raise PermissionError(f"campaign {item.get('campaign')!r} no longer in campaigns.yaml")
    ok, why = c.authorized()
    if not ok:
        raise PermissionError(f"campaign {c.id}: {why}")
    if platform not in c.platforms:
        raise PermissionError(f"campaign {c.id} doesn't allow {platform}")
    if c.budget_left() == 0:
        raise PermissionError(f"campaign {c.id}: budget used up")
    if item["status"] != "approved":
        raise PermissionError(f"clip {item['id']} is {item['status']}, approve it first")
    if not Path(item["output"]).is_file():
        raise FileNotFoundError(item["output"])
    return c


def submit_targets(item: dict, platform: str | None = None) -> list[str]:
    """Every campaign this clip should be submitted to (e.g. CORE pool + Lacy + Jason),
    limited to campaigns that pay on `platform`."""
    reasons = item.get("reasons") or {}
    targets = (reasons.get("submit_to") if isinstance(reasons, dict) else None) or [item.get("campaign")]
    if platform:
        campaigns = load_campaigns()
        targets = [t for t in targets if t == item.get("campaign") or (t in campaigns and platform in campaigns[t].platforms)]
    return targets


def _submit_note(c, item: dict | None = None, platform: str | None = None) -> str:
    campaigns = load_campaigns()
    names = [campaigns[t].display if t in campaigns else t for t in (submit_targets(item, platform) if item else [c.id])]
    where = ", ".join(f"**{n}**" for n in names)
    if c.submit_within_minutes:
        return f"⏱ Submit the link to {where} within {c.submit_within_minutes} min of posting!"
    return f"Submit the link to {where} on the campaign dashboard."


def stage_manual(item: dict, platform: str, c) -> Path:
    dest_dir = READY / platform
    dest_dir.mkdir(parents=True, exist_ok=True)
    video = dest_dir / f"{c.id}_{item['id']}.mp4"
    shutil.copy2(item["output"], video)
    caption = f"{item['title']}\n\n{item['description']}"
    if platform == "x":
        # X posts are capped at 280 characters: title + required tags only.
        caption = f"{item['title']} {' '.join(item.get('hashtags', []))}".strip()[:280]
    video.with_suffix(".txt").write_text(
        f"PLATFORM: {platform}\nCAMPAIGN: {c.display}\n\nTITLE:\n{item['title']}\n\n"
        f"CAPTION / DESCRIPTION:\n{item['description'] if platform == 'youtube' else caption}\n\n"
        f"AFTER POSTING: python -m clipbot posted {item['id']} {platform} <link>\n"
        + (f"BIO LINK REQUIRED: {c.bio_link}\n" if c.bio_link else ""),
        encoding="utf-8",
    )
    db.record_post(item["id"], platform, "staged")
    notify(f"📲 Ready to post on **{platform}** ({c.display}): `{video.name}`\n{item['title']}\n{_submit_note(c, item, platform)}")
    return video


def post_one(clip_id: str, platform: str) -> str:
    item = db.get_clip(clip_id)
    if not item:
        raise KeyError(clip_id)
    c = check_clip(item, platform)
    if mode(platform) == "manual":
        return str(stage_manual(item, platform, c))
    db.record_post(clip_id, platform, "processing")
    try:
        remote_id, url = uploader(platform)(item)
    except Exception as exc:
        db.record_post(clip_id, platform, "failed", error=str(exc)[:500])
        raise
    db.record_post(clip_id, platform, "posted", remote_id=remote_id, url=url)
    notify(f"✅ Posted on **{platform}**: {url}\n{_submit_note(c, item, platform)}")
    return url


def mark_posted(clip_id: str, platform: str, url: str):
    item = db.get_clip(clip_id)
    if not item:
        raise KeyError(clip_id)
    db.record_post(clip_id, platform, "posted", url=url)
    c = load_campaigns().get(item.get("campaign") or "")
    if c:
        print(_submit_note(c, item, platform).replace("**", ""))


def post_due() -> list[tuple[str, str, str]]:
    """At most one clip per platform per call, within the warm-up cap, spacing and posting hours."""
    if not env_bool("AUTO_POST"):
        return []
    done = []
    cap = daily_cap()
    now = datetime.now(timezone.utc)
    hour = datetime.now().hour  # local time for campaign posting hours
    campaigns = load_campaigns()
    for platform in [p for p in env_list("POST_PLATFORMS") if p in PLATFORMS]:
        count, latest = db.posts_since(platform, (now - timedelta(hours=24)).isoformat())
        if count >= cap:
            continue
        if latest and (now - datetime.fromisoformat(latest)).total_seconds() < 86400 / cap:
            continue
        # Best campaign first (easiest + biggest payout), then best clip.
        pending = db.unposted_for_platform(platform)
        pending.sort(key=lambda it: (-(campaigns[it["campaign"]].priority() if it.get("campaign") in campaigns else 0),
                                     -(it.get("score") or 0)))
        for item in pending:
            c = campaigns.get(item.get("campaign") or "")
            if not c or platform not in c.platforms or not c.can_post_now(hour):
                continue
            try:
                done.append((item["id"], platform, post_one(item["id"], platform)))
            except PermissionError as exc:
                LOG.warning("%s", exc)
                continue
            except Exception:
                LOG.exception("Posting %s to %s failed; will retry later", item["id"], platform)
            break
    return done

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
from ..notify import notify, notify_copy
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


LAUNCH_WEEK_END = date(2026, 11, 26)   # GTA 6 launch week: lots of content, post more


def _cap_file() -> Path:
    from ..config import DATA
    return DATA / "posting.json"


def read_cap_settings() -> dict:
    import json
    try:
        return json.loads(_cap_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_cap_settings(per_day: int | None = None, gap_minutes: int | None = None):
    import json
    data = read_cap_settings()
    if per_day is not None:
        data["per_day"] = max(0, per_day)
    if gap_minutes is not None:
        data["gap_minutes"] = max(0, gap_minutes)
    _cap_file().parent.mkdir(parents=True, exist_ok=True)
    _cap_file().write_text(json.dumps(data), encoding="utf-8")


def daily_cap() -> int:
    """Posts per platform per day. `set-cap N` wins. Otherwise: 5 during GTA 6 launch week,
    then the warm-up (1/day in week 1 after ACCOUNTS_STARTED, +1 each week) up to POSTS_PER_DAY."""
    saved = read_cap_settings().get("per_day")
    if saved is not None:
        return int(saved)
    if date.today() <= LAUNCH_WEEK_END:
        return env_int("LAUNCH_POSTS_PER_DAY", 5)
    cap = max(1, env_int("POSTS_PER_DAY", 3))
    started = os.getenv("ACCOUNTS_STARTED", "")
    if started:
        try:
            weeks = max(0, (date.today() - date.fromisoformat(started)).days // 7)
            cap = min(cap, 1 + weeks)
        except ValueError:
            pass
    return cap


def min_gap_seconds(cap: int) -> float:
    """Spacing between two posts on the same platform: at least MIN_GAP_MINUTES (default 2 h)."""
    gap = read_cap_settings().get("gap_minutes")
    gap = env_int("MIN_GAP_MINUTES", 120) if gap is None else int(gap)
    return gap * 60


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
    targets = [t for t in (submit_targets(item, platform) if item else [c.id])
               if t not in campaigns or campaigns[t].submit]
    if not targets:
        return "Own-channel clip: nothing to submit."
    names = [campaigns[t].display if t in campaigns else t for t in targets]
    where = ", ".join(f"**{n}**" for n in names)
    if c.submit_within_minutes:
        return f"⏱ Submit the link to {where} within {c.submit_within_minutes} min of posting!"
    return f"Submit the link to {where} on the campaign dashboard."


def captions_for(item: dict, platform: str) -> dict[str, str]:
    """What to paste on each platform: {label: text}. Hashtags are already in the description."""
    title, desc = item["title"], item["description"]
    tags = " ".join(item.get("hashtags") or [])
    if platform == "youtube":
        # Shorts: the title is its own field (100 chars max); #Shorts helps it land in the Shorts feed.
        return {"TITLE": title[:100], "DESCRIPTION": desc + ("" if "#shorts" in desc.lower() else " #Shorts")}
    if platform == "x":
        # X posts are capped at 280 characters: title + the hashtags that fit.
        out = title
        for t in tags.split():
            if len(out) + 1 + len(t) <= 280:
                out += " " + t
        return {"POST": out}
    core = title.removesuffix(" #ad").strip()
    body = desc if core and core in desc[:200] else f"{title}\n\n{desc}"  # no title twice
    return {"CAPTION": body[:2200]}


def stage_manual(item: dict, platform: str, c) -> Path:
    dest_dir = READY / platform
    dest_dir.mkdir(parents=True, exist_ok=True)
    video = dest_dir / f"{c.id}_{item['id']}.mp4"
    shutil.copy2(item["output"], video)
    texts = captions_for(item, platform)
    video.with_suffix(".txt").write_text(
        f"PLATFORM: {platform}\nCAMPAIGN: {c.display}\n\n"
        + "".join(f"{label}:\n{text}\n\n" for label, text in texts.items())
        + f"AFTER POSTING: python -m clipbot posted {item['id']} {platform} <link>\n"
        + (f"BIO LINK REQUIRED: {c.bio_link}\n" if c.bio_link else ""),
        encoding="utf-8",
    )
    db.record_post(item["id"], platform, "staged")
    # Ping: the video, then each caption as its own message so it copies cleanly on a phone.
    ad = "\n⚠️ Turn on the paid-partnership / branded-content label." if item.get("ad_disclosure") else ""
    bio = f"\n🔗 Bio link must be: {c.bio_link}" if c.bio_link else ""
    notify(f"📲 **{platform.upper()}** ({c.display}) `{video.name}`{ad}{bio}\n{_submit_note(c, item, platform)}\n"
           f"After posting: `python -m clipbot posted {item['id']} {platform} LINK`", file=video)
    for label, text in texts.items():
        if len(texts) > 1:
            notify(f"⬇️ {platform} {label.lower()}")
        notify_copy(text)
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
    if not still_to_post(item):
        finish_clip(item)
    return url


def mark_posted(clip_id: str, platform: str, url: str):
    item = db.get_clip(clip_id)
    if not item:
        raise KeyError(clip_id)
    db.record_post(clip_id, platform, "posted", url=url)
    c = load_campaigns().get(item.get("campaign") or "")
    if c:
        print(_submit_note(c, item, platform).replace("**", ""))
    left = still_to_post(item)
    if left:
        print(f"Still to post: {', '.join(left)}")
    elif finish_clip(item):
        print("Posted everywhere: video files deleted (links and details are kept).")


def target_platforms(item: dict) -> list[str]:
    """Platforms this clip is meant for: the campaign's platforms you post to (POST_PLATFORMS)."""
    c = load_campaigns().get(item.get("campaign") or "")
    allowed = c.platforms if c else list(PLATFORMS)
    mine = [p for p in env_list("POST_PLATFORMS") if p in PLATFORMS] or list(PLATFORMS)
    return [p for p in mine if p in allowed]


def still_to_post(item: dict) -> list[str]:
    done = db.posted_platforms(item["id"])
    return [p for p in target_platforms(item) if p not in done]


def delete_clip_files(item: dict) -> int:
    """Remove a clip's video files (rendered clip, captions, staged copies). The database row and links stay."""
    if env_bool("KEEP_POSTED_FILES"):
        return 0
    removed = 0
    out = Path(item.get("output") or "")
    paths = [out, out.with_suffix(".srt"), out.with_suffix(".ass"), out.with_suffix(".cap.ass")] if item.get("output") else []
    for platform in PLATFORMS:
        staged = READY / platform / f"{item.get('campaign')}_{item['id']}.mp4"
        paths += [staged, staged.with_suffix(".txt")]
    for p in paths:
        try:
            if p.is_file():
                p.unlink()
                removed += 1
        except OSError as exc:
            LOG.warning("could not delete %s: %s", p, exc)
    # The saved live moment it came from, once no other clip still needs it.
    from ..fetch import LIVE_DIR
    src = Path(item.get("source") or "")
    if src.is_file() and LIVE_DIR.resolve() in src.resolve().parents:
        busy = [r for r in db.clips_for_source(item["source"])
                if r["id"] != item["id"] and r["status"] not in ("done", "rejected", "sample")]
        if not busy:
            for p in (src, src.with_suffix(".json")):
                p.unlink(missing_ok=True)
            removed += 1
    return removed


def finish_clip(item: dict) -> bool:
    """Posted on every platform it was meant for: mark done and free the disk space."""
    db.set_status(item["id"], "done")
    return delete_clip_files(item) > 0


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
        if latest and (now - datetime.fromisoformat(latest)).total_seconds() < min_gap_seconds(cap):
            continue
        # Best campaign first (easiest + biggest payout), then best clip.
        pending = db.unposted_for_platform(platform)
        pending.sort(key=lambda it: (not (isinstance(it.get("reasons"), dict) and it["reasons"].get("first_look")),
                                     -(campaigns[it["campaign"]].priority() if it.get("campaign") in campaigns else 0),
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

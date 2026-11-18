"""Command line and 24/7 daemon.

  python -m clipbot run                       record/download, clip, stage or post approved clips, forever
  python -m clipbot campaigns                 which campaigns are active, why, and their priority
  python -m clipbot fetch                     download approved YouTube footage now
  python -m clipbot scan                      clip any new footage now
  python -m clipbot review [--all]            list clips waiting for review
  python -m clipbot approve ID [--streamer-approved]
  python -m clipbot reject ID
  python -m clipbot post ID PLATFORM          stage (manual mode) or upload (api mode) one clip now
  python -m clipbot posted ID PLATFORM URL    record the link after you posted by hand
  python -m clipbot report [--days N]         posted links per campaign, for submitting
  python -m clipbot auth youtube|tiktok       one-time sign-in for API posting
"""
from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from . import db
from .chat import ChatLogger
from .config import env_bool, env_int
from .fetch import capture_segment, category_ok, fetch_campaign, live_info
from .pipeline import scan_all
from .publish import daily_cap, mark_posted, mode, post_due, post_one
from .sources import LIVE_TYPES, authorized_campaigns, load_campaigns
from .twitch import Twitch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOG = logging.getLogger("clipbot")


class LiveWatcher:
    """Tracks live inputs: chat loggers (Twitch) and recording threads."""

    def __init__(self):
        self.twitch = Twitch()
        self.chats: dict[str, ChatLogger] = {}
        self.captures: dict[str, threading.Event] = {}

    def _is_live(self, campaign, inp, twitch_online) -> bool:
        if inp.type == "twitch" and self.twitch.configured:
            s = twitch_online.get(inp.login)
            return bool(s) and category_ok(campaign, s.get("game_name"))
        meta = live_info(inp)  # Kick / YouTube live (or Twitch without API keys)
        return meta is not None and category_ok(campaign, meta.get("category"))

    def tick(self, campaigns):
        live_inputs = [(c, i) for c in campaigns.values() for i in c.inputs if i.type in LIVE_TYPES]
        twitch_logins = [i.login for _, i in live_inputs if i.type == "twitch"]
        try:
            online = self.twitch.online(twitch_logins) if (twitch_logins and self.twitch.configured) else {}
        except Exception as exc:
            LOG.warning("Twitch status check failed: %s", exc)
            online = {}
        wanted = set()
        for c, inp in live_inputs:
            key = f"{c.id}:{inp.type}:{inp.login}"
            if not self._is_live(c, inp, online):
                continue
            wanted.add(key)
            if inp.type == "twitch" and inp.login not in self.chats:
                LOG.info("%s is live (%s)", inp.login, c.id)
                self.chats[inp.login] = ChatLogger(inp.login)
                self.chats[inp.login].start()
            if c.allow_recording and env_bool("CAPTURE_STREAMS") and key not in self.captures:
                flag = threading.Event()
                flag.set()
                threading.Thread(target=self._record, args=(c, inp, flag), daemon=True).start()
                self.captures[key] = flag
        for key in list(self.captures):
            if key not in wanted:
                self.captures.pop(key).clear()
        live_logins = {k.split(":")[2] for k in wanted if k.split(":")[1] == "twitch"}
        for login in list(self.chats):
            if login not in live_logins:
                self.chats.pop(login).stop()

    @staticmethod
    def _record(campaign, inp, flag: threading.Event):
        while flag.is_set():
            try:
                LOG.info("Captured %s", capture_segment(campaign, inp, env_int("SEGMENT_SECONDS", 300)))
            except Exception as exc:
                LOG.warning("Capture failed for %s: %s", inp.url, exc)
                time.sleep(30)


def daemon():
    db.init_db()
    watcher = LiveWatcher()
    last_fetch = 0.0
    while True:
        try:
            campaigns = authorized_campaigns()  # re-read every cycle: edits apply live
            watcher.tick(campaigns)
            if time.time() - last_fetch > env_int("FETCH_EVERY_MINUTES", 30) * 60:
                for c in campaigns.values():
                    fetch_campaign(c)
                last_fetch = time.time()
            made = scan_all(watcher.twitch if watcher.twitch.configured else None)
            if made:
                LOG.info("%d new clip(s) waiting for review", made)
            for clip_id, platform, result in post_due():
                LOG.info("%s -> %s: %s", clip_id, platform, result)
        except Exception:
            LOG.exception("Daemon cycle failure")
        time.sleep(max(15, env_int("POLL_SECONDS", 60)))


def cmd_campaigns():
    rows = []
    for c in sorted(load_campaigns().values(), key=lambda c: -c.priority()):
        ok, why = c.authorized()
        rows.append({
            "id": c.id, "priority": c.priority(), "active": ok and not c.problems(),
            "reason": "; ".join(c.problems()) or why, "inputs": [f"{i.type}:{i.target}" for i in c.inputs],
            "platforms": c.platforms, "cpm": c.cpm, "max_payout": c.max_payout,
            "budget_left": c.budget_left(), "recording": c.allow_recording, "ai_text": c.ai_text_allowed,
            "review": c.require_review, "post_hours": c.post_hours, "bio_link": c.bio_link,
        })
    print(json.dumps(rows, indent=2) if rows else "No campaigns.yaml (copy campaigns.example.yaml).")
    print(f"\nPosting: up to {daily_cap()} per platform per day right now. Modes: "
          + ", ".join(f"{p}={mode(p)}" for p in ("tiktok", "instagram", "youtube")))


def cmd_approve(clip_id: str, streamer_ok: bool):
    item = db.get_clip(clip_id)
    if not item:
        raise SystemExit(f"No clip {clip_id}")
    c = load_campaigns().get(item.get("campaign") or "")
    if c and c.clip_preapproval and not streamer_ok:
        db.set_status(clip_id, "awaiting_streamer")
        print(f"{clip_id}: the campaign must OK this clip first. Once it does: approve {clip_id} --streamer-approved")
    else:
        db.set_status(clip_id, "approved")
        print(f"{clip_id}: approved; it goes out in the next free posting slot")


def cmd_report(days: int):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = db.posts_between(since)
    if not rows:
        print("No posts in that period.")
        return
    by = {}
    for r in rows:
        by.setdefault(r["campaign"], []).append(r)
    campaigns = load_campaigns()
    for cid, posts in by.items():
        c = campaigns.get(cid)
        print(f"\n== {c.display if c else cid} ==")
        if c and c.bio_link:
            print(f"   bio link required: {c.bio_link}")
        for p in posts:
            print(f"   {p['posted_at'][:16]}  {p['platform']:<9} {p['url'] or '(no link recorded)'}  {p['title']}")


def main():
    ap = argparse.ArgumentParser(description="Campaign clip bot (approved footage only)")
    sp = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "scan", "fetch", "campaigns"):
        sp.add_parser(name)
    r = sp.add_parser("review"); r.add_argument("--all", action="store_true")
    a = sp.add_parser("approve"); a.add_argument("clip_id"); a.add_argument("--streamer-approved", action="store_true")
    j = sp.add_parser("reject"); j.add_argument("clip_id")
    po = sp.add_parser("post"); po.add_argument("clip_id"); po.add_argument("platform", choices=["youtube", "tiktok", "instagram", "x"])
    pd = sp.add_parser("posted"); pd.add_argument("clip_id"); pd.add_argument("platform"); pd.add_argument("url")
    rp = sp.add_parser("report"); rp.add_argument("--days", type=int, default=1)
    au = sp.add_parser("auth"); au.add_argument("platform", choices=["youtube", "tiktok"])
    args = ap.parse_args()
    db.init_db()

    if args.cmd == "run":
        daemon()
    elif args.cmd == "campaigns":
        cmd_campaigns()
    elif args.cmd == "fetch":
        for c in authorized_campaigns().values():
            print(c.id, "downloaded:", fetch_campaign(c))
    elif args.cmd == "scan":
        print("Clips created:", scan_all())
    elif args.cmd == "review":
        rows = db.list_clips(None if args.all else "pending_review")
        print(json.dumps(rows, indent=2) if rows else "Nothing waiting for review.")
    elif args.cmd == "approve":
        cmd_approve(args.clip_id, args.streamer_approved)
    elif args.cmd == "reject":
        db.set_status(args.clip_id, "rejected")
        print(f"{args.clip_id}: rejected")
    elif args.cmd == "post":
        print(post_one(args.clip_id, args.platform))
    elif args.cmd == "posted":
        mark_posted(args.clip_id, args.platform, args.url)
    elif args.cmd == "report":
        cmd_report(args.days)
    elif args.cmd == "auth":
        if args.platform == "youtube":
            from .publish.youtube import credentials
            credentials(interactive=True)
            print("YouTube connected.")
        else:
            from .publish.tiktok import authorize_interactive
            authorize_interactive()


if __name__ == "__main__":
    main()

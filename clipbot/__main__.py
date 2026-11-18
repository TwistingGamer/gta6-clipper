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

import yaml
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from . import db
from .chat import ChatLogger
from .config import env_bool, env_int
from .buffer import RollingBuffer, schedule_cut
from .fetch import LIVE_DIR, capture_segment, category_ok, fetch_campaign, live_info
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
        self.buffers: dict[str, RollingBuffer] = {}

    @staticmethod
    def moments_mode() -> bool:
        return os.getenv("CAPTURE_MODE", "moments").lower() != "full"

    def on_spike(self, login: str, t: float, lift: float, hype: float):
        buf = self.buffers.get(login)
        LOG.info("chat spike on %s (x%.1f, %d%% hype)%s", login, lift, hype * 100,
                 "; saving the moment" if buf else "")
        if buf:
            schedule_cut(buf, t)

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
            key = inp.url  # one recorder per stream, even if several campaigns list it
            if not self._is_live(c, inp, online):
                continue
            wanted.add(key)
            if inp.type == "twitch" and inp.login not in self.chats:
                LOG.info("%s is live (%s)", inp.login, c.id)
                self.chats[inp.login] = ChatLogger(inp.login, on_spike=self.on_spike)
                self.chats[inp.login].start()
            recording_ok = c.allow_recording and env_bool("CAPTURE_STREAMS")
            if recording_ok and self.moments_mode() and inp.type == "twitch":
                # Watch chat, keep only a few minutes of video, save just the moments chat reacts to.
                if inp.login not in self.buffers:
                    self.buffers[inp.login] = RollingBuffer(c, inp)
                    self.buffers[inp.login].start()
                continue
            if recording_ok and key not in self.captures:  # full mode, or Kick/YouTube (no chat signal)
                flag = threading.Event()
                flag.set()
                threading.Thread(target=self._record, args=(c, inp, flag), daemon=True).start()
                self.captures[key] = flag
        for key in list(self.captures):
            if key not in wanted:
                self.captures.pop(key).clear()
        live_logins = {i.login for c, i in live_inputs if i.type == "twitch" and i.url in wanted}
        for login in list(self.chats):
            if login not in live_logins:
                self.chats.pop(login).stop()
        for login in list(self.buffers):
            if login not in live_logins:
                self.buffers.pop(login).stop()

    @staticmethod
    def _record(campaign, inp, flag: threading.Event):
        while flag.is_set():
            try:
                LOG.info("Captured %s", capture_segment(campaign, inp, env_int("SEGMENT_SECONDS", 300)))
            except Exception as exc:
                LOG.warning("Capture failed for %s: %s", inp.url, exc)
                time.sleep(30)


def cleanup_old_recordings():
    """Saved moments/segments are only needed until they've been clipped."""
    hours = env_int("KEEP_RECORDINGS_HOURS", 24)
    cutoff = time.time() - hours * 3600
    for p in LIVE_DIR.glob("*.mp4") if LIVE_DIR.exists() else []:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


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
            cleanup_old_recordings()
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
        try:
            reasons = json.loads(r.get("reasons") or "{}")
        except (TypeError, ValueError):
            reasons = {}
        from .publish import submit_targets
        for t in submit_targets({"campaign": r["campaign"], "reasons": reasons}, r["platform"]):
            by.setdefault(t, []).append(r)
    campaigns = load_campaigns()
    for cid, posts in by.items():
        c = campaigns.get(cid)
        print(f"\n== {c.display if c else cid} ==")
        if c and c.bio_link:
            print(f"   bio link required: {c.bio_link}")
        for p in posts:
            print(f"   {p['posted_at'][:16]}  {p['platform']:<9} {p['url'] or '(no link recorded)'}  {p['title']}")


def cmd_enable(cid: str, joined: str | None, evidence: str | None, on: bool = True):
    from datetime import date

    from .sources import read_settings, write_settings
    cid = cid.lower()
    known = load_campaigns()
    if cid not in known:
        raise SystemExit(f"No campaign '{cid}'. Known: {', '.join(known)}")
    settings = read_settings()
    mine = settings.setdefault(cid, {})
    mine["active"] = on
    if on:
        perm = mine.setdefault("permission", {})
        perm["joined_on"] = joined or perm.get("joined_on") or date.today().isoformat()
        if evidence:
            perm["evidence"] = evidence
    write_settings(settings)
    c = load_campaigns()[cid]
    ok, why = c.authorized()
    if on and not ok:
        print(f"{cid}: saved, but NOT active yet: {why}.")
        if "evidence" in why:
            print(f"  Add the campaign link: python -m clipbot enable {cid} --evidence https://...")
    else:
        print(f"{cid}: {'ON' if on else 'off'}")


def cmd_discover():
    tw = Twitch()
    if not tw.configured:
        raise SystemExit("Add TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET to .env first.")
    covered = {}
    for c in load_campaigns().values():
        for i in c.inputs:
            if i.type == "twitch":
                covered.setdefault(i.login, []).append(c.id + ("" if c.authorized()[0] else " (off)"))
    print("Biggest live GTA streams (English):")
    for s in tw.top_streams(limit=25):
        login = s["user_login"].lower()
        where = ", ".join(covered.get(login, [])) or "no campaign: check their clipping rules before clipping"
        print(f"  {s['viewer_count']:>7,}  {login:<20} {s['game_name']:<22} {where}")


def cmd_set(cid: str, field: str, value: str):
    from .sources import read_settings, write_settings
    cid = cid.lower()
    if cid not in load_campaigns():
        raise SystemExit(f"No campaign '{cid}'")
    settings = read_settings()
    node = settings.setdefault(cid, {})
    *parents, leaf = field.split(".")
    for p in parents:
        node = node.setdefault(p, {})
    node[leaf] = yaml.safe_load(value)
    write_settings(settings)
    print(f"{cid}: {field} = {node[leaf]!r}")


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
    sa = sp.add_parser("sample", help="record a few minutes of a live stream and make one preview clip (never posted)")
    sa.add_argument("channel", nargs="?"); sa.add_argument("--minutes", type=float, default=3)
    sp.add_parser("discover", help="biggest live GTA streams right now, and whether they're in a campaign")
    en = sp.add_parser("enable", help="switch a campaign on")
    en.add_argument("campaign"); en.add_argument("--joined", help="YYYY-MM-DD (default today)")
    en.add_argument("--evidence", help="campaign/rules link, if the example doesn't have it")
    di = sp.add_parser("disable", help="switch a campaign off"); di.add_argument("campaign")
    st = sp.add_parser("set", help="override one field, e.g. set core-pool budget.used 500")
    st.add_argument("campaign"); st.add_argument("field"); st.add_argument("value")
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
    elif args.cmd == "sample":
        from .sample import run_sample
        run_sample(args.channel, args.minutes)
    elif args.cmd == "discover":
        cmd_discover()
    elif args.cmd == "enable":
        cmd_enable(args.campaign, args.joined, args.evidence)
    elif args.cmd == "disable":
        cmd_enable(args.campaign, None, None, on=False)
    elif args.cmd == "set":
        cmd_set(args.campaign, args.field, args.value)
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

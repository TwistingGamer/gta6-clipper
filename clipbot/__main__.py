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
from pathlib import Path

import yaml
import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from . import db
from .chat import ChatLogger
from .config import env_bool, env_float, env_int
from .buffer import RollingBuffer, schedule_cut
from .fetch import LIVE_DIR, capture_segment, category_ok, fetch_campaign, live_info
from .pipeline import scan_all
from .publish import daily_cap, mark_posted, mode, post_due, post_one
from .sources import (CATEGORY_NAMES, LIVE_TYPES, add_discovered, authorized_campaigns, covered_logins, load_campaigns,
                      read_discovered, write_discovered)
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
        self.categories: dict[str, str] = {}  # latest game category per live stream
        self.titles: dict[str, str] = {}
        self.discovering: set[str] = set()   # discovered streams being watched right now
        self._last_discover = 0.0
        self.viewer_hist: dict[str, list[tuple[float, int]]] = {}
        self.first_look_until: dict[str, float] = {}  # login -> end of their first-look window
        self.first_look_pending: set[str] = set()     # load-in not saved yet  # login -> [(time, viewers)]
        self._warned_discover = False

    @staticmethod
    def moments_mode() -> bool:
        return os.getenv("CAPTURE_MODE", "moments").lower() != "full"

    def on_spike(self, login: str, t: float, lift: float, hype: float):
        buf = self.buffers.get(login)
        if buf and not self.still_in_game(buf.campaign, login):
            LOG.info("chat spike on %s ignored: now streaming %r, not an allowed game",
                     login, self.categories.get(login))
            return
        LOG.info("chat spike on %s (x%.1f, %d%% hype)%s", login, lift, hype * 100,
                 "; saving the moment" if buf else "")
        if buf:
            schedule_cut(buf, t, self.moment_meta(login, t))

    # ---- first look: a streamer's first minutes in GTA 6 ----
    def moment_meta(self, login: str, t: float) -> dict:
        meta = {"category": self.categories.get(login, ""), "title": self.titles.get(login, "")}
        if t <= self.first_look_until.get(login, 0):
            meta["first_look"] = True
        return meta

    def _note_category(self, login: str, stream: dict):
        """Spot the moment a streamer loads into GTA 6: the category changes to GTA VI while we watch,
        or the stream itself just started in GTA VI."""
        from datetime import datetime
        from .pipeline import first_look_done
        new = (stream.get("game_name") or "").strip().lower()
        prev = self.categories.get(login)
        self.categories[login] = stream.get("game_name") or ""
        self.titles[login] = stream.get("title") or ""
        if new != "grand theft auto vi" or login in self.first_look_until or login in first_look_done():
            return
        switched = prev is not None and prev.strip().lower() != new
        try:
            started = datetime.fromisoformat(stream.get("started_at", "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            started = 0
        if switched or time.time() - started < 20 * 60:
            self.first_look_until[login] = time.time() + env_int("FIRST_LOOK_MINUTES", 15) * 60
            self.first_look_pending.add(login)
            LOG.info("%s just loaded into GTA 6: watching for their first reaction", login)

    def still_in_game(self, campaign, login: str) -> bool:
        """Re-check the category right now: streamers switch games mid-stream."""
        if campaign.games() is None:
            return True
        if self.twitch.configured:
            try:
                s = self.twitch.online([login]).get(login)
            except Exception:
                s = None
            if s is not None:
                self.categories[login] = s.get("game_name") or ""
                self.titles[login] = s.get("title") or ""
        return category_ok(campaign, self.categories.get(login), self.titles.get(login))

    def _is_live(self, campaign, inp, twitch_online) -> bool:
        if inp.type == "twitch" and self.twitch.configured:
            s = twitch_online.get(inp.login)
            if s:
                self._note_category(inp.login, s)
            return bool(s) and category_ok(campaign, s.get("game_name"))
        meta = live_info(inp)  # Kick / YouTube live (or Twitch without API keys)
        if meta is not None:
            self.categories[inp.login] = meta.get("category") or ""
            self.titles[inp.login] = meta.get("title") or ""
        return meta is not None and category_ok(campaign, meta.get("category"), meta.get("title"))

    def discover(self, campaigns):
        """Pick up the biggest live GTA streams that no campaign covers (campaigns with discover: true)."""
        finders = [c for c in campaigns.values() if c.discover]
        if not finders:
            self.discovering = set()
            return
        if not self.twitch.configured:
            if not self._warned_discover:
                LOG.warning("Finding GTA streams needs TWITCH_CLIENT_ID/SECRET in .env")
                self._warned_discover = True
            return
        if time.time() - self._last_discover < env_int("DISCOVER_EVERY_MINUTES", 5) * 60:
            add_discovered(campaigns, keep_hours=24)
            return
        self._last_discover = time.time()
        covered = covered_logins(load_campaigns())
        found = read_discovered()
        now_watching = set()
        for c in finders:
            lang = None if c.language.lower() in ("", "any", "all") else c.language
            try:
                games = sorted(c.games() or {"grand theft auto vi"})
                streams = self.twitch.top_streams([CATEGORY_NAMES.get(g, g) for g in games], limit=100, language=lang)
            except Exception as exc:
                LOG.warning("GTA stream search failed: %s", exc)
                return
            skip = covered | {x.lower() for x in c.discover_exclude}
            ok = [s for s in streams
                  if s.get("viewer_count", 0) >= c.discover_min_viewers and s["user_login"].lower() not in skip]
            # Big and growing first: a stream whose viewers are surging is most likely real GTA 6 on launch.
            ok.sort(key=lambda s: -s.get("viewer_count", 0) * self.surge(s["user_login"].lower(), s.get("viewer_count", 0)))
            picks = [s["user_login"].lower() for s in ok][: max(0, c.discover_limit)]
            seen = found.setdefault(c.id, {})
            for login in picks:
                if login not in self.discovering:
                    LOG.info("[%s] found live GTA stream: %s", c.id, login)
                seen[login] = time.time()
            # forget streams not seen for a day
            found[c.id] = {k: v for k, v in seen.items() if time.time() - float(v) < 86400}
            now_watching |= set(picks)
        write_discovered(found)
        self.discovering = now_watching
        add_discovered(campaigns, keep_hours=24)

    def stop_all(self):
        """Stop every recorder and chat logger (low disk space)."""
        for buf in self.buffers.values():
            buf.stop()
            for piece in buf.dir.glob("*.ts"):
                try:
                    piece.unlink()
                except OSError:
                    pass  # still open; pruned on the next start
        for flag in self.captures.values():
            flag.clear()
        for chat_logger in self.chats.values():
            chat_logger.stop()
        self.buffers.clear()
        self.captures.clear()
        self.chats.clear()

    def surge(self, login: str, viewers: int) -> float:
        """How fast viewers are growing: now vs. ~30 min ago (1.0 = flat, capped at 3)."""
        now = time.time()
        hist = [h for h in self.viewer_hist.get(login, []) if now - h[0] < 3600] + [(now, viewers)]
        self.viewer_hist[login] = hist
        old = [v for t, v in hist if now - t >= 20 * 60]
        base = old[-1] if old else hist[0][1]
        return max(0.5, min(3.0, viewers / max(base, 1)))

    def tick(self, campaigns):
        self.discover(campaigns)
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
            if c.discover and inp.login not in self.discovering:
                continue  # found earlier but no longer among the top streams
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
                if inp.login in self.first_look_pending:
                    # Save the load-in itself (the "oh my god it's finally out" bit), even without a chat spike.
                    self.first_look_pending.discard(inp.login)
                    when = time.time() + env_int("FIRST_LOOK_DELAY_SECONDS", 75)
                    schedule_cut(self.buffers[inp.login], when, self.moment_meta(inp.login, time.time()))
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
    for p in [*LIVE_DIR.glob("*.mp4"), *LIVE_DIR.glob("*.json")] if LIVE_DIR.exists() else []:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def free_gb() -> float:
    from .config import STORAGE
    import shutil as _sh
    return _sh.disk_usage(STORAGE).free / 1e9


def expire_unreviewed(hours: float | None = None, sample_hours: float | None = None) -> int:
    """Delete clips nobody approved within UNREVIEWED_HOURS (12) and sample clips after SAMPLE_HOURS (24).
    hours=0 wipes every unapproved and sample clip now (`python -m clipbot cleanup`)."""
    from .publish import delete_clip_files
    from .sample import SAMPLE_OUT
    hours = env_float("UNREVIEWED_HOURS", 12) if hours is None else hours
    sample_hours = env_float("SAMPLE_HOURS", 24) if sample_hours is None else sample_hours

    def cutoff(h):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - h * 3600 + (1 if h == 0 else 0)))

    with db.connect() as con:
        ids = [r["id"] for r in con.execute(
            "SELECT id FROM clips WHERE status IN ('pending_review','awaiting_streamer') AND created_at < ?",
            (cutoff(hours),))]
        ids += [r["id"] for r in con.execute("SELECT id FROM clips WHERE status='sample' AND created_at < ?",
                                             (cutoff(sample_hours),))]
    for cid in ids:
        item = db.get_clip(cid)
        db.set_status(cid, "expired")
        delete_clip_files(item)
    # Preview files in output\sample (and any leftovers from older versions).
    if SAMPLE_OUT.is_dir():
        for f in SAMPLE_OUT.iterdir():
            try:
                if f.is_file() and time.time() - f.stat().st_mtime >= sample_hours * 3600:
                    f.unlink()
            except OSError:
                pass
    if ids:
        LOG.info("Deleted %d unapproved/sample clip(s)", len(ids))
    return len(ids)


def cmd_cleanup():
    """Wipe everything not approved: unreviewed and sample clips, saved moments, leftover temp files."""
    from .config import OUTPUT, TMP
    n = expire_unreviewed(0, 0)
    freed = 0
    keep = set()
    with db.connect() as con:
        for r in con.execute("SELECT source, output FROM clips WHERE status IN ('approved','awaiting_streamer')"):
            keep |= {str(Path(r["source"] or "").resolve()), str(Path(r["output"] or "").resolve())}
    for folder, patterns in ((LIVE_DIR, ("*.mp4", "*.json", "*.part.mp4")), (OUTPUT, ("*.mp4", "*.srt", "*.ass")),
                             (TMP, ("*",))):
        if not folder.is_dir():
            continue
        for pat in patterns:
            for f in folder.glob(pat):
                if f.is_file() and str(f.resolve()) not in keep and str(f.with_suffix(".mp4").resolve()) not in keep:
                    try:
                        freed += f.stat().st_size
                        f.unlink()
                    except OSError:
                        pass
    print(f"Deleted {n} unapproved/sample clip(s) and {freed / 1e9:.2f} GB of other files. Approved clips were kept.")


def daemon():
    from .notify import notify
    db.init_db()
    watcher = LiveWatcher()
    last_fetch = 0.0
    low_disk = False
    while True:
        try:
            # Disk safety: stop recording below MIN_FREE_GB, resume 5 GB above it.
            free = free_gb()
            limit = env_int("MIN_FREE_GB", 20)
            if not low_disk and free < limit:
                low_disk = True
                watcher.stop_all()
                LOG.warning("Only %.0f GB free: recording paused", free)
                notify(f"⚠️ Only {free:.0f} GB free on the clip drive: recording paused. Post or reject clips "
                       f"(or free space) and it resumes by itself above {limit + 5} GB.")
            elif low_disk and free > limit + 5:
                low_disk = False
                notify(f"✅ {free:.0f} GB free again: recording resumed.")
            campaigns = authorized_campaigns()  # re-read every cycle: edits apply live
            if not low_disk:
                watcher.tick(campaigns)
            expire_unreviewed()
            if time.time() - last_fetch > env_int("FETCH_EVERY_MINUTES", 30) * 60:
                for c in campaigns.values():
                    fetch_campaign(c)
                last_fetch = time.time()
            cleanup_old_recordings()
            made = scan_all(watcher.twitch if watcher.twitch.configured else None)
            if made:
                LOG.info("%d new clip(s) waiting for review", made)
            from .phone import check_reactions
            if check_reactions():
                LOG.info("applied approve/reject reactions from Discord")
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
        print(f"\n== {c.display if c else cid} =={'  (own channel: nothing to submit)' if c and not c.submit else ''}")
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
    if on and c.permission_type == "tolerated":
        print(f"WARNING: {c.display} has no clipping campaign or written permission. Clips are tolerated today,\n"
              "  but they can be claimed or struck at any time, and they earn nothing from the streamer.")
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
                tag = "paid" if c.permission_type == "campaign" else c.permission_type
                covered.setdefault(i.login, []).append(f"{c.id} [{tag}{'' if c.authorized()[0] else ', off'}]")
    print("Biggest live GTA streams (English):")
    for s in tw.top_streams(limit=25):
        login = s["user_login"].lower()
        where = ", ".join(covered.get(login, [])) or "not on your list (gta-any picks it up if on)"
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
    a = sp.add_parser("approve", help="approve one clip, or many: approve --all / approve --top 10")
    a.add_argument("clip_id", nargs="?"); a.add_argument("--streamer-approved", action="store_true")
    a.add_argument("--all", action="store_true"); a.add_argument("--top", type=int, default=0)
    a.add_argument("--campaign", default="")
    sc = sp.add_parser("set-cap", help="posts per platform per day, e.g. set-cap 5 (--gap 120 minutes apart)")
    sc.add_argument("per_day", type=int); sc.add_argument("--gap", type=int, default=None)
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
    an = sp.add_parser("announce", help="make a Vice City style 'streamer goes live' card to post")
    an.add_argument("who"); an.add_argument("when")
    an.add_argument("--what", default="STARTS GTA 6 LIVE"); an.add_argument("--when2", default="")
    an.add_argument("--where", default=""); an.add_argument("--tags", default="#GTA6")
    an.add_argument("--caption", default=""); an.add_argument("--seconds", type=float, default=8)
    sp.add_parser("cleanup", help="delete every unapproved and sample clip and leftover recordings now")
    fin = sp.add_parser("done", help="mark a clip finished and delete its video files"); fin.add_argument("clip_id")
    args = ap.parse_args()
    db.init_db()
    if args.cmd in ("run", "scan", "sample", "fetch", "announce"):
        from .config import missing_tools
        problems = missing_tools(need_streamlink=args.cmd in ("run", "sample"))
        if problems:
            raise SystemExit("Can't start:\n  " + "\n  ".join(problems))

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
        if args.all or args.top:
            rows = [r for r in db.list_clips("pending_review", limit=1000)
                    if not args.campaign or r.get("campaign") == args.campaign]
            rows.sort(key=lambda r: -(r.get("score") or 0))
            if args.top:
                rows = rows[:args.top]
            for r in rows:
                cmd_approve(r["id"], args.streamer_approved)
            print(f"{len(rows)} clip(s) handled." if rows else "Nothing waiting for review.")
        elif args.clip_id:
            cmd_approve(args.clip_id, args.streamer_approved)
        else:
            raise SystemExit("approve CLIP_ID, or approve --all, or approve --top 10")
    elif args.cmd == "set-cap":
        from .publish import daily_cap, min_gap_seconds, write_cap_settings
        write_cap_settings(args.per_day, args.gap)
        cap = daily_cap()
        print(f"Up to {cap} post(s) per platform per day, at least {min_gap_seconds(cap) / 60:.0f} min apart.")
        if cap > 6:
            print("Heads up: brand-new accounts posting more than ~6 a day often get throttled or flagged as spam.")
    elif args.cmd == "reject":
        db.set_status(args.clip_id, "rejected")
        from .publish import delete_clip_files
        item = db.get_clip(args.clip_id)
        gone = delete_clip_files(item) if item else 0
        print(f"{args.clip_id}: rejected" + (", video files deleted" if gone else ""))
    elif args.cmd == "post":
        print(post_one(args.clip_id, args.platform))
    elif args.cmd == "posted":
        mark_posted(args.clip_id, args.platform, args.url)
    elif args.cmd == "announce":
        from .announce import run_announce
        r = run_announce(args.who, args.when, args.what, args.when2, args.where, args.tags, args.caption, args.seconds)
        print(f"Card ready: {r['output']}\n\nCaption:\n{r['description']}\n\n"
              f"Watch it, then: python -m clipbot approve {r['id']}\n"
              f"and post it now: python -m clipbot post {r['id']} tiktok   (also instagram, youtube, x)")
    elif args.cmd == "cleanup":
        cmd_cleanup()
    elif args.cmd == "done":
        from .publish import finish_clip
        item = db.get_clip(args.clip_id)
        if not item:
            raise SystemExit(f"No clip {args.clip_id}")
        finish_clip(item)
        print(f"{args.clip_id}: done, video files deleted")
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

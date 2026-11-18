"""Command line and 24/7 daemon.

  python -m clipbot run                 watch streamers, record (if allowed), clip, post approved clips
  python -m clipbot sources             show which streamers are approved and why
  python -m clipbot process FILE        clip one recording now
  python -m clipbot review              list clips waiting for review
  python -m clipbot approve ID [--streamer-approved]
  python -m clipbot reject ID
  python -m clipbot post ID PLATFORM    post one approved clip now (youtube|tiktok|instagram)
  python -m clipbot auth youtube|tiktok one-time sign-in
"""
from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from pathlib import Path

from . import db
from .chat import ChatLogger
from .config import env_bool, env_int
from .pipeline import process_file, scan_recordings
from .publish import post_due, post_one
from .sources import load_sources
from .twitch import Twitch, capture_segment, is_gta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOG = logging.getLogger("clipbot")


def _capture_loop(source, seconds: int, live: threading.Event):
    while live.is_set():
        try:
            LOG.info("Captured %s", capture_segment(source, seconds))
        except Exception as exc:
            LOG.warning("Capture failed for %s: %s", source.login, exc)
            time.sleep(30)


def daemon():
    db.init_db()
    twitch = Twitch()
    chats: dict[str, ChatLogger] = {}
    captures: dict[str, tuple[threading.Thread, threading.Event]] = {}
    while True:
        try:
            sources = {k: s for k, s in load_sources().items() if s.authorized()[0]}  # re-read: edits apply live
            online = twitch.online(list(sources)) if (sources and twitch.configured) else {}
            for login, src in sources.items():
                stream = online.get(login)
                live_gta = bool(stream) and (is_gta(stream) or not env_bool("GTA_ONLY", True))
                if live_gta and login not in chats:
                    LOG.info("%s is live: %s", login, stream.get("title", ""))
                    chats[login] = ChatLogger(login)
                    chats[login].start()
                if live_gta and src.allow_recording and env_bool("CAPTURE_STREAMS") and login not in captures:
                    flag = threading.Event()
                    flag.set()
                    t = threading.Thread(target=_capture_loop, args=(src, env_int("SEGMENT_SECONDS", 300), flag), daemon=True)
                    t.start()
                    captures[login] = (t, flag)
                if not live_gta:
                    if login in chats:
                        chats.pop(login).stop()
                    if login in captures:
                        captures.pop(login)[1].clear()
            # Stop anything for streamers removed from / revoked in sources.yaml.
            for login in list(chats):
                if login not in sources:
                    chats.pop(login).stop()
            for login in list(captures):
                if login not in sources:
                    captures.pop(login)[1].clear()

            made = scan_recordings(twitch if twitch.configured else None)
            if made:
                LOG.info("%d new clip(s) waiting for review", made)
            for clip_id, platform, remote in post_due():
                LOG.info("Posted %s -> %s (%s)", clip_id, platform, remote)
        except Exception:
            LOG.exception("Daemon cycle failure")
        time.sleep(max(15, env_int("POLL_SECONDS", 60)))


def cmd_sources():
    rows = []
    for s in load_sources().values():
        ok, why = s.authorized()
        rows.append({"login": s.login, "authorized": ok, "reason": why, "permission": s.permission_type,
                     "evidence": s.evidence, "recording": s.allow_recording, "ad_disclosure": s.ad_disclosure,
                     "ai_text": s.ai_text_allowed, "per_clip_approval": s.clip_preapproval})
    print(json.dumps(rows, indent=2) if rows else "No sources.yaml (copy sources.example.yaml) or it lists no streamers.")


def cmd_approve(clip_id: str, streamer_ok: bool):
    item = db.get_clip(clip_id)
    if not item:
        raise SystemExit(f"No clip {clip_id}")
    src = load_sources().get(item.get("streamer") or "")
    if src and src.clip_preapproval and not streamer_ok:
        db.set_status(clip_id, "awaiting_streamer")
        print(f"{clip_id}: needs the streamer's OK first. Once they approve, run: approve {clip_id} --streamer-approved")
    else:
        db.set_status(clip_id, "approved")
        print(f"{clip_id}: approved, will post on the next free slot")


def main():
    ap = argparse.ArgumentParser(description="GTA streamer clip bot (approved sources only)")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("run")
    sp.add_parser("scan")
    sp.add_parser("sources")
    sp.add_parser("check-twitch")
    p = sp.add_parser("process"); p.add_argument("file", type=Path)
    r = sp.add_parser("review"); r.add_argument("--all", action="store_true")
    a = sp.add_parser("approve"); a.add_argument("clip_id"); a.add_argument("--streamer-approved", action="store_true")
    j = sp.add_parser("reject"); j.add_argument("clip_id")
    po = sp.add_parser("post"); po.add_argument("clip_id"); po.add_argument("platform", choices=["youtube", "tiktok", "instagram"])
    au = sp.add_parser("auth"); au.add_argument("platform", choices=["youtube", "tiktok"])
    args = ap.parse_args()
    db.init_db()

    if args.cmd == "run":
        daemon()
    elif args.cmd == "scan":
        print("Clips created:", scan_recordings())
    elif args.cmd == "process":
        print("Clips created:", process_file(args.file))
    elif args.cmd == "sources":
        cmd_sources()
    elif args.cmd == "check-twitch":
        print(json.dumps(Twitch().online(list(load_sources())), indent=2))
    elif args.cmd == "review":
        rows = db.list_clips(None if args.all else "pending_review")
        print(json.dumps(rows, indent=2) if rows else "Nothing waiting for review.")
    elif args.cmd == "approve":
        cmd_approve(args.clip_id, args.streamer_approved)
    elif args.cmd == "reject":
        db.set_status(args.clip_id, "rejected")
        print(f"{args.clip_id}: rejected")
    elif args.cmd == "post":
        print(f"{args.platform} id:", post_one(args.clip_id, args.platform))
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

"""Footage -> candidate windows -> AI judge -> rendered, captioned clips in the review queue."""
from __future__ import annotations

import hashlib
import logging
import math
import os
import time
from pathlib import Path

from . import ai, chat, db, detect
from .captions import overlay_ass, transcribe
from .config import OUTPUT, VIDEO_EXTS, env_bool, env_float, env_int
from .fetch import LIVE_DIR, folder_inputs
from .render import grab_frames, render
from .sources import Campaign, authorized_campaigns

LOG = logging.getLogger("clipbot.pipeline")


def recording_meta(path: Path) -> tuple[str | None, float | None]:
    """<login>__<unix time>.mp4 (live recordings) -> (login, start time)."""
    parts = path.stem.split("__")
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0].lower(), float(parts[1])
    return None, None


def initial_status(c: Campaign) -> str:
    if c.require_review or not env_bool("AUTO_APPROVE"):
        return "pending_review"
    return "awaiting_streamer" if c.clip_preapproval else "approved"


def package(c: Campaign, verdict: dict, people: list[str] | None = None,
            login: str | None = None) -> tuple[str, str, list[str]]:
    """Title, description and hashtags that satisfy the campaign's caption rules."""
    people = people or []
    if not c.ai_text_allowed:
        plain = ai.template_text(c)
        verdict = verdict | {k: plain[k] for k in ("title", "description", "hashtags", "hook")}
    names = " & ".join(people) if people else c.display
    title = verdict["title"].replace("{members}", names).strip()
    verdict = verdict | {"description": verdict["description"].replace("{members}", names)}
    desc = verdict["description"].strip()
    for word in c.required_words:
        if word.lower() not in f"{title} {desc}".lower():
            desc = f"{desc} {word}".strip()
    tags = list(dict.fromkeys((["#ad"] if c.ad_disclosure else []) + c.required_hashtags
                              + c.member_tags(people) + verdict.get("hashtags", [])))
    lines = []
    if c.ad_disclosure:
        lines.append(f"#ad Paid partnership with {c.sponsor or c.display}")
    lines.append(desc)
    if credit := c.credit_for(login):
        lines.append(f"🎥 {credit}")
    if c.required_mentions:
        lines.append(" ".join(c.required_mentions))
    lines.append(" ".join(tags))
    if c.ad_disclosure and "#ad" not in title.lower():
        title = f"{title[:94]} #ad"
    return title[:100], "\n\n".join(lines), tags


def process_file(path: Path, campaign: Campaign, *, twitch=None) -> int:
    db.init_db()
    path = path.resolve()
    if path.suffix.lower() not in VIDEO_EXTS:
        return 0
    ok, why = campaign.authorized()
    if not ok:
        raise PermissionError(f"{campaign.id} not authorized ({why})")
    stat = path.stat()
    key = f"{campaign.id}|{path}"  # shared live recordings are processed once per campaign
    with db.connect() as con:
        prev = con.execute("SELECT size, mtime, status FROM inputs WHERE path=?", (key,)).fetchone()
        if prev and tuple(prev) == (stat.st_size, stat.st_mtime, "done"):
            return 0
        con.execute("INSERT OR REPLACE INTO inputs VALUES (?,?,?,?,?)", (key, stat.st_size, stat.st_mtime, "processing", None))
    try:
        made = _make_clips(path, campaign, stat, twitch)
        with db.connect() as con:
            con.execute("UPDATE inputs SET status='done', error=NULL WHERE path=?", (key,))
        return made
    except Exception as exc:
        with db.connect() as con:
            con.execute("UPDATE inputs SET status='error', error=? WHERE path=?", (str(exc)[:500], key))
        raise


def _make_clips(path: Path, c: Campaign, stat, twitch) -> int:
    window = max(c.min_clip_seconds, min(env_int("CLIP_SECONDS", 36), c.max_clip_seconds))
    seconds = detect.ffprobe_seconds(path)
    login, rec_start = recording_meta(path)

    signals: dict[str, list[float] | None] = {"audio": detect.audio_scores(path, seconds, window)}
    inp = c.input_for_login(login) if login else None
    if inp and inp.type == "twitch" and rec_start:
        signals["chat"] = chat.chat_scores(login, rec_start, seconds, detect.STEP, window)
        if twitch is not None:
            try:
                clips = twitch.clips_between(login, rec_start, rec_start + seconds)
                signals["clips"] = detect.clip_scores(clips, rec_start, seconds, window) if clips else None
            except Exception as exc:
                LOG.warning("Twitch clips lookup failed for %s: %s", login, exc)

    # Long videos (full VODs) get more clips than a 5-minute live segment.
    max_out = max(env_int("MAX_CLIPS_PER_SEGMENT", 2), math.ceil(seconds / 3600 * env_int("MAX_CLIPS_PER_HOUR", 6)))
    candidates = detect.pick_windows(seconds, window, signals, max_out * 2, env_float("MIN_SCORE", 0.15))
    LOG.info("[%s] %s: %d candidates (signals: %s)", c.id, path.name, len(candidates),
             ", ".join(k for k, v in signals.items() if v))

    made = 0
    for cand in candidates:
        if made >= max_out:
            break
        sig = hashlib.sha256(f"{c.id}|{path}|{stat.st_size}|{stat.st_mtime}|{cand.start}".encode()).hexdigest()[:16]
        if db.get_clip(sig) or db.clip_exists_for(str(path), cand.start):
            continue  # already clipped (possibly for a linked campaign: one clip, submitted to several)
        stem = f"{c.id}_{path.stem}_{sig}"[:120]
        srt = OUTPUT / f"{stem}.srt"
        transcript = ""
        if env_bool("CAPTIONS", True):
            try:
                transcript = transcribe(path, cand.start, cand.duration, srt)
            except Exception as exc:  # e.g. Whisper model can't download; still make the clip
                LOG.warning("Captions failed @%.0fs, continuing without: %s", cand.start, str(exc)[:200])
                srt.unlink(missing_ok=True)
        frames = grab_frames(path, cand.start, cand.duration, OUTPUT)
        try:
            verdict = ai.judge(frames, transcript, c, cand.signals)
        finally:
            for f in frames:
                f.unlink(missing_ok=True)
        if not verdict.get("keep") or verdict.get("score", 0) < env_int("MIN_AI_SCORE", 6):
            LOG.info("Skipped %s @%.0fs: %s", path.name, cand.start, verdict.get("why"))
            srt.unlink(missing_ok=True)
            continue

        # Who is in the clip: AI-detected members, plus the member whose stream this is.
        known = {m["name"].lower(): m["name"] for m in c.members}
        people = [known[p.lower()] for p in verdict.get("people", []) if p.lower() in known]
        owner = c.member_for_login(login or "")
        if owner and owner["name"] not in people:
            people.insert(0, owner["name"])
        ai_ran = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
        if c.min_members and ai_ran and len(people) < c.min_members:
            LOG.info("Skipped %s @%.0fs: only %d member(s) on screen, campaign needs %d",
                     path.name, cand.start, len(people), c.min_members)
            srt.unlink(missing_ok=True)
            continue
        title, description, tags = package(c, verdict, people, login)
        overlay = OUTPUT / f"{stem}.ass"
        hook = verdict.get("hook", "") if c.ai_text_allowed else ""
        ad_label = os.getenv("AD_LABEL", "Paid partnership #ad") if c.ad_disclosure else ""
        overlay_ass(overlay, cand.duration, hook, c.watermark or c.credit_for(login), ad_label)
        dest = OUTPUT / f"{stem}.mp4"
        render(path, cand.start, cand.duration, dest, srt=srt, overlay=overlay)

        db.insert_clip({
            "id": sig, "source": str(path), "output": str(dest),
            "score": cand.score * 0.4 + verdict.get("score", 0) / 10 * 0.6,
            "status": initial_status(c), "title": title, "created_at": db.now_iso(),
            "streamer": login or "", "campaign": c.id, "series": c.series,
            "description": description, "hashtags": tags, "hook": hook,
            "start_s": cand.start, "duration_s": cand.duration, "ad_disclosure": int(c.ad_disclosure),
            "reasons": {"signals": cand.signals, "ai": verdict.get("why", ""), "transcript": transcript[:500],
                        "people": people, "submit_to": [c.id] + c.also_submit_to,
                        "check": ([] if ai_ran or not c.min_members else
                                  [f"confirm at least {c.min_members} members are on screen and fix member hashtags"])},
        })
        LOG.info("[%s] clip ready for review: %s \"%s\"", c.id, sig, title)
        made += 1
    return made


def campaign_files(c: Campaign) -> list[Path]:
    files = []
    # Shared live recordings (one recording per streamer, used by every campaign that lists them).
    logins = c.live_logins()
    if logins and LIVE_DIR.exists():
        files += [p for p in LIVE_DIR.glob("*") if p.suffix.lower() in VIDEO_EXTS
                  and recording_meta(p)[0] in logins]
    for d in folder_inputs(c):
        if d.exists():
            files += [p for p in d.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS]
    return sorted(set(files))


def scan_all(twitch=None) -> int:
    count = 0
    # Campaigns that share clips with others (e.g. CORE -> Lacy, Jason) go first, so a moment
    # becomes one clip submitted to all of them instead of a duplicate per campaign.
    ordered = sorted(authorized_campaigns().values(), key=lambda c: (-len(c.also_submit_to), -c.priority()))
    for c in ordered:
        for path in campaign_files(c):
            if time.time() - path.stat().st_mtime < 20:  # still being written
                continue
            try:
                count += process_file(path, c, twitch=twitch)
            except Exception:
                LOG.exception("[%s] failed processing %s", c.id, path)
    return count

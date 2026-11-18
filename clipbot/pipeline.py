"""Footage -> candidate windows -> AI judge -> rendered, captioned clips in the review queue."""
from __future__ import annotations

import hashlib
import json
import re
import logging
import math
import os
import time
from pathlib import Path

from . import ai, chat, db, detect, layout
from .captions import overlay_ass, transcribe
from .config import OUTPUT, VIDEO_EXTS, env_bool, env_float, env_int
from .fetch import LIVE_DIR, folder_inputs
from .render import grab_frames, render, watermark_image
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
    streamer = login or ""
    title = verdict["title"].replace("{members}", names).replace("{streamer}", streamer).strip()
    verdict = verdict | {"description": verdict["description"].replace("{members}", names).replace("{streamer}", streamer)}
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
    side = path.with_suffix(".json")  # saved live moments record which game was on screen
    if side.exists():
        try:
            meta = json.loads(side.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        if meta.get("category") is not None and not campaign.game_ok(meta.get("category"), meta.get("title")):
            return 0
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


FIRST_LOOK_WORDS = re.compile(
    r"\b(finally|it'?s out|it'?s here|we'?re in|oh my god|oh my gosh|omg|vice city|leonida|lucia|jason|"
    r"first time|let'?s go+|no way|this is crazy|it'?s real)\b", re.I)


def moment_meta(path: Path) -> dict:
    side = path.with_suffix(".json")
    try:
        return json.loads(side.read_text(encoding="utf-8")) if side.exists() else {}
    except (OSError, ValueError):
        return {}


def first_look_done() -> dict:
    from .config import DATA
    try:
        return json.loads((DATA / "first_look.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def mark_first_look(login: str, clip_id: str):
    from .config import DATA
    done = first_look_done()
    done[login] = clip_id
    (DATA / "first_look.json").write_text(json.dumps(done), encoding="utf-8")


STOP = set("""the and that this with you your for are was what but not have just like they them its it's i'm
dont don't can't cant gonna wanna yeah okay right there here then than from about would could should because
really know think going chat yall y'all bro guys one all get got out now see look come what's that's he's she's
we're you're were will when where who how why also some more very much been being into over only even still
""".split())


def slug_words(transcript: str, chat_msgs: list[str], ai_title: str = "", n: int = 3) -> str:
    """A few words saying what the clip is about: the AI title if there is one, else the words
    chat and the streamer repeated most."""
    import collections
    if ai_title:
        words = re.findall(r"[a-z0-9]+", ai_title.lower())
        words = [w for w in words if w not in STOP and len(w) > 2]
        return "-".join(words[:4])
    counts = collections.Counter()
    for m in chat_msgs:
        for w in set(re.findall(r"[a-z0-9']{3,}", m.lower())):
            if w not in STOP:
                counts[w] += 1
    for w in re.findall(r"[a-z0-9']{4,}", (transcript or "").lower()):
        if w not in STOP:
            counts[w] += 2   # what's said on stream counts double
    return "-".join(w.replace("'", "") for w, _ in counts.most_common(n))


def clip_filename(rec_start: float | None, start: float, login: str, kind: str, slug: str, score: float, clip_id: str) -> str:
    """e.g. 2026-11-18_0442_typicalgamer_first-look_police-chase_score87_<id>.mp4"""
    when = time.strftime("%Y-%m-%d_%H%M", time.localtime((rec_start or time.time()) + start))
    parts = [when, re.sub(r"[^a-z0-9_]", "", (login or "clip").lower()), kind,
             re.sub(r"[^a-z0-9-]", "", slug)[:40].strip("-"), f"score{int(min(score, 0.99) * 100)}", clip_id]
    return "_".join(p for p in parts if p) + ".mp4"


def _make_room(login: str, score: float) -> bool:
    """Keep only the best MAX_PER_STREAM_HOUR clips per streamer per hour: a better clip bumps the weakest
    one still waiting for review; a weaker one is skipped."""
    limit = env_int("MAX_PER_STREAM_HOUR", 3)
    if not login or limit <= 0:
        return True
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 3600))
    with db.connect() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT id, score, status FROM clips WHERE streamer=? AND created_at>=? "
            "AND status IN ('pending_review','approved','awaiting_streamer','done')", (login, since))]
    if len(rows) < limit:
        return True
    waiting = sorted((r for r in rows if r["status"] == "pending_review"), key=lambda r: r["score"] or 0)
    if waiting and score > (waiting[0]["score"] or 0):
        from .publish import delete_clip_files
        weakest = db.get_clip(waiting[0]["id"])
        db.set_status(weakest["id"], "rejected")
        delete_clip_files(weakest)
        LOG.info("%s: replaced weaker clip %s (%.2f) with a better one (%.2f)", login, weakest["id"],
                 weakest.get("score") or 0, score)
        return True
    return False


def _make_clips(path: Path, c: Campaign, stat, twitch) -> int:
    window = max(c.min_clip_seconds, min(env_int("CLIP_SECONDS", 36), c.max_clip_seconds))
    seconds = detect.ffprobe_seconds(path)
    login, rec_start = recording_meta(path)

    audio_ps = detect.audio_per_second(path)
    chat_ps = None
    signals: dict[str, list[float] | None] = {"audio": detect.audio_scores(path, seconds, window, audio_ps)}
    inp = c.input_for_login(login) if login else None
    if inp and inp.type == "twitch" and rec_start:
        signals["chat"] = chat.chat_scores(login, rec_start, seconds, detect.STEP, window)
        chat_ps = chat.chat_per_second(login, rec_start, seconds)
        if twitch is not None:
            try:
                clips = twitch.clips_between(login, rec_start, rec_start + seconds)
                signals["clips"] = detect.clip_scores(clips, rec_start, seconds, window) if clips else None
            except Exception as exc:
                LOG.warning("Twitch clips lookup failed for %s: %s", login, exc)

    # Long videos (full VODs) get more clips than a 5-minute live segment.
    max_out = max(env_int("MAX_CLIPS_PER_SEGMENT", 2), math.ceil(seconds / 3600 * env_int("MAX_CLIPS_PER_HOUR", 3)))
    candidates = detect.pick_windows(seconds, window, signals, max_out * 2, env_float("MIN_MOMENT_SCORE", 0.35))
    LOG.info("[%s] %s: %d candidates (signals: %s)", c.id, path.name, len(candidates),
             ", ".join(k for k, v in signals.items() if v))

    # Context: start well before the moment (the setup) and end just after the payoff.
    lead = env_float("CONTEXT_SECONDS", 25)
    tail = env_float("AFTER_SECONDS", 10)
    total = max(c.min_clip_seconds, min(lead + tail, c.max_clip_seconds))
    lead, tail = total * lead / (lead + tail), total * tail / (lead + tail)
    meta = moment_meta(path)
    first_look = bool(meta.get("first_look")) and bool(login) and login not in first_look_done()
    if first_look and not candidates and seconds >= 15:   # the load-in itself, even if chat was slow
        candidates = [detect.Candidate(0.0, min(seconds, total), 0.5, {"first_look": 1})]
    made, used = 0, []
    for cand in candidates:
        if made >= max_out:
            break
        cand = detect.refine(cand, seconds, audio_ps, chat_ps, lead, tail)
        if any(cand.start < e and cand.start + cand.duration > s for s, e in used):
            continue  # re-centred onto a moment that's already clipped
        # Viral check before spending time on captions/AI: a real reaction is loud, and a frozen
        # menu or loading screen only counts if the streamer reacts really hard.
        seg = audio_ps[int(cand.start):int(cand.start + cand.duration)] if len(audio_ps) else audio_ps
        peak = float(seg.max()) if len(seg) else 0.0
        viewer_clip = (cand.signals.get("clips") or 0) > 0.5
        hype_chat = (cand.signals.get("chat") or 0) >= env_float("CHAT_HYPE_SCORE", 0.6)
        loud_enough = peak >= env_float("AUDIO_MIN_PEAK", 6.0) or (hype_chat and peak >= env_float("AUDIO_WITH_CHAT_PEAK", 2.5))
        if not first_look and not viewer_clip and not loud_enough:
            LOG.info("Skipped %s @%.0fs: no big reaction (peak %.1fx normal loudness)", path.name, cand.start, peak)
            continue
        if not c.min_members and layout.watching_someone_else(path, cand.start, cand.duration):
            LOG.info("Skipped %s @%.0fs: two webcams on screen, %s is watching someone else's stream",
                     path.name, cand.start, login or "streamer")
            continue
        if not first_look and peak < env_float("AUDIO_STRONG_PEAK", 8.0):
            still = detect.motion(path, cand.start, cand.duration)
            if still < env_float("STATIC_MOTION", 1.5):
                LOG.info("Skipped %s @%.0fs: static screen (menu/loading?) and no huge reaction", path.name, cand.start)
                continue
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
        ai_ran = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
        # The score threshold only applies when the AI judge actually rated the clip.
        if not verdict.get("keep") or (ai_ran and verdict.get("score", 0) < env_int("MIN_AI_SCORE", 6)):
            LOG.info("Skipped %s @%.0fs: %s", path.name, cand.start, verdict.get("why"))
            srt.unlink(missing_ok=True)
            continue

        # Who is in the clip: AI-detected members, plus the member whose stream this is.
        known = {m["name"].lower(): m["name"] for m in c.members}
        people = [known[p.lower()] for p in verdict.get("people", []) if p.lower() in known]
        owner = c.member_for_login(login or "")
        if owner and owner["name"] not in people:
            people.insert(0, owner["name"])
        if c.min_members and ai_ran and len(people) < c.min_members:
            LOG.info("Skipped %s @%.0fs: only %d member(s) on screen, campaign needs %d",
                     path.name, cand.start, len(people), c.min_members)
            srt.unlink(missing_ok=True)
            continue
        heard = bool(FIRST_LOOK_WORDS.search(transcript or ""))
        is_first = first_look and made == 0
        if is_first and not c.members and not c.sponsor:
            # "{streamer}'s first time in GTA 6": fixed wording, not AI text, so fine for no-AI campaigns too.
            verdict = verdict | {"title": f"{login}'s first time in GTA 6 😳", "keep": True}
        final_score = cand.score * 0.4 + verdict.get("score", 0) / 10 * 0.6 + (1.0 if is_first else 0.0)
        if not is_first and not _make_room(login or "", final_score):
            LOG.info("Skipped %s @%.0fs: %s already has its best clips this hour", path.name, cand.start, login)
            srt.unlink(missing_ok=True)
            continue
        title, description, tags = package(c, verdict, people, login)
        overlay = OUTPUT / f"{stem}.ass"
        hook = verdict.get("hook", "") if c.ai_text_allowed else ""
        if is_first and c.show_credit:
            hook = f"{login}'s first time in GTA 6"
        ad_label = os.getenv("AD_LABEL", "Paid partnership #ad") if c.ad_disclosure else ""
        mark = os.getenv("WATERMARK_TEXT", "@gta6.clippaa").strip() if c.my_watermark else ""
        frame = layout.choose(path, cand.start, cand.duration)
        overlay_ass(overlay, cand.duration, hook, c.watermark or c.credit_for(login), ad_label, mark, frame.hook_top)
        chat_lines = [m for _, m in chat.load_chat(login, rec_start + cand.start, rec_start + cand.start + cand.duration + 10)] \
            if (login and rec_start) else []
        kind = "first-look" if is_first else ("viewer-clip" if viewer_clip else "moment")
        slug = slug_words(transcript, chat_lines, verdict.get("title", "") if ai_ran else "")
        dest = OUTPUT / clip_filename(rec_start, cand.start, login or c.id, kind, slug, final_score, sig)
        render(path, cand.start, cand.duration, dest, srt=srt, overlay=overlay, layout=frame,
               logo=watermark_image() if c.my_watermark else None)
        used.append((cand.start, cand.start + cand.duration))
        srt.unlink(missing_ok=True)       # burned into the video now
        overlay.unlink(missing_ok=True)

        db.insert_clip({
            "id": sig, "source": str(path), "output": str(dest),
            "score": final_score,
            "status": initial_status(c), "title": title, "created_at": db.now_iso(),
            "streamer": login or "", "campaign": c.id, "series": c.series,
            "description": description, "hashtags": tags, "hook": hook,
            "start_s": cand.start, "duration_s": cand.duration, "ad_disclosure": int(c.ad_disclosure),
            "reasons": {"signals": cand.signals, "layout": frame.mode, "first_look": is_first,
                        "first_look_words": heard, "peak": round(peak, 1), "ai": verdict.get("why", ""), "transcript": transcript[:500],
                        "people": people, "submit_to": [c.id] + c.also_submit_to,
                        "check": ([] if ai_ran or not c.min_members else
                                  [f"confirm at least {c.min_members} members are on screen and fix member hashtags"])},
        })
        if is_first:
            mark_first_look(login, sig)
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

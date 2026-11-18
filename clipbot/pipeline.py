"""Recording -> candidate windows -> AI judge -> rendered, captioned clips in the review queue."""
from __future__ import annotations

import hashlib
import os
import logging
import time
from pathlib import Path

from . import ai, chat, db, detect
from .captions import overlay_ass, transcribe
from .config import OUTPUT, RECORDINGS, VIDEO_EXTS, env_bool, env_float, env_int
from .render import grab_frames, render
from .sources import Source, source_for_file

LOG = logging.getLogger("clipbot.pipeline")


class Unauthorized(RuntimeError):
    pass


def recording_start(path: Path) -> float | None:
    """<login>__<unix time>.mp4 -> unix time the recording started."""
    parts = path.stem.split("__")
    try:
        return float(parts[1]) if len(parts) > 1 else None
    except ValueError:
        return None


def process_file(path: Path, *, twitch=None) -> int:
    db.init_db()
    path = path.resolve()
    if path.suffix.lower() not in VIDEO_EXTS:
        return 0
    source = source_for_file(path.stem)
    if source is None:
        raise Unauthorized(f"{path.name}: streamer '{path.stem.split('__')[0]}' is not in sources.yaml")
    ok, why = source.authorized()
    if not ok:
        raise Unauthorized(f"{path.name}: {source.login} not authorized ({why})")

    stat = path.stat()
    with db.connect() as con:
        prev = con.execute("SELECT size, mtime, status FROM inputs WHERE path=?", (str(path),)).fetchone()
        if prev and tuple(prev) == (stat.st_size, stat.st_mtime, "done"):
            return 0
        con.execute("INSERT OR REPLACE INTO inputs VALUES (?,?,?,?,?)", (str(path), stat.st_size, stat.st_mtime, "processing", None))

    made = 0
    try:
        made = _make_clips(path, source, stat, twitch)
        with db.connect() as con:
            con.execute("UPDATE inputs SET status='done', error=NULL WHERE path=?", (str(path),))
    except Exception as exc:
        with db.connect() as con:
            con.execute("UPDATE inputs SET status='error', error=? WHERE path=?", (str(exc), str(path)))
        raise
    return made


def initial_status(source: Source) -> str:
    if not env_bool("AUTO_APPROVE"):
        return "pending_review"
    return "awaiting_streamer" if source.clip_preapproval else "approved"


def _make_clips(path: Path, source: Source, stat, twitch) -> int:
    window = min(env_int("CLIP_SECONDS", 36), source.max_clip_seconds)
    seconds = detect.ffprobe_seconds(path)
    rec_start = recording_start(path)

    signals: dict[str, list[float] | None] = {"audio": detect.audio_scores(path, seconds, window)}
    if rec_start:
        signals["chat"] = chat.chat_scores(source.login, rec_start, seconds, detect.STEP, window)
        if twitch is not None:
            try:
                clips = twitch.clips_between(source.login, rec_start, rec_start + seconds)
                signals["clips"] = detect.clip_scores(clips, rec_start, seconds, window) if clips else None
            except Exception as exc:
                LOG.warning("Twitch clips lookup failed for %s: %s", source.login, exc)

    max_out = env_int("MAX_CLIPS_PER_SEGMENT", 2)
    # Look at a few more candidates than we keep so the AI judge has a choice.
    candidates = detect.pick_windows(seconds, window, signals, max_out * 2, env_float("MIN_SCORE", 0.15))
    LOG.info("%s: %d candidate windows (signals: %s)", path.name, len(candidates),
             ", ".join(k for k, v in signals.items() if v))

    made = 0
    for cand in candidates:
        if made >= max_out:
            break
        sig = hashlib.sha256(f"{path}|{stat.st_size}|{stat.st_mtime}|{cand.start}".encode()).hexdigest()[:16]
        if db.get_clip(sig):
            continue
        stem = f"{path.stem}_{sig}"
        srt = OUTPUT / f"{stem}.srt"
        transcript = ""
        if env_bool("CAPTIONS", True):
            try:
                transcript = transcribe(path, cand.start, cand.duration, srt)
            except Exception as exc:  # e.g. Whisper model can't download; still make the clip
                LOG.warning("Captions failed for %s @%.0fs, continuing without: %s", path.name, cand.start, str(exc)[:200])
                srt.unlink(missing_ok=True)
        frames = grab_frames(path, cand.start, cand.duration, OUTPUT)
        try:
            verdict = ai.judge(frames, transcript, source.name, source.series, cand.signals)
        finally:
            for f in frames:
                f.unlink(missing_ok=True)
        if not verdict.get("keep") or verdict.get("score", 0) < env_int("MIN_AI_SCORE", 6):
            LOG.info("Skipped %s @%.0fs: %s", path.name, cand.start, verdict.get("why"))
            srt.unlink(missing_ok=True)
            continue

        if not source.ai_text_allowed:
            # Campaign bans AI content: keep the AI's pick, but use plain human-style template text.
            plain = ai._fallback(source.name, source.series)
            verdict.update(title=plain["title"], description=plain["description"], hook="", hashtags=plain["hashtags"])

        overlay = OUTPUT / f"{stem}.ass"
        ad_label = os.getenv("AD_LABEL", "Paid partnership #ad") if source.ad_disclosure else ""
        overlay_ass(overlay, cand.duration, verdict["hook"], source.credit_line, ad_label)
        dest = OUTPUT / f"{stem}.mp4"
        render(path, cand.start, cand.duration, dest, srt=srt, overlay=overlay)

        tags = list(dict.fromkeys((["#ad"] if source.ad_disclosure else []) + verdict.get("hashtags", []) + source.required_hashtags))
        description = f"{verdict['description']}\n\n🎥 Credit: {source.credit_line}\n\n{' '.join(tags)}"
        title = verdict["title"][:94]
        if source.ad_disclosure:
            description = "#ad Paid partnership with " + source.name + "\n\n" + description
            title = f"{title} #ad"
        db.insert_clip({
            "id": sig, "source": str(path), "output": str(dest),
            "score": cand.score * 0.4 + verdict.get("score", 0) / 10 * 0.6,
            "status": initial_status(source),
            "title": title, "created_at": db.now_iso(), "ad_disclosure": int(source.ad_disclosure),
            "streamer": source.login, "series": source.series,
            "description": description, "hashtags": tags, "hook": verdict["hook"],
            "start_s": cand.start, "duration_s": cand.duration,
            "reasons": {"signals": cand.signals, "ai": verdict.get("why", "")},
        })
        LOG.info("Clip ready for review: %s  \"%s\"", sig, verdict["title"])
        made += 1
    return made


def scan_recordings(twitch=None) -> int:
    count = 0
    for path in sorted(RECORDINGS.glob("*")):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
            continue
        if time.time() - path.stat().st_mtime < 20:  # still being written
            continue
        try:
            count += process_file(path, twitch=twitch)
        except Unauthorized as exc:
            LOG.warning("Skipping: %s", exc)
        except Exception:
            LOG.exception("Failed processing %s", path)
    return count

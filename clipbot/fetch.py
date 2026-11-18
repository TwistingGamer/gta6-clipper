"""Bring campaign footage in: download approved YouTube videos, record live Twitch/Kick streams."""
from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path

from . import db
from .config import DATA, FFMPEG, ROOT, STREAMLINK
from .config import RECORDINGS
from .sources import Campaign, Input

LIVE_DIR = RECORDINGS / "_live"   # one recording per streamer, shared by every campaign that lists them

LOG = logging.getLogger("clipbot.fetch")


# ---------- YouTube (approved videos / channels named by the campaign) ----------

def _seen(campaign: str, key: str) -> bool:
    with db.connect() as con:
        return con.execute("SELECT 1 FROM fetched WHERE campaign=? AND key=?", (campaign, key)).fetchone() is not None


def _mark(campaign: str, key: str, path: str):
    with db.connect() as con:
        con.execute("INSERT OR REPLACE INTO fetched VALUES (?,?,?,?)", (campaign, key, path, db.now_iso()))


def youtube_ids(inp: Input) -> list[str]:
    """Video IDs for a youtube_video or the newest uploads of a youtube_channel."""
    import yt_dlp

    if inp.type == "youtube_video":
        with yt_dlp.YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
            return [ydl.extract_info(inp.target, download=False)["id"]]
    url = inp.target.rstrip("/")
    if not url.endswith("/videos"):
        url += "/videos"
    opts = {"quiet": True, "extract_flat": True, "playlistend": max(1, inp.max_videos)}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    return [e["id"] for e in info.get("entries", []) if e.get("id")]


def download_youtube(campaign: Campaign, inp: Input) -> list[Path]:
    import yt_dlp

    got = []
    for vid in youtube_ids(inp):
        if _seen(campaign.id, f"yt:{vid}"):
            continue
        out = campaign.folder / f"yt__{vid}.%(ext)s"
        opts = {
            "quiet": True, "noprogress": True, "outtmpl": str(out),
            "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b",
            "merge_output_format": "mp4",
            "cachedir": str(DATA / "cache" / "yt-dlp"),
            **({"ffmpeg_location": str(Path(FFMPEG).parent)} if Path(FFMPEG).parent != Path(".") else {}),
        }
        if inp.since:
            opts["daterange"] = yt_dlp.utils.DateRange(inp.since.replace("-", ""))
        start, end = inp.window
        if start or end:
            # Only download the approved part of the video.
            opts["download_ranges"] = yt_dlp.utils.download_range_func(None, [(start, end or float("inf"))])
            opts["force_keyframes_at_cuts"] = True
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={vid}", download=True)
            if info is None:  # filtered out by date
                _mark(campaign.id, f"yt:{vid}", "")
                continue
            path = campaign.folder / f"yt__{vid}.mp4"
            _mark(campaign.id, f"yt:{vid}", str(path))
            LOG.info("[%s] downloaded %s (%s)", campaign.id, vid, info.get("title", ""))
            got.append(path)
        except Exception as exc:
            LOG.warning("[%s] download of %s failed: %s", campaign.id, vid, str(exc)[:200])
    return got


def fetch_campaign(campaign: Campaign) -> int:
    n = 0
    for inp in campaign.inputs:
        if inp.type in ("youtube_video", "youtube_channel"):
            n += len(download_youtube(campaign, inp))
    return n


def folder_inputs(campaign: Campaign) -> list[Path]:
    dirs = [campaign.folder]
    for inp in campaign.inputs:
        if inp.type == "folder":
            p = Path(inp.target)
            dirs.append(p if p.is_absolute() else ROOT / p)
    return dirs


# ---------- Live streams (Twitch / Kick via Streamlink) ----------

def live_info(inp: Input) -> dict | None:
    """Streamlink metadata if the channel is live, else None. Works for Twitch and Kick."""
    try:
        r = subprocess.run([STREAMLINK, "--json", inp.url], capture_output=True, text=True, timeout=45)
        data = json.loads(r.stdout or "{}")
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        return None
    if data.get("error") or not data.get("streams"):
        return None
    return data.get("metadata") or {}


def category_ok(campaign: Campaign, category: str | None, title: str | None = None) -> bool:
    return campaign.game_ok(category, title)


def capture_segment(campaign: Campaign, inp: Input, seconds: int) -> Path:
    """Record one segment. Only for campaigns that allow recording. Never bypasses
    subscriber-only streams, DRM or other access controls."""
    ok, why = campaign.authorized()
    if not ok or not campaign.allow_recording:
        raise PermissionError(f"{campaign.id}: recording not permitted ({why if not ok else 'allow_recording is false'})")
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    out = LIVE_DIR / f"{inp.login}__{int(time.time())}.mp4"
    cmd = [STREAMLINK, "--stdout", inp.url, "best"]
    if Path(FFMPEG).parent != Path("."):
        cmd[1:1] = ["--ffmpeg-ffmpeg", FFMPEG]
    if inp.type == "twitch":
        cmd.insert(2, "--twitch-disable-ads")
    elif inp.type == "youtube_live":
        cmd.insert(2, "--hls-live-edge=1")
    producer = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        result = subprocess.run(
            [FFMPEG, "-y", "-loglevel", "error", "-i", "pipe:0", "-t", str(seconds),
             "-c", "copy", "-movflags", "+faststart", str(out)],
            stdin=producer.stdout, timeout=seconds + 90,
        )
        if result.returncode != 0 or not out.exists() or out.stat().st_size < 100_000:
            out.unlink(missing_ok=True)
            raise RuntimeError(f"Unable to capture {inp.url}")
        return out
    finally:
        if producer.stdout:
            producer.stdout.close()
        producer.terminate()
        try:
            producer.wait(timeout=8)
        except subprocess.TimeoutExpired:
            producer.kill()
            producer.wait()

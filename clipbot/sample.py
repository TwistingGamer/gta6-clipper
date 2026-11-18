"""One-shot preview: record a few minutes of a live stream, make one clip, never post it.

  python -m clipbot sample                 biggest live GTA stream right now (needs Twitch keys in .env)
  python -m clipbot sample kaicenat        a specific Twitch channel
  python -m clipbot sample kaicenat --minutes 5
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
import subprocess
import time

from . import db
from .chat import ChatLogger
from .config import FFMPEG, OUTPUT, RECORDINGS, STREAMLINK
from .pipeline import process_file
from .sources import Campaign, Input
from .twitch import Twitch

LOG = logging.getLogger("clipbot.sample")
SAMPLE_OUT = OUTPUT / "sample"


def pick_channel() -> tuple[str, str]:
    tw = Twitch()
    if not tw.configured:
        raise SystemExit("No channel given and no TWITCH_CLIENT_ID/SECRET in .env.\n"
                         "Either add the Twitch keys, or name a channel: python -m clipbot sample kaicenat")
    streams = tw.top_streams(limit=5)
    if not streams:
        raise SystemExit("No English GTA streams live right now. Name a channel: python -m clipbot sample <channel>")
    for s in streams:
        print(f"  {s['viewer_count']:>7,} viewers  {s['user_login']:<20} {s['game_name']}  {s['title'][:60]}")
    top = streams[0]
    return top["user_login"].lower(), top.get("title", "")


def run_sample(channel: str | None, minutes: float) -> None:
    db.init_db()
    login, title = (channel.lower().rsplit("/", 1)[-1], "") if channel else pick_channel()
    print(f"\nRecording {minutes:g} min of twitch.tv/{login} (and its chat) for a private preview...")
    folder = RECORDINGS / "_sample"
    folder.mkdir(parents=True, exist_ok=True)
    start = int(time.time())
    raw = folder / f"{login}__{start}.mp4"

    chat = ChatLogger(login)
    chat.start()
    producer = subprocess.Popen([STREAMLINK, "--stdout", "--twitch-disable-ads", f"https://www.twitch.tv/{login}", "best"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", "pipe:0", "-t", str(int(minutes * 60)),
                        "-c", "copy", "-movflags", "+faststart", str(raw)],
                       stdin=producer.stdout, timeout=minutes * 60 + 120)
    finally:
        chat.stop()
        if producer.stdout:
            producer.stdout.close()
        producer.terminate()
        err = (producer.stderr.read().decode(errors="ignore") if producer.stderr else "")[-400:]
    if not raw.exists() or raw.stat().st_size < 200_000:
        raise SystemExit(f"Couldn't record twitch.tv/{login}. Is it live? Streamlink said:\n{err}")

    # A throwaway 'campaign' just for this preview: nothing from it can ever be posted.
    preview = Campaign(
        id="sample", name=f"{login} preview", permission_type="own",
        evidence="private local preview, never posted", inputs=[Input("twitch", login)],
        series="gta6", min_clip_seconds=15, max_clip_seconds=45, title_template=f"{login} in GTA",
    )
    # A preview always shows the best of what it recorded, even if it wouldn't pass the viral bar.
    for k in ("MIN_MOMENT_SCORE", "AUDIO_MIN_PEAK", "STATIC_MOTION"):
        os.environ[k] = "0"
    os.environ["MAX_PER_STREAM_HOUR"] = "0"
    os.environ["NOTIFY_NEW_CLIPS"] = "false"   # previews aren't sent to Discord
    os.environ["MAX_CLIPS_PER_SEGMENT"] = "1"
    os.environ["MAX_CLIPS_PER_HOUR"] = "1"
    print("Finding the best moment and making the clip (first run downloads the caption model)...")
    process_file(raw, preview)

    SAMPLE_OUT.mkdir(parents=True, exist_ok=True)
    with db.connect() as con:
        rows = [dict(r) for r in con.execute("SELECT id, output, title FROM clips WHERE source=?", (str(raw.resolve()),))]
    if not rows:
        raise SystemExit("No moment was picked (stream too quiet or too short). Try --minutes 5 or another channel.")
    for r in rows:
        db.set_status(r["id"], "sample")  # never eligible for posting
        dest = SAMPLE_OUT / Path(r["output"]).name
        shutil.move(r["output"], dest)  # moved, not copied: no second copy left in the output folder
        for extra in (".srt", ".ass"):
            Path(r["output"]).with_suffix(extra).unlink(missing_ok=True)
        print(f"\nDone: {dest}\nTitle it would use: {r['title']}")
    raw.unlink(missing_ok=True)  # keep D: tidy; the clip is in output\\sample

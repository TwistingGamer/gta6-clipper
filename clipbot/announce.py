"""Announcement posts: a short vertical Vice City style card ("Kai Cenat plays GTA 6 tonight, 8 PM ET").

  python -m clipbot announce "Kai Cenat" "TONIGHT 8 PM ET" --what "STARTS GTA 6 LIVE" \
      --when2 "5 PM PT" --where twitch.tv/kaicenat --tags "#KaiCenat #GTA6"

Original artwork only (gradient sky, sun, grid, text): no game logos, no stream footage.
The card goes into the review queue under the 'own' campaign like any clip: approve it, then
`python -m clipbot post ID tiktok` stages it (or uploads it, in API mode).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import time
from pathlib import Path

from . import db
from .captions import CYAN, FONTS_DIR, NIGHT, ORANGE, PINK, WHITE, _ass_escape, _header, _style, ass_ts
from .config import FFMPEG, OUTPUT
from .render import filter_path

ANNOUNCE_DIR = OUTPUT / "announce"


def _circle(r: int, cx: int = 0, cy: int = 0) -> str:
    k = round(r * 0.5523)
    pts = [(0, -r), (k, -r), (r, -k), (r, 0), (r, k), (k, r), (0, r), (-k, r), (-r, k), (-r, 0), (-r, -k), (-k, -r), (0, -r)]
    pts = [(x + cx, y + cy) for x, y in pts]
    out = f"m {pts[0][0]} {pts[0][1]}"
    for i in range(1, 13, 3):
        out += " b " + " ".join(f"{x} {y}" for x, y in pts[i:i + 3])
    return out


def card_ass(path: Path, seconds: float, who: str, when: str, what: str, when2: str, where: str, mark: str):
    end = ass_ts(seconds)
    styles = [
        _style("Top", "Bungee", 64, CYAN, PINK, "&H00000000", 0, 1, 3, 0, 8, 60, 60, 0, spacing=6),
        _style("Who", "Pacifico", 190, PINK, NIGHT, "&H96000000", 0, 1, 7, 6, 8, 40, 40, 0),
        _style("WhoGlow", "Pacifico", 190, PINK, CYAN, "&H00000000", 0, 1, 18, 0, 8, 40, 40, 0),
        _style("What", "Bungee", 88, WHITE, NIGHT, "&H96000000", 0, 1, 5, 4, 8, 60, 60, 0),
        _style("When", "Bungee", 124, WHITE, ORANGE, "&H96000000", 0, 1, 6, 4, 8, 40, 40, 0),
        _style("Small", "Bungee", 66, WHITE, NIGHT, "&H96000000", 0, 1, 4, 2, 8, 40, 40, 0),
        _style("Shape", "Arial", 20, WHITE, WHITE, "&H00000000", 0, 1, 0, 0, 7, 0, 0, 0),
        _style("Mark", "Pacifico", 52, ORANGE, PINK, "&H00000000", 0, 1, 2, 0, 9, 40, 44, 60),
    ]
    ev = []

    def line(layer, style, y, text, tags=""):
        ev.append(f"Dialogue: {layer},{ass_ts(0)},{end},{style},,0,0,0,,{{\\pos(540,{y}){tags}}}{text}")

    def shape(layer, drawing, tags):
        ev.append(f"Dialogue: {layer},{ass_ts(0)},{end},Shape,,0,0,0,,{{\\pos(0,0){tags}\\p1}}{drawing}{{\\p0}}")

    # Sun with stripes, sitting on the horizon (y=1180), and a neon floor grid below it.
    sun_y, horizon = 1040, 1180
    ev.append(f"Dialogue: 0,{ass_ts(0)},{end},Shape,,0,0,0,,{{\\pos(540,{sun_y})\\1c{ORANGE}\\blur3\\p1}}{_circle(280)}{{\\p0}}")
    ev.append(f"Dialogue: 0,{ass_ts(0)},{end},Shape,,0,0,0,,{{\\pos(540,{sun_y})\\1c{PINK}\\alpha&H40&\\clip(0,{sun_y},1080,{horizon})\\p1}}{_circle(280)}{{\\p0}}")
    for i, y in enumerate(range(sun_y + 40, horizon, 34)):
        shape(1, f"m 0 {y} l 1080 {y} l 1080 {y + 6 + i * 2} l 0 {y + 6 + i * 2}",
              f"\\1c&H00401A2E&\\clip(1,{_circle(280, 540, sun_y)})")
    shape(1, f"m 0 {horizon} l 1080 {horizon} l 1080 1920 l 0 1920", "\\1c&H00281024&")
    for x in range(-1620, 2701, 180):  # lines running to the vanishing point
        shape(2, f"m 538 {horizon} l 542 {horizon} l {x + 3} 1920 l {x - 3} 1920", f"\\1c{PINK}\\blur1")
    y, step = horizon + 8, 14
    while y < 1920:  # horizontal lines, further apart toward the viewer
        shape(2, f"m 0 {y} l 1080 {y} l 1080 {y + 3} l 0 {y + 3}", f"\\1c{CYAN}\\alpha&H30&")
        step = int(step * 1.35)
        y += step

    line(3, "Top", 250, "★ LIVE ALERT ★", "\\blur1\\fad(200,0)")
    line(3, "WhoGlow", 360, _ass_escape(who), "\\blur14\\fad(300,0)")
    line(4, "Who", 360, _ass_escape(who), "\\fad(300,0)\\t(0,600,\\fscx106\\fscy106)\\t(600,1200,\\fscx100\\fscy100)")
    if what:
        line(3, "What", 640, _ass_escape(what), "\\fad(500,0)")
    line(4, "When", 1300, _ass_escape(when), "\\blur1\\fad(700,0)")
    if when2:
        line(4, "Small", 1440, _ass_escape(when2), "\\fad(900,0)")
    if where:
        line(4, "Small", 1700, _ass_escape(where), "\\fad(1100,0)")
    if mark:
        ev.append(f"Dialogue: 5,{ass_ts(0)},{end},Mark,,0,0,0,,{{\\alpha&H40&}}{_ass_escape(mark)}")
    path.write_text(_header(styles) + "".join(e + "\n" for e in ev), encoding="utf-8")


def make_card(out: Path, who: str, when: str, what: str = "", when2: str = "", where: str = "",
              seconds: float = 8.0, mark: str = "") -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    ass = out.with_suffix(".ass")
    card_ass(ass, seconds, who, when, what, when2, where, mark)
    sky = ("gradients=s=1080x1920:d={d}:r=30:type=linear:x0=540:y0=0:x1=540:y1=1180:nb_colors=3:"
           "c0=0x14062b:c1=0x7a1fa2:c2=0xff6a3d:speed=0.0001").format(d=seconds)
    try:
        subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", sky,
             "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={seconds}",
             "-filter_complex", f"[0:v]subtitles=filename='{filter_path(ass)}':fontsdir='{filter_path(FONTS_DIR)}',format=yuv420p[v]",
             "-map", "[v]", "-map", "1:a", "-t", str(seconds),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", str(out)],
            check=True,
        )
    finally:
        ass.unlink(missing_ok=True)
    return out


def run_announce(who: str, when: str, what: str, when2: str, where: str, tags: str, caption: str,
                 seconds: float) -> dict:
    db.init_db()
    cid = "own"
    clip_id = hashlib.sha256(f"announce|{who}|{when}|{time.time()}".encode()).hexdigest()[:16]
    mark = os.getenv("WATERMARK_TEXT", "@gta6.clippaa").strip()
    slug = "-".join(__import__("re").findall(r"[a-z0-9]+", who.lower()))[:30]
    stamp = time.strftime("%Y-%m-%d_%H%M")
    out = make_card(ANNOUNCE_DIR / f"{stamp}_{slug}_announcement_{clip_id}.mp4", who, when, what, when2, where, seconds, mark)
    hashtags = [t if t.startswith("#") else f"#{t}" for t in tags.split()] or ["#GTA6"]
    title = " · ".join(x for x in (who, what or "LIVE", when) if x)[:100]
    body = caption or f"{title}{f' ({when2})' if when2 else ''}. Who's tuning in? 👀"
    description = "\n\n".join(x for x in (body, f"📺 {where}" if where else "", " ".join(hashtags)) if x)
    db.insert_clip({
        "id": clip_id, "source": "", "output": str(out), "score": 1.0, "status": "pending_review",
        "title": title, "created_at": db.now_iso(), "streamer": "", "campaign": cid, "series": "gta6",
        "description": description, "hashtags": hashtags, "hook": "", "start_s": 0, "duration_s": seconds,
        "ad_disclosure": 0, "reasons": {"announce": True, "submit_to": [cid]},
    })
    return {"id": clip_id, "output": str(out), "title": title, "description": description}

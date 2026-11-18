"""FFmpeg rendering: 1080x1920 vertical clip (facecam/gameplay split, fill or fit), captions and overlays."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .config import FFMPEG, FFPROBE, env_int
from .captions import FONTS_DIR, captions_ass
from .layout import Layout, video_graph


def filter_path(p: Path) -> str:
    # FFmpeg filter args need escaped Windows drive colons, quotes and brackets.
    return (p.resolve().as_posix().replace("\\", "\\\\").replace(":", "\\:")
            .replace("'", "\\'").replace("[", "\\[").replace("]", "\\]"))


def source_fps(path: Path) -> float:
    try:
        out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=avg_frame_rate",
                              "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True).stdout.strip()
        num, den = out.split("/") if "/" in out else (out, "1")
        return float(num) / float(den or 1)
    except Exception:
        return 30.0


def video_quality(source: Path) -> list[str]:
    """Posting copy: source frame rate up to 60, CRF 18 (visually lossless for phones), High profile.
    Settings: VIDEO_CRF (18), VIDEO_PRESET (medium; 'slow' is a bit sharper but ~2x slower), MAX_FPS (60)."""
    fps = 60 if source_fps(source) >= 50 and env_int("MAX_FPS", 60) >= 60 else 30
    return ["-c:v", "libx264", "-preset", os.getenv("VIDEO_PRESET", "medium"), "-crf", os.getenv("VIDEO_CRF", "18"),
            "-profile:v", "high", "-pix_fmt", "yuv420p", "-maxrate", "20M", "-bufsize", "40M",
            "-r", str(fps), "-g", str(fps * 2)]


def watermark_image() -> Path | None:
    """WATERMARK_IMAGE=path to a PNG logo (transparent background) shown small in the top-left corner."""
    p = os.getenv("WATERMARK_IMAGE", "").strip().strip('"')
    return Path(p) if p and Path(p).is_file() else None


def render(source: Path, start: float, duration: float, dest: Path,
           srt: Path | None = None, overlay: Path | None = None, layout: Layout | None = None,
           logo: Path | None = None):
    layout = layout or Layout("fit")
    graph = video_graph(layout)
    inputs = ["-ss", str(start), "-i", str(source)]
    if logo:
        inputs += ["-i", str(logo)]
        graph = graph.replace("[v0]", "[vb]") + (";[1:v]scale=170:-1,format=rgba,colorchannelmixer=aa=0.7[logo];"
                                                 "[vb][logo]overlay=40:60[v0]")
    fonts = filter_path(FONTS_DIR)
    last = "v0"
    if srt and srt.exists() and srt.stat().st_size:
        cap = srt.with_suffix(".cap.ass")
        captions_ass(srt, cap, layout.caption_y)
        graph += f";[{last}]subtitles=filename='{filter_path(cap)}':fontsdir='{fonts}'[v1]"
        last = "v1"
    if overlay and overlay.exists():
        graph += f";[{last}]subtitles=filename='{filter_path(overlay)}':fontsdir='{fonts}'[v2]"
        last = "v2"
    try:
        subprocess.run(
            [FFMPEG, "-y", "-v", "error", *inputs, "-t", str(duration),
             "-filter_complex", graph, "-map", f"[{last}]", "-map", "0:a?",
             *video_quality(source), "-c:a", "aac", "-b:a", os.getenv("AUDIO_BITRATE", "192k"), "-ar", "48000",
             "-movflags", "+faststart", str(dest)],
            check=True,
        )
    finally:
        if srt:
            srt.with_suffix(".cap.ass").unlink(missing_ok=True)


def grab_frames(source: Path, start: float, duration: float, out_dir: Path, count: int = 4) -> list[Path]:
    """JPEG stills spread across the window, for the AI judge."""
    frames = []
    for i in range(count):
        t = start + duration * (i + 0.5) / count
        p = out_dir / f"_frame_{int(start)}_{i}.jpg"
        subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(source), "-frames:v", "1",
             "-vf", "scale=768:-2", "-q:v", "4", str(p)],
            check=True,
        )
        if p.exists():
            frames.append(p)
    return frames

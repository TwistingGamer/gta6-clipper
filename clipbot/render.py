"""FFmpeg rendering: 1080x1920 vertical clip (facecam/gameplay split, fill or fit), captions and overlays."""
from __future__ import annotations

import subprocess
from pathlib import Path

from .config import FFMPEG
from .layout import Layout, video_graph


def filter_path(p: Path) -> str:
    # FFmpeg filter args need escaped Windows drive colons, quotes and brackets.
    return (p.resolve().as_posix().replace("\\", "\\\\").replace(":", "\\:")
            .replace("'", "\\'").replace("[", "\\[").replace("]", "\\]"))


def render(source: Path, start: float, duration: float, dest: Path,
           srt: Path | None = None, overlay: Path | None = None, layout: Layout | None = None):
    layout = layout or Layout("fit")
    graph = video_graph(layout)
    # SRT styles are measured on a 288-high canvas; convert the layout's caption height to it.
    margin = round(layout.caption_y / 1920 * 288)
    last = "v0"
    if srt and srt.exists() and srt.stat().st_size:
        graph += (f";[{last}]subtitles=filename='{filter_path(srt)}':"
                  f"force_style='FontName=Arial,FontSize=16,Bold=1,Alignment=2,MarginV={margin},Outline=3,Shadow=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000'[v1]")
        last = "v1"
    if overlay and overlay.exists():
        graph += f";[{last}]subtitles=filename='{filter_path(overlay)}'[v2]"
        last = "v2"
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-ss", str(start), "-i", str(source), "-t", str(duration),
         "-filter_complex", graph, "-map", f"[{last}]", "-map", "0:a?",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
         "-r", "30", "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart", str(dest)],
        check=True,
    )


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

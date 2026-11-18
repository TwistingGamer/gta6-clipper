"""Choose how a 16:9 stream fills a 9:16 phone screen.

  split  facecam on top, gameplay below (the usual clipper look). Used when a
         webcam face sits in the same spot across the clip.
  fill   gameplay cropped to fill the whole screen (no facecam found).
  fit    whole frame shrunk into the middle with a blurred background (old look).

FRAME_MODE=auto (default) picks split or fill per clip. FACECAM=x,y,w,h (fractions
of the frame, e.g. 0,0.55,0.25,0.35) forces a facecam box if detection misses it.
"""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import FFMPEG, FFPROBE

LOG = logging.getLogger("clipbot.layout")
FACE_MODEL = Path(__file__).resolve().parent / "models" / "face_detection_yunet_2023mar.onnx"
W, H = 1080, 1920
CAM_H = 640          # top band for the facecam in split mode
GAME_H = H - CAM_H   # gameplay band below it


@dataclass
class Layout:
    mode: str                                   # split | fill | fit
    cam: tuple[int, int, int, int] | None = None  # crop box in source pixels
    src: tuple[int, int] = (1920, 1080)

    @property
    def hook_top(self) -> int:
        """Top of the hook text: across the seam between facecam and gameplay, else near the top."""
        return CAM_H - 70 if self.mode == "split" else 230

    @property
    def caption_y(self) -> int:
        """Where captions sit, in pixels from the bottom of the 1920-high frame."""
        if self.mode == "split":
            return GAME_H - 260   # just below the facecam, over the top of the gameplay
        return 560


def video_size(path: Path) -> tuple[int, int]:
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "csv=p=0:s=x", str(path)], capture_output=True, text=True, check=True).stdout
    w, h = out.strip().splitlines()[0].split("x")[:2]
    return int(w), int(h)


def _frame(path: Path, t: float, width: int, height: int) -> np.ndarray | None:
    raw = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                          "-vf", f"scale={width}:{height}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
                         capture_output=True).stdout
    if len(raw) != width * height * 3:
        return None
    return np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)


def _forced_cam(src: tuple[int, int]) -> tuple[int, int, int, int] | None:
    spec = os.getenv("FACECAM", "").strip()
    if not spec:
        return None
    try:
        x, y, w, h = (float(v) for v in spec.split(","))
    except ValueError:
        LOG.warning("FACECAM should be x,y,w,h fractions, got %r", spec)
        return None
    return int(x * src[0]), int(y * src[1]), int(w * src[0]), int(h * src[1])


def find_facecam(path: Path, start: float, duration: float, src: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """A webcam face stays put; faces in the game move. Look at 8 frames and keep a face that doesn't."""
    try:
        import cv2
    except ImportError:
        LOG.info("opencv not installed: no facecam detection (pip install opencv-python-headless)")
        return None
    if not FACE_MODEL.is_file():
        return None
    aw = 960
    ah = int(round(src[1] * aw / src[0] / 2) * 2)
    detector = cv2.FaceDetectorYN.create(str(FACE_MODEL), "", (aw, ah), 0.7)
    hits: list[tuple[float, float, float]] = []   # centre x, centre y, size (fractions of the frame)
    looked = 0
    for i in range(8):
        img = _frame(path, start + duration * (i + 0.5) / 8, aw, ah)
        if img is None:
            continue
        looked += 1
        _, faces = detector.detect(img)
        for x, y, w, h in ([] if faces is None else [f[:4] for f in faces]):
            # Thumbnails and far-away characters are tiny; a face filling the screen is the game.
            if not aw * 0.03 <= w <= aw * 0.35:
                continue
            hits.append(((x + w / 2) / aw, (y + h / 2) / ah, w / aw))
    if looked < 4 or not hits:
        return None
    best, best_n = None, 0
    for cx, cy, s in hits:     # the face position seen in the most frames
        n = sum(1 for x, y, _ in hits if abs(x - cx) < 0.04 and abs(y - cy) < 0.06)
        if n > best_n:
            best, best_n = (cx, cy, s), n
    if best_n < max(4, int(looked * 0.6)):
        return None
    same = [h for h in hits if abs(h[0] - best[0]) < 0.04 and abs(h[1] - best[1]) < 0.06]
    cx, cy, s = (float(np.median([h[k] for h in same])) for k in range(3))
    # Webcam box around the face: about 5 face-widths wide (webcam framing), at the facecam band's shape.
    bw = min(src[0], s * src[0] * 5.0)
    bh = min(src[1], bw * CAM_H / W)
    bw = bh * W / CAM_H
    x = min(max(0.0, cx * src[0] - bw / 2), src[0] - bw)
    y = min(max(0.0, cy * src[1] - bh * 0.45), src[1] - bh)
    return int(x), int(y), int(bw), int(bh)


def choose(path: Path, start: float, duration: float) -> Layout:
    mode = os.getenv("FRAME_MODE", "auto").lower()
    try:
        src = video_size(path)
    except Exception:
        src = (1920, 1080)
    if src[0] <= src[1]:          # already vertical
        return Layout("fill", None, src)
    if mode == "fit":
        return Layout("fit", None, src)
    cam = _forced_cam(src)
    if cam is None and mode in ("auto", "split"):
        try:
            cam = find_facecam(path, start, duration, src)
        except Exception as exc:
            LOG.warning("facecam detection failed: %s", exc)
    if cam and mode != "fill":
        return Layout("split", cam, src)
    return Layout("fill", None, src)


def video_graph(layout: Layout) -> str:
    """filter_complex chain from [0:v] to [v0]."""
    if layout.mode == "fit":
        return ("[0:v]split=2[back][front];"
                "[back]scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=8:3,scale=1080:1920[bg];"
                "[front]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
                "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]")
    if layout.mode == "fill":
        return f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1[v0]"
    x, y, w, h = layout.cam
    # Gameplay: centre of the frame at the lower band's shape, full height.
    return (f"[0:v]split=2[c][g];"
            f"[c]crop={w}:{h}:{x}:{y},scale={W}:{CAM_H},setsar=1[cam];"
            f"[g]scale=-2:{GAME_H}:force_original_aspect_ratio=increase,crop={W}:{GAME_H},setsar=1[game];"
            f"[cam][game]vstack=inputs=2[v0]")

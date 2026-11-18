"""Find candidate highlight windows in a recording.

Signals, each scored 0..1 per 10-second step:
  * audio  - loudness peaks and variation (screams, explosions, crashes)
  * chat   - Twitch chat rate spikes and hype emotes (when a chat log exists)
  * clips  - viewers already made a Twitch clip around this moment
The weighted mix ranks windows; the AI pass (ai.py) then makes the final call.
"""
from __future__ import annotations

import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .config import FFMPEG, FFPROBE

STEP = 10


@dataclass
class Candidate:
    start: float
    duration: float
    score: float
    signals: dict = field(default_factory=dict)


def ffprobe_seconds(path: Path) -> float:
    out = subprocess.run(
        [FFPROBE, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
        check=True, text=True, capture_output=True,
    ).stdout.strip()
    return float(out)


def n_windows(seconds: float, window: int) -> int:
    return max(1, int(math.ceil(max(0.0, seconds - window) / STEP)) + 1)


def audio_per_second(path: Path) -> np.ndarray:
    """Loudness per second relative to the recording's typical level (1.0 = normal talking)."""
    raw = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
        capture_output=True, check=True,
    ).stdout
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    sec = len(x) // 8000
    if not sec:
        return np.zeros(0, dtype=np.float32)
    rms = np.sqrt(np.mean(x[: sec * 8000].reshape(sec, 8000) ** 2, axis=1))
    # Normalise against the recording's own typical level so quiet and loud streamers compare fairly.
    ref = float(np.median(rms)) or 1e-4
    return rms / ref


def audio_scores(path: Path, seconds: float, window: int, rel: np.ndarray | None = None) -> list[float]:
    """Per-window loudness score, normalised to this recording."""
    rel = audio_per_second(path) if rel is None else rel
    n = n_windows(seconds, window)
    if not len(rel):
        return [0.0] * n
    out = []
    for i in range(n):
        chunk = rel[i * STEP: i * STEP + window]
        if not len(chunk):
            out.append(0.0)
            continue
        peak = float(np.quantile(chunk, 0.9))
        spread = float(np.std(chunk))
        out.append(float(min(1.0, 0.3 * (peak - 1) + 0.3 * spread)))
    return [max(0.0, v) for v in out]


def clip_scores(clip_times: list[tuple[float, int]], rec_start: float, seconds: float, window: int) -> list[float]:
    """clip_times: (unix time the moment happened, view_count) for Twitch clips."""
    n = n_windows(seconds, window)
    out = [0.0] * n
    for t, views in clip_times:
        off = t - rec_start
        for i in range(n):
            if i * STEP <= off <= i * STEP + window:
                out[i] = min(1.0, out[i] + 0.5 + min(0.5, math.log10(max(views, 1)) / 8))
    return out


def combine(signals: dict[str, list[float] | None], weights: dict[str, float]) -> list[float]:
    present = {k: v for k, v in signals.items() if v}
    total_w = sum(weights[k] for k in present) or 1.0
    n = max(len(v) for v in present.values())
    return [sum(weights[k] * (v[i] if i < len(v) else 0.0) for k, v in present.items()) / total_w for i in range(n)]


def pick_windows(seconds: float, window: int, signals: dict[str, list[float] | None],
                 max_count: int, min_score: float, weights: dict[str, float] | None = None) -> list[Candidate]:
    if seconds < 10:
        return []
    weights = weights or {"audio": 0.3, "chat": 0.4, "clips": 0.3}  # a viewer-made Twitch clip is a strong vote
    mixed = combine(signals, weights)
    order = sorted(range(len(mixed)), key=lambda i: mixed[i], reverse=True)
    chosen: list[Candidate] = []
    for i in order:
        if mixed[i] < min_score:
            break
        start = float(i * STEP)
        dur = float(min(window, seconds - start))
        if dur < 10:
            continue
        if all(abs(start - c.start) >= window for c in chosen):
            chosen.append(Candidate(start, dur, mixed[i], {k: round(v[i], 3) for k, v in signals.items() if v and i < len(v)}))
            if len(chosen) >= max_count:
                break
    return chosen


def refine(cand: Candidate, seconds: float, audio: np.ndarray | None, chat_ps: np.ndarray | None,
           lead: float, tail: float, chat_lag: float = 5.0) -> Candidate:
    """Re-centre a window on its peak second so the viewer sees the build-up.

    The clip starts `lead` seconds before the moment (the setup) and ends `tail`
    seconds after it (the payoff and reaction). Chat reacts a few seconds late,
    so chat activity is shifted back by `chat_lag`.
    """
    a, b = int(cand.start), int(min(seconds, cand.start + cand.duration + 8))
    if b - a < 3:
        return cand
    e = np.zeros(b - a, dtype=np.float32)
    if audio is not None and len(audio):
        seg = audio[a:b]
        if len(seg):
            loud = np.clip(seg - 1.0, 0, None)
            e[:len(seg)] += loud / max(float(loud.max()), 1e-3)
    if chat_ps is not None and len(chat_ps):
        lagged = chat_ps[a + int(chat_lag): b + int(chat_lag)]
        if len(lagged) and lagged.max() > 0:
            e[:len(lagged)] += 1.3 * lagged / lagged.max()
    if not e.any():
        return cand
    smooth = np.convolve(e, np.array([1.0, 2.0, 1.0]) / 4, "same")
    peak = a + int(np.argmax(smooth))
    start = max(0.0, peak - lead)
    end = min(seconds, peak + tail)
    if end - start < lead + tail:          # near the edges: keep the full length where possible
        start = max(0.0, end - (lead + tail))
        end = min(seconds, start + lead + tail)
    if end - start < 10:
        return cand
    return Candidate(float(start), float(end - start), cand.score, cand.signals | {"peak_s": peak})


def motion(path: Path, start: float, duration: float, count: int = 8) -> float:
    """Average change between frames (0-255) across the window: menus and loading screens sit near 0."""
    frames = []
    for i in range(count):
        raw = subprocess.run([FFMPEG, "-v", "error", "-ss", f"{start + duration * (i + 0.5) / count:.2f}", "-i", str(path),
                              "-frames:v", "1", "-vf", "scale=96:54", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                             capture_output=True).stdout
        if len(raw) == 96 * 54:
            frames.append(np.frombuffer(raw, dtype=np.uint8).astype(np.float32))
    if len(frames) < 2:
        return 255.0
    return float(np.mean([np.mean(np.abs(a - b)) for a, b in zip(frames, frames[1:])]))

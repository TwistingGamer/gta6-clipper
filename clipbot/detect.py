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


def audio_scores(path: Path, seconds: float, window: int) -> list[float]:
    """Per-window loudness score from 8 kHz mono PCM, normalised to this recording."""
    raw = subprocess.run(
        [FFMPEG, "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"],
        capture_output=True, check=True,
    ).stdout
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
    n = n_windows(seconds, window)
    if not len(x):
        return [0.0] * n
    sec = len(x) // 8000
    rms = np.sqrt(np.mean(x[: sec * 8000].reshape(sec, 8000) ** 2, axis=1)) if sec else np.array([0.0])
    # Normalise against the recording's own typical level so quiet and loud streamers compare fairly.
    ref = float(np.median(rms)) or 1e-4
    rel = rms / ref
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
                 max_count: int, min_score: float) -> list[Candidate]:
    if seconds < 10:
        return []
    weights = {"audio": 0.35, "chat": 0.45, "clips": 0.2}
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

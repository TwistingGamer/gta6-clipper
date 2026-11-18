"""Twitch chat logging and chat-spike scoring.

Chat is the best highlight signal for streams: when something funny or wild
happens, message rate jumps and viewers spam emotes like KEKW / OMEGALUL or
type "clip it". We read chat anonymously over Twitch's IRC interface
(read-only "justinfan" login, which Twitch supports for reading public chat),
and append one JSON line per message to data/chat/<login>.jsonl.
"""
from __future__ import annotations

import json
import os
import logging
import random
import re
import socket
import threading
import time

import numpy as np

from .config import CHAT

LOG = logging.getLogger("clipbot.chat")

HYPE = re.compile(
    r"\b(kekw|omegalul|lul|lmao+|lmfao|icant|pog+|pogchamp|poggers|wtf+|clip\s*(it|that)|"
    r"no\s*way|holy|w+|l+|monkas|pepelaugh|sadge|aware)\b|\?{3,}",
    re.IGNORECASE,
)


def chat_file(login: str):
    return CHAT / f"{login.lower()}.jsonl"


class SpikeDetector:
    """Live 'something just happened' detector on the chat message rate.

    Fires when the last ~8 seconds of chat run SPIKE_FACTOR times above the
    channel's recent normal (median of the last 5 minutes) and enough of it is
    hype (KEKW, "clip it", ...). One trigger per SPIKE_COOLDOWN seconds.
    """

    def __init__(self, on_spike, factor: float = 3.0, min_rate: float = 2.0, cooldown: float = 60.0):
        self.on_spike = on_spike
        self.factor, self.min_rate, self.cooldown = factor, min_rate, cooldown
        self.counts: dict[int, list[int]] = {}   # second -> [messages, hype messages]
        self.last_fire = 0.0

    def add(self, t: float, text: str):
        sec = int(t)
        c = self.counts.setdefault(sec, [0, 0])
        c[0] += 1
        if HYPE.search(text):
            c[1] += 1
        self._check(sec)

    def _check(self, now: int):
        for old in [s for s in self.counts if s < now - 300]:
            del self.counts[old]
        if now - self.last_fire < self.cooldown or len(self.counts) < 30:
            return
        recent = [self.counts.get(s, [0, 0]) for s in range(now - 8, now + 1)]
        rate = sum(c[0] for c in recent) / 9
        hype = sum(c[1] for c in recent) / max(1, sum(c[0] for c in recent))
        history = sorted(self.counts.get(s, [0, 0])[0] for s in range(now - 300, now - 8))
        normal = max(0.5, history[len(history) // 2]) if history else 0.5
        if rate >= self.min_rate and rate >= self.factor * normal and hype >= 0.15:
            self.last_fire = now
            self.on_spike(float(now), rate / normal, hype)


class ChatLogger(threading.Thread):
    """Background thread that logs one channel's chat until stop() is called,
    and optionally calls on_spike(login, time, lift, hype) when chat erupts."""

    def __init__(self, login: str, on_spike=None):
        super().__init__(daemon=True, name=f"chat-{login}")
        self.login = login.lower()
        self._halt = threading.Event()
        self.detector = None
        if on_spike:
            self.detector = SpikeDetector(
                lambda t, lift, hype: on_spike(self.login, t, lift, hype),
                factor=float(os.getenv("SPIKE_FACTOR", "3")),
                min_rate=float(os.getenv("SPIKE_MIN_RATE", "2")),
                cooldown=float(os.getenv("SPIKE_COOLDOWN", "60")),
            )

    def stop(self):
        self._halt.set()

    def run(self):
        backoff = 2
        while not self._halt.is_set():
            try:
                self._session()
                backoff = 2
            except Exception as exc:  # network blips are normal; reconnect
                LOG.warning("chat %s disconnected: %s", self.login, exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def _session(self):
        sock = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=30)
        nick = f"justinfan{random.randint(10000, 99999)}"
        sock.sendall(f"NICK {nick}\r\nJOIN #{self.login}\r\n".encode())
        buf = ""
        with sock, chat_file(self.login).open("a", encoding="utf-8") as out:
            while not self._halt.is_set():
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    sock.sendall(b"PING :keepalive\r\n")
                    continue
                if not data:
                    raise ConnectionError("server closed connection")
                buf += data.decode("utf-8", errors="ignore")
                *lines, buf = buf.split("\r\n")
                for line in lines:
                    if line.startswith("PING"):
                        sock.sendall(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                    elif " PRIVMSG " in line:
                        text = line.split(" :", 1)[1] if " :" in line[1:] else ""
                        now = time.time()
                        out.write(json.dumps({"t": now, "m": text[:200]}) + "\n")
                        if self.detector:
                            try:
                                self.detector.add(now, text)
                            except Exception:
                                LOG.exception("spike detector error")
                out.flush()


def load_chat(login: str, start: float, end: float) -> list[tuple[float, str]]:
    path = chat_file(login)
    if not path.exists():
        return []
    msgs = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if start <= rec["t"] <= end:
                msgs.append((rec["t"], rec.get("m", "")))
    return msgs


def chat_scores(login: str, rec_start: float, seconds: float, step: int = 10, window: int = 36) -> list[float] | None:
    """Score each `step`-second window start by chat activity, 0..1.

    Chat lags the action by a few seconds, so we look slightly *after* each
    window. Returns None when there is no chat log for this recording.
    """
    msgs = load_chat(login, rec_start, rec_start + seconds + 15)
    if len(msgs) < 20:
        return None
    per_sec = np.zeros(int(seconds) + 16, dtype=np.float32)
    hype = np.zeros_like(per_sec)
    for t, m in msgs:
        i = int(t - rec_start)
        if 0 <= i < len(per_sec):
            per_sec[i] += 1
            if HYPE.search(m):
                hype[i] += 1
    baseline = float(np.median(per_sec[per_sec > 0])) if (per_sec > 0).any() else 1.0
    n = max(1, int(np.ceil(max(0, seconds - window) / step)) + 1)
    out = []
    for k in range(n):
        a, b = k * step + 3, k * step + window + 8   # shift for chat reaction delay
        rate = float(per_sec[a:b].mean()) if b > a else 0.0
        peak = float(np.convolve(per_sec[a:b], np.ones(5), "valid").max() / 5) if b - a >= 5 else rate
        hype_share = float(hype[a:b].sum() / max(1.0, per_sec[a:b].sum()))
        lift = (0.5 * rate + 0.5 * peak) / max(baseline, 0.5)
        out.append(0.5 * float(np.log2(max(lift, 1.0))) + hype_share)
    # Relative to this recording's busiest moment, so big and small chats compare fairly.
    top = max(out) or 1.0
    return [x / top for x in out]


def chat_per_second(login: str, rec_start: float, seconds: float) -> np.ndarray | None:
    """Messages per second (hype emotes count double) across a recording."""
    msgs = load_chat(login, rec_start, rec_start + seconds + 15)
    if len(msgs) < 20:
        return None
    per_sec = np.zeros(int(seconds) + 16, dtype=np.float32)
    for t, m in msgs:
        i = int(t - rec_start)
        if 0 <= i < len(per_sec):
            per_sec[i] += 2 if HYPE.search(m) else 1
    return per_sec

"""Rolling live buffer: keep only the last few minutes of a stream, cut out moments on demand.

Instead of recording whole streams (gigabytes per hour), each live stream is
written as short .ts pieces into data/buffer/<login>/ and anything older than
BUFFER_MINUTES is deleted. When chat erupts, the pieces around that moment are
joined into recordings/_live/<login>__<start time>.mp4, which the normal
clipping pipeline then picks up. Disk use stays flat (~0.5 GB per live stream).
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path

from .config import DATA, FFMPEG, FFPROBE, STREAMLINK, env_int
from .fetch import LIVE_DIR
from .sources import Campaign, Input

LOG = logging.getLogger("clipbot.buffer")
BUFFER_ROOT = DATA / "buffer"
PIECE_SECONDS = 6


def _piece_span(p: Path) -> tuple[float, float]:
    """(start, end) wall-clock time of one piece: it finished being written at its mtime."""
    end = p.stat().st_mtime
    return end - PIECE_SECONDS, end


class RollingBuffer(threading.Thread):
    def __init__(self, campaign: Campaign, inp: Input):
        super().__init__(daemon=True, name=f"buffer-{inp.login}")
        self.campaign, self.inp = campaign, inp
        self.login = inp.login
        self.dir = BUFFER_ROOT / self.login
        self.dir.mkdir(parents=True, exist_ok=True)
        self._halt = threading.Event()
        self._lock = threading.Lock()

    def stop(self):
        self._halt.set()

    # ---- writing ----
    def run(self):
        while not self._halt.is_set():
            try:
                self._session()
            except Exception as exc:
                LOG.warning("buffer %s: %s", self.login, exc)
            if not self._halt.is_set():
                time.sleep(15)

    def _session(self):
        ok, why = self.campaign.authorized()
        if not ok or not self.campaign.allow_recording:
            raise PermissionError(f"recording not permitted for {self.campaign.id}")
        cmd = [STREAMLINK, "--stdout", self.inp.url, os.getenv("BUFFER_QUALITY", "best")]
        if self.inp.type == "twitch":
            cmd.insert(2, "--twitch-disable-ads")
        producer = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        pattern = str(self.dir / f"{int(time.time())}_%06d.ts")
        writer = subprocess.Popen(
            [FFMPEG, "-loglevel", "error", "-i", "pipe:0", "-c", "copy", "-f", "segment",
             "-segment_time", str(PIECE_SECONDS), "-segment_format", "mpegts", "-reset_timestamps", "1", pattern],
            stdin=producer.stdout,
        )
        LOG.info("buffering %s (last %d min kept)", self.login, env_int("BUFFER_MINUTES", 8))
        try:
            while not self._halt.is_set() and writer.poll() is None:
                self.prune()
                time.sleep(5)
        finally:
            for proc in (writer, producer):
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()

    def prune(self):
        cutoff = time.time() - env_int("BUFFER_MINUTES", 8) * 60
        with self._lock:
            for p in self.dir.glob("*.ts"):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink()
                except OSError:
                    pass  # piece still open on Windows; next round

    # ---- cutting ----
    def cut(self, t_from: float, t_to: float) -> Path | None:
        """Join the pieces covering [t_from, t_to] into one mp4 for the clipping pipeline."""
        with self._lock:
            pieces = []
            now = time.time()
            for p in sorted(self.dir.glob("*.ts")):
                try:
                    start, end = _piece_span(p)
                except OSError:
                    continue
                if now - end < 1.5:  # still being written
                    continue
                if end >= t_from and start <= t_to:
                    pieces.append((start, p))
            if not pieces:
                return None
            LIVE_DIR.mkdir(parents=True, exist_ok=True)
            first_start = pieces[0][0]
            try:  # exact length of the first piece improves chat alignment
                d = float(subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of",
                                          "default=nokey=1:noprint_wrappers=1", str(pieces[0][1])],
                                         capture_output=True, text=True, check=True).stdout.strip())
                first_start = pieces[0][1].stat().st_mtime - d
            except Exception:
                pass
            lst = self.dir / f"_concat_{int(now)}.txt"
            lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for _, p in pieces), encoding="utf-8")
            tmp = LIVE_DIR / f".{self.login}__{int(first_start)}.part.mp4"
            out = LIVE_DIR / f"{self.login}__{int(first_start)}.mp4"
            try:
                subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                                "-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(tmp)],
                               check=True)
                tmp.replace(out)
            finally:
                lst.unlink(missing_ok=True)
                tmp.unlink(missing_ok=True)
            return out


def schedule_cut(buffer: RollingBuffer, t_event: float):
    """Chat reacts a few seconds after the action, so keep PRE seconds before the spike
    and POST after; wait until the POST part has been buffered, then cut."""
    pre, post = env_int("MOMENT_PRE_SECONDS", 45), env_int("MOMENT_POST_SECONDS", 20)

    def _do():
        try:
            path = buffer.cut(t_event - pre, t_event + post)
            LOG.info("moment saved: %s", path) if path else LOG.warning("no buffered video for moment on %s", buffer.login)
        except Exception as exc:
            LOG.warning("cutting moment for %s failed: %s", buffer.login, exc)

    t = threading.Timer(max(1.0, t_event + post + PIECE_SECONDS + 3 - time.time()), _do)
    t.daemon = True
    t.start()

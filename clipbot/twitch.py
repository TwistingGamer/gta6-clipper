"""Twitch Helix: live status, viewer-made clips, and opt-in stream capture."""
from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime, timezone

import requests

from .config import RECORDINGS
from .sources import Source

LOG = logging.getLogger("clipbot.twitch")
HELIX = "https://api.twitch.tv/helix"


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Twitch:
    def __init__(self):
        self.client_id = os.getenv("TWITCH_CLIENT_ID", "")
        self.secret = os.getenv("TWITCH_CLIENT_SECRET", "")
        self.access_token = ""
        self.expires_at = 0.0
        self._ids: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.secret)

    def token(self) -> str:
        if time.time() < self.expires_at - 60:
            return self.access_token
        if not self.configured:
            raise RuntimeError("TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are required")
        r = requests.post("https://id.twitch.tv/oauth2/token", data={
            "client_id": self.client_id, "client_secret": self.secret, "grant_type": "client_credentials",
        }, timeout=15)
        r.raise_for_status()
        payload = r.json()
        self.access_token = payload["access_token"]
        self.expires_at = time.time() + payload["expires_in"]
        return self.access_token

    def _get(self, path: str, params) -> dict:
        r = requests.get(f"{HELIX}/{path}", params=params, timeout=20,
                         headers={"Client-Id": self.client_id, "Authorization": f"Bearer {self.token()}"})
        r.raise_for_status()
        return r.json()

    def online(self, logins: list[str]) -> dict[str, dict]:
        if not logins:
            return {}
        data = self._get("streams", [("user_login", n) for n in logins[:100]]).get("data", [])
        return {s["user_login"].lower(): s for s in data}

    def user_id(self, login: str) -> str | None:
        if login not in self._ids:
            data = self._get("users", {"login": login}).get("data", [])
            if not data:
                return None
            self._ids[login] = data[0]["id"]
        return self._ids[login]

    def clips_between(self, login: str, start: float, end: float) -> list[tuple[float, int]]:
        """Clips viewers made during [start, end]: (approx. unix time of the moment, views)."""
        uid = self.user_id(login)
        if not uid:
            return []
        data = self._get("clips", {"broadcaster_id": uid, "started_at": _rfc3339(start),
                                   "ended_at": _rfc3339(end + 60), "first": 100}).get("data", [])
        out = []
        for c in data:
            made = datetime.fromisoformat(c["created_at"].replace("Z", "+00:00")).timestamp()
            out.append((made - float(c.get("duration", 30)) / 2, int(c.get("view_count", 0))))
        return out


def is_gta(stream: dict) -> bool:
    game = (stream.get("game_name") or "").lower()
    return "grand theft auto" in game or game.startswith("gta")


def capture_segment(source: Source, seconds: int):
    """Record one segment of a live stream with Streamlink.

    Only for sources whose permission explicitly allows recording. Does not
    bypass subscriber-only streams, DRM or any other access control.
    """
    ok, why = source.authorized()
    if not ok or not source.allow_recording:
        raise PermissionError(f"{source.login}: recording not permitted ({why if not ok else 'allow_recording is false'})")
    out = RECORDINGS / f"{source.login}__{int(time.time())}.mp4"
    producer = subprocess.Popen(
        ["streamlink", "--stdout", "--twitch-disable-ads", f"https://www.twitch.tv/{source.login}", "best"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", "pipe:0", "-t", str(seconds),
             "-c", "copy", "-movflags", "+faststart", str(out)],
            stdin=producer.stdout, timeout=seconds + 90,
        )
        if result.returncode != 0 or not out.exists() or out.stat().st_size < 100_000:
            out.unlink(missing_ok=True)
            raise RuntimeError(f"Unable to capture stream for {source.login}")
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

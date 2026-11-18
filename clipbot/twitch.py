"""Twitch Helix: live status and viewer-made clips (a strong highlight signal)."""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import requests

HELIX = "https://api.twitch.tv/helix"


def _rfc3339(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


NOT_GAMEPLAY = ("countdown", "watch party", "watchparty", "rerun", "re-run", "trailer", "news", "podcast",
                "reacting to", "reaction", "waiting for", "launch party", "until gta", "24/7 music")
SKIP_CHANNELS = {"twitch", "twitchgaming", "rockstargames", "twitchrivals"}


def is_gameplay(stream: dict) -> bool:
    """Skip official/brand channels and countdown/watch-party style streams."""
    if stream.get("user_login", "").lower() in SKIP_CHANNELS:
        return False
    title = (stream.get("title") or "").lower()
    return not any(w in title for w in NOT_GAMEPLAY)


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

    def game_ids(self, names: list[str]) -> dict[str, str]:
        data = self._get("games", [("name", n) for n in names]).get("data", [])
        return {g["id"]: g["name"] for g in data}

    def top_streams(self, game_names: list[str] | None = None, limit: int = 20, language: str | None = "en") -> list[dict]:
        """Biggest live streams in the given categories (default: GTA VI only), by viewers.
        Matched by Twitch game ID, so "Grand Theft Auto V" never sneaks in."""
        names = game_names or ["Grand Theft Auto VI"]
        games = self.game_ids(names)
        if not games:
            return []
        params = [("game_id", g) for g in games] + [("first", "100")]
        if language:
            params.append(("language", language))
        streams = [s for s in self._get("streams", params).get("data", [])
                   if is_gameplay(s) and s.get("game_id") in games]
        streams.sort(key=lambda s: s.get("viewer_count", 0), reverse=True)
        return streams[:limit]

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

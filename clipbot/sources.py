"""Approved-source registry.

Every streamer the bot touches must be listed in sources.yaml together with a
record of *why* we are allowed to clip them (a clipping campaign, written
permission, or our own channel). Nothing is recorded, clipped or posted for a
streamer that is missing, expired or marked inactive.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

import yaml

from .config import SOURCES_FILE

PERMISSION_TYPES = ("campaign", "written", "own", "public_clip_policy")


@dataclass
class Source:
    login: str
    platform: str = "twitch"
    display_name: str = ""
    permission_type: str = ""
    evidence: str = ""
    granted_on: str = ""
    expires_on: str = ""
    active: bool = True
    allow_recording: bool = False
    max_clip_seconds: int = 60
    credit: str = ""
    required_hashtags: list[str] = field(default_factory=list)
    series: str = "gta6"
    ad_disclosure: bool = False      # paid campaign: label posts as ads (#ad + platform toggles)
    ai_text_allowed: bool = True     # False: no AI-written titles/hooks (some campaigns ban AI content)
    clip_preapproval: bool = False   # True: streamer must OK each clip before it posts
    notes: str = ""

    @property
    def name(self) -> str:
        return self.display_name or self.login

    @property
    def credit_line(self) -> str:
        return self.credit or f"twitch.tv/{self.login}"

    def authorized(self, today: date | None = None) -> tuple[bool, str]:
        """Return (ok, reason). Reason explains a refusal."""
        if not self.active:
            return False, "marked inactive"
        if self.permission_type not in PERMISSION_TYPES:
            return False, f"permission_type must be one of {PERMISSION_TYPES}"
        if not self.evidence.strip():
            return False, "no permission evidence recorded"
        if self.expires_on:
            try:
                if date.fromisoformat(self.expires_on) < (today or date.today()):
                    return False, f"permission expired on {self.expires_on}"
            except ValueError:
                return False, f"bad expires_on date {self.expires_on!r}"
        return True, "ok"


def load_sources(path=SOURCES_FILE) -> dict[str, Source]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, Source] = {}
    for entry in raw.get("streamers", []) or []:
        perm = entry.pop("permission", {}) or {}
        src = Source(
            login=str(entry.pop("login")).lower(),
            permission_type=str(perm.get("type", "")),
            evidence=str(perm.get("evidence", "")),
            granted_on=str(perm.get("granted_on", "") or ""),
            expires_on=str(perm.get("expires_on", "") or ""),
            **{k: v for k, v in entry.items() if k in Source.__dataclass_fields__},
        )
        out[src.login] = src
    return out


def authorized_sources() -> dict[str, Source]:
    return {k: s for k, s in load_sources().items() if s.authorized()[0]}


def source_for_file(stem: str) -> Source | None:
    """Recordings are named <login>__<unix start time>.<ext>."""
    login = stem.split("__")[0].lower()
    return load_sources().get(login)

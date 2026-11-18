"""Campaign registry (campaigns.yaml).

A campaign is a paid clipping deal (Reach Clipping, Content Rewards, ...) or a
written permission. It says WHERE footage may come from (inputs), WHERE clips
may be posted (platforms), and the rules a clip must follow (hashtags, ad
label, length, bio link, payout caps). Nothing is downloaded, recorded,
clipped or posted for a campaign that is inactive, expired or has no
permission evidence.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from .config import RECORDINGS, SOURCES_FILE

PERMISSION_TYPES = ("campaign", "written", "own")
LIVE_TYPES = ("twitch", "kick", "youtube_live")
INPUT_TYPES = LIVE_TYPES + ("youtube_video", "youtube_channel", "folder")
POST_PLATFORMS = ("youtube", "tiktok", "instagram", "x")


def hms(text: str) -> float | None:
    """'2:46:29' / '46:29' / '29' -> seconds."""
    if not text:
        return None
    total = 0.0
    for part in str(text).split(":"):
        total = total * 60 + float(part)
    return total


@dataclass
class Input:
    type: str
    target: str            # login, URL, or folder path
    since: str = ""        # youtube_channel: only videos uploaded on/after this date (YYYY-MM-DD)
    max_videos: int = 5    # youtube_channel: newest N videos per check
    start_at: str = ""     # youtube_video: approved footage starts here ("2:46:29")
    end_at: str = ""       # youtube_video: ...and ends here (blank = end of video)

    @property
    def window(self) -> tuple[float, float | None]:
        return hms(self.start_at) or 0.0, hms(self.end_at)

    @property
    def login(self) -> str:
        """Channel login for twitch/kick inputs (accepts a bare name or a URL)."""
        t = self.target.rstrip("/")
        if t.endswith("/live"):
            t = t[:-5]
        return t.rsplit("/", 1)[-1].lstrip("@").lower()

    @property
    def url(self) -> str:
        if self.type == "twitch" and "://" not in self.target:
            return f"https://www.twitch.tv/{self.login}"
        if self.type == "kick" and "://" not in self.target:
            return f"https://kick.com/{self.login}"
        if self.type == "youtube_live":
            base = self.target if "://" in self.target else f"https://www.youtube.com/@{self.target.lstrip('@')}"
            return base.rstrip("/") + ("" if base.rstrip("/").endswith("/live") else "/live")
        return self.target


@dataclass
class Campaign:
    id: str
    name: str = ""
    active: bool = True
    permission_type: str = ""
    evidence: str = ""             # rules URL, campaign page, or note you could show if challenged
    joined_on: str = ""
    expires_on: str = ""
    inputs: list[Input] = field(default_factory=list)
    platforms: list[str] = field(default_factory=lambda: ["tiktok", "instagram", "youtube"])
    cpm: float = 0.0               # $ per 1,000 views (used to prioritise posting slots)
    min_payout: float = 0.0
    max_payout: float = 0.0        # per platform submission
    game_filter: str = ""          # e.g. "grand theft auto": only record live streams in this category
    allow_recording: bool = False  # may record live streams (twitch/kick inputs)
    min_clip_seconds: int = 15
    max_clip_seconds: int = 60
    credit: str = ""
    required_hashtags: list[str] = field(default_factory=list)
    required_mentions: list[str] = field(default_factory=list)
    watermark: str = ""            # on-screen text some campaigns require for the whole clip
    bio_link: str = ""             # must be in your profile bio (manual; shown in the morning report)
    ad_disclosure: bool = False    # "#ad" on screen + caption + platform paid-promotion toggles
    ai_text_allowed: bool = True   # False: no AI-written titles/hooks (some campaigns ban AI content)
    clip_preapproval: bool = False # streamer/campaign must OK each clip before posting
    content_rule: str = ""         # extra rule the AI judge enforces, e.g. "at least 2 Core members on screen"
    required_words: list[str] = field(default_factory=list)  # caption must contain each, e.g. ["Expedia"]
    title_template: str = ""       # used when ai_text_allowed is false, e.g. "IShowSpeed's Expedia trip"
    require_review: bool = True    # a human approves every clip, even if AUTO_APPROVE=true
    post_hours: str = ""           # local hours posting is allowed, e.g. "9-23" (you must be awake to submit)
    submit_within_minutes: int = 0 # campaign deadline to submit the link after posting (sends a ping)
    language: str = "en"
    sponsor: str = ""              # brand named in the ad disclosure, e.g. "Expedia"
    budget_total: float = 0.0      # campaign budget from the dashboard
    budget_used: float = 0.0       # update when you check; 100% used = campaign ignored
    budget_checked: str = ""
    series: str = ""               # free label used in titles/prompt, e.g. "gta6"
    notes: str = ""

    @property
    def display(self) -> str:
        return self.name or self.id

    @property
    def credit_line(self) -> str:
        if self.credit:
            return self.credit
        live = [i for i in self.inputs if i.type in LIVE_TYPES]
        return live[0].url.replace("https://www.", "").replace("https://", "") if live else ""

    @property
    def folder(self) -> Path:
        """Where this campaign's footage lands (downloads, recordings)."""
        p = RECORDINGS / self.id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def input_for_login(self, login: str) -> Input | None:
        return next((i for i in self.inputs if i.type in LIVE_TYPES and i.login == login), None)

    def ease(self) -> float:
        """Fixed footage is easiest (no recording, no waiting for streams)."""
        kinds = {i.type for i in self.inputs}
        e = 0.4 if kinds & {"twitch", "kick"} else 0.7 if "youtube_channel" in kinds else 1.0
        if self.clip_preapproval:
            e *= 0.7
        return e

    def budget_left(self) -> float | None:
        return max(0.0, self.budget_total - self.budget_used) if self.budget_total else None

    def priority(self) -> float:
        """Rough 'easiest + biggest payout' ranking: ease x CPM x payout ceiling x budget left."""
        left = self.budget_left()
        if left == 0:
            return 0.0
        cap = self.max_payout or 100
        budget = (left / self.budget_total) if self.budget_total else 0.5
        cpm = self.cpm or 0.25  # pool campaigns without a CPM: unknown, rank them low
        return round(self.ease() * cpm * math.log10(cap + 10) * (0.5 + 0.5 * budget), 3)

    def can_post_now(self, hour: int) -> bool:
        if not self.post_hours:
            return True
        a, b = (int(x) for x in self.post_hours.split("-"))
        return a <= hour <= b if a <= b else (hour >= a or hour <= b)

    def authorized(self, today: date | None = None) -> tuple[bool, str]:
        if not self.active:
            return False, "marked inactive"
        if self.permission_type not in PERMISSION_TYPES:
            return False, f"permission.type must be one of {PERMISSION_TYPES}"
        if not self.evidence.strip():
            return False, "no permission evidence recorded"
        if self.expires_on:
            try:
                if date.fromisoformat(self.expires_on) < (today or date.today()):
                    return False, f"expired on {self.expires_on}"
            except ValueError:
                return False, f"bad expires_on date {self.expires_on!r}"
        return True, "ok"

    def problems(self) -> list[str]:
        out = []
        for i in self.inputs:
            if i.type not in INPUT_TYPES:
                out.append(f"unknown input type {i.type!r}")
        for p in self.platforms:
            if p not in POST_PLATFORMS:
                out.append(f"unknown platform {p!r}")
        if not re.fullmatch(r"[a-z0-9_-]+", self.id):
            out.append("id must be lowercase letters, digits, - or _")
        return out


def _campaign(entry: dict) -> Campaign:
    entry = dict(entry)
    perm = entry.pop("permission", {}) or {}
    payout = entry.pop("payout", {}) or {}
    inputs = [Input(**{k: (str(v) if k in ("type", "target", "since", "start_at", "end_at") else v) for k, v in i.items()})
              for i in (entry.pop("inputs", []) or [])]
    known = {k: v for k, v in entry.items() if k in Campaign.__dataclass_fields__}
    known["id"] = str(known["id"]).lower()
    return Campaign(
        permission_type=str(perm.get("type", "")),
        evidence=str(perm.get("evidence", "") or ""),
        joined_on=str(perm.get("joined_on", "") or ""),
        expires_on=str(perm.get("expires_on", "") or ""),
        inputs=inputs,
        cpm=float(payout.get("cpm", 0) or 0),
        min_payout=float(payout.get("min", 0) or 0),
        max_payout=float(payout.get("max", 0) or 0),
        budget_total=float((entry.get("budget") or {}).get("total", 0) or 0),
        budget_used=float((entry.get("budget") or {}).get("used", 0) or 0),
        budget_checked=str((entry.get("budget") or {}).get("checked", "") or ""),
        **known,
    )


def load_campaigns(path: Path | None = None) -> dict[str, Campaign]:
    path = path or SOURCES_FILE
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {c.id: c for c in (_campaign(e) for e in raw.get("campaigns", []) or [])}


def authorized_campaigns() -> dict[str, Campaign]:
    return {k: c for k, c in load_campaigns().items() if c.authorized()[0] and not c.problems()}

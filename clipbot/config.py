"""Paths and environment settings shared by every module."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DATA = ROOT / "data"
CHAT = DATA / "chat"
RECORDINGS = ROOT / "recordings"
OUTPUT = ROOT / "output"
QUEUE = ROOT / "queue"
for _p in (DATA, CHAT, RECORDINGS, OUTPUT, QUEUE):
    _p.mkdir(exist_ok=True)

DB = DATA / "clipbot.db"
SOURCES_FILE = Path(os.getenv("CAMPAIGNS_FILE", ROOT / "campaigns.yaml"))
VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".webm", ".ts")


def env_bool(key: str, default: bool = False) -> bool:
    return os.getenv(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


def env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


def env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, str(default)))
    except ValueError:
        return default


def env_list(key: str) -> list[str]:
    return [x.strip() for x in os.getenv(key, "").split(",") if x.strip()]

"""Paths and environment settings shared by every module."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

# Everything the bot writes lives under STORAGE_DIR (default: the bot's own folder),
# so on a D:-only setup nothing lands on C:.
STORAGE = Path(os.getenv("STORAGE_DIR", ROOT))
DATA = STORAGE / "data"
CHAT = DATA / "chat"
MODELS = DATA / "models"
TMP = DATA / "tmp"
RECORDINGS = STORAGE / "recordings"
OUTPUT = STORAGE / "output"
QUEUE = STORAGE / "queue"
for _p in (DATA, CHAT, MODELS, TMP, RECORDINGS, OUTPUT, QUEUE):
    _p.mkdir(parents=True, exist_ok=True)

# Keep temp files and downloaded AI models (Whisper via Hugging Face) off the system drive.
# Child processes (ffmpeg, streamlink, yt-dlp) inherit these too.
if os.getenv("KEEP_SYSTEM_TEMP", "").lower() not in ("1", "true", "yes"):
    for _k in ("TEMP", "TMP", "TMPDIR"):
        os.environ[_k] = str(TMP)
os.environ.setdefault("HF_HOME", str(MODELS / "huggingface"))
os.environ.setdefault("XDG_CACHE_HOME", str(DATA / "cache"))


def _tool(env_key: str, name: str) -> str:
    """Full path from .env (e.g. FFMPEG_PATH=D:\\tools\\ffmpeg\\bin\\ffmpeg.exe) or rely on PATH."""
    return os.getenv(env_key, "").strip().strip('"') or name


def _find_ffmpeg() -> str:
    """PATH first, then common unzip spots: <drive>:\\tools, next to the bot, inside the bot folder."""
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    roots = [Path(ROOT.anchor) / "tools", ROOT.parent / "tools", ROOT / "tools", ROOT.parent]
    for r in roots:
        if not r.is_dir():
            continue
        for pattern in (exe, f"*/{exe}", f"*/bin/{exe}", f"*/*/bin/{exe}"):
            hits = sorted(r.glob(pattern))
            if hits:
                return str(hits[-1])
    return "ffmpeg"


FFMPEG = _tool("FFMPEG_PATH", "") or _find_ffmpeg()
if Path(FFMPEG).is_dir():  # a folder like D:\\tools\\ffmpeg\\bin was given
    FFMPEG = str(Path(FFMPEG) / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg"))
_ff = Path(FFMPEG)
FFPROBE = _tool("FFPROBE_PATH", str(_ff.with_name(_ff.name.replace("ffmpeg", "ffprobe"))) if _ff.parent != Path(".") else "ffprobe")
# Streamlink is installed into the bot's own Python environment by requirements.txt.
_venv_sl = Path(sys.executable).parent / ("streamlink.exe" if os.name == "nt" else "streamlink")
STREAMLINK = _tool("STREAMLINK_PATH", str(_venv_sl) if _venv_sl.exists() else "streamlink")


def missing_tools(need_streamlink: bool = False) -> list[str]:
    """Human-readable problems with FFmpeg/ffprobe/Streamlink, empty if all good."""
    problems = []
    checks = [("FFmpeg", FFMPEG, "FFMPEG_PATH"), ("ffprobe", FFPROBE, "FFMPEG_PATH")]
    if need_streamlink:
        checks.append(("Streamlink", STREAMLINK, "STREAMLINK_PATH"))
    for name, cmd, key in checks:
        found = Path(cmd).is_file() if Path(cmd).parent != Path(".") else shutil.which(cmd)
        if not found:
            hint = ("unzip the FFmpeg 'essentials' build into D:\\tools, or set FFMPEG_PATH in .env to its bin folder"
                    if name != "Streamlink" else "run: pip install -r requirements.txt  (inside the .venv)")
            problems.append(f"{name} not found (looked for: {cmd}). Fix: {hint}")
    return problems

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

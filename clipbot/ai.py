"""AI judge: decides whether a candidate is worth posting and writes the packaging.

Uses Claude (Anthropic API) with a few still frames plus the transcript. If
ANTHROPIC_API_KEY is not set, falls back to a plain heuristic title so the
pipeline still runs.
"""
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

LOG = logging.getLogger("clipbot.ai")

SCHEMA = {
    "type": "object",
    "properties": {
        "keep": {"type": "boolean"},
        "score": {"type": "integer", "description": "0-10, how likely this is to perform as a Short"},
        "why": {"type": "string"},
        "title": {"type": "string", "description": "Under 70 characters, no clickbait lies"},
        "hook": {"type": "string", "description": "On-screen text for the first seconds, max 7 words"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["keep", "score", "why", "title", "hook", "description", "hashtags"],
    "additionalProperties": False,
}

SYSTEM = """You pick and package short-form clips from Grand Theft Auto livestreams for YouTube Shorts, TikTok and Instagram Reels.
You get still frames from a ~30-60 second window, its transcript, and the signals that flagged it.
Keep a moment only if a viewer with no context would find it funny, shocking, impressive or dramatic within 3 seconds.
Reject dead air, menus, loading screens, AFK, ad breaks, 'be right back' screens, and anything showing personal info.
Never invent events that the frames or transcript don't support. Titles must be accurate.
Do not describe or speculate about unreleased/leaked game content.
Write in the style of the series: gta6 = launch-era GTA 6 moments; gta5_last_ride = nostalgic 'last days of GTA 5' moments; hype = GTA 6 launch excitement."""


def _fallback(streamer: str, series: str) -> dict:
    label = {"gta5_last_ride": "GTA 5", "hype": "GTA 6"}.get(series, "GTA 6")
    return {
        "keep": True, "score": 5, "why": "no AI key configured; heuristic only",
        "title": f"{streamer} in {label} 😂",
        "hook": "WAIT FOR IT...",
        "description": f"{streamer} {label} moment.",
        "hashtags": ["#GTA6" if label == "GTA 6" else "#GTA5", "#GTA", "#Shorts"],
    }


def judge(frames: list[Path], transcript: str, streamer: str, series: str, signals: dict) -> dict:
    if not os.getenv("ANTHROPIC_API_KEY") and not os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return _fallback(streamer, series)

    import anthropic

    client = anthropic.Anthropic()
    content: list[dict] = []
    for f in frames:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg",
                       "data": base64.standard_b64encode(f.read_bytes()).decode()},
        })
    content.append({
        "type": "text",
        "text": (f"Streamer: {streamer}\nSeries: {series}\nSignals (0-1): {json.dumps(signals)}\n"
                 f"Transcript:\n{transcript or '(no speech detected)'}"),
    })
    try:
        resp = client.beta.messages.create(
            model=os.getenv("CLAUDE_MODEL", "claude-opus-5-5"),
            max_tokens=4000,
            system=SYSTEM,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": os.getenv("CLAUDE_EFFORT", "low"),
                           "format": {"type": "json_schema", "schema": SCHEMA}},
            # If a safety classifier declines, the API retries on a fallback model in the same call.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.APIStatusError as exc:
        LOG.error("Claude API error %s: %s", exc.status_code, exc.message)
        return _fallback(streamer, series) | {"keep": False, "why": f"AI error {exc.status_code}"}
    except anthropic.APIConnectionError as exc:
        LOG.error("Claude API unreachable: %s", exc)
        return _fallback(streamer, series) | {"keep": False, "why": "AI unreachable"}

    if resp.stop_reason == "refusal":
        return _fallback(streamer, series) | {"keep": False, "why": "AI declined this clip"}
    text = "".join(b.text for b in resp.content if b.type == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        LOG.error("AI returned non-JSON output (stop_reason=%s)", resp.stop_reason)
        return _fallback(streamer, series) | {"keep": False, "why": "unparseable AI output"}

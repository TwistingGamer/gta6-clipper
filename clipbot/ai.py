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
        "hook": {"type": "string", "description": "On-screen text for the first seconds that tells a viewer who knows nothing what is going on / what to watch for (the setup), max 9 words, no spoilers of the payoff"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "people": {"type": "array", "items": {"type": "string"},
                   "description": "Names from the campaign's member list who are clearly on screen or speaking"},
    },
    "required": ["keep", "score", "why", "title", "hook", "description", "hashtags", "people"],
    "additionalProperties": False,
}

SYSTEM = """You pick and package short-form clips from streams and videos for YouTube Shorts, TikTok and Instagram Reels.
The clipper is part of an official, paid clipping campaign; the campaign's rules are given with each request and must be followed.
You get still frames from a ~15-60 second window, its transcript, and the signals that flagged it.
Keep a moment only if a viewer with no context would find it funny, shocking, impressive or dramatic within 3 seconds.
Reject dead air, menus, loading screens, AFK, ad breaks, 'be right back' screens, and anything showing personal info.
Never invent events that the frames or transcript don't support. Titles must be accurate.
If the campaign has a content rule and the clip doesn't clearly meet it, set keep=false.
Never portray the creator or sponsor negatively and never create a false narrative.
Do not describe or speculate about unreleased/leaked game content."""


def template_text(campaign) -> dict:
    """Plain, human-style packaging used without an AI key or when a campaign bans AI text."""
    title = campaign.title_template or f"{campaign.display} moment"
    return {
        "keep": True, "score": 5, "why": "heuristic only (no AI judge)",
        "title": title, "hook": "",
        "description": title + ".",
        "hashtags": [], "people": [],
    }


def judge(frames: list[Path], transcript: str, campaign, signals: dict) -> dict:
    if not os.getenv("ANTHROPIC_API_KEY") and not os.getenv("ANTHROPIC_AUTH_TOKEN"):
        return template_text(campaign)

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
        "text": (f"Campaign: {campaign.display}\n"
                 f"Content rule: {campaign.content_rule or '(none)'}\n"
                 f"Must mention: {', '.join(campaign.required_words) or '(nothing)'}\n"
                 f"Campaign members (report who is present in 'people'): "
                 f"{', '.join(m['name'] for m in campaign.members) or '(none)'}\n"
                 f"Minimum members on screen: {campaign.min_members or '(no minimum)'}\n"
                 f"Language: {campaign.language}\n"
                 f"Series/theme: {campaign.series or '(none)'}\n"
                 f"Signals (0-1): {json.dumps(signals)}\n"
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
        return template_text(campaign) | {"keep": False, "why": f"AI error {exc.status_code}"}
    except anthropic.APIConnectionError as exc:
        LOG.error("Claude API unreachable: %s", exc)
        return template_text(campaign) | {"keep": False, "why": "AI unreachable"}

    if resp.stop_reason == "refusal":
        return template_text(campaign) | {"keep": False, "why": "AI declined this clip"}
    text = "".join(b.text for b in resp.content if b.type == "text")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        LOG.error("AI returned non-JSON output (stop_reason=%s)", resp.stop_reason)
        return template_text(campaign) | {"keep": False, "why": "unparseable AI output"}

"""TikTok upload via the official Content Posting API (Direct Post, FILE_UPLOAD).

Until TikTok audits your app, every post is forced to SELF_ONLY (private).
See README > TikTok setup.
"""
from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from ..config import DATA

API = "https://open.tiktokapis.com/v2"
TOKEN = DATA / "tiktok_token.json"
MIN_CHUNK, MAX_SINGLE = 5 * 1024 * 1024, 64 * 1024 * 1024
CHUNK = 10 * 1024 * 1024


def _client() -> tuple[str, str, str]:
    key, secret = os.getenv("TIKTOK_CLIENT_KEY", ""), os.getenv("TIKTOK_CLIENT_SECRET", "")
    redirect = os.getenv("TIKTOK_REDIRECT_URI", "")
    if not (key and secret and redirect):
        raise RuntimeError("Set TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET and TIKTOK_REDIRECT_URI in .env")
    return key, secret, redirect


def _save(tok: dict):
    tok["obtained_at"] = time.time()
    TOKEN.write_text(json.dumps(tok, indent=2), encoding="utf-8")


def authorize_interactive():
    """One-time sign-in: open the printed URL, approve, paste back the URL you land on."""
    key, secret, redirect = _client()
    state = secrets.token_urlsafe(16)
    url = "https://www.tiktok.com/v2/auth/authorize/?" + urlencode({
        "client_key": key, "scope": "user.info.basic,video.publish", "response_type": "code",
        "redirect_uri": redirect, "state": state,
    })
    print("Open this URL, approve access, then paste the full URL you were redirected to:\n\n" + url + "\n")
    landed = input("Redirected URL: ").strip()
    q = parse_qs(urlparse(landed).query)
    if q.get("state", [""])[0] != state:
        raise RuntimeError("State mismatch; start again")
    r = requests.post(f"{API}/oauth/token/", timeout=20, headers={"Content-Type": "application/x-www-form-urlencoded"}, data={
        "client_key": key, "client_secret": secret, "code": q["code"][0],
        "grant_type": "authorization_code", "redirect_uri": redirect,
    })
    r.raise_for_status()
    _save(r.json())
    print("TikTok connected.")


def access_token() -> str:
    if not TOKEN.exists():
        raise RuntimeError("TikTok not signed in. Run: python -m clipbot auth tiktok")
    tok = json.loads(TOKEN.read_text(encoding="utf-8"))
    if time.time() > tok["obtained_at"] + tok.get("expires_in", 0) - 300:
        key, secret, _ = _client()
        r = requests.post(f"{API}/oauth/token/", timeout=20, headers={"Content-Type": "application/x-www-form-urlencoded"}, data={
            "client_key": key, "client_secret": secret, "grant_type": "refresh_token",
            "refresh_token": tok["refresh_token"],
        })
        r.raise_for_status()
        tok = r.json()
        if "access_token" not in tok:
            raise RuntimeError(f"TikTok refresh failed (sign in again): {tok}")
        _save(tok)
    return tok["access_token"]


def _api(path: str, token: str, body: dict) -> dict:
    r = requests.post(f"{API}/{path}", json=body, timeout=30,
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8"})
    data = r.json()
    err = data.get("error", {})
    if r.status_code >= 400 or (err.get("code") not in (None, "ok")):
        raise RuntimeError(f"TikTok {path} failed: {err or r.text}")
    return data.get("data", {})


def post(item: dict) -> tuple[str, str]:
    token = access_token()
    creator = _api("post/publish/creator_info/query/", token, {})
    options = creator.get("privacy_level_options", ["SELF_ONLY"])
    wanted = os.getenv("TIKTOK_PRIVACY", "SELF_ONLY")
    privacy = wanted if wanted in options else "SELF_ONLY"

    video = Path(item["output"])
    size = video.stat().st_size
    if size <= MAX_SINGLE:
        chunk, count = size, 1
    else:
        chunk, count = CHUNK, size // CHUNK  # last chunk absorbs the remainder, per TikTok's rules

    caption = f"{item['title']}\n\n{item['description']}"[:2200]
    init = _api("post/publish/video/init/", token, {
        "post_info": {"title": caption, "privacy_level": privacy, "disable_duet": False,
                      "disable_comment": False, "disable_stitch": False,
                      # Paid campaign: TikTok's "Paid partnership" label. TikTok does not allow
                      # branded content to be private, so it is only set once posts can be public.
                      **({"brand_content_toggle": True} if item.get("ad_disclosure") and privacy != "SELF_ONLY" else {})},
        "source_info": {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count},
    })
    with video.open("rb") as fh:
        for i in range(count):
            first = i * chunk
            last = size - 1 if i == count - 1 else first + chunk - 1
            fh.seek(first)
            body = fh.read(last - first + 1)
            r = requests.put(init["upload_url"], data=body, timeout=300, headers={
                "Content-Type": "video/mp4", "Content-Length": str(len(body)),
                "Content-Range": f"bytes {first}-{last}/{size}",
            })
            if r.status_code not in (200, 201, 206):
                raise RuntimeError(f"TikTok chunk {i + 1}/{count} upload failed: {r.status_code} {r.text[:200]}")

    publish_id = init["publish_id"]
    for _ in range(60):
        status = _api("post/publish/status/fetch/", token, {"publish_id": publish_id})
        state = status.get("status")
        if state == "PUBLISH_COMPLETE":
            ids = status.get("publicaly_available_post_id") or status.get("publicly_available_post_id") or []
            post_id = str(ids[0]) if ids else publish_id
            user = os.getenv("TIKTOK_USERNAME", "").lstrip("@")
            return post_id, (f"https://www.tiktok.com/@{user}/video/{post_id}" if ids and user else "")
        if state == "FAILED":
            raise RuntimeError(f"TikTok publish failed: {status.get('fail_reason')}")
        time.sleep(10)
    return publish_id, ""  # still processing; TikTok will finish on its side

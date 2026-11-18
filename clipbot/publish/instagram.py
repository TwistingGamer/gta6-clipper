"""Instagram Reels via the official Instagram Graph API content-publishing flow.

Needs an Instagram Professional (Business/Creator) account, an app with
instagram_content_publish permission, IG_USER_ID and a long-lived IG_ACCESS_TOKEN
(valid ~60 days; refresh it before it expires). Uses resumable upload so the
video doesn't need to be hosted on a public URL.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import requests


def _cfg() -> tuple[str, str, str]:
    user, token = os.getenv("IG_USER_ID", ""), os.getenv("IG_ACCESS_TOKEN", "")
    if not (user and token):
        raise RuntimeError("Set IG_USER_ID and IG_ACCESS_TOKEN in .env (see README > Instagram setup)")
    host = os.getenv("IG_GRAPH_HOST", "graph.facebook.com")
    ver = os.getenv("IG_GRAPH_VERSION", "v21.0")
    return f"https://{host}/{ver}", user, token


def _check(r: requests.Response) -> dict:
    data = r.json() if r.content else {}
    if r.status_code >= 400 or "error" in data:
        raise RuntimeError(f"Instagram API error: {data.get('error', r.text[:300])}")
    return data


def post(item: dict) -> str:
    base, user, token = _cfg()
    video = Path(item["output"])
    caption = f"{item['title']}\n\n{item['description']}"[:2200]

    container = _check(requests.post(f"{base}/{user}/media", timeout=30, data={
        "media_type": "REELS", "upload_type": "resumable", "caption": caption,
        "share_to_feed": "true", "access_token": token,
    }))
    cid = container["id"]
    upload_uri = container.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{base.rsplit('/', 1)[1]}/{cid}"
    size = video.stat().st_size
    with video.open("rb") as fh:
        _check(requests.post(upload_uri, data=fh, timeout=600, headers={
            "Authorization": f"OAuth {token}", "offset": "0", "file_size": str(size),
        }))

    for _ in range(60):
        status = _check(requests.get(f"{base}/{cid}", timeout=30,
                                     params={"fields": "status_code,status", "access_token": token}))
        code = status.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Instagram processing failed: {status.get('status')}")
        time.sleep(10)
    else:
        raise RuntimeError("Instagram still processing after 10 minutes; will retry")

    published = _check(requests.post(f"{base}/{user}/media_publish", timeout=60,
                                     data={"creation_id": cid, "access_token": token}))
    return published["id"]

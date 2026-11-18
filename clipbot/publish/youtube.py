"""YouTube Shorts upload via the official YouTube Data API v3 (OAuth, youtube.upload scope)."""
from __future__ import annotations

import os
from pathlib import Path

from ..config import DATA, ROOT

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
TOKEN = DATA / "youtube_token.json"


def credentials(interactive: bool = False):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets = ROOT / "client_secrets.json"
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        if not interactive:
            raise RuntimeError("YouTube not signed in (or sign-in expired). Run: python -m clipbot auth youtube")
        if not secrets.exists():
            raise RuntimeError("Missing client_secrets.json; see README (YouTube setup)")
        creds = InstalledAppFlow.from_client_secrets_file(str(secrets), SCOPES).run_local_server(port=0)
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return creds


def post(item: dict) -> tuple[str, str]:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    privacy = os.getenv("YOUTUBE_PRIVACY", "private")
    if privacy not in ("private", "unlisted", "public"):
        raise ValueError("YOUTUBE_PRIVACY must be private/unlisted/public")
    title = item["title"]
    if "#shorts" not in title.lower() and len(title) <= 90:
        title = f"{title} #Shorts"
    service = build("youtube", "v3", credentials=credentials())
    ad = bool(item.get("ad_disclosure"))
    request = service.videos().insert(
        part="snippet,status,paidProductPlacementDetails" if ad else "snippet,status",
        body={
            "snippet": {"title": title[:100], "description": item["description"][:4900],
                        "tags": [t.lstrip("#") for t in item.get("hashtags", [])][:15], "categoryId": "20"},
            "status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": False},
            # Turns on YouTube's "Includes paid promotion" label for paid clipping campaigns.
            **({"paidProductPlacementDetails": {"hasPaidProductPlacement": True}} if ad else {}),
        },
        media_body=MediaFileUpload(str(Path(item["output"])), mimetype="video/mp4", resumable=True, chunksize=8 * 1024 * 1024),
    )
    response = None
    while response is None:
        _, response = request.next_chunk()
    return response["id"], f"https://youtube.com/shorts/{response['id']}"

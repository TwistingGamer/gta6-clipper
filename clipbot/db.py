"""SQLite storage for inputs, clips and per-platform posts."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import DB, QUEUE

CLIP_COLUMNS = {
    "id": "TEXT PRIMARY KEY",
    "source": "TEXT",
    "output": "TEXT",
    "score": "REAL",
    "status": "TEXT",          # pending_review | awaiting_streamer | approved | rejected
    "title": "TEXT",
    "created_at": "TEXT",
    "uploaded_at": "TEXT",     # legacy column from the MVP (YouTube only)
    "youtube_id": "TEXT",      # legacy column from the MVP
    "streamer": "TEXT",
    "campaign": "TEXT",
    "series": "TEXT",
    "description": "TEXT",
    "hashtags": "TEXT",        # JSON list
    "hook": "TEXT",
    "start_s": "REAL",
    "duration_s": "REAL",
    "reasons": "TEXT",         # JSON: why this moment was picked
    "ad_disclosure": "INTEGER",  # 1 = paid campaign clip, must be labelled as an ad
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init_db():
    with connect() as con:
        con.execute("CREATE TABLE IF NOT EXISTS inputs (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, status TEXT, error TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS clips (id TEXT PRIMARY KEY)")
        have = {r["name"] for r in con.execute("PRAGMA table_info(clips)")}
        for col, decl in CLIP_COLUMNS.items():
            if col not in have:
                con.execute(f"ALTER TABLE clips ADD COLUMN {col} {decl.replace('PRIMARY KEY', '')}")
        # MVP clips used status 'ready' and were auto-uploadable; keep them reviewable instead.
        con.execute("UPDATE clips SET status='pending_review' WHERE status='ready'")
        con.execute(
            "CREATE TABLE IF NOT EXISTS posts (clip_id TEXT, platform TEXT, remote_id TEXT, status TEXT, "
            "error TEXT, posted_at TEXT, url TEXT, PRIMARY KEY (clip_id, platform))"
        )
        if "url" not in {r["name"] for r in con.execute("PRAGMA table_info(posts)")}:
            con.execute("ALTER TABLE posts ADD COLUMN url TEXT")
        con.execute("CREATE TABLE IF NOT EXISTS fetched (campaign TEXT, key TEXT, path TEXT, fetched_at TEXT, "
                    "PRIMARY KEY (campaign, key))")


def insert_clip(item: dict):
    cols = [c for c in CLIP_COLUMNS if c in item]
    with connect() as con:
        con.execute(
            f"INSERT OR REPLACE INTO clips ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [json.dumps(item[c]) if isinstance(item[c], (list, dict)) else item[c] for c in cols],
        )
    write_queue_file(item["id"])


def get_clip(clip_id: str) -> dict | None:
    with connect() as con:
        row = con.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
    if not row:
        return None
    item = dict(row)
    for key in ("hashtags", "reasons"):
        try:
            item[key] = json.loads(item[key]) if item.get(key) else []
        except (TypeError, json.JSONDecodeError):
            pass
    return item


def clip_exists_for(source: str, start_s: float) -> bool:
    with connect() as con:
        return con.execute("SELECT 1 FROM clips WHERE source=? AND start_s=?", (source, start_s)).fetchone() is not None


def set_status(clip_id: str, status: str):
    with connect() as con:
        con.execute("UPDATE clips SET status=? WHERE id=?", (status, clip_id))
    write_queue_file(clip_id)


def list_clips(status: str | None = None, limit: int = 50) -> list[dict]:
    q = "SELECT id, campaign, score, status, title, output FROM clips"
    args: tuple = ()
    if status:
        q += " WHERE status=?"
        args = (status,)
    q += " ORDER BY created_at DESC LIMIT ?"
    with connect() as con:
        rows = [dict(r) for r in con.execute(q, args + (limit,))]
        for r in rows:
            r["posts"] = {p["platform"]: p["status"] for p in con.execute(
                "SELECT platform, status FROM posts WHERE clip_id=?", (r["id"],))}
    return rows


def record_post(clip_id: str, platform: str, status: str, remote_id: str | None = None,
                error: str | None = None, url: str | None = None):
    with connect() as con:
        con.execute(
            "INSERT OR REPLACE INTO posts (clip_id, platform, remote_id, status, error, posted_at, url) "
            "VALUES (?,?,?,?,?,?,?)",
            (clip_id, platform, remote_id, status, error, now_iso(), url),
        )
    write_queue_file(clip_id)


def posted_platforms(clip_id: str) -> set[str]:
    with connect() as con:
        return {r["platform"] for r in con.execute(
            "SELECT platform FROM posts WHERE clip_id=? AND status='posted'", (clip_id,))}


def clips_for_source(source: str) -> list[dict]:
    with connect() as con:
        return [dict(r) for r in con.execute("SELECT id, status FROM clips WHERE source=?", (source,))]


def unposted_for_platform(platform: str) -> list[dict]:
    """Approved clips not yet posted on this platform, best first."""
    with connect() as con:
        rows = con.execute(
            "SELECT id FROM clips WHERE status='approved' AND id NOT IN "
            "(SELECT clip_id FROM posts WHERE platform=? AND status IN ('posted','processing','staged')) "
            "ORDER BY score DESC, created_at ASC LIMIT 200",
            (platform,),
        ).fetchall()
    return [get_clip(r["id"]) for r in rows]


def posts_between(since_iso: str) -> list[dict]:
    with connect() as con:
        return [dict(r) for r in con.execute(
            "SELECT p.*, c.campaign, c.title, c.output, c.reasons FROM posts p JOIN clips c ON c.id=p.clip_id "
            "WHERE p.status='posted' AND p.posted_at>=? ORDER BY p.posted_at", (since_iso,))]


def posts_since(platform: str, since_iso: str) -> tuple[int, str | None]:
    with connect() as con:
        count = con.execute(
            "SELECT COUNT(*) FROM posts WHERE platform=? AND status IN ('posted','staged') AND posted_at>=?",
            (platform, since_iso),
        ).fetchone()[0]
        latest = con.execute(
            "SELECT MAX(posted_at) FROM posts WHERE platform=? AND status IN ('posted','staged')", (platform,)
        ).fetchone()[0]
    return count, latest


def write_queue_file(clip_id: str):
    """Human-readable JSON mirror of a clip, handy for reviewing in a file browser."""
    item = get_clip(clip_id)
    if not item:
        return
    with connect() as con:
        item["posts"] = [dict(r) for r in con.execute("SELECT * FROM posts WHERE clip_id=?", (clip_id,))]
    (QUEUE / f"{clip_id}.json").write_text(json.dumps(item, indent=2), encoding="utf-8")

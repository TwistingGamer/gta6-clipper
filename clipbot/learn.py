"""Learns from your ✅ / ❌ decisions, in plain numbers you can read (`python -m clipbot stats`).

Every approve or reject is logged with what the clip looked like: streamer, kind (moment, first-look,
viewer-clip), the chat / audio / viewer-clip signals, how loud it got, and its score. From that:

  * signal weights: a signal that is clearly higher on clips you approve than on clips you reject
    gets more say when picking moments (and less if it's the other way round), within +-50%;
  * per-streamer bar: a streamer whose clips you mostly reject needs a stronger moment next time;
    one you mostly approve gets a slightly lower bar.

Nothing changes until there are enough decisions (LEARN_MIN_DECISIONS, default 10; 5 per streamer).
`python -m clipbot stats --reset` forgets everything.
"""
from __future__ import annotations

import json
import time

from . import db
from .config import env_int

BASE_WEIGHTS = {"audio": 0.3, "chat": 0.4, "clips": 0.3}
FEATURES = ("audio", "chat", "clips", "peak", "score")


def _init():
    with db.connect() as con:
        con.execute("CREATE TABLE IF NOT EXISTS decisions (clip_id TEXT PRIMARY KEY, at REAL, streamer TEXT, "
                    "campaign TEXT, kind TEXT, approved INTEGER, features TEXT)")


def features_of(item: dict) -> dict:
    reasons = item.get("reasons") if isinstance(item.get("reasons"), dict) else {}
    sig = reasons.get("signals") or {}
    kind = "first-look" if reasons.get("first_look") else ("viewer-clip" if (sig.get("clips") or 0) > 0.5 else "moment")
    return {"kind": kind, "audio": float(sig.get("audio") or 0), "chat": float(sig.get("chat") or 0),
            "clips": float(sig.get("clips") or 0), "peak": float(reasons.get("peak") or 0),
            "score": float(item.get("score") or 0)}


def record(item: dict | None, approved: bool):
    """Log one decision (approve / reject). Announcement cards aren't moments, so they're not learned from."""
    if not item or item.get("campaign") == "own" or item.get("status") == "sample":
        return
    _init()
    f = features_of(item)
    with db.connect() as con:
        con.execute("INSERT OR REPLACE INTO decisions VALUES (?,?,?,?,?,?,?)",
                    (item["id"], time.time(), item.get("streamer") or "", item.get("campaign") or "", f["kind"],
                     int(approved), json.dumps(f)))


def decisions() -> list[dict]:
    _init()
    with db.connect() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM decisions ORDER BY at")]
    for r in rows:
        r["features"] = json.loads(r["features"] or "{}")
    return rows


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def weights() -> dict[str, float]:
    """Signal weights for picking moments, nudged by what you approve."""
    rows = decisions()
    yes = [r["features"] for r in rows if r["approved"]]
    no = [r["features"] for r in rows if not r["approved"]]
    if len(rows) < env_int("LEARN_MIN_DECISIONS", 10) or not yes or not no:
        return dict(BASE_WEIGHTS)
    out = {}
    for k, w in BASE_WEIGHTS.items():
        gap = _mean([f.get(k, 0) for f in yes]) - _mean([f.get(k, 0) for f in no])   # signals are 0..1
        out[k] = w * (1 + max(-0.5, min(0.5, gap * 1.5)))
    total = sum(out.values()) or 1.0
    return {k: round(v / total, 3) for k, v in out.items()}


def streamer_offset(login: str) -> float:
    """Added to the moment bar (MIN_MOMENT_SCORE) for this streamer: +0.15 if you reject most of their
    clips, -0.05 if you approve most."""
    if not login:
        return 0.0
    mine = [r for r in decisions() if r["streamer"] == login]
    if len(mine) < 5:
        return 0.0
    rate = _mean([r["approved"] for r in mine[-20:]])
    return 0.15 if rate < 0.3 else (-0.05 if rate > 0.7 else 0.0)


def reset():
    _init()
    with db.connect() as con:
        con.execute("DELETE FROM decisions")


def report() -> str:
    rows = decisions()
    if not rows:
        return "No decisions yet. Approve (✅) or reject (❌) some clips and it starts learning."
    lines = [f"{len(rows)} decisions: {sum(r['approved'] for r in rows)} approved, "
             f"{sum(not r['approved'] for r in rows)} rejected."]

    def table(key, title):
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r[key] or "?", []).append(r["approved"])
        lines.append(f"\n{title}:")
        for name, v in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            extra = ""
            if key == "streamer":
                off = streamer_offset(name)
                extra = "  bar raised" if off > 0 else ("  bar lowered" if off < 0 else "")
            lines.append(f"  {name:<22} {sum(v):>3}/{len(v):<3} approved ({_mean(v) * 100:.0f}%){extra}")

    table("kind", "By kind")
    table("streamer", "By streamer")
    yes = [r["features"] for r in rows if r["approved"]]
    no = [r["features"] for r in rows if not r["approved"]]
    lines.append("\nAverage signal (approved vs rejected):")
    for k in FEATURES:
        lines.append(f"  {k:<6} {_mean([f.get(k, 0) for f in yes]):>6.2f} vs {_mean([f.get(k, 0) for f in no]):.2f}")
    w = weights()
    learned = w != BASE_WEIGHTS
    lines.append("\nSignal weights used to pick moments" + ("" if learned else
                 f" (default until {env_int('LEARN_MIN_DECISIONS', 10)} decisions with both ✅ and ❌)") + ":")
    lines.append("  " + ", ".join(f"{k} {v:.2f} (was {BASE_WEIGHTS[k]:.2f})" for k, v in w.items()))
    return "\n".join(lines)

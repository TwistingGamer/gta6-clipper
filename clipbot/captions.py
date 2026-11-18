"""Speech-to-text captions (faster-whisper) and on-screen text overlays (ASS)."""
from __future__ import annotations

import os
import subprocess
import threading
import wave
from pathlib import Path

import numpy as np

from .config import DATA, FFMPEG, MODELS

_MODEL = None
_LOCK = threading.Lock()


def whisper_model():
    """Load the Whisper model once per process (the MVP reloaded it for every clip)."""
    global _MODEL
    with _LOCK:
        if _MODEL is None:
            from faster_whisper import WhisperModel
            _MODEL = WhisperModel(
                os.getenv("WHISPER_MODEL", "base"),
                device=os.getenv("WHISPER_DEVICE", "cpu"),
                compute_type=os.getenv("WHISPER_COMPUTE", "int8"),
                download_root=str(MODELS / "whisper"),
            )
        return _MODEL


def srt_time(seconds: float) -> str:
    ms = round(seconds * 1000)
    hours, ms = divmod(ms, 3_600_000)
    mins, ms = divmod(ms, 60_000)
    sec, ms = divmod(ms, 1000)
    return f"{hours:02}:{mins:02}:{sec:02},{ms:03}"


def transcribe(source: Path, start: float, duration: float, srt_out: Path, words_per_line: int = 4,
               info_out: dict | None = None) -> str:
    """Write an SRT aligned to the clip and return the plain transcript."""
    tmp = DATA / f"_audio_{os.getpid()}_{threading.get_ident()}.wav"
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-ss", str(start), "-i", str(source), "-t", str(duration),
         "-vn", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(tmp)],
        check=True,
    )
    try:
        # Hand Whisper the samples directly: its own decoder (PyAV) breaks on newer PyAV releases
        # ("unexpected keyword argument 'metadata_errors'").
        with wave.open(str(tmp), "rb") as wav:
            audio = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(np.float32) / 32768
        if not len(audio):
            srt_out.write_text("", encoding="utf-8")
            return ""
        segments, info = whisper_model().transcribe(audio, vad_filter=True, word_timestamps=True)
        if info_out is not None and info is not None:
            info_out["language"] = getattr(info, "language", "") or ""
            info_out["language_probability"] = float(getattr(info, "language_probability", 0) or 0)
        lines, text, idx = [], [], 1
        for seg in segments:
            text.append(seg.text.strip())
            words = seg.words or []
            groups = [words[j:j + words_per_line] for j in range(0, len(words), words_per_line)] if words else []
            if not groups and seg.text.strip():
                lines += [str(idx), f"{srt_time(max(0, seg.start))} --> {srt_time(min(duration, seg.end))}", seg.text.strip(), ""]
                idx += 1
            for g in groups:
                lines += [str(idx), f"{srt_time(max(0, g[0].start))} --> {srt_time(min(duration, g[-1].end))}",
                          " ".join(w.word.strip() for w in g).upper(), ""]
                idx += 1
        srt_out.write_text("\n".join(lines), encoding="utf-8")
        return " ".join(t for t in text if t)
    finally:
        tmp.unlink(missing_ok=True)


def _ass_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def ass_ts(seconds: float) -> str:
    cs = round(seconds * 100)
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


# ---- Look: "vice" = 80s Miami neon (hot pink / cyan / sunset orange, glow), "plain" = white Arial ----
FONTS_DIR = Path(__file__).resolve().parent / "fonts"
# ASS colours are &HAABBGGRR
PINK, CYAN, ORANGE, WHITE, NIGHT = "&H00952DFF", "&H00FFE500", "&H003D8AFF", "&H00FFFFFF", "&H00401A2E"


def style() -> str:
    return os.getenv("CAPTION_STYLE", "vice").lower()


def _header(styles: list[str]) -> str:
    return ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
            "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            + "".join(styles)
            + "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")


def _style(name, font, size, colour, outline_colour, back, bold, border, outline, shadow, align, ml, mr, mv, spacing=0):
    return (f"Style: {name},{font},{size},{colour},{colour},{outline_colour},{back},{bold},0,0,0,100,100,{spacing},0,"
            f"{border},{outline},{shadow},{align},{ml},{mr},{mv},1\n")


def _srt_entries(srt: Path) -> list[tuple[float, float, str]]:
    def secs(t: str) -> float:
        h, m, rest = t.strip().split(":")
        sec, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000
    out = []
    for block in srt.read_text(encoding="utf-8").strip().split("\n\n"):
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) >= 3 and "-->" in lines[1]:
            a, b = lines[1].split("-->")
            out.append((secs(a), secs(b), " ".join(lines[2:])))
    return out


def captions_ass(srt: Path, out: Path, y_from_bottom: int):
    """Burned-in speech captions. Vice: chunky white letters with a neon glow that alternates pink / cyan."""
    entries = _srt_entries(srt)
    if style() == "plain":
        head = _header([_style("Cap", "Arial", 100, WHITE, "&H00000000", "&H64000000", 1, 1, 7, 2, 2, 60, 60, y_from_bottom)])
        body = "".join(f"Dialogue: 0,{ass_ts(a)},{ass_ts(b)},Cap,,0,0,0,,{_ass_escape(t)}\n" for a, b, t in entries)
    else:
        head = _header([
            _style("Glow", "Bungee", 116, WHITE, PINK, "&H00000000", 0, 1, 14, 0, 2, 60, 60, y_from_bottom),
            _style("Cap", "Bungee", 116, WHITE, NIGHT, "&H96000000", 0, 1, 5, 6, 2, 60, 60, y_from_bottom),
        ])
        body = ""
        # Each clip gets its own neon pair (from its file name), so a run of posts doesn't look identical.
        pairs = [(PINK, CYAN), (CYAN, PINK), (ORANGE, PINK), (PINK, ORANGE), (CYAN, ORANGE)]
        first, second = pairs[sum(out.stem.encode()) % len(pairs)]
        for i, (a, b, t) in enumerate(entries):
            glow = first if i % 2 == 0 else second
            shadow = second if i % 2 == 0 else first
            txt = _ass_escape(t)
            body += (f"Dialogue: 0,{ass_ts(a)},{ass_ts(b)},Glow,,0,0,0,,{{\\blur10\\3c{glow}\\fad(60,0)}}{txt}\n"
                     f"Dialogue: 1,{ass_ts(a)},{ass_ts(b)},Cap,,0,0,0,,{{\\4c{shadow}\\fad(60,0)}}{txt}\n")
    out.write_text(head + body, encoding="utf-8")


def overlay_ass(path: Path, duration: float, hook: str, credit: str, ad_label: str = "", watermark: str = "",
                hook_top: int = 230):
    """Hook text at the top for the first seconds, creator credit, ad label and your @ watermark."""
    hook_end = min(duration, float(os.getenv("HOOK_SECONDS", "4")))
    vice = style() != "plain"
    styles = [
        _style("Hook", "Pacifico", 104, PINK, NIGHT, "&H96000000", 0, 1, 6, 5, 8, 60, 60, hook_top) if vice else
        _style("Hook", "Arial", 78, WHITE, "&H00000000", "&H64000000", 1, 1, 6, 2, 8, 60, 60, hook_top),
        _style("HookGlow", "Pacifico", 104, PINK, CYAN, "&H00000000", 0, 1, 16, 0, 8, 60, 60, hook_top),
        _style("Credit", "Bungee" if vice else "Arial", 38, WHITE, PINK if vice else "&H00000000", "&H64000000", 0 if vice else 1, 1, 3, 1, 2, 40, 40, 110),
        _style("Ad", "Arial", 44, WHITE, "&H00000000", "&HB4000000", 1, 3, 2, 0, 7, 40, 40, 60),
        _style("Mark", "Pacifico" if vice else "Arial", 52, ORANGE if vice else WHITE, PINK if vice else "&H00000000", "&H00000000", 0, 1, 2, 0, 9, 40, 44, 60),
    ]
    lines = []
    if hook:
        if vice:  # cyan neon glow behind crisp pink script
            lines.append(f"Dialogue: 0,{ass_ts(0)},{ass_ts(hook_end)},HookGlow,,0,0,0,,{{\\blur12\\fad(150,200)}}{_ass_escape(hook)}")
        lines.append(f"Dialogue: 1,{ass_ts(0)},{ass_ts(hook_end)},Hook,,0,0,0,,{{\\fad(150,200)}}{_ass_escape(hook)}")
    if ad_label:
        lines.append(f"Dialogue: 1,{ass_ts(0)},{ass_ts(min(duration, 5))},Ad,,0,0,0,,{_ass_escape(ad_label)}")
    if credit:
        lines.append(f"Dialogue: 0,{ass_ts(0)},{ass_ts(duration)},Credit,,0,0,0,,{_ass_escape(credit)}")
    if watermark:  # subtle: 35% see-through, top right
        lines.append(f"Dialogue: 0,{ass_ts(0)},{ass_ts(duration)},Mark,,0,0,0,,{{\\alpha&H59&}}{_ass_escape(watermark)}")
    path.write_text(_header(styles) + "".join(l + "\n" for l in lines), encoding="utf-8")

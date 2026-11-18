"""Speech-to-text captions (faster-whisper) and on-screen text overlays (ASS)."""
from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

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


def transcribe(source: Path, start: float, duration: float, srt_out: Path, words_per_line: int = 4) -> str:
    """Write an SRT aligned to the clip and return the plain transcript."""
    tmp = DATA / f"_audio_{os.getpid()}_{threading.get_ident()}.wav"
    subprocess.run(
        [FFMPEG, "-y", "-v", "error", "-ss", str(start), "-i", str(source), "-t", str(duration),
         "-ar", "16000", "-ac", "1", str(tmp)],
        check=True,
    )
    try:
        segments, _ = whisper_model().transcribe(str(tmp), vad_filter=True, word_timestamps=True)
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


def overlay_ass(path: Path, duration: float, hook: str, credit: str, ad_label: str = ""):
    """Hook text at the top for the first seconds, creator credit for the whole clip."""
    hook_end = min(duration, float(os.getenv("HOOK_SECONDS", "4")))
    ad_line = (f"Dialogue: 1,{ass_ts(0)},{ass_ts(min(duration, 5))},Ad,,0,0,0,,{_ass_escape(ad_label)}\n"
               if ad_label else "")
    hook_line = (f"Dialogue: 0,{ass_ts(0)},{ass_ts(hook_end)},Hook,,0,0,0,,{_ass_escape(hook)}\n"
                 if hook else "")
    path.write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
        "Style: Hook,Arial,78,&H00FFFFFF,&H00000000,&H64000000,1,1,6,2,8,60,60,230\n"
        "Style: Credit,Arial,40,&H00FFFFFF,&H00000000,&H64000000,1,1,3,1,2,40,40,120\n"
        "Style: Ad,Arial,44,&H00FFFFFF,&H00000000,&HB4000000,1,3,2,0,7,40,40,60\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + hook_line + ad_line
        + f"Dialogue: 0,{ass_ts(0)},{ass_ts(duration)},Credit,,0,0,0,,{_ass_escape(credit)}\n",
        encoding="utf-8",
    )

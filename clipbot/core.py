from __future__ import annotations
import json, math, os, re, shutil, sqlite3, subprocess, time, hashlib, logging
from datetime import datetime, timezone
from pathlib import Path
import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env')
DATA = ROOT / 'data'
RECORDINGS = ROOT / 'recordings'
OUTPUT = ROOT / 'output'
QUEUE = ROOT / 'queue'
for p in (DATA, RECORDINGS, OUTPUT, QUEUE): p.mkdir(exist_ok=True)
DB = DATA / 'clipbot.db'
LOG = logging.getLogger('clipbot')


def env_bool(k: str, default=False) -> bool:
    return os.getenv(k, str(default)).strip().lower() in ('1','true','yes','on')


def run(args: list[str], *, capture=False):
    result = subprocess.run(args, check=True, text=True, capture_output=capture)
    return result.stdout.strip() if capture else None


def ffprobe_seconds(path: Path) -> float:
    result = run(['ffprobe','-v','error','-show_entries','format=duration','-of','default=nokey=1:noprint_wrappers=1',str(path)], capture=True)
    return float(result)


def init_db():
    with sqlite3.connect(DB) as con:
        con.execute('''CREATE TABLE IF NOT EXISTS inputs (path TEXT PRIMARY KEY, size INTEGER, mtime REAL, status TEXT, error TEXT)''')
        con.execute('''CREATE TABLE IF NOT EXISTS clips (id TEXT PRIMARY KEY, source TEXT, output TEXT, score REAL, status TEXT, title TEXT, created_at TEXT, uploaded_at TEXT, youtube_id TEXT)''')
        con.execute('''CREATE TABLE IF NOT EXISTS stream_state (login TEXT PRIMARY KEY, last_online INTEGER, updated_at TEXT)''')


def duration_windows(length: float, window: int, max_count: int, scores: list[float]) -> list[tuple[float, float, float]]:
    if length < 10: return []
    if length <= window: return [(0., length, scores[0] if scores else 0.)]
    candidates = [(i * 10., min(float(window), length - i*10.), s) for i,s in enumerate(scores) if i * 10 < length-10]
    candidates.sort(key=lambda x:x[2], reverse=True)
    selected=[]
    for start, dur, score in candidates:
        if all(abs(start - prev[0]) >= window*.8 for prev in selected):
            selected.append((start,dur,score))
            if len(selected)>=max_count: break
    return selected


def audio_scores(path: Path, window_seconds=36) -> tuple[float, list[float]]:
    """Estimate energetic moments using 16kHz PCM; heuristic, not semantic AI."""
    seconds = ffprobe_seconds(path)
    raw = subprocess.run(['ffmpeg','-v','error','-i',str(path),'-vn','-ac','1','-ar','8000','-f','s16le','-'],capture_output=True,check=True).stdout
    x = np.frombuffer(raw,dtype='<i2').astype(np.float32)/32768
    if not len(x): return seconds, [0.0]
    frame = 8000
    rms = np.array([float(np.sqrt(np.mean(piece**2))) for off in range(0,len(x),frame) if len(piece:=x[off:off+frame])],dtype=np.float32)
    n=max(1,int(math.ceil(max(0,seconds-window_seconds)/10))+1)
    scored=[]
    for i in range(n):
        start=i*10; chunk=rms[start:min(len(rms),start+window_seconds)]
        if not len(chunk): scored.append(0.0); continue
        peaks=float(np.quantile(chunk,.90)); avg=float(np.mean(chunk)); variation=float(np.std(chunk))
        scored.append(float(min(1.0, peaks*3.3+avg*2.0+variation*2.0)))
    return seconds, scored


def srt_time(seconds: float) -> str:
    ms=round(seconds*1000); hours,ms=divmod(ms,3600000); mins,ms=divmod(ms,60000); sec,ms=divmod(ms,1000)
    return f'{hours:02}:{mins:02}:{sec:02},{ms:03}'


def transcribe_srt(source: Path, start: float, duration: float, out: Path):
    """Whisper on an isolated clip, then create an SRT aligned with that clip."""
    from faster_whisper import WhisperModel
    temp = DATA / f'_audio_{os.getpid()}.wav'
    run(['ffmpeg','-y','-v','error','-ss',str(start),'-i',str(source),'-t',str(duration),'-ar','16000','-ac','1',str(temp)])
    model=WhisperModel(os.getenv('WHISPER_MODEL','base'), device=os.getenv('WHISPER_DEVICE','cpu'), compute_type='int8')
    try:
        segments,_=model.transcribe(str(temp),vad_filter=True,word_timestamps=True)
        lines=[]; index=1
        for segment in segments:
            words=segment.words or []
            if words:
                for j in range(0,len(words),5):
                    group=words[j:j+5]
                    if not group: continue
                    lines.extend([str(index),f'{srt_time(max(0,group[0].start))} --> {srt_time(min(duration,group[-1].end))}',' '.join(w.word.strip() for w in group),''])
                    index+=1
            elif segment.text.strip():
                lines.extend([str(index),f'{srt_time(max(0,segment.start))} --> {srt_time(min(duration,segment.end))}',segment.text.strip(),''])
                index+=1
        out.write_text('\n'.join(lines), encoding='utf-8')
        return index-1
    finally:
        temp.unlink(missing_ok=True)


def filter_path(p: Path) -> str:
    # FFmpeg filter arguments require escaped Windows separators, colons and apostrophes.
    return p.as_posix().replace('\\','\\\\').replace(':','\\:').replace("'", "\\'").replace('[','\\[').replace(']','\\]')


def render(source: Path,start:float,duration:float,srt:Path,dest:Path):
    """Output 1080x1920 H.264 vertical video; blurred background retains the original gameplay."""
    base=("[0:v]split=2[back][front];"
          "[back]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=28:12[bg];"
          "[front]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
          "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[v]")
    # Render the subtitles only when there is recognized dialogue.
    if srt.exists() and srt.stat().st_size:
        base += f";[v]subtitles=filename='{filter_path(srt)}':force_style='FontSize=17,Alignment=2,MarginV=300,Outline=3,Shadow=1'[out]"
        mapping='[out]'
    else: mapping='[v]'
    run(['ffmpeg','-y','-v','error','-ss',str(start),'-i',str(source),'-t',str(duration),'-filter_complex',base,'-map',mapping,'-map','0:a?','-c:v','libx264','-preset','veryfast','-crf','24','-pix_fmt','yuv420p','-c:a','aac','-b:a','128k','-movflags','+faststart',str(dest)])


def make_title(source:Path, score:float, streamer:str='') -> str:
    creator=streamer or source.stem.split('__')[0]
    return f'GTA 6 Highlight — {creator} | Insane Moment'[:100]


def process_file(path:Path, *, skip_rights_check=False) -> int:
    init_db()
    if not skip_rights_check and not env_bool('AUTHORIZED_SOURCE'):
        raise RuntimeError('Set AUTHORIZED_SOURCE=true only after you have permission to use and repost the recordings.')
    path=path.resolve(); stat=path.stat()
    if path.suffix.lower() not in ('.mp4','.mkv','.mov','.webm','.ts'): return 0
    with sqlite3.connect(DB) as con:
        previous=con.execute('SELECT size,mtime,status FROM inputs WHERE path=?',(str(path),)).fetchone()
        if previous and previous==(stat.st_size,stat.st_mtime,'done'): return 0
        con.execute('INSERT OR REPLACE INTO inputs VALUES (?,?,?,?,?)',(str(path),stat.st_size,stat.st_mtime,'processing',None))
    made=0
    try:
        window=int(os.getenv('CLIP_SECONDS','36'))
        length,scores=audio_scores(path,window)
        selection=duration_windows(length,window,int(os.getenv('MAX_CLIPS_PER_SEGMENT','2')),scores)
        for start,duration,score in selection:
            if score<float(os.getenv('MIN_SCORE','.25')): continue
            signature=hashlib.sha256(f'{path}|{stat.st_size}|{stat.st_mtime}|{start}'.encode()).hexdigest()[:16]
            dest=OUTPUT/f'{path.stem}_{signature}.mp4'
            srt=OUTPUT/f'{path.stem}_{signature}.srt'
            if dest.exists(): continue
            LOG.info('Generating clip from %s at %.1fs (energy %.2f)',path.name,start,score)
            transcribe_srt(path,start,duration,srt)
            render(path,start,duration,srt,dest)
            title=make_title(path,score)
            now=datetime.now(timezone.utc).isoformat()
            with sqlite3.connect(DB) as con:
                con.execute('INSERT OR IGNORE INTO clips VALUES (?,?,?,?,?,?,?,?,?)',(signature,str(path),str(dest),score,'ready',title,now,None,None))
            (QUEUE/f'{signature}.json').write_text(json.dumps({'id':signature,'file':str(dest),'title':title,'description':f'{title}\n\n#GTA6 #GTA #Gaming #Shorts','score':round(score,3),'status':'ready','source':str(path),'requires_original_value_add':True},indent=2),encoding='utf-8')
            made+=1
        with sqlite3.connect(DB) as con: con.execute('UPDATE inputs SET status=?,error=NULL WHERE path=?',('done',str(path)))
    except Exception as exc:
        with sqlite3.connect(DB) as con: con.execute('UPDATE inputs SET status=?,error=? WHERE path=?',('error',str(exc),str(path)))
        raise
    return made


def scan_recordings() -> int:
    count=0
    for path in sorted(RECORDINGS.glob('*')):
        if not path.is_file() or path.suffix.lower() not in ('.mp4','.mkv','.mov','.webm','.ts'):continue
        # Avoid files still being written: last modification at least 20 seconds ago
        if time.time()-path.stat().st_mtime<20:continue
        try: count+=process_file(path)
        except Exception: LOG.exception('Failed processing %s',path)
    return count


def statuses() -> list[dict]:
    init_db()
    with sqlite3.connect(DB) as con:
        rows=con.execute('SELECT id,output,score,status,title FROM clips ORDER BY created_at DESC LIMIT 30').fetchall()
    return [dict(zip(('id','output','score','status','title'),r)) for r in rows]

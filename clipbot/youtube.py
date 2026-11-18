"""YouTube upload using the account owner's consent-based OAuth credentials."""
import os, json, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from .core import DB, ROOT, QUEUE, env_bool, init_db
SCOPES=['https://www.googleapis.com/auth/youtube.upload']


def credentials():
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    secrets=ROOT/'client_secrets.json';cache=ROOT/'data'/'youtube_token.json'
    if not secrets.exists():raise RuntimeError('Missing client_secrets.json; see README for YouTube OAuth setup')
    creds=None
    if cache.exists():creds=Credentials.from_authorized_user_file(str(cache),SCOPES)
    if creds and creds.expired and creds.refresh_token:creds.refresh(Request())
    elif not creds or not creds.valid:
        creds=InstalledAppFlow.from_client_secrets_file(str(secrets),SCOPES).run_local_server(port=0)
    cache.write_text(creds.to_json(),encoding='utf-8')
    return creds


def upload(clip_id: str, *, confirm=False):
    if not confirm and not env_bool('AUTO_UPLOAD_YOUTUBE'):
        raise RuntimeError('Use --confirm for a manual upload or set AUTO_UPLOAD_YOUTUBE=true for scheduled uploads')
    if not env_bool('AUTHORIZED_SOURCE'):
        raise RuntimeError('AUTHORIZED_SOURCE=true required (only for videos you are allowed to repost)')
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    init_db();file=QUEUE/f'{clip_id}.json'
    if not file.exists():raise FileNotFoundError(file)
    item=json.loads(file.read_text(encoding='utf-8'))
    if item.get('status')=='uploaded':return item.get('youtube_id')
    video=Path(item['file'])
    if not video.is_file():raise FileNotFoundError(video)
    privacy=os.getenv('YOUTUBE_PRIVACY','private')
    if privacy not in ('private','unlisted','public'):raise ValueError('YOUTUBE_PRIVACY must be private/unlisted/public')
    service=build('youtube','v3',credentials=credentials())
    request=service.videos().insert(part='snippet,status',body={'snippet':{'title':item['title'],'description':item['description'],'categoryId':'20'},'status':{'privacyStatus':privacy,'selfDeclaredMadeForKids':False}},media_body=MediaFileUpload(str(video),mimetype='video/mp4',resumable=True,chunksize=8*1024*1024))
    response=None
    while response is None:
        _,response=request.next_chunk()
    video_id=response['id']
    item['status']='uploaded';item['youtube_id']=video_id
    file.write_text(json.dumps(item,indent=2),encoding='utf-8')
    with sqlite3.connect(DB) as con:
        con.execute('UPDATE clips SET status=?,youtube_id=?,uploaded_at=? WHERE id=?',('uploaded',video_id,datetime.now(timezone.utc).isoformat(),clip_id))
    return video_id


def post_next():
    """At most UPLOADS_PER_DAY total posts, spaced through UTC days."""
    if not env_bool('AUTO_UPLOAD_YOUTUBE'):return None
    init_db()
    daily=max(1,int(os.getenv('UPLOADS_PER_DAY','3')))
    today=datetime.now(timezone.utc).date().isoformat()
    with sqlite3.connect(DB) as con:
        count=con.execute("SELECT COUNT(*) FROM clips WHERE uploaded_at>=?",(today,)).fetchone()[0]
        latest=con.execute("SELECT MAX(uploaded_at) FROM clips WHERE uploaded_at IS NOT NULL").fetchone()[0]
        row=con.execute("SELECT id FROM clips WHERE status='ready' ORDER BY score DESC LIMIT 1").fetchone()
    if count>=daily or not row:return None
    if latest:
        elapsed=(datetime.now(timezone.utc)-datetime.fromisoformat(latest)).total_seconds()
        if elapsed < 86400/daily:return None
    return upload(row[0])

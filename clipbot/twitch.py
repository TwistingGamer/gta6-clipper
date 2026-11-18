"""Online-status monitoring and explicitly authorized stream capture."""
import os, subprocess, logging, time
from pathlib import Path
import requests
from .core import RECORDINGS, env_bool
LOG=logging.getLogger('clipbot.twitch')

class Twitch:
    def __init__(self):
        self.client_id=os.getenv('TWITCH_CLIENT_ID','')
        self.secret=os.getenv('TWITCH_CLIENT_SECRET','')
        self.access_token='';self.expires_at=0

    def token(self):
        if time.time()<self.expires_at-60:return self.access_token
        if not self.client_id or not self.secret:
            raise RuntimeError('TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET are required to monitor Twitch')
        r=requests.post('https://id.twitch.tv/oauth2/token',data={'client_id':self.client_id,'client_secret':self.secret,'grant_type':'client_credentials'},timeout=15)
        r.raise_for_status();payload=r.json()
        self.access_token=payload['access_token'];self.expires_at=time.time()+payload['expires_in'];return self.access_token

    def online(self, logins:list[str]):
        if not logins:return {}
        r=requests.get('https://api.twitch.tv/helix/streams',params=[('user_login',name) for name in logins],headers={'Client-Id':self.client_id,'Authorization':f'Bearer {self.token()}'},timeout=20)
        r.raise_for_status()
        return {stream['user_login'].lower():stream for stream in r.json().get('data',[])}


def capture_authorized(login:str,seconds:int):
    """One segment of approved streaming content using streamlink; caller enforces permissions."""
    if not env_bool('AUTHORIZED_SOURCE') or not env_bool('CAPTURE_STREAMS'):
        raise RuntimeError('Explicit recording permission and CAPTURE_STREAMS=true are required.')
    out=RECORDINGS/f'{login}__{int(time.time())}.mp4'
    # Recording in streamlink subprocess is time-limited by FFmpeg. No circumvention of DRM.
    producer=subprocess.Popen(['streamlink','--stdout',f'https://www.twitch.tv/{login}','best'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    try:
        result=subprocess.run(['ffmpeg','-y','-loglevel','error','-i','pipe:0','-t',str(seconds),'-c:v','libx264','-preset','ultrafast','-crf','27','-c:a','aac',str(out)],stdin=producer.stdout,timeout=seconds+90)
        if result.returncode!=0 or not out.exists() or out.stat().st_size<100_000:
            out.unlink(missing_ok=True);raise RuntimeError(f'Unable to capture stream for {login}')
        return out
    finally:
        if producer.stdout:producer.stdout.close()
        producer.terminate()
        try:producer.wait(timeout=8)
        except subprocess.TimeoutExpired:producer.kill();producer.wait()

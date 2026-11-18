import argparse, json, logging, os, sys, time, threading
from pathlib import Path
from .core import init_db,process_file,scan_recordings,statuses,RECORDINGS,env_bool
from .twitch import Twitch,capture_authorized
from .youtube import upload,post_next
logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
LOG=logging.getLogger('clipbot')

def live_capture(login,seconds):
    while True:
        try:
            p=capture_authorized(login,seconds)
            LOG.info('Captured %s',p)
        except Exception:LOG.exception('Capture failed for %s',login)
        time.sleep(10)

def daemon():
    init_db()
    logins=[p.strip().lower() for p in os.getenv('STREAMERS','').split(',') if p.strip()]
    twitch=Twitch();active={}
    while True:
        try:
            if logins:
                online=twitch.online(logins)
                for login in logins:
                    info=online.get(login)
                    if info:LOG.info('%s LIVE: %s',login,info.get('title',''))
                    if info and env_bool('CAPTURE_STREAMS') and env_bool('AUTHORIZED_SOURCE') and login not in active:
                        t=threading.Thread(target=live_capture,args=(login,int(os.getenv('SEGMENT_SECONDS','300'))),daemon=True)
                        t.start();active[login]=t
                    if login in active and not active[login].is_alive():active.pop(login)
            new=scan_recordings()
            if new:LOG.info('Created %s shorts',new)
            try:
                video=post_next()
                if video:LOG.info('Uploaded to YouTube: %s',video)
            except Exception:LOG.exception('YouTube upload failed; retry next cycle')
        except Exception:LOG.exception('Daemon cycle failure')
        time.sleep(max(15,int(os.getenv('POLL_SECONDS','60'))))

def main():
    a=argparse.ArgumentParser(description='GTA 6 24/7 rights-aware clipping prototype')
    s=a.add_subparsers(dest='cmd',required=True)
    s.add_parser('run');s.add_parser('scan');s.add_parser('status');s.add_parser('check-twitch')
    p=s.add_parser('process');p.add_argument('file',type=Path)
    u=s.add_parser('upload-youtube');u.add_argument('clip_id');u.add_argument('--confirm',action='store_true')
    args=a.parse_args();init_db()
    if args.cmd=='run':daemon()
    elif args.cmd=='process':print('Clips created:',process_file(args.file))
    elif args.cmd=='scan':print('Clips created:',scan_recordings())
    elif args.cmd=='status':print(json.dumps(statuses(),indent=2))
    elif args.cmd=='check-twitch':
        names=[x.strip() for x in os.getenv('STREAMERS','').split(',') if x.strip()]
        print(json.dumps(Twitch().online(names),indent=2))
    elif args.cmd=='upload-youtube':print('YouTube ID:',upload(args.clip_id,confirm=args.confirm))

if __name__=='__main__':main()

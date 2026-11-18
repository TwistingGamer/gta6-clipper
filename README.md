# GTA 6 Clipbot — 24/7 MVP

A working **local-first prototype** for creating captioned vertical highlight clips from content you own or are authorized to reuse. Watches Twitch live state, optionally records authorized streams, detects loud/energetic moments, transcribes dialogue using faster-whisper, renders 1080x1920 clips with FFmpeg, queues metadata, and optionally uploads to YouTube using OAuth.

**This is not a monetization guarantee.** The highlight detector is a simple audio-energy heuristic (not yet a multimodal understanding model). It can pick loud but uninteresting moments, and the generated clips need additional original commentary/editing before they may qualify for monetization. **Only record and repost where streamer permission, game publisher policies and platform rules allow it.**

## 1. Install (Windows PowerShell)

Install **Python 3.11**, **FFmpeg** (both ffmpeg and ffprobe in PATH), and optionally **Streamlink** (only if capturing an approved live stream). In this folder:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and change `AUTHORIZED_SOURCE=true` **only after permission is confirmed**. Without permission, use only your own test recordings. To test, place an MP4 of your own gameplay in `recordings/` and run:

```powershell
python -m clipbot scan
python -m clipbot status
```

Finished MP4 and SRT files go to `output/`, metadata to `queue/`. First run downloads the Whisper speech recognition model. FFmpeg must have the `subtitles` filter enabled (libass).

## 2. Continuous operation

```powershell
python -m clipbot run
```

`run` checks the folder every 60 seconds and processes finished video segments. Run it via a Windows scheduled task / systemd on a machine that stays powered on. There is no need to open a browser. For 24/7 operation a VPS needs adequate CPU, disk and bandwidth; captioning and encoding can be expensive.

### Monitor Twitch

Register your app at https://dev.twitch.tv/console/apps and set the `TWITCH_CLIENT_ID`, `TWITCH_CLIENT_SECRET`, and comma-separated `STREAMERS` in `.env`. Test with:

```powershell
python -m clipbot check-twitch
```

Monitoring Twitch does NOT imply recording permission. To record approved streams, install Streamlink CLI and explicitly set `CAPTURE_STREAMS=true` and `AUTHORIZED_SOURCE=true`; then `run` will capture ~5-minute chunks with Streamlink and FFmpeg. The recording function is a **prototype**, not guaranteed to work on every stream. It does not defeat access controls or DRM. Capturing several high-bitrate streams is bandwidth/CPU intensive. Stop capture by stopping the daemon. Capture threads may take up to one segment to notice that a streamer went offline.

## 3. YouTube Shorts uploads

1. Create a Google Cloud project, enable **YouTube Data API v3**, configure the OAuth consent screen and create an **OAuth Desktop** client. Save the downloaded JSON as `client_secrets.json` in the project root (never commit).
2. First upload manually using `python -m clipbot status` to find a clip ID, then `python -m clipbot upload-youtube YOUR_CLIP_ID --confirm`. The OAuth login opens a browser.
3. Uploads default to `private`. Confirm content rights and originality, then optionally set `YOUTUBE_PRIVACY=public` (or `unlisted`).
4. For ongoing uploads, set `AUTO_UPLOAD_YOUTUBE=true`. Uploads are limited by `UPLOADS_PER_DAY`, with an interval between posts. New accounts and unverified API projects may encounter publishing restrictions; review the current YouTube API docs.

**Do not enable auto-upload until you trust your clipping, permissions, OAuth and quality review process.**

## 4. TikTok / Instagram

Not implemented. Their official posting workflows require creator authorizations, and TikTok Direct Post requires an app audit to lift visibility restrictions. We deliberately do not use browser bots or unofficial private upload endpoints.

## 5. Source layout

- `clipbot/core.py`: scoring, Whisper captions, FFmpeg composition, SQLite & queue
- `clipbot/twitch.py`: Twitch Helix monitoring and opt-in Streamlink capture
- `clipbot/youtube.py`: OAuth upload with daily limit
- `clipbot/__main__.py`: CLI and continuously running daemon
- `tests/`: basic tests

## 6. Next steps for a production business + portfolio

- Add creator-permission database with explicit approval/revocation.
- Upgrade highlight ranking to Twitch chat spikes, speech sentiment and visual gaming-event detection.
- Add facecam-aware crop, dynamic text hooks and original commentary / analysis templates.
- Add human review dashboard, quality metrics, retention data, duplicate detection across creators and crash-safe concurrent task queue.
- Use Docker + PostgreSQL + workers and persistent object storage; rate-limit platform uploads.
- Add TikTok/Instagram only via approved, documented platform APIs.
- Track content licenses, revenue and operating costs; comply with copyright and reused-content monetization rules.

## Troubleshooting

- `ffmpeg not found`: add FFmpeg bin directory to PATH and restart PowerShell.
- `No such filter subtitles`: install an FFmpeg build with libass.
- `Unauthorized source`: do not bypass; only use material you have permission to record/repost.
- `No clips`: try lowering `MIN_SCORE` to `0.08`; quiet videos have low RMS energy.
- `Cannot load whisper model`: confirm internet is available for first download, or use a cached model.
- `YouTube permissions`: ensure OAuth has `youtube.upload` permission, verify consent screen access, and check API policies.

## Tests

```powershell
python -m unittest discover -s tests -v
```

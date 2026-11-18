# Campaign Clipbot

Clips streams and videos for **paid clipping campaigns** (Content Rewards / Whop, Reach Clipping, etc.),
starting with **GTA 6 launch streams**. It:

1. **Brings in approved footage**: records campaign streamers live on Twitch, Kick or YouTube (only while they're playing GTA, if you want), or downloads the approved YouTube video or channel uploads, or picks up a folder of footage you downloaded.
2. **Finds the best moments**: Twitch chat spikes and hype emotes, clips viewers already made, loudness peaks, then an AI judge that checks each moment against the campaign's rules.
3. **Makes a vertical clip**: 1080x1920, blurred fill, burned-in captions, a hook line, a creator credit and an `#ad` label when needed.
4. **Queues it for your OK**, then posts it on schedule. That means either a ping to your phone with the file and caption ready, or a direct upload through the official API once your API apps are approved.
5. **Tracks the links** so you can submit them to the campaign. Some campaigns require that within 30 minutes of posting.

Started from a ChatGPT-built MVP. Kept its FFmpeg pipeline and permission-first design. Fixed captions rendering off-screen and the Whisper model reloading for every clip. Added the rest.

> **Only clip footage a campaign or creator lets you use.** A campaign is ignored unless `campaigns.yaml`
> marks it active with permission evidence (its rules-page link). Strikes on Reach campaigns are permanent,
> so keep clip review on.

## Setup (Windows)

Works on **Python 3.10 to 3.13** (tested on 3.13). In PowerShell:

```powershell
# 1. Tools (FFmpeg and Streamlink), one time
winget install Gyan.FFmpeg
winget install Streamlink.Streamlink
#    then close and reopen PowerShell so they're on PATH

# 2. Unzip the bot into its own folder and go into it
Expand-Archive "$HOME\Downloads\campaign-clipbot.zip" -DestinationPath "$HOME\clipbot"
cd "$HOME\clipbot"

# 3. Python environment
py -3.13 -m venv .venv
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned   # only needed once, if Activate.ps1 is blocked
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
Copy-Item campaigns.example.yaml campaigns.yaml
```

Every time you open a new PowerShell window later: `cd "$HOME\clipbot"` then `.\.venv\Scripts\Activate.ps1`.

Then:

* **campaigns.yaml**: for each campaign you've joined, paste the rules link into `permission.evidence`, check the `CHECK` items against the rules page, and set `active: true`.
* **.env**: add your Twitch app keys (dev.twitch.tv/console, a free "Application"). They're used for live status and viewer clips. Optionally add an `ANTHROPIC_API_KEY` for the AI judge and titles (without one, every candidate is kept with template titles), and a Discord `NOTIFY_WEBHOOK_URL` for phone pings.

Check it:

```powershell
python -m clipbot campaigns     # which campaigns are active, priority, posting limits
python -m unittest discover -s tests
```

Run it on a PC that stays on (sleep disabled), or a Windows Task Scheduler "at startup" task:

```powershell
python -m clipbot run
```

## Daily use

```powershell
python -m clipbot review                     # clips waiting for you (files are in output\)
python -m clipbot approve <ID>               # or: reject <ID>
#   approved clips are staged in output\ready\<platform>\ (video + caption .txt) and you get a ping
python -m clipbot posted <ID> tiktok <link>  # after you post it; reminds you to submit the link
python -m clipbot report                     # today's links per campaign, for submitting
```

**Warm-up:** with `ACCOUNTS_STARTED` set, each platform gets 1 post a day in week 1, 2 in week 2, and so on, up to `POSTS_PER_DAY`.
Brand-new accounts that post a lot right away tend to get throttled.

**Priority:** when several campaigns have clips ready, the easiest, best-paying one goes first.
Fixed footage beats live streams, and higher CPM, a higher max payout and remaining budget all count.
Update `budget.used` from the dashboard now and then. A campaign whose budget is used up is skipped.

## Adding a campaign

Copy any block in `campaigns.yaml`. Fields:

| field | meaning |
|---|---|
| `inputs` | `twitch` / `kick` / `youtube_live` (record live), `youtube_video` (with optional `start_at`/`end_at`), `youtube_channel` (newest uploads), `folder` |
| `platforms` | where it pays: `tiktok`, `instagram`, `youtube`, `x` |
| `payout` / `budget` | CPM, min/max payout, budget total/used (used for priority) |
| `game_filter` | only record while the stream category contains this (e.g. `grand theft auto`) |
| `required_hashtags`, `required_mentions`, `required_words` | always added to captions |
| `watermark`, `credit` | on-screen text for the whole clip |
| `ad_disclosure`, `sponsor` | `#ad` on screen + caption, plus YouTube/TikTok paid-promotion labels |
| `ai_text_allowed`, `title_template` | `false` if the campaign bans AI content: template text only |
| `content_rule` | extra rule the AI judge enforces (e.g. "at least 2 Core members on screen") |
| `require_review`, `clip_preapproval` | you approve every clip / the campaign approves each clip too |
| `post_hours`, `submit_within_minutes` | only post while you're awake to submit links in time |
| `bio_link` | link the campaign needs in your profile bio (set it by hand; shown in `report`) |

## Posting through the APIs (later)

All three start in **manual** mode, because brand-new API apps can only post privately:

* **YouTube**: Google Cloud project, enable YouTube Data API v3, create an OAuth "Desktop" client and save it as `client_secrets.json`, then run `python -m clipbot auth youtube`. Uploads from unverified projects stay private until Google's audit passes.
* **TikTok**: developer app with Content Posting API (Direct Post). Set `TIKTOK_CLIENT_KEY`/`SECRET`/`REDIRECT_URI`, then run `python -m clipbot auth tiktok`. Posts are forced private until TikTok audits the app.
* **Instagram**: Professional account and an app with `instagram_content_publish`. Set `IG_USER_ID` and a long-lived `IG_ACCESS_TOKEN`, which expires about every 60 days.
* **X**: manual only for now. Video posting through the X API is a paid plan; check the price before buying.

When an app is approved, set `POST_MODE_<PLATFORM>=api` in `.env`.

## Layout

```
clipbot/sources.py    campaigns.yaml model: permission, inputs, rules, priority
clipbot/fetch.py      YouTube downloads (yt-dlp), live recording (Streamlink)
clipbot/twitch.py     Twitch live status + viewer clips      clipbot/chat.py  chat logger + spike scores
clipbot/detect.py     candidate windows from all signals     clipbot/ai.py    AI judge (Claude)
clipbot/captions.py   Whisper captions + hook/credit/#ad overlay
clipbot/render.py     FFmpeg vertical render                 clipbot/pipeline.py  footage -> clips
clipbot/publish/      scheduler, manual staging, YouTube/TikTok/Instagram uploaders
clipbot/db.py         SQLite (data/clipbot.db), JSON mirror in queue/
```

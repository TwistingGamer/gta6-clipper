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

## Setup (Windows, everything on D:)

Works on **Python 3.10 to 3.13** (tested on 3.13). Nothing below writes to C:. The Python environment,
Streamlink, recordings, clips, temp files and the downloaded speech model all live in the bot's folder.

1. **FFmpeg:** download the "essentials" build zip from gyan.dev and extract it to `D:\tools`.
   You'll get a folder like `D:\tools\ffmpeg-<version>-essentials_build\bin`. It doesn't need to be on PATH.
2. **The bot:** extract `campaign-clipbot.zip` to `D:\campaign-clipbot`.
3. In PowerShell:

```powershell
$env:TEMP = "D:\tmp"; $env:TMP = "D:\tmp"; $env:PIP_CACHE_DIR = "D:\pipcache"   # keep pip off C:
New-Item -ItemType Directory -Force D:\tmp, D:\pipcache | Out-Null
cd D:\campaign-clipbot
py -3.13 -m venv .venv
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned   # once, if Activate.ps1 is blocked (answer Y)
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
```

4. In `.env`, set `FFMPEG_PATH` to your FFmpeg `bin` folder, without quotes. For example:
   `FFMPEG_PATH=D:\tools\ffmpeg-8.0-essentials_build\bin`

Every time you open a new PowerShell window later: `cd D:\campaign-clipbot` then `.\.venv\Scripts\Activate.ps1`.

Then:

* **Campaigns**: switch on each campaign you've joined, for example `python -m clipbot enable core-pool --joined 2026-11-18`. Switch one off with `python -m clipbot disable core-pool`. You never need to edit YAML: the rules live in `campaigns.example.yaml` and `git pull` updates them. Your own settings go in `campaigns.yaml`, which is out of git. An old full copy of `campaigns.yaml` is converted automatically, and the original is kept as `campaigns.yaml.bak`.
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

## See it work: one preview clip

```powershell
python -m clipbot sample                # biggest live GTA stream right now (needs Twitch keys)
python -m clipbot sample kaicenat       # or name any live Twitch channel
```
It records 3 minutes (`--minutes 5` for more) along with the chat, then makes one captioned vertical clip in
`output\sample\`. Sample clips are never posted. `python -m clipbot discover` lists the biggest live GTA streams
and whether any of your campaigns covers them.

## How live clipping works

The bot reads each streamer's Twitch chat live and keeps only the last few minutes of video (`CAPTURE_MODE=moments`).
When chat suddenly runs about 3 times faster than normal with "KEKW / clip it / W" energy, it saves about 45 seconds before
that moment and 20 seconds after it. The clipper then finds the peak and cuts from about 25 seconds before it
(the setup) to about 10 seconds after (the payoff), so the clip makes sense to someone who wasn't watching. Disk use stays around
0.5 GB per live stream, and saved moments are deleted after a day. A short buffer works even when VODs are
subscriber-only or deleted.

## Review from your phone

Every new clip is sent to your Discord channel (`NOTIFY_WEBHOOK_URL`) with a 720p copy under 10 MB that you can watch
and save. To approve or reject by reacting ✅ / ❌ on that message:

1. https://discord.com/developers/applications > **New Application** > **Bot** > **Reset Token** > copy it into
   `.env` as `DISCORD_BOT_TOKEN=`.
2. **OAuth2 > URL Generator**: scope `bot`, permissions *View Channels* + *Read Message History*. Open the link and add
   the bot to your server.
3. Discord settings > Advanced > **Developer Mode** on. Right-click (long-press) your own name > **Copy User ID** >
   `.env` as `DISCORD_USER_ID=`. Only your reactions count.
4. Restart the bot. Reactions are picked up within a minute.

## Clip file names

`2026-11-18_0442_typicalgamer_first-look_police-chase_score87_<ID>.mp4`: local time of the moment, streamer, kind
(`first-look`, `viewer-clip`, `moment`), a few words about it (the AI title, or what chat and the streamer said most),
the score (0-99), and the clip ID you use with `approve` / `posted`.

## What makes the cut

Launch day has hours of menus and calm talking, so the bot is picky:
* chat has to blow up (about 4x its normal speed, with lots of KEKW / LMAO / clip it / 💀), measured against that
  channel's own normal, or a viewer made a Twitch clip of the moment;
* the streamer has to get loud (yelling, laughing), and frozen screens (menus, loading) only pass with a huge reaction;
* streams where two webcams sit far apart on screen (someone watching another streamer play) are skipped;
* at most 3 clips per streamer per hour: a better one replaces the weakest one still waiting for review.

**First look:** when a streamer loads into GTA 6 for the first time (their category switches to GTA VI, or their stream
just started in it), the bot saves their first reaction even without a chat spike, titles it "{streamer}'s first time
in GTA 6", and posts those first. One per streamer.

**Disk safety:** recording pauses (and you get a ping) when the drive has under 20 GB free (`MIN_FREE_GB`), and
resumes on its own. Clips you haven't approved within 12 hours are deleted (`UNREVIEWED_HOURS`), previews after 24 hours, and posted clips as soon as every link is in. `python -m clipbot cleanup` wipes everything unapproved right now. Disk use while running is
about 0.5 GB per stream being watched, plus ~10-20 MB per clip.

## How clips look

Each clip is 1080x1920. When the stream has a webcam, the streamer's face goes on top and the gameplay below
(`FRAME_MODE=auto`). With no webcam, the gameplay fills the screen. Captions (Whisper) sit under the facecam,
the streamer credit at the bottom. With an `ANTHROPIC_API_KEY` each clip also opens with a short hook line that
explains what's going on. If the facecam is missed, set `FACECAM=x,y,w,h` (fractions of the frame) in `.env`.

**Vice City look:** captions are chunky white letters with a pink / cyan neon glow, the hook line is a hot-pink script,
and your `@gta6.clippaa` watermark sits small in the top-right corner (`WATERMARK_TEXT`). Campaigns that ban logos
(CORE, Expedia) have `my_watermark: false`, so their clips carry no watermark. A logo image can be added later with
`WATERMARK_IMAGE`. `CAPTION_STYLE=plain` goes back to white Arial. Fonts: Pacifico and Bungee (SIL Open Font License),
no Rockstar logos, fonts or artwork.

**GTA 6 only:** campaigns with `game_filter: gta6` only record streams whose Twitch category is exactly
"Grand Theft Auto VI" (GTA V is skipped), and the category is checked again the moment chat spikes, because streamers
switch games mid-stream. A campaign can allow GTA V explicitly with `game_filter: "gta5, gta6"` (Lysium's GTA RP does).

## Announcement posts

```powershell
python -m clipbot announce "Kai Cenat" "TONIGHT 8 PM ET" --when2 "5 PM PT" --where twitch.tv/kaicenat --tags "#KaiCenat #GTA6"
```
Makes an 8-second Vice City style card (original sunset/grid artwork, no logos or footage) and a caption, then puts it
in the review queue under your own posts. Approve it and post it like any clip. Add a trending sound in the app.

## Clipping any GTA stream (gta-any)

`python -m clipbot enable gta-any` follows the 4 biggest live GTA 6 Twitch streams (English, at least
1,000 viewers, fastest-growing first) that none of your campaigns covers, and clips them for **your own channels**: streamer credited on
screen and in the caption, never submitted to a campaign, no story cutscenes or endings. Streamers that have a
campaign entry (paid or tolerated, on or off) are never picked up here, so their own rules apply (e.g. IShowSpeed
stays off YouTube). Needs the Twitch keys. Change how many: `python -m clipbot set gta-any discover_limit 6`.

## Daily use

```powershell
python -m clipbot review                     # clips waiting for you (files are in output\)
python -m clipbot approve <ID>               # or: reject <ID>
#   approved clips are staged in output\ready\<platform>\ (video + caption .txt) and you get a ping
python -m clipbot posted <ID> tiktok <link>  # after you post it; reminds you to submit the link
python -m clipbot report                     # today's links per campaign, for submitting
#   once a clip has a link on every platform it was meant for, its video files are deleted (links are kept);
#   rejected clips are deleted right away. `python -m clipbot done <ID>` deletes one by hand.
```

**How many posts:** `python -m clipbot set-cap 5` = up to 5 posts per platform per day, at least 2 hours apart
(`--gap 90` for 90 minutes). Without it: 5 a day during GTA 6 launch week (to Nov 26), then the warm-up, where
`ACCOUNTS_STARTED` gives 1 post a day in week 1, 2 in week 2, and so on, up to `POSTS_PER_DAY`.
Approve in bulk with `python -m clipbot approve --top 10` (the 10 best) or `approve --all`.
Titles rotate between several wordings and each clip gets its own neon colour pair, so a busy feed doesn't look copy-pasted.
Brand-new accounts that post a lot right away tend to get throttled.

**Priority:** when several campaigns have clips ready, the easiest, best-paying one goes first.
Fixed footage beats live streams, and higher CPM, a higher max payout and remaining budget all count.
Update the budget used from the dashboard now and then: `python -m clipbot set <id> budget.used 1200`. A campaign whose budget is used up is skipped.

### Posting automatically

`AUTO_POST=true` posts (or stages) approved clips on schedule, with the campaign's hashtags, mentions, credit, `#ad`
and bio-link reminders already in the caption. Paid campaigns keep `require_review: true`: Reach and Content Rewards
ban an account after one rule break, so you approve each one. For your own-channel clips you can skip review:

```powershell
python -m clipbot set gta-any require_review false
# and in .env: AUTO_APPROVE=true
```

## Adding a campaign

New campaigns go in `campaigns.example.yaml` (ask Claude to add them from the rules page). To tweak one field for yourself: `python -m clipbot set <id> <field> <value>`, for example `set expedia-ishowspeed budget.used 1200`. Fields:

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

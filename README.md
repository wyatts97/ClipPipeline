# ClipPipeline

Send a stream VOD or video link to your Telegram bot. OpenShorts cuts it into vertical clips with
captions and a hook line, and each clip comes back to Telegram as a preview with **✅ Post / ❌ Skip**
buttons. Approved clips land in `data/approved/` with a `.json` file holding the title, hook and
descriptions. With [publishing](#publishing-postiz-posts-approved-clips) on, ✅ Post also schedules the
clip on YouTube Shorts, TikTok and Instagram Reels through a self-hosted Postiz.

[Templates](#templates-captions-music-and-sound-effects) set each clip's look: caption font and colors,
background music, and sound effects like a vine boom on the punchline.

You don't have to find the good parts yourself: `/scan <VOD>` reads the chat replay, loudness and
viewer clips, and sends the stream's top moments with **🎬 Clip it** buttons. `/watch <channel>` does
that automatically every time a stream ends.

The **web dashboard** (`http://<server-ip>:8090`) shows what the machine is doing, lets you review and
edit clips at a desk, start and retry jobs, pick moments on a scan's timeline, see what's scheduled,
free disk space, and read errors.

```
 You (Telegram) ──► Cloudflare Tunnel ──► n8n ──► clipbot ──► OpenShorts (Whisper + Gemini + ffmpeg)
       ▲                                   │         │
       └──── preview + ✅/❌ buttons ◄──────┘◄────────┘  data/pending → approved/ | rejected/
                                                    │
                                    approved clips  └──► Postiz ──► YouTube / TikTok / Instagram
```

| Service | What it does | Reachable from |
|---|---|---|
| `n8n` | Telegram trigger, buttons, the workflow you'll extend | LAN `:5678`; internet only `/webhook/*` via the tunnel |
| `clipbot` | Parses your messages, scans streams for moments, watches channels, queues jobs, downloads time ranges (yt-dlp), polls OpenShorts, makes Telegram-sized previews, moves files. Also serves the web dashboard | LAN `:8090` (dashboard, password); Docker network for n8n |
| `openshorts` | [mutonby/openshorts](https://github.com/mutonby/openshorts) (MIT) at a pinned commit: picks moments, reframes to 9:16, burns captions | Docker network only |
| `postgres` | n8n's database (and Postiz's and Temporal's when publishing is on) | Docker network only |
| `janitor` | Deletes OpenShorts' files of finished jobs when you ask ([Freeing disk space](#freeing-disk-space)) | no network |
| `postiz` + `temporal`, `postiz-redis` (optional, `publish` profile) | [Postiz](https://github.com/gitroomhq/postiz-app) (AGPL-3.0): posts approved clips on a schedule. Temporal is its scheduling engine | internet via its own tunnel hostname |
| Cloudflare Tunnel | Gives Telegram an HTTPS URL without opening ports on your router: your existing `cloudflared` on the host, or the optional `cloudflared` container | outbound only |
| `dashboard` (optional) | OpenShorts' own web UI to re-edit clips by hand | LAN `:5175` |

## What you need

- The Debian 13 server: **8 GB RAM minimum (16 GB better, and needed for publishing)**, ~25 GB free disk
  for images, models and clips (Postiz adds about 6 GB of images).
  Everything runs on the CPU (see [CPU notes](#cpu-notes)).
- A Telegram account, a **Gemini API key** (free tier is fine to start), and a **Cloudflare account
  with a domain** on it (for the tunnel).

## Setup

### 1. Get this folder onto the server

It's a git repository, so the simplest route is cloning it on the server (git skips local-only folders
like `clipbot/web/node_modules`):

```bash
sudo apt-get install -y git                                      # if git isn't there yet
git clone https://github.com/<you>/<repo>.git ~/ClipPipeline     # private repo: use a GitHub token or SSH key
cd ~/ClipPipeline
```

Without git, copy it from this Windows PC (PowerShell) after deleting `clipbot\web\node_modules` and
`dev\demo\.data`, which the server doesn't need:

```powershell
scp -r C:\Users\User\Desktop\ClipPipeline you@homeserver:~/
```

### 2. Run the guided setup

```bash
bash setup.sh
```

It asks for one thing at a time, checks each value before saving it, then builds and starts everything:

| Step | You paste or do | It checks |
|---|---|---|
| This server | nothing | Docker (offers to install it), RAM, free disk |
| Telegram bot | the token from **@BotFather** (`/newbot`) | asks Telegram for the bot's name |
| Your chat | press **Start** in your new bot | reads your chat id from that message, so only you can use the bot |
| Gemini | a key from <https://aistudio.google.com/apikey> | a test call to Gemini |
| Public address | your hostname, plus the tunnel command if this server has no cloudflared | prints the exact Cloudflare route to add ([details](#route-a-hostname-to-n8n-through-cloudflare-tunnel)) |
| Dashboard | a password, or Enter to generate one | |
| Defaults | time zone, Whisper model, optional Twitch app keys | the Twitch keys with Twitch |

Then it generates the internal secrets, writes `.env` (readable only by you), fetches OpenShorts, builds
the images (10 to 30 minutes the first time), starts the stack, and confirms that n8n connected the bot
and that Telegram can deliver to it.

Run it again any time to change a value; Enter keeps everything else. Later,
`bash setup.sh --n8n-key` adds the n8n API key that lists failed workflow runs in the dashboard.

### 3. Try it

- **Telegram:** send your bot a link with a short range, so the first run is quick:
  `https://www.youtube.com/watch?v=VIDEO_ID 2:00-12:00 clips=2`. You get "👍 Job … queued", later
  "✅ Job … finished: 2 clip(s)", then one preview per clip with buttons.
- **Dashboard:** `http://<server-ip>:8090` with the password from the setup. It's only on your LAN: the
  tunnel routes nothing but `/webhook/` to n8n.
- **n8n editor:** `http://<server-ip>:5678`. Create the owner account on your first visit; the workflow
  is already there.

## Manual setup (instead of setup.sh)

The same steps by hand, if you'd rather edit `.env` yourself. Then continue with [Try it](#3-try-it).

### Install Docker

```bash
sudo apt-get update && sudo apt-get install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"   # log out and back in afterwards
```

### Make the Telegram bot and get a Gemini key

- In Telegram, talk to **@BotFather** → `/newbot` → pick a name and a username → copy the token.
- Create a Gemini key at <https://aistudio.google.com/apikey>.

### Route a hostname to n8n through Cloudflare Tunnel

Telegram needs `https://<your hostname>/webhook/…` to reach n8n on port 5678. Pick one:

**A. The server already runs cloudflared (simplest).** In the Cloudflare dashboard open your existing
tunnel (**Zero Trust → Networks → Tunnels**) and add a **public hostname** (newer dashboards call it a
*published application route*):

| Field | Value |
|---|---|
| Subdomain / Domain | e.g. `clips` / `example.com` |
| Path | `^/webhook/` (only webhooks are exposed, not the n8n editor) |
| Service | `HTTP` → `localhost:5678` |

Use `localhost`, not `n8n`: the host's cloudflared isn't on the Docker network, it reaches n8n through
the port Docker publishes. (If you set `N8N_BIND` to the LAN IP, use that IP instead of `localhost`.)

If your tunnel is managed with a local `config.yml` instead of the dashboard, add this rule above the
final catch-all and restart cloudflared (`sudo systemctl restart cloudflared`):

```yaml
  - hostname: clips.example.com
    path: ^/webhook/
    service: http://localhost:5678
```

**B. No cloudflared on the server yet.** Create a tunnel (**Zero Trust → Networks → Tunnels → Create a
tunnel → Cloudflared**, pick **Docker**) and copy the token (the long string after `--token` in the
command it shows; you don't run that command). Add the same public hostname as in A, but with
service `HTTP` → `n8n:5678`. In `.env`, set `TUNNEL_TOKEN=` to the token and `COMPOSE_PROFILES=tunnel`,
which makes `docker compose up -d` start the bundled cloudflared container too.

### Configure .env

```bash
sh scripts/init-env.sh   # creates .env with random secrets and the data/ folders
nano .env                # set TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, PUBLIC_HOSTNAME (+ TUNNEL_TOKEN for B)
```

`PUBLIC_HOSTNAME` is the hostname from the tunnel route (`clips.example.com`, no `https://`). The script
also prints the generated `DASHBOARD_PASSWORD`; change it in `.env` to anything you like.

### Build and start

```bash
sh scripts/fetch-openshorts.sh   # clones OpenShorts at the pinned commit into vendor/
docker compose build             # first build takes a while (OpenShorts pulls in PyTorch, YOLO, Whisper)
docker compose up -d
docker compose logs n8n-import   # should end with: Imported and published 'ClipPipeline - Telegram approval'.
```

The first start of `n8n-import` creates the two credentials (**ClipBot Telegram**, **ClipBot token**)
and the workflow, and publishes it. n8n then registers the Telegram webhook by itself.

### Lock the bot to you

Send `/start` to your bot. It answers with your chat id (setup mode: `ALLOWED_CHAT_IDS` is still empty).
Put it in `.env` and restart clipbot:

```bash
nano .env                     # ALLOWED_CHAT_IDS=123456789
docker compose up -d clipbot
```

From now on the bot ignores every other chat.

## Dashboard

| Page | For |
|---|---|
| **Overview** | Clips waiting for you, what OpenShorts is rendering right now (with its latest log line), problems with a one-click fix (retry a job, scan again, resend a Telegram message), the last 7 days and recent activity. The strip at the top is always visible: OpenShorts, queue, Telegram delivery, free disk. |
| **Review** | A review station: clip list, big player, editable title, hook text and TikTok/Instagram captions. **✨ Suggest** asks Gemini for fresh hook lines; **Put this hook on the video** re-renders the clip with the new one. **A** approves, **S** skips, **J/K** move between clips. Edits are saved before a decision, and edits to approved clips update their `.json`. Same result as the Telegram buttons; whichever you use first wins. Under **Look**, switch the clip's template or drop sound effects at the playhead, then **Re-render**: the new version is sent to Telegram again with fresh ✅/❌ buttons. **Post it yourself** downloads the clip and copies its TikTok, Instagram and YouTube text (what publishing would post, credit line included). The **Approved** tab downloads all approved clips as one zip, with their `.json` files. |
| **Editor** | Review → **Open the editor**: a timeline with the waveform, cuts, captions, sound effects and zooms over a live preview ([below](#clip-editor)). |
| **Templates** | Your templates with a live caption preview, an editor (font, colors, effect, words per line, music, sound effects, credit line), and the sound and font library: upload, play, delete. |
| **Jobs** | Every job with status, source, range and clip count. Open one for its error, live OpenShorts log, clipbot log and the clips it made; retry failed or cancelled jobs, cancel running ones. **New job** takes a link plus optional in/out timecodes. |
| **Streamers** | The channels you clip and their latest VODs: scanned or not (with the moments found), live ones, **Scan** per VOD, **Scan the latest N**, and per streamer: scan new streams on/off, auto-clip the top N moments, template. |
| **Scans** | The stream as a heat timeline: the score climbs from indigo to amber, with chat, hype, loudness and viewer-clip lanes below and the ranked moments marked. Click a moment, click anywhere to drop a 2:15 window, or drag to choose your own in/out, then **Clip this range**. Each moment also has **Clip it**, **Wider ×2** and **Watch** (opens the VOD at that point). |
| **Publishing** | What's scheduled and when, per channel, with **Post now**, **Move** and **Unschedule**; clips that failed to schedule (with the reason and **Retry**); approved clips that aren't scheduled; posted clips with links. |
| **Storage** | Free disk, what takes the space, and every job with files left: **Delete files** per job or **Clean up finished jobs**. |
| **Logs & errors** | clipbot's log (errors and warnings by default, click a row for the traceback), Telegram messages that are queued or gave up (send again or delete), and failed n8n runs once `N8N_API_KEY` is set. |

Jobs and scans you start from the dashboard report to your Telegram chat like any other (the first id
in `ALLOWED_CHAT_IDS`). Signing in lasts 30 days; changing `DASHBOARD_PASSWORD` signs everyone out.

## Using it

| Message | Does |
|---|---|
| `<link>` | Clips the whole video |
| `<link> 1:02:00-1:12:30` | Downloads and clips only that range (also `62:00-72:30`, `1h2m-1h12m`) |
| `<link> clips=5` | Number of clips (1-15, default `TARGET_CLIPS`) |
| `<link> t=meme` | Use that template instead of the default ([templates](#templates-captions-music-and-sound-effects)) |
| `/scan <VOD link>` | Finds the stream's best moments and sends each with 🎬 buttons ([below](#finding-moments-scan-and-watch)) |
| `/watch <channel link>` | Scans every new VOD of that channel when the stream ends |
| `/watching`, `/unwatch <channel>` | List / stop watched channels |
| `/status` | Your recent jobs and scans |
| `/cleanup` | Offers to delete the sources and leftovers of finished jobs ([below](#freeing-disk-space)) |
| `/templates` | Lists the templates, and any sounds they're missing |
| `/help` | Usage |

Links can be anything yt-dlp handles: YouTube, Twitch VODs, Kick VODs, and many more.

Buttons: **✅ Post** moves the clip to `data/approved/YYYY-MM-DD_<title>_<id>.mp4` (plus `.json`) and,
with publishing on, schedules it. **❌ Skip** moves it to `data/rejected/`. Pressing a button twice is harmless.

Defaults live in `.env` (`TARGET_CLIPS`, `CLIP_MIN/MAX_SECONDS`, `OPENSHORTS_LAYOUTS`, `AUTO_HOOK`,
`CAPTIONS`). After editing: `docker compose up -d`.

**Layouts for streams:** `auto` lets Gemini pick per video. If you keep getting a plain center crop on
facecam + gameplay streams, try `OPENSHORTS_LAYOUTS=screencast` (screen over the presenter).

## Finding moments: /scan and /watch

`/scan https://www.twitch.tv/videos/123456789` scores the whole stream in 5-second steps, each signal
compared with that stretch of the stream's own normal level, and sends the top `SCAN_TOP` moments:

```
#1 🔥 2:30:32  (window 2:29:02–2:31:18)
chat 10× normal · 🔊 loud · ✂️ 3 viewer clips (1.2k views)
💬 KEKW ×28 · Clap ×12 · ??? ×6
▶️ https://www.twitch.tv/videos/123456789?t=2h29m2s
[🎬 Clip it] [➕ Wider ×2]
```

- **▶️** opens the VOD at the window, so you can check the moment before spending CPU on it.
- **🎬 Clip it** queues a normal range job for the window (`SCAN_CLIPS_PER_MOMENT` clips, default 1).
- **➕ Wider ×2** adds a minute on each side and asks for 2 clips, for moments with a build-up.
- The window starts `SCAN_BEFORE_SECONDS` (90) before the chat peak, because chat reacts after the moment.

What each platform gives the scan:

| Platform | Chat replay | Loudness | Viewer clips | Other |
|---|---|---|---|---|
| Twitch | ✅ (TwitchDownloaderCLI) | ✅ | with `TWITCH_CLIENT_ID/SECRET` | |
| YouTube stream replays | ✅ | ✅ | | "most replayed" when YouTube has it |
| Kick | ❌ (not available in bulk) | ✅ | ✅ | |
| Anything else yt-dlp opens | | ✅ | | |

Chat counts unique chatters (one spammer counts once) and "hype" messages: KEKW, LUL, OMEGALUL, `???`,
ALL CAPS, 😂/💀, "clip it", and channel emotes built on them (`cohhLUL`, `xqcL`). Add a streamer's own
emotes with `SCAN_HYPE_WORDS=streamerJAM,streamerHYPE`. Hello and goodbye floods and the first 3 and
last 2 minutes are ignored.

Measured on this setup's code against real VODs: a 2h51m Twitch VOD scanned in ~3 minutes (chat +
audio), a 44-minute YouTube replay in 15 s, an 11-hour Kick VOD's viewer clips in seconds (audio
adds a download of the 160p video). Scans mostly download; they use little CPU compared with an
OpenShorts job.

**`/watch https://twitch.tv/<name>`** (or `kick.com/<name>`, `youtube.com/@handle`) checks the channel
every `WATCH_INTERVAL_MINUTES`. When the newest VOD stops growing between two checks, the stream is
over, and you get "📺 … finished a 4:12:00 stream. Scanning it…" followed by the moments. A stream
that is live when you send `/watch` is scanned when it ends.

The dashboard's **Streamers** page does the same with more control. Add a channel there (or with
`/watch`), then per streamer:
- **Scan right away / Scan the latest N:** scans older VODs from its list (finished ones that
  weren't scanned yet).
- **Auto-clip the top N moments:** when a scan of that streamer finishes, its best N moments become
  jobs by themselves, in the template you pick. The clips arrive in Telegram and Review like any
  other, so the whole stream → clips step needs no taps.
- **Scan new streams:** switch it off to only list the VODs and scan by hand.

The VOD list refreshes every check (or **Check now**); each VOD shows whether it's live, scanned
(click through to its moments) or not.

Limits: sub-only Twitch VODs have no public chat (the scan says "chat unavailable" and uses the rest).
Kick's `?t=` links may open at the start of the VOD.

## Clip editor

Review → **Open the editor** (any clip still waiting for review). The preview plays the clip without
captions and draws the template's captions, your cuts and zooms on top as you go, so changes show
right away; **Render** makes the real file and sends it to Telegram again.

| Track | You can |
|---|---|
| Clip | Split at the playhead (**S**), select a part and **Delete** it, drag a part's edges to trim it. The waveform shows where the talking is. |
| Captions | The **Captions** tab lists every line: fix a misheard word or empty a line to drop it. Timing stays. **Reset** brings the transcript back. |
| Effects | **Effect here** (**E**) or the **Effects** tab: sound, volume, preview; drag the diamonds to move them. They come on top of the template's own effects. |
| Zooms | **Zoom here** (**Z**): a quick punch-in on the picture (captions stay put); drag to move or resize, set how far it zooms. |
| Hook | The **Hook** tab: the hook text (with **✨ Suggest** from Gemini), on or off for this clip, top/middle/bottom, and when it shows. Drag the hook bar to move it, its ends to make it show longer. A dashed bar is the template's timing. |

The **Length** tab has OpenShorts cut the clip again from the stream: start up to 15 s earlier, end
later, or change the framing (follow the speaker, or the whole frame with blurred bars). Your cuts,
zooms and effects move along with the new start. This needs OpenShorts' copy of the source, which it
keeps for a day (`OPENSHORTS_RETENTION_SECONDS` in `.env` to keep it longer); after that a clip can
still be cut shorter, just not longer.

Space plays, ←/→ step a frame (Shift: a second), Ctrl+Z / Ctrl+Shift+Z undo and redo, **Timeline
zoom** stretches the timeline for fine trims.

## Publishing: Postiz posts approved clips

[Postiz](https://github.com/gitroomhq/postiz-app) is a free, self-hosted social media scheduler. With it
running, every ✅ Post goes out on its own:

1. clipbot uploads the approved clip to Postiz and schedules it on every connected YouTube, TikTok and
   Instagram channel at the next free time of day (`PUBLISH_SLOTS`, default `12:00,17:00,21:00` in
   `TZ`, one clip per time). Telegram confirms with **⚡ Post now** and **✖️ Unschedule**.
2. When a post goes live, Telegram sends its link and a reminder to submit it to the campaign. TikTok
   clips land in your TikTok inbox instead (see below), and failures get a message too.
3. The dashboard's **Publishing** page shows all of it. Posts moved or deleted in Postiz's own calendar
   are picked up within a few minutes.

Captions come from OpenShorts: the YouTube title is its Shorts title and the description is the hook
plus `#shorts`; TikTok and Instagram get its descriptions. Edit them in **Review** before approving.
The podcast-style "comment X and I'll send you…" line OpenShorts sometimes writes is dropped.

### Set it up

Run `bash setup.sh --publish` on the server (again any time to change something):

| Step | You do |
|---|---|
| Hostname | Pick one for Postiz, e.g. `postiz.example.com`, and add a second route to your tunnel: Path empty, Service `HTTP` → `localhost:4007` (`postiz:5000` with the tunnel container). The platforms' login pages send you back there, and Instagram downloads videos from it. |
| Developer apps | Paste the client ID and secret of each platform's app (table below). Enter skips one for now. |
| Posting times | The daily times, e.g. `12:00,17:00,21:00`. |
| Start | Postiz, Temporal and Redis start. The first start takes a few minutes. |
| Postiz account | Open `https://postiz.example.com` and sign up (only the first account can). **Add Channel** for each platform, then **Settings → Public API**, copy the key and paste it. clipbot restarts with publishing on. |

One developer app per platform. The redirect URL uses your Postiz hostname:

| Platform | Create it at | Redirect URL | Until the platform reviews your app |
|---|---|---|---|
| YouTube | [console.cloud.google.com](https://console.cloud.google.com): new project → enable **YouTube Data API v3** → OAuth consent screen / Google Auth Platform (External) → **Audience → Test users: add the Google account you sign in to YouTube with** (otherwise Google says "Error 403: access_denied … can only be accessed by developer-approved testers") → Credentials → OAuth client ID, type Web application | `https://<postiz-host>/integrations/social/youtube` | Uploads stay **private** until Google audits the project (YouTube API Services audit form). Apply early; it takes weeks. While the app is in Testing, Google ends the login after 7 days; **Audience → Publish app** stops that (you click past an "unverified app" warning when connecting). |
| TikTok | [developers.tiktok.com](https://developers.tiktok.com) → Manage apps → Connect an app: add **Login Kit** and **Content Posting API** (turn on **Direct Post**), platform Web, terms and privacy pages on your domain. Scopes: all six of `user.info.basic`, `user.info.profile`, `user.info.stats`, `video.list`, `video.upload`, `video.publish` (Postiz refuses the channel with "Missing required permissions" otherwise). Until TikTok reviews the app, use its **Sandbox** and add your TikTok account as a target user | `https://<postiz-host>/integrations/social/tiktok` | clipbot sends clips to your **TikTok inbox** (`TIKTOK_POST_MODE=UPLOAD`): open TikTok, add the campaign's sound if it asks for one, post within 24 h. Direct posting (`DIRECT_POST`) stays private until TikTok audits the app. |
| Instagram | [developers.facebook.com](https://developers.facebook.com) → Create app → use case **Instagram API with Instagram login**; add your Instagram account (creator or business) as an Instagram tester | `https://<postiz-host>/integrations/social/instagram-standalone` | Works for your own account while the app is in development mode. |

Postiz uses the stack's Postgres (its own database) plus Temporal and Redis: about 2-3 GB of RAM in
all. Sign-up is closed after the first account (`DISABLE_REGISTRATION`). Other settings:
`PUBLISH_TO` (which platforms get clips), `YOUTUBE_VISIBILITY`, and `POSTIZ_VERSION` (pinned image).

## Freeing disk space

Each job leaves OpenShorts' copy of its source (the downloaded range, or the whole video for a plain
link) plus OpenShorts' work files. Those are the big files. OpenShorts deletes them itself after
24 hours and caps them at 15 GB of sources and 25 GB of work files. To free them sooner:

- **Telegram:** once you've decided every clip of a job, the bot asks
  "🧹 … take 1.4 GB" with **🗑 Delete files** and **Keep**. `/cleanup` offers the same for every
  finished job at once.
- **Dashboard:** **Storage** shows free disk, what takes the space, and every job with files left.
  **Delete files** per job, or **Clean up finished jobs**. The job page has the button too.

A delete removes the source, OpenShorts' work files and the skipped clips. Approved clips, clips still
waiting for review, thumbnails and the job's history stay. Clipping the same stream again downloads its
part again.

The `janitor` container does the deleting: OpenShorts' files belong to its own user, so clipbot only
hands it the job's id. If the dashboard says deletions are waiting, start it: `docker compose up -d janitor`.

## Templates: captions, music and sound effects

OpenShorts cuts and reframes the clip and writes its hook line; clipbot then gives it the captions,
hook, music and sound effects of a **template**. It keeps a copy without captions plus the clip's words until
you decide on it, so you can switch a clip to another template in Review and render it again.

Five come built in (edit, copy or delete them on the **Templates** page):

| Template | Captions | Audio |
|---|---|---|
| **Standard** (default) | Anton, yellow active word, pop: the look clips had before templates | none |
| **Hormozi** | Montserrat ExtraBold, words appear as they're said | none |
| **Meme** | Luckiest Guy, one big word at a time | `vine-boom` on up to 2 punchlines or shocks |
| **Sneaky** | soft, sentence case | `sneaky-snitch` under the voice, quieter while someone talks; adds the credit line |
| **Hype** | box behind the active word | `vine-boom` where chat exploded |

Pick one per job with `t=<name>` in Telegram (`https://twitch.tv/videos/123 1:02:00-1:12:00 t=meme`) or in
**New job**; jobs without one use the default.

**The hook line** is the text over the first seconds that makes people stop scrolling. OpenShorts' AI
writes one per clip; clipbot draws it in the template's hook look: a box behind the text, an outline,
or a plain shadow, in any font and color, at the top, middle or bottom, for the first few seconds (or
the whole clip). **Rounded corners** (0–100%) round the box, up to pill-shaped ends; on hooks of
several lines the box follows each line and the joins between lines are rounded too. Emoji are left off the video, since no caption font has them. Per clip, change the
text in Review or the editor (**✨ Suggest** writes five new ones with Gemini), and in the editor turn
it off, move it, or change when it shows. `AUTO_HOOK=false` in `.env` turns hooks off unless a clip
asks for one. Clips made before this version have OpenShorts' own hook in the video, so their hook
can't change (a re-cut in the editor's **Length** tab fixes that).

**Sounds** live in `data/sounds/`; upload them on the Templates page or copy them there. A template names
a sound by its file name without the extension, so `vine-boom.mp3` is `vine-boom`. Nothing is bundled:
- **Sneaky Snitch** is Kevin MacLeod's ([incompetech.com](https://incompetech.com)), free under
  Creative Commons BY 4.0 as long as each post credits him. The Sneaky template adds that credit line to
  every post's caption.
- For a **vine boom** and other effects, use files you're allowed to use (royalty-free sound effect
  libraries have them).
- Skip copyrighted songs: YouTube and TikTok mute or claim them. Check each campaign's rules too; some
  forbid added music or want their own sound added in the TikTok app.

**Where effects land:**

| Setting | Lands |
|---|---|
| at the start / at the end | first frame / last second |
| where chat exploded | the `/scan` chat peak (clips made from a scan moment) |
| on certain words | whenever one of your words is said, e.g. `what, no way` |
| on moments | moments Gemini finds in the clip's words, by kind: punchlines, shocks, fails, awkward, hype, sneaky (uses `GEMINI_API_KEY`, a few cents per thousand clips) |
| by hand | Review → Look → **Add at playhead** |

Give each kind of moment its own rule: a vine boom on punchlines, a sad trombone on fails, an airhorn on
hype. A rule can hold several sounds; each effect picks one (never the same twice in a row, and the same
picks every time that clip renders), so a batch of clips doesn't all sound alike. Each rule has its own
volume. Effects stay at least 1.5 seconds apart. **Fonts:** Anton, Bangers, Bebas Neue, Luckiest Guy,
Montserrat ExtraBold and Poppins ExtraBold are built in (open licenses, in `clipbot/fonts`); add your own
`.ttf`/`.otf` on the Templates page. A template only with music or effects takes seconds to render;
captions mean one more video encode, about as long as the clip on this CPU.

## CPU notes

This box has no NVIDIA GPU, and OpenShorts only accelerates on CUDA/NVENC, so transcription and
encoding run on the CPU. The weak Ryzen iGPU isn't used.

- **Use `/scan` or time ranges for streams.** A 2-minute moment or a 10-minute range is a short job;
  a 4-hour VOD sent as a plain link means downloading and transcribing all 4 hours.
- `WHISPER_MODEL=base` roughly halves transcription time; `small` (default) is more accurate.
- `OPENSHORTS_CONCURRENT_JOBS=1` (default) runs one job at a time; others wait in clipbot's queue.
- First job after a fresh start also downloads the Whisper model (kept in a volume afterwards).

## Updating

```bash
docker compose build --pull clipbot && docker compose up -d clipbot   # refreshes yt-dlp
scripts/fetch-openshorts.sh main && docker compose build openshorts && docker compose up -d openshorts
```

The second line moves OpenShorts off the pinned commit. If clipbot can no longer talk to it,
go back with `scripts/fetch-openshorts.sh` (no argument) and rebuild.

**From a version before templates:** clips now get their captions from clipbot (the Standard template
looks like before). Rebuild clipbot: `docker compose build clipbot && docker compose up -d clipbot`.

**From a version before publishing and cleanup:** `git pull`, then
`docker compose build clipbot && docker compose up -d`. That restarts Postgres once (it allows more
connections now) and starts the janitor. Publishing stays off until you run `bash setup.sh --publish`.

If an update changes `n8n/workflow.template.json`, the running n8n keeps your imported copy. Re-import
it as described under Troubleshooting (this replaces any edits you made to the workflow in n8n).

## Troubleshooting

- **Bot doesn't answer.** In n8n → *Executions*, see whether anything arrived. If nothing did, check
  Telegram's view of the webhook (replace the token):
  `curl https://api.telegram.org/bot<TOKEN>/getWebhookInfo`. The URL must be
  `https://<PUBLIC_HOSTNAME>/webhook/…/webhook` with no `last_error_message`. Then check the tunnel
  (`journalctl -u cloudflared` for the host install, `docker compose logs cloudflared` for the
  container) and `docker compose logs n8n`. From the server, `curl -s localhost:5678/healthz` should
  answer `{"status":"ok"}`.
- **"Job … failed: OpenShorts refused the job"** shows OpenShorts' reason. YouTube's "confirm you're
  not a bot" means it wants cookies: put a Netscape `cookies.txt` at `data/cookies.txt` (used for
  range downloads) and/or `YOUTUBE_COOKIES` in `.env` (used by OpenShorts).
- **"Job … failed: OpenShorts failed: …"** quotes the line in OpenShorts' log that explains it, with a
  💡 hint. Passing causes (network drop, Gemini overloaded or rate-limited) are retried once
  automatically a few minutes later (`AUTO_RETRY`). The dashboard's job page has the full log. For
  `Network is unreachable`, check that the container reaches Google:
  `docker compose exec openshorts curl -sS -o /dev/null -w '%{http_code}
' https://generativelanguage.googleapis.com/`
  (any number, even 404, means it's reachable).
- **Clip previews never arrive** but the job finished: `docker compose logs clipbot` shows whether n8n
  accepted the events (clipbot keeps retrying while n8n is down or the workflow is unpublished).
- **A scan says "chat unavailable (…)"**: the reason is in the brackets. For Twitch, try the chat tool
  by hand: `docker compose exec clipbot TwitchDownloaderCLI chatdownload --id <vod id> -o /tmp/c.json`.
- **A clip has no captions:** the job's summary in Telegram says why. "No words from OpenShorts" means
  its transcript was empty (music-only footage, or its files expired before the clip was collected).
- **A template's music or effect is missing:** Telegram and the Review page name the sound; upload a file
  with that name on the Templates page and re-render the clip.
- **Moment effects never land:** clipbot needs `GEMINI_API_KEY` (the same key as OpenShorts), then
  `docker compose up -d clipbot`.
- **Postiz doesn't come up:** `docker compose logs postiz-db temporal postiz`. `postiz-db` stops with a
  message when `POSTIZ_HOSTNAME` or `POSTIZ_JWT_SECRET` is missing (`bash setup.sh --publish` sets
  both). Temporal takes about a minute on its first start, Postiz a few minutes.
- **Postiz shows "Application error: a client-side exception"** (the browser console says `/auth` gave a
  500): its web pages are up but its backend isn't answering yet. `curl -s http://localhost:4007/api/auth/can-register`
  on the server prints `{"register":true}` once it is. If that never comes, `docker compose logs --tail=200 postiz`
  shows why (a database, Redis or Temporal error near the end); `docker compose ps` shows Postiz as
  unhealthy meanwhile. `cannot have more than 3 search attribute of type Text` there means a Temporal
  set up by an older version of this stack: `docker compose up -d` with the current compose file runs
  `temporal-attrs`, which frees the slots Temporal's sample search attributes took, then starts Postiz again.
- **Clips aren't scheduled:** the dashboard's Publishing page shows why (no channel connected, a
  setting Postiz refused, Postiz down) with a **Retry** button; Telegram says so too.
- **A post failed:** Postiz's calendar shows the platform's reason under the post. An expired login
  means reconnecting that channel in Postiz.
- **Re-import the workflow** (e.g. after breaking it): delete the workflow and both ClipBot
  credentials in n8n, then `docker compose run --rm n8n-import --force && docker compose restart n8n`.
- Logs: `docker compose logs -f clipbot openshorts`.

Known quirk: OpenShorts' built-in prompt is written for podcasts and sometimes adds a "comment X and
I'll send you the workflow" call to action to the TikTok/Instagram descriptions in the `.json`.
Titles and hooks are fine. Rewrite the descriptions before posting.

## Development

```bash
# clipbot unit + integration tests (needs ffmpeg)
pip install -r clipbot/requirements.txt pytest
pytest clipbot/tests

# End-to-end: real n8n + imported workflow + clipbot (incl. a /scan of a local fake VOD),
# fake Telegram and OpenShorts. Needs Node 24, ffmpeg and yt-dlp next to that Python.
npm install -g n8n@2.43.7
python dev/e2e/run_e2e.py

# setup.sh against fake Telegram, Gemini and Twitch APIs (wrong values, re-runs, quoting)
python dev/setup_check.py

# Dashboard (clipbot/web: React, Vite, Tailwind, shadcn/ui)
cd clipbot/web && npm install && npm run build      # what the Docker image does
python dev/demo/serve.py                            # demo data on :8090, password "demo"
npm run dev                                         # hot reload on :5173, API proxied to :8080
```

`dev/demo/serve.py` seeds jobs, clips, a scored 3-hour scan, log entries and Telegram messages, and
runs the real API with OpenShorts mocked (`--reseed` starts over). Point `npm run dev` at it with
`CLIPBOT_DEV_API=http://127.0.0.1:8090 npm run dev`.

## Next: campaign tooling

Campaigns from Content Rewards (which also runs Reach's) and Discord as cards in Telegram, their rules
(hashtags, sound, watermark, length) applied to captions and checked before posting, and daily view
counts with an earnings estimate.

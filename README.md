# ClipPipeline

Send a stream VOD or video link to your Telegram bot. OpenShorts cuts it into vertical clips with
captions and a hook line, and each clip comes back to Telegram as a preview with **✅ Post / ❌ Skip**
buttons. Approved clips land in `data/approved/` with a `.json` file holding the title, hook and
descriptions, ready for publishing.

You don't have to find the good parts yourself: `/scan <VOD>` reads the chat replay, loudness and
viewer clips, and sends the stream's top moments with **🎬 Clip it** buttons. `/watch <channel>` does
that automatically every time a stream ends.

The **web dashboard** (`http://<server-ip>:8090`) shows what the machine is doing, lets you review and
edit clips at a desk, start and retry jobs, pick moments on a scan's timeline, and read errors.

```
 You (Telegram) ──► Cloudflare Tunnel ──► n8n ──► clipbot ──► OpenShorts (Whisper + Gemini + ffmpeg)
       ▲                                   │         │
       └──── preview + ✅/❌ buttons ◄──────┘◄────────┘  data/pending → approved/ | rejected/
```

| Service | What it does | Reachable from |
|---|---|---|
| `n8n` | Telegram trigger, buttons, the workflow you'll extend | LAN `:5678`; internet only `/webhook/*` via the tunnel |
| `clipbot` | Parses your messages, scans streams for moments, watches channels, queues jobs, downloads time ranges (yt-dlp), polls OpenShorts, makes Telegram-sized previews, moves files. Also serves the web dashboard | LAN `:8090` (dashboard, password); Docker network for n8n |
| `openshorts` | [mutonby/openshorts](https://github.com/mutonby/openshorts) (MIT) at a pinned commit: picks moments, reframes to 9:16, burns captions | Docker network only |
| `postgres` | n8n's database | Docker network only |
| Cloudflare Tunnel | Gives Telegram an HTTPS URL without opening ports on your router: your existing `cloudflared` on the host, or the optional `cloudflared` container | outbound only |
| `dashboard` (optional) | OpenShorts' own web UI to re-edit clips by hand | LAN `:5175` |

## What you need

- The Debian 13 server: **8 GB RAM minimum (16 GB better)**, ~25 GB free disk for images, models and clips.
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
| **Review** | A review station: clip list, big player, editable title, hook text and TikTok/Instagram captions. **A** approves, **S** skips, **J/K** move between clips. Edits are saved before a decision, and edits to approved clips update their `.json`. Same result as the Telegram buttons; whichever you use first wins. |
| **Jobs** | Every job with status, source, range and clip count. Open one for its error, live OpenShorts log, clipbot log and the clips it made; retry failed or cancelled jobs, cancel running ones. **New job** takes a link plus optional in/out timecodes. |
| **Scans** | The stream as a heat timeline: the score climbs from indigo to amber, with chat, hype, loudness and viewer-clip lanes below and the ranked moments marked. Click a moment, click anywhere to drop a 2:15 window, or drag to choose your own in/out, then **Clip this range**. Each moment also has **Clip it**, **Wider ×2** and **Watch** (opens the VOD at that point). |
| **Logs & errors** | clipbot's log (errors and warnings by default, click a row for the traceback), Telegram messages that are queued or gave up (send again or delete), and failed n8n runs once `N8N_API_KEY` is set. |

Jobs and scans you start from the dashboard report to your Telegram chat like any other (the first id
in `ALLOWED_CHAT_IDS`). Signing in lasts 30 days; changing `DASHBOARD_PASSWORD` signs everyone out.

## Using it

| Message | Does |
|---|---|
| `<link>` | Clips the whole video |
| `<link> 1:02:00-1:12:30` | Downloads and clips only that range (also `62:00-72:30`, `1h2m-1h12m`) |
| `<link> clips=5` | Number of clips (1-15, default `TARGET_CLIPS`) |
| `/scan <VOD link>` | Finds the stream's best moments and sends each with 🎬 buttons ([below](#finding-moments-scan-and-watch)) |
| `/watch <channel link>` | Scans every new VOD of that channel when the stream ends |
| `/watching`, `/unwatch <channel>` | List / stop watched channels |
| `/status` | Your recent jobs and scans |
| `/help` | Usage |

Links can be anything yt-dlp handles: YouTube, Twitch VODs, Kick VODs, and many more.

Buttons: **✅ Post** moves the clip to `data/approved/YYYY-MM-DD_<title>_<id>.mp4` (plus `.json`),
**❌ Skip** moves it to `data/rejected/`. Pressing a button twice is harmless.

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
that is live when you send `/watch` is scanned when it ends; older VODs are not (scan those by hand).

Limits: sub-only Twitch VODs have no public chat (the scan says "chat unavailable" and uses the rest).
Kick's `?t=` links may open at the start of the VOD.

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
- **Clip previews never arrive** but the job finished: `docker compose logs clipbot` shows whether n8n
  accepted the events (clipbot keeps retrying while n8n is down or the workflow is unpublished).
- **A scan says "chat unavailable (…)"**: the reason is in the brackets. For Twitch, try the chat tool
  by hand: `docker compose exec clipbot TwitchDownloaderCLI chatdownload --id <vod id> -o /tmp/c.json`.
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

## Next: publishing

Approved clips and their metadata are in `data/approved/`. Publishing (YouTube Shorts, TikTok draft
upload, Instagram via Postiz or Upload-Post) hooks into the workflow after **Send decision reply**.
Apply for the TikTok developer app and the Google Cloud API audit early; both take weeks.

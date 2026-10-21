#!/usr/bin/env bash
# ClipPipeline guided setup.
#
# Asks for each key once, checks that it works, writes .env (generating the internal secrets),
# then builds and starts the stack and checks that Telegram can reach it.
# Safe to re-run any time: Enter keeps the current value.
#
#   bash setup.sh             full guided setup
#   bash setup.sh --env-only  only write .env (no Docker, no build)
#   bash setup.sh --n8n-key   add the n8n API key later (failed n8n runs in the dashboard)
#   bash setup.sh --publish   set up Postiz, which posts approved clips (add --env-only to skip Docker)
set -euo pipefail

cd "$(dirname "$0")"
ENV_FILE=.env
TELEGRAM_API=${TELEGRAM_API:-https://api.telegram.org}
GEMINI_API=${GEMINI_API:-https://generativelanguage.googleapis.com}
TWITCH_ID_API=${TWITCH_ID_API:-https://id.twitch.tv}
N8N_LOCAL=${N8N_LOCAL:-http://localhost:5678}
POSTIZ_LOCAL=${POSTIZ_LOCAL:-http://localhost:4007}
DOCKER=(docker)
BOT_USERNAME=""
ENV_ONLY=""

# ---- output ------------------------------------------------------------------------------
if [[ -t 1 && -z ${NO_COLOR:-} ]]; then
  B=$'\e[1m' D=$'\e[2m' G=$'\e[32m' Y=$'\e[33m' R=$'\e[31m' N=$'\e[0m'
else
  B="" D="" G="" Y="" R="" N=""
fi
step() { printf '\n%s%s%s\n' "$B" "$1" "$N"; }
say()  { printf '%s\n' "$*"; }
hint() { printf '%s%s%s\n' "$D" "$*" "$N"; }
ok()   { printf '%s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '%s!%s %s\n' "$Y" "$N" "$*"; }
fail() { printf '%s✗%s %s\n' "$R" "$N" "$*"; }
die()  { fail "$*" >&2; exit 1; }

# ---- small helpers -------------------------------------------------------------------------
trim() {
  local s=$1
  s="${s#"${s%%[![:space:]]*}"}"
  printf '%s' "${s%"${s##*[![:space:]]}"}"
}
mask() {
  local v=$1
  if (( ${#v} <= 10 )); then printf '••••'; else printf '%s…%s' "${v:0:4}" "${v: -4}"; fi
}
rand_hex() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
http() { curl -sS --max-time 20 "$@" 2>/dev/null || true; }
json_str() { grep -Eo "\"$1\" *: *\"[^\"]*\"" | head -n1 | sed -E 's/^"[^"]*" *: *"//; s/"$//' || true; }
json_num() { grep -Eo "\"$1\" *: *-?[0-9]+" | tail -n1 | grep -Eo -- '-?[0-9]+$' || true; }
json_ok() { grep -Eq '"ok" *: *true' <<<"$1"; }
lan_ip() {
  local ip
  ip=$(hostname -I 2>/dev/null | awk '{print $1}' || true)
  printf '%s' "${ip:-<server-ip>}"
}

# ---- prompts (answers land in $ANSWER) -----------------------------------------------------
ask() {  # ask "Question" [default]
  local q=$1 def=${2:-} a=""
  if [[ -n $def ]]; then q+=" [$def]"; fi
  read -rp "$q: " a || [[ -n $a || -n $def ]] || die "Input ended before setup finished."
  a=$(trim "$a")
  ANSWER=${a:-$def}
}
ask_secret() {  # ask_secret "Question" [current]: hidden; Enter keeps the current value
  local q=$1 cur=${2:-} a=""
  [[ -n $cur ]] && q+=" (Enter keeps $(mask "$cur"))"
  read -rsp "$q: " a || [[ -n $a || -n $cur ]] || die "Input ended before setup finished."
  echo
  a=$(trim "$a")
  ANSWER=${a:-$cur}
}
yes_no() {  # yes_no "Question" Y|N
  local q=$1 def=${2:-Y} a=""
  if [[ $def == Y ]]; then read -rp "$q [Y/n]: " a || true; else read -rp "$q [y/N]: " a || true; fi
  a=$(trim "${a:-$def}")
  [[ $a =~ ^[Yy] ]]
}

# ---- .env ------------------------------------------------------------------------------------
env_get() {  # current value of KEY (quotes removed), empty if unset
  [[ -f $ENV_FILE ]] || return 0
  local line
  line=$(grep -E "^$1=" "$ENV_FILE" | tail -n1 || true)
  line=${line#*=}
  if [[ $line == \'*\' || $line == \"*\" ]]; then line=${line:1:${#line}-2}; fi
  printf '%s' "$line"
}
env_quote() {  # plain when simple, single-quoted otherwise (Compose reads '...' literally)
  if [[ $1 =~ ^[A-Za-z0-9_.:/@+,=-]*$ ]]; then printf '%s' "$1"; else printf "'%s'" "$1"; fi
}
env_set() {  # replace KEY=..., or its "# KEY=" placeholder, or append it
  local tmp
  tmp=$(mktemp)
  KEY=$1 VAL=$(env_quote "$2") awk '
    BEGIN { k = ENVIRON["KEY"]; v = ENVIRON["VAL"]; done = 0 }
    !done && ($0 ~ ("^" k "=") || $0 ~ ("^# *" k "=")) { print k "=" v; done = 1; next }
    { print }
    END { if (!done) print k "=" v }' "$ENV_FILE" > "$tmp"
  cat "$tmp" > "$ENV_FILE"
  rm -f "$tmp"
}
profile_set() {  # profile_set NAME on|off: edit COMPOSE_PROFILES, keeping the other profiles
  local list=() current=() p
  IFS=',' read -ra current <<<"$(env_get COMPOSE_PROFILES)"
  for p in "${current[@]}"; do
    p=$(trim "$p")
    [[ -n $p && $p != "$1" ]] && list+=("$p")
  done
  [[ $2 == on ]] && list+=("$1")
  env_set COMPOSE_PROFILES "$(IFS=,; printf '%s' "${list[*]}")"
}

# ---- 0. this server ----------------------------------------------------------------------------
install_docker() {
  say "Installing Docker Engine from Docker's official repository…"
  sudo apt-get update
  sudo apt-get install -y ca-certificates curl
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
  # shellcheck source=/dev/null
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo usermod -aG docker "$USER"
  ok "Docker installed. You're in the docker group from your next login; this run uses sudo."
  DOCKER=(sudo docker)
}

pick_docker() {  # docker, or sudo docker when this user isn't in the docker group yet
  if docker info >/dev/null 2>&1; then DOCKER=(docker)
  elif sudo docker info >/dev/null 2>&1; then DOCKER=(sudo docker)
  else die "Docker isn't running. Try: sudo systemctl start docker"
  fi
}

preflight() {
  step "Checking this server"
  local missing=() tool
  for tool in curl git; do command -v "$tool" >/dev/null || missing+=("$tool"); done
  if (( ${#missing[@]} )); then
    if yes_no "Install ${missing[*]} with apt (needs sudo)?" Y; then
      sudo apt-get update && sudo apt-get install -y "${missing[@]}"
    else
      die "${missing[*]} is needed. Install it and run this again."
    fi
  fi
  if [[ -n $ENV_ONLY ]]; then return 0; fi

  if ! command -v docker >/dev/null; then
    warn "Docker isn't installed."
    if yes_no "Install it now?" Y; then install_docker; else die "Install Docker (README: Manual setup), then run this again."; fi
  fi
  pick_docker
  [[ ${DOCKER[0]} == sudo ]] && {
    warn "Your user can't use Docker yet, so this run uses sudo. To fix it for good:"
    hint "  sudo usermod -aG docker $USER   then log out and back in"
  }
  "${DOCKER[@]}" compose version >/dev/null 2>&1 || die "The Docker Compose plugin is missing: sudo apt-get install docker-compose-plugin"
  ok "Docker $("${DOCKER[@]}" version --format '{{.Server.Version}}' 2>/dev/null || echo ready)"

  local ram disk
  ram=$(awk '/MemTotal/ {printf "%d", $2 / 1024 / 1024 + 0.5}' /proc/meminfo 2>/dev/null || echo 0)
  disk=$(df -Pk . | awk 'NR == 2 {printf "%d", $4 / 1024 / 1024}')
  if (( ram > 0 && ram < 8 )); then warn "${ram} GB RAM: OpenShorts wants 8 GB or more; long videos may fail."; else ok "${ram} GB RAM"; fi
  if (( disk < 25 )); then warn "${disk} GB free here: the images, models and clips need about 25 GB."; else ok "${disk} GB free disk"; fi
}

# ---- 1. Telegram bot -----------------------------------------------------------------------------
setup_telegram() {
  step "1/6  Telegram bot"
  local cur me token
  cur=$(env_get TELEGRAM_BOT_TOKEN)
  if [[ -z $cur ]]; then
    say "In Telegram, open ${B}@BotFather${N}, send ${B}/newbot${N}, then pick a name and a username ending in \"bot\"."
    say "It answers with a token like 123456789:AAH4… Copy it."
  fi
  while true; do
    ask_secret "Bot token" "$cur"
    token=$ANSWER
    if [[ -z $token ]]; then fail "The bot token is required."; continue; fi
    token=${token#bot}
    if [[ ! $token =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]]; then
      fail "That doesn't look like a bot token (numbers, a colon, then about 35 letters)."; cur=""; continue
    fi
    me=$(http "$TELEGRAM_API/bot$token/getMe")
    if json_ok "$me"; then
      BOT_USERNAME=$(json_str username <<<"$me")
      ok "Token works: @$BOT_USERNAME"
      break
    fi
    if [[ -z $me ]]; then
      warn "Couldn't reach Telegram to check it."
      if yes_no "Save it unchecked?" N; then break; fi
    else
      fail "Telegram refused it: $(json_str description <<<"$me")"
    fi
    cur=""
  done
  local old
  old=$(env_get TELEGRAM_BOT_TOKEN)
  if [[ -n $old && $old != "$token" ]] && docker_volume_exists n8n_data; then
    warn "n8n already has the old token. In the n8n editor, open Credentials, then \"ClipBot Telegram\", and paste the new one."
  fi
  env_set TELEGRAM_BOT_TOKEN "$token"
}

docker_volume_exists() {
  [[ -z $ENV_ONLY ]] && "${DOCKER[@]}" volume inspect "clippipeline_$1" >/dev/null 2>&1
}

# ---- 2. your chat id -----------------------------------------------------------------------------
manual_chat_id() {
  while true; do
    ask "Your Telegram chat id (digits; the bot tells you on /start)" "$(env_get ALLOWED_CHAT_IDS)"
    if [[ $ANSWER =~ ^-?[0-9]+(,-?[0-9]+)*$ ]]; then env_set ALLOWED_CHAT_IDS "$ANSWER"; return 0; fi
    if [[ -z $ANSWER ]]; then
      warn "Left empty: the bot will reply to /start with your id. Add it later by running this again."
      return 0
    fi
    fail "Chat ids are numbers, like 123456789."
  done
}

setup_chat_id() {
  step "2/6  Your Telegram chat"
  hint "The bot only answers this chat and ignores everyone else."
  local cur token info updates last offset=0 chat name deadline
  cur=$(env_get ALLOWED_CHAT_IDS)
  if [[ -n $cur ]] && ! yes_no "Allowed chat is $cur. Detect it again?" N; then return 0; fi
  token=$(env_get TELEGRAM_BOT_TOKEN)
  info=$(http "$TELEGRAM_API/bot$token/getWebhookInfo")
  if grep -Eq '"url" *: *"http' <<<"$info"; then
    hint "The bot is already connected to n8n, so it can't be detected from here."
    manual_chat_id
    return 0
  fi
  last=$(http "$TELEGRAM_API/bot$token/getUpdates?offset=-1" | json_num update_id)
  [[ -n $last ]] && offset=$((last + 1))
  say "Open ${B}https://t.me/${BOT_USERNAME:-your_bot}${N} on your phone and press ${B}Start${N} (or send it any message)."
  say "Waiting for it (up to 3 minutes)…"
  deadline=$((SECONDS + 180))
  while (( SECONDS < deadline )); do
    updates=$(http --max-time 35 "$TELEGRAM_API/bot$token/getUpdates?offset=$offset&timeout=25")
    chat=$(grep -Eo '"chat" *: *\{ *"id" *: *-?[0-9]+' <<<"$updates" | head -n1 | grep -Eo -- '-?[0-9]+$' || true)
    if [[ -n $chat ]]; then
      name=$(json_str first_name <<<"$updates")
      ok "Got it: ${name:-your chat} ($chat)"
      last=$(json_num update_id <<<"$updates")
      [[ -n $last ]] && http "$TELEGRAM_API/bot$token/getUpdates?offset=$((last + 1))&timeout=0" >/dev/null
      env_set ALLOWED_CHAT_IDS "$chat"
      return 0
    fi
    [[ -z $updates ]] && sleep 3
  done
  warn "No message arrived."
  manual_chat_id
}

# ---- 3. Gemini -----------------------------------------------------------------------------------
setup_gemini() {
  step "3/6  Gemini API key"
  hint "OpenShorts uses it to pick the moments and the layout of each clip. The free tier is enough to start."
  local cur key code
  cur=$(env_get GEMINI_API_KEY)
  [[ -z $cur ]] && say "Create one at ${B}https://aistudio.google.com/apikey${N} (Create API key) and copy it."
  while true; do
    ask_secret "Gemini API key" "$cur"
    key=$ANSWER
    if [[ -z $key ]]; then fail "The Gemini key is required."; continue; fi
    code=$(http -o /dev/null -w '%{http_code}' -H "x-goog-api-key: $key" "$GEMINI_API/v1beta/models?pageSize=1")
    case $code in
      200) ok "Key works"; break ;;
      400|401|403) fail "Google refused that key (HTTP $code). Copy it again from AI Studio." ;;
      *)
        warn "Couldn't check it with Google (HTTP ${code:-no answer})."
        if yes_no "Save it unchecked?" N; then break; fi ;;
    esac
    cur=""
  done
  env_set GEMINI_API_KEY "$key"
}

# ---- 4. public address -----------------------------------------------------------------------------
ask_hostname() {
  local h
  while true; do
    ask "Hostname for Telegram, e.g. clips.example.com" "$(env_get PUBLIC_HOSTNAME)"
    h=${ANSWER#http://}
    h=${h#https://}
    h=${h%%/*}
    h=${h,,}
    if [[ $h =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$ ]]; then
      env_set PUBLIC_HOSTNAME "$h"
      return 0
    fi
    fail "That isn't a hostname. Use a (sub)domain on Cloudflare, like clips.example.com"
  done
}

route_instructions() {  # $1 = service
  local h
  h=$(env_get PUBLIC_HOSTNAME)
  say "In the Cloudflare dashboard, open ${B}Zero Trust → Networks → Tunnels${N} → your tunnel →"
  say "${B}Public hostname${N} (or Published application routes) → Add, and enter:"
  say "  Subdomain + Domain:  ${B}${h}${N}"
  say "  Path:                ${B}^/webhook/${N}      (only webhooks get out; the n8n editor stays private)"
  say "  Service:             ${B}HTTP${N}  ${B}$1${N}"
}

setup_tunnel() {
  step "4/6  Public address for Telegram"
  hint "Telegram needs an https address that reaches n8n. A Cloudflare Tunnel gives you one without opening router ports."
  local have=N token cur
  if systemctl is-active --quiet cloudflared 2>/dev/null || pgrep -x cloudflared >/dev/null 2>&1; then
    have=Y
    ok "cloudflared is already running on this server."
  fi
  if yes_no "Use the cloudflared already installed on this server?" "$have"; then
    profile_set tunnel off
    ask_hostname
    route_instructions "localhost:5678"
    hint "localhost, because your cloudflared runs outside Docker and reaches n8n through its published port."
  else
    say "Create a tunnel: ${B}Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared${N},"
    say "name it clippipeline and pick ${B}Docker${N}. Copy the command it shows (or just the token after --token)."
    cur=$(env_get TUNNEL_TOKEN)
    while true; do
      ask_secret "Paste the command or the token" "$cur"
      token=${ANSWER##*--token }
      token=$(trim "${token%% *}")
      if [[ $token =~ ^[A-Za-z0-9+/=_-]{40,}$ ]]; then break; fi
      fail "That doesn't look like a tunnel token (a long string starting with eyJ)."
      cur=""
    done
    env_set TUNNEL_TOKEN "$token"
    profile_set tunnel on
    ask_hostname
    route_instructions "n8n:5678"
    hint "n8n:5678, because this tunnel runs as a container next to n8n."
  fi
  if getent hosts "$(env_get PUBLIC_HOSTNAME)" >/dev/null 2>&1; then
    ok "$(env_get PUBLIC_HOSTNAME) resolves"
  else
    hint "$(env_get PUBLIC_HOSTNAME) doesn't resolve yet; Cloudflare creates it when you save the route."
  fi
  yes_no "Added the route? (No is fine; add it before testing the bot)" Y || true
}

# ---- 5. dashboard password ------------------------------------------------------------------------
setup_dashboard() {
  step "5/6  Dashboard password"
  hint "For the web dashboard at http://$(lan_ip):$(env_get DASHBOARD_PORT | grep . || echo 8090), reachable only on your network."
  local p p2
  if [[ -n $(env_get DASHBOARD_PASSWORD) ]] && yes_no "Keep the current password?" Y; then return 0; fi
  say "Type a password, or press Enter to generate one."
  while true; do
    p=""
    read -rsp "Password: " p || [[ -n $p ]] || die "Input ended before setup finished."
    p=${p%$'\r'}  # pasted from Windows
    echo
    if [[ -z $p ]]; then
      p=$(rand_hex 10)
      ok "Generated: ${B}$p${N}  (save it in your password manager)"
      break
    fi
    if (( ${#p} < 8 )); then fail "Use at least 8 characters."; continue; fi
    if [[ $p == *"'"* ]]; then fail "Single quotes can't be stored in .env; pick another."; continue; fi
    read -rsp "Again: " p2 || true
    p2=${p2%$'\r'}
    echo
    if [[ $p == "$p2" ]]; then ok "Password set"; break; fi
    fail "They don't match. Try again."
  done
  env_set DASHBOARD_PASSWORD "$p"
}

# ---- 6. extras -------------------------------------------------------------------------------------
setup_extras() {
  step "6/6  A few defaults"
  local tz cur id secret code
  tz=$(timedatectl show -p Timezone --value 2>/dev/null || cat /etc/timezone 2>/dev/null || echo UTC)
  cur=$(env_get TZ)
  [[ -n $cur && $cur != UTC ]] && tz=$cur
  ask "Time zone" "${tz:-UTC}"
  env_set TZ "$ANSWER"

  hint "Transcription runs on the CPU: small is more accurate, base is about twice as fast."
  cur=$(env_get WHISPER_MODEL)
  while true; do
    ask "Whisper model (small or base)" "${cur:-small}"
    if [[ $ANSWER == small || $ANSWER == base ]]; then env_set WHISPER_MODEL "$ANSWER"; break; fi
    fail "Type small or base."
  done

  hint "Optional: a free Twitch app (dev.twitch.tv/console/apps) adds viewer clips to scans of Twitch VODs."
  if [[ -n $(env_get TWITCH_CLIENT_ID) ]]; then
    ok "Twitch keys are set."
    yes_no "Replace them?" N || return 0
  elif ! yes_no "Add Twitch API keys now?" N; then
    return 0
  fi
  while true; do
    ask "Twitch Client ID"
    id=$ANSWER
    ask_secret "Twitch Client Secret"
    secret=$ANSWER
    code=$(http -o /dev/null -w '%{http_code}' -X POST "$TWITCH_ID_API/oauth2/token" \
      -d "client_id=$id" -d "client_secret=$secret" -d grant_type=client_credentials)
    if [[ $code == 200 ]]; then ok "Twitch keys work"; break; fi
    fail "Twitch refused them (HTTP ${code:-no answer})."
    yes_no "Try again?" Y || return 0
  done
  env_set TWITCH_CLIENT_ID "$id"
  env_set TWITCH_CLIENT_SECRET "$secret"
}

# ---- generated values --------------------------------------------------------------------------------
setup_secrets() {
  local key
  for key in POSTGRES_PASSWORD N8N_ENCRYPTION_KEY CLIPBOT_TOKEN POSTIZ_JWT_SECRET; do
    # Never replaced once set: a new N8N_ENCRYPTION_KEY would lock n8n out of its saved credentials.
    if [[ -z $(env_get "$key") ]]; then env_set "$key" "$(rand_hex 32)"; ok "Generated $key"; fi
  done
  env_set PUID "$(id -u)"
  env_set PGID "$(id -g)"
  mkdir -p data/pending data/previews data/approved data/rejected data/thumbs data/purge
}

summary() {
  step "Your settings (saved in .env, readable only by you)"
  local tunnel="cloudflared on this server (route → localhost:5678)"
  [[ $(env_get COMPOSE_PROFILES) == *tunnel* ]] && tunnel="tunnel container (route → n8n:5678)"
  printf '  %-17s %s\n' \
    "Telegram bot" "@${BOT_USERNAME:-?}" \
    "Allowed chat" "$(env_get ALLOWED_CHAT_IDS | grep . || echo 'not set yet')" \
    "Gemini key" "$(mask "$(env_get GEMINI_API_KEY)")" \
    "Telegram calls" "https://$(env_get PUBLIC_HOSTNAME)/webhook/…" \
    "Tunnel" "$tunnel" \
    "Dashboard" "http://$(lan_ip):$(env_get DASHBOARD_PORT | grep . || echo 8090)" \
    "Time zone" "$(env_get TZ)" \
    "Whisper model" "$(env_get WHISPER_MODEL)" \
    "Twitch clips" "$( [[ -n $(env_get TWITCH_CLIENT_ID) ]] && echo on || echo off)" \
    "Publishing" "$( [[ -n $(env_get POSTIZ_API_KEY) ]] && echo "Postiz at https://$(env_get POSTIZ_HOSTNAME)" || echo 'off (bash setup.sh --publish)')"
}

# ---- build, start, verify -----------------------------------------------------------------------------
wait_for() {  # wait_for "what" seconds command...
  local what=$1 secs=$2
  shift 2
  local deadline=$((SECONDS + secs))
  until "$@" >/dev/null 2>&1; do
    if (( SECONDS >= deadline )); then fail "$what didn't come up within ${secs}s."; return 1; fi
    sleep 3
  done
  ok "$what is up"
}

check_telegram() {
  local token host info url err errdate deadline
  token=$(env_get TELEGRAM_BOT_TOKEN)
  host=$(env_get PUBLIC_HOSTNAME)
  deadline=$((SECONDS + 90))
  while (( SECONDS < deadline )); do
    info=$(http "$TELEGRAM_API/bot$token/getWebhookInfo")
    url=$(json_str url <<<"$info")
    [[ $url == "https://$host/"* ]] && break
    sleep 5
  done
  if [[ $url != "https://$host/"* ]]; then
    fail "n8n hasn't connected the bot yet. Check: ${DOCKER[*]} compose logs n8n-import n8n"
    return 0
  fi
  ok "n8n connected the bot to $url"

  local since
  since=$(date +%s)
  say "Now send ${B}/help${N} to @${BOT_USERNAME:-your_bot} in Telegram, then press Enter here."
  read -r _ || true
  sleep 5
  info=$(http "$TELEGRAM_API/bot$token/getWebhookInfo")
  err=$(json_str last_error_message <<<"$info")
  errdate=$(json_num last_error_date <<<"$info")
  if [[ -n $err && ${errdate:-0} -ge $since ]]; then
    fail "Telegram can't deliver to https://$host: $err"
    case $err in
      *404*) hint "The route's Path or Service is off: Path ^/webhook/, Service HTTP $(tunnel_service)." ;;
      *403*) hint "Cloudflare blocked it: turn off Access or bot protection for /webhook/ on this hostname." ;;
      *) hint "Check that the tunnel is running and the route exists (see the Cloudflare step above)." ;;
    esac
    hint "Fix it, then send /help again; Telegram retries for a while on its own."
  else
    ok "Telegram delivered without errors. The bot should have answered with its help text."
  fi
}

tunnel_service() {
  if [[ $(env_get COMPOSE_PROFILES) == *tunnel* ]]; then echo n8n:5678; else echo localhost:5678; fi
}

start_stack() {
  step "Build and start"
  if ! yes_no "Build and start everything now? The first build takes 10 to 30 minutes." Y; then
    say "Later, run this again (Enter through the questions), or:"
    hint "  sh scripts/fetch-openshorts.sh && docker compose build && docker compose up -d"
    return 0
  fi
  sh scripts/fetch-openshorts.sh
  "${DOCKER[@]}" compose build
  "${DOCKER[@]}" compose up -d
  local port
  port=$(env_get DASHBOARD_PORT | grep . || echo 8090)
  wait_for "clipbot and the dashboard" 180 curl -sf "http://localhost:$port/health" || true
  wait_for "n8n" 240 curl -sf "$N8N_LOCAL/healthz" || true
  if "${DOCKER[@]}" compose logs n8n-import 2>/dev/null | grep -q "Imported and published\|already imported"; then
    ok "Telegram workflow imported and published in n8n"
  else
    warn "Couldn't confirm the workflow import. Check: ${DOCKER[*]} compose logs n8n-import"
  fi
  check_telegram
}

# ---- n8n API key (after the first start) --------------------------------------------------------------
setup_n8n_key() {
  step "n8n API key"
  say "In the n8n editor (http://$(lan_ip):5678) open ${B}Settings → n8n API → Create an API key${N}, then copy it."
  local key code
  while true; do
    ask_secret "n8n API key" "$(env_get N8N_API_KEY)"
    key=$ANSWER
    [[ -z $key ]] && return 0
    code=$(http -o /dev/null -w '%{http_code}' -H "X-N8N-API-KEY: $key" "$N8N_LOCAL/api/v1/workflows?limit=1")
    if [[ $code == 200 ]]; then ok "Key works"; break; fi
    fail "n8n refused it (HTTP ${code:-no answer})."
  done
  env_set N8N_API_KEY "$key"
  if [[ -z $ENV_ONLY ]]; then
    pick_docker
    "${DOCKER[@]}" compose up -d clipbot && ok "clipbot restarted; failed n8n runs now show under Logs & errors"
  fi
}

# ---- publishing through Postiz (bash setup.sh --publish) ---------------------------------------------
ask_postiz_hostname() {
  local h
  while true; do
    ask "Hostname for Postiz, e.g. postiz.example.com" "$(env_get POSTIZ_HOSTNAME)"
    h=${ANSWER#http://}
    h=${h#https://}
    h=${h%%/*}
    h=${h,,}
    if [[ $h == "$(env_get PUBLIC_HOSTNAME)" ]]; then
      fail "Use a different hostname than Telegram's ($h): Postiz needs the whole hostname."
    elif [[ $h =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$ ]]; then
      env_set POSTIZ_HOSTNAME "$h"
      return 0
    else
      fail "That isn't a hostname. Use a (sub)domain on Cloudflare, like postiz.example.com"
    fi
  done
}

ask_app_keys() {  # ask_app_keys "YouTube" ID_KEY SECRET_KEY "where to get them" "redirect path"
  local name=$1 id_key=$2 secret_key=$3 where=$4 path=$5
  say ""
  say "${B}$name${N}: $where"
  say "  Redirect URL to enter there: ${B}https://$(env_get POSTIZ_HOSTNAME)$path${N}"
  if [[ -n $(env_get "$id_key") ]] && ! yes_no "  $name keys are set. Replace them?" N; then return 0; fi
  ask "  $name client ID (Enter skips for now)" "$(env_get "$id_key")"
  [[ -z $ANSWER ]] && return 0
  env_set "$id_key" "$ANSWER"
  ask_secret "  $name client secret" "$(env_get "$secret_key")"
  env_set "$secret_key" "$ANSWER"
}

# Its backend, not just the web pages: those answer first, and /auth fails with a 500 until the backend does.
postiz_up() { curl -sf -o /dev/null "$POSTIZ_LOCAL/api/auth/can-register"; }

setup_postiz_key() {
  local host key code
  host=$(env_get POSTIZ_HOSTNAME)
  say ""
  say "In Postiz (${B}https://$host${N}):"
  say "  1. Sign up. Only the first account can, so that's yours."
  say "  2. ${B}Add Channel${N} for YouTube, TikTok and Instagram (needs the keys above)."
  say "  3. ${B}Settings → Public API${N}: copy the API key."
  while true; do
    ask_secret "Postiz API key (Enter skips; run bash setup.sh --publish again later)" "$(env_get POSTIZ_API_KEY)"
    key=$ANSWER
    [[ -z $key ]] && return 0
    code=$(http -o /dev/null -w '%{http_code}' -H "Authorization: $key" "$POSTIZ_LOCAL/api/public/v1/is-connected")
    if [[ $code == 200 ]]; then ok "Postiz accepted the key"; break; fi
    fail "Postiz refused it (HTTP ${code:-no answer})."
  done
  env_set POSTIZ_API_KEY "$key"
}

setup_publish() {
  step "Publishing: Postiz posts approved clips to YouTube, TikTok and Instagram"
  hint "Postiz runs as 4 more containers (about 2-3 GB of RAM) and needs its own public hostname:"
  hint "the platforms' login pages send you back to it, and Instagram downloads the video from it."
  [[ -n $(env_get PUBLIC_HOSTNAME) ]] || die "Run bash setup.sh first."
  ask_postiz_hostname
  [[ -n $(env_get POSTIZ_JWT_SECRET) ]] || { env_set POSTIZ_JWT_SECRET "$(rand_hex 32)"; ok "Generated POSTIZ_JWT_SECRET"; }

  local service="localhost:4007"
  [[ $(env_get COMPOSE_PROFILES) == *tunnel* ]] && service="postiz:5000"
  say "Add a second route to your tunnel (${B}Zero Trust → Networks → Tunnels${N} → your tunnel → Public hostname → Add):"
  say "  Subdomain + Domain:  ${B}$(env_get POSTIZ_HOSTNAME)${N}"
  say "  Path:                (empty: Postiz needs the whole hostname)"
  say "  Service:             ${B}HTTP${N}  ${B}$service${N}"
  yes_no "Added the route? (No is fine; add it before signing in to Postiz)" Y || true

  step "Developer apps (one per platform; Enter skips any of them for now)"
  hint "Until a platform reviews your app, posts stay private (YouTube) or go to your TikTok drafts. README → Publishing."
  ask_app_keys "YouTube" YOUTUBE_CLIENT_ID YOUTUBE_CLIENT_SECRET \
    "console.cloud.google.com → new project → enable YouTube Data API v3 → OAuth client (Web application)" \
    "/integrations/social/youtube"
  ask_app_keys "TikTok" TIKTOK_CLIENT_ID TIKTOK_CLIENT_SECRET \
    "developers.tiktok.com → Manage apps → Connect an app; add Login Kit + Content Posting API" \
    "/integrations/social/tiktok"
  ask_app_keys "Instagram" INSTAGRAM_APP_ID INSTAGRAM_APP_SECRET \
    "developers.facebook.com → Create app → Instagram API with Instagram Login (needs a creator/business account)" \
    "/integrations/social/instagram-standalone"

  step "Posting times"
  hint "Approved clips go into the next free time of day, one clip per time, in $(env_get TZ | grep . || echo UTC)."
  ask "Daily posting times" "$(env_get PUBLISH_SLOTS | grep . || echo 12:00,17:00,21:00)"
  env_set PUBLISH_SLOTS "${ANSWER// /}"

  profile_set publish on
  if [[ -z $ENV_ONLY ]]; then
    pick_docker
    step "Start Postiz"
    "${DOCKER[@]}" compose up -d
    wait_for "Postiz (its first start takes a few minutes)" 600 postiz_up || {
      hint "Check: ${DOCKER[*]} compose logs postiz-db temporal postiz"; return 0; }
  fi
  setup_postiz_key
  if [[ -z $ENV_ONLY && -n $(env_get POSTIZ_API_KEY) ]]; then
    "${DOCKER[@]}" compose up -d clipbot && ok "clipbot restarted: ✅ Post now schedules clips"
  fi
  say ""
  hint "The dashboard's Publishing page shows what's scheduled. Re-run bash setup.sh --publish to change any of this."
}

usage() {
  sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
}

finish() {
  step "Done"
  local ip port
  ip=$(lan_ip)
  port=$(env_get DASHBOARD_PORT | grep . || echo 8090)
  say "  Dashboard    http://$ip:$port   (your dashboard password)"
  say "  n8n editor   http://$ip:5678    (create the owner account on your first visit)"
  say "  Telegram     send @${BOT_USERNAME:-your_bot} a link, e.g. https://www.twitch.tv/videos/123 1:02:00-1:12:00"
  if [[ -z $(env_get ALLOWED_CHAT_IDS) ]]; then
    warn "No chat is allowed yet: send /start to the bot, then run this again to add your id."
  fi
  hint "Run bash setup.sh again any time to change a key; Enter keeps everything else."
  hint "Later, for failed n8n runs in the dashboard: bash setup.sh --n8n-key"
  hint "To post approved clips to YouTube, TikTok and Instagram: bash setup.sh --publish"
}

main() {
  local mode=full arg
  for arg in "$@"; do
    case $arg in
      -h|--help) usage; return 0 ;;
      --env-only) ENV_ONLY=1 ;;
      --n8n-key) mode=n8n ;;
      --publish) mode=publish ;;
      *) usage; return 2 ;;
    esac
  done
  case $mode in
    n8n) [[ -f $ENV_FILE ]] || die "Run bash setup.sh first."; setup_n8n_key; return 0 ;;
    publish) [[ -f $ENV_FILE ]] || die "Run bash setup.sh first."; setup_publish; return 0 ;;
  esac
  [[ -t 0 ]] || warn "Not reading from a terminal; answers come from standard input."
  printf '%sClipPipeline setup%s\n' "$B" "$N"
  hint "Paste each value when asked. Secrets don't echo. Enter keeps the value shown in brackets."
  preflight
  [[ -f $ENV_FILE ]] || cp .env.example "$ENV_FILE"
  chmod 600 "$ENV_FILE"
  setup_telegram
  setup_chat_id
  setup_gemini
  setup_tunnel
  setup_dashboard
  setup_extras
  setup_secrets
  summary
  [[ -n $ENV_ONLY ]] || start_stack
  finish
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then main "$@"; fi

#!/bin/sh
# Create .env from .env.example with fresh random secrets, and the data folders.
# Safe to re-run: existing values in .env are never overwritten.
set -eu
cd "$(dirname "$0")/.."

[ -f .env ] || cp .env.example .env

rand() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

for key in POSTGRES_PASSWORD N8N_ENCRYPTION_KEY CLIPBOT_TOKEN DASHBOARD_PASSWORD POSTIZ_JWT_SECRET; do
  if grep -q "^${key}=$" .env; then
    value=$(rand)
    [ "$key" = DASHBOARD_PASSWORD ] && value=$(printf %s "$value" | cut -c1-20)  # you type this one
    sed -i "s|^${key}=$|${key}=${value}|" .env
    echo "generated ${key}"
  fi
done

# Match the container user to whoever owns this checkout, so ./data stays writable.
uid=$(id -u); gid=$(id -g)
sed -i "s|^PUID=.*|PUID=${uid}|; s|^PGID=.*|PGID=${gid}|" .env

mkdir -p data/pending data/previews data/approved data/rejected data/purge
chmod 600 .env
echo "done. Now edit .env: TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, PUBLIC_HOSTNAME"
echo "Dashboard password: $(grep '^DASHBOARD_PASSWORD=' .env | cut -d= -f2-)  (change it in .env if you like)"

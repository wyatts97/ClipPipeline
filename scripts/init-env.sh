#!/bin/sh
# Create .env from .env.example with fresh random secrets, and the data folders.
# Safe to re-run: existing values in .env are never overwritten.
set -eu
cd "$(dirname "$0")/.."

[ -f .env ] || cp .env.example .env

rand() { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'; }

for key in POSTGRES_PASSWORD N8N_ENCRYPTION_KEY CLIPBOT_TOKEN; do
  if grep -q "^${key}=$" .env; then
    sed -i "s|^${key}=$|${key}=$(rand)|" .env
    echo "generated ${key}"
  fi
done

# Match the container user to whoever owns this checkout, so ./data stays writable.
uid=$(id -u); gid=$(id -g)
sed -i "s|^PUID=.*|PUID=${uid}|; s|^PGID=.*|PGID=${gid}|" .env

mkdir -p data/pending data/previews data/approved data/rejected
chmod 600 .env
echo "done. Now edit .env: TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, PUBLIC_HOSTNAME, TUNNEL_TOKEN"

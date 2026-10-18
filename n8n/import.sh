#!/bin/sh
# Runs once in the n8n-import container: creates the two ClipBot credentials and the
# approval workflow in n8n. Later starts see the marker file and do nothing, so your
# edits in the n8n editor are never overwritten.
#
# To import again from scratch: delete the workflow + credentials in n8n, then
#   docker compose run --rm n8n-import --force
set -eu

MARKER=/home/node/.n8n/.clippipeline-imported
if [ -f "$MARKER" ] && [ "${1:-}" != "--force" ]; then
  echo "ClipPipeline workflow already imported (remove $MARKER or pass --force to redo)."
  exit 0
fi
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${CLIPBOT_TOKEN:-}" ]; then
  echo "TELEGRAM_BOT_TOKEN and CLIPBOT_TOKEN must be set in .env before the first start." >&2
  exit 1
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
node /import/render.js "$WORK"
n8n import:credentials --input="$WORK/credentials.json"
n8n import:workflow --input="$WORK/workflow.json"
# Publish (activate) it, so n8n registers the Telegram webhook as soon as it starts.
n8n publish:workflow --id=clippipelineTg01
touch "$MARKER"
echo "Imported and published 'ClipPipeline - Telegram approval'."

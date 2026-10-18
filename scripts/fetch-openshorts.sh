#!/bin/sh
# Check out OpenShorts (github.com/mutonby/openshorts, MIT) at a pinned commit into
# vendor/openshorts, where docker compose builds it from.
#
#   scripts/fetch-openshorts.sh            # the pinned commit below
#   scripts/fetch-openshorts.sh <sha|ref>  # try another commit, e.g. main
set -eu
cd "$(dirname "$0")/.."

REPO=https://github.com/mutonby/openshorts.git
# Commit reviewed for this setup (2026-10-17). Its API is what clipbot talks to.
PINNED=97ad88f396431ab4a96a967dc341ca992bcb9d41
REF=${1:-$PINNED}

if [ ! -d vendor/openshorts/.git ]; then
  mkdir -p vendor
  git clone --quiet --filter=blob:none "$REPO" vendor/openshorts
fi
git -C vendor/openshorts fetch --quiet origin
git -C vendor/openshorts checkout --quiet --detach "$REF" 2>/dev/null \
  || git -C vendor/openshorts checkout --quiet --detach "origin/$REF"
echo "vendor/openshorts is at $(git -C vendor/openshorts rev-parse --short HEAD)"
echo "next: docker compose build openshorts"

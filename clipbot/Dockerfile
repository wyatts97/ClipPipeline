# ---- the web dashboard (React + Vite), built to static files ----
FROM node:24.19.0-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim-trixie

# ffmpeg for previews and loudness, curl for the healthcheck,
# libfontconfig1 for TwitchDownloaderCLI's bundled graphics library.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates libfontconfig1 \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JS runtime for YouTube; deno is the one it recommends.
COPY --from=denoland/deno:bin-2.9.7 /deno /usr/local/bin/deno

# TwitchDownloaderCLI (github.com/lay295/TwitchDownloader, MIT) downloads Twitch chat replays for /scan.
ARG TWITCH_DOWNLOADER_VERSION=1.56.5
ARG TWITCH_DOWNLOADER_SHA256=f2bcee00d0e6da759d97c23767345634cb9cdb10cdff96cfa96f97983d51bf55
RUN curl -fsSL -o /tmp/td.zip \
      "https://github.com/lay295/TwitchDownloader/releases/download/${TWITCH_DOWNLOADER_VERSION}/TwitchDownloaderCLI-${TWITCH_DOWNLOADER_VERSION}-Linux-x64.zip" \
    && echo "${TWITCH_DOWNLOADER_SHA256}  /tmp/td.zip" | sha256sum -c - \
    && python -c "import zipfile; zipfile.ZipFile('/tmp/td.zip').extract('TwitchDownloaderCLI', '/usr/local/bin')" \
    && chmod +x /usr/local/bin/TwitchDownloaderCLI && rm /tmp/td.zip
# .NET without ICU: chat download needs no culture data.
ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
# It's a single-file .NET app that unpacks itself on first run; give it a directory any user can write
# (the default, $HOME/.net, may not exist for the PUID the container runs as).
ENV DOTNET_BUNDLE_EXTRACT_BASE_DIR=/tmp/dotnet-bundle

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    # yt-dlp is deliberately unpinned: sites change weekly and old versions stop working.
    && pip install --no-cache-dir --upgrade "yt-dlp[default]"

COPY app ./app
# Caption fonts for templates (OFL / Apache 2.0, licenses in fonts/licenses). Yours go in data/fonts.
COPY fonts ./fonts
COPY --from=web /web/dist ./web

ENV PYTHONUNBUFFERED=1 \
    CLIPBOT_WEB_DIR=/app/web \
    CLIPBOT_DATA_DIR=/data \
    HOME=/data/.home \
    XDG_CACHE_HOME=/data/.cache

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -sf http://127.0.0.1:8080/health > /dev/null || exit 1

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8080"]

"""Exercise setup.sh --env-only (and --publish --env-only) against fake Telegram, Gemini, Twitch and
Postiz APIs.

    python dev/setup_check.py

Runs the real script in a temp copy of the repo with scripted answers (wrong values first, to
check the retry paths), then re-runs it pressing Enter everywhere and expects .env unchanged.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
# On Windows "bash" may be the WSL launcher; prefer Git Bash there.
GIT_BASH = Path("C:/Program Files/Git/bin/bash.exe")
BASH = os.environ.get("SETUP_BASH") or (str(GIT_BASH) if GIT_BASH.exists() else "bash")
GOOD_TOKEN = "123456789:AAHgoodtokengoodtokengoodtoken1234"
GEMINI_KEY = "AIzaGoodGeminiKey_1234567890"
TUNNEL = "eyJhIjoiYWJjZGVmIiwidCI6IjEyMzQ1Njc4LTkwYWItY2RlZiIsInMiOiJzZWNyZXQifQ=="
POSTIZ_KEY = "postiz-good-key-0123456789"
STATE = {"acked": False}


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        m = re.match(r"^/bot([^/]+)/(\w+)$", url.path)
        if m:
            token, method = m.groups()
            if token != GOOD_TOKEN:
                return self.reply(401, {"ok": False, "error_code": 401, "description": "Unauthorized"})
            if method == "getMe":
                return self.reply(200, {"ok": True, "result": {"id": 1, "is_bot": True, "username": "clip_test_bot"}})
            if method == "getWebhookInfo":
                return self.reply(200, {"ok": True, "result": {"url": "", "pending_update_count": 0}})
            if method == "getUpdates":
                offset = q.get("offset", ["0"])[0]
                if offset == "-1":  # the newest old update, to skip past it
                    return self.reply(200, {"ok": True, "result": [{"update_id": 100, "message": {
                        "chat": {"id": 1111}, "text": "old"}}]})
                if offset == "101" and not STATE["acked"]:
                    return self.reply(200, {"ok": True, "result": [{"update_id": 101, "message": {
                        "message_id": 5, "from": {"id": 4242, "first_name": "Wyatt"},
                        "chat": {"id": 4242, "first_name": "Wyatt", "type": "private"}, "text": "/start"}}]})
                if offset == "102":
                    STATE["acked"] = True
                return self.reply(200, {"ok": True, "result": []})
        if url.path == "/api/public/v1/is-connected":
            good = self.headers.get("Authorization") == POSTIZ_KEY
            return self.reply(200 if good else 401, {"connected": True} if good else {"msg": "Invalid API key"})
        if url.path == "/v1beta/models":
            good = self.headers.get("x-goog-api-key") == GEMINI_KEY
            return self.reply(200 if good else 400, {"models": []} if good else {"error": {"code": 400}})
        return self.reply(404, {"detail": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = parse_qs(self.rfile.read(length).decode())
        if self.path == "/oauth2/token":
            good = body.get("client_id") == ["tid"] and body.get("client_secret") == ["tsecret"]
            return self.reply(200 if good else 400, {"access_token": "x"} if good else {"message": "invalid"})
        return self.reply(404, {})


def run(workdir: Path, answers: list[str], port: int, *flags: str) -> str:
    env = {"TELEGRAM_API": f"http://127.0.0.1:{port}", "GEMINI_API": f"http://127.0.0.1:{port}",
           "TWITCH_ID_API": f"http://127.0.0.1:{port}", "POSTIZ_LOCAL": f"http://127.0.0.1:{port}",
           "NO_COLOR": "1"}
    # Bytes, not text: in text mode Windows would send every newline as \r\n.
    proc = subprocess.run([BASH, "setup.sh", *flags, "--env-only"], cwd=workdir,
                          input=("\n".join(answers) + "\n").encode("utf-8"),
                          capture_output=True, timeout=120, env={**os.environ, **env})
    out = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise AssertionError(f"setup.sh exited {proc.returncode}\n{out}\n{proc.stderr.decode(errors='replace')}")
    return out


def env_of(workdir: Path) -> dict[str, str]:
    out = {}
    for line in (workdir / ".env").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    work = Path(tempfile.mkdtemp(prefix="setup-check-"))
    try:
        shutil.copy(ROOT / "setup.sh", work)
        shutil.copy(ROOT / ".env.example", work)

        out = run(work, [
            "123",                                   # not a token
            "999:BADBADBADBADBADBADBADBADBADBADBAD",  # refused by Telegram
            GOOD_TOKEN,
            "wrong-key", GEMINI_KEY,                 # Gemini: refused, then fine
            "n",                                     # no cloudflared on this host -> container
            f"docker run cloudflare/cloudflared:latest tunnel --no-autoupdate run --token {TUNNEL}",
            "https://Clips.Example.com/webhook/",    # normalised to clips.example.com
            "y",                                     # route added
            "my pass#$word", "my pass#$word",        # dashboard password with special characters
            "",                                      # time zone: keep the default
            "medium", "base",                        # invalid, then base
            "y", "tid", "nope", "y", "tid", "tsecret",  # Twitch: refused, retry, fine
        ], port)
        env = env_of(work)
        expect = {
            "TELEGRAM_BOT_TOKEN": GOOD_TOKEN, "ALLOWED_CHAT_IDS": "4242", "GEMINI_API_KEY": GEMINI_KEY,
            "TUNNEL_TOKEN": TUNNEL, "COMPOSE_PROFILES": "tunnel", "PUBLIC_HOSTNAME": "clips.example.com",
            "DASHBOARD_PASSWORD": "'my pass#$word'", "WHISPER_MODEL": "base",
            "TWITCH_CLIENT_ID": "tid", "TWITCH_CLIENT_SECRET": "tsecret",
        }
        for key, value in expect.items():
            assert env.get(key) == value, (key, env.get(key), out)
        for key in ("POSTGRES_PASSWORD", "N8N_ENCRYPTION_KEY", "CLIPBOT_TOKEN", "POSTIZ_JWT_SECRET"):
            assert re.fullmatch(r"[0-9a-f]{64}", env[key]), (key, env[key])
        assert "Token works: @clip_test_bot" in out and "Got it: Wyatt (4242)" in out
        assert "That doesn't look like a bot token" in out and "Telegram refused it: Unauthorized" in out
        assert "Google refused that key" in out and "Twitch refused them" in out
        assert STATE["acked"], "the /start update was not acknowledged"
        assert "# ---- Required: fill these in" in (work / ".env").read_text(encoding="utf-8")
        assert b"\r" not in (work / ".env").read_bytes()
        print("✓ first run: retries, auto chat id, quoting, generated secrets")

        before = (work / ".env").read_bytes()
        out = run(work, [""] * 30, port)            # Enter everywhere
        assert (work / ".env").read_bytes() == before, "re-run with Enter changed .env"
        assert "Token works" in out and "Telegram bot" in out
        print("✓ re-run pressing Enter keeps .env byte-for-byte")

        out = run(work, ["", "", "", "y", "", "y", "", "", "", ""], port)  # switch to host cloudflared
        env = env_of(work)
        assert env["COMPOSE_PROFILES"] == "" and env["PUBLIC_HOSTNAME"] == "clips.example.com"
        assert "localhost:5678" in out
        print("✓ switching to the server's own cloudflared clears the tunnel profile")

        out = run(work, [
            "clips.example.com",                     # Telegram's hostname: refused
            "https://Postiz.Example.com/",           # normalised
            "y",                                     # route added
            "yt-id", "yt-secret",                    # YouTube
            "",                                      # TikTok: skipped for now
            "ig-id", "ig-secret",                    # Instagram
            "9:00, 18:30",                           # posting times
            "wrong-key", POSTIZ_KEY,                 # Postiz: refused, then fine
        ], port, "--publish")
        env = env_of(work)
        expect = {"POSTIZ_HOSTNAME": "postiz.example.com", "YOUTUBE_CLIENT_ID": "yt-id",
                  "YOUTUBE_CLIENT_SECRET": "yt-secret", "TIKTOK_CLIENT_ID": "", "INSTAGRAM_APP_ID": "ig-id",
                  "INSTAGRAM_APP_SECRET": "ig-secret", "PUBLISH_SLOTS": "9:00,18:30",
                  "POSTIZ_API_KEY": POSTIZ_KEY, "COMPOSE_PROFILES": "publish"}
        for key, value in expect.items():
            assert env.get(key) == value, (key, env.get(key), out)
        assert "Use a different hostname than Telegram's" in out and "Postiz refused it (HTTP 401)" in out
        assert "https://postiz.example.com/integrations/social/youtube" in out and "localhost:4007" in out
        print("✓ --publish: hostname check, app keys, posting times, Postiz key check, publish profile")

        before = (work / ".env").read_bytes()
        run(work, [""] * 20, port, "--publish")
        assert (work / ".env").read_bytes() == before, "re-run of --publish with Enter changed .env"
        run(work, [""] * 40, port)                  # full setup again: back to the tunnel container
        assert env_of(work)["COMPOSE_PROFILES"] == "publish,tunnel", env_of(work)["COMPOSE_PROFILES"]
        print("✓ re-runs keep the publishing settings and the publish profile")
        print("\nSETUP CHECKS PASSED")
    finally:
        server.shutdown()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()

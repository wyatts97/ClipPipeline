"""Fake Telegram Bot API (:8081), fake OpenShorts (:8001) and fake Gemini (:8002) for the end-to-end test.

Telegram: records every call; GET /_calls returns them as JSON.
OpenShorts: accepts any job (URL or upload), reports it completed with two clips (or the
preset_clips it was given, with their copy), serves the clip bytes. It also serves <stream.mp4> at
/media/stream.mp4 (with HTTP ranges) so /scan and range downloads have a "VOD" to work on.
Gemini: the File API (upload, ACTIVE, delete) and generateContent: two clips per video for the
Director, hook lines, no sound-effect moments. Recorded as "process" / "gemini" calls.

    python fake_services.py <sample.mp4> [<stream.mp4>]
"""
from __future__ import annotations

import email
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SAMPLE = Path(sys.argv[1]).read_bytes() if len(sys.argv) > 1 else b""
STREAM = Path(sys.argv[2]).read_bytes() if len(sys.argv) > 2 else b""
CALLS: list[dict] = []
STATE = {"webhook_url": "", "message_id": 100, "presets": None, "uploads": 0}
LOCK = threading.Lock()


def read_raw(handler: BaseHTTPRequestHandler) -> bytes:
    if "chunked" in handler.headers.get("Transfer-Encoding", "").lower():
        chunks = []
        while True:
            size = int(handler.rfile.readline().split(b";")[0].strip() or b"0", 16)
            if size == 0:
                handler.rfile.readline()  # trailing CRLF after the last chunk
                return b"".join(chunks)
            chunks.append(handler.rfile.read(size))
            handler.rfile.readline()
    length = int(handler.headers.get("Content-Length") or 0)
    return handler.rfile.read(length) if length else b""


def parse_body(handler: BaseHTTPRequestHandler) -> dict:
    raw = read_raw(handler)
    ctype = handler.headers.get("Content-Type", "")
    if "multipart/form-data" in ctype:
        msg = email.message_from_bytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + raw)
        fields = {}
        for part in msg.get_payload():
            name = part.get_param("name", header="content-disposition")
            payload = part.get_payload(decode=True) or b""
            if part.get_filename():
                fields[name] = {"filename": part.get_filename(), "bytes": len(payload)}
            else:
                fields[name] = payload.decode(errors="replace")
        return fields
    if "json" in ctype and raw:
        return json.loads(raw)
    if raw:
        from urllib.parse import parse_qsl
        return dict(parse_qsl(raw.decode()))
    return {}


class Telegram(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/_calls":
            with LOCK:
                return self._send(CALLS)
        return self._handle({})

    def do_POST(self):
        return self._handle(parse_body(self))

    def _handle(self, body):
        match = re.match(r"^/bot([^/]+)/(\w+)", self.path)
        if not match:
            return self._send({"ok": False, "description": "not found"}, 404)
        method = match.group(2)
        with LOCK:
            CALLS.append({"method": method, "body": body})
            if method == "setWebhook":
                STATE["webhook_url"] = body.get("url", "")
                return self._send({"ok": True, "result": True})
            if method == "getWebhookInfo":
                return self._send({"ok": True, "result": {"url": STATE["webhook_url"]}})
            if method in ("sendMessage", "sendVideo"):
                STATE["message_id"] += 1
                chat = body.get("chat_id")
                return self._send({"ok": True, "result": {
                    "message_id": STATE["message_id"], "date": 0,
                    "chat": {"id": int(chat) if str(chat).lstrip("-").isdigit() else chat}}})
        if method == "getMe":
            return self._send({"ok": True, "result": {"id": 1, "is_bot": True, "username": "fakebot"}})
        return self._send({"ok": True, "result": True})


class OpenShorts(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = parse_body(self)
        if self.path == "/api/process":
            with LOCK:
                CALLS.append({"method": "process", "body": body})
                STATE["presets"] = body.get("preset_clips")
            return self._send({"job_id": "fake-os-job", "status": "queued"})
        if self.path == "/api/uploads":
            return self._send({"upload_id": "up1", "upload_url": "http://ignored/api/uploads/up1"})
        return self._send({"detail": "not found"}, 404)

    def do_PUT(self):
        size = len(read_raw(self))
        with LOCK:
            CALLS.append({"method": "upload", "body": {"bytes": size}})
        return self._send({"upload_id": "up1", "bytes": size})

    def _send_bytes(self, data: bytes, ctype="video/mp4"):
        rng = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        start, end = (int(rng.group(1)), int(rng.group(2) or len(data) - 1)) if rng else (0, len(data) - 1)
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data[start:end + 1])

    def do_HEAD(self):
        if self.path.startswith("/media/"):
            return self._send_bytes(STREAM)
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/media/"):
            return self._send_bytes(STREAM)
        if self.path == "/api/status/fake-os-job":
            presets = STATE["presets"]
            if presets:      # the patched OpenShorts cuts exactly these and hands their copy back
                return self._send({"status": "completed", "logs": [], "result": {"clips": [
                    {**c, "video_url": f"/videos/fake-os-job/{'ab'[i % 2]}.mp4"} for i, c in enumerate(presets)]}})
            return self._send({"status": "completed", "logs": [], "result": {"clips": [
                {"video_url": "/videos/fake-os-job/a.mp4", "video_title_for_youtube_short": "Big play",
                 "viral_hook_text": "No way he hit that 😳"},
                {"video_url": "/videos/fake-os-job/b.mp4", "video_title_for_youtube_short": "Chat goes wild"},
            ]}})
        if self.path.startswith("/videos/fake-os-job/"):
            return self._send_bytes(SAMPLE)
        if self.path.startswith("/api/clip/fake-os-job/") and self.path.endswith("/transcript"):
            words = "no way he hit that chat".split()   # clipbot burns these as captions
            return self._send({"captions": [{"text": w, "startMs": i * 400, "endMs": i * 400 + 350}
                                            for i, w in enumerate(words)], "durationSec": 3, "language": "en"})
        return self._send({"detail": "not found"}, 404)


class Gemini(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, obj, status=200, headers=None):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _file(self, name):
        return {"name": name, "uri": f"http://127.0.0.1:8002/v1beta/{name}", "mimeType": "video/mp4", "state": "ACTIVE"}

    def do_GET(self):
        return self._send(self._file(self.path.split("/v1beta/", 1)[-1]))

    def do_DELETE(self):
        with LOCK:
            CALLS.append({"method": "gemini", "body": {"kind": "delete"}})
        return self._send({})

    def do_POST(self):
        raw = read_raw(self)
        if self.path == "/upload/v1beta/files":
            with LOCK:
                STATE["uploads"] += 1
                n = STATE["uploads"]
            return self._send({}, headers={"x-goog-upload-url": f"http://127.0.0.1:8002/upload-session/{n}"})
        if self.path.startswith("/upload-session/"):
            with LOCK:
                CALLS.append({"method": "gemini", "body": {"kind": "upload", "bytes": len(raw)}})
            return self._send({"file": self._file(f"files/f{self.path.rsplit('/', 1)[1]}")})
        body = json.loads(raw)
        parts = body["contents"][0]["parts"]
        wanted = list(body["generationConfig"]["responseSchema"]["properties"])
        if wanted == ["clips"]:       # the Director: two clips in every video it was shown
            videos = [m.group(1) for part in parts for m in [re.match(r"Video (\w+) \(", part.get("text", ""))] if m]
            answer = {"clips": [c for v in videos for c in (
                {"video": v, "start": 1, "end": 21, "score": 91, "category": "chaos", "why": "it's loud",
                 "what_happened": "the streamer yells at the screen", "hook": "he did not take that well",
                 "title": "Streamer loses it at the screen", "tiktok": "he lost it #stream", "instagram": "he lost it"},
                {"video": v, "start": 22, "end": 40, "score": 70, "category": "reaction", "why": "chat",
                 "what_happened": "chat spams the same joke", "hook": "chat was ready", "title": "Chat was ready",
                 "tiktok": "chat #stream", "instagram": "chat"})]}
            with LOCK:
                CALLS.append({"method": "gemini", "body": {"kind": "director", "videos": videos,
                                                           "prompt": parts[0]["text"]}})
        elif wanted == ["hooks"]:
            answer = {"hooks": ["he did not take that well", "chat saw it coming"]}
        else:
            answer = {"moments": []}
        return self._send({"candidates": [{"content": {"parts": [{"text": json.dumps(answer)}]}}]})


def main():
    servers = [ThreadingHTTPServer(("127.0.0.1", 8081), Telegram),
               ThreadingHTTPServer(("127.0.0.1", 8001), OpenShorts),
               ThreadingHTTPServer(("127.0.0.1", 8002), Gemini)]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print("fake services up", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()

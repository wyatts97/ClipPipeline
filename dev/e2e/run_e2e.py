"""End-to-end test of the n8n workflow with the real clipbot, on one machine, no Docker.

Real: n8n (from npm), the imported workflow + credentials, clipbot, ffmpeg.
Fake: Telegram Bot API and OpenShorts (fake_services.py).

Needs: n8n on PATH (or N8N_CMD="node /path/to/n8n/bin/n8n"), ffmpeg, and a Python
with clipbot/requirements.txt installed. Run from anywhere:

    python dev/e2e/run_e2e.py
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOKEN = "e2e-clipbot-token"
CHAT = 111
N8N = shlex.split(os.environ.get("N8N_CMD", "n8n"), posix=os.name != "nt")


def http(method: str, url: str, body=None, headers=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw)


def telegram_calls(method=None):
    calls = http("GET", "http://127.0.0.1:8081/_calls")[1]
    return [c for c in calls if method is None or c["method"] == method]


def wait_for(predicate, what: str, timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="clippipeline-e2e-"))
    procs: list[subprocess.Popen] = []
    logs = {name: open(work / f"{name}.log", "w", encoding="utf-8") for name in ("fake", "clipbot", "n8n")}
    env = {**os.environ,
           "N8N_USER_FOLDER": str(work / "n8n"), "N8N_ENCRYPTION_KEY": "e2e-encryption-key",
           "N8N_WEBHOOK_URL": "http://127.0.0.1:5678/", "N8N_DIAGNOSTICS_ENABLED": "false",
           "N8N_SECURE_COOKIE": "false", "DB_TYPE": "sqlite",
           "TELEGRAM_BOT_TOKEN": "123:fake", "TELEGRAM_API_URL": "http://127.0.0.1:8081",
           "CLIPBOT_TOKEN": TOKEN, "CLIPBOT_URL": "http://127.0.0.1:8080"}
    try:
        sample = work / "sample.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc=size=540x960:rate=30:duration=4", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=4", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-shortest", str(sample)], check=True)
        # A 6-minute "VOD" that is quiet except for a 10-second shout at 3:00, for /scan.
        stream = work / "stream.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "testsrc=size=320x240:rate=10:duration=360", "-f", "lavfi", "-i",
                        "sine=frequency=300:duration=360", "-af",
                        "volume='if(between(t,180,190),1,0.02)':eval=frame", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", "-shortest",
                        str(stream)], check=True)
        procs.append(subprocess.Popen(
            [sys.executable, str(ROOT / "dev/e2e/fake_services.py"), str(sample), str(stream)],
            stdout=logs["fake"], stderr=subprocess.STDOUT))

        # Same steps as n8n/import.sh, minus the container.
        (work / "render").mkdir()
        subprocess.run(["node", str(ROOT / "n8n/render.js"), str(work / "render")], env=env, check=True)
        for args in (["import:credentials", f"--input={work / 'render/credentials.json'}"],
                     ["import:workflow", f"--input={work / 'render/workflow.json'}"],
                     ["publish:workflow", "--id=clippipelineTg01"]):
            subprocess.run(N8N + args, env=env, check=True, stdout=logs["n8n"], stderr=subprocess.STDOUT)

        clipbot_env = {**env, "CLIPBOT_DATA_DIR": str(work / "data"),
                       "OPENSHORTS_URL": "http://127.0.0.1:8001",
                       "OPENSHORTS_OUTPUT_DIR": str(work / "no-shared-volume"),
                       "N8N_EVENTS_URL": "http://127.0.0.1:5678/webhook/clipbot-events",
                       "CLIPBOT_SELF_URL": "http://127.0.0.1:8080", "ALLOWED_CHAT_IDS": str(CHAT),
                       "POLL_SECONDS": "1",
                       # yt-dlp lives next to this Python (venv Scripts/bin).
                       "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ.get("PATH", "")}
        (work / "data").mkdir()
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--port", "8080"],
            cwd=ROOT / "clipbot", env=clipbot_env, stdout=logs["clipbot"], stderr=subprocess.STDOUT))
        procs.append(subprocess.Popen(N8N + ["start"], env=env, stdout=logs["n8n"], stderr=subprocess.STDOUT))

        hook = wait_for(lambda: telegram_calls("setWebhook"), "n8n to register the Telegram webhook", 180)[-1]["body"]
        print("✓ n8n published the workflow and called setWebhook:", hook["url"])
        assert sorted(hook["allowed_updates"]) == ["callback_query", "message"], hook
        tg_headers = {"X-Telegram-Bot-Api-Secret-Token": hook["secret_token"]}

        def update(payload):
            return http("POST", hook["url"], {"update_id": int(time.time() * 1000), **payload}, tg_headers)

        def msg(chat, text, mid=1):
            return {"message": {"message_id": mid, "date": 0, "text": text,
                                "chat": {"id": chat, "type": "private"}, "from": {"id": chat, "is_bot": False}}}

        # Forged update without Telegram's secret header is refused.
        try:
            http("POST", hook["url"], msg(CHAT, "/start"))
            raise AssertionError("update without secret token was accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 403, e.code
        print("✓ update without the secret token gets 403")

        update(msg(CHAT, "/start"))
        help_msg = wait_for(lambda: telegram_calls("sendMessage"), "help reply")[-1]["body"]
        assert str(help_msg["chat_id"]) == str(CHAT) and "Send me a video link" in help_msg["text"], help_msg
        print("✓ /start -> help text")

        update(msg(999, "https://youtu.be/stranger"))
        time.sleep(3)
        assert len(telegram_calls("sendMessage")) == 1, "stranger got a reply"
        print("✓ message from a chat not in ALLOWED_CHAT_IDS is ignored")

        update(msg(CHAT, "https://www.twitch.tv/videos/42 clips=2", mid=2))
        wait_for(lambda: any("queued" in c["body"].get("text", "") for c in telegram_calls("sendMessage")),
                 "queued reply")
        print("✓ link -> 'queued' reply")

        videos = wait_for(lambda: (v := telegram_calls("sendVideo")) and len(v) >= 2 and v, "two clip videos", 120)
        texts = [c["body"]["text"] for c in telegram_calls("sendMessage")]
        assert any("finished: 2 clip(s)" in t for t in texts), texts
        first = videos[0]["body"]
        assert first["video"]["bytes"] > 1000 and "Big play" in first["caption"], first
        assert "🎨 Standard" in first["caption"], first["caption"]           # captions rendered by clipbot
        assert not any("no words" in t or "template failed" in t for t in texts), texts
        keyboard = json.loads(first["reply_markup"])["inline_keyboard"][0]
        assert [b["text"] for b in keyboard] == ["✅ Post", "❌ Skip"], keyboard
        ok_data, skip_data = keyboard[0]["callback_data"], json.loads(
            videos[1]["body"]["reply_markup"])["inline_keyboard"][0][1]["callback_data"]
        print("✓ clips arrive as videos with ✅/❌ buttons:", ok_data, skip_data)

        def press(data, qid):
            return update({"callback_query": {
                "id": qid, "data": data, "from": {"id": CHAT, "is_bot": False},
                "message": {"message_id": 555, "date": 0, "chat": {"id": CHAT, "type": "private"}}}})

        press(ok_data, "q1")
        answer = wait_for(lambda: telegram_calls("answerCallbackQuery"), "callback answer")[-1]["body"]
        assert answer["callback_query_id"] == "q1" and answer["text"] == "Approved", answer
        reply = wait_for(lambda: [c for c in telegram_calls("sendMessage")
                                  if "Approved →" in c["body"].get("text", "")], "approve reply")[-1]["body"]
        assert str(reply["reply_to_message_id"]) == "555", reply
        assert list((work / "data/approved").glob("*.mp4")), "approved file missing"
        print("✓ ✅ Post -> toast 'Approved', reply, file in approved/")

        press(skip_data, "q2")
        wait_for(lambda: [c for c in telegram_calls("answerCallbackQuery") if c["body"]["text"] == "Rejected"],
                 "reject answer")
        press(ok_data, "q3")
        wait_for(lambda: [c for c in telegram_calls("answerCallbackQuery") if c["body"]["text"] == "Already approved"],
                 "double-press answer")
        print("✓ ❌ Skip -> 'Rejected'; pressing ✅ again -> 'Already approved'")

        # ---- /scan: real yt-dlp + ffmpeg loudness on the fake VOD ----
        update(msg(CHAT, "/scan http://127.0.0.1:8001/media/stream.mp4", mid=3))
        wait_for(lambda: any("🔎 Scan" in c["body"].get("text", "") and "queued" in c["body"]["text"]
                             for c in telegram_calls("sendMessage")), "scan queued reply")
        summary = wait_for(lambda: [c for c in telegram_calls("sendMessage")
                                    if "Scan done" in c["body"].get("text", "")], "scan summary", 120)[-1]["body"]
        assert "audio" in summary["text"], summary
        choice = wait_for(lambda: [c for c in telegram_calls("sendMessage")
                                   if c["body"].get("text", "").startswith("#1 🔥")], "moment message")[-1]["body"]
        assert "3:0" in choice["text"].splitlines()[0] and "🔊 loud" in choice["text"], choice["text"]
        markup = choice["reply_markup"]
        buttons = (json.loads(markup) if isinstance(markup, str) else markup)["inline_keyboard"][0]
        assert [b["text"] for b in buttons] == ["🎬 Clip it", "➕ Wider ×2"], buttons
        print("✓ /scan -> summary + '#1 🔥 3:0x' moment with 🎬 buttons")

        videos_before = len(telegram_calls("sendVideo"))
        press(buttons[0]["callback_data"], "q4")
        wait_for(lambda: [c for c in telegram_calls("answerCallbackQuery") if c["body"]["text"] == "Queued"],
                 "moment queued answer")
        wait_for(lambda: [c for c in telegram_calls("sendMessage") if "of moment #1" in c["body"].get("text", "")],
                 "moment job reply")
        uploaded = wait_for(lambda: telegram_calls("upload"), "range upload to OpenShorts", 120)[-1]["body"]
        assert uploaded["bytes"] > 10_000, uploaded
        wait_for(lambda: len(telegram_calls("sendVideo")) >= videos_before + 2, "clips from the moment", 120)
        press(buttons[0]["callback_data"], "q5")
        wait_for(lambda: [c for c in telegram_calls("answerCallbackQuery") if c["body"]["text"] == "Already queued"],
                 "second 🎬 tap")
        print("✓ 🎬 -> range downloaded, uploaded, clipped, previews sent; second tap -> 'Already queued'")
        print("\nALL E2E CHECKS PASSED")
    except Exception:
        for f in logs.values():
            f.flush()
        for name in ("clipbot", "n8n"):
            tail = (work / f"{name}.log").read_text(encoding="utf-8", errors="replace").splitlines()[-25:]
            print(f"\n----- {name}.log (tail) -----\n" + "\n".join(tail))
        raise
    finally:
        for p in reversed(procs):
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
        for f in logs.values():
            f.close()
        if os.environ.get("KEEP_E2E") != "1":
            shutil.rmtree(work, ignore_errors=True)
        else:
            print("kept", work)


if __name__ == "__main__":
    main()

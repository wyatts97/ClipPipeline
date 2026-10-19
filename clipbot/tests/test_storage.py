"""Disk use per job, deleting finished jobs' files (clipbot's part and scripts/janitor.sh), and the
Telegram side: the offer after the last decision, /cleanup and the 🗑 / Keep buttons."""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from app import storage
from app.parsing import parse_message
from app.store import Store

OS_JOB = "0b5c7e2a-1f3d-4c6b-9a8e-123456789abc"
OTHER_OS_JOB = "9f9f9f9f-0000-4000-8000-000000000000"
MB = 1024 * 1024
JANITOR = Path(__file__).resolve().parents[2] / "scripts" / "janitor.sh"


@pytest.fixture
def env(settings):
    store = Store(settings.db_path)
    yield settings, store
    store.close()


def make_job(settings, store, status="done", os_job=OS_JOB, source_mb=25):
    job = store.create_job(111, "https://www.twitch.tv/videos/1", 0.0, 600.0, 2)
    store.update_job(job.id, status=status, openshorts_job=os_job)
    work = settings.openshorts_output_dir / os_job
    work.mkdir(parents=True)
    (work / "clip_0.mp4").write_bytes(b"x" * MB)
    settings.openshorts_uploads_dir.mkdir(parents=True, exist_ok=True)
    (settings.openshorts_uploads_dir / f"{os_job}_source.mp4").write_bytes(b"x" * source_mb * MB)
    return store.get_job(job.id)


def add_clip(settings, store, job, idx, status, size=2 * MB):
    path = settings.folder(status) / f"{job.id}_{idx}.mp4"
    path.write_bytes(b"x" * size)
    clip = store.add_clip(job.id, idx, f"Clip {idx}", None, {}, 3.0, str(path), None)
    if status != "pending":
        store.decide_clip(clip.id, status, str(path))
    return store.get_clip(clip.id)


def test_usage_counts_what_a_cleanup_frees(env):
    settings, store = env
    job = make_job(settings, store)
    add_clip(settings, store, job, 0, "approved")
    add_clip(settings, store, job, 1, "rejected", size=3 * MB)
    usage = storage.job_usage(settings, store, job)
    assert usage == {"openshorts": 26 * MB, "rejected": 3 * MB, "pending": 0, "approved": 2 * MB}
    assert storage.freeable(usage) == 29 * MB


def test_delete_asks_the_janitor_and_drops_skipped_clips(env):
    settings, store = env
    job = make_job(settings, store)
    approved = add_clip(settings, store, job, 0, "approved")
    rejected = add_clip(settings, store, job, 1, "rejected")
    assert [j.id for j, _ in storage.cleanup_candidates(settings, store)] == [job.id]

    assert storage.delete_job_files(settings, store, job) == 28 * MB
    assert (settings.data_dir / "purge" / OS_JOB).is_file()       # for the janitor
    assert not Path(rejected.path).exists() and Path(approved.path).is_file()
    assert storage.purge_requested(settings, OS_JOB)
    assert storage.cleanup_candidates(settings, store) == []        # not offered twice


def test_running_jobs_keep_their_files(env):
    settings, store = env
    job = make_job(settings, store, status="processing")
    with pytest.raises(ValueError):
        storage.delete_job_files(settings, store, job)
    assert storage.cleanup_candidates(settings, store) == []


def test_odd_openshorts_ids_are_never_touched(env):
    settings, store = env
    job = store.create_job(111, "https://youtu.be/x", None, None, 1)
    store.update_job(job.id, status="done", openshorts_job="../../etc")
    assert storage.openshorts_paths(settings, "../../etc") == []
    assert storage.delete_job_files(settings, store, store.get_job(job.id)) == 0
    assert not (settings.data_dir / "purge").exists()


def _bash():
    for candidate in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe"):
        if candidate and Path(candidate).is_file() and "System32" not in candidate:
            return candidate
    return None


@pytest.mark.skipif(_bash() is None, reason="needs a POSIX shell")
def test_janitor_deletes_only_the_requested_job(tmp_path):
    purge, output, uploads = tmp_path / "purge", tmp_path / "output", tmp_path / "uploads"
    for folder in (purge, output / OS_JOB, output / OTHER_OS_JOB, uploads):
        folder.mkdir(parents=True)
    (output / OS_JOB / "clip.mp4").write_bytes(b"x")
    (uploads / f"{OS_JOB}_source.mp4").write_bytes(b"x")
    (uploads / f"{OTHER_OS_JOB}_source.mp4").write_bytes(b"x")
    (purge / OS_JOB).touch()
    (purge / "not-an-id!").touch()
    env = {**os.environ, "ONCE": "1", "PURGE": purge.as_posix(), "OUTPUT": output.as_posix(),
           "UPLOADS": uploads.as_posix()}
    result = subprocess.run([_bash(), JANITOR.as_posix()], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert not (output / OS_JOB).exists() and not (uploads / f"{OS_JOB}_source.mp4").exists()
    assert (output / OTHER_OS_JOB).is_dir() and (uploads / f"{OTHER_OS_JOB}_source.mp4").is_file()
    assert list(purge.iterdir()) == []
    assert "ignoring request 'not-an-id!'" in result.stdout


def test_janitor_stuck_when_requests_pile_up(env):
    settings, _ = env
    request = settings.folder("purge") / OS_JOB
    request.touch()
    assert not storage.janitor_stuck(settings)
    old = time.time() - 300
    os.utime(request, (old, old))
    assert storage.janitor_stuck(settings)


def test_cleanup_is_offered_once_every_clip_is_decided(env):
    settings, store = env
    job = make_job(settings, store)
    add_clip(settings, store, job, 0, "approved")
    waiting = add_clip(settings, store, job, 1, "pending")
    storage.offer_cleanup(settings, store, job)
    assert store.outbox() == []                                     # a clip still needs review
    store.decide_clip(waiting.id, "rejected", waiting.path)
    storage.offer_cleanup(settings, store, job)
    (event,) = [e["payload"] for e in store.outbox()]
    assert event["event"] == "choice" and "28 MB" in event["text"]
    assert [b["data"] for b in event["buttons"]] == [f"rm:{job.id}", f"kp:{job.id}"]


def test_small_leftovers_are_not_worth_a_message(env):
    settings, store = env
    job = make_job(settings, store, source_mb=1)
    add_clip(settings, store, job, 0, "approved")
    storage.offer_cleanup(settings, store, job)
    assert store.outbox() == []


def test_buttons(env):
    settings, store = env
    job = make_job(settings, store)
    assert storage.button(settings, store, 222, f"rm:{job.id}") == ("Unknown job", None)   # someone else's
    assert storage.button(settings, store, 111, f"kp:{job.id}")[0] == "Kept"
    toast, reply = storage.button(settings, store, 111, f"rm:{job.id}")
    assert toast == "Deleting" and "26 MB" in reply
    assert (settings.data_dir / "purge" / OS_JOB).is_file()


def test_cleanup_command(env):
    settings, store = env
    assert parse_message("/cleanup").kind == "cleanup"
    assert storage.offer_cleanup_all(settings, store, 111).startswith("🧹 Nothing to clean up")
    first = make_job(settings, store)
    second = make_job(settings, store, os_job=OTHER_OS_JOB, source_mb=5)
    assert storage.offer_cleanup_all(settings, store, 111) is None   # the offer comes as its own message
    (event,) = [e["payload"] for e in store.outbox()]
    assert "2 finished job(s)" in event["text"] and event["buttons"][0]["data"] == "rm:all"
    toast, reply = storage.button(settings, store, 111, "rm:all")
    assert "from 2 job(s)" in reply
    assert storage.purge_requested(settings, first.openshorts_job)
    assert storage.purge_requested(settings, second.openshorts_job)

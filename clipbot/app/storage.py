"""Disk use per job, and deleting the files of jobs that are done with.

What a job leaves on disk:
  OpenShorts  output/<its job id>/ (work files; the whole download for whole-video jobs) and
              uploads/<its job id>_* (the downloaded section for range jobs). The big ones.
              OpenShorts deletes them itself after 24 h (JOB_RETENTION_SECONDS).
  clipbot     pending/ and approved/ clips (kept), rejected/ clips, previews/, thumbs/.

OpenShorts' files belong to its own user, so clipbot can't delete them. It drops an empty file
named after the OpenShorts job id into data/purge/, and the janitor container
(scripts/janitor.sh) deletes that job's files.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from .config import Settings
from .store import Job, Store

log = logging.getLogger("clipbot.storage")

FINISHED = ("done", "failed", "cancelled")
ASK_ABOVE_BYTES = 20 * 1024 ** 2      # don't offer a cleanup for less than this
JANITOR_STUCK_SECONDS = 60
_OPENSHORTS_ID = re.compile(r"^[0-9a-f][0-9a-f-]{7,63}$")   # OpenShorts job ids are UUIDs


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def size_of(path: Path) -> int:
    try:
        return dir_size(path) if path.is_dir() else path.stat().st_size
    except OSError:
        return 0


def openshorts_paths(settings: Settings, os_job: Optional[str]) -> list[Path]:
    """The OpenShorts files of one of its jobs that still exist."""
    if not os_job or not _OPENSHORTS_ID.match(os_job):
        return []
    found = []
    work = settings.openshorts_output_dir / os_job
    if work.is_dir():
        found.append(work)
    uploads = settings.openshorts_uploads_dir
    if uploads.is_dir():
        found += sorted(uploads.glob(f"{os_job}_*"))
    return found


def rejected_files(store: Store, job: Job) -> list[Path]:
    return [Path(c.path) for c in store.clips_for_job(job.id)
            if c.status == "rejected" and Path(c.path).is_file()]


def job_usage(settings: Settings, store: Store, job: Job) -> dict[str, int]:
    """Bytes a job takes: what a cleanup frees (openshorts + rejected) and what it keeps."""
    clips = store.clips_for_job(job.id)
    kept = {s: sum(size_of(Path(c.path)) for c in clips if c.status == s) for s in ("pending", "approved")}
    return {"openshorts": sum(size_of(p) for p in openshorts_paths(settings, job.openshorts_job)),
            "rejected": sum(size_of(p) for p in rejected_files(store, job)), **kept}


def freeable(usage: dict[str, int]) -> int:
    return usage["openshorts"] + usage["rejected"]


def delete_job(settings: Settings, store: Store, job: Job) -> int:
    """Delete a failed or cancelled job for good: its files (as delete_job_files, plus any clips it
    left behind), then its record. Returns the bytes freed."""
    if job.status not in ("failed", "cancelled"):
        raise ValueError("Only failed or cancelled jobs can be deleted.")
    clips = store.clips_for_job(job.id)
    if any(c.status == "approved" for c in clips):
        raise ValueError("This job has approved clips; delete its files instead.")
    freed = delete_job_files(settings, store, job)
    for clip in clips:
        for path in (clip.path, clip.preview_path,
                     settings.data_dir / "clean" / f"{job.id}_{clip.idx}.mp4",
                     settings.data_dir / "thumbs" / f"{job.id}_{clip.idx}.jpg"):
            if path and Path(path).is_file():
                freed += size_of(Path(path))
                Path(path).unlink(missing_ok=True)
    store.delete_job(job.id)
    log.info("job %s: deleted", job.id)
    return freed


def purge_requested(settings: Settings, os_job: Optional[str]) -> bool:
    return bool(os_job) and (settings.data_dir / "purge" / os_job).exists()


def delete_job_files(settings: Settings, store: Store, job: Job) -> int:
    """Delete a finished job's OpenShorts files (through the janitor) and its rejected clips.
    Approved and pending clips, thumbnails and the job's history stay. Returns the bytes freed."""
    if job.status not in FINISHED:
        raise ValueError("The job is still running.")
    usage = job_usage(settings, store, job)
    if openshorts_paths(settings, job.openshorts_job):
        (settings.folder("purge") / job.openshorts_job).touch()
    for path in rejected_files(store, job):
        path.unlink(missing_ok=True)
    freed = freeable(usage)
    if freed:
        log.info("job %s: deleting %s of files", job.id, human(freed), extra={"job_id": job.id})
    return freed


def cleanup_candidates(settings: Settings, store: Store) -> list[tuple[Job, int]]:
    """Finished jobs that still have files a cleanup would delete, biggest first."""
    found = []
    for job in store.list_jobs(limit=2000):
        if job.status in FINISHED and not purge_requested(settings, job.openshorts_job):
            freed = freeable(job_usage(settings, store, job))
            if freed:
                found.append((job, freed))
    return sorted(found, key=lambda item: item[1], reverse=True)


def janitor_stuck(settings: Settings, now: Optional[float] = None) -> bool:
    """True when delete requests sit unhandled: the janitor container isn't running."""
    folder = settings.data_dir / "purge"
    now = time.time() if now is None else now
    try:
        return any(now - p.stat().st_mtime > JANITOR_STUCK_SECONDS for p in folder.iterdir())
    except OSError:
        return False


def usage_report(settings: Settings, store: Store) -> dict[str, Any]:
    """Everything the dashboard's Storage page shows."""
    folders = {name: size_of(settings.data_dir / name)
               for name in ("pending", "clean", "approved", "rejected", "previews", "thumbs", "tmp", "sounds")}
    jobs = []
    tracked: set[str] = set()
    for job in store.list_jobs(limit=2000):
        if job.openshorts_job:
            tracked.add(job.openshorts_job)
        usage = job_usage(settings, store, job)
        if any(usage.values()):
            jobs.append({"job": job, "usage": usage, "deleting": purge_requested(settings, job.openshorts_job)})
    openshorts_total = {"work": size_of(settings.openshorts_output_dir),
                        "sources": size_of(settings.openshorts_uploads_dir)}
    disk = shutil.disk_usage(settings.data_dir)
    return {"disk": {"total": disk.total, "free": disk.free}, "folders": folders,
            "openshorts": openshorts_total, "jobs": jobs, "janitor_stuck": janitor_stuck(settings)}


def human(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    return f"{max(1, round(n / 1024 ** 2))} MB"


# ---- Telegram ------------------------------------------------------------------------
def offer_cleanup(settings: Settings, store: Store, job: Job) -> None:
    """Once every clip of a job is decided, ask whether to delete what's left of it."""
    if any(c.status == "pending" for c in store.clips_for_job(job.id)):
        return
    freed = freeable(job_usage(settings, store, job))
    if freed < ASK_ABOVE_BYTES:
        return
    store.enqueue_event({
        "event": "choice", "chat_id": job.chat_id,
        "text": (f"🧹 All clips of job {job.id} are decided. Its source and leftover files take "
                 f"{human(freed)}. Approved clips stay either way."),
        "buttons": [{"text": "🗑 Delete files", "data": f"rm:{job.id}"},
                    {"text": "Keep", "data": f"kp:{job.id}"}],
    })


def offer_cleanup_all(settings: Settings, store: Store, chat_id: int) -> Optional[str]:
    """/cleanup: offer to delete the leftovers of every finished job (the offer arrives as its own
    message with buttons, so there is no reply then)."""
    candidates = cleanup_candidates(settings, store)
    disk = shutil.disk_usage(settings.data_dir)
    free = f"Disk: {human(disk.free)} free of {human(disk.total)}."
    if not candidates:
        return f"🧹 Nothing to clean up: no finished job has files left. {free}"
    total = sum(n for _, n in candidates)
    store.enqueue_event({
        "event": "choice", "chat_id": chat_id,
        "text": (f"🧹 {len(candidates)} finished job(s) still hold {human(total)} of sources, work files "
                 f"and skipped clips. {free}\nApproved and unreviewed clips stay."),
        "buttons": [{"text": f"🗑 Delete {human(total)}", "data": "rm:all"}, {"text": "Keep", "data": "kp:all"}],
    })
    return None


def button(settings: Settings, store: Store, chat_id: int, data: str) -> tuple[str, Optional[str]]:
    """rm:<job> / rm:all delete files, kp:<job> / kp:all keep them."""
    action, _, target = data.partition(":")
    if action == "kp":
        return "Kept", "OK, keeping them. OpenShorts deletes its copies after 24 h anyway."
    if target == "all":
        candidates = cleanup_candidates(settings, store)
        total = sum(delete_job_files(settings, store, job) for job, _ in candidates)
        return "Deleting", f"🗑 Deleting {human(total)} from {len(candidates)} job(s)." if total else "Nothing left to delete."
    job = store.get_job(target)
    if not job or job.chat_id != chat_id:
        return "Unknown job", None
    try:
        freed = delete_job_files(settings, store, job)
    except ValueError as e:
        return "Still running", f"⚠️ {e}"
    return "Deleting", f"🗑 Deleting {human(freed)} of job {job.id}'s files." if freed else "Already gone."

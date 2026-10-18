"""Route clipbot's own log records into SQLite for the dashboard's Logs page.

Log calls can tag a record with the job or scan it belongs to:
    log.warning("...", extra={"job_id": job.id})
"""
from __future__ import annotations

import logging
import traceback

from .store import Store

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class DbLogHandler(logging.Handler):
    def __init__(self, store: Store, level: int = logging.INFO):
        super().__init__(level)
        self.store = store

    def emit(self, record: logging.LogRecord) -> None:
        try:
            detail = getattr(record, "detail", None)  # extra={"detail": ...}: e.g. OpenShorts' full log
            if record.exc_info:
                detail = "".join(traceback.format_exception(*record.exc_info))
            source = record.name.removeprefix("clipbot.").removeprefix("clipbot") or "api"
            self.store.add_log(record.created, record.levelname, source, record.getMessage(),
                               getattr(record, "job_id", None), getattr(record, "scan_id", None), detail)
        except Exception:  # never let logging break the caller
            self.handleError(record)


def install(store: Store) -> DbLogHandler:
    """Send clipbot.* INFO+ to the database and to stderr (container logs)."""
    logger = logging.getLogger("clipbot")
    logger.setLevel(logging.INFO)
    if not any(getattr(h, "_clipbot_stream", False) for h in logger.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(FORMAT))
        stream._clipbot_stream = True  # type: ignore[attr-defined]
        logger.addHandler(stream)
    handler = DbLogHandler(store)
    logger.addHandler(handler)
    return handler


def uninstall(handler: DbLogHandler) -> None:
    logging.getLogger("clipbot").removeHandler(handler)

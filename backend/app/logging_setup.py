"""Worker logging configuration — see LOGGING.md.

One place to satisfy the cross-cutting requirements: every line carries a
level and timestamp (GEN-1), lines are single-line and greppable (GEN-7),
and the `httpx` logger is pinned to WARNING so the 3s poll loop's per-request
chatter doesn't drown the signal (GEN-6). Level defaults to INFO; set
TASTER_LOG_LEVEL=DEBUG to surface payload contents and per-request detail
(GEN-4/GEN-5 — off by default because logs travel off the NAS).
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
from datetime import datetime
from pathlib import Path

# asctime defaults to an ISO-8601-equivalent local timestamp with ms — that
# satisfies GEN-1 (basicConfig's *default* format has no time, hence this).
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class _JsonLines(logging.Formatter):
    """One JSON object per line, for the admin portal's Logs tab to filter."""

    def format(self, record: logging.LogRecord) -> str:
        d = {
            "t": datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            d["exc"] = self.formatException(record.exc_info)[-1500:]
        return json.dumps(d, ensure_ascii=False)


def log_dir() -> Path:
    return Path(os.environ.get("ADMIN_DATA_DIR", "/data")) / "logs"


def setup_logging(source: str = "") -> None:
    """`source` names the process ("worker", "admin"). When given, its log is
    also written as JSON lines to <ADMIN_DATA_DIR>/logs/<source>.log, a directory
    the worker and taster-admin share — that file is what the admin portal's Logs
    tab reads, so no process needs access to the docker socket to show another's
    logs. Rotated (2 MB x 3), INFO and above only: DEBUG carries payload contents
    (GEN-4) and must not turn up in a web page."""
    level = os.environ.get("TASTER_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format=_FORMAT)
    # GEN-6: httpx logs every outbound request at INFO — dozens of identical
    # `GET /worker/jobs/next` lines a minute otherwise.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not source:
        return
    try:
        d = log_dir()
        d.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            d / f"{source}.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8",
        )
    except OSError as e:  # a missing volume must not stop the process starting
        logging.getLogger("logging").warning("no file log for %s (%s)", source, e)
        return
    handler.setLevel(logging.INFO)
    handler.setFormatter(_JsonLines())
    logging.getLogger().addHandler(handler)


def secret_state(value: str | None) -> str:
    """GEN-3/GEN-8: secrets are echoed as present/absent, never their value."""
    return "present" if value else "absent"

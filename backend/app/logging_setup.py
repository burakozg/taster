"""Worker logging configuration — see LOGGING.md.

One place to satisfy the cross-cutting requirements: every line carries a
level and timestamp (GEN-1), lines are single-line and greppable (GEN-7),
and the `httpx` logger is pinned to WARNING so the 3s poll loop's per-request
chatter doesn't drown the signal (GEN-6). Level defaults to INFO; set
TASTER_LOG_LEVEL=DEBUG to surface payload contents and per-request detail
(GEN-4/GEN-5 — off by default because logs travel off the NAS).
"""
from __future__ import annotations

import logging
import os

# asctime defaults to an ISO-8601-equivalent local timestamp with ms — that
# satisfies GEN-1 (basicConfig's *default* format has no time, hence this).
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging() -> None:
    level = os.environ.get("TASTER_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format=_FORMAT)
    # GEN-6: httpx logs every outbound request at INFO — dozens of identical
    # `GET /worker/jobs/next` lines a minute otherwise.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def secret_state(value: str | None) -> str:
    """GEN-3/GEN-8: secrets are echoed as present/absent, never their value."""
    return "present" if value else "absent"

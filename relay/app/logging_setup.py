"""Relay logging configuration — see LOGGING.md.

Cross-cutting requirements in one place: level + timestamp on every line
(GEN-1), single-line greppable format (GEN-7). The relay's own noise source
is uvicorn's access log, which would emit a line for every worker poll of
`GET /worker/jobs/next` (every few seconds, forever) — GEN-6 says a healthy
idle system should be near-silent, so those access lines are filtered out
here. Real 4xx/5xx access lines (REL-5) and every application log line are
untouched.
"""
from __future__ import annotations

import logging
import os

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class _SuppressPollAccess(logging.Filter):
    """Drop uvicorn access-log lines for the worker's job poll (GEN-6). The
    access record's rendered message contains the request path, so a simple
    substring test on the healthy, high-frequency poll endpoint is enough."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "/worker/jobs/next" not in record.getMessage()


def setup_logging() -> None:
    level = os.environ.get("TASTER_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=level, format=_FORMAT)
    logging.getLogger("uvicorn.access").addFilter(_SuppressPollAccess())


def secret_state(value: str | None) -> str:
    """GEN-3/GEN-8: secrets are echoed as present/absent, never their value."""
    return "present" if value else "absent"

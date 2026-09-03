"""Shared worker error types.

`PhaseError` tags a job failure with the integration boundary it broke at
(WRK-1: poll / claude_call / validation / couchdb_query / couchdb_write /
result_post) so the worker's job-failed line names the culprit without
anyone having to read the traceback (goal 1). Lives here rather than in
worker.py to avoid an import cycle — capture_service/lookup_service raise it,
worker.py catches it.
"""
from __future__ import annotations


def exc_label(exc: BaseException) -> str:
    """`TypeName: message`, or bare `TypeName` when there is no message.

    Several exceptions that reach a catch-all carry nothing at all in `str()` —
    httpx's ReadTimeout/ConnectTimeout are the ones seen here, and they turned
    a user-facing error into "unexpected error:" with an empty tail. The type
    name alone is enough to tell a timeout from a parse bug, which is the
    distinction that decides whether you retry or go read code.
    """
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


class PhaseError(Exception):
    def __init__(self, phase: str, original: BaseException) -> None:
        # exc_label, not str(original): wrapping a message-less exception (an
        # httpx timeout against CouchDB, say) produced a PhaseError that was
        # also message-less, so the phase tag arrived with nothing beside it.
        super().__init__(exc_label(original))
        self.phase = phase
        self.original = original

"""Shared worker error types.

`PhaseError` tags a job failure with the integration boundary it broke at
(WRK-1: poll / claude_call / validation / couchdb_query / couchdb_write /
result_post) so the worker's job-failed line names the culprit without
anyone having to read the traceback (goal 1). Lives here rather than in
worker.py to avoid an import cycle — capture_service/lookup_service raise it,
worker.py catches it.
"""
from __future__ import annotations


class PhaseError(Exception):
    def __init__(self, phase: str, original: BaseException) -> None:
        super().__init__(str(original))
        self.phase = phase
        self.original = original

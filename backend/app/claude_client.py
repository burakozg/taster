"""Shared Anthropic client factory + call-telemetry logging (WRK-2/WRK-3)."""
from __future__ import annotations

import logging
from functools import lru_cache

import anthropic

from app.config import Settings

logger = logging.getLogger("worker.claude")


@lru_cache
def _client_singleton(api_key: str | None) -> anthropic.AsyncAnthropic:
    # api_key=None lets the SDK fall back to ANTHROPIC_API_KEY / ant auth
    # profile resolution — see claude-api skill's auth precedence notes.
    return anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()


def get_claude_client(settings: Settings) -> anthropic.AsyncAnthropic:
    return _client_singleton(settings.anthropic_api_key)


def log_call_summary(
    *,
    job_id: str,
    site: str,
    model: str,
    stop_reason: str | None,
    input_tokens: int,
    output_tokens: int,
    iterations: int,
    query_notes: int,
    web_search: bool,
    duration_s: float,
) -> None:
    """WRK-2: one INFO summary line per job's Claude interaction. Token counts
    are summed across the tool loop's turns so this line is the per-job cost
    ledger entry (goal 2 — the only spend record besides the Anthropic
    Console). No payload content here (GEN-4)."""
    logger.info(
        "claude call job_id=%s site=%s model=%s stop_reason=%s input_tokens=%d "
        "output_tokens=%d iterations=%d query_notes=%d web_search=%s duration_s=%.2f",
        job_id, site, model, stop_reason, input_tokens, output_tokens,
        iterations, query_notes, web_search, duration_s,
    )


def log_call_error(*, job_id: str, site: str, model: str, exc: Exception) -> None:
    """WRK-3: on an Anthropic API error, log the HTTP status, error type, and
    the request-id support needs — never the request payload (GEN-3/GEN-4)."""
    status = getattr(exc, "status_code", None)
    request_id = getattr(exc, "request_id", None) or getattr(exc, "_request_id", None)
    error_type = None
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            error_type = err.get("type")
    logger.error(
        "claude call failed job_id=%s site=%s model=%s status=%s error_type=%s request_id=%s: %s",
        job_id, site, model, status, error_type, request_id, _exc_summary(exc),
    )


def _exc_summary(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"

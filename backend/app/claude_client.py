"""Shared Anthropic client factory + call-telemetry logging (WRK-2/WRK-3)."""
from __future__ import annotations

import logging
from functools import lru_cache

import anthropic

from app.config import Settings
from app.errors import exc_label

logger = logging.getLogger("worker.claude")


@lru_cache
def _client_singleton(api_key: str | None) -> anthropic.AsyncAnthropic:
    # api_key=None lets the SDK fall back to ANTHROPIC_API_KEY / ant auth
    # profile resolution — see claude-api skill's auth precedence notes.
    return anthropic.AsyncAnthropic(api_key=api_key) if api_key else anthropic.AsyncAnthropic()


def get_claude_client(settings: Settings) -> anthropic.AsyncAnthropic:
    return _client_singleton(settings.anthropic_api_key)


def count_web_searches(content, job_id: str, site: str) -> int:
    """How many web searches the model actually ran in one response, logging any
    that failed.

    `web_search=True` on the summary line only ever meant "the tool was
    attached", which is the wrong half of the question when the complaint is
    "it isn't looking things up" — an attached tool the model never calls and
    an attached tool that errors both log identically. Searches are server-side,
    so they appear as `server_tool_use` blocks in the response.

    Server-tool failures do NOT raise: they come back HTTP 200 with the result
    block's `content` set to a single error object (e.g. `max_uses_exceeded`)
    instead of the usual list of results. Silent on every path unless something
    looks, so this looks.
    """
    searches = 0
    for block in content:
        btype = getattr(block, "type", None)
        if btype == "server_tool_use" and getattr(block, "name", None) == "web_search":
            searches += 1
        elif btype == "web_search_tool_result":
            result = getattr(block, "content", None)
            # Success is a list of results; an error is a bare object.
            error_code = getattr(result, "error_code", None)
            if error_code is None and isinstance(result, dict):
                error_code = result.get("error_code")
            if error_code:
                logger.warning(
                    "web search failed job_id=%s site=%s error_code=%s",
                    job_id, site, error_code,
                )
    return searches


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
    web_searches: int | None = None,
) -> None:
    """WRK-2: one INFO summary line per job's Claude interaction. Token counts
    are summed across the tool loop's turns so this line is the per-job cost
    ledger entry (goal 2 — the only spend record besides the Anthropic
    Console). No payload content here (GEN-4).

    `web_search` is whether the tool was attached; `web_searches` is how many
    the model actually ran (omitted on paths that can't count them). The two
    together are what distinguish "search is off" from "search is on and the
    model declined to use it" — see count_web_searches.
    """
    logger.info(
        "claude call job_id=%s site=%s model=%s stop_reason=%s input_tokens=%d "
        "output_tokens=%d iterations=%d query_notes=%d web_search=%s "
        "web_searches=%s duration_s=%.2f",
        job_id, site, model, stop_reason, input_tokens, output_tokens,
        iterations, query_notes, web_search,
        "?" if web_searches is None else web_searches, duration_s,
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
        job_id, site, model, status, error_type, request_id, exc_label(exc),
    )


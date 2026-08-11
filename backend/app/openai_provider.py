"""OpenAI provider path (Responses API) — used when the admin panel picks a
gpt-* model. Mirrors the Claude path's shape: one call with vision (photo
captures), server-side web search, the query_notes function tool, and a
JSON-schema-nudged output that schema.py's Pydantic layer then enforces.

Status: live-tested up to request acceptance — dispatch, auth, and the
schema format have hit the real API (first attempt 400'd because strict
defaults to true on the Responses API; fixed by passing strict: False).
A full successful capture/lookup round-trip is still pending.
Requires OPENAI_API_KEY in the worker's .env; a gpt-* model selected
without it fails the job with an explicit message rather than a crash.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from app import usage
from app.config import Settings
from app.couchdb_client import CouchDBClient
from app.model_output import loads_model_json, no_text_output, truncated
from app.tools import QUERY_NOTES_TOOL, query_notes_impl

logger = logging.getLogger(__name__)

# config.yaml effort levels → OpenAI reasoning effort.
_EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def is_openai_model(model: str) -> bool:
    return model.startswith("gpt-")


_client = None


def get_openai_client(settings: Settings):
    global _client
    if _client is None:
        if not settings.openai_api_key:
            raise RuntimeError(
                "an OpenAI model is selected in the admin panel but OPENAI_API_KEY "
                "is not set in the worker's .env — add it (and recreate the worker) "
                "or switch back to a Claude model"
            )
        from openai import AsyncOpenAI  # imported lazily; only needed on this path

        _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


def _query_notes_tool() -> dict[str, Any]:
    # Responses API function tools are flat (no nested "function" wrapper).
    return {
        "type": "function",
        "name": QUERY_NOTES_TOOL["name"],
        "description": QUERY_NOTES_TOOL["description"],
        "parameters": QUERY_NOTES_TOOL["input_schema"],
    }


async def _run_tool_loop(
    client,
    *,
    job_id: str,
    site: str,
    model: str,
    effort: str,
    max_output_tokens: int,
    system_prompt: str,
    db: CouchDBClient,
    text: str,
    image_b64: str | None,
    image_media_type: str | None,
    use_web_search: bool,
    output_schema: dict | None,
    max_iterations: int,
):
    content: list[dict[str, Any]] = []
    if image_b64:
        content.append({
            "type": "input_image",
            "image_url": f"data:{image_media_type or 'image/jpeg'};base64,{image_b64}",
        })
    content.append({"type": "input_text", "text": text})

    tools: list[dict[str, Any]] = [_query_notes_tool()]
    if use_web_search:
        tools.append({"type": "web_search"})

    kwargs: dict[str, Any] = {}
    if output_schema is not None:
        # Non-strict on purpose — strict mode requires every property to be
        # required (our optional fields and either-or pairing suggestions
        # don't fit that), and the Responses API defaults strict to true, so
        # it must be disabled explicitly. Pydantic is the real enforcement
        # layer, same philosophy as the Claude path.
        kwargs["text"] = {
            "format": {
                "type": "json_schema",
                "name": "tasting_note",
                "strict": False,
                "schema": output_schema,
            }
        }

    input_list: list[Any] = [{"role": "user", "content": content}]

    # Cost-ledger counters — the OpenAI analogue of the Claude WRK-2 line, so
    # gpt-* captures/lookups leave a summable spend record too (goal 2).
    iterations = query_notes_calls = input_tokens = output_tokens = 0
    started = time.monotonic()

    def log_summary() -> None:
        logger.info(
            "openai call job_id=%s site=%s model=%s input_tokens=%d output_tokens=%d "
            "iterations=%d query_notes=%d web_search=%s duration_s=%.2f",
            job_id, site, model, input_tokens, output_tokens, iterations,
            query_notes_calls, use_web_search, time.monotonic() - started,
        )

    for _ in range(max_iterations):
        iterations += 1
        response = await client.responses.create(
            model=model,
            instructions=system_prompt,
            input=input_list,
            tools=tools,
            reasoning={"effort": _EFFORT_MAP.get(effort, "medium")},
            max_output_tokens=max_output_tokens,
            **kwargs,
        )
        if (call_usage := getattr(response, "usage", None)) is not None:
            call_in = getattr(call_usage, "input_tokens", 0) or 0
            call_out = getattr(call_usage, "output_tokens", 0) or 0
            input_tokens += call_in
            output_tokens += call_out
            usage.record("openai", model, call_in, call_out)

        calls = [item for item in response.output if item.type == "function_call"]
        if not calls:
            log_summary()
            return response

        # Echo the model's output (function_call + any reasoning items) back,
        # then append one output per call — the Responses API loop pattern.
        input_list += response.output
        for call in calls:
            query_notes_calls += 1
            result = await query_notes_impl(db, json.loads(call.arguments))
            input_list.append({
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": json.dumps(result),
            })

    log_summary()
    raise RuntimeError("exceeded max tool-use iterations without a final answer (openai)")


async def extract_structured(
    settings: Settings,
    db: CouchDBClient,
    *,
    job_id: str,
    model: str,
    system_prompt: str,
    text: str,
    image_b64: str | None,
    image_media_type: str | None,
    use_web_search: bool,
    output_schema: dict,
    site: str = "capture",
    max_output_tokens: int | None = None,
) -> dict:
    """Capture/manage path: returns the raw structured dict (validated by caller)."""
    cfg = settings.models.claude
    client = get_openai_client(settings)
    response = await _run_tool_loop(
        client,
        job_id=job_id,
        site=site,
        model=model,
        effort=cfg.effort,
        max_output_tokens=max_output_tokens or cfg.max_tokens_capture,
        system_prompt=system_prompt,
        db=db,
        text=text,
        image_b64=image_b64,
        image_media_type=image_media_type,
        use_web_search=use_web_search,
        output_schema=output_schema,
        max_iterations=cfg.max_tool_iterations,
    )
    raw = response.output_text
    # Reasoning tokens are spent from max_output_tokens first, so an
    # under-budgeted request truncates the JSON. Detection is OpenAI-specific;
    # the wording comes from app.model_output so all three paths match.
    reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
    if reason == "max_output_tokens" or getattr(response, "status", None) == "incomplete":
        raise truncated("openai", "max_tokens_capture")
    if not raw:
        raise no_text_output("openai", reason)
    return loads_model_json(raw, "openai")


async def answer_question(
    settings: Settings,
    db: CouchDBClient,
    *,
    job_id: str,
    model: str,
    system_prompt: str,
    question: str,
    image_b64: str | None,
    image_media_type: str | None,
) -> str:
    """Lookup path: returns the conversational answer text."""
    cfg = settings.models.claude
    client = get_openai_client(settings)
    response = await _run_tool_loop(
        client,
        job_id=job_id,
        site="lookup",
        model=model,
        effort=cfg.effort,
        max_output_tokens=cfg.max_tokens_lookup,
        system_prompt=system_prompt,
        db=db,
        text=question,
        image_b64=image_b64,
        image_media_type=image_media_type,
        use_web_search=cfg.web_search_lookup,
        output_schema=None,
        max_iterations=cfg.max_tool_iterations,
    )
    return response.output_text or ""

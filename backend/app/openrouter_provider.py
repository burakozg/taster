"""OpenRouter provider path — used when the admin panel picks a namespaced
`vendor/model` id (e.g. `google/gemini-3.6-flash`). Mirrors the Claude/OpenAI/
Mistral paths' shape: one logical call with vision (photo captures), web search,
the query_notes function tool, and a JSON-schema-nudged output that schema.py's
Pydantic layer then enforces.

Why a fourth provider at all: OpenRouter is a router, not a lab. One key reaches
Google, xAI, Moonshot, Qwen and the rest — model families this app otherwise
can't offer without a third-party account and a fourth SDK each. The models
already covered by a direct path (Anthropic, OpenAI, Mistral) are deliberately
NOT listed in the catalog through OpenRouter: routing them here would only add
a hop and a markup.

Surface: OpenRouter speaks the OpenAI **chat completions** API at
`https://openrouter.ai/api/v1`, so this path reuses the `openai` SDK that is
already a dependency — just pointed at a different base_url. Not the Responses
API that openai_provider.py uses; OpenRouter's Responses support is newer and
narrower than its chat surface, and chat completions is what every model behind
the router implements.

Two OpenRouter-specific request extras, both passed through `extra_body`:

- **web search** — the `web` plugin runs search server-side and injects results,
  so it works uniformly across models rather than depending on each one's native
  search. Its default `engine: "auto"` uses the underlying provider's built-in
  search where there is one (Gemini, Grok) and Exa otherwise. Billed per result
  on top of tokens, so `max_results` is tied to the same `web_search_max_uses`
  budget the Claude path caps its searches with.
- **reasoning effort** — OpenRouter normalises `reasoning: {"effort": ...}`
  across model families. Models with no reasoning mode ignore it: OpenRouter
  drops parameters a model doesn't support rather than erroring, which is why
  this is sent unconditionally instead of maintained as a per-model table.

Status: implemented, NOT yet verified against the live OpenRouter API (no key in
dev) — the same status the Mistral path carries. The model ids in the relay's
catalog, however, ARE live-verified (see models_catalog.py). Requires
OPENROUTER_API_KEY in the worker's .env; an OpenRouter model selected without it
fails the job with an explicit message rather than a crash.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from app import usage
from app.config import Settings
from app.couchdb_client import CouchDBClient
from app.model_output import ModelOutputError, loads_model_json, no_text_output, truncated
from app.tools import QUERY_NOTES_TOOL, query_notes_impl

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# config.yaml effort levels → OpenRouter's normalised reasoning effort.
_EFFORT_MAP = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def is_openrouter_model(model: str) -> bool:
    # Every OpenRouter id is namespaced `vendor/model`, and no other provider's
    # ids contain a slash — so this is the whole discriminator. It is checked
    # FIRST in providers.py for the same reason: `openai/gpt-5.1` and
    # `mistralai/mistral-large-2512` are OpenRouter ids that must not be
    # mistaken for direct-path ones (they aren't today — the direct checks are
    # `startswith` — but a prefix check that ever loosens shouldn't silently
    # steal traffic from here).
    return "/" in model


_client = None


def get_openrouter_client(settings: Settings):
    global _client
    if _client is None:
        if not settings.openrouter_api_key:
            raise RuntimeError(
                "an OpenRouter model is selected in the admin panel but "
                "OPENROUTER_API_KEY is not set in the worker's .env — add it "
                "(and recreate the worker) or switch back to a Claude model"
            )
        from openai import AsyncOpenAI  # imported lazily; only needed on this path

        _client = AsyncOpenAI(
            api_key=settings.openrouter_api_key,
            base_url=OPENROUTER_BASE_URL,
            # Optional attribution headers OpenRouter reads for its app
            # rankings. Harmless, and they make this worker's spend
            # identifiable in the OpenRouter activity log.
            default_headers={"X-Title": "Tasting Log"},
        )
    return _client


def _query_notes_function_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": QUERY_NOTES_TOOL["name"],
            "description": QUERY_NOTES_TOOL["description"],
            "parameters": QUERY_NOTES_TOOL["input_schema"],
        },
    }


def _count_citations(message: Any) -> int:
    """How many web results the `web` plugin actually injected into this turn.

    OpenRouter's plugin is not a tool the model chooses to call — it searches
    and injects results into the prompt, then reports what it used as
    `message.annotations` entries of type "url_citation". So this is the ONLY
    signal that distinguishes "search ran and the model ignored the results"
    from "search never happened", and without it the log line below reported
    `web_search=True` on every call purely because the flag was set — true by
    construction, and worthless for diagnosing an empty `common_notes`.
    """
    annotations = getattr(message, "annotations", None) or []
    count = 0
    for a in annotations:
        kind = getattr(a, "type", None) or (a.get("type") if isinstance(a, dict) else None)
        if kind == "url_citation":
            count += 1
    return count


def _user_content(text: str, image_b64: str | None, image_media_type: str | None) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    if image_b64:
        # Nested {"url": ...} — the OpenAI chat shape. (Mistral's own API takes
        # a bare string here; that difference is why the two paths don't share
        # this helper.)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{image_media_type or 'image/jpeg'};base64,{image_b64}"},
        })
    content.append({"type": "text", "text": text})
    return content


async def _run_tool_loop(
    client,
    *,
    job_id: str,
    site: str,
    model: str,
    effort: str,
    max_tokens: int,
    system_prompt: str,
    db: CouchDBClient,
    text: str,
    image_b64: str | None,
    image_media_type: str | None,
    use_web_search: bool,
    web_search_max_results: int,
    output_schema: dict | None,
    max_iterations: int,
) -> str:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": _user_content(text, image_b64, image_media_type)},
    ]
    tools = [_query_notes_function_tool()]

    extra_body: dict[str, Any] = {"reasoning": {"effort": _EFFORT_MAP.get(effort, "medium")}}
    if use_web_search:
        extra_body["plugins"] = [{"id": "web", "max_results": web_search_max_results}]

    kwargs: dict[str, Any] = {}
    if output_schema is not None:
        # Non-strict, as on the OpenAI and Mistral paths: strict mode requires
        # every property to be required, which our optional fields don't fit.
        # Pydantic is the real enforcement layer.
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "tasting_note", "schema": output_schema, "strict": False},
        }

    iterations = query_notes_calls = input_tokens = output_tokens = citations = 0
    started = time.monotonic()

    def log_summary() -> None:
        logger.info(
            "openrouter call job_id=%s site=%s model=%s input_tokens=%d output_tokens=%d "
            "iterations=%d query_notes=%d web_search=%s web_citations=%d duration_s=%.2f",
            job_id, site, model, input_tokens, output_tokens, iterations,
            query_notes_calls, use_web_search, citations, time.monotonic() - started,
        )

    for _ in range(max_iterations):
        iterations += 1
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            extra_body=extra_body,
            **kwargs,
        )
        if (call_usage := getattr(response, "usage", None)) is not None:
            call_in = getattr(call_usage, "prompt_tokens", 0) or 0
            call_out = getattr(call_usage, "completion_tokens", 0) or 0
            input_tokens += call_in
            output_tokens += call_out
            usage.record("openrouter", model, call_in, call_out)

        # OpenRouter can answer 200 with no `choices` and an `error` object
        # instead — an upstream provider refusing or falling over mid-route.
        # Reaching for choices[0] there raises a bare IndexError/TypeError far
        # from the cause, so it's named here as unusable output.
        if not getattr(response, "choices", None):
            err = getattr(response, "error", None) or {}
            detail = err.get("message") if isinstance(err, dict) else str(err)
            raise no_text_output("openrouter", detail or "no choices returned")

        choice = response.choices[0]
        message = choice.message
        citations += _count_citations(message)
        tool_calls = getattr(message, "tool_calls", None) or []

        # Reasoning tokens come out of the same max_tokens ceiling, so an
        # under-budgeted request truncates. Capture needs complete JSON so it
        # fails; lookup keeps the partial answer with a marker, matching the
        # Claude and Mistral lookup paths.
        if getattr(choice, "finish_reason", None) == "length":
            logger.warning(
                "openrouter %s truncated job_id=%s at max_tokens=%d", site, job_id, max_tokens,
            )
            if site == "lookup":
                return f"{message.content or ''}\n\n[cut off — raise max_tokens_lookup in config.yaml]"
            raise truncated("openrouter", "max_tokens_capture")

        if not tool_calls:
            log_summary()
            content = (message.content or "").strip()
            if not content:
                # Reasoning models on this router split their turn: chain of
                # thought into `reasoning`, answer into `content`. When the
                # answer never lands in `content`, falling through with "" gave
                # a JSON parse error about column 1 of an empty string — which
                # looks like the model said nothing, while its actual output sat
                # unread in the other field. Prefer content; fall back rather
                # than discard.
                reasoning = (getattr(message, "reasoning", None) or "").strip()
                if reasoning:
                    logger.warning(
                        "openrouter %s job_id=%s: empty content, falling back to "
                        "reasoning field (%d chars)", site, job_id, len(reasoning),
                    )
                    return reasoning
                logger.warning(
                    "openrouter %s job_id=%s: model returned neither content nor "
                    "reasoning (finish_reason=%s)",
                    site, job_id, getattr(choice, "finish_reason", None),
                )
            return content

        messages.append({
            "role": "assistant",
            "content": message.content or "",
            "tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in tool_calls
            ],
        })
        for tc in tool_calls:
            name = tc.function.name
            if name != QUERY_NOTES_TOOL["name"]:
                # query_notes is the only tool declared; anything else is the
                # model inventing one. Answer it as an error rather than
                # silently handing back vault rows it never asked for.
                logger.warning(
                    "openrouter %s job_id=%s: unknown tool %r requested", site, job_id, name,
                )
                messages.append({
                    "role": "tool", "tool_call_id": tc.id, "name": name,
                    "content": json.dumps({"error": f"no such tool: {name}"}),
                })
                continue
            query_notes_calls += 1
            args = tc.function.arguments
            if isinstance(args, str):
                args = json.loads(args or "{}")
            result = await query_notes_impl(db, args or {})
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "name": name, "content": json.dumps(result),
            })

    log_summary()
    # ModelOutputError, not a bare RuntimeError: this is "the model ran but gave
    # no usable answer", which the job handlers report as a clean failure. As a
    # RuntimeError it reached the PWA as "unexpected error: ..." — the same
    # asymmetry model_output.py exists to remove.
    raise ModelOutputError(
        f"the model kept calling tools and never produced an answer "
        f"({max_iterations} iterations) — raise max_tool_iterations in "
        f"config.yaml, or narrow the instruction [openrouter]"
    )


# --------------------------------------------------------------------------
# Public API (same signatures as openai_provider / mistral_provider)
# --------------------------------------------------------------------------

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
    client = get_openrouter_client(settings)
    raw = await _run_tool_loop(
        client,
        job_id=job_id,
        site=site,
        model=model,
        effort=cfg.effort,
        max_tokens=max_output_tokens or cfg.max_tokens_capture,
        system_prompt=system_prompt,
        db=db,
        text=text,
        image_b64=image_b64,
        image_media_type=image_media_type,
        use_web_search=use_web_search,
        web_search_max_results=cfg.web_search_max_uses,
        output_schema=output_schema,
        max_iterations=cfg.max_tool_iterations,
    )
    if not raw:
        raise no_text_output("openrouter")
    return loads_model_json(raw, "openrouter")


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
    client = get_openrouter_client(settings)
    return await _run_tool_loop(
        client,
        job_id=job_id,
        site="lookup",
        model=model,
        effort=cfg.effort,
        max_tokens=cfg.max_tokens_lookup,
        system_prompt=system_prompt,
        db=db,
        text=question,
        image_b64=image_b64,
        image_media_type=image_media_type,
        use_web_search=cfg.web_search_lookup,
        web_search_max_results=cfg.web_search_max_uses,
        output_schema=None,
        max_iterations=cfg.max_tool_iterations,
    )

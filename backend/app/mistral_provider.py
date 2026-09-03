"""Mistral provider path — used when the admin panel picks a mistral-*/pixtral-*
model. Mirrors the Claude/OpenAI path's shape: one logical call with vision
(photo captures), web search, the query_notes function tool, and a
JSON-schema-nudged output that schema.py's Pydantic layer then enforces.

Two Mistral surfaces are used, with automatic fallback:

- **Agents / Conversations API** (primary) — `client.beta.conversations` runs the
  built-in `web_search` connector server-side alongside our `query_notes`
  function tool. This is the ONLY Mistral surface with web search, so it's how
  `common_notes`/gap fields get real grounding. Agentless: we pass model + tools
  per request, so there's no persistent agent to create on the Mistral portal.
- **Chat completions** (fallback) — `client.chat.complete_async`, NO web search.
  Used automatically if the Conversations call raises (older SDK, unsupported
  `response_format`, shape drift), so a capture/lookup never hard-fails on the
  newer API — it just degrades to no-web-search.

Status: implemented, NOT yet verified against the live Mistral API (no key in
dev). The Conversations-API request/response shape below is written to Mistral's
Agents API but its exact SDK field names must be confirmed on the first real
call; if they're off, the fallback keeps the app working (minus web search) and
the mismatch is logged. Requires MISTRAL_API_KEY in the worker's .env; a
mistral-* model selected without it fails the job with an explicit message.
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


def is_mistral_model(model: str) -> bool:
    # `ministral-*` (Ministral 3) is NOT a typo for `mistral-*` — it's a
    # separate family on the same API, and it does not match the `mistral-`
    # prefix. Missing it here routes Ministral ids into the Claude branch.
    # `pixtral-*` is retired and gone from the relay's catalog, but stays
    # matched on purpose: a stale stored override should reach this provider
    # and fail with Mistral's own `invalid_model` 400, not surface as a
    # baffling Anthropic 404.
    return model.startswith(("mistral-", "ministral-", "pixtral-"))


_client = None


def get_mistral_client(settings: Settings):
    global _client
    if _client is None:
        if not settings.mistral_api_key:
            raise RuntimeError(
                "a Mistral model is selected in the admin panel but MISTRAL_API_KEY "
                "is not set in the worker's .env — add it (and recreate the worker) "
                "or switch back to a Claude model"
            )
        try:
            # mistralai 2.x moved the client into the `mistralai.client`
            # subpackage. Top-level `mistralai` is now a namespace package with
            # no __init__.py, so the 1.x spelling (`from mistralai import
            # Mistral`) fails with ImportError "cannot import name 'Mistral'
            # from 'mistralai' (unknown location)" — which reads like a broken
            # install but is just the rename. requirements.txt pins >=2.0.
            from mistralai.client import Mistral  # imported lazily; only needed on this path
        except ImportError as e:
            raise RuntimeError(
                "the Mistral SDK isn't importable in this worker build "
                f"({type(e).__name__}: {e}) — expected mistralai>=2.0 exposing "
                "`mistralai.client.Mistral`; rebuild the worker "
                "(./deploy), or switch the model back to a Claude/OpenAI "
                "one in Admin → Models"
            ) from e

        _client = Mistral(api_key=settings.mistral_api_key)
    return _client


def _query_notes_function_tool() -> dict[str, Any]:
    # OpenAI-compatible function-tool shape (used by both Mistral surfaces).
    return {
        "type": "function",
        "function": {
            "name": QUERY_NOTES_TOOL["name"],
            "description": QUERY_NOTES_TOOL["description"],
            "parameters": QUERY_NOTES_TOOL["input_schema"],
        },
    }


def _user_content(text: str, image_b64: str | None, image_media_type: str | None) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    if image_b64:
        content.append({
            "type": "image_url",
            "image_url": f"data:{image_media_type or 'image/jpeg'};base64,{image_b64}",
        })
    content.append({"type": "text", "text": text})
    return content


# --------------------------------------------------------------------------
# Conversations API (web search) — the primary path
# --------------------------------------------------------------------------

def _outputs_of(resp) -> list[Any]:
    # SDK has varied between `.outputs` and `.output`; accept either.
    return getattr(resp, "outputs", None) or getattr(resp, "output", None) or []


def _output_type(o) -> str:
    return getattr(o, "type", None) or (o.get("type") if isinstance(o, dict) else "") or ""


def _get(o, key):
    return getattr(o, key, None) if not isinstance(o, dict) else o.get(key)


def _message_text(outputs: list[Any]) -> str:
    """Concatenate the text of the assistant message output(s). Content may be a
    plain string or a list of {type:text, text} chunks."""
    parts: list[str] = []
    for o in outputs:
        if _output_type(o) not in ("message.output", "message", "assistant"):
            continue
        content = _get(o, "content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for chunk in content:
                t = _get(chunk, "text")
                if isinstance(t, str):
                    parts.append(t)
    return "".join(parts)


async def _run_conversation_loop(
    client,
    *,
    job_id: str,
    site: str,
    model: str,
    max_tokens: int,
    system_prompt: str,
    db: CouchDBClient,
    text: str,
    image_b64: str | None,
    image_media_type: str | None,
    response_format: dict | None,
    max_iterations: int,
    use_web_search: bool = True,
) -> str:
    """Agentless Conversations run: web_search connector + query_notes function
    tool. Returns the final assistant text. Raises on any API/shape problem so
    the caller can fall back to chat completions.

    `use_web_search` exists so the connector is a per-call decision here as it
    already is on the other two providers — it used to be hardcoded on, which
    is why lookup searched on Mistral and nowhere else."""
    tools: list[dict[str, Any]] = [_query_notes_function_tool()]
    if use_web_search:
        tools.insert(0, {"type": "web_search"})
    completion_args: dict[str, Any] = {"max_tokens": max_tokens}
    if response_format is not None:
        completion_args["response_format"] = response_format

    input_tokens = output_tokens = query_notes_calls = 0
    started = time.monotonic()

    def add_usage(resp) -> None:
        nonlocal input_tokens, output_tokens
        call_usage = getattr(resp, "usage", None)
        if call_usage is not None:
            call_in = getattr(call_usage, "prompt_tokens", 0) or 0
            call_out = getattr(call_usage, "completion_tokens", 0) or 0
            input_tokens += call_in
            output_tokens += call_out
            usage.record("mistral", model, call_in, call_out)

    resp = await client.beta.conversations.start_async(
        model=model,
        instructions=system_prompt,
        inputs=[{"role": "user", "content": _user_content(text, image_b64, image_media_type)}],
        tools=tools,
        completion_args=completion_args,
    )
    conversation_id = getattr(resp, "conversation_id", None)
    add_usage(resp)

    for iteration in range(max_iterations):
        outputs = _outputs_of(resp)
        calls = [o for o in outputs if _output_type(o) in ("function.call", "tool.call")]
        if not calls:
            logger.info(
                "mistral call job_id=%s site=%s model=%s surface=conversations input_tokens=%d "
                "output_tokens=%d iterations=%d query_notes=%d web_search=True duration_s=%.2f",
                job_id, site, model, input_tokens, output_tokens, iteration + 1,
                query_notes_calls, time.monotonic() - started,
            )
            return _message_text(outputs)

        results = []
        for c in calls:
            query_notes_calls += 1
            args = _get(c, "arguments")
            if isinstance(args, str):
                args = json.loads(args or "{}")
            result = await query_notes_impl(db, args or {})
            results.append({
                "type": "function.result",
                "tool_call_id": _get(c, "tool_call_id") or _get(c, "id"),
                "result": json.dumps(result),
            })
        resp = await client.beta.conversations.append_async(
            conversation_id=conversation_id,
            inputs=results,
            tools=tools,
            completion_args=completion_args,
        )
        add_usage(resp)

    raise RuntimeError("exceeded max tool-use iterations without a final answer (mistral conversations)")


# --------------------------------------------------------------------------
# Chat completions (no web search) — the fallback path
# --------------------------------------------------------------------------

async def _run_chat_loop(
    client,
    *,
    job_id: str,
    site: str,
    model: str,
    max_tokens: int,
    system_prompt: str,
    db: CouchDBClient,
    text: str,
    image_b64: str | None,
    image_media_type: str | None,
    output_schema: dict | None,
    max_iterations: int,
) -> str:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": _user_content(text, image_b64, image_media_type)},
    ]
    tools = [_query_notes_function_tool()]

    kwargs: dict[str, Any] = {}
    if output_schema is not None:
        kwargs["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "tasting_note", "schema": output_schema, "strict": False},
        }

    iterations = query_notes_calls = input_tokens = output_tokens = 0
    started = time.monotonic()

    for _ in range(max_iterations):
        iterations += 1
        response = await client.chat.complete_async(
            model=model, messages=messages, tools=tools, max_tokens=max_tokens, **kwargs,
        )
        if (call_usage := getattr(response, "usage", None)) is not None:
            call_in = getattr(call_usage, "prompt_tokens", 0) or 0
            call_out = getattr(call_usage, "completion_tokens", 0) or 0
            input_tokens += call_in
            output_tokens += call_out
            usage.record("mistral", model, call_in, call_out)

        choice = response.choices[0]
        message = choice.message
        tool_calls = getattr(message, "tool_calls", None) or []

        # Chat completions is OpenAI-shaped, so a truncated answer arrives as
        # finish_reason "length" (getattr-guarded — the Conversations surface
        # above doesn't carry the field). Capture needs complete JSON so it
        # fails; lookup keeps the partial answer with a marker, matching what
        # the Claude lookup path does.
        if getattr(choice, "finish_reason", None) == "length":
            logger.warning(
                "mistral %s truncated job_id=%s at max_tokens=%d", site, job_id, max_tokens,
            )
            if site == "lookup":
                return f"{message.content or ''}\n\n[cut off — raise max_tokens_lookup in config.yaml]"
            raise truncated("mistral", "max_tokens_capture")
        if not tool_calls:
            logger.info(
                "mistral call job_id=%s site=%s model=%s surface=chat input_tokens=%d "
                "output_tokens=%d iterations=%d query_notes=%d web_search=False duration_s=%.2f",
                job_id, site, model, input_tokens, output_tokens, iterations,
                query_notes_calls, time.monotonic() - started,
            )
            return message.content or ""

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
            query_notes_calls += 1
            args = tc.function.arguments
            if isinstance(args, str):
                args = json.loads(args or "{}")
            result = await query_notes_impl(db, args)
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "name": tc.function.name, "content": json.dumps(result),
            })

    raise RuntimeError("exceeded max tool-use iterations without a final answer (mistral chat)")


# --------------------------------------------------------------------------
# Public API (same signatures as openai_provider)
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
    """Capture/manage path: returns the raw structured dict (validated by caller).
    Tries the Conversations API (web search) when requested, falling back to
    chat completions (no web search) on any Conversations-API problem."""
    cfg = settings.models.claude
    client = get_mistral_client(settings)
    max_tokens = max_output_tokens or cfg.max_tokens_capture

    raw: str | None = None
    if use_web_search:
        try:
            raw = await _run_conversation_loop(
                client, job_id=job_id, site=site, model=model, max_tokens=max_tokens,
                system_prompt=system_prompt, db=db, text=text,
                image_b64=image_b64, image_media_type=image_media_type,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "tasting_note", "schema": output_schema, "strict": False},
                },
                max_iterations=cfg.max_tool_iterations,
            )
        except ModelOutputError:
            # The Conversations call worked; its *output* was unusable. Retrying
            # on chat would just re-run the same model against the same prompt,
            # so report it instead of masking it as a web-search downgrade.
            raise
        except Exception as e:  # noqa: BLE001 — degrade to chat rather than fail the capture
            logger.warning(
                "mistral conversations path failed job_id=%s (%s) — falling back to "
                "chat completions without web search", job_id, e,
            )
            raw = None
    if raw is None:
        raw = await _run_chat_loop(
            client, job_id=job_id, site=site, model=model, max_tokens=max_tokens,
            system_prompt=system_prompt, db=db, text=text,
            image_b64=image_b64, image_media_type=image_media_type,
            output_schema=output_schema, max_iterations=cfg.max_tool_iterations,
        )

    if not raw:
        raise no_text_output("mistral")
    return loads_model_json(raw, "mistral")


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
    """Lookup path: returns the conversational answer text. Tries the
    Conversations API (web search) first, falling back to chat completions."""
    cfg = settings.models.claude
    client = get_mistral_client(settings)
    try:
        return await _run_conversation_loop(
            client, job_id=job_id, site="lookup", model=model, max_tokens=cfg.max_tokens_lookup,
            system_prompt=system_prompt, db=db, text=question,
            image_b64=image_b64, image_media_type=image_media_type,
            response_format=None, max_iterations=cfg.max_tool_iterations,
            use_web_search=cfg.web_search_lookup,
        )
    except ModelOutputError:
        raise  # unusable output, not a broken surface — see extract_structured
    except Exception as e:  # noqa: BLE001 — degrade to chat rather than fail the lookup
        logger.warning(
            "mistral conversations path failed job_id=%s (%s) — falling back to "
            "chat completions without web search", job_id, e,
        )
        return await _run_chat_loop(
            client, job_id=job_id, site="lookup", model=model, max_tokens=cfg.max_tokens_lookup,
            system_prompt=system_prompt, db=db, text=question,
            image_b64=image_b64, image_media_type=image_media_type,
            output_schema=None, max_iterations=cfg.max_tool_iterations,
        )

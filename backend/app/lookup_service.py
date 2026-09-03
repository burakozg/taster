"""Lookup: query_notes tool loop over the vault, natural-language answer.
Covers both text lookups and shop-mode (photo) lookups — see §4.5.

Uses the same manual tool-loop shape as capture_service.py (rather than the
SDK's beta tool runner) so there's one pattern to reason about across both
call sites; the design doc's mention of the tool runner is an equally valid
alternative implementation, not a requirement.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import anthropic

from app import usage
from app.claude_client import count_web_searches, get_claude_client, log_call_error, log_call_summary
from app.config import Settings
from app.couchdb_client import CouchDBClient
from app.errors import PhaseError
from app.providers import provider_for
from app.tools import QUERY_NOTES_TOOL, query_notes_impl

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You answer questions about a personal whisky/cigar/coffee/pipe-tobacco/beer/chocolate/rakı tasting vault \
using the query_notes tool. Never guess at vault contents — always query \
before answering a question that depends on prior tastings.

If the user provides a photo (shop mode: they're standing in front of a \
bottle deciding whether to buy it), identify the item from the image first, \
then query_notes by name/producer to check whether it's been tasted before \
and to surface similar highly-rated items in the same region/category.

query_notes is the source of truth for what the user owns and has tasted — \
never answer from web_search where query_notes can answer. Use web_search \
only for facts the vault cannot hold: identifying an unfamiliar label, or \
the character of something not tasted yet (typical in shop mode). Keep it to \
1-2 searches, and say which part of the answer came from the web rather than \
from their own notes.

For pairing questions ("what should I have with this?"), prefer tried \
pairings (query_notes with type: "pairing", filtering on the relevant item's \
id if known) over generic `pairings_suggested` entries on item notes — and \
say clearly which kind of answer you're giving ("you've actually tried..." \
vs "no tried pairing on record, but this note suggested...").

Keep answers short and conversational — this is a chat reply, not a report.
"""


class LookupResult:
    def __init__(self, answer: str) -> None:
        self.answer = answer


async def run_lookup(
    lookup_id: str,
    settings: Settings,
    db: CouchDBClient,
    *,
    question: str,
    image_b64: str | None = None,
    image_media_type: str | None = None,
    model_override: str | None = None,
) -> LookupResult:
    claude_cfg = settings.models.claude
    # Admin-panel choice (delivered per job by the relay) wins over the baked-in
    # config.yaml default. Non-Claude ids route to an alternate provider module
    # — see providers.py for the id → provider table.
    model = model_override or claude_cfg.text_model
    if (provider := provider_for(model)) is not None:
        answer = await provider.answer_question(
            settings, db,
            job_id=lookup_id,
            model=model,
            system_prompt=SYSTEM_PROMPT,
            question=question,
            image_b64=image_b64,
            image_media_type=image_media_type,
        )
        return LookupResult(answer=answer)

    client = get_claude_client(settings)

    user_content: list[dict[str, Any]] = []
    if image_b64:
        user_content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": image_media_type or "image/jpeg", "data": image_b64},
        })
    user_content.append({"type": "text", "text": question})

    messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
    tools: list[dict[str, Any]] = [QUERY_NOTES_TOOL]
    if claude_cfg.web_search_lookup:
        tools.append({
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": claude_cfg.web_search_max_uses,
        })

    iterations = query_notes_calls = input_tokens = output_tokens = web_searches = 0
    stop_reason: str | None = None
    # web_search_20260209 filters results by running code server-side, which
    # provisions a container that must be named on every later turn — same
    # requirement the capture loop hit. Only reachable now that lookup can
    # search; before this it was structurally impossible here.
    container_id: str | None = None
    started = time.monotonic()
    summary_logged = False

    def emit_summary() -> None:
        nonlocal summary_logged
        if summary_logged:
            return
        summary_logged = True
        log_call_summary(
            job_id=lookup_id, site="lookup", model=model, stop_reason=stop_reason,
            input_tokens=input_tokens, output_tokens=output_tokens, iterations=iterations,
            query_notes=query_notes_calls, web_search=claude_cfg.web_search_lookup,
            web_searches=web_searches, duration_s=time.monotonic() - started,
        )

    for _ in range(claude_cfg.max_tool_iterations):
        iterations += 1
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=claude_cfg.max_tokens_lookup,
                thinking={"type": "adaptive"},
                output_config={"effort": claude_cfg.effort},
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
                **({"container": container_id} if container_id else {}),
            )
        except anthropic.APIError as e:
            log_call_error(job_id=lookup_id, site="lookup", model=model, exc=e)
            raise PhaseError("claude_call", e) from e
        input_tokens += response.usage.input_tokens
        output_tokens += response.usage.output_tokens
        usage.record("anthropic", model, response.usage.input_tokens, response.usage.output_tokens)
        stop_reason = response.stop_reason
        web_searches += count_web_searches(response.content, lookup_id, "lookup")
        if response.container is not None:
            container_id = response.container.id

        if response.stop_reason == "refusal":
            logger.warning("lookup %s claude refusal", lookup_id)
            emit_summary()
            return LookupResult(answer="I can't help with that request.")

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use" and block.name == "query_notes":
                    query_notes_calls += 1
                    try:
                        result = await query_notes_impl(db, block.input)
                    except Exception as e:  # noqa: BLE001
                        raise PhaseError("couchdb_query", e) from e
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        if response.stop_reason == "pause_turn":
            logger.warning("lookup %s claude pause_turn resume (iteration %d)", lookup_id, iterations)
            messages.append({"role": "assistant", "content": response.content})
            continue

        emit_summary()
        text_block = next((b for b in response.content if b.type == "text"), None)

        if response.stop_reason == "max_tokens":
            # Adaptive thinking spends from max_tokens first, so a tight budget
            # can cut the answer short — or eat the whole budget and leave no
            # text at all. Either way, say so instead of returning "".
            logger.warning("lookup %s truncated at max_tokens=%d", lookup_id, claude_cfg.max_tokens_lookup)
            partial = text_block.text if text_block else ""
            return LookupResult(
                answer=(f"{partial}\n\n[cut off — raise max_tokens_lookup in config.yaml]"
                        if partial else
                        "The answer was cut off before it started — thinking used the whole "
                        "token budget. Raise max_tokens_lookup in config.yaml, or lower effort.")
            )

        return LookupResult(answer=text_block.text if text_block else "")

    emit_summary()
    return LookupResult(answer="Sorry, I couldn't find an answer within the query budget.")

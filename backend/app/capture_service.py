"""Capture: one Claude call (vision optional + web search + query_notes +
structured output), per tasting-log-design.md §4.2/§4.5.

Note on §10's "failed enrichment" open question: merging vision+search+parse
into a single call (rather than a serial vision-then-search pipeline) means
there's no longer a clean "extraction succeeded, enrichment failed" split to
fall back from — if web search comes up empty or errors internally, Claude
just leaves those fields blank in the same structured response (graceful
degradation, not a Python exception). A capture only reaches `failed` here
when there's truly nothing usable: an API/network error, a refusal, or a
structured response that fails Pydantic validation. In that case there's no
partial note to persist, so the capture is marked failed and the caller
should retry the whole thing.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import date, datetime, timezone
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, ValidationError

from app import usage
from app.capture_json_schema import CAPTURE_OUTPUT_SCHEMA
from app.claude_client import get_claude_client, log_call_error, log_call_summary
from app.config import Settings
from app.couchdb_client import CouchDBClient
from app.errors import PhaseError
from app.markdown import render_markdown
from app.model_output import ModelOutputError, loads_model_json, no_text_output, refused, truncated
from app.providers import provider_for
from app.schema import AnyNote, parse_any_note, slug_tokens
from app.tools import QUERY_NOTES_TOOL, query_notes_impl

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You extract structured tasting-log entries for whisky, cigars, coffee, pipe \
tobacco, beer, chocolate, and rakı.

`producer` is the brand/distillery/roaster ("My Father", "Glen Scotia"); \
`name` is the product/expression ONLY ("Blue Series", "Double Cask") and \
must never repeat the producer. Parse carefully: in "smoked My Father Blue \
Series", "smoked" is the user's verb, the producer is "My Father". If \
unsure how a title splits, use web_search or query_notes to check.

For a photo capture: read the label image and extract name, producer, \
category, and category-specific fields. Use web_search to fill fields not \
printed on the label (cask finish/maturation for whisky, roast process/\
origin for coffee, wrapper/strength for cigars, blend_type/cut/component \
leaf for pipe tobacco, style/ABV/IBU for beer, cacao %/type/bean origin for \
chocolate, base spirit/aniseed/ABV for rakı).

`country_of_origin` is MANDATORY for whisky, cigars, coffee, pipe tobacco, \
beer, chocolate, and rakı — the \
country the item is made in ("Scotland", "Nicaragua", "Ethiopia"). Infer it \
from the producer/distillery/roaster or use web_search; only use "unknown" \
if it truly can't be determined. For whisky, still also set `region` (the \
sub-region, e.g. "Speyside") — the two are separate and both matter.

For COFFEE, the rating is for the (bean, brew method) couple — the same bean \
can be great as espresso and poor over a V60. Set `brew_method` (espresso, \
V60, drip, French press, AeroPress, moka, cold brew, …) whenever a brewing \
method is mentioned. Treat the same bean brewed a different way as a SEPARATE \
entry with its own rating — do not merge it with an existing one.

For RAKI (Turkish anise spirit — "rakı", also written raki; Yeni Rakı, \
Tekirdağ, Efe, Sarı Zeybek, Mercan are producers, not names), set `raki_base` \
(fresh grape / dried grape / fig / mulberry), `anise` (aniseed character and \
provenance — Çeşme aniseed if stated), `abv`, `distillations` (1-3; "üç kere \
damıtılmış" = 3), and `serving` (neat / with water / on ice) when mentioned. \
`country_of_origin` is Türkiye unless the label says otherwise. Rakı is a \
DRINK for pairing purposes, so it pairs with a companion (cigar, pipe, or \
chocolate) and carries NO `cocktail_pairings`.

For CHOCOLATE, set `chocolate_type` (dark/milk/white/ruby), `cacao_percent`, \
`cacao_origin` (the bean's origin, distinct from where the bar is made), and \
`form` (bar/truffle/bonbon/drinking) when stated.

For a chat capture: parse the same fields from natural language. Distinguish \
`status: tasted` (an actual tasting, needs a `rating` 1-5 — decimals allowed \
at one-decimal precision, so "four and a half stars" is 4.5) from \
`status: to-try` (a recommendation, NO rating, may have `recommended_by`).

Notes have two strictly separate dimensions:
- `notes`: the USER'S OWN impressions, kept in their words — a transcription, \
not a write-up. If they did not describe how it tasted, `notes` MUST be empty. \
A product name, an ABV, a price, a date or a star rating is NOT a tasting \
note: "Bomonti Filtresiz 4.4%, 3 stars" means `notes` is empty, not a \
paragraph about malt aroma. Never invent, extrapolate, or write what a typical \
taster would say — putting words in the user's mouth is worse than an empty \
field, because later they cannot tell it wasn't theirs. Never mix in web or \
vendor information; that is what `common_notes` is for.
- `common_notes`: the established tasting profile — what the vendor says, \
what reviewers commonly report, or well-known characteristics ("Oloroso \
sherry cask: dried fruit, walnut"). Use web_search (1-2 searches) when \
available, otherwise your own knowledge. Never put the user's opinion here. \
Leave empty rather than inventing anything for obscure items.

A PAIRING is ALWAYS cross-category: one COMPANION (a thing you savor — cigar, \
pipe, or chocolate) with one drink (whisky, coffee, beer, or rakı). Never pair \
same-side (no cigar-with-chocolate, no whisky-with-beer).

If the message reports a PAIRING already tried (e.g. "had the Glenfarclas 15 \
with the Padron 1964, great match") instead of a single item: use \
query_notes to resolve each named item to its real vault _id (search by \
name_contains), and produce `type: pairing` with `items: [id1, id2]` — do \
NOT invent ids, and do not guess if no match is found; in that case fall \
back to a regular item note instead and mention the ambiguity in `notes`. \
If the two reported items are the same side (both companions or both drinks), \
it is NOT a valid pairing — log them as separate item notes instead.

For item notes (not pairings): suggest 1-2 pairings in `pairings_suggested`, \
each pairing this item with the OPPOSITE side (a companion — cigar, pipe, or \
chocolate — → a drink; a drink → a companion). For each, give a `profile`: the ideal \
archetype in a SPECIFIC, tightly-specified style ("a sherry-cask matured 10+ \
yo single malt", "a natural-process Ethiopian with berry acidity"), never a \
generic category like "espresso" or "a bourbon". Then use query_notes on the \
opposite category (prefer highly-rated, use min_rating) to find 0-2 `matches` \
— concrete items the user already OWNS that fit that profile, referenced by \
their vault `item` id. If nothing in the vault fits, leave `matches` empty — \
the profile alone is the recommendation. Give a short `reason` tied to this \
item's character. Do not call query_notes more than a couple of times — this \
is a nudge, not a research task.

For CIGAR and PIPE only, ALSO fill `cocktail_pairings` with 1-2 of the most \
common CLASSICAL cocktails that suit it (Old Fashioned, Manhattan, Negroni, \
Sazerac, Whiskey Sour, Daiquiri, Boulevardier, Rusty Nail, …) — just the \
`name` and a short `reason` it works with this tobacco's character. A cocktail \
accompanies a smoke, so ALL other items (chocolate and the drinks) leave \
`cocktail_pairings` empty.

`tags` are lowercase-kebab-case (no spaces — Obsidian rejects them), \
describe character ("medium-body", "slight-sweet"), and never duplicate \
the product or producer name.

Coffee dial-in fields (grind_size, dose_g, brew_time_s, grinder, machine) are \
only ever present if the user explicitly states them — never invent a grind \
size, dose, grinder, or machine, and never assume a default rig. Leave them out \
when unstated. `grind_size` is free text (e.g. "medium-fine", "metal filter") — \
put a stated grind description there.

Respond only with the structured JSON. `source` must be "photo" or "chat" \
matching how this capture arrived. If `date` isn't mentioned, use today.
"""

# The Claude path cannot use output_config.format: CAPTURE_OUTPUT_SCHEMA has 47
# optional properties and Anthropic's structured outputs cap grammar
# compilation at 24 ("Schemas contains too many optional parameters"; 400
# invalid_request_error). There is no `strict: false` escape hatch like the one
# the OpenAI path uses, and 31 of those 47 are category-specific fields that
# only ever apply to one note type, so the count can't come down without
# splitting the schema per type. The schema goes into the prompt as text
# instead — it still tells the model the exact field names, which the prose
# above only partly covers — and schema.py's Pydantic layer remains the real
# enforcement (same philosophy as capture_json_schema.py's header).
CLAUDE_SYSTEM_PROMPT = SYSTEM_PROMPT + (
    "\nYour reply must be a single JSON object conforming to this schema — no "
    "markdown fences, no prose before or after it:\n"
    + json.dumps(CAPTURE_OUTPUT_SCHEMA)
)


class CaptureResult(BaseModel):
    capture_id: str
    status: Literal["pending", "done", "failed"]
    note: AnyNote | None = None
    doc_id: str | None = None
    prior_match: dict[str, Any] | None = None
    error: str | None = None


async def run_capture(
    capture_id: str,
    settings: Settings,
    db: CouchDBClient,
    *,
    source: Literal["photo", "chat"],
    text: str | None = None,
    stars: float | None = None,
    image_b64: str | None = None,
    image_media_type: str | None = None,
    model_override: str | None = None,
) -> CaptureResult:
    claude_cfg = settings.models.claude
    # Admin-panel choice (delivered per job by the relay) wins over the
    # baked-in config.yaml default. Non-Claude ids route to an alternate
    # provider module — see providers.py for the id → provider table.
    model = model_override or claude_cfg.capture_model

    prompt_parts = []
    if stars is not None:
        prompt_parts.append(f"Star rating given at capture: {stars}/5.")
    if text:
        prompt_parts.append(text)
    if not prompt_parts:
        prompt_parts.append("Extract the tasting entry from the label photo.")
    prompt_parts.append(f'This capture arrived via "{source}" — set source accordingly.')
    prompt_text = "\n".join(prompt_parts)

    try:
        if (provider := provider_for(model)) is not None:
            data = await provider.extract_structured(
                settings, db,
                job_id=capture_id,
                model=model,
                system_prompt=SYSTEM_PROMPT,
                text=prompt_text,
                image_b64=image_b64,
                image_media_type=image_media_type,
                # Chat captures search too — common_notes wants vendor/review
                # grounding, not just label-photo gap-filling. Every alternate
                # provider honours the flag; Mistral is the one that may still
                # end up without search, if its Conversations call falls back.
                use_web_search=True,
                output_schema=CAPTURE_OUTPUT_SCHEMA,
            )
        else:
            client = get_claude_client(settings)
            user_content: list[dict[str, Any]] = []
            if image_b64:
                user_content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": image_media_type or "image/jpeg", "data": image_b64},
                })
            user_content.append({"type": "text", "text": prompt_text})

            # web search on every capture (not just photos) — common_notes
            # wants vendor/review grounding; max_uses keeps cost bounded.
            tools: list[dict[str, Any]] = [
                QUERY_NOTES_TOOL,
                {
                    "type": "web_search_20260209",
                    "name": "web_search",
                    "max_uses": claude_cfg.web_search_max_uses,
                },
            ]

            messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
            data = await _run_extraction_loop(client, claude_cfg, model, tools, messages, db, capture_id=capture_id)

        data.setdefault("date", date.today().isoformat())
        try:
            note = parse_any_note(data)
        except Exception as e:  # noqa: BLE001
            # WRK-4: name the field(s) that failed at WARNING; the raw model
            # output is content, so it goes to DEBUG only (GEN-4).
            logger.warning("capture %s validation failed fields=%s", capture_id, _validation_fields(e))
            logger.debug("capture %s raw model output: %s", capture_id, json.dumps(data))
            raise CaptureFailed(f"structured output failed validation: {e}") from e
    except ModelOutputError as e:  # CaptureFailed included — see its docstring
        logger.warning("capture %s failed: %s", capture_id, e)
        return CaptureResult(capture_id=capture_id, status="failed", error=str(e))
    except Exception as e:  # noqa: BLE001 — genuinely want to catch-all here and surface it
        logger.exception("capture %s failed with unexpected error", capture_id)
        return CaptureResult(capture_id=capture_id, status="failed", error=f"unexpected error: {e}")

    _drop_fabricated_notes(note, text, capture_id)

    # Stamp a stable logical id so reverse-sync can track this record across
    # future Obsidian renames (see schema.BaseNote.uid / reconcile.py).
    if getattr(note, "uid", None) is None:
        note.uid = uuid.uuid4().hex

    # Past this point the model work is done; remaining failures are CouchDB
    # integration failures, tagged with their phase (WRK-1) so the worker's
    # job-failed line names them without unpacking the traceback.
    prior_match = None
    if note.item_type() != "pairing":
        try:
            prior_match = await _deterministic_repeat_check(db, note)
        except Exception as e:  # noqa: BLE001
            raise PhaseError("couchdb_query", e) from e
        if prior_match:
            logger.info("capture %s prior_match doc_id=%s", capture_id, prior_match.get("doc_id"))

    markdown = render_markdown(note)
    try:
        doc_id = await db.write_note(note, markdown)
    except Exception as e:  # noqa: BLE001
        raise PhaseError("couchdb_write", e) from e
    # WRK-5: the one line that answers "did it reach the vault?".
    logger.info("note written job_id=%s doc_id=%s", capture_id, doc_id)

    return CaptureResult(
        capture_id=capture_id,
        status="done",
        note=note,
        doc_id=doc_id,
        prior_match=prior_match,
    )


class CaptureFailed(ModelOutputError):
    """A capture that can't produce a note. Subclasses ModelOutputError so the
    handler in run_capture catches Claude's failures and the provider modules'
    identically — before, only this one reported cleanly and the other two
    surfaced as "unexpected error: ..."."""


async def _run_extraction_loop(client, claude_cfg, model: str, tools, messages, db: CouchDBClient, *, capture_id: str) -> dict:
    """Claude tool loop; returns the raw structured dict — run_capture owns
    validation (shared with the OpenAI path). Emits the WRK-2 telemetry line
    (tokens summed across the loop for the cost ledger) and WRK-3 on API
    errors."""
    web_search = any("web_search" in str(t.get("type", "")) for t in tools)
    iterations = query_notes_calls = input_tokens = output_tokens = 0
    stop_reason: str | None = None
    # web_search_20260209 filters results dynamically by running code
    # server-side, which provisions a container. Once that happens the
    # container has to be named on every later turn of this conversation, or
    # the next request 400s with "container_id is required when there are
    # pending tool uses generated by code execution with tools". We never
    # declare code_execution ourselves — the search tool brings it along.
    container_id: str | None = None
    started = time.monotonic()
    summary_logged = False

    def emit_summary() -> None:
        nonlocal summary_logged
        if summary_logged:
            return
        summary_logged = True
        log_call_summary(
            job_id=capture_id, site="capture", model=model, stop_reason=stop_reason,
            input_tokens=input_tokens, output_tokens=output_tokens, iterations=iterations,
            query_notes=query_notes_calls, web_search=web_search,
            duration_s=time.monotonic() - started,
        )

    for _ in range(claude_cfg.max_tool_iterations):
        iterations += 1
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=claude_cfg.max_tokens_capture,
                thinking={"type": "adaptive"},
                output_config={"effort": claude_cfg.effort},
                system=CLAUDE_SYSTEM_PROMPT,
                tools=tools,
                messages=messages,
                **({"container": container_id} if container_id else {}),
            )
        except anthropic.APIError as e:
            log_call_error(job_id=capture_id, site="capture", model=model, exc=e)
            raise
        input_tokens += response.usage.input_tokens
        output_tokens += response.usage.output_tokens
        usage.record("anthropic", model, response.usage.input_tokens, response.usage.output_tokens)
        stop_reason = response.stop_reason
        # Carry the container forward before any branch below continues the loop.
        if response.container is not None:
            container_id = response.container.id

        if response.stop_reason == "refusal":
            logger.warning("capture %s claude refusal", capture_id)
            emit_summary()
            detail = getattr(getattr(response, "stop_details", None), "category", None)
            raise refused("anthropic", detail)

        if response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use" and block.name == "query_notes":
                    query_notes_calls += 1
                    result = await query_notes_impl(db, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result),
                    })
            messages.append({"role": "user", "content": tool_results})
            continue

        if response.stop_reason == "pause_turn":
            # Server-side web_search loop hit its internal iteration cap;
            # resend to resume — do NOT append an extra user "continue" turn.
            logger.warning("capture %s claude pause_turn resume (iteration %d)", capture_id, iterations)
            messages.append({"role": "assistant", "content": response.content})
            continue

        if response.stop_reason == "max_tokens":
            # Adaptive thinking spends from max_tokens before any visible text,
            # so a tight budget truncates the JSON mid-string. Name that rather
            # than letting json.loads raise a cryptic "Unterminated string"
            # (the OpenAI path surfaces the same case via incomplete_details).
            logger.warning("capture %s truncated at max_tokens=%d", capture_id, claude_cfg.max_tokens_capture)
            emit_summary()
            raise truncated("anthropic", "max_tokens_capture")

        # end_turn: expect a single JSON text block. Prompt-nudged rather than
        # grammar-guaranteed (see CLAUDE_SYSTEM_PROMPT), so be forgiving of a
        # stray markdown fence before handing off to Pydantic.
        emit_summary()
        text_block = next((b for b in response.content if b.type == "text"), None)
        if text_block is None:
            raise no_text_output("anthropic")
        return loads_model_json(text_block.text, "anthropic")

    emit_summary()
    raise CaptureFailed("exceeded max tool-use iterations without a final answer")


# Words that can legitimately appear in `notes` without appearing in what the
# user typed — articles, connectives and intensifiers a transcription may add
# while tidying. English and Turkish, since captures come in both.
_NOTE_FILLER = frozenset({
    "and", "the", "but", "was", "were", "are", "its", "it", "this", "that",
    "with", "for", "not", "very", "really", "quite", "bit", "some", "had",
    "has", "have", "then", "than", "also", "just", "too",
    "ve", "ama", "cok", "bir", "biraz", "ile", "ama", "daha", "gibi",
})

# How much of `notes` must be traceable to the user's own words. Generous:
# a transcription is near-verbatim, so genuine notes score close to 1.0.
_NOTE_OVERLAP_FLOOR = 0.6


def _content_tokens(text: str) -> set[str]:
    return {t for t in slug_tokens(text) if len(t) > 2 and t not in _NOTE_FILLER}


def _drop_fabricated_notes(note: AnyNote, user_text: str | None, capture_id: str) -> None:
    """`notes` is the user's own words — so its words must be the user's words.

    Prompting alone doesn't hold this line. Observed in the wild: a capture
    carrying only a product name, an ABV and a star rating came back with a
    paragraph of first-person tasting impressions — weak malt aroma, thin
    finish, the lot. None of it was the user's, and once written it is
    indistinguishable from something they actually said.

    Two gates, because length alone is too weak: it catches an invented
    paragraph but waves through an invented sentence ("Crisp and refreshing."
    is shorter than the product name it was invented from). The real invariant
    is provenance — `notes` transcribes what the user typed, so its content
    words should be words they used. Tags and common_notes are model-generated
    by design and left alone; this is the one field that must stay theirs.
    """
    notes = (getattr(note, "notes", "") or "").strip()
    if not notes:
        return

    said = (user_text or "").strip()
    reason: str | None = None
    if not said:
        reason = "user typed nothing to transcribe"
    elif len(notes) > int(len(said) * 1.5) + 20:
        reason = f"{len(notes)} chars of notes from {len(said)} chars of user text"
    else:
        written = _content_tokens(notes)
        # No content words at all ("Nice.") — nothing to trace, nothing to gain
        # from dropping it.
        if written:
            shared = written & _content_tokens(said)
            if len(shared) / len(written) < _NOTE_OVERLAP_FLOOR:
                reason = f"only {len(shared)}/{len(written)} words came from the user"

    if reason:
        logger.warning("capture %s dropped fabricated notes (%s)", capture_id, reason)
        logger.debug("capture %s discarded notes: %s", capture_id, notes)
        note.notes = ""


def _validation_fields(exc: Exception) -> str:
    """Comma-joined dotted field paths from a Pydantic ValidationError (WRK-4);
    a bare '?' when the error isn't one we can introspect."""
    if isinstance(exc, ValidationError):
        locs = {".".join(str(p) for p in err["loc"]) for err in exc.errors()}
        return ",".join(sorted(loc for loc in locs if loc)) or "?"
    return "?"


async def _deterministic_repeat_check(db: CouchDBClient, note: AnyNote) -> dict[str, Any] | None:
    """Server-side (not LLM-driven) repeat-detection — deterministic, not
    dependent on the model remembering to check. See §4.2."""
    import re

    docs = await db.find(
        {"type": note.item_type(), "name": {"$regex": f"(?i){re.escape(note.name)}"}},
        limit=5,
    )
    if not docs:
        return None
    # Most recent prior tasting by date, excluding the note we're about to write.
    prior = sorted(docs, key=lambda d: d.get("date", ""), reverse=True)[0]
    return {
        "doc_id": prior.get("_id"),
        "date": prior.get("date"),
        "rating": prior.get("rating"),
        "status": prior.get("status"),
    }

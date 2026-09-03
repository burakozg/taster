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
from app.claude_client import count_web_searches, get_claude_client, log_call_error, log_call_summary
from app.config import Settings
from app.couchdb_client import CouchDBClient
from app.errors import PhaseError, exc_label
from app.markdown import render_markdown
from app.model_output import ModelOutputError, loads_model_json, no_text_output, refused, truncated
from app.providers import provider_for
from app.schema import AnyNote, parse_any_note, slug_tokens
from app.text_facts import extract_facts
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
printed on the label (cask finish/maturation and ABV for whisky, roast process/\
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
sherry cask: dried fruit, walnut"). ALWAYS run at least one web_search for \
this specific product before writing this field, on chat captures as much as \
photo ones. Grounding this field is the reason the search tool is here, and \
your own recall of one particular bottling or tin is not a substitute for \
looking it up. Write what you find as real sentences. Fall back to your own \
knowledge only if the searches genuinely returned nothing about this product, \
and omit the field only if that is empty too. Never put the user's opinion \
here, and never invent a profile for something you found nothing on.

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
generic category like "espresso" or "a bourbon". Then you MUST call \
query_notes with `pair_group` set to the OPPOSITE side ("drink" for a \
companion, "companion" for a drink) to see what the user actually owns — ONE \
call returns that entire side, so the whole matching step costs a single tool \
call. Prefer highly-rated results (use min_rating). From what comes back, pick \
0-2 `matches` per suggestion that fit the profile, each referenced by its vault \
`item` id. Leaving `matches` empty is only valid AFTER that call, when nothing \
returned actually fits — then the profile alone is the recommendation. Give a \
short `reason` tied to this item's character. One or two query_notes calls is \
the budget; this is a nudge, not a research task.

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

Every item note (everything except a pairing) MUST include `name` and \
`status` — never omit them even when the text is terse (e.g. "Glenfiddich \
15, balanced, 3.8 stars" still has a `name` and a `status` of "tasted"). \
`status` is "tasted" if any tasting/rating is described, "to-try" only for \
a bare recommendation with no rating.

The schema below lists every field across every category, but almost none of \
them apply to any single item. That rule is about the FACTUAL per-category \
fields (cask, age_years, abv, wrapper, blend_type, dose_g, …): include one \
only when you genuinely know or can determine its value for THIS item, and \
OMIT the rest entirely. NEVER fill an unknown or inapplicable factual field \
with a placeholder like "", 0, or false just to satisfy the shape — an omitted \
key and a hollow placeholder are not the same thing to the person reading this \
later.

It does NOT apply to `common_notes`, `pairings_suggested`, `cocktail_pairings` \
and `tags`. Those four are yours to produce on every item note they apply to, \
and you produce them by doing the work described above — searching the web, \
querying the vault — not by deciding up front that you don't know. An item \
note that comes back with no `common_notes`, or with `pairings_suggested` \
entries whose `matches` are empty because query_notes was never called, is a \
failed capture even though its JSON parses.

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
    model = model_override or claude_cfg.image_model

    # Read outright-stated facts from the user's text before the model sees it.
    # A chat capture has no rating slider, so "3,6 stars" is the only rating
    # there is — and telling the model the number rather than hoping it parses
    # a decimal comma is both more reliable and stops it copying "3,6 stars"
    # into `notes` as though that were a tasting impression.
    facts = extract_facts(text)

    prompt_parts = []
    if (given_rating := stars if stars is not None else facts.rating) is not None:
        prompt_parts.append(f"Star rating given at capture: {given_rating}/5.")
    if facts.abv is not None:
        prompt_parts.append(f"ABV stated by the user: {facts.abv}%.")
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

        # The SYSTEM_PROMPT makes `common_notes` and `pairings_suggested`
        # mandatory on every item note — they are the whole reason the
        # web_search and query_notes tools are attached — and calls a note that
        # comes back without them "a failed capture even though its JSON
        # parses". Nothing downstream honoured that: schema.py defaults both to
        # "" / [], so a model that one-shots a bare note (seen in the wild:
        # DeepSeek V4 Pro returning ~340 tokens in a single turn, zero tool
        # calls) had its hollow note written to the vault and reported as a
        # success. Check it here, still inside the try, so it surfaces through
        # the ModelOutputError path as a clean, retriable failure.
        if note.item_type() != "pairing" and (missing := _missing_enrichment(note)):
            logger.warning("capture %s missing enrichment: %s", capture_id, ", ".join(missing))
            logger.debug("capture %s raw model output: %s", capture_id, json.dumps(data))
            raise CaptureFailed(
                f"the model skipped the enrichment step ({'; '.join(missing)}) — "
                f"try the capture again"
            )
    except ModelOutputError as e:  # CaptureFailed included — see its docstring
        logger.warning("capture %s failed: %s", capture_id, e)
        return CaptureResult(capture_id=capture_id, status="failed", error=str(e))
    except Exception as e:  # noqa: BLE001 — genuinely want to catch-all here and surface it
        logger.exception("capture %s failed with unexpected error", capture_id)
        # exc_label, not f"{e}": the network exceptions that actually land here
        # carry NO message. httpx.ReadTimeout stringifies to "", so this read
        # "Failed: unexpected error:" on the user's screen — a failure that
        # names nothing, for a capture that was one timeout away from working.
        return CaptureResult(
            capture_id=capture_id, status="failed", error=f"unexpected error: {exc_label(e)}",
        )

    # --- what the user supplied themselves is a FACT, not something to infer ---
    #
    # A photo capture carries two things the user entered by hand: the star
    # rating they set on the slider, and the note they typed. Round-tripping
    # either through the model can only lose them — and on a weak model it
    # reliably does: an observed capture came back with the rating zero-filled
    # (which _zero_rating_is_none then turns into "unrated") and the typed note
    # shredded into `tags` with `notes` left empty.
    #
    # There is nothing for a model to add here, so it isn't asked. This holds on
    # every provider, which matters because the capture model is selectable and
    # most of the choices are far weaker at field discipline than Claude.
    #
    # Chat captures are deliberately excluded: there the typed text is the whole
    # capture — name, producer, rating and impressions mixed together — so it
    # genuinely needs parsing, and `notes` must be the impressions only.
    pinned_notes = False
    if note.item_type() != "pairing":
        # The slider wins over parsed text when both exist — it is an exact
        # value the user set, not a reading of a sentence.
        if given_rating is not None:
            # A rating means it was tasted; setting one on a to-try note would
            # produce a record the schema rejects (see _rating_only_when_tasted).
            note.status = "tasted"
            note.rating = round(float(given_rating), 1)
        # Only onto types that HAVE an abv (whisky, beer, rakı). A percentage
        # in a chocolate note is cacao content, and `hasattr` is what keeps this
        # from quietly writing a field the model was right to leave alone.
        if facts.abv is not None and hasattr(note, "abv"):
            note.abv = facts.abv
        # `notes` is pinned for PHOTO captures only. There the typed text is
        # purely the user's impressions — the label supplies the identity. In a
        # chat capture the same string also carries the product name and the
        # rating, so it needs the model to separate out the impressions, and
        # _drop_fabricated_notes below is what keeps that honest.
        if source == "photo" and (text or "").strip():
            note.notes = text.strip()
            pinned_notes = True

    # Only meaningful when the model authored `notes`. When the user's own text
    # was pinned above there is nothing to second-guess.
    if not pinned_notes:
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
    iterations = query_notes_calls = input_tokens = output_tokens = web_searches = 0
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
            web_searches=web_searches, duration_s=time.monotonic() - started,
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
        web_searches += count_web_searches(response.content, capture_id, "capture")
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
            if not tool_results:
                # stop_reason was tool_use but nothing we own was called. An
                # empty user turn is a 400, which would surface as an opaque
                # API error rather than the loop's own problem — so name it.
                emit_summary()
                raise CaptureFailed("model requested an unknown tool")
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

# How much of `notes` must be traceable to the user's own words, once there are
# enough words for the ratio to mean anything. A transcription is near-verbatim,
# but it is allowed to tidy — so this is a floor, not a demand for a match.
_NOTE_OVERLAP_FLOOR = 0.5
# Below this many content words the ratio is noise, not evidence: at three
# words, one tidy-up ("honey" written as "honeyed") swings it by a third. Short
# notes are held to the length gate and the described-anything gate instead,
# both of which a short note passes or fails cleanly.
_NOTE_MIN_TOKENS_FOR_RATIO = 5
# Words that describe the RECORD rather than the taste: a rating, a price, a
# strength. The prompt already says a star rating or an ABV is not a tasting
# note, and gate 1 below has to agree — otherwise "Double Cask 4 stars" counts
# as the user having described something, and an invented sentence rides in on
# the word "stars". Matched by stem, so "star"/"stars" and "rate"/"rated" are
# one entry each. Deliberately excludes anything that doubles as a flavour word
# ("date" would take "dates" with it).
_NOTE_METADATA = frozenset({
    "star", "yildiz", "rating", "rated", "rate", "score", "scored", "point",
    "points", "price", "cost", "sek", "kronor", "eur", "usd", "euro", "dollar",
    "abv", "proof", "percent", "vol",
})

# How many leading characters two words must share to count as the same word.
# Tasting vocabulary inflects constantly — peat/peaty, smoke/smoky,
# honey/honeyed, oak/oaky — and exact token matching scored every one of those
# as a word the user never said.
_NOTE_STEM_LEN = 4


def _content_tokens(text: str) -> set[str]:
    return {
        t for t in slug_tokens(text)
        if len(t) > 2 and t not in _NOTE_FILLER and not t.isdigit()
    }


def _echoes(word: str, pool: set[str]) -> bool:
    """Is `word` one of the user's words, allowing for inflection?"""
    if word in pool:
        return True
    if len(word) < _NOTE_STEM_LEN:
        return False
    stem = word[:_NOTE_STEM_LEN]
    return any(w[:_NOTE_STEM_LEN] == stem for w in pool if len(w) >= _NOTE_STEM_LEN)


def _drop_fabricated_notes(note: AnyNote, user_text: str | None, capture_id: str) -> None:
    """`notes` is the user's own words — so its words must be the user's words.

    Prompting alone doesn't hold this line. Observed in the wild: a capture
    carrying only a product name, an ABV and a star rating came back with a
    paragraph of first-person tasting impressions — weak malt aroma, thin
    finish, the lot. None of it was the user's, and once written it is
    indistinguishable from something they actually said.

    Three gates, in order of how much they prove:

    1. **Did they describe anything at all?** Product identity doesn't count.
       "Bomonti Filtresiz 4.4%, 3 stars" is a name, a strength and a rating —
       every word of it is the label, so a tasting note built from it was
       invented no matter how short. This is the gate that catches an invented
       *sentence*, which length alone waves through.
    2. **Length.** A transcription is about the size of what was said, not
       several times longer. This catches the invented paragraph.
    3. **Provenance.** Enough of the words should be traceable to theirs.

    Gate 3 used to be the whole of gates 1 and 3 together, and it was badly
    calibrated for PHOTO captures. There, the user types only their
    impressions — the name comes off the label — so `said` is short and there
    are few words to match against; a three-word note plus one tidy-up scored
    as fabrication. Chat captures hid this because their text carries the
    product name too, which pads the pool. Hence the token floor on gate 3, the
    stem matching in _echoes, and gate 1 taking over the job of catching a
    short invention.

    Tags and common_notes are model-generated by design and left alone; this is
    the one field that must stay theirs.
    """
    notes = (getattr(note, "notes", "") or "").strip()
    if not notes:
        return

    said = (user_text or "").strip()
    said_tokens = _content_tokens(said)
    # What the user contributed BEYOND naming and scoring the thing. Name,
    # producer, rating and price are identity/metadata, not impressions —
    # echoing them back is not a tasting note.
    not_impressions = _content_tokens(
        f"{getattr(note, 'producer', '') or ''} {getattr(note, 'name', '') or ''}"
    ) | _NOTE_METADATA
    described = {t for t in said_tokens if not _echoes(t, not_impressions)}

    reason: str | None = None
    if not said:
        reason = "user typed nothing to transcribe"
    elif not described:
        reason = "user gave only the product name/rating, no impressions"
    elif len(notes) > int(len(said) * 1.5) + 20:
        reason = f"{len(notes)} chars of notes from {len(said)} chars of user text"
    else:
        written = _content_tokens(notes)
        # Below the floor the ratio says more about the model's phrasing than
        # about provenance — gates 1 and 2 already cleared this note.
        if len(written) >= _NOTE_MIN_TOKENS_FOR_RATIO:
            shared = {w for w in written if _echoes(w, said_tokens)}
            if len(shared) / len(written) < _NOTE_OVERLAP_FLOOR:
                reason = f"only {len(shared)}/{len(written)} words came from the user"

    if reason:
        logger.warning("capture %s dropped fabricated notes (%s)", capture_id, reason)
        logger.debug("capture %s discarded notes: %s", capture_id, notes)
        note.notes = ""


def _missing_enrichment(note: AnyNote) -> list[str]:
    """Which mandatory model-authored fields an item note came back without.

    `common_notes` (the established tasting profile, grounded by web_search) and
    `pairings_suggested` (1-2 opposite-side profiles, informed by query_notes)
    are required on every item note per SYSTEM_PROMPT — a note without them is a
    failed capture even when the JSON is valid. Both default to empty in
    schema.py and `_clean_pairings_suggested` drops malformed entries, so an
    empty value here means the model either skipped the step or produced only
    junk for it; either way there is nothing worth persisting. `common_notes`
    matters most (it's the field the user notices missing); the caller treats
    any non-empty return as a retriable failure.
    """
    missing: list[str] = []
    if not (getattr(note, "common_notes", "") or "").strip():
        missing.append("no common_notes")
    if not getattr(note, "pairings_suggested", None):
        missing.append("no pairings_suggested")
    return missing


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

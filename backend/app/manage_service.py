"""AI vault maintenance — bulk-edit existing records via a natural-language
instruction, behind a mandatory plan → approve → apply flow.

Two phases, two job types (see worker.py dispatch):

- **plan** (`run_manage_plan`) — Claude reads the whole vault (a compact
  projection + `query_notes` for detail + `web_search` for facts like a
  country of origin) and returns a *proposed* list of per-record edits. It
  writes NOTHING. The instruction can be anything: "give every whisky a
  country of origin", "reorder pairing suggestions best-first", "assign a uid
  to records that lack one". For the last case the model does NOT invent a
  uid (LLMs can't produce collision-safe ids) — it sets `generate_uid`, and
  the apply phase mints the real one.
- **apply** (`run_manage_apply`) — takes the subset of changes the human
  approved and applies them deterministically: merge the edits onto the
  current document, re-validate through the schema (a bad edit fails that one
  change, never the batch), then update the JSON doc + re-project the vault
  file. Every change is logged.

Everything a change touches goes through Pydantic (`parse_any_note`), so the
maintenance path can't write a document the capture path wouldn't accept.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any

import anthropic

from app import usage
from app.categories import CATEGORIES, NOTE_TYPES
from app.claude_client import count_web_searches, get_claude_client, log_call_error, log_call_summary
from app.config import Settings
from app.couchdb_client import CouchDBClient
from app.items_query import query_all_items
from app.markdown import render_markdown
from app.model_output import ModelOutputError, loads_model_json, no_text_output, refused
from app.providers import provider_for
from app.schema import parse_any_note
from app.tools import QUERY_NOTES_TOOL, query_notes_impl

logger = logging.getLogger("worker.manage")

# Fields worth showing the planner up front (keeps the prompt compact vs.
# dumping every field of every record; it can query_notes for more).
_COMPACT_FIELDS = (
    "_id", "uid", "type", "name", "producer", "status", "rating", "date",
    "country_of_origin", "region", "origin", "category", "stock", "tags",
)

SYSTEM_PROMPT = """\
You are a maintenance planner for a personal whisky/cigar/coffee/pipe-tobacco/beer/chocolate/rakı \
tasting vault. Given an instruction and the current records, produce a PLAN of \
per-record changes — you never apply anything yourself; a human reviews and \
approves the plan first.

Rules:
- Only include records that actually need a change. Leave everything else out.
- Each record carries a `missing` list naming the fields that are currently \
empty (or set to the "unknown" placeholder). Use it to find the records an \
instruction applies to — "every item with an empty common_notes" means every \
record whose `missing` contains "common_notes". It is a projection of the \
records themselves, so it is authoritative; you do not need query_notes to \
find out which fields are blank.
- Identify each target by its `uid` when it has one, and always include its \
`doc_id` (the record's _id) and `name` so the human can recognise it.
- Express edits as a list of {field, value} pairs. Use the real field names \
from the records (e.g. country_of_origin, region, notes, rating). A scalar \
field's value is a string (numbers and booleans are coerced on apply); a \
LIST field (tags, components) takes a JSON array of strings, e.g. \
{"field": "tags", "value": ["peaty", "medium-body"]} — never a comma-joined \
string for those.
- For facts you don't know (a country of origin, a region), use web_search — \
do not guess. Use "unknown" only when it truly can't be determined.
- NEVER propose a change you have not actually filled in. Every change must \
carry its real new value in `edits` (the sole exception is generate_uid, whose \
value the system mints). A change with empty `edits` and a reason describing \
work still to be done — "empty common_notes; needs a web-searched tasting \
profile" — is a to-do list, not a plan; it is REJECTED on apply and wastes the \
human's review. If you cannot determine a value for a record, leave that \
record out of the plan entirely.
- Your web_search budget is limited and shared across the whole plan. When the \
instruction needs a lookup per record and there are more records than you can \
research, cover as many as you can COMPLETELY, leave the rest out, and say so \
in the summary — a short plan of real edits beats a long list of intentions.
- ONE change per record. If a record needs two fields filled, that is one \
change with two entries in `edits`, not two changes naming the same doc_id.

A correct change, with the researched values IN `edits` where they are applied \
from — this exact shape, every time:

    {"doc_id": "whisky:togouchi-travel-exclusive:2024-06-18",
     "uid": "…", "name": "Shiki", "type": "whisky",
     "edits": [{"field": "abv", "value": "45"},
               {"field": "common_notes", "value": "Soft peat and green apple over vanilla oak; light-bodied, gentle smoke on the finish."}],
     "reason": "ABV and tasting profile from the distillery announcement and a review."}

The same research written as `"reason": "ABV confirmed as 45% from the review; \
filling common_notes and ABV in one go"` with `edits` empty or missing changes \
NOTHING. The value must be in `edits`. Prose about the value is not the value.
- If the instruction is to assign an identifier / uid to records missing one, \
set generate_uid=true for those records and do NOT put a uid value in the \
edits — the system mints a real unique id on apply.
- Pairings have no country_of_origin; skip that field for type=pairing.
- Give a short overall `summary` and a one-line `reason` per change.
"""

MANAGE_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "one-line description of the whole plan"},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string", "description": "the record's _id"},
                    "uid": {"type": "string", "description": "the record's uid, if it has one"},
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": list(NOTE_TYPES)},
                    "edits": {
                        "type": "array",
                        "description": (
                            "the actual new values, e.g. [{\"field\": \"abv\", \"value\": \"45\"}, "
                            "{\"field\": \"common_notes\", \"value\": \"Sherry-cask: dried fruit, walnut.\"}]. "
                            "REQUIRED and non-empty for every change except a uid-only one "
                            "(generate_uid). If you researched a value, it belongs here."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "field": {"type": "string"},
                                "value": {
                                    "description": "string for scalar fields; a JSON array of strings for list fields (tags, components)",
                                    "anyOf": [
                                        {"type": "string"},
                                        {"type": "array", "items": {"type": "string"}},
                                    ],
                                },
                            },
                            "required": ["field", "value"],
                            "additionalProperties": False,
                        },
                    },
                    "generate_uid": {"type": "boolean", "description": "mint a fresh uid on apply (do not put a uid in edits)"},
                    "reason": {
                        "type": "string",
                        "description": (
                            "one short line on WHY this record is being changed. Never put the new "
                            "values here — they go in `edits` and nowhere else. A reason that "
                            "describes work ('found the ABV and tasting profile') while `edits` is "
                            "empty applies nothing and is rejected."
                        ),
                    },
                },
                # `edits` is required, not optional. It used to be optional while
                # `reason` was mandatory, and a planner that had genuinely done
                # the research would satisfy the schema by writing its findings
                # up in the prose field and leaving `edits` off entirely —
                # observed with real values in hand ("ABV confirmed as 45% from
                # ...") and nothing to apply.
                "required": ["doc_id", "reason", "edits"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "changes"],
    "additionalProperties": False,
}


# Fields a maintenance instruction typically backfills. Their VALUES are too
# bulky to project for every record (common_notes alone is a paragraph each),
# but the planner still has to know which are empty — "for every item with an
# empty common_notes" is unanswerable from a projection that omits the field
# entirely, and the planner's only recourse was to spend a query_notes round
# trip pulling full documents. So each record carries the cheap half: the NAMES
# of its empty fields.
_ENRICHABLE_FIELDS = (
    "common_notes", "notes", "producer", "country_of_origin", "region",
    "abv", "cask", "age_years", "tags", "uid",
)


# Which of those fields each note type actually has — a beer has no `cask`, so
# listing one as "missing" invites an edit that Pydantic then ignores, leaving a
# change that reports success and writes nothing.
_FIELDS_BY_TYPE = {c.type: frozenset(c.model.model_fields) for c in CATEGORIES}


def _is_empty(value: Any) -> bool:
    """Empty for maintenance purposes — including the "unknown" placeholder,
    which is a filled-in field that still needs filling in."""
    if value is None or value == [] or value == {}:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip().lower() == "unknown"
    return False


def _compact(records: list[dict]) -> list[dict]:
    out = []
    for r in records:
        c = {k: r[k] for k in _COMPACT_FIELDS if k in r}
        applicable = _FIELDS_BY_TYPE.get(r.get("type"), frozenset())
        missing = [
            f for f in _ENRICHABLE_FIELDS
            if f in applicable and _is_empty(r.get(f))
        ]
        if missing:
            c["missing"] = missing
        out.append(c)
    return out


# --- regenerate pairings (dedicated maintenance: re-run just the pairing step
# over existing items, cross-category companion<->drink, without re-capturing) ---

# Richer projection than _compact so the planner can reason about character.
_REPAIR_FIELDS = (
    "_id", "uid", "type", "name", "producer", "country_of_origin", "region",
    "origin", "roast_level", "style", "strength", "blend_type",
    "chocolate_type", "cacao_percent", "tags", "rating",
)

_PAIRING_SUGGESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "profile": {"type": "string", "description": "the ideal partner archetype, SPECIFIC ('a sherry-cask matured 10+ yo single malt'), never a generic category"},
        "matches": {
            "type": "array",
            "description": "0-2 items from the provided list on the OPPOSITE side that fit the profile; empty if none",
            "items": {
                "type": "object",
                "properties": {
                    "item": {"type": "string", "description": "the matched item's exact _id from the list"},
                    "name": {"type": "string"},
                },
                "additionalProperties": False,
            },
        },
        "reason": {"type": "string"},
    },
    "required": ["profile", "reason"],
    "additionalProperties": False,
}

REPAIR_PAIRINGS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string", "description": "the item's _id"},
                    "uid": {"type": "string"},
                    "name": {"type": "string"},
                    "type": {"type": "string", "enum": list(NOTE_TYPES)},
                    "pairings": {"type": "array", "items": _PAIRING_SUGGESTION_SCHEMA},
                    "cocktails": {
                        "type": "array",
                        "description": "CIGAR and PIPE only: 1-2 classical cocktails, name + reason. Empty for chocolate and drink items.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "reason": {"type": "string"},
                            },
                            "required": ["name", "reason"],
                            "additionalProperties": False,
                        },
                    },
                    "reason": {"type": "string"},
                },
                "required": ["doc_id", "pairings"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "changes"],
    "additionalProperties": False,
}

REPAIR_SYSTEM_PROMPT = """\
You regenerate cross-category PAIRING SUGGESTIONS for a personal tasting vault. \
A pairing is ALWAYS one COMPANION (a thing you savor — cigar, pipe, chocolate) \
with one drink (whisky, coffee, beer, rakı) — never same-side.

For EACH item in the provided list, propose 1-2 `pairings`, each pairing THAT \
item with the OPPOSITE side (a companion → a drink; a drink → a companion):
- `profile`: the ideal partner archetype, SPECIFIC and tightly-specified ("a \
sherry-cask matured 10+ yo single malt", "a natural-process Ethiopian with \
berry acidity"), never a generic category like "a whisky" or "espresso".
- `matches`: 0-2 items FROM THE PROVIDED LIST that are on the opposite side and \
fit the profile, each referenced by its exact `_id`. Empty if nothing in the \
list fits — the profile alone is the recommendation. Never invent ids.
- `reason`: why it complements this item's character.

For CIGAR and PIPE only, ALSO set `cocktails`: 1-2 of the most common CLASSICAL \
cocktails that suit it (Old Fashioned, Manhattan, Negroni, Sazerac, Whiskey \
Sour, Daiquiri, Boulevardier, …), each with a `name` and a short `reason`. A \
cocktail accompanies a smoke, so ALL other items (chocolate and the drinks) \
leave `cocktails` empty.

Rules:
- Produce a change for EVERY item note (its fresh pairings replace any old \
ones). Include `doc_id` (the item's _id), `uid` if present, `name`, and `type`.
- Draw matches only from the provided list. Use web_search only to inform a \
profile, never to add items the user doesn't own.
- Give a short overall `summary`.
"""


def _repair_compact(records: list[dict]) -> list[dict]:
    return [{k: r[k] for k in _REPAIR_FIELDS if k in r} for r in records]


async def run_repair_pairings_plan(
    manage_id: str,
    settings: Settings,
    db: CouchDBClient,
    *,
    model_override: str | None = None,
) -> dict:
    """Propose (do not apply) fresh cross-category pairings for every item note,
    reusing the manage plan/approve/apply flow. The approved changes carry a
    structured `pairings` list that _apply_one writes to `pairings_suggested`.

    Chunked: one change per item means the output grows with the vault and would
    eventually overflow max_tokens_manage in a single response. So items are
    processed in batches of `repair_batch_size`, each batch a separate model call
    whose output is bounded; every call still sees the FULL inventory so matches
    can be drawn from the whole vault, not just the batch. Results are merged."""
    claude_cfg = settings.models.claude
    model = model_override or claude_cfg.text_model

    records = await query_all_items(db)
    items = [r for r in records if r.get("type") != "pairing"]
    all_compact = _repair_compact(items)

    async def _run_batch(targets: list[dict], batch_no: int) -> list[dict]:
        prompt = (
            "Regenerate cross-category pairing suggestions. Draw all `matches` from "
            f"the FULL inventory below.\n\nFull inventory ({len(items)}):\n"
            f"{json.dumps(all_compact)}\n\n"
            f"Produce a change for ONLY these {len(targets)} target items:\n"
            f"{json.dumps(_repair_compact(targets))}"
        )
        batch_id = f"{manage_id}#b{batch_no}"
        if (provider := provider_for(model)) is not None:
            data = await provider.extract_structured(
                settings, db,
                job_id=batch_id, model=model,
                system_prompt=REPAIR_SYSTEM_PROMPT, text=prompt,
                image_b64=None, image_media_type=None,
                use_web_search=True, output_schema=REPAIR_PAIRINGS_SCHEMA,
                site="repair", max_output_tokens=claude_cfg.max_tokens_manage,
            )
        else:
            client = get_claude_client(settings)
            tools: list[dict[str, Any]] = [
                QUERY_NOTES_TOOL,
                {"type": "web_search_20260209", "name": "web_search", "max_uses": claude_cfg.web_search_max_uses},
            ]
            messages: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
            data = await _run_plan_loop(
                client, claude_cfg, model, tools, messages, db, manage_id=batch_id,
                system=REPAIR_SYSTEM_PROMPT, output_schema=REPAIR_PAIRINGS_SCHEMA, site="repair",
            )
        return data.get("changes") or []

    size = max(1, claude_cfg.repair_batch_size)
    batches = [items[i:i + size] for i in range(0, len(items), size)]
    all_changes: list[dict] = []
    for n, batch in enumerate(batches, 1):
        batch_changes = await _run_batch(batch, n)
        all_changes.extend(batch_changes)
        logger.info(
            "repair pairings %s: batch %d/%d -> %d change(s)",
            manage_id, n, len(batches), len(batch_changes),
        )

    logger.info(
        "repair pairings %s: %d item(s) proposed across %d batch(es)",
        manage_id, len(all_changes), len(batches),
    )
    return {
        "summary": f"Regenerated pairings for {len(all_changes)} item(s) in {len(batches)} batch(es).",
        "changes": all_changes,
    }


async def run_manage_plan(
    manage_id: str,
    settings: Settings,
    db: CouchDBClient,
    *,
    instruction: str,
    model_override: str | None = None,
) -> dict:
    """Produce (do not apply) a plan of record changes for `instruction`."""
    claude_cfg = settings.models.claude
    model = model_override or claude_cfg.text_model

    records = await query_all_items(db)
    prompt = (
        f"Instruction:\n{instruction}\n\n"
        f"Current records ({len(records)}):\n{json.dumps(_compact(records))}"
    )

    if (provider := provider_for(model)) is not None:
        data = await provider.extract_structured(
            settings, db,
            job_id=manage_id,
            model=model,
            system_prompt=SYSTEM_PROMPT,
            text=prompt,
            image_b64=None,
            image_media_type=None,
            use_web_search=True,
            output_schema=MANAGE_PLAN_SCHEMA,
            site="manage",
            max_output_tokens=claude_cfg.max_tokens_manage,
        )
    else:
        client = get_claude_client(settings)
        tools: list[dict[str, Any]] = [
            QUERY_NOTES_TOOL,
            # A maintenance plan researches ONE FACT PER RECORD ("give every
            # whisky its ABV"), so the per-capture budget of 3 is the wrong
            # scale here — it caps the plan at three researched records however
            # many need one, and the planner fills the rest with intentions
            # instead of values. See web_search_max_uses_manage.
            {"type": "web_search_20260209", "name": "web_search",
             "max_uses": claude_cfg.web_search_max_uses_manage},
        ]
        messages: list[dict[str, Any]] = [{"role": "user", "content": [{"type": "text", "text": prompt}]}]
        data = await _run_plan_loop(client, claude_cfg, model, tools, messages, db, manage_id=manage_id)

    changes = data.get("changes") or []
    logger.info("manage plan %s: %d proposed change(s)", manage_id, len(changes))
    # Shape the relay/PWA sees. `applied=False` until an apply job runs.
    return {"summary": data.get("summary", ""), "changes": changes}


async def _run_plan_loop(client, claude_cfg, model, tools, messages, db, *, manage_id,
                         system=SYSTEM_PROMPT, output_schema=MANAGE_PLAN_SCHEMA, site="manage"):
    web_search = any("web_search" in str(t.get("type", "")) for t in tools)
    iterations = query_notes_calls = input_tokens = output_tokens = web_searches = 0
    stop_reason: str | None = None
    # See capture_service._run_extraction_loop: web_search_20260209 runs code
    # server-side to filter results, and the container it provisions must be
    # named on every subsequent turn of the conversation.
    container_id: str | None = None
    started = time.monotonic()
    summary_logged = False

    def emit_summary() -> None:
        nonlocal summary_logged
        if summary_logged:
            return
        summary_logged = True
        log_call_summary(
            job_id=manage_id, site=site, model=model, stop_reason=stop_reason,
            input_tokens=input_tokens, output_tokens=output_tokens, iterations=iterations,
            query_notes=query_notes_calls, web_search=web_search,
            web_searches=web_searches, duration_s=time.monotonic() - started,
        )

    for _ in range(claude_cfg.max_tool_iterations):
        iterations += 1
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=claude_cfg.max_tokens_manage,
                thinking={"type": "adaptive"},
                output_config={
                    "effort": claude_cfg.effort,
                    "format": {"type": "json_schema", "schema": output_schema},
                },
                system=system,
                tools=tools,
                messages=messages,
                **({"container": container_id} if container_id else {}),
            )
        except anthropic.APIError as e:
            log_call_error(job_id=manage_id, site=site, model=model, exc=e)
            raise
        input_tokens += response.usage.input_tokens
        output_tokens += response.usage.output_tokens
        usage.record("anthropic", model, response.usage.input_tokens, response.usage.output_tokens)
        stop_reason = response.stop_reason
        web_searches += count_web_searches(response.content, manage_id, site)
        if response.container is not None:
            container_id = response.container.id

        if response.stop_reason == "refusal":
            logger.warning("manage plan %s claude refusal", manage_id)
            emit_summary()
            detail = getattr(getattr(response, "stop_details", None), "category", None)
            raise refused("anthropic", detail)

        if response.stop_reason == "max_tokens":
            logger.warning("manage plan %s hit max_tokens (plan too large)", manage_id)
            emit_summary()
            raise ManageFailed(
                "the plan was too large for one response (hit the output token limit) — "
                "narrow the instruction (e.g. one item type at a time) or raise "
                "max_tokens_manage in config.yaml"
            )

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
            logger.warning("manage plan %s claude pause_turn resume (iteration %d)", manage_id, iterations)
            messages.append({"role": "assistant", "content": response.content})
            continue

        emit_summary()
        text_block = next((b for b in response.content if b.type == "text"), None)
        if text_block is None:
            raise no_text_output("anthropic")
        return loads_model_json(text_block.text, "anthropic")

    emit_summary()
    raise ManageFailed("exceeded max tool-use iterations without a plan")


class ManageFailed(ModelOutputError):
    """Same rationale as capture's CaptureFailed: one exception type covers a
    maintenance run that can't produce a plan, whichever provider ran it. The
    truncation message here stays manage-specific on purpose — "narrow the
    instruction" is more actionable than the generic budget advice."""


async def run_manage_apply(
    manage_id: str,
    settings: Settings,
    db: CouchDBClient,
    *,
    changes: list[dict],
) -> dict:
    """Apply the human-approved subset of a plan. Each change is independent:
    a validation failure fails only that record."""
    results: list[dict] = []
    applied = failed = 0

    for change in changes:
        name = change.get("name") or change.get("doc_id")
        try:
            await _apply_one(db, change)
            applied += 1
            results.append({"doc_id": change.get("doc_id"), "name": name, "status": "applied"})
            logger.info("manage apply %s: updated doc_id=%s", manage_id, change.get("doc_id"))
        except Exception as e:  # noqa: BLE001 — one bad change must not sink the batch
            failed += 1
            results.append({"doc_id": change.get("doc_id"), "name": name, "status": "failed", "error": str(e)})
            logger.warning("manage apply %s: failed doc_id=%s: %s", manage_id, change.get("doc_id"), e)

    logger.info("manage apply %s: applied=%d failed=%d", manage_id, applied, failed)
    return {"applied": applied, "failed": failed, "results": results}


async def _apply_one(db: CouchDBClient, change: dict) -> None:
    # Resolve the target by uid first (stable across renames), else by _id.
    target: dict | None = None
    uid = change.get("uid")
    if uid:
        matches = await db.find({"uid": uid}, limit=1)
        target = matches[0] if matches else None
    if target is None and change.get("doc_id"):
        target = await db.get_document(change["doc_id"])
    if target is None:
        raise ManageFailed("record not found")

    edits = [e for e in (change.get("edits") or []) if e.get("field")]
    # A change that carries no instruction mutates nothing, validates cleanly,
    # and used to be reported as "applied" — the worst possible outcome, since
    # the plan looked like it worked. Seen for real when a planner proposed ten
    # `common_notes` backfills as reasons ("needs web-searched tasting profile")
    # without ever filling in the values. Nothing to apply is a failed change,
    # not an applied one.
    mints_uid = bool(change.get("generate_uid")) and not target.get("uid")
    if not (edits or mints_uid
            or change.get("pairings") is not None
            or change.get("cocktails") is not None):
        raise ManageFailed(
            "change carries no edits — the planner described the change "
            "without filling in a value"
        )

    data = {k: v for k, v in target.items() if k not in ("_rev", "markdown")}
    for edit in edits:
        data[edit["field"]] = edit.get("value")

    # Regenerate-pairings changes carry a structured `pairings` list (which the
    # {field, value:string} edits can't express); write it to the note's
    # pairings_suggested, re-validated below like any other change.
    if change.get("pairings") is not None:
        data["pairings_suggested"] = change["pairings"]
    # Cigar/pipe also carry classical `cocktails` (dropped by the schema for
    # any other item via _cocktails_smoke_only).
    if change.get("cocktails") is not None:
        data["cocktail_pairings"] = change["cocktails"]

    # Ensure a stable identity: mint on request, or carry the existing one.
    if change.get("generate_uid") and not data.get("uid"):
        data["uid"] = uuid.uuid4().hex
    elif not data.get("uid") and target.get("uid"):
        data["uid"] = target["uid"]

    note = parse_any_note(data)  # schema is the enforcement layer (raises on bad edit)
    markdown = render_markdown(note)
    await db.update_note(note, markdown, doc_id=target["_id"], rev=target["_rev"])

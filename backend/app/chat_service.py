"""Chat over the whole vault: answer questions, and propose maintenance actions.

Two jobs in one conversation:

  * Questions ("is there a pattern in my favourite whiskies?") — answered from
    the vault itself. The model gets every record in compact form PLUS
    precomputed statistics (counts, mean rating per country / region / cask /
    age band …, and how many fields are empty). The numbers are computed here,
    not by the model: language models miscount, and a pattern claim is only
    worth anything with the real count behind it.

  * Requests ("fetch all missing ABVs", "re-match the whiskies") — never run by
    the model. It proposes ACTIONS with a loose selector (fields, types, or
    specific ids); this module resolves the selector against the vault into the
    exact items and the exact number of lookups, and the person confirms with a
    button. Confirming starts the same fetch-details / pairing jobs the buttons
    start, whose results still go through the review step — chat adds a way to
    ask, not a way around the checks.

The model cannot edit values, delete or capture; it says so and points to the
right place.
"""
from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from typing import Any

from app.config import Settings
from app.couchdb_client import CouchDBClient
from app.detail_fetch import FETCHABLE, is_empty
from app.items_query import query_all_items
from app.model_output import ModelOutputError
from app.providers import provider_for

logger = logging.getLogger("worker.chat")

MAX_TURNS = 12          # earlier turns are dropped, not summarised
MAX_MESSAGE_CHARS = 4000
_PAIR_CHUNK = 5         # items per pairing job — the repair_batch_size default

ACTION_KINDS = ("fetch_details", "regenerate_pairings", "rematch_pairings")

_ALL_FETCHABLE = sorted({f for fs in FETCHABLE.values() for f in fs})

_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "your answer to the user, plain text, short"},
        "actions": {
            "type": "array",
            "description": "maintenance actions to PROPOSE (the user confirms each); empty for a plain answer",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(ACTION_KINDS)},
                    "fields": {"type": "array", "items": {"type": "string", "enum": _ALL_FETCHABLE},
                               "description": "fetch_details only: which fields; omit for every empty field"},
                    "types": {"type": "array", "items": {"type": "string"},
                              "description": "limit to these item types (whisky, cigar, …); omit for all"},
                    "doc_ids": {"type": "array", "items": {"type": "string"},
                                "description": "limit to these exact _ids from the data; omit to use the other filters"},
                    "only_empty": {"type": "boolean", "description": "fetch_details: true (default) fills empty fields only; false also re-checks filled ones"},
                    "why": {"type": "string", "description": "one short line describing the action"},
                },
                "required": ["kind"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["reply"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = f"""\
You are the assistant for a personal tasting log (whisky, cigars, coffee, beer, \
pipe tobacco, chocolate, rakı). You are given the user's whole vault as JSON \
(`items`), precomputed `stats`, and `gaps` (how many records have each field \
empty), then the conversation.

Answering questions
- Use only that data. If it cannot answer the question, say so — never invent \
items, ratings or facts about the user's taste.
- For patterns ("what do my favourites have in common?") lean on `stats`: it \
holds the real counts and mean ratings per feature. Quote the numbers, name \
the items, and be plain about small samples (fewer than 3 items is an \
anecdote, not a pattern). Ratings are 1-5; only tasted items carry one.
- Keep replies short and conversational — a chat message, not a report.

Requests to change data
- You cannot edit a value, delete, or capture a tasting; say so and point to \
the item's edit view or the capture tab.
- You CAN propose actions. They are not run by you: the user sees each as a \
button and confirms it, so describe in `reply` what you are proposing and never \
say it is done.
  - fetch_details: web-search lookups for missing details. Name the `fields` \
({', '.join(_ALL_FETCHABLE)}) and limit by `types` or `doc_ids`. Leave \
`only_empty` true unless the user asks to re-check values already filled in. \
"Fetch all missing ABVs" is fields=["abv"], no other filters — the server picks \
every item that has an abv field and has it empty.
  - regenerate_pairings / rematch_pairings: rebuild an item's pairing \
suggestions, or keep the profiles and only redo the matching. Limit with \
`types` or `doc_ids`; with neither, it means every item.
- doc_ids must be exact `_id` values from `items`. If the user means one \
particular item, use its `_id`.
- If nothing needs doing (no gaps), say so instead of proposing an action.
"""

_ITEM_KEYS = (
    "_id", "type", "producer", "name", "country_of_origin", "region", "bottler", "category",
    "age_years", "abv", "cask", "peated", "style", "ibu", "roaster", "origin", "process",
    "roast_level", "wrapper", "vitola", "strength", "blend_type", "cut", "chocolate_type",
    "cacao_percent", "cacao_origin", "raki_base", "rating", "status", "stock", "tags", "date",
)


def _compact(item: dict[str, Any]) -> dict[str, Any]:
    out = {k: item[k] for k in _ITEM_KEYS if not is_empty(k, item.get(k))}
    if (notes := (item.get("notes") or "").strip()):
        out["my_notes"] = notes[:160]
    if (item.get("common_notes") or "").strip():
        out["has_common_notes"] = True
    return out


def _bucket(field: str, value: Any) -> Any:
    if field == "age_years" and isinstance(value, (int, float)):
        return "<10" if value < 10 else "10-14" if value < 15 else "15-19" if value < 20 else "20+"
    if field == "abv" and isinstance(value, (int, float)):
        return "<43" if value < 43 else "43-46" if value < 46 else "46-50" if value < 50 else "50+"
    return value


_FACETS = ("country_of_origin", "region", "category", "peated", "cask", "age_years", "abv", "producer",
           "bottler", "style", "roast_level", "origin", "process", "wrapper", "strength", "blend_type",
           "chocolate_type", "cacao_origin")


def build_stats(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts and mean rating per type and per feature value — computed here so
    the model can quote them rather than count."""
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for it in items:
        by_type[it.get("type", "?")].append(it)
    stats: dict[str, Any] = {}
    for t, group in sorted(by_type.items()):
        rated = [i for i in group if isinstance(i.get("rating"), (int, float))]
        entry: dict[str, Any] = {
            "items": len(group), "rated": len(rated),
            "mean_rating": round(sum(i["rating"] for i in rated) / len(rated), 2) if rated else None,
        }
        top = [i for i in rated if i["rating"] >= 4]
        entry["rated_4_or_more"] = len(top)
        facets: dict[str, Any] = {}
        for f in _FACETS:
            groups: dict[Any, list[float]] = defaultdict(list)
            for i in rated:
                v = _bucket(f, i.get(f))
                if not is_empty(f, v):
                    groups[str(v)].append(i["rating"])
            rows = [(v, len(rs), round(sum(rs) / len(rs), 2)) for v, rs in groups.items() if len(rs) >= 2]
            if rows:
                rows.sort(key=lambda r: (-r[2], -r[1]))
                facets[f] = [{"value": v, "n": n, "mean_rating": m} for v, n, m in rows[:8]]
        if facets:
            entry["mean_rating_by"] = facets
        stats[t] = entry
    return stats


def build_gaps(items: list[dict[str, Any]]) -> dict[str, Any]:
    gaps: dict[str, Any] = {}
    for t, fields in FETCHABLE.items():
        group = [i for i in items if i.get("type") == t]
        if not group:
            continue
        gaps[t] = {"items": len(group), "empty": {f: sum(is_empty(f, i.get(f)) for i in group) for f in fields}}
    return gaps


def _resolve(action: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Turn the model's loose selector into the exact items and cost, or None if
    it selects nothing."""
    kind = action.get("kind")
    if kind not in ACTION_KINDS:
        return None
    by_id = {i["_id"]: i for i in items if i.get("_id")}
    if action.get("doc_ids"):
        pool = [by_id[d] for d in dict.fromkeys(action["doc_ids"]) if d in by_id]
    else:
        pool = list(items)
    if action.get("types"):
        pool = [i for i in pool if i.get("type") in set(action["types"])]
    pool = [i for i in pool if i.get("type") in FETCHABLE]  # real item types only

    if kind == "fetch_details":
        fields = [f for f in (action.get("fields") or []) if f in _ALL_FETCHABLE] or None
        overwrite = action.get("only_empty") is False
        targets, lookups = [], 0
        for i in pool:
            allowed = FETCHABLE[i["type"]]
            wanted = [f for f in (fields or allowed) if f in allowed]
            if not fields:
                wanted = [f for f in wanted if f != "bottler"]
            need = [f for f in wanted if overwrite or is_empty(f, i.get(f))]
            if need:
                targets.append(i["_id"])
                lookups += len(need)
        if not targets:
            return None
        label = f"Look up {', '.join(fields) if fields else 'missing details'} for {len(targets)} item(s)"
        return {"kind": kind, "doc_ids": targets, "fields": fields, "overwrite": overwrite,
                "items": len(targets), "lookups": lookups,
                "label": label, "detail": f"{lookups} web-search lookup(s) plus one review call per item; proposals only, nothing is saved until you accept."}

    ids = [i["_id"] for i in pool if i.get("_id")]
    if not ids:
        return None
    mode = "rematch" if kind == "rematch_pairings" else "regenerate"
    calls = 0 if mode == "rematch" else math.ceil(len(ids) / _PAIR_CHUNK)
    return {"kind": kind, "doc_ids": ids, "mode": mode, "items": len(ids), "lookups": calls,
            "label": f"{'Re-match' if mode == 'rematch' else 'Regenerate'} pairings for {len(ids)} item(s)",
            "detail": ("no model calls, only the matching step" if mode == "rematch" else f"about {calls} model call(s)")
                      + "; applied as it goes, the previous pairings are kept for undo."}


def _clean_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    out = []
    for m in messages[-MAX_TURNS:]:
        role = "assistant" if m.get("role") == "assistant" else "user"
        text = str(m.get("content") or "").strip()[:MAX_MESSAGE_CHARS]
        if text:
            out.append({"role": role, "content": text})
    if not out or out[-1]["role"] != "user":
        raise ValueError("the last message must be from the user")
    return out


async def run_chat(
    job_id: str,
    settings: Settings,
    db: CouchDBClient,
    *,
    messages: list[dict[str, Any]],
    model_override: str | None = None,
) -> dict[str, Any]:
    """One chat turn: the reply, plus resolved actions awaiting confirmation."""
    convo = _clean_messages(messages)
    model = model_override or settings.models.claude.text_model
    provider = provider_for(model)
    if provider is None:
        raise ModelOutputError(f"{model!r} is not a recognised model id")

    items = [r for r in await query_all_items(db) if r.get("type") != "pairing"]
    context = {
        "items": [_compact(i) for i in items],
        "stats": build_stats(items),
        "gaps": build_gaps(items),
    }
    transcript = "\n".join(f"{'User' if m['role'] == 'user' else 'Assistant'}: {m['content']}" for m in convo)
    text = f"VAULT DATA:\n{json.dumps(context, ensure_ascii=False, separators=(',', ':'))}\n\nCONVERSATION:\n{transcript}"

    out = await provider.extract_structured(
        settings, db, job_id=job_id, model=model,
        system_prompt=SYSTEM_PROMPT, text=text,
        image_b64=None, image_media_type=None,
        use_web_search=False, output_schema=_SCHEMA, site="chat",
    )
    reply = str(out.get("reply") or "").strip()
    if not reply:
        raise ModelOutputError("the model returned no reply")
    actions, dropped = [], 0
    for a in out.get("actions") or []:
        resolved = _resolve(a, items)
        if resolved is None:
            dropped += 1
            continue
        resolved["why"] = str(a.get("why") or "")
        actions.append(resolved)
    if dropped and not actions:
        reply += "\n\n(There was nothing to do for that request — no matching items have gaps.)"
    logger.info("chat %s: %d item(s) in context, %d action(s) proposed, %d dropped",
                job_id, len(items), len(actions), dropped)
    return {"reply": reply, "actions": actions}

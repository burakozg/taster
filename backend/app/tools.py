"""The `query_notes` tool — read-only vault search, shared by capture
(repeat-detection, vault-grounded pairing suggestions, pairing resolution)
and lookup (§4.5). Never used to write; that's the one hard rule."""
from __future__ import annotations

from typing import Any

from app.categories import NOTE_TYPES
from app.couchdb_client import CouchDBClient

QUERY_NOTES_TOOL = {
    "name": "query_notes",
    "description": (
        "Search the tasting vault. Read-only — cannot create, modify, or delete notes. "
        "Use this to: find prior notes by name/producer (repeat-detection, resolving a "
        "pairing report to real item ids), filter by type/region/rating/status/tags to "
        "answer lookup questions, or ground pairing suggestions in items actually tasted "
        "and rated rather than guessing generically."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": list(NOTE_TYPES)},
            "name_contains": {"type": "string", "description": "case-insensitive substring match on name"},
            "producer_contains": {"type": "string", "description": "case-insensitive substring match on producer"},
            "region": {"type": "string"},
            "min_rating": {"type": "number", "description": "only notes rated at least this high (ratings are 1-5, one decimal)"},
            "status": {"type": "string", "enum": ["tasted", "to-try"]},
            "tags": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer", "description": "max results, default 10"},
        },
        "additionalProperties": False,
    },
}


async def query_notes_impl(db: CouchDBClient, tool_input: dict[str, Any]) -> list[dict]:
    # Default to our note types — the db also holds LiveSync's internal
    # documents (chunks, file entries), which must never match a query.
    selector: dict[str, Any] = {"type": {"$in": list(NOTE_TYPES)}}
    if t := tool_input.get("type"):
        selector["type"] = t
    if status := tool_input.get("status"):
        selector["status"] = status
    if region := tool_input.get("region"):
        selector["region"] = region
    if min_rating := tool_input.get("min_rating"):
        selector["rating"] = {"$gte": min_rating}
    if tags := tool_input.get("tags"):
        selector["tags"] = {"$all": tags}

    # CouchDB Mango has no case-insensitive substring operator; $regex is the
    # closest built-in. Fine at personal-vault scale (hundreds of docs), and
    # keeps this a single query instead of pulling everything client-side.
    if name_contains := tool_input.get("name_contains"):
        selector["name"] = {"$regex": f"(?i){_escape_regex(name_contains)}"}
    if producer_contains := tool_input.get("producer_contains"):
        selector["producer"] = {"$regex": f"(?i){_escape_regex(producer_contains)}"}

    limit = int(tool_input.get("limit") or 10)
    docs = await db.find(selector, limit=limit)

    # Trim to what the model actually needs — keep tokens down, drop the
    # rendered markdown blob (redundant with the structured fields).
    return [
        {k: v for k, v in doc.items() if k not in ("markdown", "_rev")}
        for doc in docs
    ]


def _escape_regex(s: str) -> str:
    import re

    return re.escape(s)

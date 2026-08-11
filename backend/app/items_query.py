"""Full items snapshot for the relay's /items cache — see worker.py.
Filtering by type/status/rating/tag now happens relay-side against this
snapshot, not here; the worker just pushes everything."""
from __future__ import annotations

from typing import Any

from app.categories import NOTE_TYPES
from app.couchdb_client import CouchDBClient


async def query_all_items(db: CouchDBClient, limit: int = 1000) -> list[dict[str, Any]]:
    # The db also holds LiveSync's own documents (entry docs typed
    # "plain"/"newnote", "leaf" chunks, versioninfo) — constrain to our note
    # types or they'd leak into the /items cache.
    docs = await db.find({"type": {"$in": list(NOTE_TYPES)}}, limit=limit)
    return [{k: v for k, v in d.items() if k not in ("markdown", "_rev")} for d in docs]

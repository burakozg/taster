"""GET /items — PWA-facing, served from the cached snapshot the QNAP worker
pushes periodically (see routes/worker.py's /worker/items/snapshot). Never
queries CouchDB directly — the relay has no way to reach it (LAN-only)."""
from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Query

from app.auth import require_client_key
from app.db import get_items_cache
from app.rate_limit import rate_limit

logger = logging.getLogger("relay.items")

# REL-4: log the empty cache ONCE, not per request — an empty cache means the
# worker has never pushed a snapshot, but /items is polled continuously.
_empty_cache_logged = False

router = APIRouter(
    prefix="/items",
    tags=["items"],
    dependencies=[Depends(require_client_key), Depends(rate_limit)],
)


@router.get("")
async def list_items(
    # Free-form so new categories (see the worker's app.categories registry)
    # need no relay change; unknown values simply match nothing.
    type: str | None = Query(default=None),
    status: Literal["tasted", "to-try"] | None = Query(default=None),
    min_rating: float | None = Query(default=None, ge=1, le=5),
    tag: str | None = Query(default=None),
) -> dict:
    items = get_items_cache()

    global _empty_cache_logged
    if not items and not _empty_cache_logged:
        logger.warning("items cache empty on query — worker has never pushed a snapshot")
        _empty_cache_logged = True
    elif items:
        _empty_cache_logged = False

    if type:
        items = [i for i in items if i.get("type") == type]
    if status:
        items = [i for i in items if i.get("status") == status]
    if min_rating:
        items = [i for i in items if (i.get("rating") or 0) >= min_rating]
    if tag:
        items = [i for i in items if tag in (i.get("tags") or [])]

    return {"items": items}

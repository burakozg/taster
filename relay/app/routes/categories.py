"""GET /categories — PWA-facing category registry (group order/labels + edit-
form field specs), served from the cache the QNAP worker pushes with each items
snapshot (see routes/worker.py and the worker's app.categories). Empty until the
first push arrives; the PWA falls back to its own baked list in that window."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import require_client_key
from app.db import get_categories_cache
from app.rate_limit import rate_limit

router = APIRouter(
    prefix="/categories",
    tags=["categories"],
    dependencies=[Depends(require_client_key), Depends(rate_limit)],
)


@router.get("")
async def list_categories() -> dict:
    return {"categories": get_categories_cache()}

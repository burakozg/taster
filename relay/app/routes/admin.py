"""/admin/* — the PWA's Admin tab. Same auth as the rest of the PWA
surface (client bearer key) + rate limit; nothing here is a separate
credential domain, it's a UI over relay state.

Model selection flow: settings saved here are attached by routes/worker.py
to every job the worker claims (`model_overrides`), so a change applies to
the NEXT capture/lookup — no worker restart, no new inbound path to the
QNAP. Unset settings mean the worker's baked-in config.yaml defaults."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth import require_client_key
from app.db import (
    get_admin_settings,
    get_items_cache,
    get_items_cache_updated_at,
    job_counts,
    list_recent_jobs,
    set_admin_settings,
    usage_by_day,
    usage_totals,
)
from app.models_catalog import MODEL_CATALOG, MODEL_IDS, prune_unknown_models
from app.rate_limit import rate_limit

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_client_key), Depends(rate_limit)],
)


@router.get("/models")
async def list_models() -> dict:
    return {"models": MODEL_CATALOG}


@router.get("/settings")
async def get_settings() -> dict:
    # Pruned so the Admin tab shows "using the default" rather than a retired
    # id the dropdown can no longer render — matches what the worker will
    # actually receive (see routes/worker.py).
    s = prune_unknown_models(get_admin_settings())
    return {
        "capture_model": s.get("capture_model"),
        "lookup_model": s.get("lookup_model"),
    }


class AdminSettings(BaseModel):
    # None = clear the override, fall back to the worker's config.yaml.
    capture_model: str | None = None
    lookup_model: str | None = None


@router.put("/settings")
async def put_settings(body: AdminSettings) -> dict:
    for field in ("capture_model", "lookup_model"):
        value = getattr(body, field)
        if value is not None and value not in MODEL_IDS:
            raise HTTPException(status_code=400, detail=f"{field}: unknown model id {value!r}")
    settings = {k: v for k, v in body.model_dump().items() if v is not None}
    set_admin_settings(settings)
    return {"ok": True, **body.model_dump()}


@router.get("/jobs")
async def recent_jobs(limit: int = Query(default=20, ge=1, le=100)) -> dict:
    return {"jobs": list_recent_jobs(limit)}


@router.get("/usage")
async def usage(days: int = Query(default=14, ge=1, le=365)) -> dict:
    """WRK-10 token ledger: the last `days` days with any usage, newest first,
    each with a per-model breakdown, plus all-time totals.

    Days are the worker's local dates (see routes/worker.py) — the relay only
    ever stores and returns the strings it was given, so this endpoint has no
    timezone of its own to get wrong."""
    return {"days": usage_by_day(days), "totals": usage_totals()}


@router.get("/status")
async def status() -> dict:
    return {
        "items_count": len(get_items_cache()),
        "snapshot_updated_at": get_items_cache_updated_at(),
        "job_counts": job_counts(),
    }

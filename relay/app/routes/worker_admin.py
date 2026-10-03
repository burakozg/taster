"""/worker/admin/* — what the NAS-side admin portal (taster-admin) reads and
changes on the relay.

The relay holds three things the portal needs to see: the model choices, the token
ledger, and the job queue. They used to be served to the phone app's Admin tab under
the client key; that whole surface moved to the NAS, so they are now reachable only
with the WORKER key — which lives on the NAS and never on a phone. A lost or shared
phone can capture a tasting; it can no longer change which models run or read the
job history.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth import require_worker_key
from app.db import (
    get_admin_settings,
    get_items_cache,
    get_items_cache_updated_at,
    job_counts,
    list_recent_jobs,
    set_admin_settings,
    usage_by_day,
    usage_by_task,
    usage_totals,
)
from app.models_catalog import (
    MODEL_CATALOG,
    MODEL_IDS,
    migrate_model_settings,
    prune_unknown_models,
)

ROLE_SETTINGS = ("vision_model", "research_model", "reasoning_model")

router = APIRouter(prefix="/worker/admin", tags=["worker-admin"], dependencies=[Depends(require_worker_key)])


@router.get("/models")
async def models() -> dict:
    return {"models": MODEL_CATALOG}


@router.get("/settings")
async def get_settings() -> dict:
    # Pruned so the page shows "default" rather than a retired id its dropdown
    # can no longer render — what the worker will actually receive.
    s = migrate_model_settings(prune_unknown_models(get_admin_settings()))
    return {role: s.get(role) for role in ROLE_SETTINGS}


class AdminSettings(BaseModel):
    # None = clear the override and fall back to the worker's config.yaml.
    vision_model: str | None = None
    research_model: str | None = None
    reasoning_model: str | None = None


@router.put("/settings")
async def put_settings(body: AdminSettings) -> dict:
    for field in ROLE_SETTINGS:
        value = getattr(body, field)
        if value is not None and value not in MODEL_IDS:
            raise HTTPException(status_code=400, detail=f"{field}: unknown model id {value!r}")
    set_admin_settings({k: v for k, v in body.model_dump().items() if v is not None})
    return {"ok": True, **body.model_dump()}


@router.get("/jobs")
async def recent_jobs(limit: int = Query(default=20, ge=1, le=100)) -> dict:
    return {"jobs": list_recent_jobs(limit)}


@router.get("/usage")
async def usage(days: int = Query(default=14, ge=1, le=365)) -> dict:
    """The token ledger: the last `days` days with any usage, newest first, each
    with a per-model breakdown, plus all-time totals and spend by task. Days are
    the reporting process's local dates — the relay only stores what it is given."""
    return {"days": usage_by_day(days), "totals": usage_totals(), "by_task": usage_by_task(days)}


@router.get("/status")
async def status() -> dict:
    return {
        "items_count": len(get_items_cache()),
        "snapshot_updated_at": get_items_cache_updated_at(),
        "job_counts": job_counts(),
    }

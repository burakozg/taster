"""/worker/* — the QNAP-only surface. Authenticated with a separate key
from the PWA-facing routes (see auth.py) so a leaked client-side key can
enqueue/poll jobs but never claim or complete one, or overwrite the items
cache.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth import require_worker_key
from app.config import Settings, get_settings
from app.db import (
    add_usage,
    claim_next_job,
    complete_job,
    fail_job,
    get_admin_settings,
    set_categories_cache,
    set_items_cache,
)
from app.models_catalog import prune_unknown_models

logger = logging.getLogger("relay.worker")

router = APIRouter(prefix="/worker", tags=["worker"], dependencies=[Depends(require_worker_key)])


@router.get("/jobs/next")
async def next_job(settings: Settings = Depends(get_settings)) -> dict | None:
    job = claim_next_job(settings.job_claim_timeout_s)
    if job is not None:
        # Admin-panel model choices ride along with the claim — the worker
        # applies them per job, so a dropdown change takes effect on the
        # very next capture/lookup without any restart or inbound call.
        # Pruned first: a stored id the provider has since retired would
        # otherwise fail every job until someone re-picked one by hand.
        job["model_overrides"] = prune_unknown_models(get_admin_settings())
    return job


class JobResult(BaseModel):
    status: Literal["done", "failed"]
    result: dict[str, Any] | None = None
    error: str | None = None


@router.post("/jobs/{job_id}/result")
async def post_result(job_id: str, body: JobResult) -> dict:
    if body.status == "done":
        complete_job(job_id, body.result or {})
    else:
        fail_job(job_id, body.error or "unknown error")
    return {"ok": True}


class ItemsSnapshot(BaseModel):
    items: list[dict[str, Any]]
    # Category metadata (group order/labels + edit-field specs) from the worker's
    # registry; optional so an older worker that doesn't send it still works.
    categories: list[dict[str, Any]] | None = None


@router.post("/items/snapshot")
async def items_snapshot(body: ItemsSnapshot) -> dict:
    set_items_cache(body.items)
    if body.categories is not None:
        set_categories_cache(body.categories)
    # REL-4: item count + payload size so the "did the snapshot the PWA shows
    # actually update?" question (goal 5) is answerable from the relay side.
    logger.info(
        "items snapshot accepted count=%d payload_bytes=%d",
        len(body.items), len(json.dumps(body.items)),
    )
    return {"ok": True, "count": len(body.items)}


class UsageRow(BaseModel):
    # The worker's LOCAL date (YYYY-MM-DD), stored verbatim — the relay never
    # re-derives a day from its own clock, which is what keeps the two sides
    # from disagreeing about where a day ends. See backend app/usage.py.
    day: str
    provider: str
    model: str
    # ge=0 because these are added to a running total: one negative row from a
    # mangled payload would silently walk the ledger backwards.
    calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class UsageReport(BaseModel):
    # Stable across retries of the same batch, so a push the relay applied but
    # never managed to acknowledge is ignored the second time instead of
    # double-counting real spend.
    report_id: str
    rows: list[UsageRow]


@router.post("/usage")
async def usage_report(body: UsageReport) -> dict:
    """WRK-10: accept a batch of token-usage deltas from the worker."""
    rows = [r.model_dump() for r in body.rows]
    applied = add_usage(body.report_id, rows)
    if not applied:
        # Not an error: the worker is retrying a batch whose ack it never saw.
        # Logged all the same — a steady stream of these means acks are being
        # lost every time, which is a broken push loop wearing a benign face.
        logger.info("usage report ignored (duplicate) report_id=%s rows=%d", body.report_id, len(rows))
        return {"ok": True, "applied": False}
    logger.info(
        "usage report accepted report_id=%s rows=%d calls=%d input_tokens=%d output_tokens=%d",
        body.report_id, len(rows), sum(r["calls"] for r in rows),
        sum(r["input_tokens"] for r in rows), sum(r["output_tokens"] for r in rows),
    )
    return {"ok": True, "applied": True}

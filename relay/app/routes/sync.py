"""/sync/* — forced full-vault sync (Admin "Sync" panel).

The DB (JSON records) and the Obsidian vault (LiveSync files) can drift apart;
these are the manual reconcile controls. Same job broker pattern as
capture/lookup/manage — the worker does the CouchDB work (sync_service.py):

  POST /sync/status          -> counts records vs. vault files
  POST /sync/rebuild-vault   -> DB → Obsidian, re-projects every record's file
  POST /sync/rebuild-records -> Obsidian → DB, upsert-only (never deletes)
  POST /sync/normalize       -> re-validate every record to the current schema
  GET  /sync/{id}            -> poll for the result

The Obsidian → DB direction is deliberately upsert-only — it never deletes a
record whose file is missing, so it's safe even against an empty vault.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.auth import require_client_key
from app.db import create_job, get_job
from app.rate_limit import rate_limit

router = APIRouter(
    prefix="/sync",
    tags=["sync"],
    dependencies=[Depends(require_client_key), Depends(rate_limit)],
)


@router.post("/status", status_code=202)
async def status() -> dict:
    return {"sync_id": create_job("sync_status", {}), "status": "pending"}


@router.post("/rebuild-vault", status_code=202)
async def rebuild_vault() -> dict:
    return {"sync_id": create_job("sync_rebuild_vault", {}), "status": "pending"}


@router.post("/rebuild-records", status_code=202)
async def rebuild_records() -> dict:
    # Obsidian → DB, upsert-only (never deletes) — see sync_service.py.
    return {"sync_id": create_job("sync_rebuild_records", {}), "status": "pending"}


@router.post("/normalize", status_code=202)
async def normalize() -> dict:
    # Deterministic schema-drift fix (e.g. origin_country -> country_of_origin).
    return {"sync_id": create_job("sync_normalize", {}), "status": "pending"}


@router.get("/{sync_id}")
async def sync_status(sync_id: str) -> dict:
    job = get_job(sync_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown sync id")
    response = {"sync_id": sync_id, "status": job["status"]}
    if job["status"] == "done":
        response.update(job["result"] or {})
    elif job["status"] == "failed":
        response["error"] = job["error"]
    return response

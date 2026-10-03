"""/data/* — the phone app's way into the maintenance jobs the admin page also runs.

The worker executes the same functions taster-admin calls (pairing regeneration,
detail fetching, applying accepted details), so this file only queues them:

  POST /data/pairings        {doc_ids | all, mode}  chunked pairing jobs
  POST /data/details         {doc_id, fields?, overwrite?}  look up missing details
                             (proposals only — nothing is written)
  POST /data/details/apply   {doc_id, fields}       write the accepted proposals
  GET  /data/jobs?ids=       status + result of those jobs

Pairing work is many small jobs (CHUNK items each), written as each finishes, so a
failure costs one chunk and the run can outlast the claim timeout safely.
"""
from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth import require_client_key
from app.db import create_job, get_items_cache, get_jobs
from app.rate_limit import rate_limit

router = APIRouter(
    prefix="/data",
    tags=["data"],
    dependencies=[Depends(require_client_key), Depends(rate_limit)],
)

# The worker's repair_batch_size: what one model call handles well.
CHUNK = 5
MAX_ITEMS = 500


class PairingsRequest(BaseModel):
    doc_ids: list[str] | None = None
    all: bool = False
    # regenerate: new profiles + matches. rematch: keep the stored profiles and
    # redo only the matching against the vault as it is now (no model call).
    mode: Literal["regenerate", "rematch"] = "regenerate"


@router.post("/pairings", status_code=202)
async def pairings(body: PairingsRequest) -> dict:
    if body.all:
        ids = [i["_id"] for i in get_items_cache() if i.get("_id") and i.get("type") != "pairing"]
    else:
        ids = list(dict.fromkeys(body.doc_ids or []))
    if not ids:
        raise HTTPException(status_code=400, detail="no items selected")
    if len(ids) > MAX_ITEMS:
        raise HTTPException(status_code=400, detail=f"at most {MAX_ITEMS} items per request")
    jobs = []
    for n in range(0, len(ids), CHUNK):
        chunk = ids[n:n + CHUNK]
        jobs.append({
            "job_id": create_job("repair_pairings_items", {"doc_ids": chunk, "mode": body.mode}),
            "doc_ids": chunk,
        })
    return {"jobs": jobs, "items": len(ids)}


class DetailsRequest(BaseModel):
    doc_id: str
    fields: list[str] | None = None
    overwrite: bool = False


@router.post("/details", status_code=202)
async def details(body: DetailsRequest) -> dict:
    job_id = create_job("fetch_details", body.model_dump())
    return {"job_id": job_id, "status": "pending"}


class ApplyRequest(BaseModel):
    doc_id: str
    fields: dict[str, Any]


@router.post("/details/apply", status_code=202)
async def details_apply(body: ApplyRequest) -> dict:
    if not body.fields:
        raise HTTPException(status_code=400, detail="no fields to apply")
    job_id = create_job("details_apply", body.model_dump())
    return {"job_id": job_id, "status": "pending"}


@router.get("/jobs")
async def jobs(ids: str = Query(..., description="comma-separated job ids")) -> dict:
    wanted = [i for i in ids.split(",") if i][:200]
    return {"jobs": [
        {"job_id": j["id"], "status": j["status"], "error": j["error"], "result": j["result"]}
        for j in get_jobs(wanted)
    ]}

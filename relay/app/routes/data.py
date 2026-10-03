"""/data/* — the phone app's way into fetching missing details for one item.

Everything administrative (pairings, sync, models, usage, bulk maintenance) lives
on the NAS in taster-admin; the phone keeps only what belongs next to an item:

  POST /data/details         {doc_id, fields?, overwrite?}  look up missing details
                             (proposals only — nothing is written)
  POST /data/details/apply   {doc_id, fields}               write the accepted proposals
  GET  /data/jobs?ids=       status + result of those jobs

The worker runs the same `fetch_details` / `apply_details` functions taster-admin
calls, so the two front ends cannot drift apart.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth import require_client_key
from app.db import create_job, get_jobs
from app.rate_limit import rate_limit

router = APIRouter(
    prefix="/data",
    tags=["data"],
    dependencies=[Depends(require_client_key), Depends(rate_limit)],
)


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

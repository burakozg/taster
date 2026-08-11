"""/manage/* — the PWA's AI vault-maintenance surface (Admin "Maintain").

Same client bearer key + rate limit as the rest of the PWA API. The flow is
deliberately two-step so nothing is written without human approval:

  POST /manage           {instruction}      -> enqueues a `manage_plan` job
  GET  /manage/{id}                          -> poll; returns the proposed plan
  POST /manage/apply     {changes:[...]}     -> enqueues a `manage_apply` job
  GET  /manage/{id}                          -> poll; returns the apply summary

The worker does the actual Claude/CouchDB work (see manage_service.py); the
relay only brokers the jobs, exactly like capture/lookup.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import require_client_key
from app.db import create_job, get_job
from app.rate_limit import rate_limit

router = APIRouter(
    prefix="/manage",
    tags=["manage"],
    dependencies=[Depends(require_client_key), Depends(rate_limit)],
)


class ManageRequest(BaseModel):
    instruction: str


@router.post("", status_code=202)
async def propose(body: ManageRequest) -> dict:
    if not body.instruction.strip():
        raise HTTPException(status_code=400, detail="instruction must not be empty")
    job_id = create_job("manage_plan", {"instruction": body.instruction})
    return {"manage_id": job_id, "status": "pending"}


@router.post("/repair-pairings", status_code=202)
async def repair_pairings() -> dict:
    """Enqueue a regenerate-pairings plan — fresh cross-category pairings for
    every item note, reviewed and applied through the same /apply flow."""
    job_id = create_job("repair_pairings_plan", {})
    return {"manage_id": job_id, "status": "pending"}


class ApplyRequest(BaseModel):
    changes: list[dict[str, Any]]


@router.post("/apply", status_code=202)
async def apply(body: ApplyRequest) -> dict:
    if not body.changes:
        raise HTTPException(status_code=400, detail="no changes to apply")
    job_id = create_job("manage_apply", {"changes": body.changes})
    return {"manage_id": job_id, "status": "pending"}


@router.get("/{manage_id}")
async def manage_status(manage_id: str) -> dict:
    job = get_job(manage_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown manage id")
    response = {"manage_id": manage_id, "status": job["status"]}
    if job["status"] == "done":
        response.update(job["result"] or {})
    elif job["status"] == "failed":
        response["error"] = job["error"]
    return response

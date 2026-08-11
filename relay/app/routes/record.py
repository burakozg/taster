"""/record/* — edit or delete a single record from the Search detail view.

Same client bearer key + rate limit as the rest of the PWA API; same job
broker pattern (the worker does the CouchDB work, see record_service.py):

  POST /record/update  {doc_id, fields}  -> enqueue a record_update job
  POST /record/delete  {doc_id}          -> enqueue a record_delete job
  GET  /record/{id}                       -> poll for the result
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import require_client_key
from app.db import create_job, get_job
from app.rate_limit import rate_limit

router = APIRouter(
    prefix="/record",
    tags=["record"],
    dependencies=[Depends(require_client_key), Depends(rate_limit)],
)


class UpdateRequest(BaseModel):
    doc_id: str
    fields: dict[str, Any]


@router.post("/update", status_code=202)
async def update(body: UpdateRequest) -> dict:
    if not body.doc_id:
        raise HTTPException(status_code=400, detail="doc_id required")
    if not body.fields:
        raise HTTPException(status_code=400, detail="no fields to update")
    return {"record_id": create_job("record_update", body.model_dump()), "status": "pending"}


class DeleteRequest(BaseModel):
    doc_id: str


@router.post("/delete", status_code=202)
async def delete(body: DeleteRequest) -> dict:
    if not body.doc_id:
        raise HTTPException(status_code=400, detail="doc_id required")
    return {"record_id": create_job("record_delete", {"doc_id": body.doc_id}), "status": "pending"}


@router.get("/{record_id}")
async def record_status(record_id: str) -> dict:
    job = get_job(record_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown record job id")
    response = {"record_id": record_id, "status": job["status"]}
    if job["status"] == "done":
        response.update(job["result"] or {})
    elif job["status"] == "failed":
        response["error"] = job["error"]
    return response

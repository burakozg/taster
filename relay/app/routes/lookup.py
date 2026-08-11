"""POST /lookup, GET /lookup/{id} — PWA-facing, async job/poll like capture.

Was a single synchronous call in the old direct-to-backend design; now goes
through the same enqueue-and-poll shape as capture, since the actual work
(query_notes tool loop against Claude) happens on the QNAP worker, which
only ever polls outbound — it can't be called into directly anymore.
"""
from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.auth import require_client_key
from app.db import create_job, get_job
from app.rate_limit import rate_limit

router = APIRouter(
    prefix="/lookup",
    tags=["lookup"],
    dependencies=[Depends(require_client_key), Depends(rate_limit)],
)


@router.post("", status_code=202)
async def lookup(
    question: str = Form(...),
    image: UploadFile | None = File(default=None),
) -> dict:
    if not question.strip():
        raise HTTPException(status_code=400, detail="question must not be empty")

    payload: dict = {"question": question}
    if image is not None:
        raw = await image.read()
        payload["image_b64"] = base64.b64encode(raw).decode("ascii")
        payload["image_media_type"] = image.content_type or "image/jpeg"

    job_id = create_job("lookup", payload)
    return {"lookup_id": job_id, "status": "pending"}


@router.get("/{lookup_id}")
async def lookup_status(lookup_id: str) -> dict:
    job = get_job(lookup_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown lookup id")

    response = {"lookup_id": lookup_id, "status": job["status"]}
    if job["status"] == "done":
        response["answer"] = (job["result"] or {}).get("answer", "")
    elif job["status"] == "failed":
        response["error"] = job["error"]
    return response

"""POST /capture/photo, POST /capture/chat, GET /capture/{id} — PWA-facing.

Unlike the old direct-to-backend design, these just enqueue a job; the QNAP
worker polls for it, does the actual Claude/CouchDB work, and posts the
result back via /worker/jobs/{id}/result.
"""
from __future__ import annotations

import base64

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.auth import require_client_key
from app.db import create_job, get_job
from app.rate_limit import rate_limit

# Cap the image we'll pull into memory and base64 into the SQLite job payload.
# The relay VM is small (256MB) and the payload persists until the job finishes,
# so a runaway upload is both a memory spike and DB bloat. 10MB comfortably fits
# any phone photo; larger uploads are rejected before we read the whole body.
_MAX_IMAGE_BYTES = 10 * 1024 * 1024

router = APIRouter(
    prefix="/capture",
    tags=["capture"],
    dependencies=[Depends(require_client_key), Depends(rate_limit)],
)


@router.post("/photo", status_code=202)
async def capture_photo(
    image: UploadFile = File(...),
    stars: float | None = Form(default=None),
    note: str | None = Form(default=None),
) -> dict:
    if stars is not None:
        if not (1 <= stars <= 5):
            raise HTTPException(status_code=400, detail="stars must be between 1 and 5")
        # One decimal of precision — matches the PWA slider's step and the
        # schema's stored precision; also cleans up float artifacts.
        stars = round(stars, 1)

    # Read one byte past the cap so we can detect (and reject) an oversized
    # image without ever holding more than the limit in memory.
    raw = await image.read(_MAX_IMAGE_BYTES + 1)
    if len(raw) > _MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"image exceeds the {_MAX_IMAGE_BYTES // (1024 * 1024)}MB limit",
        )
    if not raw:
        raise HTTPException(status_code=400, detail="image must not be empty")
    job_id = create_job(
        "capture_photo",
        {
            "image_b64": base64.b64encode(raw).decode("ascii"),
            "image_media_type": image.content_type or "image/jpeg",
            "stars": stars,
            "note": note,
        },
    )
    return {"capture_id": job_id, "status": "pending"}


@router.post("/chat", status_code=202)
async def capture_chat(text: str = Form(...)) -> dict:
    if not text.strip():
        raise HTTPException(status_code=400, detail="text must not be empty")

    job_id = create_job("capture_chat", {"text": text})
    return {"capture_id": job_id, "status": "pending"}


@router.get("/{capture_id}")
async def capture_status(capture_id: str) -> dict:
    job = get_job(capture_id)
    if job is None:
        raise HTTPException(status_code=404, detail="unknown capture id")

    response = {"capture_id": capture_id, "status": job["status"]}
    if job["status"] == "done":
        response.update(job["result"] or {})
    elif job["status"] == "failed":
        response["error"] = job["error"]
    return response

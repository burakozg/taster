"""Data-maintenance web UI — runs on the NAS, next to CouchDB.

A second, tiny entry point into the same image as the worker
(`python -m app.admin_web`). It exists because maintaining the data (fixing
fields, regenerating or re-matching pairings for one / some / all items) does
not need the Fly relay's job queue at all: this process can reach CouchDB
directly, so edits are immediate, the item list is live rather than a
minute-old snapshot, and nothing about it is exposed to the internet.

  GET  /                   the page (static/admin.html; carries no data)
  GET  /api/items          every item note, live from CouchDB
  GET  /api/categories     category + edit-field registry (same as the PWA's)
  POST /api/record/update  {doc_id, fields}   edit one record (synchronous)
  POST /api/pairings       {doc_ids | all, mode}  start pairing jobs
  POST /api/details        {doc_ids, fields?, overwrite?}  look up missing details
                           (one job per item; PROPOSALS only, nothing is written)
  POST /api/details/apply  {doc_id, fields}   write the proposals a person accepted
  GET  /api/jobs?ids=      progress of those jobs
  GET  /api/history        recent pairing changes, each with what it replaced

There is NO authentication in this process, on purpose: login is the homelab's
central one (homelab-auth, enforced by Traefik's forwardAuth in front of this
app's hostname). That only holds because the container publishes no port and is
reachable solely through Traefik on the shared `homelab-internal` network — do
not add a `ports:` mapping to it, or the login gate can be walked around.

Pairing work is split into chunks of `repair_batch_size` items, run one chunk
at a time, and each chunk is WRITTEN as soon as it finishes — a failure costs
that chunk, not the run. Each applied item keeps the pairings it replaced
(history.jsonl under ADMIN_DATA_DIR) so a change can be undone from the page.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.categories import categories_metadata
from app.config import get_settings
from app.couchdb_client import CouchDBClient
from app.detail_fetch import FETCHABLE, fetch_details
from app.items_query import query_all_items
from app.logging_setup import setup_logging
from app.manage_service import run_repair_pairings_items
from app.record_service import update_record

logger = logging.getLogger("admin")

PAGE = Path(__file__).parent / "static" / "admin.html"
MAX_ITEMS = 500
HISTORY_KEEP = 500


class UpdateBody(BaseModel):
    doc_id: str
    fields: dict[str, Any]


class PairingsBody(BaseModel):
    doc_ids: list[str] | None = None
    all: bool = False
    mode: Literal["regenerate", "rematch"] = "regenerate"


class DetailsBody(BaseModel):
    doc_ids: list[str]
    fields: list[str] | None = None
    overwrite: bool = False


class ApplyBody(BaseModel):
    doc_id: str
    fields: dict[str, Any]


class Jobs:
    """In-memory progress for the pairing jobs of this process. Lost on a
    restart on purpose: what matters survives in CouchDB and history.jsonl."""

    def __init__(self) -> None:
        self.by_id: dict[str, dict[str, Any]] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()


def _history_path() -> Path:
    return Path(os.environ.get("ADMIN_DATA_DIR", "/data")) / "history.jsonl"


def _append_history(entries: list[dict[str, Any]]) -> None:
    if not entries:
        return
    path = _history_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text().splitlines() if path.exists() else []
        lines = (existing + [json.dumps(e, ensure_ascii=False) for e in entries])[-HISTORY_KEEP:]
        path.write_text("\n".join(lines) + "\n")
    except OSError as e:
        # Undo is a convenience; losing it must not fail a run that wrote fine.
        logger.warning("could not write %s: %s", path, e)


def _read_history() -> list[dict[str, Any]]:
    path = _history_path()
    try:
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        return []
    return list(reversed(rows))


async def _runner(app: FastAPI) -> None:
    """One worker task: pairing chunks run strictly one after another, so a
    big run can't hammer the model provider or fight itself over CouchDB."""
    jobs: Jobs = app.state.jobs
    settings, db = app.state.settings, app.state.db
    while True:
        job_id = await jobs.queue.get()
        job = jobs.by_id[job_id]
        job["status"] = "processing"
        try:
            if job["kind"] == "details":
                job.update(status="done", result=await fetch_details(
                    job_id, settings, db, doc_id=job["doc_ids"][0],
                    fields=job.get("fields"), overwrite=job.get("overwrite", False),
                ))
                continue
            result = await run_repair_pairings_items(
                job_id, settings, db, doc_ids=job["doc_ids"], mode=job["mode"],
            )
            job.update(status="done", result=result)
            _append_history([
                {
                    "kind": "pairings",
                    "job_id": job_id, "at": datetime.now(timezone.utc).isoformat(),
                    "doc_id": r["doc_id"], "name": r.get("name"), "mode": result["mode"],
                    "previous": r["previous"],
                }
                for r in result["results"] if r.get("status") == "applied" and r.get("previous") is not None
            ])
        except Exception as e:  # noqa: BLE001 — a failed chunk must not stop the queue
            logger.exception("pairing job %s failed", job_id)
            job.update(status="failed", error=str(e) or type(e).__name__)


def create_app() -> FastAPI:
    setup_logging()
    settings = get_settings()
    db = CouchDBClient(
        base_url=settings.couchdb_url, db=settings.couchdb_db,
        user=settings.couchdb_user, password=settings.couchdb_password,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runner = asyncio.create_task(_runner(app))
        try:
            yield
        finally:
            app.state.runner.cancel()

    app = FastAPI(title="Tasting Log data", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.db, app.state.jobs = settings, db, Jobs()

    @app.get("/", include_in_schema=False)
    async def page() -> FileResponse:
        return FileResponse(PAGE, media_type="text/html", headers={"Cache-Control": "no-store"})

    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/items")
    async def items() -> dict:
        rows = await query_all_items(db)
        return {"items": [r for r in rows if r.get("type") != "pairing"]}

    @app.get("/api/categories")
    async def categories() -> dict:
        # `fetchable`: which fields "Fetch details" can look up, per type.
        return {"categories": categories_metadata(), "fetchable": {t: list(f) for t, f in FETCHABLE.items()}}

    @app.post("/api/record/update")
    async def record_update(body: UpdateBody) -> dict:
        if not body.fields:
            raise HTTPException(status_code=400, detail="no fields to update")
        try:
            return await update_record(db, body.doc_id, body.fields)
        except ValueError as e:  # not found
            raise HTTPException(status_code=404, detail=str(e)) from e
        except Exception as e:  # noqa: BLE001 — schema rejection etc: show the user why
            raise HTTPException(status_code=422, detail=str(e)) from e

    @app.post("/api/pairings", status_code=202)
    async def pairings(body: PairingsBody) -> dict:
        if body.all:
            ids = [r["_id"] for r in await query_all_items(db) if r.get("type") != "pairing" and r.get("_id")]
        else:
            ids = list(dict.fromkeys(body.doc_ids or []))
        if not ids:
            raise HTTPException(status_code=400, detail="no items selected")
        if len(ids) > MAX_ITEMS:
            raise HTTPException(status_code=400, detail=f"at most {MAX_ITEMS} items per request")
        if body.mode == "rematch" and not settings.typesafe_api_key:
            raise HTTPException(status_code=400, detail="re-matching needs TYPESAFE_API_KEY")
        size = max(1, settings.models.claude.repair_batch_size)
        jobs: Jobs = app.state.jobs
        out = []
        for n in range(0, len(ids), size):
            chunk = ids[n:n + size]
            job_id = str(uuid.uuid4())
            jobs.by_id[job_id] = {"kind": "pairings", "status": "queued", "doc_ids": chunk, "mode": body.mode, "result": None, "error": None}
            jobs.queue.put_nowait(job_id)
            out.append({"job_id": job_id, "doc_ids": chunk})
        logger.info("pairings: queued %d item(s) in %d job(s) mode=%s", len(ids), len(out), body.mode)
        return {"jobs": out, "items": len(ids)}

    @app.post("/api/details", status_code=202)
    async def details(body: DetailsBody) -> dict:
        ids = list(dict.fromkeys(body.doc_ids))
        if not ids:
            raise HTTPException(status_code=400, detail="no items selected")
        if len(ids) > 100:
            raise HTTPException(status_code=400, detail="at most 100 items per request")
        jobs: Jobs = app.state.jobs
        out = []
        for doc_id in ids:
            job_id = str(uuid.uuid4())
            jobs.by_id[job_id] = {"kind": "details", "status": "queued", "doc_ids": [doc_id],
                                  "fields": body.fields, "overwrite": body.overwrite, "result": None, "error": None}
            jobs.queue.put_nowait(job_id)
            out.append({"job_id": job_id, "doc_ids": [doc_id]})
        logger.info("details: queued %d item(s) fields=%s overwrite=%s", len(ids), body.fields or "all-empty", body.overwrite)
        return {"jobs": out, "items": len(ids)}

    @app.post("/api/details/apply")
    async def details_apply(body: ApplyBody) -> dict:
        doc = await db.get_document(body.doc_id)
        if doc is None:
            raise HTTPException(status_code=404, detail="record not found")
        allowed = set(FETCHABLE.get(doc.get("type", ""), ()))
        bad = [k for k in body.fields if k not in allowed]
        if bad or not body.fields:
            raise HTTPException(status_code=400, detail=f"not a fetchable field: {', '.join(bad) or '(none given)'}")
        previous = {k: doc.get(k) for k in body.fields}
        try:
            await update_record(db, body.doc_id, body.fields)
        except Exception as e:  # noqa: BLE001 — the schema's reason is what the user needs
            raise HTTPException(status_code=422, detail=str(e)) from e
        name = " — ".join(x for x in (doc.get("producer"), doc.get("name")) if x) or body.doc_id
        _append_history([{
            "kind": "fields", "at": datetime.now(timezone.utc).isoformat(), "doc_id": body.doc_id,
            "name": name, "mode": "details", "previous": previous, "applied": body.fields,
        }])
        return {"ok": True, "applied": list(body.fields)}

    @app.get("/api/jobs")
    async def job_status(ids: str = Query(...)) -> dict:
        jobs: Jobs = app.state.jobs
        found = []
        for i in [x for x in ids.split(",") if x][:200]:
            j = jobs.by_id.get(i)
            # Unknown id = the container restarted since it was queued.
            found.append({"job_id": i, "status": j["status"] if j else "failed",
                          "error": j["error"] if j else "lost — the admin container restarted",
                          "result": j["result"] if j else None})
        return {"jobs": found}

    @app.get("/api/history")
    async def history() -> dict:
        return {"entries": _read_history()}

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("ADMIN_PORT", "8088")), log_level="warning")


if __name__ == "__main__":
    main()

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
  POST /api/chat           {messages}  one chat turn: a reply plus proposed actions
                           (answered concurrently, not queued behind long jobs)

The operations that used to live in the phone app's Admin tab live here too, so
the phone keeps only capture, search and per-item actions:

  GET  /api/status         relay queue + snapshot state (is the worker alive?)
  GET  /api/relay-jobs     the relay's recent jobs (captures, lookups …)
  GET  /api/usage          the token ledger, by day, model and task
  GET  /api/models         catalog + the three role choices + effective models
  PUT  /api/models         change the role choices (applies to the next call)
  POST /api/sync/{action}  status | rebuild-vault | rebuild-records | normalize
  POST /api/maintain/plan  {instruction}  free-form AI bulk-edit PLAN (writes nothing)
  POST /api/maintain/apply {changes}      apply the ticked part of a plan
  GET  /api/logs           the worker's and this portal's logs, filterable
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
(app/history.py, shared with the worker) so a change can be undone from the page.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.categories import categories_metadata
from app.config import get_settings
from app.chat_service import run_chat
from app.couchdb_client import CouchDBClient
from app import history
from app.detail_fetch import FETCHABLE, apply_details, fetch_details
from app.items_query import query_all_items
from app.logging_setup import setup_logging
from app.jev_models import list_jev_models
from app.logs import read_logs, sources as log_sources
from app.manage_service import run_manage_apply, run_manage_plan, run_repair_pairings_items
from app.model_roles import admin_overrides, matching_model, model_for_role
from app.record_service import update_record
from app.relay_client import RelayClient
from app import usage
from app import model_roles
from app.sync_service import normalize_records, rebuild_records, rebuild_vault, sync_status

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


class SettingsBody(BaseModel):
    vision_model: str | None = None
    research_model: str | None = None
    reasoning_model: str | None = None
    # Jev's model for pairing matches (not an LLM role — see jev_models.py).
    matching_model: str | None = None


class MaintainPlanBody(BaseModel):
    instruction: str


class MaintainApplyBody(BaseModel):
    changes: list[dict[str, Any]]


_SYNC_ACTIONS = {
    "status": sync_status,
    "rebuild-vault": rebuild_vault,
    "rebuild-records": rebuild_records,
    "normalize": normalize_records,
}


class ChatBody(BaseModel):
    messages: list[dict[str, Any]]


class ApplyBody(BaseModel):
    doc_id: str
    fields: dict[str, Any]


class Jobs:
    """In-memory progress for the pairing jobs of this process. Lost on a
    restart on purpose: what matters survives in CouchDB and history.jsonl."""

    def __init__(self) -> None:
        self.by_id: dict[str, dict[str, Any]] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()


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
            # The Admin tab's model choices, fetched fresh (briefly cached) — the
            # same ones the worker receives with a job from the phone app.
            ov = await admin_overrides(app.state.relay)
            if job["kind"] == "details":
                job.update(status="done", result=await fetch_details(
                    job_id, settings, db, doc_id=job["doc_ids"][0],
                    fields=job.get("fields"), overwrite=job.get("overwrite", False),
                    model_override=model_for_role(settings, "research", ov),
                    reviewer_model=model_for_role(settings, "reasoning", ov),
                ))
                continue
            if job["kind"] == "sync":
                job.update(status="done", result=await _SYNC_ACTIONS[job["action"]](db))
                continue
            if job["kind"] == "maintain_apply":
                job.update(status="done", result=await run_manage_apply(job_id, settings, db, changes=job["changes"]))
                continue
            result = await run_repair_pairings_items(
                job_id, settings, db, doc_ids=job["doc_ids"], mode=job["mode"],
                model_override=model_for_role(settings, "research", ov),
                matching_model=matching_model(settings, ov),
            )
            job.update(status="done", result=result)
        except Exception as e:  # noqa: BLE001 — a failed chunk must not stop the queue
            logger.exception("pairing job %s failed", job_id)
            job.update(status="failed", error=str(e) or type(e).__name__)


def create_app() -> FastAPI:
    setup_logging("admin")
    settings = get_settings()
    db = CouchDBClient(
        base_url=settings.couchdb_url, db=settings.couchdb_db,
        user=settings.couchdb_user, password=settings.couchdb_password,
    )

    relay = RelayClient(settings)

    async def usage_loop() -> None:
        # Everything this process spends goes to the same ledger as the worker's.
        # Without this its calls (chat, detail workers, pairings) were booked in
        # memory and never reported.
        while True:
            await asyncio.sleep(60)
            await usage.push(relay)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.runner = asyncio.create_task(_runner(app))
        app.state.usage_task = asyncio.create_task(usage_loop())
        try:
            yield
        finally:
            app.state.runner.cancel()
            app.state.usage_task.cancel()
            await usage.push(relay)   # whatever was booked since the last tick

    app = FastAPI(title="Tasting Log data", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings, app.state.db, app.state.jobs, app.state.relay = settings, db, Jobs(), relay

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
        return {
            "categories": categories_metadata(),
            "fetchable": {t: list(f) for t, f in FETCHABLE.items()},
            # Items per pairing call, so the page's "N model calls" estimate matches.
            "repair_batch_size": settings.models.claude.repair_batch_size,
        }

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
        try:
            return await apply_details(db, body.doc_id, body.fields)
        except ValueError as e:
            raise HTTPException(status_code=404 if "not found" in str(e) else 400, detail=str(e)) from e
        except Exception as e:  # noqa: BLE001 — the schema's reason is what the user needs
            raise HTTPException(status_code=422, detail=str(e)) from e

    def spawn(kind: str, work) -> str:
        """Run `work()` as its own task (not behind the serial queue) and track it
        like any job. For things that are quick or interactive: chat, a plan, a
        read-only sync check."""
        job_id = str(uuid.uuid4())
        jobs: Jobs = app.state.jobs
        jobs.by_id[job_id] = {"kind": kind, "status": "processing", "doc_ids": [], "result": None, "error": None}

        async def go() -> None:
            job = jobs.by_id[job_id]
            try:
                job.update(status="done", result=await work(job_id))
            except Exception as e:  # noqa: BLE001 — shown to the person, not swallowed
                logger.exception("%s %s failed", kind, job_id)
                job.update(status="failed", error=str(e) or type(e).__name__)

        app.state.tasks = getattr(app.state, "tasks", set())
        task = asyncio.create_task(go())
        app.state.tasks.add(task)
        task.add_done_callback(app.state.tasks.discard)
        return job_id

    async def relay_call(coro):
        try:
            return await coro
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"the relay did not answer: {str(e) or type(e).__name__}") from e

    @app.get("/api/status")
    async def status() -> dict:
        return await relay_call(relay.admin_status())

    @app.get("/api/relay-jobs")
    async def relay_jobs(limit: int = Query(default=10, ge=1, le=100)) -> dict:
        return {"jobs": await relay_call(relay.admin_jobs(limit))}

    @app.get("/api/usage")
    async def usage_ledger(days: int = Query(default=14, ge=1, le=365)) -> dict:
        return await relay_call(relay.admin_usage(days))

    @app.get("/api/models")
    async def models() -> dict:
        catalog = await relay_call(relay.admin_models())
        all_chosen = await relay_call(relay.get_model_settings())
        chosen = {k: v for k, v in all_chosen.items() if k in model_roles.ROLES_KEYS}
        defaults = {r: model_for_role(settings, r) for r in model_roles.ROLES}
        effective = {r: model_for_role(settings, r, chosen) for r in model_roles.ROLES}
        jev = await list_jev_models(settings)
        return {
            "models": catalog, "chosen": chosen, "defaults": defaults, "effective": effective,
            # Jev: its own list, fetched from Jev. `configured` False = no TYPESAFE_API_KEY,
            # so pairing matches stay the research model's own guess and there is
            # nothing to choose.
            "matching": {
                "configured": bool(settings.typesafe_api_key),
                "options": jev,
                "chosen": all_chosen.get("matching_model"),
                "config_default": settings.models.claude.matching_model,
            },
        }

    @app.put("/api/models")
    async def set_models(body: SettingsBody) -> dict:
        # Validate a CHANGED Jev model against Jev's own list. An unchanged value is
        # carried through untouched, so saving the LLM roles never fails because Jev
        # is unreachable or has retired the name (the Models tab flags that case).
        current = (await relay_call(relay.get_model_settings())).get("matching_model")
        if body.matching_model and body.matching_model != current:
            known = {m["name"] for m in await list_jev_models(settings)}
            if body.matching_model not in known:
                raise HTTPException(status_code=400, detail=(
                    f"Jev does not offer {body.matching_model!r}"
                    + (f" (it offers: {', '.join(sorted(known))})" if known else " — or its model list could not be read")))
        await relay_call(relay.admin_put_settings(body.model_dump()))
        model_roles.forget_cache()   # the next operation picks the new choice up at once
        return {"ok": True}

    @app.post("/api/sync/{action}", status_code=202)
    async def sync(action: str) -> dict:
        if action not in _SYNC_ACTIONS:
            raise HTTPException(status_code=404, detail="unknown sync action")
        if action == "status":
            return {"job_id": spawn("sync", lambda _id: _SYNC_ACTIONS["status"](db))}
        job_id = str(uuid.uuid4())
        jobs: Jobs = app.state.jobs
        jobs.by_id[job_id] = {"kind": "sync", "action": action, "status": "queued", "doc_ids": [], "result": None, "error": None}
        jobs.queue.put_nowait(job_id)
        return {"job_id": job_id}

    @app.post("/api/maintain/plan", status_code=202)
    async def maintain_plan(body: MaintainPlanBody) -> dict:
        if not body.instruction.strip():
            raise HTTPException(status_code=400, detail="describe a change first")

        async def work(job_id: str) -> dict:
            ov = await admin_overrides(relay)
            return await run_manage_plan(
                job_id, settings, db, instruction=body.instruction,
                model_override=model_for_role(settings, "reasoning", ov),
            )
        return {"job_id": spawn("maintain", work)}

    @app.post("/api/maintain/apply", status_code=202)
    async def maintain_apply(body: MaintainApplyBody) -> dict:
        if not body.changes:
            raise HTTPException(status_code=400, detail="no changes to apply")
        job_id = str(uuid.uuid4())
        jobs: Jobs = app.state.jobs
        jobs.by_id[job_id] = {"kind": "maintain_apply", "status": "queued", "doc_ids": [], "changes": body.changes, "result": None, "error": None}
        jobs.queue.put_nowait(job_id)
        return {"job_id": job_id}

    @app.get("/api/logs")
    async def logs(source: str | None = None, level: str = "INFO", q: str = "", limit: int = Query(default=300, ge=1, le=1000)) -> dict:
        return {"sources": log_sources(), "entries": read_logs(source=source or None, level=level, q=q, limit=limit)}

    @app.post("/api/chat", status_code=202)
    async def chat(body: ChatBody) -> dict:
        job_id = str(uuid.uuid4())
        jobs: Jobs = app.state.jobs
        jobs.by_id[job_id] = {"kind": "chat", "status": "processing", "doc_ids": [], "result": None, "error": None}

        async def go() -> None:
            job = jobs.by_id[job_id]
            try:
                ov = await admin_overrides(relay)
                job.update(status="done", result=await run_chat(
                    job_id, settings, db, messages=body.messages,
                    model_override=model_for_role(settings, "reasoning", ov),
                ))
            except Exception as e:  # noqa: BLE001 — shown in the chat, not swallowed
                logger.exception("chat %s failed", job_id)
                job.update(status="failed", error=str(e) or type(e).__name__)

        # Held on app.state so the task isn't garbage-collected mid-flight.
        app.state.chats = getattr(app.state, "chats", set())
        task = asyncio.create_task(go())
        app.state.chats.add(task)
        task.add_done_callback(app.state.chats.discard)
        return {"job_id": job_id}

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
    async def history_entries() -> dict:
        return {"entries": history.read()}

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("ADMIN_PORT", "8088")), log_level="warning")


if __name__ == "__main__":
    main()

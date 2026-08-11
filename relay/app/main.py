from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings
from app.db import init_db, job_counts, prune_old_jobs
from app.logging_setup import secret_state, setup_logging
from app.rate_limit import _LIMIT_PER_MINUTE
from app.routes import admin, capture, categories, items, lookup, manage, record, sync, worker

setup_logging()
logger = logging.getLogger("relay")

STATIC_DIR = Path(__file__).parent.parent / "static"


def _log_startup(settings: Settings) -> None:
    """REL-6: GEN-8 config echo (secrets present/absent), whether the SQLite
    file already existed, and how many jobs sit in each status — a restart
    with hundreds of `pending` jobs is a stuck-worker signal."""
    logger.info(
        "relay config: db_path=%s job_claim_timeout_s=%d job_retention_days=%d "
        "cors_allow_origin=%s rate_limit_per_min=%d client_api_key=%s worker_api_key=%s",
        settings.db_path, settings.job_claim_timeout_s, settings.job_retention_days,
        settings.cors_allow_origin, _LIMIT_PER_MINUTE,
        secret_state(settings.client_api_key), secret_state(settings.worker_api_key),
    )


_PRUNE_INTERVAL_S = 3600  # hourly — the table is tiny, this is just housekeeping


async def _prune_loop(retention_days: int) -> None:
    """Background housekeeping: prune terminal jobs past the retention window so
    the SQLite file (base64 capture payloads land here) can't grow without
    bound. Runs once at startup, then hourly; a failure is logged, never fatal."""
    while True:
        try:
            prune_old_jobs(retention_days)
        except Exception:  # noqa: BLE001 — housekeeping must never crash the app
            logger.exception("job prune pass failed")
        await asyncio.sleep(_PRUNE_INTERVAL_S)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db_existed = Path(settings.db_path).exists()
    init_db(settings.db_path)
    _log_startup(settings)
    logger.info("relay startup: sqlite_existed=%s job_counts=%s", db_existed, job_counts())
    pruner = asyncio.create_task(_prune_loop(settings.job_retention_days))
    try:
        yield
    finally:
        pruner.cancel()


app = FastAPI(title="Tasting Log Relay", lifespan=lifespan)

settings = get_settings()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.cors_allow_origin],
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(capture.router)
app.include_router(lookup.router)
app.include_router(items.router)
app.include_router(categories.router)
app.include_router(worker.router)
app.include_router(admin.router)
app.include_router(manage.router)
app.include_router(sync.router)
app.include_router(record.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


# PWA static files. Mounted last, at "/", so the API routes above take
# precedence over any same-named path (none currently collide, but order
# matters if that ever changes).
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

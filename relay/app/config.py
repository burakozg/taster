"""Relay settings — all secrets, no model config here (that lives in
backend/config.yaml since the relay never talks to Claude directly)."""
from __future__ import annotations

import os
from functools import lru_cache

from pydantic import BaseModel


class Settings(BaseModel):
    # Bearer token the PWA sends — same value the PWA's setup screen stores.
    client_api_key: str
    # A SEPARATE bearer token, known only to the QNAP worker, for the
    # /worker/* endpoints. Deliberately distinct from client_api_key so a
    # leaked PWA key (client-side, in localStorage) can't be used to claim
    # or complete jobs — only to enqueue and poll them.
    worker_api_key: str
    db_path: str = "/data/relay.db"
    # The PWA is served SAME-ORIGIN with this API, so it needs no CORS grant at
    # all; this only governs *other* origins' browser access. Default to the
    # deployed origin (not "*") so a leaked key can't also be driven from an
    # arbitrary website — override via CORS_ALLOW_ORIGIN for a different deploy.
    cors_allow_origin: str = "https://your-taster-relay.fly.dev"
    # How long a job can sit "processing" before it's treated as abandoned
    # and made claimable again — covers a worker crash mid-job.
    job_claim_timeout_s: int = 300
    # Terminal (done/failed) jobs older than this are pruned so the SQLite file
    # on the Fly volume doesn't grow without bound (capture payloads carry
    # base64 image data). The image payload itself is cleared as soon as a job
    # finishes; this prune reclaims the rows (and any result) after the window.
    job_retention_days: int = 7


@lru_cache
def get_settings() -> Settings:
    return Settings(
        client_api_key=os.environ["TASTER_API_KEY"],
        worker_api_key=os.environ["WORKER_API_KEY"],
        db_path=os.environ.get("RELAY_DB_PATH", "/data/relay.db"),
        cors_allow_origin=os.environ.get("CORS_ALLOW_ORIGIN", "https://your-taster-relay.fly.dev"),
        job_claim_timeout_s=int(os.environ.get("JOB_CLAIM_TIMEOUT_S", "300")),
        job_retention_days=int(os.environ.get("JOB_RETENTION_DAYS", "7")),
    )

"""Worker configuration: secrets from environment, model choice from
config.yaml.

Kept deliberately split: config.yaml is meant to be hand-edited to swap
models without touching secrets or code (see design doc's "which model"
discussion). Everything security-sensitive (API keys, CouchDB credentials,
the worker's relay auth) stays in the environment / .env, never in YAML.

No PWA-facing bearer token or CORS/rate-limit settings here anymore — the
worker never accepts inbound connections at all; that surface moved to
relay/app/config.py.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

CONFIG_YAML_PATH = Path(os.environ.get("TASTER_CONFIG_PATH", Path(__file__).parent.parent / "config.yaml"))


class ClaudeConfig(BaseModel):
    capture_model: str = "claude-opus-4-8"
    lookup_model: str = "claude-opus-4-8"
    max_tokens_capture: int = 4096
    max_tokens_lookup: int = 2048
    # Bulk-maintenance plans emit one JSON object covering many records, and
    # reasoning/thinking tokens are spent from this same budget first — so it
    # needs far more headroom than a single capture or the plan truncates
    # mid-string (an "Unterminated string" JSON error).
    max_tokens_manage: int = 16384
    # Regenerate-pairings produces a change for EVERY item, which in one response
    # eventually overflows max_tokens_manage as the vault grows. So it's chunked:
    # this many target items per model call, results merged. Keeps each call's
    # output bounded no matter how large the vault gets.
    repair_batch_size: int = 10
    effort: str = "medium"
    web_search_max_uses: int = 3
    # Server-side web search on /lookup. Off historically — but only for Claude
    # and OpenAI, because Mistral's Conversations surface bundles the connector
    # and nobody parameterised it, so the same question searched on one provider
    # and not the others. All three execute search server-side and all three
    # require the tool to be declared per request; there is no implicit search.
    # On by default: shop mode ("standing in front of a bottle") is exactly the
    # case that needs facts the vault doesn't have. Set false to trade that for
    # latency — lookup is the interactive path.
    web_search_lookup: bool = True
    max_tool_iterations: int = 6


class ModelConfig(BaseModel):
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)


class Settings(BaseModel):
    # --- secrets / environment-only ---
    anthropic_api_key: str | None = None
    # Optional — only needed when the admin panel selects a gpt-* model.
    openai_api_key: str | None = None
    # Optional — only needed when the admin panel selects a mistral-*/pixtral-* model.
    mistral_api_key: str | None = None
    # Optional — only needed when the admin panel selects a namespaced
    # `vendor/model` id, which routes through OpenRouter.
    openrouter_api_key: str | None = None
    couchdb_url: str = "http://taster-couchdb:5984"
    couchdb_db: str = "tastings"
    couchdb_user: str
    couchdb_password: str

    # --- relay (Fly.io) — the worker polls this outbound, never the other
    # way around ---
    relay_url: str
    worker_api_key: str
    poll_interval_s: float = 3.0
    items_snapshot_interval_s: float = 60.0
    # How often reverse-sync scans CouchDB's _changes for Obsidian edits to
    # fold back into the queryable JSON docs (reconcile.py). Cheap (one
    # incremental _changes page), so a tighter interval than the snapshot is
    # fine; 30s keeps edits visible in the PWA quickly without busy-looping.
    reconcile_interval_s: float = 30.0
    # Liveness heartbeat cadence (WRK-8). Slow on purpose — GEN-6 silences
    # per-poll logging, so this line is how "is the worker alive" is answered
    # from `docker logs` without grepping absence. Default 30 min.
    heartbeat_interval_s: float = 1800.0

    # --- model config, loaded from config.yaml ---
    models: ModelConfig = Field(default_factory=ModelConfig)


def _load_model_config() -> ModelConfig:
    if not CONFIG_YAML_PATH.exists():
        return ModelConfig()
    with open(CONFIG_YAML_PATH) as f:
        raw = yaml.safe_load(f) or {}
    return ModelConfig.model_validate(raw)


@lru_cache
def get_settings() -> Settings:
    return Settings(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        openai_api_key=os.environ.get("OPENAI_API_KEY"),
        mistral_api_key=os.environ.get("MISTRAL_API_KEY"),
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY"),
        couchdb_url=os.environ.get("COUCHDB_URL", "http://taster-couchdb:5984"),
        couchdb_db=os.environ.get("COUCHDB_DB", "tastings"),
        couchdb_user=os.environ["COUCHDB_USER"],
        couchdb_password=os.environ["COUCHDB_PASSWORD"],
        relay_url=os.environ["RELAY_URL"],
        worker_api_key=os.environ["WORKER_API_KEY"],
        poll_interval_s=float(os.environ.get("POLL_INTERVAL_S", "3.0")),
        items_snapshot_interval_s=float(os.environ.get("ITEMS_SNAPSHOT_INTERVAL_S", "60.0")),
        reconcile_interval_s=float(os.environ.get("RECONCILE_INTERVAL_S", "30.0")),
        heartbeat_interval_s=float(os.environ.get("HEARTBEAT_INTERVAL_S", "1800.0")),
        models=_load_model_config(),
    )

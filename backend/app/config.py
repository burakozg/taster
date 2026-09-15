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
    """Despite the name, every provider module reads its budgets/effort/etc.
    from this one block (`settings.models.claude`) — Mistral and OpenRouter
    included. It predates both (and predates dropping the Anthropic/OpenAI
    direct-API paths entirely); renaming the YAML key and every
    `settings.models.claude` call site is a bigger refactor than the naming
    confusion currently costs, but see model_output.truncated() for a
    user-facing message that used to read as Claude-specific when it wasn't."""

    # Two models, split on the one axis that matters: does the job carry an
    # image (see worker.process_job's model_for). This replaced a per-surface
    # capture_model/lookup_model pair — both of those meant "the model for a
    # photo job", so they were one setting wearing two labels.
    #
    # Both defaults are OpenRouter ids (the `vendor/model` slash is what
    # routes them there — see providers.py) since this app runs entirely on
    # open-weight models now. Qwen3 VL for image_model because it's the
    # vision-capable entry in the catalog; swap to a different `vendor/model`
    # id, or a mistral-*/ministral-* one, in config.yaml if you prefer.
    image_model: str = "qwen/qwen3-vl-235b-a22b-instruct"
    # Everything without an image — chat captures, plain lookups, maintenance
    # plans, regenerate-pairings. These are reasoning/tool/JSON jobs, and a
    # vision-tuned model is the wrong instrument for all of them.
    text_model: str = "deepseek/deepseek-v4-pro"
    max_tokens_capture: int = 16384
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
    # Separate, larger budget for the AI-maintenance PLAN, which researches one
    # fact per record rather than one product per call.
    web_search_max_uses_manage: int = 25
    # Server-side web search on /lookup. Both remaining providers (OpenRouter's
    # `web` plugin, Mistral's Conversations connector) execute search
    # server-side and require the tool declared per request — there is no
    # implicit search on either. On by default: shop mode ("standing in front
    # of a bottle") is exactly the case that needs facts the vault doesn't
    # have. Set false to trade that for latency — lookup is the interactive path.
    web_search_lookup: bool = True
    max_tool_iterations: int = 8


class ModelConfig(BaseModel):
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)


class Settings(BaseModel):
    # --- secrets / environment-only ---
    # Optional — only needed when the admin panel selects a mistral-*/ministral-*/
    # pixtral-* model.
    mistral_api_key: str | None = None
    # Required in practice — every default model id is a namespaced
    # `vendor/model` OpenRouter id (see config.ClaudeConfig), so this is the
    # key the app actually calls out with day to day. Left optional here
    # rather than enforced at startup so an admin override to a Mistral model
    # still works with only MISTRAL_API_KEY set.
    openrouter_api_key: str | None = None
    couchdb_url: str = "http://taster-couchdb:5984"
    couchdb_db: str = "hobby"
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
        mistral_api_key=os.environ.get("MISTRAL_API_KEY"),
        openrouter_api_key=os.environ.get("OPENROUTER_API_KEY"),
        couchdb_url=os.environ.get("COUCHDB_URL", "http://taster-couchdb:5984"),
        couchdb_db=os.environ.get("COUCHDB_DB", "hobby"),
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

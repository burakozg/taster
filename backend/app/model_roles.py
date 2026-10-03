"""Which model runs which kind of work.

Two knobs (`image_model`, `text_model`) were too coarse: everything without a
photo shared one model, whether it was a cheap web-search lookup repeated
hundreds of times or a judgement over the user's whole vault. Work now falls
into three ROLES:

  vision     reading a label or photo — and nothing else. The one job that needs
             images; a vision model is the wrong instrument for the rest.
  research   web search + strict JSON, many small calls: capture extraction and
             enrichment, the per-field detail workers, pairing profiles.
  reasoning  judgement and long context, few calls: chat over the vault, the
             detail reviewer, free-form maintenance plans, lookup answers.

Resolution, most specific first:
  1. the Admin tab's choice for the role            (relay -> `{role}_model`)
  2. the Admin tab's legacy choice                  (`image_model` / `text_model`)
  3. config.yaml's `{role}_model`
  4. config.yaml's legacy `image_model` / `text_model`

One function, used by the worker (jobs from the phone app) AND by taster-admin
(the web page), so the same operation runs on the same model whichever front end
started it.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.config import Settings

logger = logging.getLogger("worker.models")

ROLES = ("vision", "research", "reasoning")
ROLES_KEYS = tuple(f"{r}_model" for r in ROLES)
_LEGACY = {"vision": "image_model", "research": "text_model", "reasoning": "text_model"}


def model_for_role(settings: Settings, role: str, overrides: dict[str, Any] | None = None) -> str:
    if role not in ROLES:
        raise ValueError(f"unknown model role {role!r}")
    o = overrides or {}
    cfg = settings.models.claude
    return (
        o.get(f"{role}_model") or o.get(_LEGACY[role])
        or getattr(cfg, f"{role}_model") or getattr(cfg, _LEGACY[role])
    )


def effort_for_role(settings: Settings, role: str) -> str | None:
    """The role's reasoning effort, or None for the provider's global default."""
    return getattr(settings.models.claude, f"effort_{role}", None)


# ---- the Admin tab's choices, for processes that don't receive them per job ----
# The worker is handed them with every job. taster-admin has no job to carry them,
# so it asks the relay (the one place they are stored), briefly cached.

_CACHE_TTL_S = 60.0
_cache: tuple[float, dict[str, Any]] | None = None


async def admin_overrides(relay: Any) -> dict[str, Any]:
    """The relay's current model choices; {} (config.yaml defaults) if it can't
    be reached — a model-choice lookup must not take an operation down."""
    global _cache
    now = time.monotonic()
    if _cache and now - _cache[0] < _CACHE_TTL_S:
        return _cache[1]
    try:
        data = await relay.get_model_settings()
    except Exception as e:  # noqa: BLE001
        logger.warning("could not read model choices from the relay (%s); using config.yaml", e)
        data = _cache[1] if _cache else {}
    _cache = (now, data)
    return data


def forget_cache() -> None:
    """Drop the cached Admin-tab choices — called after they are changed here, so
    the very next operation sees the new model rather than the one from a minute ago."""
    global _cache
    _cache = None

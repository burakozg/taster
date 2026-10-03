"""The models Jev (TypeSafe) currently offers, fetched live.

Jev is not an LLM role: it has its own model list and its own key, and the relay
(which never holds that key) cannot know the list. So the admin portal asks Jev
itself — a new model Jev ships appears in the Models tab with no code change, and
a name saved long ago that Jev has since retired is caught here rather than
silently failing every match.

Cached briefly: the list changes on the order of weeks.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.config import Settings

logger = logging.getLogger("worker.jev")

_TTL_S = 600.0
_cache: tuple[float, list[dict[str, Any]]] | None = None


async def list_jev_models(settings: Settings) -> list[dict[str, Any]]:
    """[{name, description}] — [] when Jev isn't configured or can't be reached."""
    global _cache
    if not settings.typesafe_api_key:
        return []
    now = time.monotonic()
    if _cache and now - _cache[0] < _TTL_S:
        return _cache[1]
    try:
        from typesafe_sdk import AsyncTypeSafeClient
        client = AsyncTypeSafeClient(api_key=settings.typesafe_api_key)
        resp = await client.models.list()
        items = getattr(resp, "data", None) or getattr(resp, "models", None) or resp
        models = [{"name": m.name, "description": getattr(m, "description", "") or ""} for m in items]
    except Exception as e:  # noqa: BLE001 — a dropdown must not take the page down
        logger.warning("could not list Jev models: %s", str(e) or type(e).__name__)
        return _cache[1] if _cache else []
    _cache = (now, models)
    return models

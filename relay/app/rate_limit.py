"""Simple in-memory sliding-window rate limiter. The relay is now the one
piece actually facing the public internet, so this is the load-bearing
rate limit in the system (the QNAP worker no longer accepts inbound
requests at all)."""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, status

logger = logging.getLogger("relay.ratelimit")

_hits: dict[str, deque[float]] = defaultdict(deque)
# Sized so one legitimate client can't trip it: the PWA polls a job every
# 2s (up to ~30 GETs/min) plus the enqueue itself and any items refreshes.
# Claude spend is bounded separately by max_uses/max_tokens per call, so
# this only needs to stop hammering, not meter usage tightly.
_LIMIT_PER_MINUTE = 60

# The per-IP map would otherwise keep one (eventually empty) deque per address
# ever seen. Sweep entries whose window has fully aged out, no more than once a
# minute so the sweep itself is cheap.
_SWEEP_INTERVAL_S = 60.0
_last_sweep = 0.0


def _sweep(now: float) -> None:
    global _last_sweep
    if now - _last_sweep < _SWEEP_INTERVAL_S:
        return
    _last_sweep = now
    stale = [k for k, w in _hits.items() if not w or now - w[-1] > 60]
    for k in stale:
        del _hits[k]


def rate_limit(request: Request) -> None:
    # Behind Fly's proxy, request.client.host is the proxy's address (uvicorn
    # only trusts X-Forwarded-For from localhost by default), which would
    # collapse "per-IP" into one shared bucket for every client. Fly always
    # sets Fly-Client-IP with the real client address, and on Fly the app is
    # only reachable through that proxy, so the header can't be spoofed.
    # Locally (docker-compose.dev.yml) the header is absent and the direct
    # peer address is correct.
    key = request.headers.get("fly-client-ip") or (request.client.host if request.client else "unknown")
    now = time.monotonic()
    _sweep(now)
    window = _hits[key]
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= _LIMIT_PER_MINUTE:
        # REL-3: sized above the PWA's polling rate, so a 429 should essentially
        # never happen for a legitimate single user — any occurrence is signal.
        logger.warning("rate limit exceeded client_ip=%s path=%s", key, request.url.path)
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="rate limit exceeded")
    window.append(now)

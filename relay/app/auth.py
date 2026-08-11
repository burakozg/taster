"""Two separate bearer-token checks — see config.py's comment on why the
PWA-facing and worker-facing secrets are deliberately different."""
from __future__ import annotations

import hmac
import logging

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings

logger = logging.getLogger("relay.auth")

_bearer = HTTPBearer(auto_error=False)


def _client_ip(request: Request) -> str:
    # Same resolution as rate_limit.py: Fly-Client-IP is the real client
    # behind Fly's proxy and can't be spoofed there; direct peer locally.
    return request.headers.get("fly-client-ip") or (request.client.host if request.client else "unknown")


def _log_401(request: Request, surface: str) -> None:
    # REL-2: which key surface was hit, the path, and the client IP — the
    # leaked-key / probe signal (goal 6). Never the presented credential
    # itself (GEN-3), not even truncated.
    logger.warning(
        "auth failure surface=%s path=%s client_ip=%s",
        surface, request.url.path, _client_ip(request),
    )


def require_client_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    if credentials is None or not hmac.compare_digest(credentials.credentials, settings.client_api_key):
        _log_401(request, surface="client")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing API key")


def require_worker_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    if credentials is None or not hmac.compare_digest(credentials.credentials, settings.worker_api_key):
        _log_401(request, surface="worker")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing worker key")

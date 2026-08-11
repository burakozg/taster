"""Thin client for the worker's outbound-only calls to the Fly relay —
poll for work, post results, push the items snapshot. Never anything
inbound; this is the entire network surface the worker exposes to the
internet (none — it only ever initiates)."""
from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings


class RelayClient:
    def __init__(self, settings: Settings) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.relay_url.rstrip("/"),
            headers={"Authorization": f"Bearer {settings.worker_api_key}"},
            timeout=30.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_next_job(self) -> dict[str, Any] | None:
        resp = await self._client.get("/worker/jobs/next")
        resp.raise_for_status()
        return resp.json()

    async def post_job_done(self, job_id: str, result: dict[str, Any]) -> None:
        resp = await self._client.post(f"/worker/jobs/{job_id}/result", json={"status": "done", "result": result})
        resp.raise_for_status()

    async def post_job_failed(self, job_id: str, error: str) -> None:
        resp = await self._client.post(f"/worker/jobs/{job_id}/result", json={"status": "failed", "error": error})
        resp.raise_for_status()

    async def push_items_snapshot(
        self, items: list[dict[str, Any]], categories: list[dict[str, Any]] | None = None
    ) -> None:
        # Category metadata rides along so the relay can serve /categories to the
        # PWA (group headings + edit forms) from the worker's single registry —
        # sent with every snapshot so a relay restart self-heals like the items
        # cache does. Small and static, so the repeat cost is negligible.
        body: dict[str, Any] = {"items": items}
        if categories is not None:
            body["categories"] = categories
        resp = await self._client.post("/worker/items/snapshot", json=body)
        resp.raise_for_status()

    async def push_usage(self, report_id: str, rows: list[dict[str, Any]]) -> None:
        """Report a batch of token-usage deltas (WRK-10). `report_id` is stable
        across retries of the same batch so the relay can ignore one it has
        already added — see usage.take_report()."""
        resp = await self._client.post(
            "/worker/usage", json={"report_id": report_id, "rows": rows},
        )
        resp.raise_for_status()

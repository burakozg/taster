"""The QNAP worker — polls the Fly relay for jobs, does the actual Claude +
CouchDB work, posts results back. Never accepts an inbound connection;
this replaces the old FastAPI server entirely (see main.py's removal).

Run with: python -m app.worker

Logging follows LOGGING.md (the WRK-* requirements): job lifecycle with
durations and failure phase (WRK-1), a slow liveness heartbeat that replaces
per-poll noise (WRK-8), and dampened relay-unreachable reporting (WRK-9).
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from app import usage
from app.capture_service import run_capture
from app.categories import categories_metadata
from app.config import Settings, get_settings
from app.couchdb_client import CouchDBClient
from app.errors import PhaseError
from app.items_query import query_all_items
from app.logging_setup import secret_state, setup_logging
from app.lookup_service import run_lookup
from app.manage_service import run_manage_apply, run_manage_plan, run_repair_pairings_plan
from app.reconcile import reconcile_vault_edits
from app.record_service import delete_record, update_record
from app.relay_client import RelayClient
from app.sync_service import normalize_records, rebuild_records, rebuild_vault, sync_status

logger = logging.getLogger("worker")

# Job types that change vault records — after one succeeds we push a fresh
# items snapshot so the PWA's /items reflects the change immediately instead of
# waiting for the periodic timer (that lag is why a normalize/edit/delete can
# look like it "didn't work" until the next push).
_MUTATING_JOB_TYPES = {
    "capture_photo", "capture_chat", "manage_apply",
    "sync_rebuild_vault", "sync_rebuild_records", "sync_normalize",
    "record_update", "record_delete",
}


async def process_job(job: dict, settings: Settings, db: CouchDBClient) -> dict:
    """Dispatch one job to the right service and return a JSON-safe result
    dict for posting back to the relay. Raises on failure — the caller
    reports that as a failed job rather than swallowing it here."""
    payload = job["payload"]
    # Admin-panel model choices, attached by the relay to each claimed job
    # (empty when nothing is set — the services fall back to config.yaml).
    overrides = job.get("model_overrides") or {}

    if job["type"] == "capture_photo":
        result = await run_capture(
            job["id"], settings, db,
            source="photo",
            text=payload.get("note"),
            stars=payload.get("stars"),
            image_b64=payload["image_b64"],
            image_media_type=payload.get("image_media_type"),
            model_override=overrides.get("capture_model"),
        )
        return result.model_dump(mode="json")

    if job["type"] == "capture_chat":
        result = await run_capture(
            job["id"], settings, db, source="chat", text=payload["text"],
            model_override=overrides.get("capture_model"),
        )
        return result.model_dump(mode="json")

    if job["type"] == "lookup":
        result = await run_lookup(
            job["id"], settings, db,
            question=payload["question"],
            image_b64=payload.get("image_b64"),
            image_media_type=payload.get("image_media_type"),
            model_override=overrides.get("lookup_model"),
        )
        return {"answer": result.answer}

    if job["type"] == "manage_plan":
        # AI maintenance, phase 1: propose changes, write nothing.
        return await run_manage_plan(
            job["id"], settings, db,
            instruction=payload["instruction"],
            model_override=overrides.get("capture_model"),
        )

    if job["type"] == "manage_apply":
        # Phase 2: apply the human-approved subset of a plan (also used for the
        # approved subset of a regenerate-pairings plan — the changes carry a
        # structured `pairings` field instead of `edits`).
        return await run_manage_apply(
            job["id"], settings, db, changes=payload["changes"],
        )

    if job["type"] == "repair_pairings_plan":
        # Dedicated maintenance: propose fresh cross-category pairings for every
        # item (no re-capture). Applied via manage_apply, like any plan.
        return await run_repair_pairings_plan(
            job["id"], settings, db,
            model_override=overrides.get("capture_model"),
        )

    if job["type"] == "sync_status":
        return await sync_status(db)

    if job["type"] == "sync_rebuild_vault":
        return await rebuild_vault(db)

    if job["type"] == "sync_rebuild_records":
        return await rebuild_records(db)

    if job["type"] == "sync_normalize":
        return await normalize_records(db)

    if job["type"] == "record_update":
        return await update_record(db, payload["doc_id"], payload.get("fields") or {})

    if job["type"] == "record_delete":
        return await delete_record(db, payload["doc_id"])

    raise ValueError(f"unknown job type: {job['type']!r}")


def _exc_summary(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _log_startup_config(settings: Settings) -> None:
    """GEN-8: echo the effective non-secret config once at startup; secrets
    are reported present/absent only (GEN-3)."""
    c = settings.models.claude
    # usage_day echoes today's WRK-10 day key: the ledger's days are the
    # worker's LOCAL days, so this line is where "which timezone are those
    # totals in?" is answered without reading usage.py.
    logger.info(
        "worker config: relay_url=%s poll_interval_s=%.1f snapshot_interval_s=%.1f "
        "heartbeat_interval_s=%.0f couchdb_url=%s couchdb_db=%s couchdb_user=%s "
        "capture_model=%s lookup_model=%s effort=%s max_tokens_capture=%d "
        "max_tokens_lookup=%d web_search_max_uses=%d max_tool_iterations=%d usage_day=%s "
        "anthropic_api_key=%s openai_api_key=%s mistral_api_key=%s openrouter_api_key=%s "
        "worker_api_key=%s couchdb_password=%s",
        settings.relay_url, settings.poll_interval_s, settings.items_snapshot_interval_s,
        settings.heartbeat_interval_s, settings.couchdb_url, settings.couchdb_db,
        settings.couchdb_user, c.capture_model, c.lookup_model, c.effort,
        c.max_tokens_capture, c.max_tokens_lookup, c.web_search_max_uses,
        c.max_tool_iterations, datetime.now().astimezone().strftime("%Y-%m-%d %Z%z"),
        secret_state(settings.anthropic_api_key),
        secret_state(settings.openai_api_key), secret_state(settings.mistral_api_key),
        secret_state(settings.openrouter_api_key),
        secret_state(settings.worker_api_key), secret_state(settings.couchdb_password),
    )


async def run_worker_loop() -> None:
    setup_logging()
    settings = get_settings()
    _log_startup_config(settings)

    db = CouchDBClient(
        base_url=settings.couchdb_url,
        db=settings.couchdb_db,
        user=settings.couchdb_user,
        password=settings.couchdb_password,
    )
    await db.ensure_db_and_indexes()
    relay = RelayClient(settings)

    logger.info("worker started, polling %s every %.1fs", settings.relay_url, settings.poll_interval_s)

    last_snapshot = 0.0            # monotonic — drives the snapshot timer
    last_snapshot_wall: datetime | None = None  # wall clock — reported in the heartbeat
    last_reconcile = 0.0          # monotonic — drives the reverse-sync timer
    # WRK-8 heartbeat counters (reset each heartbeat).
    polls = 0
    jobs_processed = 0
    last_heartbeat = time.monotonic()
    # WRK-9 relay-unreachable dampening.
    consecutive_poll_failures = 0
    first_failure_t = 0.0

    try:
        while True:
            now = time.monotonic()
            # WRK-10: emit yesterday's token rollup as soon as the date turns.
            # Checked every pass (a string compare, once per poll interval) so
            # the line lands within seconds of midnight rather than waiting for
            # the next job or heartbeat, which on a quiet day may be hours.
            usage.log_daily()
            if now - last_heartbeat >= settings.heartbeat_interval_s:
                logger.info(
                    "heartbeat polls=%d jobs_processed=%d last_snapshot=%s usage_unreported_calls=%d",
                    polls, jobs_processed,
                    last_snapshot_wall.isoformat() if last_snapshot_wall else "never",
                    usage.pending_calls(),
                )
                polls = 0
                jobs_processed = 0
                last_heartbeat = now
                # Catches the case where the relay accepts jobs but rejects
                # usage pushes: without this the counter would only advance
                # after the next job, and a quiet worker would look healthy
                # while its ledger silently backed up.
                await _push_usage(relay)

            polls += 1
            job = None
            try:
                job = await relay.fetch_next_job()
                if consecutive_poll_failures:
                    logger.info(
                        "relay reachable again after %d failed polls, %.0fs",
                        consecutive_poll_failures, time.monotonic() - first_failure_t,
                    )
                    consecutive_poll_failures = 0
            except Exception as e:  # noqa: BLE001 — network flake; dampened per WRK-9
                consecutive_poll_failures += 1
                if consecutive_poll_failures == 1:
                    first_failure_t = time.monotonic()
                    logger.warning("relay poll failed (will retry): %s", _exc_summary(e))
                # Subsequent failures are counted but not logged (no traceback
                # spam at a 3s interval) — the recovery line above reports how
                # many and how long once the relay comes back.

            if job is None:
                # Reverse-sync: fold any Obsidian edits back into the JSON
                # docs, then push a fresh snapshot if anything changed so the
                # PWA reflects the edit right away.
                if time.monotonic() - last_reconcile >= settings.reconcile_interval_s:
                    changed = await _reconcile(db)
                    last_reconcile = time.monotonic()
                    if changed:
                        if (pushed := await _push_snapshot(relay, db, trigger="reconcile")) is not None:
                            last_snapshot_wall = pushed
                        last_snapshot = time.monotonic()
                if time.monotonic() - last_snapshot >= settings.items_snapshot_interval_s:
                    if (pushed := await _push_snapshot(relay, db, trigger="timer")) is not None:
                        last_snapshot_wall = pushed
                    last_snapshot = time.monotonic()
                await asyncio.sleep(settings.poll_interval_s)
                continue

            job_id, job_type = job["id"], job["type"]
            started = time.monotonic()
            logger.info("job start job_id=%s type=%s", job_id, job_type)
            try:
                result = await process_job(job, settings, db)
            except PhaseError as e:
                logger.error(
                    "job failed job_id=%s type=%s phase=%s duration_s=%.2f: %s",
                    job_id, job_type, e.phase, time.monotonic() - started, e.original,
                    exc_info=e.original,
                )
                await _report_failed(relay, job_id, e.original)
            except Exception as e:  # noqa: BLE001 — any failure becomes a failed job, not a crashed worker
                logger.error(
                    "job failed job_id=%s type=%s phase=unknown duration_s=%.2f",
                    job_id, job_type, time.monotonic() - started, exc_info=True,
                )
                await _report_failed(relay, job_id, e)
            else:
                # A record-mutating job refreshes the items snapshot BEFORE
                # signalling done, so a client that polls the result right away
                # (after an edit/delete/normalize/rebuild) sees fresh /items
                # rather than the pre-change snapshot.
                if job_type in _MUTATING_JOB_TYPES:
                    if (pushed := await _push_snapshot(relay, db, trigger="post-job")) is not None:
                        last_snapshot_wall = pushed
                    last_snapshot = time.monotonic()
                try:
                    await relay.post_job_done(job_id, result)
                except Exception as e:  # noqa: BLE001 — the result never reached the relay
                    logger.error(
                        "job failed job_id=%s type=%s phase=result_post duration_s=%.2f",
                        job_id, job_type, time.monotonic() - started, exc_info=True,
                    )
                    await _report_failed(relay, job_id, e)
                else:
                    # A capture that produced no usable note comes back as a
                    # normally-posted result whose own status is "failed"
                    # (graceful degradation, see capture_service) — surface
                    # that here so the lifecycle line tells the truth (goal 1).
                    inner = result.get("status") if isinstance(result, dict) else None
                    if inner == "failed":
                        logger.info(
                            "job end job_id=%s type=%s status=failed duration_s=%.2f error=%r",
                            job_id, job_type, time.monotonic() - started, result.get("error"),
                        )
                    else:
                        logger.info(
                            "job end job_id=%s type=%s status=done duration_s=%.2f",
                            job_id, job_type, time.monotonic() - started,
                        )
            jobs_processed += 1
            # Every job that ran a model spent tokens; report them now rather
            # than at the next heartbeat, so the Admin tab's counter moves at
            # roughly the same time as the job it belongs to.
            await _push_usage(relay)
    finally:
        # A restart mid-day would otherwise drop both traces of the day so far:
        # log the partial rollup, and make one last attempt to hand the unsent
        # deltas to the relay.
        usage.log_daily(final=True)
        await _push_usage(relay)
        await relay.aclose()
        await db.aclose()


async def _push_usage(relay: RelayClient) -> None:
    """Hand the pending token-usage deltas to the relay (WRK-10).

    Never raises: this is bookkeeping, and a relay hiccup must not fail a job
    that already succeeded. An unacked batch simply stays in flight and goes
    out again on the next attempt, under the same report id — so a push that
    the relay actually applied can't be counted twice when the reply is lost.
    """
    report = usage.take_report()
    if report is None:
        return
    report_id, rows = report
    try:
        await relay.push_usage(report_id, rows)
    except Exception as e:  # noqa: BLE001 — retried on the next pass
        logger.warning("usage push failed (will retry): %s", _exc_summary(e))
    else:
        usage.ack(report_id)


async def _report_failed(relay: RelayClient, job_id: str, exc: BaseException) -> None:
    """Report a failed job to the relay; never let the report itself kill the
    loop — the relay's claim timeout will make the job claimable again."""
    try:
        await relay.post_job_failed(job_id, str(exc))
    except Exception:  # noqa: BLE001
        logger.exception("failed to report job as failed job_id=%s", job_id)


async def _reconcile(db: CouchDBClient) -> int:
    """Run one reverse-sync pass; never let it crash the loop (a CouchDB blip
    is a WARNING, retried next interval)."""
    try:
        return await reconcile_vault_edits(db)
    except Exception as e:  # noqa: BLE001
        logger.warning("reconcile pass failed: %s", _exc_summary(e))
        return 0


async def _push_snapshot(relay: RelayClient, db: CouchDBClient, *, trigger: str) -> datetime | None:
    """Push the items snapshot; log count + trigger on success (WRK-7),
    WARNING (not a traceback) on failure. Returns the push time, or None on
    failure, so the caller can report it in the heartbeat."""
    try:
        items = await query_all_items(db)
        await relay.push_items_snapshot(items, categories_metadata())
        logger.info("snapshot pushed items=%d trigger=%s", len(items), trigger)
        return datetime.now(timezone.utc)
    except Exception as e:  # noqa: BLE001
        logger.warning("failed to push items snapshot trigger=%s: %s", trigger, _exc_summary(e))
        return None


if __name__ == "__main__":
    asyncio.run(run_worker_loop())

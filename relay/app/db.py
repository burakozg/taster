"""SQLite-backed job queue + items cache for the relay.

Single-writer assumption: this relay is meant to run as exactly one Fly
machine (see fly.toml — no horizontal scaling). SQLite's file locking is
enough at that concurrency level; this is not designed to survive multiple
relay instances writing at once.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

JobType = Literal[
    "capture_photo", "capture_chat", "lookup",
    "manage_plan", "manage_apply", "repair_pairings_plan",
    "sync_status", "sync_rebuild_vault", "sync_rebuild_records", "sync_normalize",
    "record_update", "record_delete",
]
JobStatus = Literal["pending", "processing", "done", "failed"]

logger = logging.getLogger("relay.jobs")

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _age_s(from_iso: str | None) -> float:
    """Seconds between an ISO timestamp column and now — used for the queued
    time, wall time, and stale-hold durations in the REL-1 lifecycle log."""
    if not from_iso:
        return 0.0
    return (datetime.now(timezone.utc) - datetime.fromisoformat(from_iso)).total_seconds()


def init_db(db_path: str) -> None:
    global _conn
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(db_path, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                result TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                claimed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_status_created
                ON jobs(status, created_at);

            CREATE TABLE IF NOT EXISTS items_cache (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                items_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                settings_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            -- WRK-10 token ledger. One row per (day, provider, model); the
            -- worker sends deltas and they are added here, so this survives
            -- worker restarts and log rotation. `day` is the WORKER's local
            -- date, passed through verbatim — the relay never derives it, so
            -- the two sides can't disagree about where a day ends.
            --
            -- Deliberately NOT covered by prune_old_jobs' retention window:
            -- this is the spend record, it is a handful of rows per day, and
            -- a year of it is smaller than one capture_photo payload.
            CREATE TABLE IF NOT EXISTS usage_daily (
                day TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                calls INTEGER NOT NULL DEFAULT 0,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (day, provider, model)
            );

            -- Ids of usage batches already added, so a retry of a push whose
            -- response was lost doesn't double-count. Short-lived: pruned with
            -- the jobs (see prune_old_jobs).
            CREATE TABLE IF NOT EXISTS usage_reports (
                report_id TEXT PRIMARY KEY,
                received_at TEXT NOT NULL
            );
            """
        )
        _conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_job(job_type: JobType, payload: dict[str, Any]) -> str:
    job_id = str(uuid.uuid4())
    now = _now()
    payload_json = json.dumps(payload)
    with _lock:
        _conn.execute(
            "INSERT INTO jobs (id, type, status, payload, created_at, updated_at) "
            "VALUES (?, ?, 'pending', ?, ?, ?)",
            (job_id, job_type, payload_json, now, now),
        )
        _conn.commit()
    # REL-1: log the size, never the payload itself (photo payloads carry
    # base64 image data — GEN-4).
    logger.info("job created job_id=%s type=%s payload_bytes=%d", job_id, job_type, len(payload_json))
    return job_id


def get_job(job_id: str) -> dict[str, Any] | None:
    with _lock:
        row = _conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return _row_to_dict(row) if row else None


def claim_next_job(claim_timeout_s: int) -> dict[str, Any] | None:
    """Claim the oldest pending job, or a 'processing' job whose claim has
    expired (covers a worker crash mid-job — see job_claim_timeout_s)."""
    stale_cutoff = (datetime.now(timezone.utc) - timedelta(seconds=claim_timeout_s)).isoformat()
    now = _now()
    with _lock:
        row = _conn.execute(
            "SELECT * FROM jobs "
            "WHERE status = 'pending' OR (status = 'processing' AND claimed_at < ?) "
            "ORDER BY created_at ASC LIMIT 1",
            (stale_cutoff,),
        ).fetchone()
        if row is None:
            return None
        _conn.execute(
            "UPDATE jobs SET status = 'processing', claimed_at = ?, updated_at = ? WHERE id = ?",
            (now, now, row["id"]),
        )
        _conn.commit()
        job = _row_to_dict(row)
    # REL-1: a claim of a row already in 'processing' is a stale-job reclaim —
    # a worker died mid-job and its claim expired. That's the earliest visible
    # sign of worker trouble, so it's WARNING, with how long the dead claim held.
    if row["status"] == "processing":
        logger.warning(
            "stale-job reclaim job_id=%s type=%s held_s=%.0f",
            row["id"], row["type"], _age_s(row["claimed_at"]),
        )
    else:
        logger.info(
            "job claimed job_id=%s type=%s queued_s=%.1f",
            row["id"], row["type"], _age_s(row["created_at"]),
        )
    job["status"] = "processing"
    return job


def complete_job(job_id: str, result: dict[str, Any]) -> None:
    with _lock:
        row = _conn.execute("SELECT created_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
        # Clear the payload the moment the job is terminal: it's never read
        # again, and for capture_photo it holds base64 image data that would
        # otherwise sit in the DB until the retention prune. The result stays
        # (the PWA still polls for it) until the row is pruned.
        _conn.execute(
            "UPDATE jobs SET status = 'done', result = ?, payload = '{}', updated_at = ? WHERE id = ?",
            (json.dumps(result), _now(), job_id),
        )
        _conn.commit()
    # REL-1: total wall time from creation, so goal-1 timelines are readable
    # from the relay log alone.
    logger.info("job completed job_id=%s status=done wall_s=%.1f", job_id, _age_s(row["created_at"] if row else None))


def fail_job(job_id: str, error: str) -> None:
    with _lock:
        row = _conn.execute("SELECT created_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
        _conn.execute(
            "UPDATE jobs SET status = 'failed', error = ?, payload = '{}', updated_at = ? WHERE id = ?",
            (error, _now(), job_id),
        )
        _conn.commit()
    logger.info("job completed job_id=%s status=failed wall_s=%.1f", job_id, _age_s(row["created_at"] if row else None))


def prune_old_jobs(retention_days: int) -> int:
    """Delete terminal (done/failed) jobs whose last update is older than the
    retention window, reclaiming SQLite space on the Fly volume. Pending and
    processing jobs are never touched, whatever their age. Returns the count
    deleted (0 is the steady state). Uses the (status, created_at) index via
    updated_at is not indexed, but at personal scale the table is tiny."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    with _lock:
        cur = _conn.execute(
            "DELETE FROM jobs WHERE status IN ('done', 'failed') AND updated_at < ?",
            (cutoff,),
        )
        _conn.commit()
        deleted = cur.rowcount
    if deleted:
        logger.info("pruned %d terminal job(s) older than %d day(s)", deleted, retention_days)
    # The usage rows themselves are kept forever (see the schema comment); only
    # the dedup ids expire. They exist to catch a retry seconds after a lost
    # response, so the job retention window is far longer than needed and the
    # table stays at a handful of rows.
    with _lock:
        _conn.execute("DELETE FROM usage_reports WHERE received_at < ?", (cutoff,))
        _conn.commit()
    return deleted


# --- WRK-10 token ledger ---------------------------------------------------

def add_usage(report_id: str, rows: list[dict[str, Any]]) -> bool:
    """Add one batch of usage deltas. Returns False if `report_id` was already
    applied (a retried push whose first response was lost) and nothing changed.

    The whole batch — dedup marker included — commits in one transaction, so
    there is no window where the id is recorded but its numbers aren't, or the
    reverse.
    """
    now = _now()
    with _lock:
        seen = _conn.execute(
            "SELECT 1 FROM usage_reports WHERE report_id = ?", (report_id,)
        ).fetchone()
        if seen:
            return False
        try:
            _conn.execute("INSERT INTO usage_reports (report_id, received_at) VALUES (?, ?)",
                          (report_id, now))
            for r in rows:
                _conn.execute(
                    "INSERT INTO usage_daily (day, provider, model, calls, input_tokens, "
                    "output_tokens, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(day, provider, model) DO UPDATE SET "
                    "calls = calls + excluded.calls, "
                    "input_tokens = input_tokens + excluded.input_tokens, "
                    "output_tokens = output_tokens + excluded.output_tokens, "
                    "updated_at = excluded.updated_at",
                    (r["day"], r["provider"], r["model"], r["calls"],
                     r["input_tokens"], r["output_tokens"], now),
                )
        except Exception:
            _conn.rollback()
            raise
        _conn.commit()
    return True


def usage_by_day(days: int) -> list[dict[str, Any]]:
    """The last `days` days that have usage, newest first, each with its
    per-model breakdown. A day on which nothing ran is simply absent rather
    than zero-filled: the relay can't invent the missing keys without deciding
    where the worker's day boundaries fall, which is exactly the guess this
    design avoids."""
    with _lock:
        rows = _conn.execute(
            "SELECT day, provider, model, calls, input_tokens, output_tokens FROM usage_daily "
            "WHERE day IN (SELECT DISTINCT day FROM usage_daily ORDER BY day DESC LIMIT ?) "
            "ORDER BY day DESC, output_tokens DESC",
            (days,),
        ).fetchall()
    by_day: dict[str, dict[str, Any]] = {}
    for row in rows:
        day = by_day.setdefault(row["day"], {
            "day": row["day"], "calls": 0, "input_tokens": 0, "output_tokens": 0, "by_model": [],
        })
        day["calls"] += row["calls"]
        day["input_tokens"] += row["input_tokens"]
        day["output_tokens"] += row["output_tokens"]
        day["by_model"].append({
            "provider": row["provider"], "model": row["model"], "calls": row["calls"],
            "input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"],
        })
    return list(by_day.values())


def usage_totals() -> dict[str, Any]:
    """All-time totals, plus the first day on record — so the Admin tab can say
    what window the number covers instead of showing a figure with no span."""
    with _lock:
        row = _conn.execute(
            "SELECT COUNT(DISTINCT day) AS days, MIN(day) AS since, "
            "COALESCE(SUM(calls), 0) AS calls, "
            "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
            "COALESCE(SUM(output_tokens), 0) AS output_tokens FROM usage_daily"
        ).fetchone()
    return {
        "days": row["days"], "since": row["since"], "calls": row["calls"],
        "input_tokens": row["input_tokens"], "output_tokens": row["output_tokens"],
    }


# Category metadata (group order/labels + edit-field specs) the worker pushes
# with each items snapshot — see backend app.categories. Kept in memory (tiny,
# static, and self-heals on the worker's next push after a relay restart); the
# relay is NOT a source of truth for categories, so the default is empty and the
# PWA falls back to its own baked list until the worker's first push arrives.
_categories_cache: list[dict[str, Any]] = []


def get_categories_cache() -> list[dict[str, Any]]:
    with _lock:
        return _categories_cache


def set_categories_cache(categories: list[dict[str, Any]]) -> None:
    global _categories_cache
    with _lock:
        _categories_cache = categories


def get_items_cache() -> list[dict[str, Any]]:
    with _lock:
        row = _conn.execute("SELECT items_json FROM items_cache WHERE id = 1").fetchone()
    return json.loads(row["items_json"]) if row else []


def set_items_cache(items: list[dict[str, Any]]) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO items_cache (id, items_json, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET items_json = excluded.items_json, updated_at = excluded.updated_at",
            (json.dumps(items), _now()),
        )
        _conn.commit()


def get_admin_settings() -> dict[str, Any]:
    """Admin-panel overrides (currently model choices). {} when unset —
    the worker then falls back to its config.yaml defaults."""
    with _lock:
        row = _conn.execute("SELECT settings_json FROM admin_settings WHERE id = 1").fetchone()
    return json.loads(row["settings_json"]) if row else {}


def set_admin_settings(settings: dict[str, Any]) -> None:
    with _lock:
        _conn.execute(
            "INSERT INTO admin_settings (id, settings_json, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET settings_json = excluded.settings_json, updated_at = excluded.updated_at",
            (json.dumps(settings), _now()),
        )
        _conn.commit()


def list_recent_jobs(limit: int = 20) -> list[dict[str, Any]]:
    """Recent jobs for the admin panel — deliberately WITHOUT payload
    (photo payloads carry base64 image data) and without the full result."""
    with _lock:
        rows = _conn.execute(
            "SELECT id, type, status, created_at, updated_at, error FROM jobs "
            "ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def job_counts() -> dict[str, int]:
    with _lock:
        rows = _conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def get_items_cache_updated_at() -> str | None:
    with _lock:
        row = _conn.execute("SELECT updated_at FROM items_cache WHERE id = 1").fetchone()
    return row["updated_at"] if row else None


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["payload"] = json.loads(d["payload"])
    if d.get("result"):
        d["result"] = json.loads(d["result"])
    return d

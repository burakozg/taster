"""Daily token-consumption ledger (WRK-10).

WRK-2 already logs one line per model call, which answers "what did *that*
capture cost?". It does not answer "what have I spent today?" — that needs
someone to grep a day of `docker logs` and add up columns, and the columns are
gone the moment log rotation drops the file (10MB x 3, see qnap/docker-compose.yml).
So the same numbers are accumulated here as well, and leave two durable traces:

1. **A daily rollup line** in the worker's log, emitted when the date rolls over
   (and once more on shutdown for the day in progress, since a worker that
   restarts at 18:00 would otherwise never log that day at all).
2. **A counter on the relay**, pushed as deltas and summed there into
   `usage_daily`. That is the copy that survives a worker restart, an image
   rebuild, and log rotation, and it's what the PWA's Admin tab reads.

Counting happens **per API call**, not per job: one job can make many calls
(the tool loop, and repair-pairings' batches), and a job that dies on iteration
three still paid for iterations one and two. Counting at the job's summary line
instead would quietly under-report exactly the jobs that went wrong.

The day key is the **worker's local date** (the container sets
TZ=Europe/Stockholm), not UTC. One clock decides where a day ends: the worker
computes the key and the relay stores whatever string it is given, so there is
no way for the two sides to disagree about which day a call belongs to. The
cost of that choice is that the day key means nothing without knowing the
worker's timezone — which is why it's stated here and echoed at startup.

Not thread-safe, and doesn't need to be: the worker is a single asyncio loop,
and every mutation below happens between awaits.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger("worker.usage")


@dataclass
class _Counts:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, *, calls: int, input_tokens: int, output_tokens: int) -> None:
        self.calls += calls
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens


# Deltas not yet accepted by the relay, keyed (day, provider, model).
_pending: dict[tuple[str, str, str], _Counts] = {}
# The batch currently being pushed, held with a STABLE id until the relay acks
# it — see take_report().
_in_flight: tuple[str, list[dict[str, Any]]] | None = None
# Per-day, per-model totals for this process, drained by log_daily().
_days: dict[str, dict[tuple[str, str], _Counts]] = {}


def _today() -> str:
    return datetime.now().date().isoformat()


def record(provider: str, model: str, input_tokens: int, output_tokens: int) -> None:
    """Book one model API call. Call this wherever a response's usage is read,
    on every provider path — that placement is the whole contract (WRK-10)."""
    day = _today()
    counts = dict(calls=1, input_tokens=input_tokens or 0, output_tokens=output_tokens or 0)
    _pending.setdefault((day, provider, model), _Counts()).add(**counts)
    _days.setdefault(day, {}).setdefault((provider, model), _Counts()).add(**counts)


# --------------------------------------------------------------------------
# 1. The daily log line
# --------------------------------------------------------------------------

def log_daily(*, final: bool = False) -> None:
    """Emit the rollup for every day that has finished — plus, when `final`,
    the day still in progress (the worker is shutting down, so nothing more
    will be added to it). Logged days are dropped, so this is also what keeps
    `_days` from growing for the life of the process."""
    today = _today()
    for day in sorted(_days):
        if day < today or final:
            _emit(day, complete=day < today)
            del _days[day]


def _emit(day: str, *, complete: bool) -> None:
    by_model = _days[day]
    total = _Counts()
    for counts in by_model.values():
        total.add(calls=counts.calls, input_tokens=counts.input_tokens, output_tokens=counts.output_tokens)
    if not total.calls:
        return
    # Ordered priciest-looking first (most output tokens) so a runaway model is
    # the first thing on the line rather than buried mid-list.
    breakdown = ",".join(
        f"{model}:{c.calls}/{c.input_tokens}/{c.output_tokens}"
        for (_provider, model), c in sorted(
            by_model.items(), key=lambda kv: -kv[1].output_tokens
        )
    )
    logger.info(
        "usage day=%s complete=%s calls=%d input_tokens=%d output_tokens=%d by_model=%s",
        day, str(complete).lower(), total.calls, total.input_tokens, total.output_tokens, breakdown,
    )


# --------------------------------------------------------------------------
# 2. The delta push to the relay
# --------------------------------------------------------------------------

def take_report() -> tuple[str, list[dict[str, Any]]] | None:
    """The next batch to push to the relay, as `(report_id, rows)`, or None if
    there is nothing to send.

    Deltas, not running totals: the relay adds them up, so a worker restart
    loses only what it hadn't pushed yet instead of resetting the day.

    The catch with additive deltas is the push that succeeds on the relay but
    whose response never arrives — retrying it would double-count real spend.
    So a batch keeps a stable `report_id` until it is acked, and the relay
    ignores an id it has already applied. New usage recorded while a batch is
    stuck in flight simply accumulates for the next one.
    """
    global _in_flight
    if _in_flight is not None:
        return _in_flight
    if not _pending:
        return None
    rows = [
        {
            "day": day, "provider": provider, "model": model,
            "calls": c.calls, "input_tokens": c.input_tokens, "output_tokens": c.output_tokens,
        }
        for (day, provider, model), c in sorted(_pending.items())
    ]
    _pending.clear()
    _in_flight = (str(uuid.uuid4()), rows)
    return _in_flight


def ack(report_id: str) -> None:
    """Mark the in-flight batch accepted. A mismatched id is ignored rather
    than clearing the slot — that would drop a batch that was never sent."""
    global _in_flight
    if _in_flight is not None and _in_flight[0] == report_id:
        _in_flight = None


def pending_calls() -> int:
    """Calls booked but not yet acked by the relay — in-flight included. Read
    by the heartbeat so a relay that is quietly rejecting usage pushes shows up
    as a number that only ever climbs."""
    in_flight = sum(r["calls"] for r in _in_flight[1]) if _in_flight else 0
    return sum(c.calls for c in _pending.values()) + in_flight

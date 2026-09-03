# Logging Requirements

What the Tasting Log system must log, why, and what must never be logged.
This is a requirements document — it says *what*, not *how*. Requirement
keywords follow RFC 2119 (MUST / SHOULD / MAY). Each requirement has an ID
(`REL-*` relay, `WRK-*` worker, `GEN-*` cross-cutting, `RET-*`
retention/access) so implementation and review can reference them.

## Current state (baseline, as of 2026-07-18)

Not "nothing", but close to it — and none of it is designed:

- **Worker** (`backend/app/worker.py`): `logging.basicConfig(INFO)`. Logs
  startup (relay URL + poll interval), per-job start, `job <id> done`,
  `job <id> failed` with traceback, poll/snapshot/report failures. No
  durations, no token usage, no Claude call metadata.
- **Relay** (`relay/app/main.py`): `logging.basicConfig(INFO)` is set but
  **no application code ever logs** — the only output is uvicorn's access
  log. Job lifecycle, auth failures, and rate-limit hits are invisible.
- **httpx** logs every outbound request at INFO in the worker — this is
  noise that drowns the signal (dozens of identical `GET /worker/jobs/next`
  lines per minute).
- **No retention story anywhere**: Fly.io keeps logs only briefly unless
  shipped; the QNAP containers use Docker's default `json-file` driver with
  no size cap (unbounded growth on the NAS); nothing is rotated.
- **CouchDB** logs to its container stdout with its own defaults (out of
  scope here except retention, see RET-3).

## Goals

The logs must be able to answer these questions after the fact. These are
the acceptance criteria for the requirements below:

1. *"Why did capture/lookup X fail?"* — from either the relay or the worker
   log alone, using the id shown in the PWA.
2. *"What did Claude cost me this week, and on what?"* — per-call token
   usage summable from worker logs.
3. *"Is the worker alive, and when did it last do anything?"* — without
   SSHing into CouchDB or guessing from silence.
4. *"Is anyone (or anything) hammering the relay?"* — auth failures and
   rate-limit hits with client IPs.
5. *"Did the snapshot the PWA's Items tab shows actually update?"* — last
   push time and item count, both sides.
6. *"Did a leaked key get used?"* — auth failures distinguishable by which
   surface (PWA key vs worker key) and from which IP.

## Non-goals

- No metrics/observability stack (Prometheus, Grafana, OpenTelemetry
  tracing). Personal system; logs are the observability.
- No external log aggregation service. Logs stay on Fly and the NAS.
- No audit-grade tamper evidence.

---

## Cross-cutting requirements (GEN)

- **GEN-1** Every log line MUST include a level and an ISO-8601 or
  equivalent timestamp (the `logging`/uvicorn defaults satisfy this once
  configured with a format that includes time — note `basicConfig`'s
  default format does *not*).
- **GEN-2 (correlation)** Every log line about a specific job MUST include
  the job id (`capture_id`/`lookup_id` — they are the same value on both
  sides). This is what makes goal 1 possible: the id the PWA displays must
  grep cleanly in both the relay and worker logs.
- **GEN-3 (secrets)** The following MUST NEVER appear in any log line at
  any level: `TASTER_API_KEY`, `WORKER_API_KEY`, `ANTHROPIC_API_KEY`,
  CouchDB credentials, `Authorization` header values. On auth failure, log
  that a bad credential was presented — never the credential itself, not
  even truncated.
- **GEN-4 (payload hygiene)** Image data (raw or base64) MUST NOT be
  logged. Free-text capture/lookup content (the user's note text,
  questions, Claude's answers) MUST NOT be logged at INFO or above; it MAY
  be logged at DEBUG (this is a single-user system, but logs travel — Fly's
  infrastructure, NAS backups — while the vault content belongs only in
  CouchDB). Note names/doc ids are fine: they're identifiers, not content.
- **GEN-5 (levels)** Level usage MUST follow:
  - `DEBUG` — payload contents, per-request HTTP chatter. Off by default.
  - `INFO` — state changes worth a line: job lifecycle, writes, snapshots,
    startup/shutdown, Claude call summaries.
  - `WARNING` — degraded but self-healing: poll failure that will be
    retried, stale-job reclaim, index-creation refusal, rate-limit hit.
  - `ERROR` — a job failed or a component gave up; includes traceback.
- **GEN-6 (noise)** Routine polling MUST NOT produce per-request log lines
  at INFO. Concretely: the `httpx` logger MUST be set to WARNING in the
  worker, and uvicorn access-log entries for `GET /worker/jobs/next` SHOULD
  be suppressed or sampled — a healthy idle system should produce
  near-zero log volume (see WRK-8 for the liveness heartbeat that replaces
  it).
- **GEN-7 (format)** Lines MUST be single-line, human-greppable, with
  stable `key=value` fields for the machine-relevant parts (ids, counts,
  durations, token counts). Structured JSON logging MAY be adopted later;
  it is not required at this scale.
- **GEN-8 (startup config echo)** Each component MUST log, once at
  startup, its effective non-secret configuration (relay: db path, claim
  timeout, CORS origin, rate limit; worker: relay URL, poll/snapshot
  intervals, CouchDB URL + database + user *name*, model ids, effort,
  token ceilings, `max_uses`, iteration cap). Secrets are logged as
  present/absent only.

## Relay requirements (REL)

- **REL-1 (job lifecycle)** The relay MUST log: job created (`job_id`,
  `type`, payload size in bytes — not the payload), job claimed by the
  worker (`job_id`, time spent queued), job completed (`job_id`,
  done/failed, total wall time from creation), and **stale-job reclaim**
  (`job_id`, how long the dead claim held it) at WARNING — a reclaim means
  a worker died mid-job and is the earliest visible sign of worker trouble.
- **REL-2 (auth failures)** Every 401 MUST be logged at WARNING with:
  which key surface was hit (client vs worker endpoint), the request path,
  and the client IP (via `Fly-Client-IP`, matching `rate_limit.py`'s
  resolution). Repeated failures from one IP are goal-6's leaked-key /
  probe signal.
- **REL-3 (rate limiting)** Every 429 MUST be logged at WARNING with
  client IP and path. A legitimate single-user system should essentially
  never emit these (the limit is sized above the PWA's polling rate), so
  any occurrence is signal.
- **REL-4 (snapshot cache)** Each `/worker/items/snapshot` acceptance MUST
  be logged at INFO with item count and payload size. Serving `/items`
  MUST NOT be logged per-request (it's covered by the access log), but the
  cache being **empty** when queried SHOULD be logged once (not per
  request) — it means the worker has never pushed.
- **REL-5 (4xx/5xx visibility)** Application errors returned to the PWA
  (404 unknown job id, 400 validation) SHOULD be visible at INFO via the
  access log; unhandled 500s MUST log the traceback at ERROR.
- **REL-6 (startup)** On startup: REL's GEN-8 config echo, plus whether
  the SQLite file already existed and how many jobs were in each status —
  a restart with hundreds of `pending` jobs is a stuck-worker signal.
- **REL-7 (usage reports)** Each `POST /worker/usage` (the WRK-10 ledger)
  MUST log the report id, row count, and the batch's call/token totals at
  INFO. A duplicate report — one the relay has already added — MUST also
  be logged: it is the expected, harmless outcome of a lost
  acknowledgement, but a steady stream of them means every ack is being
  lost, which is a stuck push loop that would otherwise look like silence.

## Worker requirements (WRK)

- **WRK-1 (job lifecycle)** For every job: start (`job_id`, `type`) and
  end (`job_id`, done/failed, duration) at INFO; failures at ERROR with
  traceback (already present) **and** the failure phase — poll, Claude
  call, validation, CouchDB write, or result post — so goal 1 doesn't
  require reading the traceback to know which integration broke.
- **WRK-2 (Claude call telemetry)** Every Claude API call MUST log one
  INFO summary line: `job_id`, call site (capture/lookup), model,
  `stop_reason`, input tokens, output tokens, number of loop iterations,
  number of `query_notes` executions, whether web search was attached
  (`web_search`), how many searches the model actually ran (`web_searches`,
  `?` on provider paths that can't count them), and duration. The two
  web-search fields are deliberately separate: "attached" alone cannot
  distinguish search being off from the model simply declining to search,
  which is the first question asked whenever an enriched field like
  `common_notes` comes back empty. Token counts come from `response.usage`
  and MUST be summable per line (goal 2 — this is the cost ledger; there is
  no other record of spend). `pause_turn` resumes and `refusal` stop reasons
  MUST each log their own WARNING line, as MUST a server-side web search that
  returns an error block (those arrive HTTP 200 and raise nothing).
- **WRK-3 (Claude call failures)** API errors MUST log the HTTP status,
  the Anthropic error type, and the `request-id` from the response
  (Anthropic support needs it; the SDK exposes it as `_request_id`) — at
  ERROR, without the request payload.
- **WRK-4 (validation failures)** A structured-output validation failure
  (the `CaptureFailed` path) MUST log which field(s) failed at WARNING.
  The raw model output MAY be logged at DEBUG only (GEN-4).
- **WRK-5 (CouchDB writes)** Every successful note write MUST log
  `doc_id` and `job_id` at INFO — this is the one place the "did it reach
  the vault" question is answerable before the LiveSync spike is done.
  Repeat-detection hits (`prior_match`) SHOULD log the matched `doc_id`.
- **WRK-6 (CouchDB errors)** Connection/auth failures against CouchDB MUST
  be logged at ERROR with the operation attempted, never with credentials
  (GEN-3). The startup `ensure_db_and_indexes` outcome MUST be logged:
  db existed / db created / index warnings (the member-account 403 on
  index creation is expected and MUST stay WARNING, not ERROR).
- **WRK-7 (snapshot pushes)** Every snapshot push MUST log item count and
  trigger (post-capture vs timer) at INFO; failures at WARNING (already
  present, keep).
- **WRK-8 (liveness heartbeat)** Because GEN-6 silences per-poll logging,
  the worker MUST emit a heartbeat line at INFO on a slow interval
  (e.g. every 30–60 min): polls since last heartbeat, jobs processed,
  last snapshot time. This is goal 3 — "is it alive" answerable from
  `docker logs taster-worker` without grepping absence.
- **WRK-9 (relay unreachable)** Consecutive poll failures MUST NOT log a
  full traceback each attempt (3s interval → traceback spam). Log the
  first failure at WARNING with the exception summary, then suppress or
  count until recovery, and log recovery ("relay reachable again after N
  failed polls, Xs") at INFO.
- **WRK-10 (daily token ledger)** WRK-2 answers "what did that job cost";
  this answers "what did today cost", which no amount of per-job lines
  gives you once rotation (RET-1) drops the file. Every model call's
  tokens MUST be accumulated per day and per model — counted **per API
  call**, not per job, so a job that fails on its third tool iteration
  still books the first two — and MUST leave two traces:
  - one INFO rollup line per day (`usage day=… calls=… input_tokens=…
    output_tokens=… by_model=…`), emitted when the date rolls over, plus
    the partial day on shutdown so a restart doesn't erase it;
  - the same numbers pushed to the relay as **deltas** and summed there
    into `usage_daily`, which is the copy that outlives the worker.
  Days are the worker's LOCAL dates; the relay MUST store the day key it
  is given rather than deriving one, so the two sides cannot disagree
  about where a day ends. Pushes MUST carry a report id that is stable
  across retries — additive deltas replayed after a lost response would
  otherwise inflate the ledger. The heartbeat (WRK-8) MUST report the
  count of calls not yet acked, so a relay that is quietly rejecting
  usage pushes shows up as a number that only climbs.

## Retention & access requirements (RET)

- **RET-1 (QNAP)** Both QNAP containers MUST cap log growth via Docker
  `json-file` rotation options in `qnap/docker-compose.yml`
  (e.g. `max-size: 10m`, `max-file: 3` — exact numbers implementation's
  choice). Unbounded logs on the NAS share are not acceptable.
- **RET-2 (Fly)** Fly's ephemeral log retention is ACCEPTED as-is for the
  relay — no log shipping required. Consequence to accept knowingly:
  goals 4 and 6 are only answerable for the recent window on the relay
  side. If that ever becomes insufficient, revisit (Fly log shipper or
  writing security-relevant WARNINGs to the SQLite volume) — out of scope
  now.
- **RET-3 (CouchDB)** CouchDB's own logging stays at its image defaults;
  it is covered by RET-1's rotation cap. Its logs contain credentials
  never (verify once) and are not part of the answerable-questions goals.
- **RET-4 (access)** Reading logs MUST require nothing beyond what exists:
  `fly logs` for the relay, `ssh` + `docker logs` for the QNAP (per the
  documented absolute docker path). No new access paths.
- **RET-5 (backups)** Logs MUST NOT be added to the backup set. The
  backup story (INSTALL.md → Backups) covers the vault only; logs are
  operational exhaust, and RET-1's rotation makes them unsuitable as
  records anyway. This is exactly why the token-spend ledger does not
  live in the logs alone: WRK-2's per-job lines rotate away, so WRK-10
  mirrors the same numbers into the relay's SQLite volume, which is
  durable. That copy is the app's own spend history; each provider's
  console remains the authoritative billing record.

## Explicitly out of scope

- PWA/client-side logging (browser console is enough; no error-reporting
  endpoint).
- `deploy.sh` / build tooling logging (interactive, already prints).
- Log-based alerting/notification of any kind.

## Implementation notes (non-normative)

Hints, not requirements: GEN-6 is one line
(`logging.getLogger("httpx").setLevel(logging.WARNING)`); REL-2/REL-3 fit
naturally in `auth.py`/`rate_limit.py` where the 401/429 is raised; WRK-2's
counters are all local to the loops in `capture_service.py` /
`lookup_service.py`; WRK-10's live in `app/usage.py`, called from wherever a
response's usage is read — that placement is the whole contract, and it is
the one thing to check when a new provider path is added; RET-1 is a
`logging:` block per service in the QNAP compose file. Nothing here needs new
dependencies.

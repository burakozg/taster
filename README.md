# Tasting Log

A personal system for logging whisky, cigar, coffee, pipe-tobacco, beer, and
chocolate tastings with minimal friction, and recalling them naturally — including
standing in a shop deciding what to buy. Categories are **registry-driven**
(`backend/app/categories.py`): adding a new one (pipe tobacco, then beer, were
added this way) is a Pydantic model plus a single registry line — no scattered
edits across queries, prompts, or the PWA.

Two capture modes (photo of a label, or free-text chat — either can take a live
camera shot **or** an uploaded image/screenshot), one enrichment step (Claude
extracts fields and fills gaps via web search, in a single call), one source of
truth (an Obsidian vault, synced via self-hosted CouchDB), and two lookup modes
(offline Dataview queries, or a conversational chat — including a "shop mode"
that takes a photo of a bottle on a shelf). Records aren't write-only: a
periodic **reverse-sync** folds Obsidian edits back into the queryable data, and
the PWA can edit/delete a record or run **AI bulk maintenance** (plan → approve
→ apply) over the whole vault.

The QNAP worker never accepts an inbound connection from anything — no
reverse proxy, no DuckDNS, no port-forwarding. It polls a small relay on
Fly.io outbound for work and posts results back the same way, which works
through any home router/firewall with zero configuration. See
`ARCHITECTURE.md` for the full split.

## Documents

| Document | What it's for |
|---|---|
| [`ARCHITECTURE.md`](./ARCHITECTURE.md) | How the system is actually built today — components, data flow, data model, API surface, config. Start here to understand the code. |
| [`tasting-log-design.md`](./tasting-log-design.md) | The original design doc and decision log — why things are shaped the way they are, alternatives considered, open questions. Read this for rationale; read `ARCHITECTURE.md` for current fact. |
| [`INSTALL.md`](./INSTALL.md) | Step-by-step setup: local verify, relay deploy to Fly.io, worker deploy to the QNAP, Obsidian LiveSync. |
| [`LOGGING.md`](./LOGGING.md) | Logging requirements — what each component must log (job lifecycle, Claude token spend, auth/rate-limit events), what must never be logged, retention. **Implemented** (`logging_setup.py` in both services): structured job-lifecycle logging with durations/failure-phase, a slow liveness heartbeat, and dampened relay-unreachable reporting. |

## Repo layout

```
backend/               The QNAP worker — polls the relay, does the Claude/
                       CouchDB work. NOT an HTTP server; no inbound port.
  app/                  application code (worker.py is the entrypoint)
  config.yaml           model choice + Claude call tuning
  .env.example          secrets template — copy to .env, never commit the real one
  Dockerfile
relay/                 PWA + job queue — the only piece exposed to the
                       public internet, deployed to Fly.io
  app/                  FastAPI: enqueue/poll (PWA-facing), claim/result
                       (worker-facing), SQLite job queue
  static/                the PWA itself (index.html, app.js, style.css,
                       manifest.json + sw.js + icon.svg for install-
                       ability) — served same-origin as the API, no
                       separate backend URL needed
  fly.toml
  .env.example
qnap/                  The real QNAP deployment — Container Station app config
  docker-compose.yml     couchdb + worker, both under
                         /share/Container/taster/ — worker has NO build:
                         here; it's an image loaded via `docker load`
  docker-compose.yml     (shipped to the NAS by ./deploy — never edit it there)
../deploy              builds the worker for the NAS's CPU architecture
                         on your Mac, streams it straight into `docker
                         load` over a single SSH pipe (no rsync or scp —
                         neither works on this NAS's SSH setup), restarts
  couchdb.env.example    CouchDB admin bootstrap credentials template
docker-compose.dev.yml Mac-local dev/test only — couchdb + relay + worker
                       together, so a full capture/lookup can be driven
                       end to end at http://localhost:8000
tasting-log-design.md
ARCHITECTURE.md
INSTALL.md
```

## Quick start

See [`INSTALL.md`](./INSTALL.md) for the full walkthrough. Short version:

```bash
# Verify the whole pipeline on Mac first (couchdb + relay + worker together)
cp backend/.env.example backend/.env   # fill in secrets, see INSTALL.md step 1
cp relay/.env.example relay/.env
docker compose -f docker-compose.dev.yml up -d --build
open http://localhost:8000   # the actual PWA, driving a local worker

# Deploy the relay (PWA + job queue) to Fly.io
# (--region must match fly.toml's primary_region, or the deploy can't
# schedule onto the volume)
cd relay && fly launch --no-deploy && fly volumes create taster_relay_data --size 1 --region arn
fly secrets set TASTER_API_KEY=... WORKER_API_KEY=...
fly deploy && cd ..

# Determine the NAS's CPU architecture once (don't guess — an image built
# for the wrong one fails outright, "exec format error"). Confirmed for
# this NAS: x86_64 -> linux/amd64.
cp deploy.env.example .deploy.env   # then set NAS_SSH / NAS_PLATFORM in it

# Build the worker for that architecture and stream it straight into
# `docker load` on the NAS over a single SSH pipe (no rsync, no scp —
# neither works on this NAS's SSH setup) — see INSTALL.md for the
# one-time compose-file + secrets setup this depends on first.
./deploy
```

## Status

- Relay and worker are built and verified to work correctly, end to end:
  the full CouchDB + relay + worker stack was brought up locally
  (`docker-compose.dev.yml`) and a real capture job was driven through the
  entire chain — PWA enqueue → relay job queue → worker poll/claim →
  real Anthropic API call (failed only on auth, since no real key was
  available in that test environment — everything up to and including the
  live API call worked) → result posted back → PWA poll correctly showed
  the failure. Auth boundaries (separate PWA vs. worker secrets), the
  items-snapshot cache, and the lookup job flow were all verified the same
  way. The least-privilege CouchDB flow (INSTALL.md step 6) was also
  verified against a real `couchdb:3` container: member account created,
  `_security` locked down, worker restarted on member credentials and ran
  cleanly (queries and writes confirmed).
- The worker's cross-build → stream → load → run pipeline for the QNAP was
  also verified for real: built for `linux/amd64` on this arm64 Mac,
  piped through `save | gzip | gunzip | load` with zero intermediate files
  (no scp/rsync involved, matching this NAS's SSH setup), and ran the
  reloaded image under emulation.
- **Now deployed and in real use.** The relay is live on Fly.io
  (health-checked, all routes registered) and the worker runs on the QNAP
  against real CouchDB + Claude. LiveSync is
  confirmed working end to end: worker-written notes surface in Obsidian, and
  human edits there flow back via the reverse-sync reconciler
  (`backend/app/reconcile.py`).
- **Deploy split to remember**: the relay carries the PWA + all job-broker
  endpoints (`fly deploy`); the worker carries the schema, the category
  registry, prompts, and all the CouchDB/Claude work (rebuild the image via
  `./deploy`). A backend change (new field, new category, prompt tweak)
  needs the **worker** redeployed; a PWA or endpoint change needs the **relay**
  redeployed. Both bake in their config/code at build time, so neither is a
  live edit-in-place.
- **Beyond capture/lookup**, the system now also does: reverse-sync of Obsidian
  edits, AI bulk maintenance (`manage_plan`/`manage_apply`, behind mandatory
  approval), forced full-vault sync (`sync_status`/`rebuild_vault`/
  `rebuild_records`/`normalize`), and single-record edit/delete
  (`record_update`/`record_delete`) from the PWA's item detail view.

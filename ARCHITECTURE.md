# Architecture

This describes the system as it's actually built. For the reasoning behind
these choices (alternatives considered, tradeoffs, open questions), see
[`tasting-log-design.md`](./tasting-log-design.md) — that document is the
design history; this one is current fact, kept in sync with the code.

## Data flow

The system is split into three pieces, and the key property is that **the
QNAP worker never accepts an inbound connection from anything** — no
reverse proxy, no DuckDNS, no port-forwarding. It only ever makes outbound
calls (to the Fly relay and to CouchDB), which works through any home
router/firewall with zero configuration. This replaced an earlier
reverse-proxy design specifically to avoid needing inbound exposure on a
home network at all.

```
┌─────────────┐     ┌─────────────┐
│ Photo capture│     │ Chat entry  │   Browser — PWA served by the relay
│ (PWA)        │     │ (PWA)       │   (same origin as its own API)
└──────┬───────┘     └──────┬──────┘
       │                    │
       └─────────┬──────────┘
                  ▼
         POST /capture/photo or /capture/chat     relay/app/routes/capture.py
                  │                               (Fly.io — public internet)
                  ▼ 202 Accepted (capture_id) — enqueued in SQLite, returned
                  │                              immediately
         ┌─────────────────────┐
         │  relay/app/db.py     │  jobs table: pending → processing → done/failed
         │  (SQLite on a Fly    │
         │   volume)             │
         └────────┬─────────────┘
                  ▲ outbound poll only, every few seconds
                  │ GET /worker/jobs/next
         ┌─────────────────────┐
         │  QNAP worker          │   backend/app/worker.py — runs on the QNAP,
         │  (poll → process →    │   NO inbound port, NO HTTP server at all
         │   post result)        │
         └────────┬─────────────┘
                  │
                  ▼
     One Claude API call:                    backend/app/capture_service.py
       image block (photo only)
     + web_search_20260209 (photo only)
     + query_notes tool (always)
     + output_config.format (structured JSON)
                  │
                  ▼
     Pydantic validation                     backend/app/schema.py
     + deterministic repeat-detection
                  │
                  ▼
     Markdown + frontmatter render            backend/app/markdown.py
                  │
                  ▼
     Written to CouchDB                       backend/app/couchdb_client.py
     (append-only, new _id every time)
                  │
                  ▼
     POST /worker/jobs/{id}/result ───────────► relay marks job done, PWA's
                                                 poll of GET /capture/{id}
                                                 picks it up
                  │
                  ▼
                  CouchDB (qnap/docker-compose.yml)
                  │
     ┌────────────┴────────────┐
     ▼                          ▼
Obsidian LiveSync          Worker reads directly, pushes a full
(phone / laptop)           items snapshot to the relay's cache
     │  ▲                  (after every mutating job + every 60s);
     ▼  │ reverse-sync     reverse-sync also folds Obsidian edits
Dataview queries           back into the JSON docs (reconcile.py)
(offline, instant)              │
                                ▼
                           GET /items on the relay serves that
                            cached snapshot — relay can't reach
                            CouchDB directly (LAN-only)

Lookup: POST /lookup → same enqueue/poll shape as capture. The worker's
query_notes tool loop (backend/app/lookup_service.py) runs exactly as
before; only the transport around it changed.
```

The single most consequential simplification versus a naive design:
**capture is one Claude call**, not a serial vision-then-search pipeline.
The photo (if any), the server-side web search tool, the `query_notes`
tool, and the structured-output schema are all present in the same
request; Claude resolves label extraction, gap-filling, repeat-detection
support, and pairing resolution/suggestion within that one call (looping
only on `tool_use` / `pause_turn`, handled in `capture_service.py`). That
part is unchanged by the relay/worker split below — only the transport
between the browser and this call changed.

## Components

### Relay (`relay/`)

FastAPI app deployed to Fly.io (`Dockerfile`, `fly.toml`) — the **only**
piece of this system exposed to the public internet. Two jobs:

1. **Serves the PWA** — static files (`static/index.html`, `static/app.js`,
   `static/style.css`, plus `manifest.json`/`sw.js`/`icon.svg`, which make
   it an installable PWA — the service worker is deliberately cache-free,
   install-ability only) mounted at `/`, same origin as the API below. No
   separate "backend URL" setup step in the PWA anymore — just the shared
   API key.
2. **Holds the job queue** — SQLite (`app/db.py`) on a Fly volume. PWA
   requests enqueue a job and return immediately (`202` + job id); the PWA
   polls for the result. The QNAP worker polls a separate endpoint for
   pending jobs and posts results back.

| File | Responsibility |
|---|---|
| `app/main.py` | App wiring: CORS, route registration, static file mount, `init_db()` on startup |
| `app/config.py` | Settings — `TASTER_API_KEY` (PWA-facing), `WORKER_API_KEY` (NAS-facing), DB path |
| `app/db.py` | SQLite job queue + items cache — `create_job`, `claim_next_job` (atomic claim with a staleness timeout for a crashed worker), `complete_job`/`fail_job`, `get_items_cache`/`set_items_cache`, plus the in-memory category cache (`get`/`set_categories_cache`) |
| `app/auth.py` | Two **separate** bearer-token checks — see Security summary below for why |
| `app/rate_limit.py` | In-memory sliding-window limiter — this is now the load-bearing rate limit in the system, since the worker accepts no inbound traffic at all |
| `app/routes/capture.py`, `routes/lookup.py` | PWA-facing: enqueue a job, poll for its result |
| `app/routes/items.py` | PWA-facing: serves the cached items snapshot (never queries CouchDB — the relay has no way to reach it) |
| `app/routes/categories.py` | PWA-facing: serves the category registry metadata (group labels + edit forms) the worker pushes — see the Categories section |
| `app/routes/manage.py` | PWA-facing: enqueue/poll the AI-maintenance plan + apply jobs |
| `app/routes/sync.py` | PWA-facing: enqueue/poll the forced full-vault sync jobs (status/rebuild/normalize) |
| `app/routes/record.py` | PWA-facing: enqueue/poll single-record update/delete jobs |
| `app/routes/admin.py` | PWA-facing: the Admin tab's API — model selection (stored in SQLite, attached to each job the worker claims), recent-jobs list, status |
| `app/models_catalog.py` | The curated model list the admin dropdowns offer (Anthropic/OpenAI/Mistral, with relative € cost) — also validates `PUT /admin/settings` |
| `app/routes/worker.py` | NAS-facing only: `GET /worker/jobs/next` (claim — response carries the admin panel's `model_overrides`), `POST /worker/jobs/{id}/result`, `POST /worker/items/snapshot` (items **+ category metadata**) |

**Single-instance constraint**: SQLite here assumes exactly one Fly
machine. `fly.toml` doesn't configure horizontal scaling — don't add it
without also replacing the job queue with something that tolerates
concurrent writers.

### QNAP worker (`backend/app/`)

A plain Python polling loop (`worker.py`, run via `python -m app.worker`)
— **not** an HTTP server. No `EXPOSE` in the Dockerfile, no listening
socket, no inbound connection of any kind. It polls the relay every
`POLL_INTERVAL_S` (default 3s) for work, processes it, posts the result
back, and pushes a full items snapshot (with the category metadata) after
**every record-mutating job** and on a `ITEMS_SNAPSHOT_INTERVAL_S` (default 60s)
timer as a catch-all for manual CouchDB edits. It also runs the reverse-sync
reconciler on a `RECONCILE_INTERVAL_S` (default 30s) timer between jobs.

| File | Responsibility |
|---|---|
| `app/worker.py` | The poll/process/post loop — `run_worker_loop()`, entrypoint; dispatches all job types; snapshot-after-mutation + reverse-sync timers |
| `app/relay_client.py` | Thin outbound HTTP client to the relay — `fetch_next_job`, `post_job_done`/`post_job_failed`, `push_items_snapshot` (items **+ category metadata**). The entire network surface the worker exposes: none — it only ever initiates |
| `app/config.py` | Settings — `RELAY_URL`, `WORKER_API_KEY`, poll/snapshot/reconcile/heartbeat intervals, CouchDB credentials, model/effort/token settings (incl. `max_tokens_manage`) from `config.yaml` |
| `app/categories.py` | **The category registry** — single source of truth for the note types (see the Categories section under Data model) |
| `app/schema.py` | Pydantic models: `WhiskyNote`, `CigarNote`, `CoffeeNote`, `PipeTobaccoNote`, `BeerNote`, `PairingNote` — discriminated on `type`; the unions + `parse_any_note` derive from one model tuple. The real validation layer |
| `app/capture_json_schema.py` | The flat JSON schema passed to Claude as `output_config.format` on capture calls (type enum derived from the registry) |
| `app/capture_service.py` | The capture call: builds the request (image + tools + schema), runs the tool-use/`pause_turn` loop, validates, runs deterministic repeat-detection, stamps `uid` |
| `app/lookup_service.py` | The lookup call: same tool-loop shape, natural-language answer, shop-mode photo support |
| `app/manage_service.py` | AI bulk maintenance — `run_manage_plan` (propose changes, write nothing), `run_manage_apply` (apply the approved subset, each change re-validated through the schema), and `run_repair_pairings_plan` (regenerate every item's cross-category pairings without re-capture; applied via `manage_apply`, whose changes then carry a structured `pairings` field) |
| `app/sync_service.py` | Forced full-vault sync — `sync_status`, `rebuild_vault` (DB → Obsidian, re-render), `rebuild_records` (Obsidian → DB, upsert-only), `normalize_records` (schema-drift fix, e.g. legacy `origin_country` → `country_of_origin`) |
| `app/record_service.py` | Single-record `update_record`/`delete_record` for the PWA's item detail view (the sanctioned in-place exceptions to append-only capture) |
| `app/reconcile.py` | Reverse-sync — folds a human's Obsidian edit back into the JSON doc, matched by `uid` (see the CouchDB section) |
| `app/markdown.py` / `app/markdown_parse.py` | Render a validated note to markdown+frontmatter, and its inverse (used by reverse-sync) |
| `app/tools.py` | `query_notes` — the one read-only tool shared by capture, lookup, and maintenance |
| `app/items_query.py` | Pulls the full items list from CouchDB for the snapshot push — filtering by type/status/rating/tag happens relay-side against this snapshot |
| `app/couchdb_client.py` | HTTP client + Mango query wrapper — see the CouchDB section for what this does and doesn't guarantee (incl. reverse-sync helpers, `uid` index) |
| `app/openai_provider.py` | Responses-API mirror of the capture/lookup/manage shape, used when an admin `gpt-*` model is selected |
| `app/mistral_provider.py` | Mistral path for `mistral-*`/`pixtral-*` models — Agents/Conversations API (vision + `web_search` connector + `query_notes` + structured output), auto-falling-back to Mistral's chat API (no web search) if that call fails |
| `app/logging_setup.py`, `app/errors.py` | Structured logging setup (LOGGING.md) and `PhaseError` for failure-phase reporting |

`process_job()` in `worker.py` dispatches by job `type` to the matching service
function. Job types: `capture_photo`, `capture_chat`, `lookup`; `manage_plan`,
`manage_apply`, `repair_pairings_plan`; `sync_status`, `sync_rebuild_vault`, `sync_rebuild_records`,
`sync_normalize`; `record_update`, `record_delete`. The record-mutating ones push
a fresh items snapshot before signalling done, so the PWA reflects the change
immediately rather than after the next timer tick.

### CouchDB

A service in `qnap/docker-compose.yml` alongside the worker — stock
`couchdb:3` image (pinned to the same major version
`docker-compose.dev.yml` tests against), no custom build. LAN-only — never
internet-exposed, and unreachable from the relay (which is why the items
cache exists at all). The worker reaches it by service name
(`http://taster-couchdb:5984`) over `taster-internal`, a bridge network
private to the Application (qnet can't carry container-to-container
traffic on this NAS — see the compose file's networking note), and
LiveSync-enabled Obsidian clients reach it directly on the LAN via its
static qnet IP.

Its actual data directory is bind-mounted onto the NAS share
(`/share/Container/taster/couchdb/data`), not a Docker-managed named
volume — so it lives inside the same folder tree as the rest of the app and
QNAP's backup tools can capture it directly (see `INSTALL.md` → Backups).

Because CouchDB and the worker start together on first boot, the worker
service declares `depends_on: taster-couchdb: condition: service_healthy`
against a TCP-level healthcheck — otherwise the worker's
`ensure_db_and_indexes()` (no retry loop) can lose the startup race and
fail before CouchDB is accepting connections. Verified locally: bringing
both up together, Compose reports `couchdb: Waiting → Healthy` before the
worker container even starts.

(`docker-compose.dev.yml` at the repo root *also* runs a CouchDB container,
alongside a local relay and worker — see Deployment below.)

**Document format (spike resolved)**: `couchdb_client.py` writes each note
twice. The plain JSON document (frontmatter fields flattened to the top
level, plus a `markdown` field) under the `{type}:{slug}:{date}` id is what
every worker query runs against — items snapshots, `/lookup`,
repeat-detection, pairing resolution. The Self-hosted LiveSync plugin
ignores that shape (confirmed live: "Skipped unexpected non-note
document"), so `write_note()` additionally writes a LiveSync-format
projection — a content-addressed `h:…` leaf chunk holding the markdown and
an entry document keyed by the lowercased vault path — reverse-engineered
from documents a live LiveSync v0.25 client (E2EE off) wrote to this same
database. The capture write path is append-only, but the projection is no
longer strictly one-way: `reconcile.py` runs a periodic **reverse-sync**
pass (default every 30s, `RECONCILE_INTERVAL_S`) that watches CouchDB's
`_changes` feed for a human's Obsidian edit to a vault file, reassembles the
markdown from its chunks, parses it back to structured fields
(`markdown_parse.py`, the inverse of `render_markdown`), and folds the change
into the matching JSON doc in place. The match key is a stable `uid` stamped
on each note at capture and carried in the frontmatter, so an edit that
*renames* a note (which changes the derived `_id`) still updates the right
record. Reverse-sync only ever writes the JSON doc — never the LiveSync
chunk/entry — so the two directions can't fight over the same document, and
it is idempotent (an unchanged file is a no-op, so the worker's own writes
don't feed back). Since LiveSync's own documents share the `tastings` db,
every worker query constrains `type` to the registry's note types (`categories.
NOTE_TYPES`) so chunks and file entries never leak into results.

### Obsidian (client-side)

LiveSync plugin on phone + laptop, pointed at the CouchDB instance.
Intended vault structure:

```
Tastings/
  Whisky/
  Cigars/
  Coffee/
  Pipe Tobacco/
  Beers/
  Chocolate/
  Pairings/
```

This layout is implemented by `write_note()`'s LiveSync projection: each
capture lands at `Tastings/<Folder>/<Name> (<date>).md` (filename
sanitized; pairings use the `a+b` slug from the doc id).

Dataview plugin for offline structured queries — no network or LLM
dependency once synced.

### Claude API usage

Three call sites, all in `backend/app/`, all using `claude-opus-4-8` by
default (configurable in `config.yaml`), all invoked from `worker.py`'s
job dispatch rather than an HTTP route handler:

- **Capture** (`capture_service.py`) — `thinking: {"type": "adaptive"}`,
  `output_config.format` (structured JSON schema), `tools`: `query_notes`
  always, `web_search_20260209` on photo captures only. Handles
  `stop_reason` of `tool_use` (execute `query_notes`, feed result back),
  `pause_turn` (resend to resume a server-tool loop), and `refusal`.
- **Lookup** (`lookup_service.py`) — same shape minus structured output
  (the answer is conversational text, not a schema-bound document), plus
  optional image input for shop mode.
- **Maintenance plan** (`manage_service.py`) — reads the whole vault and
  proposes structured per-record edits (`query_notes` + `web_search` to fill
  facts like a country of origin), writing nothing. It gets a much larger
  `max_tokens` (`max_tokens_manage`, default 16384) because one plan covers many
  records and reasoning tokens are spent from the same budget — under-budgeting
  truncates the JSON. `max_tokens`/`refusal` stop reasons surface as clear
  errors. The apply step is deterministic (no model call).

All of these are manual tool-loops (not the SDK's beta tool runner) so there's
one pattern to reason about across every call site (capture, lookup, the
maintenance plan, and the regenerate-pairings plan share `_run_plan_loop`'s
shape).

**Model selection & the alternate providers**: the PWA's Admin tab can override
the model per role (capture/lookup); the relay attaches that choice to
every job the worker claims, so it applies from the next job with no
restart (precedence: admin panel > `config.yaml`). The worker routes by id, in
one table (`providers.py`) that all four call sites share — capture, lookup, the
maintenance plan, regenerate-pairings:

- `claude-*` → Anthropic, inline in each service.
- `gpt-*` → `openai_provider.py` (a Responses-API mirror: vision, server-side
  web search, `query_notes` function tool, JSON-schema-nudged output, same
  Pydantic enforcement).
- `mistral-*`/`pixtral-*` → `mistral_provider.py` (vision + tools + structured
  output, with **web search via Mistral's Agents/Conversations API** — the
  built-in `web_search` connector runs server-side alongside `query_notes`,
  agentless so there's no portal agent to create; it **auto-falls-back to
  Mistral's chat API without web search** if the Conversations call fails, so
  capture/lookup never hard-fails on the newer API).
- anything namespaced `vendor/model` (e.g. `google/gemini-3.6-flash`) →
  `openrouter_provider.py`. OpenRouter is a router, not a lab: one key reaches
  Google, xAI, Moonshot and Qwen, which is why it's here at all — the models
  already covered by a direct path are deliberately not offered through it. It
  speaks OpenAI-compatible **chat completions**, so this path reuses the `openai`
  SDK against a different `base_url` and adds no dependency. Web search is
  OpenRouter's server-side `web` plugin (native engine where the underlying
  model has one, Exa otherwise), capped by the same `web_search_max_uses`
  budget and billed **per result** on top of tokens.

**Token accounting**: every model call, on every provider path, books its tokens
into `app/usage.py` (WRK-10) — per API call rather than per job, so a job that
dies mid-tool-loop still records what it already spent. Those counts leave the
worker twice: as a daily rollup line in its own log (emitted at midnight, and
for the partial day on shutdown), and as **deltas pushed to the relay**, which
sums them into a `usage_daily` table on its Fly volume and serves them at
`GET /admin/usage` for the Admin tab's Token usage card. The relay's copy is the
durable one — worker restarts and log rotation don't touch it. Two details make
the arithmetic trustworthy: the day key is the **worker's** local date, stored
verbatim so neither side re-derives it, and each push carries a report id that
survives retries, so a batch the relay applied but couldn't acknowledge is
ignored the second time instead of double-counted.

Each alternate needs its own key in the worker's `.env` (`OPENAI_API_KEY`,
`MISTRAL_API_KEY`, `OPENROUTER_API_KEY`); a model selected without its key fails
the job with a clear message. All three alternate paths are implemented but
**unverified against their live APIs** (no keys were available in dev — the same
status the Claude path had before its first real call). The OpenRouter *model
ids* in the catalog are live-verified, though: its `/api/v1/models` endpoint
needs no key, so vision/tools/pricing were read from it rather than from docs.

## Data model

One document per tasting note. **Capture** is append-only — it always creates a
new document, never updates an existing one — which sidesteps CouchDB `_rev`
conflict handling for the capture path entirely. Three later additions *do*
update in place, as sanctioned exceptions: reverse-sync (`reconcile.py`, folding
an Obsidian edit back), single-record edit/delete (`record_service.py`), and AI
maintenance apply (`manage_service.py`). Each resolves its target by the stable
`uid` first, and re-validates through the schema before writing.

`_id` scheme: `{type}:{slug}:{date}`, e.g. `whisky:glenfarclas-15:2026-07-17`.
**Coffee** is rated per **(bean, brew method)** couple — the same bean is a
separate entry per method (espresso, V60, …) with its own stars, since a bean
can shine as espresso yet fall flat over a pour-over. So a coffee's `brew_method`
is folded into both its id (`coffee:{slug}:{method}:{date}`) and its vault
filename; a coffee logged with no method keeps the plain `coffee:{slug}:{date}`.

Ratings are **1.0–5.0 with one decimal of precision** (a slider in the
PWA; chat captures like "four and a half stars" extract to 4.5). Enforced
and rounded to one decimal in `schema.py`; only valid on `status: tasted`.
Because of that last rule, **rating a `to-try` entry is the transition from
recommendation to tasting**: the item detail modal's "Rate it" control writes
`rating` + `status: tasted` + today's `date` in one `record_update` (a rating
alone would be rejected, and the old date is the day it was recommended, not
the day it was drunk). Re-rating an already-tasted record touches the rating
only.

Shared base (`schema.py` `BaseNote`, on every **item** note):
`schema_version`, `uid` (see below), `status`, `name`, `producer`,
`country_of_origin`, `rating`, `date`, `created`/`updated`, `price_sek`,
`stock`, `source`, `recommended_by`, `tags`, `pairings_suggested`,
`cocktail_pairings` (cigar/pipe-only), `notes`, `common_notes`.

- **`uid`** — a stable logical id stamped at capture and carried in the
  frontmatter, independent of the name/date-derived `_id`. It's what lets
  reverse-sync and AI maintenance find the right record even after an Obsidian
  rename changes the `_id`.
- **`country_of_origin`** — mandatory on every item type (`"unknown"` when
  genuinely undeterminable); distinct from the finer per-type location fields
  (`whisky.region`, `coffee.origin`), which stay alongside it.
- **`stock`** — integer, "how many I have at home" (default `0`); drives the
  Search "In stock" filter and is editable in the PWA or via AI maintenance.

| Type | Fields beyond the shared base |
|---|---|
| `whisky` | `category`, `region`, `peated`, `cask`, `age_years` |
| `cigar` | `wrapper`, `vitola`, `strength` |
| `coffee` | `brew_method` (see below), `roaster`, `origin`, `process`, `roast_level`, plus espresso dial-in: `grind_size` (free text, e.g. "medium-fine" or "metal filter"), `dose_g`, `brew_time_s`, `grinder`, `machine` — all captured only when stated, default empty (no assumed rig) and editable in the PWA |
| `pipe` | `blend_type`, `cut`, `components` (list of leaf, e.g. Virginia/Latakia/Perique), `strength`, `room_note`, `tin_date` |
| `beer` | `style`, `abv`, `ibu`, `serving` |
| `chocolate` | `chocolate_type` (dark/milk/white/ruby), `cacao_percent`, `cacao_origin` (bean origin, distinct from `country_of_origin`), `form` (bar/truffle/…) — pairs as a **companion** (with drinks) |
| `pairing` | its own document type (not an item, no `country_of_origin`/`stock`) — `items` (exactly two note `_id`s), `rating`, `date`, `tags`, `notes` |

**Pairings are cross-category.** A pairing is always one **companion** — a thing
you savor (cigar, pipe, chocolate) — with one **drink** (whisky, coffee, beer, rakı),
never same-side (so cigar↔chocolate, both companions, is not a pairing). The
sides live in `schema.PAIR_GROUP_BY_TYPE` (re-exported by the registry), and
`PairingNote`'s validator rejects a same-side pair (it reads each item's type
from its id prefix; unresolved/legacy ids pass rather than block). The AI pairing
*suggestions* on each item note (`pairings_suggested`) follow the same rule and
use the shape **`{profile, matches[], reason}`**: an ideal archetype ("a
sherry-cask matured 10+ yo single malt") plus 0–2 concrete vault items from the
opposite side that fit it (empty when nothing owned fits — the profile alone is
the recommendation). Shown in the PWA's item-detail view, owned matches
clickable.

**Cigar and pipe also carry `cocktail_pairings`** — 1–2 of the most common
*classical* cocktails (Old Fashioned, Manhattan, Negroni, Sazerac, …) as
**`{name, reason}`**, name-and-why only (a cocktail isn't vault inventory, so
there's no owned-match resolution). Restricted to cigar/pipe: a cocktail
accompanies a smoke, so `BaseNote`'s `_cocktails_smoke_only` validator drops the
field on any other item (chocolate and the drinks). Captured alongside the drink
pairings and regenerated by the same **Regenerate pairings** flow; shown as its
own "Classic cocktails" block in item detail.

Full field-by-field schema and rationale: see `tasting-log-design.md` §5, and
the authoritative types in `backend/app/schema.py`.

### Categories (the registry)

The list of categories is **not** hardcoded across the codebase — it lives once
in `backend/app/categories.py`. That registry pairs each `type` with its
Pydantic model, its display label, and its ordered edit-form fields (with render
kinds). Everything that needs "the set of note types" derives from it: the
CouchDB `$in` selectors (`items_query`, `sync_service`, `tools`), the
capture/manage JSON-schema enums, reverse-sync's type constraint, and — via
`categories_metadata()` pushed to the relay and served at `/categories` — the
**PWA's group headings and edit forms**. The relay stores nothing categorical of
its own (its `type` filter is free-form); the PWA carries a baked fallback only
for the cold-start window before the worker's first push.

Adding a category is therefore: define its model in `schema.py` (and add it to
the `_ITEM_MODELS` tuple, from which the discriminated unions and `parse_any_note`
derive), add its fields to `capture_json_schema.py` + a hint in the capture
prompt, and add one line to `CATEGORIES`. No relay or PWA change. Pipe tobacco
was the first category added this way.

## API surface

### Relay (PWA-facing, public internet, bearer = `TASTER_API_KEY`)

| Endpoint | Method | Purpose |
|---|---|---|
| `/capture/photo` | POST | image + optional stars/note → `202` + `capture_id`; enqueued for the worker |
| `/capture/chat` | POST | free-text → `202` + `capture_id`; enqueued (tasted, to-try, or a pairing report) |
| `/capture/{capture_id}` | GET | job status: `pending`/`processing` \| `done` (note + any prior-tasting match) \| `failed` |
| `/lookup` | POST | free-text question, optional image (shop mode) → `202` + `lookup_id`; enqueued |
| `/lookup/{lookup_id}` | GET | job status \| `done` (`answer`) \| `failed` |
| `/items` | GET | filtered list (`type` — free-form, `status`, `min_rating`, `tag`) against the cached snapshot — no LLM, no CouchDB access, Dataview-free fallback |
| `/categories` | GET | category registry metadata (group order/labels + edit-form field specs) the PWA builds its groups + edit forms from |
| `/manage` | POST | AI maintenance: `instruction` → `202` + `manage_id` (plan job, writes nothing) |
| `/manage/repair-pairings` | POST | regenerate every item's cross-category pairings → `202` + `manage_id` (a plan; reviewed + applied via `/manage/apply`) |
| `/manage/apply` | POST | apply the approved subset of a plan (regular edits or regenerated pairings) → `202` + `manage_id` |
| `/manage/{manage_id}` | GET | plan/apply job status \| `done` (`summary`+`changes`, or apply `results`) \| `failed` |
| `/sync` | POST | forced full-vault sync (`status`/`rebuild_vault`/`rebuild_records`/`normalize`) → `202` + `sync_id` |
| `/sync/{sync_id}` | GET | sync job status \| `done` (counts + the delta behind them: `records_without_file`, `files_without_record`, `colliding_records`) \| `failed` |
| `/record/update`, `/record/delete` | POST | single-record edit/delete → `202` + `record_id` |
| `/record/{record_id}` | GET | record job status \| `done` \| `failed` |
| `/admin/models` | GET | the curated model catalog (id, label, provider, relative € cost) |
| `/admin/settings` | GET/PUT | capture/lookup model choice; `null` clears back to the worker's config.yaml default. Applies from the next claimed job |
| `/admin/jobs` | GET | recent jobs (id/type/status/timestamps/error — never the payload, which can carry image data) |
| `/admin/status` | GET | items count, snapshot age, job counts by status |
| `/health` | GET | liveness check, no auth |

### Relay (worker-facing, bearer = `WORKER_API_KEY` — a different secret)

| Endpoint | Method | Purpose |
|---|---|---|
| `/worker/jobs/next` | GET | atomically claim the oldest pending (or stale-processing) job |
| `/worker/jobs/{id}/result` | POST | report a job done (with result) or failed (with error) |
| `/worker/items/snapshot` | POST | replace the cached items list (`/items`) and the cached category metadata (`/categories`) in one push |

All PWA-facing endpoints are also rate-limited (`relay/app/rate_limit.py`).

## Configuration

Split across the two deployables, by who needs what:

- **`backend/config.yaml`** — model choice (`capture_model`, `lookup_model`),
  effort level, token ceilings, web search `max_uses`, tool-loop iteration
  cap. Not a secret; meant to be hand-edited to tune behavior or swap models
  without touching application code. On the QNAP deployment specifically,
  it's baked into the worker image (see Deployment below) rather than
  bind-mounted, so a change still requires `./qnap/deploy.sh` to take
  effect — not a code change, but not a live edit-in-place either.
- **`backend/.env`** (from `.env.example`, never committed) — Anthropic API
  key, CouchDB credentials/URL, `RELAY_URL`, `WORKER_API_KEY`, poll/snapshot
  intervals. No PWA-facing bearer token or CORS/rate-limit settings here
  anymore — the worker accepts no inbound connections at all.
- **`relay/.env.example`** / Fly secrets — `TASTER_API_KEY` (PWA-facing),
  `WORKER_API_KEY` (must match the worker's value exactly), `CORS_ALLOW_ORIGIN`
  (defaults to the deployed origin, not `*`), `JOB_CLAIM_TIMEOUT_S`,
  `JOB_RETENTION_DAYS` (terminal-job prune window, default 7).

## Security summary

See `tasting-log-design.md` §8 for the original threat-model writeup
(written before the relay/worker split, but the reasoning still applies).
In brief: bearer token only (never a query string), constant-time
comparison, CouchDB LAN-only with a least-privilege member user, no delete
endpoint, per-IP rate limiting plus `max_uses`/`max_tokens` bounds on
Claude calls to cap the blast radius of a leaked key, non-root Docker
containers, and a deliberate choice *not* to enable LiveSync end-to-end
encryption (it would make documents unreadable to the worker, which is
what `/lookup` and the items snapshot depend on).

**New with the relay/worker split**: the PWA-facing (`TASTER_API_KEY`) and
worker-facing (`WORKER_API_KEY`) secrets are deliberately separate. A
leaked PWA key (client-side, sits in `localStorage`, the more exposed of
the two) lets an attacker enqueue jobs and poll results — annoying and a
Claude-spend risk, bounded by rate limiting and per-call token limits — but
never lets them claim or complete jobs, or overwrite the items cache,
since those require the worker-only key. The worker itself now has **zero**
inbound attack surface: it never listens on a port, so there is nothing on
the QNAP for anything on the internet to connect to at all.

**On the "nothing is written without approval" property**: the plan → approve
→ apply split is a *UX* gate, not a server-enforced safety boundary.
`POST /manage/apply` applies whatever `changes[]` array the client sends,
independent of any plan the worker actually produced — so a script holding the
PWA key can apply arbitrary edits without ever running a plan. What *is*
enforced server-side is that every applied change re-validates through the
Pydantic schema (`parse_any_note`), so the maintenance path can never write a
document the capture path wouldn't accept. That's the real guarantee; the
approval step protects the honest user from a bad AI plan, not the account from
a leaked key (rate-limiting + per-call token caps bound that, as above).

**Request-size / retention bounds** (relay): `/capture/photo` rejects images
over 10 MB before reading the whole body (the small relay VM would otherwise
spike on a runaway upload); a finished job's base64 payload is cleared
immediately, and terminal jobs are pruned after `JOB_RETENTION_DAYS` (default
7) so the SQLite file on the Fly volume can't grow without bound. CORS defaults
to the deployed origin rather than `*` — the PWA is same-origin so needs no
grant, and this keeps a leaked key from being driven from an arbitrary
website's JavaScript.

## Deployment

Four separate compose/config files, each with a distinct job:

| File | Runs where | Purpose |
|---|---|---|
| `docker-compose.dev.yml` | Your Mac | Local verify loop — CouchDB + relay + worker together on a plain bridge network, so a full capture/lookup can be driven end to end via `http://localhost:8000` without touching Fly or a NAS. Not used in production. |
| `relay/fly.toml` | Fly.io | The relay — PWA + job queue, the one piece actually exposed to the public internet. |
| `qnap/docker-compose.yml` | The QNAP, via Container Station | CouchDB (stock image, pulled normally) + the worker, both under `/share/Container/taster/`. **No `build:` for the worker** — that image is cross-built on Mac for the NAS's actual CPU architecture and loaded via `docker load`, not built on the NAS. |
| `qnap/deploy.sh` | Your Mac → the NAS | Builds the worker image for the NAS's architecture and streams it over (see below). |

**The worker image never carries source code onto the NAS, and nothing
uses rsync or scp — neither works against this NAS's SSH server.**
`qnap/deploy.sh` cross-builds it with `docker buildx build --platform <NAS
arch> --load`, then streams it directly into `docker load` on the NAS over
one SSH pipe: `docker save "$tag" | gzip | ssh -p "$NAS_SSH_PORT"
"$NAS_HOST" "gunzip -c | '$NAS_DOCKER_BIN' load"` — no intermediate tar
file on either end, just plain SSH command execution with stdin piped
through (works with any SSH server, since it doesn't depend on the
SCP/SFTP subsystem). The absolute `$NAS_DOCKER_BIN` path is required, not
cosmetic — Container Station only puts `docker` on `PATH` for interactive
logins, not `ssh host "command"`. The only things that ever reach the NAS
filesystem directly are the compose file itself (streamed the same way,
via `ssh host "cat > path" < localfile`) and a handful of small
`.env`/secrets files.

Verified end-to-end locally, not assumed: cross-built the worker for
`linux/amd64` on an arm64 Mac, confirmed the architecture via `docker image
inspect`, piped it through the full `save | gzip | gunzip | load` chain
with zero intermediate files, and ran the reloaded image under emulation.
Separately, brought up the full CouchDB + relay + worker stack locally
(`docker-compose.dev.yml`) and drove a real job through the entire chain —
enqueue via the relay's PWA-facing API, worker claims it, calls the real
Anthropic API (failed only on auth, since no real key was available in
that environment — everything up to and including the live API call
worked), posts the result back, and the PWA-facing poll correctly surfaced
the failure. Confirmed the job-level status (relay: did the worker finish
processing) is correctly distinct from the application-level status
(`capture_service`: did the capture itself succeed) — a job can be relay-
`"done"` while its embedded result is `"failed"`, and the route merge logic
surfaces the inner status correctly.

Getting the target architecture wrong doesn't degrade gracefully — it's a
silent `exec format error` at container start. `deploy.sh` requires
`NAS_PLATFORM` to be set explicitly (no default) for exactly this reason;
see `INSTALL.md` step 0.

See `INSTALL.md` for the full walkthrough.

## Known placeholders

- `qnap/docker-compose.yml`'s static IP for CouchDB (`10.0.0.2` on
  `qnet-static-eth1-dc7e3a`, the address LiveSync clients sync against) —
  it's a placeholder; pick a free address on your own LAN subnet before
  applying. The worker has **no** LAN IP:
  container-to-container traffic over qnet turned out not to work on this
  NAS (no embedded DNS, and direct-IP connections between qnet containers
  failed too), so worker↔CouchDB runs over `taster-internal`, a plain
  bridge network private to the Application, and the worker's outbound
  internet (the Fly relay) goes through that bridge's NAT.
- `relay/fly.toml`'s `app = "your-taster-relay"` — change it to whatever
  name `fly apps create` accepts for you (the deployed URL follows the app
  name: `https://<app>.fly.dev`).

Confirmed (not placeholders, but environment-specific facts baked into
`qnap/deploy.sh`'s defaults): `NAS_PLATFORM=linux/amd64` (`uname -m` →
`x86_64` on `nas.local`); `docker` lives at
`/share/CACHEDEV1_DATA/.qpkg/container-station/bin/docker` and is only on
`PATH` for interactive logins, not `ssh host "command"` — hence every
remote invocation in `deploy.sh`/`INSTALL.md` uses that absolute path
explicitly; there's no standalone `docker-compose` binary, only `docker
compose` (v2.29.1-qnap2) as a plugin subcommand.

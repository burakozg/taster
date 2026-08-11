# Tasting Log — Design & Architecture

> **⚠️ Historical document — partially superseded.** This is the original
> design doc and decision log, kept for rationale ("why is it shaped this
> way?"). For how the system is *actually built today*, read
> [`ARCHITECTURE.md`](./ARCHITECTURE.md). The largest divergence: the
> direct-to-backend design described in §3/§4.2 (an inbound FastAPI server
> on the QNAP) was replaced by a Fly.io relay + outbound-polling worker
> split, so the QNAP accepts no inbound connections at all. Smaller ones:
> the implementation uses a manual tool loop (not the SDK tool runner
> mentioned in §4.5) and validates structured output with its own Pydantic
> layer (not `client.messages.parse()`).

## 1. Purpose

A personal system for logging whisky, cigar, and coffee tastings with minimal friction, and for recalling them naturally — including standing in a shop deciding what to buy.

Two capture modes (photo of label, or conversational), one enrichment step (Claude extracts + fills gaps via web search, in a single call), one source of truth (Obsidian vault, synced via self-hosted CouchDB), two lookup modes (offline Dataview queries, conversational chat).

## 2. Non-goals

- Not a general note-taking app — scoped to tastings only.
- Not multi-user — single owner, no auth beyond a shared API key.
- Not trying to replace Vivino-style social features — no ratings sharing, no community data.
- Not a cellar/inventory manager — no open/finished bottle state, stock counts, or humidor tracking. Notes are append-only records of experiences, not mutable state.

## 3. High-level architecture

```
┌─────────────┐     ┌─────────────┐
│ Photo capture│     │ Chat entry  │
│ (PWA)        │     │ (PWA)       │
└──────┬───────┘     └──────┬──────┘
       │                    │
       └─────────┬──────────┘
                  ▼
         ┌─────────────────────┐
         │  FastAPI backend     │ ── 202 Accepted returned
         │  (QNAP, Docker)      │    immediately to the PWA
         └────────┬─────────────┘
                  │ background task
                  ▼
         ┌─────────────────────┐
         │  Claude API          │  single Messages call:
         │  vision + web search │  image block + web_search
         │  + structured output │  server tool + JSON schema
         └────────┬─────────────┘
                  ▼
     Structured note ───► Written via CouchDB API
                                 │
                                 ▼
                        Obsidian (LiveSync plugin)
                        on phone / laptop
                                 │
                    ┌────────────┴────────────┐
                    ▼                          ▼
             Dataview queries          Chat lookup
             (offline, instant)   (backend reads CouchDB
                                   directly via a query
                                   tool, no Obsidian
                                   dependency)
```

## 4. Components

### 4.1 Capture PWA
- Single-page mobile-first web app, no login screen — gated by a shared API key stored in local device storage after first setup.
- Two primary actions:
  - **Photo + stars**: camera capture, 1–5 star tap, optional short text note.
  - **Chat**: free-text or voice-to-text input box, sent as-is to the backend.
- Third action: **lookup** — a chat box for "have I liked any Speyside whiskies" type queries, with an optional **photo input** ("shop mode"): snap a bottle or shelf in the shop and get back whether you've tasted it, how you rated it, and similar items you've liked. Also a simple filtered list view as a Dataview-free fallback for when Obsidian isn't open on that device.
- Capture is **fire-and-forget**: the backend acks with `202 Accepted` before enrichment runs, so the shop-counter interaction is "snap, tap stars, done" regardless of Claude latency. The PWA can poll `/capture/{id}` for the finished note or just trust it.
- Hosted on a small PaaS (Fly.io / Render) for a stable HTTPS URL — not on the QNAP, so capture works with zero dependency on home network reachability.

### 4.2 Backend (FastAPI)
Runs as a Docker container on the QNAP, same macvlan pattern as the calendar and digest agents.

Responsibilities:
- Receives capture payloads (image + stars + optional text, or free-text chat), validates the API key, writes a pending capture record, and returns `202` immediately.
- In a background task, makes **one Claude API call per capture** (model `claude-opus-4-8`, adaptive thinking):
  - **Photo captures**: the request carries the label image as an image content block *and* declares the server-side web search tool (`web_search_20260209`, `max_uses: 3`). Claude extracts name, producer, category, and category-specific fields from the label and fills gaps not printed on it (cask finish/maturation for whisky, roast process/origin for coffee, wrapper/strength for cigars) via search — all server-side, in the same turn. No separate vision-then-search pipeline.
  - **Chat captures**: same call shape minus the image; extracts the same fields from natural language, including distinguishing `tasted` vs `to-try` (recommendations) — and recognizing **pairing reports** ("had the Glenfarclas with the Padrón, great match"), which produce a `pairing` document (§5) instead of an item note.
  - The capture call also gets the read-only `query_notes` tool (§4.5), used for two things: **vault-grounded pairing suggestions** (`pairings_suggested` references items you've actually tasted and rated, not generic advice) and **repeat detection** — if the same name+producer already exists, the capture still writes a new note (re-tasting is legitimate) but the `/capture/{id}` result surfaces it: "previously tasted 2026-03-02, rated 4".
  - Output is constrained with **structured outputs** — a Pydantic `TastingNote` model via `client.messages.parse()` (discriminated by `type` for the per-category fields). The backend gets schema-valid JSON, never parses free-form model text.
  - Handles `stop_reason == "pause_turn"` (server-tool loops can pause; re-send the assistant turn to resume, capped at a few continuations).
- Renders the markdown note + YAML frontmatter itself from the validated JSON (deterministic, testable) and writes it into CouchDB via its HTTP API (one document per note, see §5).
- Serves the chat lookup endpoint — see §4.5. Reads directly from CouchDB (not through Obsidian), so lookups don't depend on any device having Obsidian open.

### 4.3 CouchDB
- Docker container on the QNAP, static IP, matches existing homelab pattern.
- Acts as the sync backend for the **Self-hosted LiveSync** Obsidian plugin.
- Single database, one document per tasting note.
- **LAN-only.** Not exposed to the internet; the PWA never talks to CouchDB — only the backend and LiveSync clients on the LAN do. Remote LiveSync sync, if ever needed, goes through a VPN (WireGuard/Tailscale) rather than exposing the port.
- Backend uses a **dedicated CouchDB user** with member (read/write) access to the tastings database only — not the admin account.

### 4.4 Obsidian (client-side, human-facing view)
- LiveSync plugin installed on phone + laptop, pointed at the CouchDB instance.
- Vault structure:
  ```
  Tastings/
    Whisky/
    Cigars/
    Coffee/
    Pairings/
  ```
- Item notes carry `[[wikilinks]]` to paired items in the body, so pairings are click-navigable in Obsidian; a Dataview snippet on each item's saved view lists "pairings involving this item".
- Dataview plugin installed for offline structured queries — no LLM or network dependency once synced.

### 4.5 Claude API usage
- **Capture (photo or chat)**: one Messages call — image block (photo only) + `web_search_20260209` server tool + structured output schema. See §4.2.
- **Lookup**: tool-driven retrieval, not stuff-the-vault-in-context. The backend exposes a `query_notes` tool to Claude (filters: `type`, `region`, `min_rating`, `status`, `tags`, free-text match) implemented as a CouchDB Mango query. The SDK tool runner (`@beta_tool` + `client.beta.messages.tool_runner`) drives the loop: Claude translates "have I liked any Speyside whiskies" into a structured query, only matching notes enter context, and Claude answers in natural language over them. Scales past the point where the whole vault fits in context. The tool is **read-only** — lookup can never mutate the vault.
  - **Shop mode**: `/lookup` accepts an optional image. Claude identifies the item from the photo (same vision capability as capture, no web search needed), then queries the vault — "you tasted this in 2025, rated 3, but loved these two similar Speysides." Same call shape as text lookup plus an image block.
  - **Pairing queries**: "what should I smoke with this bottle?" — answered from `pairing` documents (tried, rated) first, `pairings_suggested` fields second, clearly distinguishing the two.
- **Prompt caching**: the shared system prompt (schema description, extraction rules) carries `cache_control: {"type": "ephemeral"}`. Note the minimum cacheable prefix on Opus 4.8 is 4096 tokens — below that, caching silently no-ops, which is fine.
- **Spend bounds**: `max_uses` on web search, `max_tokens` sized per endpoint, and a monthly spend limit configured on the Anthropic workspace as a backstop against a leaked capture key (see §8).

## 5. Data model

One markdown file per item, frontmatter-driven so Dataview can query it directly.

**Document identity and versioning:**
- `_id`: `{type}:{slug}:{date}` — e.g. `whisky:glenfarclas-15:2026-07-17`. Type-prefixed and sortable; tasting the same bottle on a different day is naturally a new document.
- **Append-only**: captures always create a new document, never update an existing one. This sidesteps CouchDB `_rev` conflict resolution entirely for the write path; LiveSync conflicts can then only arise from human edits in Obsidian, which LiveSync's own conflict UI handles.
- `schema_version` in every document, so future frontmatter changes can be migrated (or handled) deliberately.
- `created` / `updated` ISO timestamps alongside the tasting `date`.

```yaml
---
schema_version: 1
type: whisky            # whisky | cigar | coffee
status: tasted           # tasted | to-try
name: Glenfarclas 15
producer: Glenfarclas
category: single malt     # single malt | blend | bourbon | ... (per type)
region: Speyside
rating: 4                 # 1-5, omitted if status: to-try
peated: false
cask: sherry oak
age_years: 15
date: 2026-07-17
created: 2026-07-17T18:42:00Z
updated: 2026-07-17T18:42:00Z
price_sek: 650
source: photo             # photo | chat — how the entry was captured
recommended_by:           # only for status: to-try
tags: [sherry, nutty]
pairings_suggested:       # written once at capture by the enrichment call; never updated
  - item: "cigar:padron-1964:2026-03-02"   # vault match (via query_notes)
    reason: "sherry sweetness vs maduro earthiness"
  - name: "medium-roast Ethiopian"          # generic suggestion, no vault match
    reason: "bright acidity cuts the nuttiness"
---
Tasting notes in free text — whatever was said or extracted.
```

Cigar and coffee types reuse `type`, `status`, `name`, `producer`, `rating`, `date`, `price_sek`, `source`, `tags`, `pairings_suggested`, plus type-specific fields:
- Cigar: `wrapper`, `vitola`, `strength`, `origin_country`
- Coffee: `roaster`, `origin`, `process`, `roast_level`, plus **espresso dial-in fields** — the dialed-in recipe for the bean:
  - `grind_size` — free-text grind description ("medium-fine", "coarse", a number, or "metal filter" when the bean was ground elsewhere); text, not a weight. Replaced the older grinder-specific `grind_setting`; Normalize folds any legacy `grind_setting` value into this.
  - `dose_g` — ground coffee in the basket
  - `brew_time_s` — shot time (optional; lever shots on the La Pavoni vary, still useful as a reference)
  - `grinder` / `machine` — which grinder and brewer this recipe is for. Captured from the entry only when stated and left empty otherwise — the backend does **not** assume a default rig, so a pour-over or a bean brewed on borrowed gear never gets a phantom espresso setup. Both are editable in the PWA.

  Dial-in is iterative, but the *note* records the settled recipe, not every test shot: capture the coffee when first tasted, then refine `grind_size`/`dose_g` by editing the note in Obsidian (human edits are the sanctioned exception to append-only, §5) or via a chat re-capture ("dialed in the Ethiopian medium-fine, 16g in") once the recipe lands. Chat extraction parses these fields from natural language like any other.

**Pairing documents** — a tried pairing is an *experience*, not an attribute: it happens on a date, has its own rating, and belongs to two items at once. Making it its own document type keeps item notes append-only (no updates when a pairing is later tried) and avoids arbitrarily picking which item "owns" it. Captured through the normal chat endpoint — "had X with Y, 5 stars" parses into this; no dedicated UI.

```yaml
---
schema_version: 1
type: pairing             # _id: pairing:{slug}+{slug}:{date}
items: ["whisky:glenfarclas-15:2026-07-17", "cigar:padron-1964:2026-03-02"]
rating: 5
date: 2026-08-01
created: 2026-08-01T21:15:00Z
updated: 2026-08-01T21:15:00Z
source: chat
tags: [evening]
---
The sherry sweetness stood up to the maduro perfectly. [[Glenfarclas 15]] + [[Padrón 1964]]
```

Notes for Dataview queries: `rating` is absent on `to-try` items — saved queries that sort/filter on rating should filter `status = "tasted"` first. Pairing views filter `type = "pairing"` and match on `contains(items, ...)`.

## 6. API surface (backend)

| Endpoint | Method | Purpose |
|---|---|---|
| `/capture/photo` | POST | image + stars + optional note → `202` + capture id; note created async |
| `/capture/chat` | POST | free-text → `202` + capture id; note created async (tasted, to-try, or pairing) |
| `/capture/{id}` | GET | status of an async capture: `pending` \| `done` (with the note + any prior-tasting match) \| `failed` |
| `/lookup` | POST | free-text question, optional image (shop mode) → answer via the `query_notes` tool over CouchDB |
| `/items` | GET | simple filtered list (fallback UI, no LLM) |

All endpoints require a bearer token (shared API key) in the header — never in a query string, so it can't leak into logs. Capture and lookup endpoints are rate-limited at the app level (a small per-minute cap is generous for one human and starves an abuser).

## 7. Deployment

- `docker-compose.yml` on the QNAP with two services: `backend` (FastAPI) and `couchdb`, both on the existing macvlan network with static IPs, consistent with the calendar and digest agent deployments.
- Capture PWA deployed separately on Fly.io or Render — the only externally-facing piece, communicating with the QNAP backend's `/capture` and `/lookup` endpoints over HTTPS.
- Containers run as non-root; CouchDB data on a dedicated volume included in the QNAP backup rotation (the vault *is* the data — LiveSync replicas are sync copies, not backups).
- Secrets (Claude API key, CouchDB credentials, shared API key) via environment variables / `.env`, not committed.

## 8. Security

**Threat model:** single user, self-hosted, low-value data (tasting notes). The goals are: a leaked capture key must not become expensive or destructive, CouchDB must not be reachable from the internet, and untrusted web content must not corrupt the vault.

- **Shared API key**
  - Sent as a bearer header only; compared with a constant-time comparison server-side.
  - Stored in PWA local storage — accepted risk for this threat model. Rotation is cheap and should be exercised: change the env var, restart the backend, re-enter on devices. Rotate immediately on any suspicion of leakage.
  - Blast radius of a leak is bounded by: app-level rate limits (§6), `max_uses`/`max_tokens` on Claude calls, and a workspace spend cap on the Anthropic console. A leaked key can spam notes but cannot delete them (no delete endpoint) or run up unbounded API spend.
- **CouchDB**
  - LAN-only, never internet-exposed; remote sync via VPN if ever needed.
  - Backend uses a least-privilege member user scoped to the tastings database; admin credentials stay out of the backend's environment.
- **LiveSync end-to-end encryption: deliberately not enabled.** E2E encryption at the CouchDB layer would make documents unreadable to the backend, killing `/lookup` and `/items` — the two features that justify the backend reading CouchDB directly. With CouchDB LAN-only and credentialed, at-rest exposure is limited to someone already on the LAN with the DB password, which is outside this threat model. Revisit only if CouchDB is ever exposed beyond the LAN.
- **Untrusted web content (prompt injection).** Enrichment reads arbitrary web pages via the search tool; a hostile page could try to steer the model. Mitigations: structured outputs mean search results can only influence the *values of schema fields* on the note being created — there is no free-form action surface; the capture call has no tools besides web search; the lookup path's only tool is read-only. Worst case is a wrong `cask` value on one note, which the human sees in Obsidian.
- **PWA / transport**: HTTPS everywhere (PaaS-terminated for the PWA, reverse-proxied cert for the backend); CORS on the backend restricted to the PWA origin.
- **Claude API key** lives only in the backend's environment on the QNAP — never in the PWA or client-side code.

## 9. Build order (suggested milestones for Claude Code)

1. **Note schema + CouchDB write path** — a minimal script that writes a hand-crafted note (with the §5 `_id`/versioning scheme) into CouchDB and confirms it appears in Obsidian via LiveSync.
2. **FastAPI backend skeleton** — `/capture/chat` with the 202 + background-task shape: parse free text with Claude (structured outputs, including the `pairing` document type), write to CouchDB, `/capture/{id}` status.
3. **Add `/capture/photo`** — same call shape plus the image block and web search tool; extraction *and* enrichment land in this one milestone.
4. **Add `/lookup`** — `query_notes` tool + tool runner over CouchDB, answer in natural language; then reuse `query_notes` in the capture call for pairing suggestions + repeat detection (it's the same tool).
5. **Shop mode** — optional image on `/lookup`; identify from photo, then query the vault.
6. **Capture PWA** — camera + stars UI, chat box, lookup with photo input, calling the capture endpoints; rate limiting + CORS.
7. **Dataview queries** — starter queries for each category (with the `status = "tasted"` guard) plus per-item pairing views, added as saved views in the vault.
8. **Docker Compose + QNAP deployment** — wire it into the existing macvlan setup, non-root containers, backup volume.

## 10. Open questions

- Voice-to-text: handled client-side (browser API) or sent as audio to the backend? Recommend client-side to start — simpler, no extra API cost.
- Should `to-try` recommendations get their own lightweight capture flow (e.g. quick-add without a photo), or just go through the chat endpoint? Recommend starting with chat-only, since it's rare enough not to need a dedicated UI.
- Failed enrichments: should a capture that fails mid-enrichment still write a bare note from whatever was extracted (degraded but present), or stay `failed` for retry? Leaning toward writing the bare note — a note with gaps beats a lost tasting.

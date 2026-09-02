# Installation

A step-by-step walkthrough for standing up Tasting Log end to end: verify
the whole pipeline on your Mac, deploy the relay (PWA + job queue) to
Fly.io, build the worker image for the QNAP's actual CPU architecture and
stream only that image over (no source code, no rsync, no scp — just SSH
piping), bring up CouchDB + the worker as a QNAP Container Station
Application, and configure Obsidian LiveSync. See `ARCHITECTURE.md` for
what each piece does and why — in particular, why there's no reverse proxy
or DuckDNS anymore: the worker never accepts an inbound connection at all,
it only polls the relay outbound.

## Prerequisites

- Docker Desktop on your Mac (for the local verify step and cross-platform
  builds — it bundles buildx + QEMU emulation, no extra setup needed).
- A QNAP with Container Station, plus SSH access (the only supported way to
  move anything onto this NAS — no rsync, no scp, just plain SSH command
  execution with stdin piped through).
- An Anthropic API key.
- [`flyctl`](https://fly.io/docs/flyctl/install/) installed and logged in
  (`fly auth login`), for the relay deploy.
- Obsidian with the **Self-hosted LiveSync** community plugin installed on
  your phone and laptop.

## 0. Determine the NAS's CPU architecture and confirm `docker`'s path

If your Mac is Apple Silicon (check with `uname -m` → `arm64`), Docker
builds ARM images by default. An image built for the wrong architecture
doesn't partially work — it fails outright (`exec format error`) when the
container tries to start. Confirmed for this NAS (`nas.local`):

```bash
[admin@nas.local ~]$ uname -m
x86_64
```

→ `NAS_PLATFORM=linux/amd64`. Also confirm `docker`'s absolute path while
you're in there — Container Station only wires `docker` (and here, `docker
compose` as a v2 plugin subcommand rather than a standalone
`docker-compose` binary) onto `PATH` for *interactive* logins, not for the
one-off `ssh host "command"` invocations `./deploy` and this guide use
everywhere below:

```bash
[admin@nas.local ~]$ which docker
/share/CACHEDEV1_DATA/.qpkg/container-station/bin/docker
[admin@nas.local ~]$ /share/CACHEDEV1_DATA/.qpkg/container-station/bin/docker compose version
Docker Compose version v2.29.1-qnap2
```

Write these into a git-ignored `.deploy.env` at the repo root — copy
`deploy.env.example` and fill it in. `./deploy` auto-sources it, so they
never need re-exporting each session:

```bash
cp deploy.env.example .deploy.env && $EDITOR .deploy.env
```

```ini
NAS_SSH=admin@nas.local
NAS_PLATFORM=linux/amd64
NAS_SSH_PORT=44   # only if your SSH server isn't on the default port 22
NAS_DOCKER_BIN=/share/CACHEDEV1_DATA/.qpkg/container-station/bin/docker
```

The manual `ssh` commands below reference the same names, so source it in
this shell too, or you'll hit `sh: : command not found` (an empty command —
the variable silently expanding to nothing):

```bash
set -a; . ./.deploy.env; set +a
```

> `NAS_SSH`, not `NAS_HOST`. The name was ambiguous across the four homelab
> projects — ssh destination in some, a container's macvlan IP in others —
> and `./deploy` now rejects `NAS_HOST` outright rather than guess.

## 1. Verify the whole pipeline on your Mac first

`docker-compose.dev.yml` runs CouchDB, the relay, and the worker together
locally, so you can drive a real capture/lookup end to end before touching
Fly or the NAS:

```bash
cp backend/.env.example backend/.env
cp relay/.env.example relay/.env
```

Generate two secrets and fill them into both files (they must match
exactly — this is the worker↔relay auth, separate from the PWA's key):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # -> TASTER_API_KEY (relay/.env)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # -> WORKER_API_KEY (both files, must match)
```

In `backend/.env`: fill in `ANTHROPIC_API_KEY`, set `WORKER_API_KEY` to the
generated value, leave `RELAY_URL` — `docker-compose.dev.yml` overrides it
to `http://relay:8000` for you. **Change** `COUCHDB_USER`/`PASSWORD` to
`admin`/`adminpass` — the `.env.example` default (`taster_backend`, no
password) doesn't match anything `docker-compose.dev.yml` actually creates;
only `RELAY_URL` and `COUCHDB_URL` are overridden by the compose file, not
these two.

In `relay/.env`: fill in `TASTER_API_KEY` (any generated value) and
`WORKER_API_KEY` (must match `backend/.env`'s value).

```bash
docker compose -f docker-compose.dev.yml up -d --build
curl http://localhost:8000/health   # -> {"status":"ok"}
```

Open `http://localhost:8000` in a browser — that's the actual PWA, served
by the relay, talking to the local worker. Try a chat capture (it'll fail
at the real Claude API call only if `ANTHROPIC_API_KEY` isn't a real key —
everything else in the pipeline will have already worked correctly by that
point).

```bash
docker compose -f docker-compose.dev.yml down -v   # tear down when done
rm backend/.env relay/.env                          # don't leave test secrets lying around
```

This builds **native** (arm64, on Apple Silicon) images for local testing
— separate from the `linux/amd64` (or whatever `NAS_PLATFORM` is) worker
image built for the NAS in step 4. Neither `docker-compose.dev.yml` nor
the relay's local run here ever touches the NAS.

## 2. Deploy the relay to Fly.io

```bash
cd relay
fly launch --no-deploy   # interactive: picks/confirms a unique app name, rewrites fly.toml
fly volumes create taster_relay_data --size 1 --region arn   # match fly.toml's primary_region
```

Set the relay's secrets (generate fresh values — don't reuse the local
test ones from step 1). Generate into shell variables **first** and echo
them — don't inline the generation into `fly secrets set`, or the values
go to Fly without ever being displayed and can't be read back:

```bash
TASTER_API_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
WORKER_API_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
echo "TASTER_API_KEY=$TASTER_API_KEY"
echo "WORKER_API_KEY=$WORKER_API_KEY"

fly secrets set TASTER_API_KEY="$TASTER_API_KEY" WORKER_API_KEY="$WORKER_API_KEY"
```

**Save both echoed values now** (password manager) — `fly secrets set`
doesn't let you read them back later. If you ever lose them, just
generate fresh ones and re-run `fly secrets set` (it overwrites), then
update the PWA key and the NAS `.env` to match.
`TASTER_API_KEY` goes into the PWA's setup screen later (step 8);
`WORKER_API_KEY` goes into the worker's `.env` on the NAS (step 4) and
must match exactly.

```bash
fly deploy
cd ..
```

Note the deployed URL (`https://<your-app-name>.fly.dev`) — that's both
the PWA's address and the worker's `RELAY_URL`.

## 3. Copy the worker image and compose file onto the NAS share

Everything under `/share/Container/taster/` — CouchDB's data included, so
the whole app's state sits in one folder tree QNAP's backup tools can
capture:

```bash
ssh -p "$NAS_SSH_PORT" "$NAS_SSH" mkdir -p /share/Container/taster/couchdb/data
./deploy ship
```

This cross-builds the worker for `$NAS_PLATFORM`, streams it straight into
`docker load` on the NAS over a single SSH pipe — no source code, no
intermediate file on either end — and pushes `qnap/docker-compose.yml` to
`/share/Container/taster/docker-compose.yml`. `ship` deliberately stops
there and doesn't start anything, which is what you want before the secrets
exist. Fill those in over the next two steps, then `./deploy` (no verb) to
ship and bring the stack up.

The compose file no longer needs pushing by hand — every `ship` overwrites
it, so what runs on the NAS is always what's in this repo.

## 4. Fill in the NAS-side secrets

Create two files directly on the NAS via heredocs (fill in real values
before running — don't paste placeholder text literally):

**`/share/Container/taster/.env`** (the worker's):

```bash
ssh -p "$NAS_SSH_PORT" "$NAS_SSH" "cat > /share/Container/taster/.env" <<'EOF'
ANTHROPIC_API_KEY=sk-ant-your-real-key
COUCHDB_URL=http://taster-couchdb:5984
COUCHDB_DB=hobby
COUCHDB_USER=admin
COUCHDB_PASSWORD=pick-a-password
RELAY_URL=https://your-app-name.fly.dev
WORKER_API_KEY=the-worker-key-from-step-2
POLL_INTERVAL_S=3.0
ITEMS_SNAPSHOT_INTERVAL_S=60.0
TASTER_COUCHDB_LAN_IP=10.0.0.2
EOF
```

`COUCHDB_USER`/`PASSWORD` here should match `couchdb.env` below for now
(the admin account) — the worker's very first boot has to run as admin
anyway, since creating the database and its Mango indexes requires admin
rights. Step 6 then creates a proper least-privilege member account and
switches this file over to it. `WORKER_API_KEY` must exactly match the
value you set on the relay in step 2. `TASTER_COUCHDB_LAN_IP` is read by
Compose itself (not the worker) for `qnap/docker-compose.yml`'s
`${TASTER_COUCHDB_LAN_IP:-...}` — Compose auto-loads `.env` from the
compose file's own directory, so this same file doubles as the
substitution source. Pick a free address on your qnet subnet (see step 5)
before deploying; the value above is only a placeholder.

**`/share/Container/taster/couchdb.env`** (CouchDB's admin bootstrap
account):

```bash
ssh -p "$NAS_SSH_PORT" "$NAS_SSH" "cat > /share/Container/taster/couchdb.env" <<'EOF'
COUCHDB_USER=admin
COUCHDB_PASSWORD=pick-a-password
EOF
```

(same password as `COUCHDB_PASSWORD` above)

## 5. Bring the Application up

```bash
./deploy   # rerun now that steps 3-4 are in place
```

Check the LAN address you set as `TASTER_COUCHDB_LAN_IP` in step 4 (the
only LAN address this app claims; the worker lives on a private bridge
instead, see the compose file's networking note) isn't already used by
another Application on `qnet-static-eth1-dc7e3a` first — via Container
Station's network view, or
`ssh -p "$NAS_SSH_PORT" "$NAS_SSH" "'$NAS_DOCKER_BIN' network inspect qnet-static-eth1-dc7e3a"`.
Adjust `.env` and rerun `./deploy` if it collides with anything —
no need to re-copy the compose file itself, since the address lives in
`.env`, not in `qnap/docker-compose.yml`.

CouchDB and the worker start together here for the first time; the
worker's service definition waits on CouchDB's healthcheck
(`condition: service_healthy`) before starting, so there's no race where the
worker tries to create its database before CouchDB is actually accepting
connections.

```bash
ssh -p "$NAS_SSH_PORT" "$NAS_SSH" "'$NAS_DOCKER_BIN' logs taster-worker"
```

You should see it log that it's polling the relay. No port to curl here —
the worker doesn't listen on anything.

## 6. Create the worker's least-privilege CouchDB account

So far the worker has been authenticating as the admin bootstrap account
from `couchdb.env` — that was necessary for its first boot (creating the
`hobby` database and its Mango indexes requires admin rights). Now
replace it with a dedicated **member** account that can read/write the
`hobby` database and nothing else. This is the account
`ARCHITECTURE.md`'s security summary refers to.

**Run this after step 5** — the `hobby` database must already exist
(the worker created it on its first, admin-authenticated boot).

These requests run **inside the CouchDB container against loopback**
(`docker exec taster-couchdb curl http://127.0.0.1:5984/...`). That's not
just convenience — on this NAS nothing else works: `qnet-static-eth1-dc7e3a`
can't be reached from the NAS host shell (macvlan behavior), and — verified
during install — **not even from another container on that same qnet
network** (a throwaway `curlimages/curl` container on qnet couldn't
connect to `TASTER_COUCHDB_LAN_IP` either; same reason the worker talks to
CouchDB over the `taster-internal` bridge instead). Loopback inside the
container sidesteps every network layer, and the `couchdb:3` image ships
`curl`. The qnet address is only reachable from *real* LAN devices (your Mac, phone) —
which is fine, since Obsidian LiveSync is its only consumer.

Set the two passwords as shell variables first — **don't paste `<...>`
placeholder text into the commands themselves** (bash reads `<` as a
redirect and errors out). Edit these two lines, then the commands below
can be pasted as-is. Keep the passwords shell-safe (letters/digits — they
pass through two layers of shell quoting):

```bash
CDB_ADMIN_PW=changeme-admin      # the password from couchdb.env
MEMBER_PW=changeme-member        # NEW password for the worker's account — don't reuse the admin one
```

First create the `_users` system database — a fresh single-node CouchDB
doesn't have it until you create it (verified against `couchdb:3`: without
this, the user-creation request below fails with
`{"error":"not_found","reason":"Database does not exist."}`):

```bash
ssh -p "$NAS_SSH_PORT" "$NAS_SSH" \
  "'$NAS_DOCKER_BIN' exec taster-couchdb curl -s -X PUT http://127.0.0.1:5984/_users \
   -u admin:$CDB_ADMIN_PW"
```

Create the user:

```bash
ssh -p "$NAS_SSH_PORT" "$NAS_SSH" \
  "'$NAS_DOCKER_BIN' exec taster-couchdb curl -s \
   -X PUT http://127.0.0.1:5984/_users/org.couchdb.user:taster_backend \
   -u admin:$CDB_ADMIN_PW \
   -H 'Content-Type: application/json' \
   -d '{\"name\":\"taster_backend\",\"password\":\"$MEMBER_PW\",\"roles\":[],\"type\":\"user\"}'"
```

Grant it member access to `hobby` (and, as a side effect, lock the
database down — once `_security.members` is non-empty, unauthenticated
reads are rejected):

```bash
ssh -p "$NAS_SSH_PORT" "$NAS_SSH" \
  "'$NAS_DOCKER_BIN' exec taster-couchdb curl -s \
   -X PUT http://127.0.0.1:5984/hobby/_security \
   -u admin:$CDB_ADMIN_PW \
   -H 'Content-Type: application/json' \
   -d '{\"admins\":{\"names\":[],\"roles\":[]},\"members\":{\"names\":[\"taster_backend\"],\"roles\":[]}}'"
```

Then update `COUCHDB_USER`/`COUCHDB_PASSWORD` in
`/share/Container/taster/.env` to `taster_backend` / the new password
(re-stream the file as in step 4, or edit it in place), and recreate the
worker so it picks the change up:

```bash
ssh -p "$NAS_SSH_PORT" "$NAS_SSH" "cd /share/Container/taster && '$NAS_DOCKER_BIN' compose up -d --force-recreate taster-worker"
```

The worker's startup check tolerates the downgrade: it only attempts the
admin-requiring database/index creation when the database is actually
missing, so running day-to-day as a member is fine (see
`couchdb_client.py`'s `ensure_db_and_indexes`).

Note your Obsidian LiveSync clients (step 7) will also need credentials
now that the database is members-only — either this same member account,
or the admin one. For a personal, single-user instance, skipping this
whole step and letting the worker keep using the admin account is also a
reasonable simplification — your call.

## 7. Spike: confirm Obsidian LiveSync actually shows the notes

**Do this before relying on the system.** `ARCHITECTURE.md` flags that the
worker's CouchDB document shape has not been verified against LiveSync's
actual storage format.

1. Point the Self-hosted LiveSync plugin at your CouchDB instance
   (its address on the NAS's LAN, port `5984`).
2. Trigger one chat capture (step 8 below) or write a note by hand into
   the `hobby` database.
3. Check whether it appears as a file in your vault.

If it doesn't show up as expected, the fix is isolated to `write_note()` in
`backend/app/couchdb_client.py` — everything else in the app is unaffected.

## 8. First-run configuration + end-to-end test

Open the relay's Fly URL (`https://your-app-name.fly.dev`) — that's the
PWA itself. On first load it asks for just the `TASTER_API_KEY` from
step 2 — no separate backend URL anymore, since the PWA and its API are
the same origin. Stored in `localStorage` only.

Then:

1. In **Chat**, send *"Had the Glenfarclas 15, 4 stars, sherry and nutty"*.
2. Watch it move from "Enriching..." to "Saved: Glenfarclas 15" — this
   round-trips through the relay's queue and the QNAP worker's poll loop.
3. Check **Items** — the note should appear (via the worker's post-capture
   snapshot push).
4. Check Obsidian — after a sync cycle, the note should show up under
   `Tastings/Whisky/` (this is what step 7 was de-risking).
5. Try **Lookup** — e.g. *"Have I liked any Speyside whiskies?"*.

If capture reports `"failed"`, check the worker's logs:

```bash
ssh -p "$NAS_SSH_PORT" "$NAS_SSH" "'$NAS_DOCKER_BIN' logs taster-worker"
```

A missing `ANTHROPIC_API_KEY`, a wrong CouchDB credential, a wrong
`RELAY_URL`/`WORKER_API_KEY` (check it matches the relay's Fly secret
exactly), or a Claude refusal are the most likely causes at this stage.

## Iterating after this

Changed worker code (`backend/`)? `./deploy` rebuilds, re-streams,
and restarts — nothing else to do. Changed `config.yaml` (model/effort
settings)? That file is baked into the worker image now, not bind-mounted,
so it also needs a `./deploy` to take effect — see the comment in
`qnap/docker-compose.yml` if you'd rather trade that for a bind-mounted
`config.yaml` you can edit in place without a rebuild.

Changed relay code (`relay/`)? `./deploy relay`.

## Backups

`/share/Container/taster/couchdb/data` holds the actual vault data (per
tasting-log-design.md §7: LiveSync copies on your devices are sync
artifacts, not backups — this bind-mounted directory is the one that
matters). This used to say "add it to whatever QNAP backup job already
covers your other Container Station app data" — advice nobody had actually
verified was being followed. As of 2026-08-30 it has a real, tested one
instead: `homelab/backup-vault.sh`, run nightly from the Mac (LAN-reachable
CouchDB, so the dump lands off the NAS from the start — see
`homelab/README.md`'s "Backing up the vault" for the full story). Since
2026-09-02 that means the `hobby` database specifically (`VAULT_DB=hobby`,
its own scheduled LaunchAgent) — taster's data moved out of the shared
`tastings` database that run, see "The vault split" in `homelab/README.md`.
This QNAP
bind-mount path is still worth covering by a NAS-side backup job too if one
already exists for other reasons, but it is no longer the only copy.

The relay's SQLite job queue (on its Fly volume) is **not** meant to be
backed up — it only ever holds in-flight or recently-completed job state,
nothing durable. The vault (CouchDB) is the only thing that needs a backup
story.

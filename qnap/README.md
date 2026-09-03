# QNAP deployment

Two services now: CouchDB and the worker. No reverse proxy, no DuckDNS, no
port-forwarding — the worker only ever polls the Fly relay outbound for
jobs (see `../relay/`) and posts results back the same way. See
`../ARCHITECTURE.md` for the full relay/worker split and `../INSTALL.md`
for the full walkthrough. Short version:

1. On your Mac: build the worker image **for the NAS's actual CPU
   architecture** (not your Mac's — confirmed `linux/amd64` for this NAS)
   and stream it onto the NAS — one command via `./deploy`, or manually
   (see its header comment). No source code, `build:` context, rsync, or
   scp involved (none of those work against this NAS's SSH setup) — just
   `docker save | gzip | ssh -p 44 nas "gunzip -c | <docker path> load"`,
   a single pipe with no intermediate file on either end.
2. Create `/share/Container/taster/.env` and `couchdb.env` on the NAS (from
   the `.example` templates) — e.g.
   `ssh -p 44 nas "cat > /share/Container/taster/.env" < backend/.env`. Also
   add `TASTER_COUCHDB_LAN_IP=<a free address on your qnet subnet>` to that
   same `.env` — `docker-compose.yml`'s CouchDB static IP reads it from
   there (Compose auto-loads `.env` from the compose file's directory for
   `${...}` substitution; see the compose file's header comment).
3. Create an empty `/share/Container/taster/couchdb/data` directory on the
   NAS (CouchDB's bind-mounted data dir — nothing to copy, just needs to
   exist).
4. Stream this directory's `docker-compose.yml` to
   `/share/Container/taster/docker-compose.yml` the same way (`ssh -p 44
   nas "cat > ..." < qnap/docker-compose.yml`) and import it as a Container
   Station Application (or `<docker path> compose up -d` over SSH) —
   **after** step 1's image is already loaded, since this file has no
   `build:` directive for the worker to fall back on.

**On `docker`'s path and Compose:** Container Station only puts `docker` on
`PATH` for interactive logins, not for `ssh host "command"` — every remote
command needs its absolute path (`/share/CACHEDEV1_DATA/.qpkg/container-station/bin/docker`
on this NAS; `./deploy` defaults to it, override with `NAS_DOCKER_BIN` in .deploy.env if
yours differs). This NAS also has no standalone `docker-compose` binary —
only `docker compose` (space) as a v2 plugin subcommand, confirmed via
`docker compose version` → `v2.29.1-qnap2`.

Both services live under one `/share/Container/taster/` tree: `taster-couchdb`
(stock public image, pulled normally — its data bind-mounted onto the share
so QNAP's backup tools can see it, not a Docker-managed volume), and
`taster-worker` (waits for CouchDB's healthcheck before starting, since
both come up together on first boot).

`./deploy` (at the repo root) is the repeatable path for worker code
changes — rerun it any time `backend/` changes; it rebuilds, re-streams,
pushes this directory's `docker-compose.yml`, and restarts the Application.
`./deploy ship` does everything except the restart. See homelab/README.md
for the verb contract all four NAS projects share.

The relay itself (`../relay/`) doesn't deploy through this directory at
all — it's a separate Fly.io app; see `../INSTALL.md`.

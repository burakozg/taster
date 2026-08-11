#!/usr/bin/env bash
# Build the worker image (backend/) on this Mac for the QNAP's actual CPU
# architecture and stream it straight into `docker load` on the NAS over a
# single SSH pipe — no scp, no rsync (neither is available on this QNAP's
# SSH setup), just plain SSH command execution with stdin piped through,
# which every SSH server supports. No source code ever reaches the NAS —
# only the finished image (streamed, never touching disk on either end as
# an intermediate file) plus the small config/secrets files described in
# ../INSTALL.md.
#
# There's no reverse proxy to build/deploy anymore — the worker only ever
# polls the Fly relay outbound (see relay/), never accepts an inbound
# connection.
#
# Required env vars:
#   NAS_HOST     SSH destination, e.g. "nas" (an ssh-config alias) or
#                "admin@192.168.1.50"
#   NAS_PLATFORM linux/amd64 or linux/arm64 — DO NOT GUESS THIS. Check once:
#                  ssh -p <port, if non-default> $NAS_HOST uname -m
#                x86_64 → linux/amd64 · aarch64/arm64 → linux/arm64
#                Building for the wrong one produces an image that fails
#                with "exec format error" on the NAS — not a partial
#                failure, a silent non-start.
#
# Optional:
#   NAS_APP_DIR    defaults to /share/Container/taster
#   NAS_SSH_PORT   defaults to 22
#   NAS_DOCKER_BIN defaults to Container Station's usual path
#                  (/share/CACHEDEV1_DATA/.qpkg/container-station/bin/docker)
#                  — confirmed via `which docker` on nas.local. `docker`
#                  (and `docker compose`, which this NAS has as a v2 plugin
#                  subcommand rather than a standalone `docker-compose`
#                  binary) isn't on PATH for non-interactive SSH commands,
#                  since QNAP only wires it into PATH for interactive
#                  logins — hence the explicit path rather than relying on
#                  resolution. Override if your Container Station install
#                  uses a different storage pool name.
#
# Usage:
#   export NAS_HOST=admin@nas.local
#   export NAS_PLATFORM=linux/amd64
#   ./qnap/deploy.sh

set -euo pipefail

NAS_HOST="${NAS_HOST:?set NAS_HOST, e.g. export NAS_HOST=admin@nas.local}"
NAS_PLATFORM="${NAS_PLATFORM:?set NAS_PLATFORM, e.g. export NAS_PLATFORM=linux/amd64}"
NAS_APP_DIR="${NAS_APP_DIR:-/share/Container/taster}"
NAS_SSH_PORT="${NAS_SSH_PORT:-22}"
NAS_DOCKER_BIN="${NAS_DOCKER_BIN:-/share/CACHEDEV1_DATA/.qpkg/container-station/bin/docker}"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="taster-worker:nas"

echo "== building for $NAS_PLATFORM =="
docker buildx build --platform "$NAS_PLATFORM" -t "$TAG" --load "$REPO_ROOT/backend"

echo "== streaming directly into 'docker load' on $NAS_HOST (single SSH pipe, no intermediate file on either end) =="
docker save "$TAG" | gzip | ssh -p "$NAS_SSH_PORT" "$NAS_HOST" "gunzip -c | '$NAS_DOCKER_BIN' load"

echo "== restarting the Application on the NAS =="
ssh -p "$NAS_SSH_PORT" "$NAS_HOST" "cd '$NAS_APP_DIR' && '$NAS_DOCKER_BIN' compose up -d"
echo "done."

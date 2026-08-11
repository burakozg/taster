#!/bin/sh
# Fix /data ownership, then drop to appuser and run the server.
#
# Why: the Fly volume (fly.toml [[mounts]]) is mounted at /data owned by
# root, shadowing the appuser-owned /data baked into the image — so a
# container that starts directly as appuser can't create relay.db there.
# (Local docker-compose named volumes copy the image's ownership on first
# use, which is why this only bites on Fly.) The container therefore starts
# as root just long enough to chown the mount, then execs uvicorn as
# appuser via setpriv — the running server process is never root.
set -eu

if [ "$(id -u)" = "0" ]; then
    chown appuser:appuser /data
    exec setpriv --reuid appuser --regid appuser --init-groups "$@"
fi

# Already non-root (e.g. compose `user:` override) — ownership must have
# been handled outside; just run.
exec "$@"

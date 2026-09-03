#!/usr/bin/env bash
# Deploy the relay to Fly.io.
#
# fly.toml is tracked with placeholder values (app name, volume source) —
# see the repo's real-values-never-in-tracked-files convention — but flyctl
# has no ${VAR} substitution for fly.toml the way Docker Compose does for
# its own files. So this renders a real copy to a git-ignored deploy-out/
# and deploys with `-c` pointed at that, leaving fly.toml itself untouched.
#
# Required env vars (put them in a git-ignored relay/.deploy.env — this
# script auto-sources it):
#   FLY_APP     the actual Fly app name, e.g. "taster-relay"
#   FLY_VOLUME  the actual attached volume name, e.g. "taster_relay_data"
#               (`fly volumes list -a "$FLY_APP"` shows it if unsure — must
#               match exactly or flyctl refuses to reattach it)
#
# Usage:
#   echo 'FLY_APP=taster-relay' >> .deploy.env
#   echo 'FLY_VOLUME=taster_relay_data' >> .deploy.env
#   ./deploy.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$REPO_ROOT/.deploy.env" ] && . "$REPO_ROOT/.deploy.env"

FLY_APP="${FLY_APP:?set FLY_APP in relay/.deploy.env, e.g. FLY_APP=taster-relay}"
FLY_VOLUME="${FLY_VOLUME:?set FLY_VOLUME in relay/.deploy.env, e.g. FLY_VOLUME=taster_relay_data}"

OUT_DIR="$REPO_ROOT/deploy-out"
mkdir -p "$OUT_DIR"
sed -e "s/^app = 'your-taster-relay'.*/app = '$FLY_APP'/" \
    -e "s/^  source = 'your_taster_relay_data'.*/  source = '$FLY_VOLUME'/" \
    "$REPO_ROOT/fly.toml" > "$OUT_DIR/fly.toml"

echo "== deploying $FLY_APP (volume: $FLY_VOLUME) =="
# The positional working directory is REQUIRED, not decoration. flyctl derives
# the build context from the config file's own directory, so with only
# `-c deploy-out/fly.toml` it looks for the Dockerfile inside deploy-out/ —
# which holds nothing but the rendered toml — and fails with the misleading
# "app does not have a Dockerfile or buildpacks configured", as though the app
# were misconfigured on Fly's side rather than pointed at the wrong folder.
# Passing the repo root explicitly keeps the context at relay/ (where the
# Dockerfile and the COPY paths it uses live) while -c still supplies the
# rendered config.
fly deploy "$REPO_ROOT" -c "$OUT_DIR/fly.toml" -a "$FLY_APP"

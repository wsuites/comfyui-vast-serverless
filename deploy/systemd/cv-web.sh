#!/bin/sh
# Start the built Astro server with exactly the variables it needs.
#
# Two reasons this is a script and not a list of Environment= lines.
#
# The built server reads configuration from process.env at request time, and
# nothing seeds it: apps/web/astro.config.mjs loads .env, but astro.config only
# runs during build and dev, never inside dist/server/entry.mjs. Under the CLI
# the parent process exports these (packages/cli/src/web/astro.ts, serverEnv);
# under systemd there is no parent, so this is that function.
#
# And it is a subset on purpose. .env also holds the Vast API key and the R2
# credentials, and this is the one process the internet can reach. webapp/
# server.py refuses to bind a public address so that a tunnel cannot reach the
# key; handing the same key to the process that *is* behind the tunnel would
# undo that. Node gets the seven variables it uses and nothing else.
#
# Reading .env at every start is what keeps this honest: rotate WEBAPP_PASS
# with `cv web password` and a `systemctl restart cv-web` picks it up. A copy
# of these values in a second file would drift, and the symptom of that drift
# is a login prompt that rejects the password you just set.
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
ENV_FILE="$ROOT/.env"
[ -r "$ENV_FILE" ] || { echo "cv-web: no readable $ENV_FILE" >&2; exit 78; }

# Last assignment wins, matching scripts/config.py, and quotes are stripped the
# same way. sed rather than `.` because sourcing .env would import all of it.
get() {
  sed -n "s/^[[:space:]]*$1[[:space:]]*=//p" "$ENV_FILE" \
    | tail -1 | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' \
                    -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"
}

# HOST and PORT, not WEB_HOST and WEB_PORT: those are the names @astrojs/node
# standalone reads (process.env.HOST ?? options.host). Getting this wrong does
# not fail - it silently binds whatever address astro.config.mjs baked into
# dist/ at build time, on whichever machine ran the build. Exported here so the
# address is a decision this host makes, and defaulted to loopback so that a
# build done elsewhere cannot put the site on the public IP.
HOST=$(get WEB_HOST); export HOST="${HOST:-127.0.0.1}"
PORT=$(get WEB_PORT); export PORT="${PORT:-4321}"

API_HOST=$(get API_HOST); export API_HOST="${API_HOST:-127.0.0.1}"
API_PORT=$(get API_PORT); export API_PORT="${API_PORT:-8800}"

# Off by default would be the wrong way to fail on a public hostname.
WEBAPP_AUTH=$(get WEBAPP_AUTH); export WEBAPP_AUTH="${WEBAPP_AUTH:-on}"
WEBAPP_USER=$(get WEBAPP_USER); export WEBAPP_USER="${WEBAPP_USER:-admin}"
WEBAPP_PASS=$(get WEBAPP_PASS); export WEBAPP_PASS
if [ "$WEBAPP_AUTH" = "on" ] && [ -z "$WEBAPP_PASS" ]; then
  echo "cv-web: WEBAPP_AUTH=on with an empty WEBAPP_PASS; refusing to start" >&2
  exit 78
fi

ENTRY="$ROOT/apps/web/dist/server/entry.mjs"
[ -f "$ENTRY" ] || { echo "cv-web: $ENTRY missing; run pnpm web:build" >&2; exit 72; }

# Autostart stays enabled: the CLI disables it because it calls startServer()
# itself, but here the process *is* the server.
exec node "$ENTRY"

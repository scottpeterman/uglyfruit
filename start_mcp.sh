#!/usr/bin/env bash
# Launch the mcpssh streamable-http server the cockpit's investigation pane reaches.
# The Netlapse token is resolved INSIDE uf/mcpssh/config.py (env first, then the
# local ~/.netlapse/config.yaml), so nothing token-related lives here.
set -euo pipefail
cd "$(dirname "$0")"

[ -f ./secrets.local.sh ] && source ./secrets.local.sh   # only needed for a REMOTE Netlapse

# --- Netlapse hop (mcpssh pulls inventory from here) ---
export MCPSSH_NETLAPSE_URL="${MCPSSH_NETLAPSE_URL:-http://localhost:8888}"
export MCPSSH_NETLAPSE_SCHEME="bearer"
export MCPSSH_NETLAPSE_VERIFY_TLS="${MCPSSH_NETLAPSE_VERIFY_TLS:-false}"  # inert on http
export MCPSSH_COMMAND_FILE=./commands.yml

# --- HTTP transport the cockpit connects to ---
export MCPSSH_TRANSPORT=streamable-http
export MCPSSH_HTTP_HOST=127.0.0.1
export MCPSSH_HTTP_PORT=8000
export MCPSSH_HTTP_PATH=/mcp
export MCPSSH_HTTP_AUTH_ENABLED=false            # lab: cockpit sends no bearer, so match it

# --- device SSH creds mcpssh uses ---
export MCPSSH_USERNAME="${MCPSSH_USERNAME:-cisco}"
export MCPSSH_PASSWORD="${MCPSSH_PASSWORD:-cisco}"
# export MCPSSH_KEY_FILE="$HOME/.ssh/id_rsa"

python -m uf.mcpssh
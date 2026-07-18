#!/usr/bin/env bash
# Launch the cockpit against the live lab. Publishing-safe: no secrets inline.
set -euo pipefail
cd "$(dirname "$0")"

# Token: for a LOCAL Netlapse the cockpit reads it straight from
# ~/.netlapse/config.yaml — nothing to set here, nothing to keep in sync.
# For a REMOTE Netlapse, drop it into secrets.local.sh (git-ignored):
#     export NETLAPSE_API_TOKEN="..."
[ -f ./secrets.local.sh ] && source ./secrets.local.sh

# --- device SSH (the cockpit's own terminal + Tier 1.5 broker) ---
export LAB_SSH_USERNAME="${LAB_SSH_USERNAME:-cisco}"
export LAB_SSH_PASSWORD="${LAB_SSH_PASSWORD:-cisco}"
# export LAB_SSH_KEY_FILE="$HOME/.ssh/id_rsa"   # key auth instead of password
# export LAB_SSH_LEGACY="true"                  # enable legacy algos for old gear

# --- Netlapse (the live tree source) ---
export MCPSSH_NETLAPSE_URL="${MCPSSH_NETLAPSE_URL:-http://localhost:8888}"
export MCPSSH_NETLAPSE_VERIFY_TLS="${MCPSSH_NETLAPSE_VERIFY_TLS:-false}"  # inert on http

# --- Ollama (the investigation model host) ---
export OLLAMA_URL="${OLLAMA_URL:-http://10.0.0.2:11434}"
export LAB_SSH_PASSWORD="cisco"
export LAB_USERNAME="cisco"
# No --token: a local Netlapse token is auto-resolved from its config.
# Preflight validates SSH creds + service reachability and aborts on blockers.
python -m uf.cockpit.app \
  --netlapse "$MCPSSH_NETLAPSE_URL" \
  --ollama   "$OLLAMA_URL"
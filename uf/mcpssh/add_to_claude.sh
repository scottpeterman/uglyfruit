claude mcp add -s user mcpssh \
  -e MCPSSH_CONFIG="$REPO/.mcpssh.yml" \
  -- "$REPO/.venv/bin/mcpssh"

claude mcp add -s user mcpssh \
  -e MCPSSH_CONFIG="$REPO/.mcpssh.yml" \
  -e MCPSSH_USERNAME="cisco" \
  -e MCPSSH_VENDOR_ARISTA_KEY_FILE="$HOME/.ssh/id_rsa" \
  -e MCPSSH_PASSWORD="cisco" \
  -- "$REPO/.venv/bin/mcpssh"

claude mcp remove -s  user mcpssh
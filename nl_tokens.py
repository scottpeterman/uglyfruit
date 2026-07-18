#!/usr/bin/env python3
"""Resolve the Netlapse API token for launcher scripts.

Standalone and dependency-free (pure stdlib; imports nothing from uf.*), so the
mcpssh and cockpit launchers stay independent of each other. Resolution order:

    1. MCPSSH_NETLAPSE_TOKEN   (env)
    2. NETLAPSE_API_TOKEN      (env)
    3. the local config's auth.api_tokens[0] — ONLY when the target URL is
       local (loopback / this host). A remote target never borrows the local
       token, since the remote instance may run a different one.

Prints the token to stdout and exits 0 on success; prints nothing and exits 1
if none is found. The value is never written to stderr or logged.

    export MCPSSH_NETLAPSE_TOKEN="$(python nl_token.py --netlapse http://localhost:8888)"
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from urllib.parse import urlparse

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", ""}


def _is_local(url: str | None) -> bool:
    host = (urlparse(url).hostname or "").lower() if url else ""
    if host in _LOCAL_HOSTS:
        return True
    try:
        return host == socket.gethostname().lower() or \
            socket.gethostbyname(host).startswith("127.")
    except OSError:
        return False


def _config_path() -> str:
    return os.environ.get("NETLAPSE_CONFIG") or os.path.expanduser("~/.netlapse/config.yaml")


def _from_config(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    try:
        import yaml
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        tokens = ((data.get("auth") or {}).get("api_tokens")) or []
        return tokens[0] if tokens else None
    except ImportError:
        # narrow stdlib fallback: first api_tokens entry under a top-level auth:
        in_auth = in_tokens = False
        try:
            with open(path, encoding="utf-8") as fh:
                for raw in fh:
                    s = raw.strip()
                    if not s or s.startswith("#"):
                        continue
                    if not raw[:1].isspace():          # a top-level key
                        in_auth = s.startswith("auth:")
                        in_tokens = False
                        continue
                    if in_auth and s.startswith("api_tokens:"):
                        in_tokens = True
                        continue
                    if in_tokens and s.startswith("- "):
                        return s[2:].strip().strip('"').strip("'")
        except OSError:
            return None
        return None
    except Exception:
        return None


def resolve(url: str | None) -> str | None:
    for var in ("MCPSSH_NETLAPSE_TOKEN", "NETLAPSE_API_TOKEN"):
        v = os.environ.get(var)
        if v:
            return v
    if _is_local(url):
        return _from_config(_config_path())
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Resolve the Netlapse API token.")
    ap.add_argument("--netlapse", default=os.environ.get("MCPSSH_NETLAPSE_URL"),
                    help="target base URL (default: $MCPSSH_NETLAPSE_URL)")
    args = ap.parse_args()
    tok = resolve(args.netlapse)
    if tok:
        sys.stdout.write(tok)   # no newline; clean for command substitution
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
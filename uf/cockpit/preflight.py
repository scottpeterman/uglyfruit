"""uf/cockpit/preflight.py — startup guard for the cockpit.

Fails fast and loud when the environment is half-configured, instead of letting
a missing credential or an unreachable service surface as a deep traceback
mid-session (e.g. a blank SSHClientConfig only blowing up when you pick a device
and a broker start is attempted).

Lab posture, on purpose:
  * TLS verification is a respected option, never forced. When a verify flag is
    off, the probe connects without validating the cert — matching how the rest
    of the stack treats verify-off on a trusted lab hop.
  * Everything the cockpit can run degraded without is a WARNING, not a hard
    stop, unless --strict is passed. Only the things it genuinely cannot run
    without (a reachable tree source and usable SSH auth) abort the launch.
  * Demo mode needs nothing external, so preflight is a no-op there.

Pure stdlib: urllib, ssl, os, socket. No import of anything below cockpit, so
importing this never drags in paramiko or Qt.
"""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
_COLOR = {PASS: "\033[92m", WARN: "\033[93m", FAIL: "\033[91m"}
_RESET = "\033[0m"


@dataclass
class Check:
    level: str
    name: str
    detail: str
    hint: str = ""


def _env_true(name: str, default: bool = True) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _expand(path: str | None) -> str | None:
    return os.path.expanduser(path) if path else None


# --- Netlapse token resolution ---------------------------------------------

def _netlapse_config_path() -> str:
    return os.environ.get("NETLAPSE_CONFIG") or os.path.expanduser("~/.netlapse/config.yaml")


def _scan_api_token(path: str) -> str | None:
    """Stdlib fallback: pull the first auth.api_tokens entry without PyYAML.
    Narrow on purpose — only understands the one block it needs."""
    in_auth = in_tokens = False
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.rstrip("\n")
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                top_level = not line[:1].isspace()
                if top_level:
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


def _token_from_netlapse_config(path: str | None = None) -> str | None:
    path = path or _netlapse_config_path()
    if not os.path.exists(path):
        return None
    try:
        import yaml  # present in any venv that runs Netlapse
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        tokens = ((data.get("auth") or {}).get("api_tokens")) or []
        return tokens[0] if tokens else None
    except ImportError:
        return _scan_api_token(path)
    except Exception:
        return None


_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", ""}


def _is_local_target(url: str | None) -> bool:
    if not url:
        return False
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return True
    try:
        if host == socket.gethostname().lower():
            return True
        return socket.gethostbyname(host).startswith("127.")
    except OSError:
        return False


def resolve_netlapse_token(args) -> tuple[str | None, str]:
    """Resolve the effective token and where it came from, in priority order:
    explicit --token, then NETLAPSE_API_TOKEN, then — ONLY when --netlapse points
    at a local target — the Netlapse config's auth.api_tokens. No side effects."""
    if getattr(args, "token", None):
        return args.token, "--token"
    env = os.environ.get("NETLAPSE_API_TOKEN")
    if env:
        return env, "NETLAPSE_API_TOKEN"
    url = getattr(args, "netlapse", None)
    if _is_local_target(url):
        tok = _token_from_netlapse_config()
        if tok:
            return tok, _netlapse_config_path()
    return None, "(none)"


def _http_probe(url: str, verify: bool, timeout: float = 3.0) -> tuple[bool, str]:
    """Return (reachable, detail). Any HTTP response — even 401/404/405 — counts
    as reachable; only a transport failure (refused/timeout/DNS) is unreachable.
    """
    ctx = None
    if url.lower().startswith("https://") and not verify:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return True, f"HTTP {e.code}"           # server answered -> reachable
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
        reason = getattr(e, "reason", e)
        return False, f"unreachable ({reason})"


# --- individual checks ------------------------------------------------------

def check_ssh_env() -> Check:
    """Mirror make_lab_provider's resolution so this catches exactly what would
    otherwise crash at broker start."""
    username = os.environ.get("LAB_SSH_USERNAME", "admin")
    password = os.environ.get("LAB_SSH_PASSWORD")
    key_file = _expand(os.environ.get("LAB_SSH_KEY_FILE"))

    if not password and not key_file:
        return Check(
            FAIL, "ssh auth",
            f"user={username!r} but no LAB_SSH_PASSWORD or LAB_SSH_KEY_FILE set",
            hint="source ./start_app.sh, or export LAB_SSH_PASSWORD / LAB_SSH_KEY_FILE",
        )
    if key_file and not os.path.exists(key_file):
        return Check(
            FAIL, "ssh auth",
            f"LAB_SSH_KEY_FILE points at a missing file: {key_file}",
            hint="fix the path or switch to LAB_SSH_PASSWORD",
        )
    how = "key" if key_file else "password"
    return Check(PASS, "ssh auth", f"user={username!r}, {how} auth")


def check_netlapse(url: str, token: str | None, source: str, scheme: str, verify: bool) -> list[Check]:
    out: list[Check] = []
    reachable, detail = _http_probe(url, verify)
    out.append(Check(PASS if reachable else FAIL, "netlapse", f"{url} — {detail}",
                     hint="" if reachable else "start Netlapse, or check --netlapse URL/port"))
    if token:
        tail = f"…{token[-4:]}" if len(token) >= 4 else "set"
        out.append(Check(PASS, "netlapse token", f"from {source} ({tail})"))
    elif scheme == "bearer":
        out.append(Check(WARN, "netlapse token",
                         "bearer scheme but no token (CLI / env / local config)",
                         hint="pass --token, export NETLAPSE_API_TOKEN, "
                              "or run against a local Netlapse with a token in its config"))
    return out


def check_mcpssh(url: str, verify: bool) -> Check:
    reachable, detail = _http_probe(url, verify)
    if reachable:
        return Check(PASS, "mcpssh", f"{url} — {detail}")
    return Check(WARN, "mcpssh", f"{url} — {detail}",
                 hint="start ./start_mcp.sh — investigation pane will be dead until it's up")


def check_ollama(url: str, model: str) -> Check:
    tags_url = url.rstrip("/") + "/api/tags"
    reachable, detail = _http_probe(tags_url, verify=True)
    if not reachable:
        return Check(WARN, "ollama", f"{url} — {detail}",
                     hint="start Ollama, or point --ollama at the right host")
    # reachable: try to confirm the model is pulled
    try:
        with urllib.request.urlopen(tags_url, timeout=3.0) as r:
            names = [m.get("name", "") for m in json.load(r).get("models", [])]
        if model and not any(n == model or n.startswith(model.split(":")[0]) for n in names):
            return Check(WARN, "ollama model",
                         f"{model!r} not pulled (have: {', '.join(names) or 'none'})",
                         hint=f"ollama pull {model}")
    except (urllib.error.URLError, socket.timeout, ValueError, OSError):
        pass
    return Check(PASS, "ollama", f"{url} — model {model!r} ready")


# --- orchestration ----------------------------------------------------------

def run_preflight(args, invest_cfg, strict: bool = False, skip: bool = False,
                  token_source: str = "") -> bool:
    """Return True to proceed with launch, False to abort. Prints a result
    table. Aborts on any FAIL, or on any WARN when strict."""
    if skip:
        print("preflight: skipped (--skip-preflight)", file=sys.stderr)
        return True

    live = bool(getattr(args, "netlapse", None))
    if not live:
        print("preflight: demo mode — no external services required.", file=sys.stderr)
        return True

    checks: list[Check] = [check_ssh_env()]

    token = getattr(args, "token", None)
    if token and not token_source:
        token_source = "--token/env"
    if not token:  # caller didn't pre-resolve; do it here so the check is accurate
        token, token_source = resolve_netlapse_token(args)
    nl_verify = _env_true("MCPSSH_NETLAPSE_VERIFY_TLS", default=True)
    checks += check_netlapse(args.netlapse, token, token_source,
                             getattr(args, "scheme", "bearer"), nl_verify)

    checks.append(check_mcpssh(invest_cfg.mcpssh_url, invest_cfg.verify_tls))
    checks.append(check_ollama(invest_cfg.ollama_url, invest_cfg.model))

    use_color = sys.stderr.isatty()
    print("\n── preflight ─────────────────────────────────────────", file=sys.stderr)
    for c in checks:
        tag = f"{_COLOR[c.level]}{c.level}{_RESET}" if use_color else c.level
        print(f"  [{tag}] {c.name:<14} {c.detail}", file=sys.stderr)
        if c.hint and c.level != PASS:
            print(f"         ↳ {c.hint}", file=sys.stderr)

    failed = [c for c in checks if c.level == FAIL]
    warned = [c for c in checks if c.level == WARN]

    if failed:
        print(f"\npreflight: {len(failed)} blocker(s) — not launching.\n",
              file=sys.stderr)
        return False
    if warned and strict:
        print(f"\npreflight: {len(warned)} warning(s) and --strict — not launching.\n",
              file=sys.stderr)
        return False
    if warned:
        print(f"\npreflight: launching with {len(warned)} warning(s) "
              f"(degraded, not fatal).\n", file=sys.stderr)
    else:
        print("\npreflight: all clear.\n", file=sys.stderr)
    return True
"""Global configuration for the mcpssh server.

Settings load from (in priority order): constructor args, MCPSSH_* environment
variables, then a YAML file (MCPSSH_CONFIG, default ~/.mcpssh.yml). Secrets are
never config fields — the HTTP bearer token and all device credentials live in
the environment only.

The Netlapse bearer token follows that same rule but is convenience-resolved:
if MCPSSH_NETLAPSE_TOKEN is unset and netlapse_url points at a local target, the
token is read from the local Netlapse config's auth.api_tokens and seeded into
the environment. It therefore still only ever lives in env at runtime, and a
remote target never borrows the local token.
"""

import os
import socket
from pathlib import Path
from typing import Literal, Optional, Tuple, Type
from urllib.parse import urlparse

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)


# --- Netlapse token resolution ---------------------------------------------
# The token is a secret, so it is never a settings field. When it is not in the
# environment and the target is local, read it from the local Netlapse config
# and seed the environment — keeping the "secret lives in env only" contract.

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", ""}


def _netlapse_config_path() -> Path:
    override = os.environ.get("NETLAPSE_CONFIG")
    return Path(override).expanduser() if override else Path.home() / ".netlapse" / "config.yaml"


def _is_local_target(url: Optional[str]) -> bool:
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return True
    try:
        return host == socket.gethostname().lower() or \
            socket.gethostbyname(host).startswith("127.")
    except OSError:
        return False


def _token_from_local_config() -> Optional[str]:
    path = _netlapse_config_path()
    if not path.is_file():
        return None
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        tokens = ((data.get("auth") or {}).get("api_tokens")) or []
        print(tokens)
        return tokens[0] if tokens else None
    except Exception:
        return None


def resolve_netlapse_token(url: Optional[str]) -> Optional[str]:
    """Effective token, in priority order: MCPSSH_NETLAPSE_TOKEN env,
    NETLAPSE_API_TOKEN env, then the local Netlapse config (local target only)."""
    for var in ("MCPSSH_NETLAPSE_TOKEN", "NETLAPSE_API_TOKEN"):
        v = os.environ.get(var)
        if v:
            return v
    if _is_local_target(url):
        return _token_from_local_config()
    return None


class McpConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MCPSSH_", extra="ignore")

    # Inventory source. Netlapse is the uglyfruit SOT: set netlapse_url and the
    # inventory is built from Netlapse /nodes (so a name is valid across all tiers).
    # If netlapse_url is unset, fall back to the Secure Cartography topology file
    # (mcpssh's original standalone source). The Netlapse TOKEN is a secret and is
    # read from the environment (MCPSSH_NETLAPSE_TOKEN), never a config field.
    topology_file: Optional[str] = Field(default=None)
    netlapse_url: Optional[str] = Field(default=None)
    netlapse_scheme: Literal["bearer", "apikey"] = Field(default="bearer")
    netlapse_verify_tls: bool = Field(default=True)   # False = VPN / internal-CA verify-off

    # Security whitelist/blacklist file (default-deny).
    command_file: str = Field(default="~/.mcpssh-commands.yml")
    allow_pipe: bool = Field(default=False)
    unsafe_chars: list[str] = Field(default=[";", "\n", "\r", "&"])
    pipe_modifiers: list[str] = Field(default=["include", "exclude", "section", "begin", "count"])

    # Connection / concurrency.
    connect_timeout: int = Field(default=30)
    max_workers: int = Field(default=10)
    save_output_dir: str = Field(default="~/.mcpssh_tmp")

    # Optional TextFSM (tfsm_fire) template database. None -> parser default search.
    tfsm_db_path: Optional[str] = Field(default=None)

    # Transport.
    transport: Literal["stdio", "streamable-http"] = Field(default="stdio")
    http_host: str = Field(default="127.0.0.1")
    http_port: int = Field(default=8000)
    http_path: str = Field(default="/mcp")
    http_auth_enabled: bool = Field(default=True)

    # Audit logging.
    audit_log_enabled: bool = Field(default=True)
    audit_log_destination: Literal["file", "syslog", "both"] = Field(default="file")
    audit_log_file: str = Field(default="~/.mcpssh_audit.log")
    audit_log_syslog_address: str = Field(default="/dev/log")
    audit_log_syslog_facility: str = Field(default="local0")
    audit_log_read_transcript: bool = Field(default=False)
    audit_log_transcript_dir: str = Field(default="~/.mcpssh_transcripts")

    @property
    def netlapse_token(self) -> Optional[str]:
        """Resolve the bearer token at access time (never a serialised field).
        Priority: MCPSSH_NETLAPSE_TOKEN, NETLAPSE_API_TOKEN, then the local
        Netlapse config when netlapse_url is a local target."""
        return resolve_netlapse_token(self.netlapse_url)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: Type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> Tuple[PydanticBaseSettingsSource, ...]:
        config_path_str = os.environ.get("MCPSSH_CONFIG")
        config_path = (
            Path(config_path_str).expanduser()
            if config_path_str
            else Path.home() / ".mcpssh.yml"
        )
        sources = [init_settings, env_settings]
        if config_path.is_file():
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=config_path))
        return tuple(sources)


settings = McpConfig()

# Convenience seed: if no token was supplied but a local Netlapse config carries
# one, populate the environment so existing env-based readers pick it up with no
# code change. A remote netlapse_url is left untouched (no local-token borrow).
if not os.environ.get("MCPSSH_NETLAPSE_TOKEN"):
    _seed = resolve_netlapse_token(settings.netlapse_url)
    if _seed:
        os.environ["MCPSSH_NETLAPSE_TOKEN"] = _seed
"""
Environment-variable credential sidecar for the SSH MCP server.

Secrets never live in the topology map or any config file — the map is a pure
structural artifact. Credentials are resolved at connect time from the process
environment, mirroring how the HTTP bearer token is handled in netmiko_mcp.

Both auth modes are supported, matching SSHClientConfig: password, key
(key_file + optional passphrase), or key-with-password fallback.

Resolution is per-field, most specific wins:

    device  ->  vendor  ->  role  ->  site  ->  global

so you can set a global username once and override only the password for the
classic-IOS core, or point one vendor at a key file and the rest at a password.

Env var grammar (FIELD in USERNAME, PASSWORD, KEY_FILE, KEY_PASSPHRASE):

    MCPSSH_<FIELD>                          global default
    MCPSSH_DEV_<SAFE_DEVICE>_<FIELD>        per device
    MCPSSH_VENDOR_<SAFE_VENDOR>_<FIELD>     per vendor   (e.g. ..._VENDOR_CISCO_...)
    MCPSSH_ROLE_<SAFE_ROLE>_<FIELD>         per role
    MCPSSH_SITE_<SAFE_SITE>_<FIELD>         per site

<SAFE_*> = uppercased, non-alphanumerics collapsed to single underscores.
"""

import os
from dataclasses import dataclass
from typing import Optional

from uf.mcpssh.inventory import Device, _safe

_FIELDS = ("USERNAME", "PASSWORD", "KEY_FILE", "KEY_PASSPHRASE")


@dataclass
class Credentials:
    """Resolved credentials, mapping directly onto SSHClientConfig fields."""
    username: str
    password: Optional[str] = None
    key_file: Optional[str] = None
    key_passphrase: Optional[str] = None

    @property
    def auth_mode(self) -> str:
        if self.key_file and self.password:
            return "key+password"
        if self.key_file:
            return "key"
        return "password"


def _scopes(device: Device) -> list[str]:
    """Env var prefixes from most specific to least, skipping unknowns."""
    scopes = [f"DEV_{_safe(device.name)}"]
    if device.vendor and device.vendor != "unknown":
        scopes.append(f"VENDOR_{_safe(device.vendor)}")
    if device.role:
        scopes.append(f"ROLE_{_safe(device.role)}")
    if device.site:
        scopes.append(f"SITE_{_safe(device.site)}")
    return scopes


def _lookup(field: str, device: Device) -> Optional[str]:
    """First defined value walking device -> vendor -> role -> site -> global."""
    for scope in _scopes(device):
        val = os.environ.get(f"MCPSSH_{scope}_{field}")
        if val is not None:
            return val
    return os.environ.get(f"MCPSSH_{field}")  # global fallback


def resolve(device: Device) -> Credentials:
    """
    Resolve credentials for a device, or raise ValueError if the result would
    not satisfy SSHClientConfig (needs a username and at least one of
    password / key_file).
    """
    values = {f: _lookup(f, device) for f in _FIELDS}
    username = values["USERNAME"]
    if not username:
        raise ValueError(
            f"No username resolved for '{device.name}'. Set MCPSSH_USERNAME "
            f"or a more specific MCPSSH_*_USERNAME."
        )
    if not values["PASSWORD"] and not values["KEY_FILE"]:
        raise ValueError(
            f"No credentials resolved for '{device.name}'. Set a password "
            f"(MCPSSH_PASSWORD) or a key file (MCPSSH_KEY_FILE), scoped as needed."
        )
    return Credentials(
        username=username,
        password=values["PASSWORD"],
        key_file=values["KEY_FILE"],
        key_passphrase=values["KEY_PASSPHRASE"],
    )

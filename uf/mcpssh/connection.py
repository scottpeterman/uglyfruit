"""
Thin adapter from resolved inventory + credentials to a single SSHClient run.

This module owns exactly one thing: turning (Device, Credentials, host,
command) into raw device output by driving the SSHClient lifecycle once,
against one host. It holds no policy. Command validation, audit records,
connect-candidate fallback, structured parsing, and group concurrency all
live in the server layer, where they are engine-agnostic.

Swapping SSHClient for another engine should mean rewriting only this file.

The import below points at the vendored client; repoint it at
scng.discovery.ssh.ssh_client to share the real one with no copy.
"""

from uf.mcpssh.credentials import Credentials
from uf.mcpssh.inventory import Device, wants_legacy
from uf.mcpssh.ssh_client import SSHClient, SSHClientConfig


def build_config(
    device: Device, creds: Credentials, host: str, *, timeout: int = 30
) -> SSHClientConfig:
    """Map inventory facts + resolved credentials onto an SSHClientConfig.

    legacy_mode is decided here from the platform hint the topology map
    carried — the one engine-specific inference the adapter is allowed to make.
    """
    return SSHClientConfig(
        host=host,
        username=creds.username,
        password=creds.password,
        key_file=creds.key_file,
        key_passphrase=creds.key_passphrase,
        legacy_mode=wants_legacy(device.platform),
        timeout=timeout,
    )


def run_show_command_once(config: SSHClientConfig, command: str) -> str:
    """Run one show command against one host and return raw output.

    The SSHClient context manager guarantees disconnect on every path. Any
    connect/exec failure propagates unchanged — the server maps it to an audit
    outcome and decides whether to fall back to the next candidate.
    """
    with SSHClient(config) as client:
        prompt = client.find_prompt()
        client.set_expect_prompt(prompt)
        client.disable_pagination()
        return client.execute_command(command)

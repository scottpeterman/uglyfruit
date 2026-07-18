"""mcpssh — MCP server for SSH show-command collection over a Secure Cartography map.

The connection adapter (connection.py) holds only the engine-specific SSHClient
ritual. Everything engine-agnostic is here: the security gate, audit records,
connect-candidate fallback, tfsm routing, and group concurrency.
"""

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import paramiko
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.types import ASGIApp

from uf.mcpssh.audit import (
    ALLOWED,
    DENIED,
    OUTCOME_AUTH_FAILURE,
    OUTCOME_ERROR,
    OUTCOME_INVENTORY_ERROR,
    OUTCOME_SUCCESS,
    CommandAuditContext,
    configure_audit_logger,
    log_tool_invocation,
)
from uf.mcpssh.config import settings
from uf.mcpssh.connection import build_config, run_show_command_once
from uf.mcpssh.credentials import resolve
from uf.mcpssh.http_auth import BearerTokenMiddleware
from uf.mcpssh.inventory import (
    get_device,
    get_device_names,
    get_group_names,
    get_neighbors,
    get_sanitized_inventory,
)
from uf.mcpssh.outputs import (
    list_device_outputs as _list_device_outputs,
    read_device_output as _read_device_output,
    save_device_output,
)
from uf.mcpssh.parsing import parse_with_tfsm
from uf.mcpssh.security import ValidationResult, validate_command

mcp = FastMCP(
    "mcpssh",
    instructions="MCP server for SSH show-command collection over a network topology map",
    host=settings.http_host,
    port=settings.http_port,
    streamable_http_path=settings.http_path,
)


# --------------------------------------------------------------------------
# Internal: run one already-validated command against one device, with
# connect-candidate fallback. Shared by the single and group tools.
# --------------------------------------------------------------------------
def _collect_one(
    device_name: str,
    command: str,
    use_textfsm: bool,
    *,
    tool: str,
    correlation_id: str | None = None,
) -> str | dict[str, Any] | list[Any]:
    cid = correlation_id or str(uuid.uuid4())
    audit = CommandAuditContext(cid, tool, device_name, command)

    try:
        device = get_device(device_name, settings.topology_file)
        creds = resolve(device)
    except ValueError as e:
        audit.log_outcome(OUTCOME_INVENTORY_ERROR, detail=str(e))
        return f"Inventory Error: {e}"

    if not device.connect_candidates:
        audit.log_outcome(OUTCOME_INVENTORY_ERROR, detail="no connect candidates")
        return f"Inventory Error: '{device_name}' has no usable connect address."

    last_err: Exception | None = None
    for host in device.connect_candidates:
        try:
            raw = run_show_command_once(
                build_config(device, creds, host, timeout=settings.connect_timeout),
                command,
            )
        except paramiko.AuthenticationException:
            # Same creds against every candidate — no point trying the next host.
            audit.log_outcome(OUTCOME_AUTH_FAILURE, detail=f"host={host}")
            return f"Connection Error: Authentication failed for '{device_name}'."
        except Exception as e:  # timeout / SSH / unreachable — advance to next candidate
            last_err = e
            continue
        audit.log_outcome(OUTCOME_SUCCESS, detail=f"host={host}")
        return parse_with_tfsm(raw, device.vendor, command) if use_textfsm else raw

    audit.log_outcome(OUTCOME_ERROR, detail=f"all candidates failed: {last_err}")
    return f"Connection Error: all candidates failed for '{device_name}': {last_err}"


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
@mcp.tool()
def list_groups() -> str:
    """List device groups derived from the topology (all, vendor:*, role:*, site:*, crawled, leaf)."""
    log_tool_invocation(tool="list_groups", arguments={})
    try:
        return json.dumps(get_group_names(settings.topology_file))
    except ValueError as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_devices(device_or_group: str = "all") -> str:
    """List devices (facts only, no credentials) for 'all', a group, or a device name."""
    log_tool_invocation(tool="list_devices", arguments={"device_or_group": device_or_group})
    return get_sanitized_inventory(device_or_group, settings.topology_file)


@mcp.tool()
def list_neighbors(device_name: str) -> str:
    """List a device's topology adjacencies: local interface, remote device/interface, far-side IP."""
    log_tool_invocation(tool="list_neighbors", arguments={"device_name": device_name})
    try:
        return json.dumps(get_neighbors(device_name, settings.topology_file), indent=2)
    except ValueError as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def send_show_command(
    device_name: str, command: str, use_textfsm: bool = False
) -> str | dict[str, Any] | list[Any]:
    """Run a show command on one device.

    Args:
        device_name: Exact device name from the topology.
        command: The show command (must pass the allow-list).
        use_textfsm: If True, route output through tfsm_fire for structured records.
    """
    cid = str(uuid.uuid4())
    audit = CommandAuditContext(cid, "send_show_command", device_name, command)
    result: ValidationResult = validate_command(command)
    audit.log_attempt(ALLOWED if result.allowed else DENIED, result.reason)
    if not result.allowed:
        return f"Security Error: Command '{command}' is not permitted."
    return _collect_one(
        device_name, command, use_textfsm, tool="send_show_command", correlation_id=cid
    )


@mcp.tool()
def send_show_command_to_group(
    device_or_group: str,
    command: str,
    use_textfsm: bool = False,
    save_output: bool = False,
) -> dict[str, Any]:
    """Run a show command concurrently across a group (or single device).

    Validated once before any connection. Returns a dict of device -> output
    (or saved file path when save_output=True).
    """
    result: ValidationResult = validate_command(command)
    if not result.allowed:
        cid = str(uuid.uuid4())
        CommandAuditContext(
            cid, "send_show_command_to_group", f"GROUP:{device_or_group}", command
        ).log_attempt(DENIED, result.reason)
        return {"error": f"Security Error: Command '{command}' is not permitted."}

    gcid = str(uuid.uuid4())
    CommandAuditContext(
        gcid, "send_show_command_to_group", f"GROUP:{device_or_group}", command
    ).log_attempt(ALLOWED, result.reason)

    try:
        names = get_device_names(device_or_group, settings.topology_file)
    except ValueError as e:
        return {"error": f"Inventory Error: {e}"}

    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=settings.max_workers) as executor:
        future_to_device = {
            executor.submit(
                _collect_one, name, command, use_textfsm, tool="send_show_command_to_group"
            ): name
            for name in names
        }
        for future in as_completed(future_to_device):
            name = future_to_device[future]
            try:
                output = future.result()
                results[name] = (
                    f"Saved to: {save_device_output(name, command, output)}"
                    if save_output
                    else output
                )
            except Exception as e:
                results[name] = f"Execution Error: {e}"
    return results


@mcp.tool()
def list_device_outputs(device_or_group: str) -> dict[str, Any]:
    """List saved output files for a device, group, or 'all' (newest first)."""
    log_tool_invocation(tool="list_device_outputs", arguments={"device_or_group": device_or_group})
    return _list_device_outputs(device_or_group)


@mcp.tool()
def read_device_output(device_name: str, filename: str) -> str:
    """Read a previously saved output file for a device."""
    log_tool_invocation(
        tool="read_device_output",
        arguments={"device_name": device_name, "filename": filename},
    )
    return _read_device_output(device_name, filename)


@mcp.tool()
def ping() -> str:
    """Health check."""
    log_tool_invocation(tool="ping", arguments={})
    return "pong"


# --------------------------------------------------------------------------
# Startup / transport
# --------------------------------------------------------------------------
def _get_bearer_token() -> str:
    token = os.environ.get("MCPSSH_HTTP_BEARER_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "Startup Error: MCPSSH_HTTP_BEARER_TOKEN must be set when running "
            "streamable-http with http_auth_enabled: true."
        )
    return token


def _validate_startup() -> None:
    # Inventory source: Netlapse (the uglyfruit SOT) OR the Secure Cartography
    # topology file. Netlapse takes precedence; the topology file is required ONLY
    # when Netlapse is not configured. Reachability of Netlapse is deliberately NOT
    # probed here — a startup probe over a VPN can hang or flap; the first inventory
    # read surfaces a clear error instead (build_from_netlapse raises, never a silent
    # empty). Sanity-check reachability out of band if you want it up front.
    if settings.netlapse_url:
        pass                                    # Netlapse is the source — no topology file needed
    elif not settings.topology_file:
        raise SystemExit(
            "Startup Error: no inventory source configured. Set MCPSSH_NETLAPSE_URL "
            "(Netlapse SOT) or MCPSSH_TOPOLOGY_FILE (Secure Cartography topology).")
    elif not Path(settings.topology_file).expanduser().is_file():
        raise SystemExit(f"Startup Error: topology_file '{settings.topology_file}' does not exist.")
    if not Path(settings.command_file).expanduser().is_file():
        raise SystemExit(
            f"Startup Error: command_file '{settings.command_file}' does not exist. "
            f"Create it with your allowed_commands before starting."
        )
    if settings.transport == "streamable-http" and settings.http_auth_enabled:
        _get_bearer_token()


def _run_http() -> None:
    app: ASGIApp = mcp.streamable_http_app()
    if settings.http_auth_enabled:
        app = BearerTokenMiddleware(app, _get_bearer_token())
    uvicorn.run(app, host=settings.http_host, port=settings.http_port)


def main() -> None:
    _validate_startup()
    configure_audit_logger()
    if settings.transport == "streamable-http":
        _run_http()
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
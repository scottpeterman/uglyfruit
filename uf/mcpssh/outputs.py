"""Saved command-output file helpers.

Engine-agnostic policy, so it lives in the server layer rather than the
connection adapter. Directories are 0o700 and files 0o600; reads are guarded
against path traversal.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from uf.mcpssh.config import settings
from uf.mcpssh.inventory import get_device_names


def _sanitize_command_for_filename(command: str) -> str:
    normalized = "_".join(command.split())
    safe = "".join(c if c.isalnum() or c == "_" else "_" for c in normalized)
    return safe[:50]


def save_device_output(device_name: str, command: str, output: Any) -> str:
    base_dir = Path(settings.save_output_dir).expanduser()
    base_dir.mkdir(parents=True, exist_ok=True)
    base_dir.chmod(0o700)
    device_dir = base_dir / device_name
    device_dir.mkdir(exist_ok=True)
    device_dir.chmod(0o700)

    cmd_name = _sanitize_command_for_filename(command)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    file_path = device_dir / f"{cmd_name}_{timestamp}.txt"
    content = json.dumps(output, indent=2) if isinstance(output, (list, dict)) else str(output)
    file_path.write_text(content, encoding="utf-8")
    file_path.chmod(0o600)
    return str(file_path)


def list_device_outputs(device_or_group: str) -> dict[str, Any]:
    try:
        device_names = get_device_names(device_or_group, settings.topology_file)
    except ValueError as e:
        return {"error": f"Inventory Error: {e}"}
    base_dir = Path(settings.save_output_dir).expanduser()
    result: dict[str, Any] = {}
    for name in device_names:
        device_dir = base_dir / name
        result[name] = (
            sorted((f.name for f in device_dir.glob("*.txt")), reverse=True)
            if device_dir.is_dir()
            else []
        )
    return result


def read_device_output(device_name: str, filename: str) -> str:
    if "/" in filename or "\\" in filename or ".." in filename:
        return f"Security Error: Invalid filename '{filename}'."
    base_dir = Path(settings.save_output_dir).expanduser()
    device_dir = base_dir / device_name
    file_path = device_dir / filename
    try:
        if not file_path.resolve().is_relative_to(device_dir.resolve()):
            return f"Security Error: Invalid filename '{filename}'."
    except Exception:
        return f"Security Error: Invalid filename '{filename}'."
    if not device_dir.is_dir():
        return f"Error: No saved output found for device '{device_name}'."
    if not file_path.is_file():
        return f"Error: File '{filename}' not found for device '{device_name}'."
    return file_path.read_text(encoding="utf-8")

"""
Topology-backed inventory for the SSH MCP server.

The inventory is derived from a Secure Cartography topology map — the same
seed artifact consumed by the rest of the tool ecosystem. The map is loaded
once at startup and never written back: the MCP is a pure consumer with no
runtime coupling to Secure Cartography.

A device appears in the map one of two ways:

  1. As a top-level key — it was *crawled*. node_details carries its
     management IP and platform. This is the authoritative connect target.

  2. Only inside another node's `peers` block — it was *discovered* as a
     neighbour (CDP/LLDP) but never logged into. A leaf. There is no
     node_details for it; the only addresses known are the per-link interface
     IPs its neighbours reported, which are NOT guaranteed to be a reachable
     management address.

build_inventory() unions both. Crawled nodes use mgmt_ip as the primary
connect candidate; leaf nodes fall back to a peer-reported interface IP,
flagged address_inferred=True so the uncertainty is visible rather than
silently papered over. Peer blocks are retained as adjacencies (graph edges)
so the server can answer neighbour/structure questions, not just run commands.

NOTE: the topology path is read from MCPSSH_TOPOLOGY here for standalone use.
In the assembled server this comes from McpConfig.topology_file, exactly as
netmiko_mcp's command_file does.
"""

import json
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional


def vendor_of(platform: Optional[str]) -> str:
    """Best-effort vendor classification from a platform string."""
    p = (platform or "").lower()
    if "arista" in p or "eos" in p:
        return "arista"
    if "juniper" in p or "junos" in p:
        return "juniper"
    if "nx-os" in p or "nexus" in p:
        return "cisco"
    if "cisco" in p:
        return "cisco"
    return "unknown"


def wants_legacy(platform: Optional[str]) -> bool:
    """
    Heuristic for SSHClientConfig.legacy_mode.

    Classic IOS images (e.g. 'Cisco 15.2(4)M11') predate modern cipher/KEX
    defaults and frequently need the legacy algorithm list. IOSv, IOS-XE,
    NX-OS, and vEOS negotiate fine modern, so they stay off legacy.
    """
    p = (platform or "").lower()
    if "iosv" in p or "ios-xe" in p or "nx-os" in p or "eos" in p:
        return False
    # Bare 'cisco <ver>' with no modern image marker -> assume classic IOS.
    return bool(re.search(r"cisco\s+\d", p))


def _safe(name: str) -> str:
    """hostname/group -> ENV-safe token: upper, non-alnum collapsed to '_'."""
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9]", "_", name)).strip("_").upper()


def _role_site(name: str) -> tuple[Optional[str], Optional[str]]:
    """Parse <site>-<role>-<n>(.domain) -> (site, role). None if it doesn't fit."""
    short = name.split(".")[0]
    m = re.match(r"^([A-Za-z]+)-([A-Za-z]+)-?\d*$", short)
    if not m:
        return None, None
    return m.group(1).lower(), m.group(2).lower()


@dataclass
class Adjacency:
    """One link from a device's own perspective."""
    local_interface: str
    remote_device: str
    remote_interface: str
    remote_ip: Optional[str] = None  # peer-reported interface IP on the far side


@dataclass
class Device:
    name: str
    crawled: bool
    mgmt_ip: Optional[str]
    platform: Optional[str]
    vendor: str
    site: Optional[str]
    role: Optional[str]
    peer_ips: list[str] = field(default_factory=list)
    adjacencies: list[Adjacency] = field(default_factory=list)

    @property
    def connect_candidates(self) -> list[str]:
        """Ordered connect targets. Crawled: mgmt first, then interface IPs."""
        if self.crawled and self.mgmt_ip:
            extra = [ip for ip in self.peer_ips if ip != self.mgmt_ip]
            return [self.mgmt_ip, *extra]
        return list(self.peer_ips)

    @property
    def address_inferred(self) -> bool:
        """True when the only connect target is a peer-reported interface IP."""
        return not (self.crawled and self.mgmt_ip)

    @property
    def groups(self) -> list[str]:
        g = ["all", f"vendor:{self.vendor}", "crawled" if self.crawled else "leaf"]
        if self.site:
            g.append(f"site:{self.site}")
        if self.role:
            g.append(f"role:{self.role}")
        return g

    def facts(self) -> dict[str, Any]:
        """Creds-free fact view (nothing sensitive lives in the map)."""
        return {
            "name": self.name,
            "host": self.connect_candidates[0] if self.connect_candidates else None,
            "connect_candidates": self.connect_candidates,
            "address_inferred": self.address_inferred,
            "platform": self.platform,
            "vendor": self.vendor,
            "site": self.site,
            "role": self.role,
            "crawled": self.crawled,
            "neighbor_count": len(self.adjacencies),
        }


@lru_cache(maxsize=4)
def build_inventory(topology_path: Optional[str] = None) -> dict[str, Device]:
    """Load the device model (cached). Netlapse is the uglyfruit SOT: if a Netlapse
    URL is configured (MCPSSH_NETLAPSE_URL / config netlapse_url), the inventory is
    built from Netlapse /nodes so a device name resolves identically across every
    tier — the fix for 'name known to the cockpit, unknown to mcpssh'. Otherwise the
    original Secure Cartography topology file is used (mcpssh's standalone source)."""
    nl_url = os.environ.get("MCPSSH_NETLAPSE_URL")
    nl_scheme = os.environ.get("MCPSSH_NETLAPSE_SCHEME", "bearer")
    nl_verify = os.environ.get("MCPSSH_NETLAPSE_VERIFY_TLS", "true").lower() != "false"
    try:                                    # config file / defaults, if importable
        from uf.mcpssh.config import settings
        nl_url = nl_url or settings.netlapse_url
        nl_scheme = settings.netlapse_scheme if settings.netlapse_url else nl_scheme
        nl_verify = settings.netlapse_verify_tls if settings.netlapse_url else nl_verify
    except Exception:
        pass
    if nl_url:
        # lazy import avoids a load-time cycle (that module imports Device from here)
        from uf.mcpssh.netlapse_inventory import build_from_netlapse, netlapse_token_from_env
        return build_from_netlapse(nl_url, netlapse_token_from_env(), nl_scheme, nl_verify)

    path_str = topology_path or os.environ.get("MCPSSH_TOPOLOGY_FILE")
    if not path_str:
        raise ValueError("No topology path set (MCPSSH_TOPOLOGY).")
    path = Path(path_str).expanduser()
    if not path.is_file():
        raise ValueError(f"Topology file not found: {path}")
    topo = json.loads(path.read_text(encoding="utf-8"))

    devices: dict[str, Device] = {}

    # Pass 1: crawled nodes (top-level keys) — authoritative facts.
    for name, node in topo.items():
        det = node.get("node_details", {}) or {}
        site, role = _role_site(name)
        devices[name] = Device(
            name=name,
            crawled=True,
            mgmt_ip=det.get("ip"),
            platform=det.get("platform"),
            vendor=vendor_of(det.get("platform")),
            site=site,
            role=role,
        )

    # Pass 2: walk peer blocks for adjacencies + leaf discovery.
    for name, node in topo.items():
        for peer_name, pdata in (node.get("peers", {}) or {}).items():
            peer_ip = pdata.get("ip")
            peer_platform = pdata.get("platform")

            # Record this device's adjacencies (its own perspective).
            for conn in pdata.get("connections", []) or []:
                local_if, remote_if = (conn + [None, None])[:2]
                devices[name].adjacencies.append(
                    Adjacency(local_if, peer_name, remote_if, peer_ip)
                )

            # A peer we never crawled is a leaf — synthesize it from what
            # neighbours reported. Its only known address is an interface IP.
            if peer_name not in devices:
                site, role = _role_site(peer_name)
                devices[peer_name] = Device(
                    name=peer_name,
                    crawled=False,
                    mgmt_ip=None,
                    platform=peer_platform,
                    vendor=vendor_of(peer_platform),
                    site=site,
                    role=role,
                )
            leaf = devices[peer_name]
            if not leaf.crawled and not leaf.platform and peer_platform:
                leaf.platform = peer_platform
                leaf.vendor = vendor_of(peer_platform)
            if peer_ip and peer_ip not in leaf.peer_ips:
                leaf.peer_ips.append(peer_ip)

    # Stable ordering for inferred candidate lists.
    for d in devices.values():
        d.peer_ips.sort()
    return devices


# --------------------------------------------------------------------------
# Server-facing surface — same shape netmiko_mcp's server.py already expects,
# so server.py / security.py / audit.py / http_auth.py transfer unchanged.
# --------------------------------------------------------------------------

def get_group_names(topology_path: Optional[str] = None) -> list[str]:
    inv = build_inventory(topology_path)
    groups: set[str] = set()
    for d in inv.values():
        groups.update(d.groups)
    return sorted(groups)


def get_device_names(device_or_group: str, topology_path: Optional[str] = None) -> list[str]:
    inv = build_inventory(topology_path)
    if device_or_group in inv:
        return [device_or_group]
    matched = sorted(n for n, d in inv.items() if device_or_group in d.groups)
    if not matched:
        raise ValueError(f"'{device_or_group}' is not a known device or group.")
    return matched


def get_device(name: str, topology_path: Optional[str] = None) -> Device:
    inv = build_inventory(topology_path)
    if name not in inv:
        raise ValueError(f"Device '{name}' not found in topology.")
    return inv[name]


def get_sanitized_inventory(device_or_group: str, topology_path: Optional[str] = None) -> str:
    try:
        names = get_device_names(device_or_group, topology_path)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    inv = build_inventory(topology_path)
    return json.dumps({n: inv[n].facts() for n in names}, indent=2)


def get_neighbors(name: str, topology_path: Optional[str] = None) -> list[dict[str, Any]]:
    """Graph edges for a device — the payload a flat inventory can't give you."""
    d = get_device(name, topology_path)
    return [
        {
            "local_interface": a.local_interface,
            "remote_device": a.remote_device,
            "remote_interface": a.remote_interface,
            "remote_ip": a.remote_ip,
        }
        for a in d.adjacencies
    ]
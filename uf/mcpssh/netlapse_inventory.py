"""
uf/mcpssh/netlapse_inventory.py — build mcpssh's inventory from Netlapse.

mcpssh predates this project; it inherited a Secure Cartography topology FILE as
its device source. But in the assembled uglyfruit stack, Netlapse is the single
source of truth for device identity — Tier 1 and Tier 1.5 already resolve against
it. Two inventories meant two truths, and that is precisely why a name the cockpit
knew ('eng-peer-1') could be unknown to mcpssh's topology. Pointing mcpssh at
Netlapse removes the mismatch by construction: the name the gate binds IS a valid
mcpssh key, because both read the same list.

This provider produces the SAME `Device` model the topology loader does, so every
server-facing function (get_device / get_device_names / send_show_command's connect
path) works unchanged. It reads Netlapse's `/nodes` — the oxidized-compatible list
the IdentityResolver already uses — which carries name, ip (mgmt), and model (a
netmiko platform string, so vendor_of / wants_legacy classify it unchanged).

What it does NOT carry: adjacencies (the Secure Cartography graph edges behind
`list_neighbors`). Netlapse's `/nodes` is a device list, not a topology graph, so
neighbors come back empty under this source. The send/connect path does not need
them; if the graph is wanted later it is reconstructable from Netlapse's own LLDP
captures (Tier 1 already exposes `lldp`). That trade is called out, not hidden.

VPN/TLS: this is a NEW http surface — mcpssh reaching Netlapse over the tunnel.
`verify_tls=False` skips certificate verification for an internal-CA Netlapse; it
is inert on plain http://. The token is read from the environment
(MCPSSH_NETLAPSE_TOKEN), never a config field, matching mcpssh's secrets discipline.

The `fetch` is injectable so this self-tests with canned /nodes — no Netlapse, no
network. Run: python -m uf.mcpssh.netlapse_inventory
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.request
from typing import Any, Callable, Optional


def _default_fetch(base_url: str, token: Optional[str], scheme: str,
                   verify_tls: bool, timeout: float = 15.0) -> Callable[[str], Any]:
    """A token-aware, verify-aware GET returning parsed JSON. Stdlib only, so the
    mcpssh process gains no new dependency for reading Netlapse."""
    base = base_url.rstrip("/")
    ctx: Optional[ssl.SSLContext] = None
    if base.startswith("https"):
        ctx = ssl.create_default_context()
        if not verify_tls:                       # the VPN / internal-CA switch
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    headers: dict[str, str] = {}
    if token:
        headers = ({"X-API-Key": token} if scheme == "apikey"
                   else {"Authorization": f"Bearer {token}"})

    def fetch(path: str) -> Any:
        req = urllib.request.Request(base + path, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))
    return fetch


def build_from_netlapse(base_url: str, token: Optional[str] = None,
                        scheme: str = "bearer", verify_tls: bool = True,
                        fetch: Optional[Callable[[str], Any]] = None) -> dict:
    """Build mcpssh's {name: Device} from Netlapse `/nodes`. Raises on an
    unreachable/garbled Netlapse rather than returning an empty inventory — a silent
    empty would read as 'device not found' and hide the real cause (the SOT is down)."""
    # Local imports avoid a load-time cycle (inventory imports this lazily).
    from uf.mcpssh.inventory import Device, _role_site, vendor_of

    fetch = fetch or _default_fetch(base_url, token, scheme, verify_tls)
    nodes = fetch("/nodes")
    if not isinstance(nodes, list):
        raise ValueError(f"Netlapse /nodes returned {type(nodes).__name__}, expected a list")

    devices: dict[str, Any] = {}
    for n in nodes:
        if not isinstance(n, dict):
            continue
        # derive the name EXACTLY as the cockpit's IdentityResolver does, so the key
        # mcpssh stores is byte-identical to the name the gate binds — no drift.
        name = n.get("name") or n.get("full_name")
        if not name:
            continue
        platform = n.get("model")               # netmiko-style platform string
        site, role = _role_site(name)
        devices[name] = Device(
            name=name,
            crawled=True,                        # Netlapse collects it -> reachable/known
            mgmt_ip=n.get("ip"),
            platform=platform,
            vendor=vendor_of(platform),
            site=site,
            role=role,
        )
    return devices


def netlapse_token_from_env() -> Optional[str]:
    """The Netlapse token is a secret -> environment only (never a config field)."""
    return os.environ.get("MCPSSH_NETLAPSE_TOKEN")


# ──────────────────────────────────────────────────────────────────────────────
# Self-test — a Netlapse-sourced inventory resolves the very name that failed
# against the topology file. Canned /nodes, no network.
#   python -m uf.mcpssh.netlapse_inventory
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    OK, BAD = "\u2713", "\u2717"
    fails = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        global fails
        if not cond:
            fails += 1
        print(f"  {OK if cond else BAD} {name}" + (f"  — {detail}" if detail else ""))

    NODES = [
        {"name": "eng-peer-1", "ip": "198.51.100.134", "model": "arista_eos",
         "group": "lab", "status": "success", "last": "success"},
        {"name": "eng-tor-2", "ip": "172.16.101.1", "model": "juniper_junos",
         "group": "lab", "status": "success", "last": "success"},
        {"name": "old-ios-1", "ip": "172.16.9.9", "model": "cisco_ios",
         "group": "lab", "status": "failure", "last": "shell_timeout"},
        {"name": "", "ip": "1.2.3.4", "model": "x"},   # nameless row is skipped
    ]

    def fake_fetch(path: str) -> Any:
        assert path == "/nodes", path
        return NODES

    inv = build_from_netlapse("http://nl:8888", fetch=fake_fetch)

    check("the name that failed against the topology file now RESOLVES",
          "eng-peer-1" in inv)
    d = inv["eng-peer-1"]
    check("mgmt_ip carried from Netlapse (the connect target)", d.mgmt_ip == "198.51.100.134")
    check("connect_candidates leads with mgmt_ip", d.connect_candidates == ["198.51.100.134"])
    check("vendor classified from Netlapse's model string", d.vendor == "arista", d.vendor)
    check("juniper node classified too", inv["eng-tor-2"].vendor == "juniper")
    check("nameless row skipped (no junk keys)", "" not in inv and len(inv) == 3, str(sorted(inv)))
    check("site/role is best-effort — None when the name doesn't fit <site>-<role>-<n> "
          "(identical to the topology loader)", inv["eng-peer-1"].site is None)

    # unreachable Netlapse must RAISE, not return an empty (empty == silent 'not found')
    def bad_fetch(path: str) -> Any:
        raise ConnectionError("netlapse down")
    raised = False
    try:
        build_from_netlapse("http://nl:8888", fetch=bad_fetch)
    except Exception:
        raised = True
    check("a dead Netlapse raises (never a silent empty inventory)", raised)

    print()
    print(f"  {'netlapse-backed inventory holds' if not fails else str(fails) + ' FAILED'}")
    raise SystemExit(1 if fails else 0)
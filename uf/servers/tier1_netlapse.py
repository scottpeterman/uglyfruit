"""
uglyfruit / servers — Tier 1 (Historical) MCP surface over Netlapse.

The cheapest, safest tier: "what did this device look like at the last capture,
and what changed since?" — answered with ZERO device contact, from Netlapse's
stored snapshots. Two tools, both keyed on `(device, capability)` exactly the way
Tier 1.5's widgets are, so the routing policy above sees one addressing scheme
across both lower tiers and grows no per-tier special cases:

    historical_diff(device, capability)      ← the marquee: semantic added/removed/changed
    historical_snapshot(device, capability)  ← point-in-time last capture (raw + parsed)

Identity is NOT this server's job. It imports the shared resolver
(`uf.host.identity`) to turn a device name/IP into the Netlapse int `device_id`
the structured endpoints need — and that int never leaves this layer. The server
does not call Tier 1.5 or Tier 2; it is its own process, like mcpssh.

Two honesty rules, both borrowed from the determination law one tier down:

  1. **No historical capture is not a green.** A capability Netlapse never
     captured (ospf, lldp, …) returns `available=False` with a reason — the
     historical analog of UNREAD. The tier says "I don't have that," never an
     empty PRESENT.
  2. **Carry what you couldn't trust up as a caveat** (Vision §5). If Netlapse's
     LAST collection of the box failed (`shell_timeout`), the slice may predate
     the failure — so the envelope says so, rather than handing the agent stale
     history as if it were current.

The FastMCP wiring is behind `register()` so this module imports and self-tests
with no `mcp` package and no network. Stdlib only (+ uf.host.identity). 3.10+.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

from uf.host.identity import IdentityResolver, make_fetch, _urllib_fetch


# ──────────────────────────────────────────────────────────────────────────
# Capability → Netlapse capture_type. Confirmed live against the lab Netlapse
# (/api/v1/search/capture_types -> arp,bgp,config,interfaces,lldp,mac,ospf,routes).
# The overlap with the 1.5 manifest is the prize: ospf/bgp/routes/interfaces/lldp
# all have HISTORY here, so an agent asks the same capability key at both tiers —
# "what is ospf now" (1.5) and "what changed in ospf since capture" (Tier 1).
# A capability with no entry has no historical slice and the tool says so
# (available=False), never a fabricated historical green. `mac` has no 1.5 peer
# but is a real capture, so it's exposed too. Re-confirm against a target's
# capture_types — jobs differ per fleet.
# ──────────────────────────────────────────────────────────────────────────
CAPABILITY_TO_CAPTURE: dict[str, str] = {
    "config": "config",
    "bgp": "bgp",
    "ospf": "ospf",
    "routes": "routes",
    "interfaces": "interfaces",
    "lldp": "lldp",
    "arp": "arp",
    "mac": "mac",
    # version / environment / mlag: no Tier 1 capture -> available=False, honestly.
}


class NoHistoryError(Exception):
    """Fewer than two stored captures of a type — there is nothing to diff yet.
    Distinct from a transport failure: the device is fine, history just hasn't
    accrued. The historical analog of 'read succeeded, nothing to compare.'"""


class NetlapseClient:
    """Thin read-only REST wrapper. Injectable fetch so the tools test offline;
    otherwise carries the Netlapse machine token on every call (the /api/v1
    slices are behind Netlapse's auth gate — see uf.host.identity.make_fetch).
    Read-only by CONSTRUCTION: it only ever calls GET endpoints, even though a
    Netlapse api_token is admin-grade — the same read-only-by-construction-vs-
    credential split the broker carries (Transport README §5.2)."""

    def __init__(self, base_url: str, fetch_json: Optional[Callable[[str], Any]] = None,
                 token: Optional[str] = None, scheme: str = "bearer"):
        self.base_url = base_url.rstrip("/")
        self._fetch = fetch_json if fetch_json is not None else make_fetch(token, scheme)

    def capture_types(self) -> list[str]:
        try:
            return list(self._fetch(f"{self.base_url}/api/v1/search/capture_types") or [])
        except Exception:
            return []

    def latest_snapshot(self, device_id: int, capture_type: str) -> Any:
        q = urllib.parse.urlencode({"capture_type": capture_type})
        return self._fetch(
            f"{self.base_url}/api/v1/devices/{device_id}/snapshots/latest?{q}")

    def list_snapshots(self, device_id: int, capture_type: str,
                       limit: int = 50) -> list:
        # limit is generous because the scheduler collects ONE type per run
        # (round-robin), so the second-most-recent capture of a given type can be
        # several runs back. 50 comfortably covers an 8-type rotation; the rows
        # are metadata only (no payload), so it's cheap.
        q = urllib.parse.urlencode({"capture_type": capture_type, "limit": limit})
        return list(self._fetch(
            f"{self.base_url}/api/v1/devices/{device_id}/snapshots?{q}") or [])

    def structured_diff(self, device_id: int, capture_type: str,
                        mask: bool = True) -> Any:
        """Diff the two most recent stored states of <capture_type>, explicitly.

        Confirmed contract (gear, dev 1): the version ref field is `version_id`;
        the live tree is the literal `"current"` and carries EVERY type; each
        timestamped historical carries the single type collected that run
        (round-robin scheduler). So two states of one capability are found by
        FILTERING the snapshot list to rows whose `capture_types` includes it
        (newest-first), then diffing row[1] -> row[0] via from_commit/to_commit.
        The newest is normally `"current"`. Omitting the refs 404s, so they are
        always sent explicitly. Fewer than two matching rows -> NoHistoryError:
        this type has been captured at most once, nothing to diff yet (the honest
        `config` case — config has only ever landed in `current` here)."""
        snaps = self.list_snapshots(device_id, capture_type, limit=50)
        rows = [s for s in snaps
                if isinstance(s, dict)
                and capture_type in (s.get("capture_types") or [])]
        if len(rows) < 2:
            raise NoHistoryError(
                f"{len(rows)} stored state(s) of {capture_type!r} "
                f"(one-type-per-run scheduler; this type's history is sparse)")
        to_ref = rows[0].get("version_id")     # newest (usually 'current')
        from_ref = rows[1].get("version_id")   # the prior stored state of this type
        params = {"capture_type": capture_type, "mask": "true" if mask else "false",
                  "from_commit": from_ref, "to_commit": to_ref}
        q = urllib.parse.urlencode(params)
        return self._fetch(f"{self.base_url}/api/v1/devices/{device_id}/diff?{q}")


# ──────────────────────────────────────────────────────────────────────────
# Tool implementations — plain functions, so they test without the mcp runtime.
# Each returns a normalized envelope an agent can read the same way across both
# Tier 1 tools (and shaped to sit beside a 1.5 Reading without colliding with its
# PRESENT/ABSENT/UNREAD vocabulary — Tier 1's axis is captured / not-captured).
# ──────────────────────────────────────────────────────────────────────────
def _envelope(device: str, capability: str, **kw) -> dict[str, Any]:
    base = {"tier": "historical", "device": device, "capability": capability,
            "available": False, "reason": "", "caveat": "",
            "device_id": None, "vendor": None, "capture_type": None, "payload": None}
    base.update(kw)
    return base


def _resolve_or_explain(resolver: IdentityResolver, device: str, capability: str
                        ) -> tuple[Optional[Any], Optional[str], dict[str, Any]]:
    """Returns (identity, capture_type, error_envelope_or_None-fields). On any
    miss it hands back a fully-formed unavailable envelope so each tool is a
    two-liner."""
    ident = resolver.resolve(device)
    if ident is None:
        return None, None, _envelope(device, capability, reason="device unknown to Tier 1 "
                                     "(not in Netlapse inventory; possibly cold-open)")
    capture = CAPABILITY_TO_CAPTURE.get(capability)
    if capture is None:
        return ident, None, _envelope(
            device, capability, vendor=ident.vendor, device_id=ident.device_id,
            reason=f"no historical capture for '{capability}' (Tier 1 captures: "
                   f"{sorted(CAPABILITY_TO_CAPTURE)})")
    if ident.device_id is None:
        return ident, capture, _envelope(
            device, capability, vendor=ident.vendor, capture_type=capture,
            reason="device_id unresolved — structured slice needs the "
                   "/api/v1/devices join (name-keyed oxidized fallback not wired)")
    return ident, capture, {}   # empty dict = proceed


def _liveness_caveat(resolver: IdentityResolver, device: str) -> str:
    ok, why = resolver.liveness(device)
    if ok:
        return ""
    return (f"Tier 1's last collection of {device} FAILED ({why}); this historical "
            f"slice may predate the failure and the box may now be unreachable.")


def historical_snapshot_impl(resolver: IdentityResolver, client: NetlapseClient,
                             device: str, capability: str = "config") -> dict[str, Any]:
    ident, capture, miss = _resolve_or_explain(resolver, device, capability)
    if miss:
        return miss
    try:
        snap = client.latest_snapshot(ident.device_id, capture)
    except Exception as e:
        return _envelope(device, capability, vendor=ident.vendor,
                         device_id=ident.device_id, capture_type=capture,
                         reason=f"Netlapse fetch failed: {e}")
    return _envelope(device, capability, available=True, vendor=ident.vendor,
                     device_id=ident.device_id, capture_type=capture, payload=snap,
                     caveat=_liveness_caveat(resolver, device))


# Presentation keys the Netlapse diff carries for the UI side-by-side viewer, but
# an AGENT must not read as drift: `raw_diff` is a unified TEXT diff that still
# shows volatile timer lines (lldp "age", "last changed N ago") even when the
# SEMANTIC added/removed/changed are EMPTY; `aligned` is the line-aligned version
# of the same text. A model handed the whole payload narrates the wiggle in
# raw_diff as a change — exactly what qwen3 did on a provably-clean spine. Vision
# §6: Tier 1 hands the agent a digested object, not scrollback. So project the
# diff to its semantic essentials before it leaves this tier, and make the verdict
# unmissable (has_changes + counts) for a small/quantized model.
_DIFF_PRESENTATION_KEYS = ("raw_diff", "aligned")


def _agent_diff(diff: Any) -> Any:
    if not isinstance(diff, dict):
        return diff
    d = {k: v for k, v in diff.items() if k not in _DIFF_PRESENTATION_KEYS}
    a = d.get("added") or []
    r = d.get("removed") or []
    c = d.get("changed") or []
    d["n_added"], d["n_removed"], d["n_changed"] = len(a), len(r), len(c)
    d["has_changes"] = bool(a or r or c)
    return d


def historical_diff_impl(resolver: IdentityResolver, client: NetlapseClient,
                         device: str, capability: str = "config") -> dict[str, Any]:
    ident, capture, miss = _resolve_or_explain(resolver, device, capability)
    if miss:
        return miss
    try:
        diff = client.structured_diff(ident.device_id, capture)
    except NoHistoryError as e:
        return _envelope(device, capability, vendor=ident.vendor,
                         device_id=ident.device_id, capture_type=capture,
                         reason=f"only {e}; nothing to diff yet (history accrues as "
                                f"Netlapse re-collects this capture)")
    except urllib.error.HTTPError as e:
        return _envelope(device, capability, vendor=ident.vendor,
                         device_id=ident.device_id, capture_type=capture,
                         reason=f"Netlapse diff HTTP {e.code} {e.reason}")
    except Exception as e:
        return _envelope(device, capability, vendor=ident.vendor,
                         device_id=ident.device_id, capture_type=capture,
                         reason=f"Netlapse diff failed: {e}")
    return _envelope(device, capability, available=True, vendor=ident.vendor,
                     device_id=ident.device_id, capture_type=capture,
                     payload=_agent_diff(diff),
                     caveat=_liveness_caveat(resolver, device))


def historical_inventory_impl(resolver: IdentityResolver) -> list[dict[str, Any]]:
    """What Tier 1 knows, with per-device liveness — lets an agent (and the gate)
    see which boxes Netlapse last reached before choosing where to look."""
    return [{"device": d.name, "ip": d.ip, "vendor": d.vendor, "group": d.group,
             "collect_ok": d.collect_ok, "last": d.collect_last}
            for d in resolver.all_devices()]


# ──────────────────────────────────────────────────────────────────────────
# FastMCP wiring — attached by register() so import never needs the mcp package.
# Mirrors mcpssh's server style (one process, its own tools). Run: `python -m
# servers.tier1_netlapse --netlapse http://0.0.0.0:8888`.
# ──────────────────────────────────────────────────────────────────────────
def register(mcp, resolver: IdentityResolver, client: NetlapseClient) -> None:
    @mcp.tool()
    def historical_diff(device: str, capability: str = "config") -> dict[str, Any]:
        """Tier 1: what changed in <capability> on <device> since the last capture.
        Semantic added/removed/changed records, zero device contact. capability in
        config/bgp/routes/arp; anything else returns available=False with a reason."""
        return historical_diff_impl(resolver, client, device, capability)

    @mcp.tool()
    def historical_snapshot(device: str, capability: str = "config") -> dict[str, Any]:
        """Tier 1: the last captured state of <capability> on <device> (raw + parsed),
        zero device contact. available=False (with reason) if Tier 1 has no such capture."""
        return historical_snapshot_impl(resolver, client, device, capability)

    @mcp.tool()
    def historical_inventory() -> list[dict[str, Any]]:
        """Tier 1: the devices Netlapse knows, each with last-collection liveness."""
        return historical_inventory_impl(resolver)


def main() -> int:
    import argparse, os
    p = argparse.ArgumentParser(description="uglyfruit Tier 1 (historical) MCP server")
    p.add_argument("--netlapse", default="http://0.0.0.0:8888",
                   help="Netlapse base URL (default %(default)s)")
    p.add_argument("--token", default=os.environ.get("NETLAPSE_API_TOKEN"),
                   help="Netlapse api_token (or env NETLAPSE_API_TOKEN). Must be "
                        "listed under auth.api_tokens in Netlapse config.")
    p.add_argument("--scheme", choices=("bearer", "apikey"), default="bearer")
    p.add_argument("--ttl", type=float, default=300.0, help="identity cache TTL (s)")
    args = p.parse_args()

    from mcp.server.fastmcp import FastMCP
    resolver = IdentityResolver(args.netlapse, token=args.token, scheme=args.scheme,
                                ttl=args.ttl)
    client = NetlapseClient(args.netlapse, token=args.token, scheme=args.scheme)
    mcp = FastMCP("uglyfruit-tier1-historical")
    register(mcp, resolver, client)
    mcp.run()
    return 0


# ──────────────────────────────────────────────────────────────────────────
# Self-test — proves both tools and the honesty rules with no mcp, no network.
#   python -m servers.tier1_netlapse --self-test
# ──────────────────────────────────────────────────────────────────────────
if __name__ == "__main__" and "--self-test" in __import__("sys").argv:
    OK, BAD = "\u2713", "\u2717"

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"  {OK if cond else BAD} {name}" + (f"  — {detail}" if detail else ""))

    NODES = [
        {"name": "eng-spine-1", "ip": "172.16.2.2", "model": "arista_eos",
         "group": "eng", "status": "success", "time": "t", "last": "success"},
        {"name": "eng-spine-2", "ip": "172.16.2.6", "model": "arista_eos",
         "group": "eng", "status": "failure", "time": "t", "last": "shell_timeout"},
    ]
    DEVICES = [{"id": 12, "name": "eng-spine-1", "primary_ip4": "172.16.2.2"},
               {"id": 13, "name": "eng-spine-2", "primary_ip4": "172.16.2.6"}]
    DIFF_BGP = {"added": [{"peer": "172.16.24.32"}], "removed": [],
                "changed": [{"peer": "198.51.100.130", "field": "prefixAccepted",
                             "from": 374, "to": 375}]}
    SNAP_BGP = {"raw": "...show ip bgp summary...", "parsed": {"peers": 4, "down": 1}}

    def id_fetch(url: str) -> Any:
        if url.endswith("/nodes"):
            return NODES
        if url.endswith("/api/v1/devices"):
            return DEVICES
        raise AssertionError(url)

    def nl_fetch(url: str) -> Any:
        if "/snapshots/latest?" in url and "/devices/13/" in url:
            return SNAP_BGP
        if "/snapshots?" in url and "/devices/12/" in url:   # the LIST (for diff)
            # round-robin shape: 'current' carries all types; one historical bgp.
            return [{"version_id": "current", "collected_at": "t2",
                     "capture_types": ["config", "bgp", "ospf"]},
                    {"version_id": "20260630T010337Z", "collected_at": "t1",
                     "capture_types": ["bgp"]}]
        if "/diff?" in url and "/devices/12/" in url and "capture_type=bgp" in url:
            return DIFF_BGP
        if url.endswith("/search/capture_types"):
            return ["config", "bgp", "routes", "arp"]
        raise AssertionError(f"unexpected netlapse url {url}")

    resolver = IdentityResolver("http://x:8888", fetch_json=id_fetch)
    client = NetlapseClient("http://x:8888", fetch_json=nl_fetch)

    print("historical_diff — the marquee Tier 1 tool, keyed (device, capability)")
    d = historical_diff_impl(resolver, client, "eng-spine-1", "bgp")
    check("resolved name->id=12, capture=bgp, semantic diff returned",
          d["available"] and d["device_id"] == 12 and d["capture_type"] == "bgp"
          and d["payload"]["changed"][0]["to"] == 375,
          f"available={d['available']} id={d['device_id']}")
    check("clean box -> no caveat", d["caveat"] == "")

    print("\nhonesty rule 1 — a capability Tier 1 never captured is NOT a green")
    o = historical_diff_impl(resolver, client, "eng-spine-1", "environment")
    check("environment -> available=False with a reason (historical analog of UNREAD)",
          (not o["available"]) and "no historical capture" in o["reason"], o["reason"])

    print("\nhonesty rule 2 — a failed last collection rides up as a caveat")
    s = historical_snapshot_impl(resolver, client, "eng-spine-2", "bgp")
    check("eng-spine-2 snapshot available but caveated (shell_timeout)",
          s["available"] and "FAILED (shell_timeout)" in s["caveat"], s["caveat"][:60] + "…")

    print("\nunknown device — never fabricated")
    g = historical_diff_impl(resolver, client, "ghost-99", "bgp")
    check("ghost-99 -> available=False, 'unknown to Tier 1'",
          (not g["available"]) and "unknown to Tier 1" in g["reason"])

    print("\ninventory — what Tier 1 knows + liveness, for the agent and the gate")
    inv = historical_inventory_impl(resolver)
    check("inventory carries per-device liveness",
          any(x["device"] == "eng-spine-2" and not x["collect_ok"] for x in inv),
          f"{len(inv)} devices")
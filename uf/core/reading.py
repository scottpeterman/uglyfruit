"""
uglyfruit / Tier 1.5 — the Honest-1.5 contract, in code.

This module is the §3 schema and the §4 discriminator from Design Note 01,
made executable. It commits to nethuds' *existing* read tables: it consumes the
exact dict a collector already emits and returns one `Reading` per capability.

Two ideas carry the whole thing:

  1. State is three-valued, not two.  PRESENT / ABSENT / UNREAD.
     `read-failed` is NOT `feature-absent`. (§2.1)

  2. One law makes silent failure structurally impossible (§4):
        absence must be positively evidenced; empty defaults to UNREAD.
     A vendor discriminator may only return ABSENT when the box *answered*
     with a structurally-valid "nothing here." Anything it cannot positively
     read as absence is UNREAD, never defaulted to fine.

The frame invariant (§2.2) is enforced by construction, not by discipline:
a Frame can exist *only* on a PRESENT Reading. Default-to-green is unreachable.

Stdlib only. Python 3.10+.
"""

from __future__ import annotations

import time
from typing import Any

# Re-export the law so existing `import reading` / `from reading import Reading,
# State, ...` consumers (transport.py, session.py, host/identity.py) keep working
# unchanged after the split. Dual-import: bare-name regime | package regime.
try:
    from law import (State, Status, Frame, frame_status, Reading, classify,
                     Discriminator)
    from vendors import arista, juniper
except ImportError:
    from uf.core.law import (State, Status, Frame, frame_status, Reading,
                             classify, Discriminator)
    from uf.core.vendors import arista, juniper


# ──────────────────────────────────────────────────────────────────────────
# Capability determination — the registry that turns one-off discriminators
# into a TOTAL operation. The global (vendor, key) -> discriminator table is
# ASSEMBLED from each vendor module's own map (Note 05 §2a). Adding a vendor is
# one import + one .update() below — the law and mechanics never change.
# ──────────────────────────────────────────────────────────────────────────
DISCRIMINATORS: dict[tuple[str, str], Discriminator] = {}
DISCRIMINATORS.update(arista.ARISTA_DISCRIMINATORS)
DISCRIMINATORS.update(juniper.JUNIPER_DISCRIMINATORS)   # <- vendor 2


def determine(vendor: str, key: str, value: Any,
              as_of: float | None = None) -> Reading:
    """Determine ONE capability's state. TOTAL: every key yields a Reading.

    A key with no registered discriminator is UNDETERMINED. That is the TOOL's
    gap, not the device's — but an undetermined capability still must not be
    trusted as a green, so it collapses to the law's safe value, UNREAD, with a
    reason that preserves the distinction for audit. Nothing passes through as
    raw-and-unjudged the way the old per-key branch let it.
    """
    fn = DISCRIMINATORS.get((vendor, key))
    if fn is None:
        return Reading(key, State.UNREAD, payload=value,
                       as_of=as_of if as_of is not None else time.time(),
                       reason=f"undetermined: no discriminator for {vendor}/{key}")
    return fn(value, as_of)


def capabilities(vendor: str) -> set[str]:
    """The capabilities this tier can positively DETERMINE for a vendor — the
    keys with a registered discriminator. The literal 'capabilities determined'
    set, separate from any one poll's PRESENT/ABSENT/UNREAD verdicts."""
    return {k for (v, k) in DISCRIMINATORS if v == vendor}


def determine_all(vendor: str, results: dict[str, Any],
                  as_of: float | None = None) -> dict[str, Reading]:
    """Determine a whole poll's worth of results -> a capability profile.
    {key: Reading} for every key the box answered. The device's determined
    capability picture in one pass."""
    return {k: determine(vendor, k, v, as_of) for k, v in results.items()}


# ──────────────────────────────────────────────────────────────────────────
# Self-test — fixtures shaped like the *real* collector output. Run directly:
#   python reading.py
# Proves PRESENT / ABSENT / UNREAD all three fall out, and that the ambiguous
# case (well-formed, but path missing) lands UNREAD rather than ABSENT.


if __name__ == "__main__":
    print("\ncapability determination")
    print(f"  determinable for 'arista': {sorted(capabilities('arista'))}")
    poll = {
        "ospf": {"vrfs": {"default": {"instList": {"1": {"ospfNeighborEntries": [
            {"adjacencyState": "full"}]}}}}},
        "bgp": {"vrfs": {"default": {"routerId": "0.0.0.0", "asn": "0", "peers": {}}}},
        "mlag": {"state": "disabled"},          # explicit-token ABSENT
        "lldp": {"lldpNeighbors": []},           # empty -> UNREAD, NOT ABSENT
        "interfaces": {"interfaceStatuses": {    # PRESENT-by-existence, one fault
            "Ethernet1": {"linkStatus": "connected", "lineProtocolStatus": "up"},
            "Ethernet4": {"linkStatus": "errdisabled", "lineProtocolStatus": "down"}}},
        "version": {"modelName": "DCS-7280SR", "version": "4.27.3M"},  # PRESENT, frameless
        "environment": {"power": {"powerSupplies": {"1": {"state": "ok"}}},
                        "temperature": {"systemStatus": "temperatureOk", "tempSensors": []},
                        "cooling": {"systemStatus": "coolingOk", "fanTraySlots": []}},
        "routes": {"vrfs": {"default": {"totalRoutes": 1842}}},        # PRESENT-by-existence
        "transceivers": {"xcvrSlots": {"1": {"mfgName": "Arista",      # PRESENT-by-existence, frameless
            "modelName": "QSFP-100G-SR4", "serialNum": "XCV0000001"},
            "2": {"mfgName": "Not Present"}}},
        "proc": {"cpuInfo": {"%Cpu(s)": {"idle": 91.5, "user": 6.0}},   # PRESENT, CPU 8.5% -> OK
                 "processes": {"1893": {"cmd": "Bcm", "cpuPct": 6.2}}},
        "vxlan": {"interfaces": {}},   # no discriminator registered -> undetermined
    }
    profile = determine_all("arista", poll)
    for k, r in profile.items():
        print(f"    {k:<11} -> {str(r.state):<8} {r.reason}")
    assert str(profile["ospf"].state) == "PRESENT"
    assert str(profile["bgp"].state) == "ABSENT"
    assert str(profile["mlag"].state) == "ABSENT"   # explicit 'disabled' token
    assert str(profile["lldp"].state) == "UNREAD"   # empty neighbors stays unproven
    assert str(profile["interfaces"].state) == "PRESENT"  # by-existence, frame carries fault
    assert str(profile["version"].state) == "PRESENT"     # identity, frameless
    assert str(profile["environment"].state) == "PRESENT" # has a fan -> real read
    assert str(profile["routes"].state) == "PRESENT"      # RIB present
    assert str(profile["transceivers"].state) == "PRESENT" and not profile["transceivers"].frames
    assert str(profile["proc"].state) == "PRESENT" and profile["proc"].frames[0].label == "cpu utilization"
    assert str(profile["vxlan"].state) == "UNREAD"  # undetermined collapses safe
    assert "undetermined" in profile["vxlan"].reason
    print("  ✓ 10 determined (ospf/bgp/mlag/lldp/interfaces/version/environment/routes/transceivers/proc) · "
          "vxlan UNDETERMINED->UNREAD (no green leaks)")

    # ── Contract conformance (Note 05 §4) ───────────────────────────────────
    # Every flat-cap PRESENT payload the EOS translators emit MUST satisfy the
    # cross-vendor contract. This is the mechanical form of "the contract gives,
    # not the translator": when a second vendor can't fill a required field,
    # THIS is the check that fails first — in its own self-test, before any
    # widget depends on the shape. Dual-mode import keeps `python reading.py`
    # working alongside `python -m uf.core.reading`.
    try:
        from contract import conforms          # python reading.py (same dir)
    except ImportError:
        from uf.core.contract import conforms   # python -m uf.core.reading
    present_payloads = [
        ("bgp", arista.arista_bgp({"vrfs": {"default": {"routerId": "10.0.0.1", "peers": {
            "10.0.0.2": {"peerState": "Established", "description": "edge1",
                         "prefixReceived": 5}}}}})),
        ("ospf", arista.arista_ospf({"vrfs": {"default": {"instList": {"1": {
            "ospfNeighborEntries": [{"routerId": "10.0.0.1", "adjacencyState": "full",
                                     "interfaceName": "Ethernet1",
                                     "details": {"areaId": "0.0.0.0"}}]}}}}})),
        ("lldp", arista.arista_lldp({"lldpNeighbors": [
            {"port": "Ethernet1", "neighborDevice": "spine1",
             "neighborPort": "Ethernet1"}]})),
    ]
    for cap, r in present_payloads:
        assert str(r.state) == "PRESENT", f"{cap}: expected PRESENT, got {r.state}"
        viol = conforms(cap, r.payload)
        assert viol == [], f"{cap}: contract violations: {viol}"
    print("  ✓ 3 flat-cap PRESENT payloads conform to the core contract "
          "(bgp/ospf/lldp) — EOS translator identity-clean")
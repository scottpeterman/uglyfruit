"""
uglyfruit / vendors / arista — the Arista EOS discriminators.

Design Note 05 §2a: one vendor's epistemic reasoning plus its captured evidence,
in its own module. Everything EOS-shaped lives here — the ten discriminators,
their structural/error helpers (`_eos_errors_verdict`, `_sub_structured`,
`_xcvr_populated`, `_cpu_used_pct`, ...), the EOS->contract translators, the
gear-captured self-test fixtures, and the ARISTA_DISCRIMINATORS map the core
registry imports. The law (core/law.py) is imported, never redefined; absence
stays vendor-aware here, PRESENT normalizes to the shared contract.

`python arista.py` runs this vendor's own suite. Dual-import keeps that working
alongside `python -m uf.core.vendors.arista`.
"""
from __future__ import annotations

import time
from typing import Any

try:  # bare-name regime (uf/core on sys.path) | package regime
    from law import (State, Status, Frame, frame_status, Reading, classify,
                     Discriminator)
except ImportError:
    from uf.core.law import (State, Status, Frame, frame_status, Reading,
                             classify, Discriminator)


# ──────────────────────────────────────────────────────────────────────────
# §6  First vertical slice — arista ospf, the proving instance.
#
# Built against the JSON shape nhd already renders (arista/static/index.html
# line ~875):  ospf.vrfs.default.instList[<pid>].ospfNeighborEntries[]
# Neighbor fields: adjacencyState, routerId, interfaceName, details{...}
#
# The collector's failure contract (arista/collector.py): on success
# data["ospf"] is the parsed JSON dict; on command/socket failure it is
# {"_error": ...}; on JSON decode failure {"_raw": ..., "_error": "json_decode_failed"}.
# ──────────────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────────
# EOS has NO uniform absence convention — absence encoding is per-capability,
# confirmed across three live shapes on the same vendor:
#   bgp,  no `router bgp`   -> {"errors": ["BGP inactive"]}   (errors envelope)
#   ospf, no `router ospf`  -> {"vrfs": {}}                    (empty vrfs map)
#   (and neither matches the empty-instList this code first assumed for ospf)
# So there is nothing to factor out to a vendor level. The errors-envelope
# interpreter below is real but BGP-relevant; ospf reads its own empty-map shape
# structurally (§2 of arista_ospf). A recognized inactivity marker reads ABSENT;
# ANY other errors content is a genuine failure -> UNREAD (reading an arbitrary
# error as ABSENT would be the exact C6 hole). See Design Note 03 §2-3.
# ──────────────────────────────────────────────────────────────────────────
_EOS_INACTIVE = ("inactive",)   # the observed EOS token; extend as the lab shows more


def _eos_errors_verdict(value: Any) -> tuple[bool | None, str] | None:
    """Interpret an EOS errors envelope.

        not an errors envelope        -> None            (caller proceeds)
        recognized inactivity marker  -> (False, reason) -> ABSENT
        any other error content       -> (None,  reason) -> UNREAD

    Never returns (True, …): an errors envelope is never evidence of PRESENT.
    """
    if not (isinstance(value, dict)
            and isinstance(value.get("errors"), list) and value["errors"]):
        return None
    msgs = [str(m) for m in value["errors"]]
    low = " | ".join(msgs).lower()
    if any(tok in low for tok in _EOS_INACTIVE):
        return False, f"eos reports inactive: {msgs[0]!r}"
    return None, f"eos errors, not an inactivity marker: {msgs[0]!r}"


_DOWN = "full"  # an adjacency is "up" iff adjacencyState lowercases to 'full'


def arista_ospf(ospf_value: Any, as_of: float | None = None) -> Reading:
    """Turn the arista collector's `data['ospf']` into a Reading.

    The honest inversion of the UI's `safe(d,'ospf.vrfs.default.instList',{})`:
    where the dashboard defaults a missing path to empty (and so silently
    cannot tell read-broken from feature-gone), here a missing intermediate
    key is UNREAD, and only a present-but-empty instList is ABSENT.
    """
    ts = as_of if as_of is not None else time.time()
    key = "ospf"

    # ── 1. Did the read even succeed? ────────────────────────────────────
    # The collector's universal failure marker is an "_error" key.
    read_ok = isinstance(ospf_value, dict) and "_error" not in ospf_value
    if not read_ok:
        why = ""
        if isinstance(ospf_value, dict):
            why = ospf_value.get("_error", "")
        state, reason = classify(read_ok=False, present=None)
        return Reading(key, state, payload=ospf_value, as_of=ts,
                       reason=f"{reason}: {why}" if why else reason)

    # ── 1b. EOS errors envelope — interpret before the structural walk. ──
    # Inactive feature -> ABSENT; any other error -> UNREAD. (EOS-wide; a real
    # no-ospf box confirms whether ospf uses this or the empty-instList branch.)
    verdict = _eos_errors_verdict(ospf_value)
    if verdict is not None:
        present, why = verdict
        state, reason = classify(read_ok=True, present=present)
        return Reading(key, state, payload=ospf_value, as_of=ts, reason=why)

    # ── 2. Read succeeded. Is the capability present, absent, or unprovable? ─
    # We DO NOT default missing intermediate keys. Their absence is ambiguous,
    # and ambiguous -> UNREAD (the law), never ABSENT. But an EMPTY vrfs map is
    # NOT ambiguous: the box enumerated its OSPF VRF contexts and found none.
    # CONFIRMED live (eng-tor-3, static-routes-only): no `router ospf` -> {"vrfs":{}}.
    # (Notably this is NOT the errors envelope BGP uses, nor the empty-instList
    # this code first assumed — a THIRD shape. EOS has no uniform absence
    # convention; absence encoding is per-capability. See Note 03 §2-3.)
    vrfs = ospf_value.get("vrfs")
    if not isinstance(vrfs, dict):
        # No vrfs container at all -> no structure to read. Unprovable -> UNREAD.
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=ospf_value, as_of=ts,
                       reason=f"{reason}: no vrfs container")
    if not vrfs:
        # vrfs present and EMPTY -> zero VRFs carry OSPF. Positive "nothing here",
        # and `show vrf` confirms only the default VRF exists, so no OSPF anywhere.
        state, reason = classify(read_ok=True, present=False)
        return Reading(key, state, payload=ospf_value, as_of=ts,
                       reason="no ospf in default vrf (empty vrfs map)")
    if "default" not in vrfs:
        # vrfs has entries, but not 'default' -> OSPF runs in some VRF while the
        # default scope is unrepresented. Can't positively assert default-VRF
        # absence from a non-default shape -> UNREAD.
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=ospf_value, as_of=ts,
                       reason=f"{reason}: vrfs present, default scope not found")

    inst_list = vrfs["default"].get("instList")
    if not isinstance(inst_list, dict):
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=ospf_value, as_of=ts,
                       reason=f"{reason}: instList missing or malformed")

    if not inst_list:
        # Box answered with a structurally-valid, empty instance list:
        # zero OSPF processes. Positive evidence of absence -> ABSENT. (§4)
        state, reason = classify(read_ok=True, present=False)
        return Reading(key, state, payload=ospf_value, as_of=ts,
                       reason="no ospf process configured (empty instList)")

    # ── 3. PRESENT. Gather neighbors across all process instances. ───────
    neighbors: list[dict] = []
    for pid in inst_list.values():
        if isinstance(pid, dict):
            neighbors.extend(pid.get("ospfNeighborEntries", []) or [])

    # Translate to the contract, then frame OVER the contract list (Note 05 §3).
    records = _eos_ospf_to_contract(neighbors)
    down = sum(
        1 for n in records
        if str(n.get("adjacencyState", "")).lower() != _DOWN
    )
    frame = Frame(
        label="adjacencies not Full",
        value=down,
        ceiling=0,
        status=frame_status(down, 0),
    )
    state, reason = classify(read_ok=True, present=True)
    detail = f"{len(records)} adjacenc{'y' if len(records)==1 else 'ies'}, {down} not Full"
    return Reading(key, state, payload=records, frames=[frame],
                   as_of=ts, reason=f"{reason}: {detail}")


# ──────────────────────────────────────────────────────────────────────────
# §6  Second discriminator — arista bgp. Same law, DIFFERENT absence question.
#
# The OSPF rule does NOT transfer. For OSPF, empty instList == "no process" ==
# positive ABSENT. For BGP, empty `peers {}` is AMBIGUOUS: a configured
# `router bgp` with zero neighbors is indistinguishable, at the peers level,
# from no BGP at all. So absence can't be read off emptiness alone — we need a
# PROCESS indicator (EOS populates routerId/asn when `router bgp` exists). With
# the process present, even zero peers is PRESENT (configured, idle). Only
# empty-peers-AND-no-process is positive evidence of absence — and the exact
# no-BGP shape EOS emits is VERIFY-IN-LAB until a real no-bgp box confirms it;
# anything we can't read as positive absence stays UNREAD, never defaulted.
#
# PRESENT shape (from the collector / arista bgp summary | json):
#   bgp.vrfs.default.peers[<ip>].peerState ("Established" == up), routerId, asn
# ──────────────────────────────────────────────────────────────────────────
_BGP_UP = "established"  # a peer is "up" iff peerState lowercases to 'established'


def arista_bgp(bgp_value: Any, as_of: float | None = None) -> Reading:
    """Turn the arista collector's `data['bgp']` into a Reading.

    Mirrors arista_ospf's honesty (missing path -> UNREAD, never ABSENT), but
    decides present-ness on a BGP-process indicator, not on peer-count, so a
    configured-but-idle BGP isn't mislabeled absent.
    """
    ts = as_of if as_of is not None else time.time()
    key = "bgp"

    # ── 1. Did the read succeed? ─────────────────────────────────────────
    read_ok = isinstance(bgp_value, dict) and "_error" not in bgp_value
    if not read_ok:
        why = bgp_value.get("_error", "") if isinstance(bgp_value, dict) else ""
        state, reason = classify(read_ok=False, present=None)
        return Reading(key, state, payload=bgp_value, as_of=ts,
                       reason=f"{reason}: {why}" if why else reason)

    # ── 1b. EOS errors envelope — the CONFIRMED no-bgp shape on real gear. ──
    # {"errors": ["BGP inactive"]} -> ABSENT (positive). Other errors -> UNREAD.
    verdict = _eos_errors_verdict(bgp_value)
    if verdict is not None:
        present, why = verdict
        state, reason = classify(read_ok=True, present=present)
        return Reading(key, state, payload=bgp_value, as_of=ts, reason=why)

    # ── 2. Walk to vrfs, distinguishing positive absence from no-structure ──
    vrfs = bgp_value.get("vrfs")
    if not isinstance(vrfs, dict):
        # No vrfs container at all -> no structure to read. Unprovable -> UNREAD.
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=bgp_value, as_of=ts,
                       reason=f"{reason}: no vrfs container")
    if not vrfs:
        # vrfs present and EMPTY -> the box enumerated its BGP VRF contexts and
        # found none. Positive "nothing here", mirroring OSPF's empty instList.
        # `show vrf` on the test box shows ONLY the default VRF, so an empty BGP
        # vrfs map means no BGP in the only VRF == BGP absent.
        # VERIFY-IN-LAB: confirm EOS emits {"vrfs": {}} here (this ABSENT branch)
        # rather than a missing vrfs key (the UNREAD branch above). The captured
        # bgp_*_shape decides which fired; until then both are handled honestly.
        state, reason = classify(read_ok=True, present=False)
        return Reading(key, state, payload=bgp_value, as_of=ts,
                       reason="no bgp in default vrf (empty vrfs map)")
    if "default" not in vrfs:
        # vrfs has entries, but not 'default': BGP exists in some VRF while the
        # default scope is unrepresented. Can't positively assert default-VRF
        # absence from a non-default shape -> UNREAD.
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=bgp_value, as_of=ts,
                       reason=f"{reason}: vrfs present, default scope not found")

    default = vrfs["default"]
    if not isinstance(default, dict):
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=bgp_value, as_of=ts,
                       reason=f"{reason}: vrfs.default malformed")

    peers = default.get("peers")
    if not isinstance(peers, dict):
        # No peers key in a well-formed default. Unprovable as absence -> UNREAD.
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=bgp_value, as_of=ts,
                       reason=f"{reason}: no peers key (bgp absence shape unconfirmed)")

    # ── 3. Process indicator decides ABSENT vs PRESENT — NOT peer count ───
    # EOS fills routerId/asn iff `router bgp` is configured. A live routerId or
    # asn is positive evidence the process exists even with zero adjacencies.
    router_id = str(default.get("routerId", "")).strip()
    asn = str(default.get("asn", "")).strip()
    process_present = router_id not in ("", "0.0.0.0") or asn not in ("", "0")

    if not peers and not process_present:
        # Empty peers AND no process indicator: best positive evidence of
        # "no BGP configured". ABSENT — but VERIFY-IN-LAB that EOS truly zeroes
        # routerId/asn here rather than omitting default (caught above).
        state, reason = classify(read_ok=True, present=False)
        return Reading(key, state, payload=bgp_value, as_of=ts,
                       reason="no bgp process (empty peers, no routerId/asn)")

    # ── 4. PRESENT. Translate to the contract, then frame OVER the contract ─
    #    list (Note 05 §3): the frame runs on the canonical shape, once, so it
    #    de-duplicates across vendors instead of re-reading each box's fields.
    records = _eos_bgp_to_contract(peers)
    down = sum(
        1 for p in records
        if str(p.get("peerState", "")).lower() != _BGP_UP
    )
    frame = Frame(
        label="peers not Established",
        value=down,
        ceiling=0,
        status=frame_status(down, 0),
    )
    state, reason = classify(read_ok=True, present=True)
    detail = f"{len(records)} peer{'' if len(records)==1 else 's'}, {down} not Established"
    return Reading(key, state, payload=records,
                   frames=[frame], as_of=ts, reason=f"{reason}: {detail}")


# ──────────────────────────────────────────────────────────────────────────
# §6  Third discriminator — arista mlag. A FOURTH absence convention.
#
# OSPF answered absence with an empty container ({"vrfs":{}} / empty instList).
# BGP answered it with an errors envelope ({"errors":["BGP inactive"]}).
# MLAG answers with neither: a populated dict carrying an explicit status STRING
# field. `show mlag | json` -> {"state": "...", ...}. When the feature is not
# configured the agent answers `state: "disabled"`; when it is, the field carries
# an operational health word (active / connected / primary / secondary, and the
# transient active|inactive/reload during the reload-delay window).
#
# This is the cleanest confirmation yet of Design Note 03 §2-3: EOS has NO
# uniform absence convention. There is nothing to factor up to a vendor level —
# the discriminator IS the per-capability knowledge.
#
# The load-bearing distinction (and the C6 trap to avoid): "disabled" means the
# FEATURE is absent; "inactive/reload" means the feature is CONFIGURED and merely
# transiently down inside the reload-delay timer. They differ by one substring
# and they mean opposite things. So the ABSENT marker is the exact token
# "disabled", never a loose "not active" — reading a transient reload as
# feature-absence would paint a configured, recovering MLAG as ABSENT (no frame),
# the precise default-to-green-by-omission this contract forbids.
#
# Unlike BGP, an unrecognized-but-populated `state` is read PRESENT, not UNREAD:
# a non-empty, non-"disabled" status field is itself the process indicator (the
# agent answered with an operational status, so the feature exists), even when we
# don't recognize the exact health word. That one asymmetry from bgp is
# deliberate and is the whole reason `state` can carry the determination.
#
# VERIFY-IN-LAB: the determination keys ONLY on `state` (high-confidence). The
# frame's port/negotiation field NAMES below are EOS-version-variable and are
# accessed tolerantly until a live `show mlag | json` capture pins them. Same
# discipline as the bgp absence-shape VERIFY-IN-LAB above.
# ──────────────────────────────────────────────────────────────────────────
_MLAG_DISABLED = "disabled"   # the EOS token for "feature not configured" -> ABSENT
_MLAG_UP = ("active", "connected", "primary", "secondary")  # recognized operational


def _mlag_get(d: dict, *names, default=None):
    """First present key among `names` (EOS json key spellings drift by version)."""
    for n in names:
        if n in d:
            return d[n]
    return default


def arista_mlag(mlag_value: Any, as_of: float | None = None) -> Reading:
    """Turn the arista collector's `data['mlag']` into a Reading.

    Determination rides the explicit `state` string — the fourth EOS absence
    shape. `state == "disabled"` is positive ABSENT; any other populated
    operational state is PRESENT (the field is its own process indicator);
    a missing/unreadable state is UNREAD, never defaulted to fine.
    """
    ts = as_of if as_of is not None else time.time()
    key = "mlag"

    # ── 1. Did the read succeed? ─────────────────────────────────────────
    read_ok = isinstance(mlag_value, dict) and "_error" not in mlag_value
    if not read_ok:
        why = mlag_value.get("_error", "") if isinstance(mlag_value, dict) else ""
        state, reason = classify(read_ok=False, present=None)
        return Reading(key, state, payload=mlag_value, as_of=ts,
                       reason=f"{reason}: {why}" if why else reason)

    # ── 1b. EOS errors envelope (shared interpreter) — UNREAD unless a marker.
    verdict = _eos_errors_verdict(mlag_value)
    if verdict is not None:
        present, why = verdict
        state, reason = classify(read_ok=True, present=present)
        return Reading(key, state, payload=mlag_value, as_of=ts, reason=why)

    # ── 2. The status STRING decides — not emptiness, not a container walk. ─
    raw_state = _mlag_get(mlag_value, "state", "mlagState", default=None)
    if not isinstance(raw_state, str) or not raw_state.strip():
        # No state field to read -> nothing positive to assert. Unprovable -> UNREAD.
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=mlag_value, as_of=ts,
                       reason=f"{reason}: no mlag state field")
    low = raw_state.strip().lower()

    # "disabled" -> feature not configured -> ABSENT (positive). Note: substring
    # match on the exact token only, so "active/reload" never trips this and
    # "inactive/reload" (configured, transiently down) is NOT read as absence.
    if _MLAG_DISABLED in low and "reload" not in low:
        state, reason = classify(read_ok=True, present=False)
        return Reading(key, state, payload=mlag_value, as_of=ts,
                       reason=f"mlag not configured (state={raw_state!r})")

    # ── 3. PRESENT. A populated, non-disabled state IS the process indicator. ─
    recognized = any(tok in low for tok in _MLAG_UP) or "reload" in low
    # Frame = a single count of MLAG signals NOT in their healthy state, ceiling 0,
    # so a configured-but-unhealthy domain can never read green by omission. Every
    # component below is a thing that *should* be zero/up; any nonzero -> CRIT.
    # VERIFY-IN-LAB: key names (negStatus / peerLinkStatus / localIntfStatus and
    # the mlagPorts subkeys) drift across EOS versions — accessed tolerantly.
    neg = str(_mlag_get(mlag_value, "negStatus", "negotiationStatus", default="")).lower()
    peer_link = str(_mlag_get(mlag_value, "peerLinkStatus", default="")).lower()
    local_int = str(_mlag_get(mlag_value, "localIntfStatus", "localIntStatus",
                              default="")).lower()
    ports = _mlag_get(mlag_value, "mlagPorts", default={}) or {}
    if not isinstance(ports, dict):
        ports = {}

    def _portn(*names) -> int:
        v = _mlag_get(ports, *names, default=0)
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    inactive = _portn("Inactive", "inactive")
    partial = _portn("Active-partial", "activePartial")

    issues = inactive + partial
    if neg and neg != "connected":
        issues += 1
    if peer_link and peer_link not in ("up", "connected"):
        issues += 1
    if local_int and local_int not in ("up", "connected"):
        issues += 1

    frame = Frame(
        label="mlag signals not healthy",
        value=issues,
        ceiling=0,
        status=frame_status(issues, 0),
    )
    state, reason = classify(read_ok=True, present=True)
    flag = "" if recognized else " (unrecognized state, treated PRESENT: field is its own process indicator)"
    detail = (f"state={raw_state!r}, {inactive} inactive + {partial} active-partial port(s)"
              f"{flag}")
    return Reading(key, state, payload=mlag_value, frames=[frame],
                   as_of=ts, reason=f"{reason}: {detail}")


# ──────────────────────────────────────────────────────────────────────────
# §6  Fourth discriminator — arista lldp. The case where ABSENT is NOT
#     determinable from the read, and the law's honesty is the whole point.
#
# `show lldp neighbors | json` -> {"lldpNeighbors": [ {port, neighborDevice,
# neighborPort, ttl}, ... ], ...}. The payload carries NO process/admin
# indicator — there is no routerId-equivalent telling us whether the LLDP agent
# is enabled. So an EMPTY neighbor list is irreducibly ambiguous: LLDP enabled
# with nothing cabled looks identical to LLDP globally disabled. BGP could break
# that tie with a process field; here there is none in this command's output.
#
# Therefore lldp can land PRESENT (neighbors observed -> agent demonstrably
# running) or UNREAD (empty/unprovable) — but NEVER ABSENT from this read alone.
# That is not a missing branch; it is the correct epistemics. Proving lldp ABSENT
# would require a *second* read (`show lldp | json` admin-state), which is a
# manifest decision, not something to fabricate by reading emptiness as absence
# (the exact C6 hole). Recording UNREAD here keeps the door open honestly.
#
# Second property worth naming: a PRESENT lldp is legitimately FRAMELESS — there
# is no "down neighbor" state the way ospf has non-Full or bgp has non-Established
# (a neighbor is present or it is gone). The contract permits this: frames are
# forbidden OFF a PRESENT reading, never required ON one. (If a live capture
# confirms a tablesDrops counter, that becomes the one honest lldp health frame —
# VERIFY-IN-LAB; until then PRESENT carries no fabricated number.)
# ──────────────────────────────────────────────────────────────────────────
def arista_lldp(lldp_value: Any, as_of: float | None = None) -> Reading:
    """Turn the arista collector's `data['lldp']` into a Reading.

    PRESENT iff neighbors are observed; otherwise UNREAD. ABSENT is unreachable
    by construction — emptiness cannot positively prove the agent is disabled.
    """
    ts = as_of if as_of is not None else time.time()
    key = "lldp"

    # ── 1. Did the read succeed? ─────────────────────────────────────────
    read_ok = isinstance(lldp_value, dict) and "_error" not in lldp_value
    if not read_ok:
        why = lldp_value.get("_error", "") if isinstance(lldp_value, dict) else ""
        state, reason = classify(read_ok=False, present=None)
        return Reading(key, state, payload=lldp_value, as_of=ts,
                       reason=f"{reason}: {why}" if why else reason)

    # ── 1b. EOS errors envelope. A recognized inactivity marker would be the
    # ONLY positive-absence signal lldp could ever carry — but it arrives via the
    # errors envelope, not the neighbors list. If a real disabled-lldp box proves
    # EOS emits such a marker here, _eos_errors_verdict already routes it ABSENT;
    # any other error is UNREAD. (VERIFY-IN-LAB whether EOS errors on disabled lldp
    # at all, or simply returns an empty list — the empty-list path below is the
    # conservative default and stays UNREAD either way.)
    verdict = _eos_errors_verdict(lldp_value)
    if verdict is not None:
        present, why = verdict
        state, reason = classify(read_ok=True, present=present)
        return Reading(key, state, payload=lldp_value, as_of=ts, reason=why)

    # ── 2. Neighbors observed -> PRESENT. Empty/missing -> UNREAD, NOT ABSENT. ─
    neighbors = lldp_value.get("lldpNeighbors")
    if not isinstance(neighbors, list):
        # No neighbors container at all -> no structure to read. Unprovable.
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=lldp_value, as_of=ts,
                       reason=f"{reason}: no lldpNeighbors container")
    if not neighbors:
        # Empty neighbor list: enabled-but-idle is indistinguishable from disabled
        # at this read. The law refuses to call this ABSENT. UNREAD — and the
        # reason makes the ambiguity auditable instead of silently green.
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=lldp_value, as_of=ts,
                       reason=f"{reason}: zero lldp neighbors "
                              f"(agent enabled-but-idle vs disabled is unprovable here)")

    # ── 3. PRESENT, legitimately frameless (no down-neighbor concept). ───────
    state, reason = classify(read_ok=True, present=True)
    detail = f"{len(neighbors)} lldp neighbor{'' if len(neighbors)==1 else 's'} observed"
    return Reading(key, state, payload=_eos_lldp_to_contract(neighbors), as_of=ts,
                   reason=f"{reason}: {detail}")


# ──────────────────────────────────────────────────────────────────────────
# §6  Fifth discriminator — arista interfaces. The SECOND flavor of
#     unreachable-ABSENT, and distinct from lldp's.
#
# lldp can't reach ABSENT because absence is UNDETERMINABLE from the read.
# interfaces can't reach ABSENT because absence is PHYSICALLY IMPOSSIBLE: every
# box has interfaces, so an empty `interfaceStatuses` is not "this box has zero
# interfaces" (a thing that never happens) — it is a read that did not enumerate
# them. Empty -> UNREAD, never ABSENT. The capability is PRESENT-by-existence;
# the discriminator's whole job is the FRAME, not the present/absent question.
#
# Shape — confirmed against nethuds' own consumer (arista/static/index.html):
#   safe(d, 'interfaces.interfaceStatuses', {})  -> { "<name>": { ... }, ... }
#   per-interface: linkStatus ('connected' | 'disabled' | ...), description,
#                  bandwidth, interfaceType.   (UI branches ONLY on linkStatus.)
# So linkStatus is CONFIRMED; lineProtocolStatus and the exact fault tokens
# ('errdisabled'/'notconnect') are EOS-known but UI-unreferenced -> VERIFY-IN-LAB,
# accessed tolerantly. Determination rides only the confirmed fields.
#
# §7 FRAME POLICY — the open question, made concrete. From `show interfaces
# status | json` alone, with NO expected-up set, you cannot tell a down FAULT
# from an enabled-but-unused port: both read not-'connected'. Counting every
# enabled-down port CRIT would perma-redden any switch with spare ports — the
# alert-fatigue failure, the mirror image of default-to-green. So the frame here
# counts ONLY unambiguous faults (an error linkStatus, or link-up-protocol-down),
# and reports the connected / admin-disabled / not-connected breakdown as
# CONTEXT in the reason, never as alarm. Refining "which down ports are EXPECTED
# up" needs an expected-state manifest and is deferred to a frame policy (§7),
# not fabricated from status. (FRAME_INCLUDE_NOTCONNECT flips to the broader
# policy when an operator wants every enabled-down port flagged.)
# ──────────────────────────────────────────────────────────────────────────
_IFACE_UP = "connected"        # CONFIRMED token: link up / active
_IFACE_ADMIN_DOWN = "disabled" # CONFIRMED token: administratively shut (intentional)
_IFACE_FAULT = ("errdis",)     # VERIFY-IN-LAB: substring of the error link state(s)
FRAME_INCLUDE_NOTCONNECT = False  # §7 policy switch: count enabled-but-down as fault?


def arista_interfaces(intf_value: Any, as_of: float | None = None) -> Reading:
    """Turn the arista collector's `data['interfaces']` into a Reading.

    PRESENT-by-existence (a box always has interfaces); empty/missing -> UNREAD,
    never ABSENT. The frame counts only unambiguous interface faults, with the
    full up/down/disabled breakdown carried in the reason as context.
    """
    ts = as_of if as_of is not None else time.time()
    key = "interfaces"

    # ── 1. Did the read succeed? ─────────────────────────────────────────
    read_ok = isinstance(intf_value, dict) and "_error" not in intf_value
    if not read_ok:
        why = intf_value.get("_error", "") if isinstance(intf_value, dict) else ""
        state, reason = classify(read_ok=False, present=None)
        return Reading(key, state, payload=intf_value, as_of=ts,
                       reason=f"{reason}: {why}" if why else reason)

    # ── 2. Locate the interface map. Missing OR empty -> UNREAD (never ABSENT) ─
    statuses = intf_value.get("interfaceStatuses")
    if not isinstance(statuses, dict):
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=intf_value, as_of=ts,
                       reason=f"{reason}: no interfaceStatuses container")
    if not statuses:
        # A real box always enumerates SOMETHING (management at least). An empty
        # map is a non-enumerating read, NOT positive evidence of zero interfaces.
        # ABSENT is physically unreachable here -> UNREAD.
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=intf_value, as_of=ts,
                       reason=f"{reason}: interfaceStatuses empty "
                              f"(non-enumerating read; interfaces can't be ABSENT)")

    # ── 3. PRESENT-by-existence. Classify each interface off linkStatus. ─────
    up = admin_down = not_connected = faulted = 0
    for v in statuses.values():
        if not isinstance(v, dict):
            continue
        link = str(v.get("linkStatus", "")).lower()
        proto = str(v.get("lineProtocolStatus", "")).lower()  # VERIFY-IN-LAB, optional

        if link == _IFACE_ADMIN_DOWN:
            admin_down += 1
            continue                       # intentional down — never a fault
        if link == _IFACE_UP:
            # Up at link; the one unambiguous fault on an up port is protocol-down.
            if proto == "down":
                faulted += 1
            else:
                up += 1
            continue
        # Neither connected nor admin-disabled: down for some non-intentional
        # reason. NOTE: proto is naturally 'down' here (no link) — that is NOT a
        # fault, it's the definition of not-connected. Only an explicit error
        # link state is a true fault; bare not-connected is context (could be an
        # unused-but-enabled port). FRAME_INCLUDE_NOTCONNECT opts into the
        # broader policy that treats every enabled-down port as a fault.
        not_connected += 1
        is_error = any(tok in link for tok in _IFACE_FAULT)
        if is_error or FRAME_INCLUDE_NOTCONNECT:
            faulted += 1

    frame = Frame(
        label="interfaces faulted",
        value=faulted,
        ceiling=0,
        status=frame_status(faulted, 0),
    )
    state, reason = classify(read_ok=True, present=True)
    total = len(statuses)
    detail = (f"{total} interfaces: {up} up, {admin_down} admin-disabled, "
              f"{not_connected} not-connected, {faulted} faulted"
              + ("" if FRAME_INCLUDE_NOTCONNECT
                 else " (not-connected counted as context, not fault — §7)"))
    return Reading(key, state, payload=statuses, frames=[frame],
                   as_of=ts, reason=f"{reason}: {detail}")


# ──────────────────────────────────────────────────────────────────────────
# §6  Sixth discriminator — arista version. PRESENT-by-existence, FRAMELESS.
#
# A THIRD reason a PRESENT reading carries no frame: lldp is frameless because
# there is no down-neighbor concept; version is frameless because it is IDENTITY,
# not health. modelName / serialNumber / uptime / memTotal / memFree describe the
# box; none is a fault number, and there is NO device-declared threshold to read.
# Inventing a memory-utilization ceiling here would be the §7 fabrication this
# contract forbids — so version reports identity and frames nothing. (A memory-
# pressure frame is a legitimate §7 add ONCE a ceiling policy exists; until then
# memFree/memTotal stays context, never a fabricated CRIT.)
#
# ABSENT is physically unreachable (every box answers `show version`). Empty or
# unrecognized -> UNREAD, never "no version."
# Shape — confirmed against nethuds (arista/static/index.html lines 660-687):
#   modelName, serialNumber, uptime, memTotal, memFree (+ version string).
# ──────────────────────────────────────────────────────────────────────────
def arista_version(version_value: Any, as_of: float | None = None) -> Reading:
    """Turn the arista collector's `data['version']` into a Reading.

    Identity capability: PRESENT-by-existence and frameless. Empty -> UNREAD.
    """
    ts = as_of if as_of is not None else time.time()
    key = "version"

    read_ok = isinstance(version_value, dict) and "_error" not in version_value
    if not read_ok:
        # Surface WHY, always: the error mode plus a snippet of what the box
        # actually returned (the /var/core banner, a `% Invalid input`, a truncated
        # read) — so the nameplate's "identity unread" says the cause, not just that
        # it failed. Never an empty reason.
        if isinstance(version_value, dict):
            why = version_value.get("_error") or "unparseable read"
            raw = str(version_value.get("_raw", ""))
        else:
            why = f"non-dict read ({type(version_value).__name__})"
            raw = str(version_value)
        state, base = classify(read_ok=False, present=None)
        snippet = " ".join(raw.split())[:120]
        reason = f"{base}: {why}" + (f" · saw “{snippet}…”" if snippet else "")
        return Reading(key, state, payload=version_value, as_of=ts, reason=reason)

    # A box always answers version. A read with NO identifying field is a
    # non-read, not a box-without-a-version -> UNREAD (absence is unreachable).
    model = version_value.get("modelName")
    ver = version_value.get("version") or version_value.get("internalVersion")
    has_mem = "memTotal" in version_value
    if not (model or ver or has_mem):
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=version_value, as_of=ts,
                       reason=f"{reason}: no identifying version field")

    # PRESENT, frameless. memFree/memTotal carried as CONTEXT only (no ceiling).
    state, reason = classify(read_ok=True, present=True)
    bits = []
    if model:
        bits.append(str(model))
    if ver:
        bits.append(f"EOS {ver}")
    mt, mf = version_value.get("memTotal"), version_value.get("memFree")
    if isinstance(mt, (int, float)) and mt and isinstance(mf, (int, float)):
        bits.append(f"mem {100*(mt-mf)/mt:.0f}% used (context, no §7 ceiling)")
    return Reading(key, state, payload=version_value, as_of=ts,
                   reason=f"{reason}: {', '.join(bits) if bits else 'identity read'}")


# ──────────────────────────────────────────────────────────────────────────
# §6  Seventh discriminator — arista environment. A MULTI-READ capability:
#     THREE `| json` sub-reads combined, the text parser retired.
#
# `show environment all | json` is an UNCONVERTED command on real EOS — it
# returns {"errors": ["This is an unconverted command"]} (confirmed, eng-oob-1).
# So environment is read as the structured sub-commands the box DOES convert,
# each covering a DISTINCT hardware domain that lives nowhere else:
#   power       -> show environment power | json        PSU state + PSU fans/sensors
#   temperature -> show environment temperature | json  temp sensors + systemStatus
#   cooling     -> show environment cooling | json      CHASSIS fan trays + ambient
# The chassis fan trays (fanTraySlots) appear ONLY in `cooling`; power carries
# just the PSU fans. Reading power+temperature alone left a silent blind spot on
# system cooling — a real false-negative surface — so cooling is its own sub.
#
# This deletes the regex parser AND its worst failure mode (the empty skeleton
# green-on-a-non-read). JSON failure is honest: {"_error":…} or an errors
# envelope. The reference frame comes STRAIGHT FROM THE BOX — inAlertState,
# overheatThreshold, systemStatus ("temperatureOk"/"coolingOk"), PSU/fan status.
#
# STRICT completeness (the §4 law on a split read): PRESENT only if ALL sub-reads
# are structured. A partial read can't certify hardware health, so any failed sub
# -> UNREAD, never a partial green computed over the subs that happened to answer.
# The reason FRONT-LOADS which sub failed (the classify() prefix would otherwise
# bury it past a truncating printer). ABSENT is unreachable (every chassis has
# power, thermal, and cooling hardware).
# ──────────────────────────────────────────────────────────────────────────
_ENV_SUBS = ("power", "temperature", "cooling")  # all required to certify health


def _sub_structured(v: Any) -> bool:
    """A sub-read is usable iff it's a dict, carries no _error, and is not an
    EOS errors envelope (e.g. the 'unconverted command' answer)."""
    if not isinstance(v, dict) or "_error" in v:
        return False
    return _eos_errors_verdict(v) is None  # envelope present -> not structured


def _sub_why(v: Any) -> str:
    """One-token diagnosis of WHY a sub-read isn't usable — surfaced inline so the
    failure mode is visible without digging a truncated capture out of results.md.
        json_decode_failed -> read truncated mid-JSON (timeout/size)
        no_json_found      -> box returned non-JSON (unsupported? error string?)
        empty_output       -> nothing came back
        exec_failed: …     -> transport-level exception
        errors-envelope    -> EOS errors envelope (e.g. unconverted command)
    For no_json/decode failures the actual device line is appended (echo + prompt
    stripped), so 'temperature:no_json_found[% This is an unconverted command]'
    tells the whole story in one place — the project's own legibility law, applied
    to its diagnostics.
    """
    if not isinstance(v, dict):
        return f"non-dict:{type(v).__name__}"
    if "_error" in v:
        code = str(v["_error"])
        raw = v.get("_raw")
        if raw:
            # Strip the echoed command (first non-blank line) and the trailing
            # prompt (last) so the snippet is the device's actual response.
            lines = [ln for ln in str(raw).splitlines() if ln.strip()]
            body = lines[1:-1] if len(lines) > 2 else lines
            snip = " ".join(" ".join(body).split())[:50]
            if snip:
                return f"{code}[{snip}]"
        return code
    if _eos_errors_verdict(v) is not None:
        msgs = v.get("errors") or []
        return f"errors-envelope:{str(msgs[0])[:30]!r}" if msgs else "errors-envelope"
    return "unstructured"


def _bad_status(v: Any, ok=("ok",)) -> bool:
    """A status string the box reports as anything other than ok/empty."""
    s = str(v).lower()
    return bool(s) and s not in ok


def _eos_sensor_fault(s: dict) -> bool:
    """The per-sensor fault rule (hoisted from arista_environment unchanged so
    the contract translator and the frame count share ONE verdict — the widget
    colors on `fault`, and `fault` must be the same truth the frame counted)."""
    if s.get("inAlertState") is True:
        return True
    if _bad_status(s.get("hwStatus", "ok")):
        return True
    cur, crit = s.get("currentTemperature"), s.get("criticalThreshold")
    return isinstance(cur, (int, float)) and isinstance(crit, (int, float)) and cur >= crit


def _eos_env_to_groups(power: dict, temp: dict, cooling: dict) -> dict[str, Any]:
    """The three EOS sub-reads -> the RATIFIED environment deep-cap contract
    ({sensors, fans, power} of {name, status, fault} + optional enrichments —
    contract.py). This is the Arista side of the amendment the juniper shape
    forced: EOS moves from raw-payload to a translator INTO the shared shape,
    exactly the move every flat cap already made. Optional fields are emitted
    ONLY when the box measured them — never fabricated to look complete.

    `status` carries the box's word verbatim; `fault` carries the SAME verdict
    the frame counted (_eos_sensor_fault / _bad_status), computed once here so
    no vendor vocabulary ever reaches the widget."""
    def _numopt(rec: dict, key: str, v: Any) -> None:
        if isinstance(v, (int, float)):
            rec[key] = v

    sensors: list[dict] = []
    chassis_sensors = list(temp.get("tempSensors") or [])
    for slot in (temp.get("powerSupplySlots") or []):
        chassis_sensors.extend(slot.get("tempSensors") or [])
    for s in chassis_sensors:
        if not isinstance(s, dict):
            continue
        rec: dict[str, Any] = {
            "name": s.get("name") or "sensor",
            "status": str(s.get("hwStatus")
                          or ("alert" if s.get("inAlertState") else "ok")),
            "fault": _eos_sensor_fault(s),
        }
        _numopt(rec, "tempC", s.get("currentTemperature"))
        _numopt(rec, "critC", s.get("criticalThreshold"))
        _numopt(rec, "warnC", s.get("overheatThreshold"))
        sensors.append(rec)

    fans: list[dict] = []
    psus = power.get("powerSupplies") or {}
    for tray in (cooling.get("fanTraySlots") or []):
        if not isinstance(tray, dict):
            continue
        tray_fans = [f for f in (tray.get("fans") or []) if isinstance(f, dict)]
        if not tray_fans:   # a fanless tray record still shows its own health
            fans.append({"name": str(tray.get("label") or "tray"),
                         "status": str(tray.get("status") or ""),
                         "fault": _bad_status(tray.get("status", "ok"))})
            continue
        for f in tray_fans:
            rec = {"name": str(f.get("label") or tray.get("label") or "fan"),
                   "status": str(f.get("status") or ""),
                   "fault": _bad_status(f.get("status", "ok"))}
            _numopt(rec, "speedPct", f.get("actualSpeed") or f.get("speed"))
            fans.append(rec)
    for psu in psus.values():                      # PSU fans (health-only rows)
        if not isinstance(psu, dict):
            continue
        for fname, f in (psu.get("fans") or {}).items():
            if isinstance(f, dict):
                fans.append({"name": str(fname),
                             "status": str(f.get("status") or ""),
                             "fault": _bad_status(f.get("status", "ok"))})

    power_recs: list[dict] = []
    for pid, psu in psus.items():
        if not isinstance(psu, dict):
            continue
        rec = {"name": f"PSU-{pid}",
               "status": str(psu.get("state") or ""),
               "fault": _bad_status(psu.get("state", "ok"))}
        if psu.get("modelName"):
            rec["model"] = str(psu["modelName"])
        _numopt(rec, "watts", psu.get("outputPower"))
        _numopt(rec, "capacityW", psu.get("capacity"))
        _numopt(rec, "ampsIn", psu.get("inputCurrent"))
        _numopt(rec, "ampsOut", psu.get("outputCurrent"))
        power_recs.append(rec)
        for sname, s in (psu.get("tempSensors") or {}).items():   # PSU sensors
            if isinstance(s, dict):                               # (status-only)
                sensors.append({"name": str(sname),
                                "status": str(s.get("status") or ""),
                                "fault": _bad_status(s.get("status", "ok"))})

    out: dict[str, Any] = {"sensors": sensors, "fans": fans, "power": power_recs}
    amb = cooling.get("ambientTemperature")
    if isinstance(amb, (int, float)):
        out["ambientC"] = amb
    if cooling.get("systemStatus"):
        out["coolingStatus"] = str(cooling["systemStatus"])
    if temp.get("systemStatus"):
        out["tempStatus"] = str(temp["systemStatus"])
    return out


def arista_environment(env_value: Any, as_of: float | None = None) -> Reading:
    """Combine power + temperature + cooling sub-reads into one hardware-health
    Reading. PRESENT only when all three are structured; frame counts faults the
    BOX itself flags. Partial/failed read -> UNREAD (front-loaded which sub).
    ABSENT is unreachable.
    """
    ts = as_of if as_of is not None else time.time()
    key = "environment"

    if not isinstance(env_value, dict) or "_error" in env_value:
        why = env_value.get("_error", "") if isinstance(env_value, dict) else ""
        state, reason = classify(read_ok=False, present=None)
        return Reading(key, state, payload=env_value, as_of=ts,
                       reason=f"{reason}: {why}" if why else reason)

    subs = {name: env_value.get(name) for name in _ENV_SUBS}
    ok = {name: _sub_structured(v) for name, v in subs.items()}

    # ── STRICT: any unread domain -> UNREAD. Front-load WHICH sub failed AND WHY
    # (its _error code) so the failure mode survives a truncating printer — no
    # need to dig the raw out of a capped results.md capture.
    if not all(ok.values()):
        diag = " ".join(f"{n}:{_sub_why(subs[n])}" for n in _ENV_SUBS if not ok[n])
        got = ",".join(n for n in _ENV_SUBS if ok[n]) or "none"
        state, _ = classify(read_ok=True, present=None)
        return Reading(key, state, payload=env_value, as_of=ts,
                       reason=f"env incomplete — {diag} (got: {got})")

    power, temp, cooling = subs["power"], subs["temperature"], subs["cooling"]
    faults = 0

    # ── Temperature: the box's own per-sensor verdict + systemStatus. ────────
    if _bad_status(temp.get("systemStatus"), ok=("temperatureok", "ok")):
        faults += 1

    # per-sensor rule hoisted to _eos_sensor_fault (module level) so the frame
    # count and the contract translator share ONE verdict — logic unchanged.
    sensors = list(temp.get("tempSensors") or [])
    for slot in (temp.get("powerSupplySlots") or []):
        sensors.extend(slot.get("tempSensors") or [])
    hot = sum(1 for s in sensors if isinstance(s, dict) and _eos_sensor_fault(s))
    faults += hot

    # ── Power: PSU state + each PSU's own fans/tempSensors. ──────────────────
    psus = power.get("powerSupplies") or {}
    psu_failed = psu_fan_failed = 0
    for psu in psus.values():
        if not isinstance(psu, dict):
            continue
        if _bad_status(psu.get("state", "ok")):
            psu_failed += 1
        for fan in (psu.get("fans") or {}).values():
            if isinstance(fan, dict) and _bad_status(fan.get("status", "ok")):
                psu_fan_failed += 1
        for s in (psu.get("tempSensors") or {}).values():
            if isinstance(s, dict) and _bad_status(s.get("status", "ok")):
                faults += 1
    faults += psu_failed + psu_fan_failed

    # ── Cooling: CHASSIS fan trays (live only here) + cooling systemStatus. ──
    # PSU fans are already counted from `power`; here we count the system fan
    # trays and the cooling-level verdict, no double-count.
    if _bad_status(cooling.get("systemStatus"), ok=("coolingok", "ok")):
        faults += 1
    tray_failed = tray_fan_failed = 0
    for tray in (cooling.get("fanTraySlots") or []):
        if not isinstance(tray, dict):
            continue
        if _bad_status(tray.get("status", "ok")):
            tray_failed += 1
        for fan in (tray.get("fans") or []):
            if isinstance(fan, dict) and _bad_status(fan.get("status", "ok")):
                tray_fan_failed += 1
    faults += tray_failed + tray_fan_failed

    frame = Frame(
        label="environment faults",
        value=faults,
        ceiling=0,
        status=frame_status(faults, 0),
    )
    state, reason = classify(read_ok=True, present=True)
    ambient = cooling.get("ambientTemperature")
    n_trays = len(cooling.get("fanTraySlots") or [])
    detail = (f"{len(psus)} psus ({psu_failed} failed, {psu_fan_failed} fan-bad), "
              f"{len(sensors)} temp sensors ({hot} in/over alert), "
              f"{n_trays} fan trays ({tray_failed + tray_fan_failed} bad), "
              f"ambient={ambient if ambient is not None else '?'}C, "
              f"temp={temp.get('systemStatus','?')}/cool={cooling.get('systemStatus','?')}")
    # RATIFICATION AMENDMENT (contract.py "environment"): the PRESENT payload
    # is the shared deep-cap shape, no longer raw EOS — the same
    # translator-into-contract move every flat cap made, forced by the second
    # real shape (juniper). State, frame, and reason logic above are untouched;
    # only what a PRESENT consumer sees changed. Non-PRESENT paths still carry
    # env_value so the failure evidence stays raw and auditable.
    return Reading(key, state, payload=_eos_env_to_groups(power, temp, cooling),
                   frames=[frame], as_of=ts, reason=f"{reason}: {detail}")


# ──────────────────────────────────────────────────────────────────────────
# §6  Eighth discriminator — arista routes. SAME vrfs shape as ospf/bgp,
#     OPPOSITE absence semantics; nhd-grade RIB composition.
#
# `show ip route summary | json` -> vrfs.default.{totalRoutes, connected,
# internal, static, bgpCounts, ospfCounts, maskLen}. It walks the exact vrfs
# structure ospf and bgp do — but the absence rule INVERTS. For a routing
# PROTOCOL, an empty vrfs map is positive ABSENT (no process). For the RIB
# ITSELF, a live box can never have zero routing tables, so an empty/missing vrfs
# is a non-read -> UNREAD, never ABSENT. Same shape, opposite epistemics.
#
# COMPOSITION (mirrors nhd's RIB SUMMARY panel): the reading surfaces the RIB
# broken down by source — connected / ospf / bgp / internal / static — plus the
# prefix-length distribution. ospfCounts and bgpCounts are SUMMED rather than
# hardcoding each sub-type (ospfIntraArea, ospfExternal2, bgpInternal, …), so the
# total survives EOS adding/renaming sub-counts. The payload keeps the full
# default-VRF dict so a consumer (HUD, AI) gets every sub-count.
#
# FRAME: the one non-fabricated single-poll fault is a DEGENERATE RIB —
# totalRoutes positively zero (an up box with not even connected routes is
# broken, and the box is asserting it). "Connected-only" (learned == 0) is a soft
# signal surfaced in the reason but NOT framed CRIT: a static-only edge is a
# legitimate connected-only box, so "should this have learned routes" needs
# context (§7), not a fabricated alarm. nhd dims zero counts; it never faults them.
# ──────────────────────────────────────────────────────────────────────────
def _sum_counts(d: Any) -> int:
    """Sum the numeric values of an EOS counts sub-dict (ospfCounts/bgpCounts),
    tolerant of new/renamed sub-types — the total is what the RIB summary shows."""
    if not isinstance(d, dict):
        return 0
    return int(sum(v for v in d.values() if isinstance(v, (int, float))))


def arista_routes(routes_value: Any, as_of: float | None = None) -> Reading:
    """Turn the arista collector's `data['routes']` into a Reading.

    PRESENT-by-existence via the same vrfs walk as ospf/bgp, but empty vrfs is
    UNREAD here (the RIB can't be ABSENT). Surfaces the full nhd-style RIB
    composition; frame flags only a degenerate (zero-route) RIB.
    """
    ts = as_of if as_of is not None else time.time()
    key = "routes"

    read_ok = isinstance(routes_value, dict) and "_error" not in routes_value
    if not read_ok:
        why = routes_value.get("_error", "") if isinstance(routes_value, dict) else ""
        state, reason = classify(read_ok=False, present=None)
        return Reading(key, state, payload=routes_value, as_of=ts,
                       reason=f"{reason}: {why}" if why else reason)

    vrfs = routes_value.get("vrfs")
    # NOTE the inversion: unlike ospf/bgp, empty/missing vrfs is UNREAD, NOT
    # ABSENT. A live box always has a RIB; an empty enumeration is a non-read.
    if not isinstance(vrfs, dict) or not vrfs:
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=routes_value, as_of=ts,
                       reason=f"{reason}: no vrfs/RIB enumerated "
                              f"(routes can't be ABSENT — a box always has a table)")
    if "default" not in vrfs or not isinstance(vrfs["default"], dict):
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=routes_value, as_of=ts,
                       reason=f"{reason}: default-vrf RIB not found")

    default = vrfs["default"]
    total = default.get("totalRoutes")
    if not isinstance(total, (int, float)):
        # RIB present but no count to read -> can't assert health. UNREAD.
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=routes_value, as_of=ts,
                       reason=f"{reason}: RIB present but totalRoutes unreadable")
    total = int(total)

    # ── Composition (nhd RIB SUMMARY): connected / ospf / bgp / internal / static
    connected = int(default.get("connected", 0) or 0)
    internal = int(default.get("internal", 0) or 0)
    static = int(default.get("static", 0) or 0) + int(default.get("staticNexthopGroup", 0) or 0)
    ospf = _sum_counts(default.get("ospfCounts"))
    bgp = _sum_counts(default.get("bgpCounts"))
    learned = ospf + bgp + internal          # routes from a protocol, not connected/static

    # Prefix-length distribution (top contributors), e.g. "/24:312 /32:98".
    mask_len = default.get("maskLen")
    dist = ""
    if isinstance(mask_len, dict) and mask_len:
        top = sorted(((str(m), int(c)) for m, c in mask_len.items()
                      if isinstance(c, (int, float))), key=lambda kv: kv[1], reverse=True)[:6]
        dist = " ".join(f"/{m}:{c}" for m, c in top)

    # ── FRAME: degenerate (zero-route) RIB only. ─────────────────────────────
    degenerate = 1 if total == 0 else 0
    frame = Frame(
        label="empty routing table",
        value=degenerate,
        ceiling=0,
        status=frame_status(degenerate, 0),
    )
    state, reason = classify(read_ok=True, present=True)
    comp = (f"{total} routes — {connected} connected, {ospf} ospf, {bgp} bgp, "
            f"{internal} internal, {static} static")
    flags = []
    if degenerate:
        flags.append("DEGENERATE RIB")
    elif learned == 0:
        flags.append("connected-only (no learned routes)")   # soft signal, not CRIT
    detail = comp + (f"; {', '.join(flags)}" if flags else "") + (f"; prefix {dist}" if dist else "")
    return Reading(key, state, payload=default, frames=[frame],
                   as_of=ts, reason=f"{reason}: {detail}")


# ──────────────────────────────────────────────────────────────────────────
# §6  Ninth discriminator — arista transceivers. OPTIC INVENTORY, not optic
#     health. From `show inventory | json`.xcvrSlots — the SAME command surface
#     that carries card/PSU/fan inventory; we read only the transceiver slots.
#
# EPISTEMICS mirror `interfaces`: a switch always has an inventory, so the read
# either ANSWERS or it doesn't. Missing/unreadable xcvrSlots -> UNREAD (didn't
# answer). A populated-count of ZERO is a box with no optics installed — the
# inventory answered, it just lists empty slots — which is PRESENT, NOT ABSENT.
# A slot whose `mfgName == "Not Present"` is an empty cage, not a fault. ABSENT
# is therefore unreachable (you cannot positively-absent a chassis's inventory).
#
# FRAMELESS (like lldp/version): an inventory read asserts no health signal.
# There is nothing the BOX is flagging as wrong, so there is nothing to count;
# framing an empty cage would be the fabricated alarm the law forbids. Optic
# HEALTH — rx/tx dBm, module temperature, DOM alarms — is a SEPARATE measurement
# read (`show interfaces transceiver | json`); if that lane lands it carries its
# own frame and combines here as a second sub-read, exactly the power/temp/
# cooling pattern in arista_environment.
#
# PAYLOAD = the raw xcvrSlots dict (the box's own sub-object); the widget derives
# populated/total for presentation. Consistent with environment (emit the raw
# sub-structure) and routes (emit the default-VRF dict) — the discriminator
# judges state, the widget renders detail.
# ──────────────────────────────────────────────────────────────────────────
def _xcvr_populated(slot: Any) -> bool:
    """A transceiver slot is populated iff it's a dict carrying a real mfgName
    (EOS writes the literal 'Not Present' for an empty cage)."""
    return (isinstance(slot, dict)
            and bool(slot.get("mfgName"))
            and str(slot.get("mfgName")).lower() != "not present")


def arista_transceivers(inv_value: Any, as_of: float | None = None) -> Reading:
    """Turn `show inventory | json` into a transceiver-inventory Reading.

    PRESENT-by-existence via xcvrSlots (zero populated is still PRESENT — the
    inventory answered); missing/unreadable xcvrSlots -> UNREAD; ABSENT is
    unreachable (a chassis inventory can't be positively absent). Frameless.
    """
    ts = as_of if as_of is not None else time.time()
    key = "transceivers"

    read_ok = isinstance(inv_value, dict) and "_error" not in inv_value
    if not read_ok:
        why = inv_value.get("_error", "") if isinstance(inv_value, dict) else ""
        state, reason = classify(read_ok=False, present=None)
        return Reading(key, state, payload=inv_value, as_of=ts,
                       reason=f"{reason}: {why}" if why else reason)

    xcvr = inv_value.get("xcvrSlots")
    if not isinstance(xcvr, dict):
        # Inventory answered, but the optic-slot structure isn't there to read.
        # Unprovable as either present or absent -> UNREAD (the law), never green.
        state, _ = classify(read_ok=True, present=None)
        return Reading(key, state, payload=inv_value, as_of=ts,
                       reason="no xcvrSlots in inventory")

    populated = sum(1 for s in xcvr.values() if _xcvr_populated(s))
    total = len(xcvr)

    # Zero populated is PRESENT (a legitimate box with no optics), not ABSENT.
    state, reason = classify(read_ok=True, present=True)
    return Reading(key, state, payload=xcvr, as_of=ts,
                   reason=f"{reason}: {populated}/{total} slots populated")


# ──────────────────────────────────────────────────────────────────────────
# §6  Tenth discriminator — arista proc (COMPUTE). CPU + process table from
#     `show processes top once | json` — the old collector `proc` read, wired.
#     cpuInfo carries the top-style `%Cpu(s)` line; processes is pid-keyed.
#
# EPISTEMICS mirror interfaces/version/transceivers: a live box always answers
# `show processes top`, so the read ANSWERS or it doesn't. No cpuInfo -> UNREAD;
# ABSENT is unreachable (you can't positively-absent the process table).
#
# FRAME — the project's first genuine THRESHOLD frame (contrast version, which
# leaves memory frameless for want of a box-declared ceiling; and environment,
# whose ceiling-0 frames count box-DECLARED faults). CPU% is naturally bounded
# [0,100], so the frame value is box-reported truth (used = 100 - idle) and the
# ceiling is the true max (100), NOT a fabricated threshold. The WARN/CRIT
# *status*, however, is TOOL POLICY — EOS declares no CPU threshold — so the
# bands below are named, tunable, and honestly not box truth. This is exactly
# the §7 open question frame_status() already flags; drop status to always-OK
# for strict box-truth, or tune the bands for the deployment.
#
# MEM is deliberately NOT read here. memTotal/memFree belong to `version` (one
# cap = one command). A COMPUTE widget that wants a mem donut takes it from the
# version payload the shell already carries, or from this command's own mem
# summary if gear confirms one — NEVER by a cross-cap join into version (§7).
#
# PAYLOAD = the raw proc dict (cpuInfo + processes + whatever else the box
# returns); the widget derives the top process and renders the CPU breakdown.
# ──────────────────────────────────────────────────────────────────────────
_CPU_WARN = 70.0   # TOOL POLICY (not box-declared). Tune per deployment, or drop
_CPU_CRIT = 85.0   # the status to always-OK for strict box-truth (§7).


def _cpu_used_pct(cpu_info: Any) -> float | None:
    """Used CPU% from the top-style `%Cpu(s)` line: used = 100 - idle. Tolerant
    of the '%Cpu(s)' wrapper key and of a flat cpuInfo dict."""
    if not isinstance(cpu_info, dict):
        return None
    line = cpu_info.get("%Cpu(s)")
    if not isinstance(line, dict):
        line = cpu_info
    idle = line.get("idle")
    if not isinstance(idle, (int, float)):
        return None
    return max(0.0, min(100.0, 100.0 - float(idle)))


def arista_proc(proc_value: Any, as_of: float | None = None) -> Reading:
    """Turn `show processes top once | json` into a COMPUTE Reading.

    PRESENT-by-existence via cpuInfo; no readable CPU line -> UNREAD; ABSENT
    unreachable. Frame = CPU utilization (value=used%, ceiling=100); WARN/CRIT
    status is tunable tool policy. Payload is the raw proc dict.
    """
    ts = as_of if as_of is not None else time.time()
    key = "proc"

    read_ok = isinstance(proc_value, dict) and "_error" not in proc_value
    if not read_ok:
        why = proc_value.get("_error", "") if isinstance(proc_value, dict) else ""
        state, reason = classify(read_ok=False, present=None)
        return Reading(key, state, payload=proc_value, as_of=ts,
                       reason=f"{reason}: {why}" if why else reason)

    used = _cpu_used_pct(proc_value.get("cpuInfo"))
    if used is None:
        # Answered, but no CPU line to read -> non-read (unprovable) -> UNREAD.
        state, _ = classify(read_ok=True, present=None)
        return Reading(key, state, payload=proc_value, as_of=ts,
                       reason="no cpuInfo/%Cpu(s) idle field")

    status = (Status.CRIT if used >= _CPU_CRIT
              else Status.WARN if used >= _CPU_WARN else Status.OK)
    frame = Frame(label="cpu utilization", value=round(used, 1),
                  ceiling=100, status=status)
    n_proc = len(proc_value.get("processes") or {})
    state, reason = classify(read_ok=True, present=True)
    return Reading(key, state, payload=proc_value, frames=[frame], as_of=ts,
                   reason=f"{reason}: cpu {used:.1f}% used, {n_proc} processes")


# ──────────────────────────────────────────────────────────────────────────
# EOS → contract translators — the vendor seam (Note 05 §3/§4).
#
# The PRESENT payload leaves a discriminator through a translator that emits the
# cross-vendor contract shape (core/contract.py). For EOS these are near-identity
# — the "eos being mostly cosmetic" bet paying off: the contract is modeled on
# the EOS shape, so the gear-proven widgets never move. These functions are what
# a second vendor REPLACES, not what it edits: juniper.py will carry
# `_junos_*_to_contract` mapping XML into the SAME names. They sit here beside the
# Arista discriminators today; they move to arista.py at the §8 package split.
# Translators touch the PRESENT payload ONLY — absence is decided upstream,
# per-vendor, and never routes through here.
# ──────────────────────────────────────────────────────────────────────────
def _eos_bgp_to_contract(peers: dict) -> list[dict]:
    """EOS `vrfs.default.peers` {addr: {...}} -> contract records.

    peerAddress is injected from the map key; peerState / description /
    prefixReceived ride along from EOS as-is (cosmetic). This is the only
    non-identity flat translator — EOS keys peers by address, the contract lists
    them — and it is exactly the shaping that used to live inline in the return.
    """
    return [{**p, "peerAddress": addr} for addr, p in peers.items()]


def _eos_ospf_to_contract(neighbors: list) -> list[dict]:
    """EOS `ospfNeighborEntries` already carry routerId / adjacencyState /
    interfaceName — those pass through untouched (the contract triple needs
    no work). The seam now ALSO does its first deliberate work: aliasing EOS
    spellings onto the CONVERGED extras vocabulary the widget reads
    vendor-blind (Note 07 debt 3 — neighborAddress / drState / area /
    priority / upTime). ADDITIVE ONLY: every original EOS field still rides
    along; aliases are added, never moved, so nothing downstream that read
    the raw spelling breaks.

    VERIFY-IN-LAB (one live `show ip ospf neighbor | json` capture): the EOS
    spellings below are the documented eAPI names, accessed tolerantly —
    interfaceAddress (top-level), drState (top-level), details.areaId,
    details.stateTime. An unrecognized spelling simply yields no alias and
    the widget dashes the column; a capture that reveals a different name is
    a one-line tuple edit here."""
    out: list[dict] = []
    for n in neighbors:
        if not isinstance(n, dict):
            continue
        rec = dict(n)                          # identity, preserved whole
        det = n.get("details") if isinstance(n.get("details"), dict) else {}
        if "neighborAddress" not in rec:
            v = n.get("interfaceAddress") or det.get("interfaceAddress")
            if v:
                rec["neighborAddress"] = v
        if "drState" not in rec:
            v = det.get("drState")             # top-level spelling already matches
            if v:
                rec["drState"] = v
        if "area" not in rec:
            v = n.get("areaId") or det.get("areaId")
            if v is not None:
                rec["area"] = str(v)
        if "upTime" not in rec:
            v = det.get("stateTime")
            if v:
                rec["upTime"] = str(v)
        # priority: EOS spelling already IS the converged name; rides via identity.
        out.append(rec)
    return out


def _eos_lldp_to_contract(neighbors: list) -> list[dict]:
    """EOS `lldpNeighbors` already carry port / neighborDevice / neighborPort.
    Identity; same seam role as ospf."""
    return list(neighbors)


# ──────────────────────────────────────────────────────────────────────────
# The Arista (vendor, cap) -> discriminator map. Note 05 §2a: this lives WITH
# the vendor whose evidence its comments document — the absence-shape notes are
# Arista's, not core's. The registry (reading.py) assembles the global table by
# importing each vendor's map. Add a cap = add a line here.
# ──────────────────────────────────────────────────────────────────────────
ARISTA_DISCRIMINATORS: dict[tuple[str, str], Discriminator] = {
    ("arista", "ospf"): arista_ospf,    # absence shape: empty container
    ("arista", "bgp"):  arista_bgp,     # absence shape: errors envelope + process indicator
    ("arista", "mlag"): arista_mlag,    # absence shape: explicit status string ("disabled")
    ("arista", "lldp"): arista_lldp,    # absence: NOT determinable from this read (-> never ABSENT)
    ("arista", "interfaces"): arista_interfaces,  # absence: physically impossible (-> never ABSENT)
    ("arista", "version"): arista_version,         # PRESENT-by-existence, frameless (identity)
    ("arista", "environment"): arista_environment, # parsed-text skeleton guard; richest frame
    ("arista", "routes"): arista_routes,           # vrfs shape, INVERTED absence (RIB can't be ABSENT)
    ("arista", "transceivers"): arista_transceivers,  # optic inventory: never ABSENT (like interfaces), frameless
    ("arista", "proc"): arista_proc,               # COMPUTE: cpu/processes; never ABSENT; CPU%-vs-100 frame (status = tunable policy)
}


def run_selftests() -> None:
    def _entry(rid, st, intf="Ethernet1"):
        return {"routerId": rid, "adjacencyState": st, "interfaceName": intf,
                "details": {"areaId": "0.0.0.0"}}

    fixtures: list[tuple[str, Any]] = [
        # PRESENT: process up, 3 neighbors, one stuck in 2-Way -> down=1, CRIT
        ("present / one down", {
            "vrfs": {"default": {"instList": {"1": {"ospfNeighborEntries": [
                _entry("10.0.0.1", "full"),
                _entry("10.0.0.2", "full"),
                _entry("10.0.0.3", "2-Way"),
            ]}}}}
        }),
        # PRESENT: process up, all Full -> down=0, OK
        ("present / all full", {
            "vrfs": {"default": {"instList": {"1": {"ospfNeighborEntries": [
                _entry("10.0.0.1", "full"),
            ]}}}}
        }),
        # ABSENT: box answered, valid empty instList -> no process
        ("absent / empty instList", {"vrfs": {"default": {"instList": {}}}}),
        # UNREAD: collector reported a command/socket error
        ("unread / collector error", {"_error": "Timeout: read timed out"}),
        # UNREAD: collector caught a JSON decode failure
        ("unread / json decode", {"_raw": "% bad", "_error": "json_decode_failed"}),
        # ABSENT (gear-confirmed, eng-tor-3 static-only): empty vrfs map -> no ospf
        # in the only VRF. A THIRD absence shape, distinct from BGP's envelope.
        ("absent / empty vrfs map", {"vrfs": {}}),
        # UNREAD (the important one): well-formed dict, no vrfs container at all.
        # The UI's safe() would default this to {} and paint it ABSENT-looking.
        # The contract refuses: structureless -> absence unproven -> UNREAD.
        ("unread / no vrfs container", {"routerStatus": "ok"}),
    ]

    print(f"{'fixture':<28} {'STATE':<8} {'FRAME':<22} reason")
    print("-" * 92)
    for name, val in fixtures:
        r = arista_ospf(val)
        fr = (f"{r.frames[0].label}={r.frames[0].value} "
              f"[{r.frames[0].status}]") if r.frames else "—"
        print(f"{name:<28} {str(r.state):<8} {fr:<22} {r.reason}")

    # And prove the invariant bites: you cannot smuggle a frame onto a non-PRESENT.
    print("\ninvariant check:", end=" ")
    try:
        Reading("ospf", State.ABSENT, frames=[Frame("x", 0, 0, Status.OK)])
        print("FAILED — frame allowed on ABSENT (contract broken)")
    except ValueError as e:
        print(f"held — {e}")

    # ── BGP: the absence question that does NOT mirror OSPF ──────────────────
    bgp_fixtures: list[tuple[str, Any]] = [
        # PRESENT: process up, 2 peers, one Active -> down=1, CRIT
        ("present / one down", {"vrfs": {"default": {
            "routerId": "10.0.0.1", "asn": "65000", "peers": {
                "172.16.7.4": {"peerState": "Established"},
                "172.16.7.5": {"peerState": "Active"}}}}}),
        # PRESENT (the load-bearing BGP case): router bgp configured, ZERO peers.
        # Must NOT be ABSENT — empty peers + a live process is up-but-idle.
        ("present / configured, no peers", {"vrfs": {"default": {
            "routerId": "10.0.0.1", "asn": "65000", "peers": {}}}}),
        # ABSENT: the CONFIRMED live shape — EOS errors envelope, inactive.
        ("absent / eos 'BGP inactive'", {"errors": ["BGP inactive"]}),
        # UNREAD (C6 guard): an errors envelope that is NOT an inactivity marker
        # must stay UNREAD, never be fabricated into ABSENT.
        ("unread / eos other error", {"errors": ["Some transient failure"]}),
        # ABSENT: empty peers AND no process indicator -> positively no bgp.
        ("absent / no process", {"vrfs": {"default": {
            "routerId": "0.0.0.0", "asn": "0", "peers": {}}}}),
        # ABSENT: vrfs present but EMPTY -> box enumerated BGP contexts, none.
        ("absent / empty vrfs", {"vrfs": {}}),
        # UNREAD: no vrfs container, no errors envelope -> no structure to read.
        ("unread / no vrfs", {"notes": "bgp not configured"}),
        # UNREAD: vrfs has entries but no default scope.
        ("unread / no default scope", {"vrfs": {"mgmt": {"peers": {}}}}),
        # UNREAD: read failed.
        ("unread / collector error", {"_error": "Timeout"}),
    ]
    print("\nbgp discriminator (empty peers != absent — process indicator decides)")
    print(f"{'fixture':<32} {'STATE':<8} {'FRAME':<22} reason")
    print("-" * 96)
    for name, val in bgp_fixtures:
        r = arista_bgp(val)
        fr = (f"{r.frames[0].label}={r.frames[0].value} "
              f"[{r.frames[0].status}]") if r.frames else "—"
        print(f"{name:<32} {str(r.state):<8} {fr:<22} {r.reason}")

    # ── MLAG: absence is an explicit status string, NOT emptiness ────────────
    mlag_fixtures: list[tuple[str, Any]] = [
        # PRESENT / healthy: active, connected, peer-link up, all ports active-full.
        ("present / active healthy", {"state": "active", "negStatus": "connected",
            "peerLinkStatus": "up", "localIntfStatus": "up",
            "mlagPorts": {"Inactive": 0, "Active-partial": 0, "Active-full": 4}}),
        # PRESENT / unhealthy: configured & active, but a port stuck active-partial
        # and peer-link down -> issues>0 -> CRIT. Must NOT read green by omission.
        ("present / active, degraded", {"state": "active", "negStatus": "connected",
            "peerLinkStatus": "down", "localIntfStatus": "up",
            "mlagPorts": {"Inactive": 1, "Active-partial": 1, "Active-full": 2}}),
        # PRESENT (the C6 trap): configured, transiently down inside reload-delay.
        # "inactive/reload" contains 'inactive' but MUST NOT be read as absence.
        ("present / inactive-reload", {"state": "Inactive/Reload (0:03:00 left)",
            "negStatus": "connecting", "peerLinkStatus": "up", "localIntfStatus": "up",
            "mlagPorts": {"Inactive": 0, "Active-partial": 0, "Active-full": 0}}),
        # ABSENT: the explicit not-configured token. The fourth EOS absence shape.
        ("absent / disabled", {"state": "disabled"}),
        # UNREAD: well-formed dict, no state field -> nothing positive to read.
        ("unread / no state field", {"domainId": "core", "peerAddress": "10.0.0.2"}),
        # UNREAD: read failed.
        ("unread / collector error", {"_error": "Timeout"}),
    ]
    print("\nmlag discriminator (absence is the explicit token 'disabled', "
          "not emptiness; 'inactive/reload' is PRESENT)")
    print(f"{'fixture':<28} {'STATE':<8} {'FRAME':<26} reason")
    print("-" * 104)
    for name, val in mlag_fixtures:
        r = arista_mlag(val)
        fr = (f"{r.frames[0].label}={r.frames[0].value} "
              f"[{r.frames[0].status}]") if r.frames else "—"
        print(f"{name:<28} {str(r.state):<8} {fr:<26} {r.reason}")

    # ── LLDP: ABSENT is unreachable — empty neighbors can't prove disabled ───
    lldp_fixtures: list[tuple[str, Any]] = [
        # PRESENT, legitimately FRAMELESS: neighbors observed, no down-neighbor concept.
        ("present / two neighbors", {"lldpNeighbors": [
            {"port": "Ethernet1", "neighborDevice": "spine1", "neighborPort": "Ethernet1"},
            {"port": "Ethernet2", "neighborDevice": "spine2", "neighborPort": "Ethernet1"}]}),
        # UNREAD (the whole point): zero neighbors. Enabled-but-idle is
        # indistinguishable from disabled here -> NOT ABSENT. Never green.
        ("unread / zero neighbors", {"lldpNeighbors": []}),
        # UNREAD: no neighbors container at all.
        ("unread / no container", {"tablesInserts": 0}),
        # UNREAD: read failed.
        ("unread / collector error", {"_error": "Timeout"}),
    ]
    print("\nlldp discriminator (PRESENT or UNREAD only — ABSENT is unreachable "
          "from this read; PRESENT is frameless)")
    print(f"{'fixture':<28} {'STATE':<8} {'FRAME':<10} reason")
    print("-" * 92)
    lldp_states = set()
    for name, val in lldp_fixtures:
        r = arista_lldp(val)
        lldp_states.add(str(r.state))
        fr = (f"{r.frames[0].label}={r.frames[0].value}") if r.frames else "—"
        print(f"{name:<28} {str(r.state):<8} {fr:<10} {r.reason}")
    assert "ABSENT" not in lldp_states, "lldp must never reach ABSENT from neighbors alone"
    print("  ✓ lldp never reached ABSENT (absence is not determinable from this read)")

    # ── INTERFACES: PRESENT-by-existence; ABSENT physically unreachable ──────
    def _if(link, proto="up"):
        return {"linkStatus": link, "lineProtocolStatus": proto,
                "description": "", "bandwidth": 0}
    iface_fixtures: list[tuple[str, Any]] = [
        # PRESENT / clean: mix of up, admin-disabled, and unused not-connected.
        # not-connected is CONTEXT, not fault -> frame stays 0/OK (no alert fatigue).
        ("present / clean (spare ports)", {"interfaceStatuses": {
            "Ethernet1": _if("connected"), "Ethernet2": _if("connected"),
            "Ethernet3": _if("disabled", "down"), "Ethernet9": _if("notconnect", "down"),
            "Management1": _if("connected")}}),
        # PRESENT / faulted: an errdisabled port and a link-up-protocol-down port
        # -> faulted=2 -> CRIT. The unambiguous faults, no false positives.
        ("present / faults present", {"interfaceStatuses": {
            "Ethernet1": _if("connected"), "Ethernet4": _if("errdisabled", "down"),
            "Ethernet5": _if("connected", "down")}}),
        # UNREAD: empty map -> non-enumerating read, NOT zero interfaces (impossible).
        ("unread / empty map (not absent)", {"interfaceStatuses": {}}),
        # UNREAD: no container.
        ("unread / no container", {"foo": "bar"}),
        # UNREAD: read failed.
        ("unread / collector error", {"_error": "Timeout"}),
    ]
    print("\ninterfaces discriminator (PRESENT-by-existence; empty -> UNREAD not "
          "ABSENT; not-connected is context, only true faults frame CRIT)")
    print(f"{'fixture':<32} {'STATE':<8} {'FRAME':<24} reason")
    print("-" * 116)
    iface_states = set()
    for name, val in iface_fixtures:
        r = arista_interfaces(val)
        iface_states.add(str(r.state))
        fr = (f"{r.frames[0].label}={r.frames[0].value} "
              f"[{r.frames[0].status}]") if r.frames else "—"
        print(f"{name:<32} {str(r.state):<8} {fr:<24} {r.reason}")
    assert "ABSENT" not in iface_states, "interfaces must never reach ABSENT (impossible)"
    print("  ✓ interfaces never reached ABSENT (absence is physically impossible)")

    # ── VERSION: PRESENT-by-existence, frameless (identity, not health) ──────
    ver_fixtures: list[tuple[str, Any]] = [
        ("present / full identity", {"modelName": "DCS-7280SR-48C6", "version": "4.27.3M",
            "serialNumber": "JPE1", "memTotal": 8076400, "memFree": 5527624}),
        ("present / minimal", {"version": "4.30.1F"}),
        ("unread / no identity field", {"architecture": "i686"}),
        ("unread / collector error", {"_error": "Timeout"}),
    ]
    print("\nversion discriminator (PRESENT-by-existence, frameless — identity not health)")
    print(f"{'fixture':<28} {'STATE':<8} {'FRAME':<8} reason")
    print("-" * 96)
    ver_states = set()
    for name, val in ver_fixtures:
        r = arista_version(val)
        ver_states.add(str(r.state))
        print(f"{name:<28} {str(r.state):<8} {'—':<8} {r.reason}")
    assert "ABSENT" not in ver_states
    assert not any(arista_version(v).frames for _, v in ver_fixtures), "version is frameless"
    print("  ✓ version never ABSENT, never framed (clean identity capability)")

    # ── ENVIRONMENT: three JSON sub-reads combined; box's own thresholds ─────
    # Healthy fixture mirrors the real eng-oob-1 captures (power/temp/cooling).
    env_healthy = {
        "power": {"powerSupplies": {
            "1": {"state": "ok", "fans": {"FanP1/1": {"status": "ok"}},
                  "tempSensors": {"TempSensorP1/1": {"status": "ok"}}},
            "2": {"state": "ok", "fans": {"FanP2/1": {"status": "ok"}}}}},
        "temperature": {"systemStatus": "temperatureOk", "tempSensors": [
            {"name": "TempSensor1", "currentTemperature": 34.1, "criticalThreshold": 100.0,
             "inAlertState": False, "hwStatus": "ok"}]},
        "cooling": {"systemStatus": "coolingOk", "ambientTemperature": 28.75,
            "fanTraySlots": [
                {"status": "ok", "label": "1", "fans": [{"status": "ok", "label": "1/1"}]},
                {"status": "ok", "label": "2", "fans": [{"status": "ok", "label": "2/1"}]}]},
    }
    env_fault = {
        "power": {"powerSupplies": {
            "1": {"state": "powerLoss", "fans": {"FanP1/1": {"status": "ok"}}},
            "2": {"state": "ok", "fans": {"FanP2/1": {"status": "failed"}}}}},
        "temperature": {"systemStatus": "temperatureCritical", "tempSensors": [
            {"name": "TempSensor1", "currentTemperature": 101.0, "criticalThreshold": 100.0,
             "inAlertState": True, "hwStatus": "ok"}]},
        "cooling": {"systemStatus": "coolingKo", "ambientTemperature": 41.0,
            "fanTraySlots": [{"status": "failed", "label": "3",
                              "fans": [{"status": "failed", "label": "3/1"}]}]},
    }
    env_fixtures: list[tuple[str, Any]] = [
        ("present / all healthy", env_healthy),
        # PSU powerLoss + failed PSU fan + sensor inAlertState + bad temp status +
        # failed fan tray + failed tray fan + bad cooling status -> 7
        ("present / multi-domain fault", env_fault),
        # UNREAD: cooling sub failed -> can't certify -> UNREAD, names the sub first.
        ("unread / cooling sub failed", {"power": env_healthy["power"],
            "temperature": env_healthy["temperature"], "cooling": {"_error": "Timeout"}}),
        # UNREAD: the 'all'-style unconverted envelope leaked into a sub.
        ("unread / unconverted envelope", {"power": env_healthy["power"],
            "temperature": {"errors": ["This is an unconverted command"]},
            "cooling": env_healthy["cooling"]}),
        # UNREAD: whole capability failed before sub-reads.
        ("unread / collector error", {"_error": "Connection failed"}),
    ]
    print("\nenvironment discriminator (THREE JSON sub-reads — power/temp/cooling; "
          "box's own status; partial read -> UNREAD, failed sub named first)")
    print(f"{'fixture':<32} {'STATE':<8} {'FRAME':<24} reason")
    print("-" * 132)
    env_states = set()
    for name, val in env_fixtures:
        r = arista_environment(val)
        env_states.add(str(r.state))
        fr = (f"{r.frames[0].label}={r.frames[0].value} "
              f"[{r.frames[0].status}]") if r.frames else "—"
        print(f"{name:<32} {str(r.state):<8} {fr:<24} {r.reason}")
    assert "ABSENT" not in env_states, "environment must never reach ABSENT"
    # The incomplete-read reason must name the failed sub AND its error code, and
    # do it within a truncating budget (front-loaded).
    inc = arista_environment({"power": env_healthy["power"],
        "temperature": env_healthy["temperature"],
        "cooling": {"_raw": "...", "_error": "no_json_found"}})
    assert inc.reason.startswith("env incomplete — cooling:no_json_found"), inc.reason[:60]
    # An errors-envelope sub must report as such, not as a bare 'unread'.
    env2 = arista_environment({"power": env_healthy["power"],
        "temperature": {"errors": ["This is an unconverted command"]},
        "cooling": env_healthy["cooling"]})
    assert "errors-envelope" in env2.reason, env2.reason[:80]
    # A no_json_found sub with _raw must surface the device's actual line (echo +
    # prompt stripped) — mirrors the real eng-tor-1 finding.
    env3 = arista_environment({"power": env_healthy["power"],
        "temperature": {"_raw": "show environment temperature | json\n"
                                "% This is an unconverted command\neng-tor-1#",
                        "_error": "no_json_found"},
        "cooling": env_healthy["cooling"]})
    assert "% This is an unconverted command" in env3.reason, env3.reason
    print("  ✓ incomplete read -> UNREAD; failed sub, code, AND device line surface inline")

    # RATIFICATION AMENDMENT receipts: the PRESENT payload is now the shared
    # deep-cap contract shape (contract.py "environment"), emitted by
    # _eos_env_to_groups. conforms() is the verdict; the spot-checks pin the
    # translator's judgment calls: PSU temp sensors (status-only) ride in
    # `sensors` with NO tempC (never fabricated); PSU fans join chassis tray
    # fans in `fans`; electricals/thresholds appear only when the box measured
    # them; per-record `fault` echoes the SAME rule the frame counted.
    try:
        from contract import conforms as _conf
    except ImportError:
        from uf.core.contract import conforms as _conf
    for nm, fx in (("healthy", env_healthy), ("fault", env_fault)):
        rr = arista_environment(fx)
        viol = _conf("environment", rr.payload)
        assert viol == [], f"env {nm}: contract violations: {viol}"
    rr = arista_environment(env_healthy)
    pp = rr.payload
    assert [s["name"] for s in pp["sensors"]] == ["TempSensor1", "TempSensorP1/1"]
    assert "tempC" in pp["sensors"][0] and "tempC" not in pp["sensors"][1], \
        "status-only PSU sensor must carry NO tempC — never fabricated"
    assert {f["name"] for f in pp["fans"]} == {"1/1", "2/1", "FanP1/1", "FanP2/1"}, \
        "chassis tray fans AND PSU fans ride in `fans`"
    assert [p["name"] for p in pp["power"]] == ["PSU-1", "PSU-2"]
    assert pp["ambientC"] == 28.75 and pp["coolingStatus"] == "coolingOk"
    rf = arista_environment(env_fault).payload
    assert rf["power"][0]["fault"] is True and rf["sensors"][0]["fault"] is True, \
        "record fault must echo the frame's rule"
    assert any(f["fault"] for f in rf["fans"])
    print("  ✓ CONTRACT HOLDS — eos environment conforms to the ratified deep-cap "
          "contract; payload is the translator's shape, raw EOS no longer leaks "
          "to consumers on PRESENT")

    # ── ROUTES: same vrfs shape, INVERTED absence (RIB can't be ABSENT) ──────
    routes_fixtures: list[tuple[str, Any]] = [
        # PRESENT / full composition (mirrors a real leaf RIB summary).
        ("present / composed RIB", {"vrfs": {"default": {
            "totalRoutes": 521, "connected": 5, "internal": 2, "static": 1,
            "ospfCounts": {"ospfIntraArea": 180, "ospfExternal2": 12},
            "bgpCounts": {"bgpInternal": 300, "bgpExternal": 21},
            "maskLen": {"24": 312, "32": 98, "31": 64, "30": 47}}}}),
        # PRESENT / connected-only: a static/edge box with no learned routes.
        # Soft signal in the reason, NOT a CRIT frame.
        ("present / connected-only", {"vrfs": {"default": {
            "totalRoutes": 6, "connected": 5, "static": 1,
            "ospfCounts": {}, "bgpCounts": {}, "maskLen": {"24": 5, "32": 1}}}}),
        # PRESENT / degenerate: zero routes -> the one honest fault.
        ("present / degenerate RIB", {"vrfs": {"default": {"totalRoutes": 0, "connected": 0}}}),
        # UNREAD: empty vrfs map. For ospf/bgp this is ABSENT; for the RIB it CAN'T be.
        ("unread / empty vrfs (NOT absent)", {"vrfs": {}}),
        ("unread / no default scope", {"vrfs": {"mgmt": {"totalRoutes": 5}}}),
        ("unread / collector error", {"_error": "Timeout"}),
    ]
    print("\nroutes discriminator (nhd-grade RIB composition; vrfs shape like "
          "ospf/bgp but empty vrfs is UNREAD not ABSENT — the RIB always exists)")
    print(f"{'fixture':<32} {'STATE':<8} {'FRAME':<26} reason")
    print("-" * 124)
    routes_states = set()
    for name, val in routes_fixtures:
        r = arista_routes(val)
        routes_states.add(str(r.state))
        fr = (f"{r.frames[0].label}={r.frames[0].value} "
              f"[{r.frames[0].status}]") if r.frames else "—"
        print(f"{name:<32} {str(r.state):<8} {fr:<26} {r.reason}")
    assert "ABSENT" not in routes_states, "routes must never reach ABSENT (RIB always exists)"
    # Composition must surface protocol breakdown + prefix distribution.
    comp = arista_routes(routes_fixtures[0][1])
    assert "321 bgp" in comp.reason and "192 ospf" in comp.reason, comp.reason
    assert "prefix /24:312" in comp.reason, comp.reason
    # connected-only must be flagged softly, not framed CRIT.
    co = arista_routes(routes_fixtures[1][1])
    assert "connected-only" in co.reason and co.frames[0].value == 0, co.reason
    print("  ✓ empty vrfs -> UNREAD not ABSENT; full RIB composition + prefix dist surfaced")

    # ── TRANSCEIVERS: optic INVENTORY off show inventory | json .xcvrSlots.
    #    Never ABSENT (a chassis inventory can't be positively absent), frameless
    #    (an inventory asserts no fault). Zero populated is still PRESENT. ───────
    xcvr_fixtures: list[tuple[str, Any]] = [
        # PRESENT / populated — 'Not Present' marks an empty cage, not a fault.
        ("present / 3 of 4 populated", {"xcvrSlots": {
            "1": {"mfgName": "Arista",  "modelName": "QSFP-100G-SR4", "serialNum": "XCV0000001"},
            "2": {"mfgName": "Arista",  "modelName": "QSFP-100G-SR4", "serialNum": "XCV0000002"},
            "3": {"mfgName": "Not Present"},
            "5": {"mfgName": "Generic", "modelName": "SFP-10G-SR",    "serialNum": "XCV0000005"}}}),
        # PRESENT / all cages empty — inventory answered; a box with no optics is
        # not an ABSENT capability.
        ("present / all slots empty", {"xcvrSlots": {
            "1": {"mfgName": "Not Present"}, "2": {"mfgName": "Not Present"}}}),
        # UNREAD: inventory answered but no xcvrSlots structure to read.
        ("unread / no xcvrSlots", {"cardSlots": {}}),
        # UNREAD: collector error marker.
        ("unread / collector error", {"_error": "no_json_found"}),
    ]
    print("\ntransceivers discriminator (optic INVENTORY off `show inventory | json` "
          "xcvrSlots; never ABSENT like interfaces; frameless — inventory asserts no fault)")
    print(f"{'fixture':<32} {'STATE':<8} {'FRAME':<10} reason")
    print("-" * 104)
    xcvr_states = set()
    for name, val in xcvr_fixtures:
        r = arista_transceivers(val)
        xcvr_states.add(str(r.state))
        fr = (f"{r.frames[0].label}={r.frames[0].value}") if r.frames else "—"
        print(f"{name:<32} {str(r.state):<8} {fr:<10} {r.reason}")
    assert "ABSENT" not in xcvr_states, "transceivers must never reach ABSENT (inventory always exists)"
    # PRESENT payload is the raw xcvrSlots dict (widget derives populated/total).
    pop = arista_transceivers(xcvr_fixtures[0][1])
    assert str(pop.state) == "PRESENT" and not pop.frames, "populated inventory is PRESENT + frameless"
    assert isinstance(pop.payload, dict) and "1" in pop.payload, "payload is the raw xcvrSlots dict"
    assert "3/4 slots populated" in pop.reason, pop.reason
    # Zero populated is PRESENT, NOT ABSENT.
    empty = arista_transceivers(xcvr_fixtures[1][1])
    assert str(empty.state) == "PRESENT" and "0/2 slots populated" in empty.reason, empty.reason
    print("  ✓ populated + all-empty both PRESENT (never ABSENT); frameless; raw xcvrSlots payload")

    # ── PROC (COMPUTE): cpu + process table off `show processes top once | json`.
    #    Never ABSENT (a live box always has a process table); CPU%-vs-100 frame
    #    whose WARN/CRIT status is TUNABLE POLICY, not box truth (§7). ──────────
    proc_fixtures: list[tuple[str, Any]] = [
        # PRESENT / healthy — idle 92 -> 8% used -> OK band.
        ("present / low cpu (OK)", {
            "cpuInfo": {"%Cpu(s)": {"user": 5.0, "system": 2.0, "idle": 92.0,
                                     "nice": 0.0, "ioWait": 0.5, "hwIrq": 0.5}},
            "processes": {"1893": {"cmd": "Bcm", "cpuPct": 6.2, "residentMem": "142m"},
                          "2044": {"cmd": "Sysdb", "cpuPct": 1.1, "residentMem": "88m"}}}),
        # PRESENT / hot — idle 8 -> 92% used -> CRIT band (status is tool policy).
        ("present / high cpu (CRIT)", {
            "cpuInfo": {"%Cpu(s)": {"user": 70.0, "system": 22.0, "idle": 8.0}},
            "processes": {"1893": {"cmd": "Bcm", "cpuPct": 74.0, "residentMem": "150m"}}}),
        # UNREAD: answered but no CPU line to read.
        ("unread / no cpuInfo", {"processes": {}}),
        # UNREAD: collector error marker.
        ("unread / collector error", {"_error": "Timeout"}),
    ]
    print("\nproc/COMPUTE discriminator (cpu + processes off `show processes top once | json`; "
          "never ABSENT; CPU%-vs-100 frame — status is TUNABLE POLICY, not box truth)")
    print(f"{'fixture':<28} {'STATE':<8} {'FRAME':<28} reason")
    print("-" * 116)
    proc_states = set()
    for name, val in proc_fixtures:
        r = arista_proc(val)
        proc_states.add(str(r.state))
        fr = (f"{r.frames[0].label}={r.frames[0].value} [{r.frames[0].status}]") if r.frames else "—"
        print(f"{name:<28} {str(r.state):<8} {fr:<28} {r.reason}")
    assert "ABSENT" not in proc_states, "proc must never reach ABSENT (process table always exists)"
    hot = arista_proc(proc_fixtures[1][1])
    assert str(hot.state) == "PRESENT" and hot.frames[0].value == 92.0 \
        and str(hot.frames[0].status) == "CRIT", hot.reason
    ok = arista_proc(proc_fixtures[0][1])
    assert isinstance(ok.payload, dict) and "processes" in ok.payload, "payload is the raw proc dict"
    assert str(ok.frames[0].status) == "OK", ok.reason
    print("  ✓ cpu framed (used=100-idle, ceiling 100); status is policy; raw proc payload; never ABSENT")

    # ── Capability determination — the registry as a total operation ─────────


if __name__ == "__main__":
    run_selftests()
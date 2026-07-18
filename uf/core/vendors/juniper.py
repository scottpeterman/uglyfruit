"""
uglyfruit / vendors / juniper — the Junos discriminators.

Design Note 05 §3 / §5: the SECOND vendor, and the first XML read lane. Two
things are farmed from the legacy Juniper HUD collector and two are deliberately
NOT.

FARMED (correct, reused): the PRESENT-path parse. `xml_to_juniper_dict` +
`| display xml` yield the same array-wrapped shape as `| display json` — every
leaf is [{"data": v}]. `_jval`/`_jnum` below are that reader (the legacy jval
contract), lifted verbatim in spirit. That work was right; re-deriving it would
be foolish.

NOT FARMED (illegal under the law): the legacy `_error`-key collapse. A renderer
treats parse-fail and no-BGP-configured identically — both draw nothing — which
is the exact two-state assumption the three-state law forbids (C6). Here they
split: producer/parse failure (`_error`) -> UNREAD (read failed); a positively-
evidenced 'not configured' rpc-error -> ABSENT; peers present -> PRESENT;
empty-but-no-error -> UNREAD (unprovable, never defaulted green).

SHAPE NOTE: Junos array-wraps and nests differently from EOS, so the Junos ->
contract translator is a GENUINE normalization pass, not the near-identity the
EOS ones were (Note 05 §4 predicted exactly this).

FIRST CAP: bgp — the contract test. If the Junos peer record maps onto
{peerAddress, peerState, description?, prefixReceived?} cleanly, the bgp contract
was written to BGP, not to EOS's BGP. `conforms()` in the self-test is the verdict.

`python juniper.py` runs this vendor's suite.
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
# Junos-shape readers — farmed from the legacy HUD jval family. Junos wraps
# every leaf as [{"data": v, "attributes": {...}}] and every branch as [{...}];
# these unwrap one level so the discriminator/translator read scalars, not
# wrappers. This is producer-lane shape handling, kept minimal and local.
# ──────────────────────────────────────────────────────────────────────────
def _jval(node: Any, default: str = "") -> str:
    """Unwrap Junos [{"data": v}] to the scalar string. Missing/odd -> default."""
    if isinstance(node, list) and node and isinstance(node[0], dict) and "data" in node[0]:
        return str(node[0]["data"]).strip()
    if isinstance(node, str):
        return node.strip()
    return default


def _jnum(node: Any, default: int | None = None) -> int | None:
    """_jval, then int(). Non-numeric -> default (never fabricates a 0)."""
    s = _jval(node)
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def _jnum_lead(node: Any, default: int | None = None) -> int | None:
    """Leading integer of a unit-suffixed leaf: "49096 MB" -> 49096. The
    eng-edge-1 capture proved memory-dram-size carries its unit INLINE — the
    version fixture's bare "32768" was cleaner than the gear, so _jnum's
    strict int() silently dropped memTotal on the live box for every poll
    (the Note 06 fabricated-fixture class, second occurrence). Tolerates a
    bare number; None when the lead token isn't one."""
    s = _jval(node)
    if not s:
        return default
    try:
        return int(s.split()[0])
    except (TypeError, ValueError, IndexError):
        return default


def _jfloat(node: Any, default: float | None = None) -> float | None:
    """_jval, then float(). Non-numeric -> default (never fabricates a 0).
    Needed by the environment enrichment subs: dc voltages/currents arrive as
    decimal strings ("12.31") that _jnum's int() rejects."""
    s = _jval(node)
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


def _jpct(node: Any) -> float | None:
    """A percent leaf ("30%") -> 30.0. Tolerates a bare number; None when
    non-numeric — a fan with no reported speed stays speed-less."""
    s = _jval(node)
    if not s:
        return None
    try:
        return float(s.rstrip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def _first(node: Any) -> Any:
    """Junos branch elements arrive wrapped as [ {...} ]. Return the inner dict
    (or the value unchanged if not so wrapped)."""
    if isinstance(node, list) and node:
        return node[0]
    return node


def _jlist(node: Any) -> list:
    """A repeated Junos element is a list; a lone one may be a single dict.
    Normalize to a list so callers iterate uniformly. None -> []."""
    if isinstance(node, list):
        return node
    if node is None:
        return []
    return [node]


def _jsecs(node: Any) -> int | None:
    """Read the `junos:seconds` ATTRIBUTE off a leaf — Junos time values arrive
    as human text with the machine value in the attribute (e.g. up-time:
    {"data": "41 days, 3:22", "attributes": {"junos:seconds": "3555720"}}).
    The parse lane preserves attributes precisely for this. None if absent."""
    if isinstance(node, list) and node and isinstance(node[0], dict):
        attrs = node[0].get("attributes")
        if isinstance(attrs, dict):
            try:
                return int(attrs.get("junos:seconds"))
            except (TypeError, ValueError):
                return None
    return None


def _jattr_float(node: Any, attr: str) -> float | None:
    """Read a named machine-value ATTRIBUTE off a leaf as a float — the
    `_jsecs` pattern generalized (environment temps arrive as human text with
    `junos:celsius` carrying the number). None if absent/non-numeric: a sensor
    with no measurement stays measurement-less, NEVER a fabricated 0 (the
    legacy extractor's isNaN->0 collapse is exactly the fabrication the law
    forbids — a 0C reading and no reading are different facts)."""
    if isinstance(node, list) and node and isinstance(node[0], dict):
        attrs = node[0].get("attributes")
        if isinstance(attrs, dict):
            try:
                return float(attrs.get(attr))
            except (TypeError, ValueError):
                return None
    return None


# ──────────────────────────────────────────────────────────────────────────
# bgp — first cap. Mirrors arista_bgp's STRUCTURE (the law is shared) with
# Junos MARKERS (the evidence is vendor-specific, Note 05 §3).
# ──────────────────────────────────────────────────────────────────────────
_JUNOS_BGP_UP = "established"   # Junos peer-state 'Established' == up — SAME word
                               # as EOS. The cosmetic vocabulary converges here.

# VERIFY-IN-LAB: the exact marker Junos emits for 'BGP not configured'. On a box
# with no `protocols bgp`, `show bgp summary | display xml` returns an rpc-error
# (message ~ "BGP is not running"), NOT an empty bgp-information. The precise
# element/text is unconfirmed until a real no-bgp Junos box is captured; until
# then anything not positively a not-running rpc-error stays UNREAD, never ABSENT.
_JUNOS_NOTRUN_TOKENS = ("not running", "not configured")


def _junos_bgp_error_verdict(value: dict) -> tuple[bool | None, str] | None:
    """Junos-dialect sibling of arista's `_eos_errors_verdict`. Inspects a
    structurally-valid reply for an rpc-error / xnm:error.

      (False, why) -> ABSENT   (message positively says not-running/not-configured)
      (None,  why) -> UNREAD   (some other rpc-error — real, but not proof of absence)
      None         -> no error marker; caller proceeds to PRESENT/empty logic.

    Does NOT handle producer `_error` (parse failure) — that is a READ failure,
    caught earlier as read_ok=False. VERIFY-IN-LAB: the not-running token match."""
    err = value.get("rpc-error") or value.get("xnm:error") or value.get("error")
    if err is None:
        return None
    inner = _first(err)
    msg = _jval(inner.get("message")) if isinstance(inner, dict) else _jval(err)
    low = msg.lower()
    if any(tok in low for tok in _JUNOS_NOTRUN_TOKENS):
        return (False, f"bgp not configured (rpc-error: {msg[:70]})")   # VERIFY-IN-LAB
    return (None, f"read succeeded but absence is unproven: rpc-error, "
                  f"not a not-configured signal ({msg[:70]})")


def _junos_bgp_to_contract(peers: list) -> list[dict]:
    """Junos bgp-peer records -> the cross-vendor bgp contract
    {peerAddress, peerState, description?, prefixReceived?}.

    A GENUINE normalization pass (not the EOS near-identity): _jval unwraps the
    array-wrapping, and prefixReceived is nested under bgp-rib. peerState maps
    directly and shares the 'Established' vocabulary. description is NOT in
    `show bgp summary` output — it is legitimately absent on this read, which the
    contract PERMITS (optional). That clean degradation IS the contract test
    passing: a required field missing would have forced the contract to give."""
    out: list[dict] = []
    for p in peers:
        if not isinstance(p, dict):
            continue
        rec: dict[str, Any] = {
            "peerAddress": _jval(p.get("peer-address")),
            "peerState":   _jval(p.get("peer-state")),
        }
        desc = _jval(p.get("description"))         # optional — summary omits it
        if desc:
            rec["description"] = desc
        rib = _first(p.get("bgp-rib"))             # optional — nested count
        if isinstance(rib, dict) and "received-prefix-count" in rib:
            n = _jnum(rib.get("received-prefix-count"))
            if n is not None:
                rec["prefixReceived"] = n
        out.append(rec)
    return out


def juniper_bgp(bgp_value: Any, as_of: float | None = None) -> Reading:
    """`show bgp summary | display xml` -> xml_to_juniper_dict -> here.

    Shape (post-parse): {"bgp-information": [{"bgp-peer": [ {peer-address,
    peer-state, peer-as, bgp-rib{received-prefix-count}}, ... ]}]}."""
    ts = as_of if as_of is not None else time.time()
    key = "bgp"

    # 0. Producer/parse failure -> UNREAD (read_ok=False). The legacy `_error`
    #    key means the XML never parsed: a READ failure, never absence. This is
    #    the exact C6 line the HUD renderer erased by drawing nothing.
    if isinstance(bgp_value, dict) and "_error" in bgp_value:
        state, reason = classify(read_ok=False, present=None)
        return Reading(key, state, payload=bgp_value, as_of=ts,
                       reason=f"{reason}: collector/parse error "
                              f"({_jval(bgp_value.get('_error')) or bgp_value.get('_error')})")
    if not isinstance(bgp_value, dict):
        state, reason = classify(read_ok=False, present=None)
        return Reading(key, state, payload=bgp_value, as_of=ts,
                       reason=f"{reason}: non-dict reply")

    # 1. rpc-error verdict — the Junos absence lane (§3).
    verdict = _junos_bgp_error_verdict(bgp_value)
    if verdict is not None:
        present, why = verdict
        state, reason = classify(read_ok=True, present=present)
        return Reading(key, state, payload=bgp_value, as_of=ts, reason=why)

    # 2. Locate the bgp-information container.
    info = _first(bgp_value.get("bgp-information"))
    if not isinstance(info, dict):
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=bgp_value, as_of=ts,
                       reason=f"{reason}: no bgp-information container")

    peers = _jlist(info.get("bgp-peer"))
    # 3. Empty bgp-peer is the SAME ambiguity as EOS empty-peers: configured-idle
    #    vs not-running is not distinguishable from summary alone. Without a
    #    confirmed process indicator, empty stays UNREAD, never ABSENT (C6).
    #    VERIFY-IN-LAB whether a Junos header field (peer-count/group-count) can
    #    break the tie the way EOS's routerId does; until then, conservative.
    if not peers:
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=bgp_value, as_of=ts,
                       reason=f"{reason}: bgp-information present, zero bgp-peer "
                              f"(configured-idle vs not-running unprovable here)")

    # 4. PRESENT. Translate to the contract, frame OVER the contract list (§3).
    records = _junos_bgp_to_contract(peers)
    down = sum(1 for p in records
               if str(p.get("peerState", "")).lower() != _JUNOS_BGP_UP)
    frame = Frame(label="peers not Established", value=down, ceiling=0,
                  status=frame_status(down, 0))
    state, reason = classify(read_ok=True, present=True)
    detail = f"{len(records)} peer{'' if len(records)==1 else 's'}, {down} not Established"
    return Reading(key, state, payload=records, frames=[frame], as_of=ts,
                   reason=f"{reason}: {detail}")


# ──────────────────────────────────────────────────────────────────────────
# The Juniper (vendor, cap) -> discriminator map. Assembled into the global
# registry by reading.py. Add a cap = add a line here.
# ──────────────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────────────
# Shared rpc-error verdict core — the Junos absence lane, factored once.
# The MARKERS stay per-cap (each cap passes its own not-running tokens, per
# Note 06 Step 2: "the evidence is vendor- AND cap-specific"); only the walk
# of the rpc-error envelope is shared, because that shape is Junos-wide.
# ──────────────────────────────────────────────────────────────────────────
def _junos_error_verdict(value: dict, notrun_tokens: tuple[str, ...],
                         cap: str) -> tuple[bool | None, str] | None:
    """Inspect a structurally-valid reply for an rpc-error / xnm:error.

      (False, why) -> ABSENT   (message positively matches a not-running token)
      (None,  why) -> UNREAD   (some other rpc-error — real, but not proof of absence)
      None         -> no error marker; caller proceeds to PRESENT/empty logic.

    Does NOT handle producer `_error` (parse failure) — that is a READ failure,
    caught earlier as read_ok=False."""
    err = value.get("rpc-error") or value.get("xnm:error") or value.get("error")
    if err is None:
        return None
    inner = _first(err)
    msg = _jval(inner.get("message")) if isinstance(inner, dict) else _jval(err)
    low = msg.lower()
    if any(tok in low for tok in notrun_tokens):
        return (False, f"{cap} not configured (rpc-error: {msg[:70]})")   # VERIFY-IN-LAB
    return (None, f"read succeeded but absence is unproven: rpc-error, "
                  f"not a not-configured signal ({msg[:70]})")


def _junos_read_gate(value: Any, key: str, ts: float) -> Reading | None:
    """Steps 0 of the recipe skeleton, shared: producer/parse failure and
    non-dict replies -> UNREAD (read_ok=False). Returns the Reading to emit,
    or None to proceed. The C6 line: a read failure is never absence."""
    if isinstance(value, dict) and "_error" in value:
        state, reason = classify(read_ok=False, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: collector/parse error "
                              f"({_jval(value.get('_error')) or value.get('_error')})")
    if not isinstance(value, dict):
        state, reason = classify(read_ok=False, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: non-dict reply")
    return None


# ──────────────────────────────────────────────────────────────────────────
# ospf — cap N+1, the first real cross-vendor test of the OSPF contract
# (bgp's contract was exercised by two vendors before it was a contract;
# ospf's was written from EOS alone — Note 06 §4). Same structure as bgp,
# ospf markers.
# ──────────────────────────────────────────────────────────────────────────
_JUNOS_OSPF_UP = "full"   # Junos ospf-neighbor-state 'Full' == up — SAME word
                          # as EOS (arista's _DOWN token). Vocabulary converges
                          # again; CONFIRMED against the legacy HUD extractor,
                          # VERIFY on live gear per Note 06 §3 (state words).

# VERIFY-IN-LAB: on a box with no `protocols ospf`, `show ospf neighbor
# | display xml` returns an rpc-error (message ~ "OSPF instance is not
# running"). "not running" covers the known wording; the exact element/text is
# unconfirmed until a real no-ospf Junos box is captured. Until then anything
# not positively matched stays UNREAD, never ABSENT.
_JUNOS_OSPF_NOTRUN_TOKENS = ("not running", "not configured", "instance is not")


def _junos_ospf_to_contract(neighbors: list) -> list[dict]:
    """Junos ospf-neighbor records -> the cross-vendor ospf contract
    {routerId, adjacencyState, interfaceName}.

    Field map (transcribed from the legacy HUD extractor, which read live gear
    for months — the Step 0 capture that already existed):
        neighbor-id         -> routerId
        ospf-neighbor-state -> adjacencyState   ('Full' == up)
        interface-name      -> interfaceName
        neighbor-address    -> neighborAddress  (extra; rides along — the
                               contract is a floor, not a ceiling)
        neighbor-priority   -> priority         (extra; IS on the brief read;
                               0 == never-DR, 128 default)

    TOLERANT DETAIL EXTRAS — populated ONLY when the manifest runs
    `show ospf neighbor detail` (VERIFY-IN-LAB: capture the detail shape with
    diag before flipping the manifest line; fixtures below cover brief only):
        dr-address          -> drAddress        (the elected DR, per neighbor)
        bdr-address         -> bdrAddress
        ospf-area           -> area
        neighbor-up-time    -> upTime
        derived             -> drState          Junos reports the segment's
                               DR/BDR ADDRESSES, not a per-neighbor role; the
                               role is DERIVED: neighbor-address == dr-address
                               -> "DR", == bdr-address -> "BDR", else (both
                               markers present, so the segment HAS an election)
                               -> "DROther". EOS reports the role directly;
                               both vendors converge on the extra name
                               `drState` with EOS's vocabulary. No markers ->
                               no drState (p2p links have no election).
    Under brief these keys are simply not emitted — clean degradation, and the
    widget dashes them. Note the EOS asymmetry: _eos_ospf_to_contract now
    ALIASES EOS spellings onto the same converged extras (neighborAddress,
    drState, area) — the deliberate extras-vocabulary convergence Note 07
    debt 3 names."""
    out: list[dict] = []
    for n in neighbors:
        if not isinstance(n, dict):
            continue
        rec: dict[str, Any] = {
            "routerId":       _jval(n.get("neighbor-id")),
            "adjacencyState": _jval(n.get("ospf-neighbor-state")),
            "interfaceName":  _jval(n.get("interface-name")),
        }
        for src, dst in (("neighbor-address", "neighborAddress"),
                         ("dr-address", "drAddress"),
                         ("bdr-address", "bdrAddress"),
                         ("ospf-area", "area")):
            v = _jval(n.get(src))
            if v:
                rec[dst] = v
        # upTime: converged semantic is DURATION SECONDS (a number). Junos
        # time leaves carry it in the junos:seconds attribute; prefer that,
        # fall back to the human text (the widget renders numbers via
        # fmtUptime and strings raw, so either degrades cleanly).
        secs = _jsecs(n.get("neighbor-up-time"))
        if secs is not None:
            rec["upTime"] = secs
        else:
            ut = _jval(n.get("neighbor-up-time"))
            if ut:
                rec["upTime"] = ut
        pri = _jnum(n.get("neighbor-priority"))
        if pri is not None:
            rec["priority"] = pri
        # drState: derived role, EOS vocabulary (see docstring). Only when the
        # detail read supplied election markers; brief emits none -> no claim.
        addr = rec.get("neighborAddress")
        dr, bdr = rec.get("drAddress"), rec.get("bdrAddress")
        if addr and (dr or bdr):
            if addr == dr:
                rec["drState"] = "DR"
            elif addr == bdr:
                rec["drState"] = "BDR"
            else:
                rec["drState"] = "DROther"
        out.append(rec)
    return out


def juniper_ospf(ospf_value: Any, as_of: float | None = None) -> Reading:
    """`show ospf neighbor | display xml` -> xml_to_juniper_dict -> here.

    Shape (post-parse, confirmed by the legacy extractor):
    {"ospf-neighbor-information": [{"ospf-neighbor": [ {neighbor-id,
    ospf-neighbor-state, interface-name, neighbor-address, ...}, ... ]}]}."""
    ts = as_of if as_of is not None else time.time()
    key = "ospf"

    # 0. Producer/parse failure -> UNREAD (read_ok=False).
    gate = _junos_read_gate(ospf_value, key, ts)
    if gate is not None:
        return gate

    # 1. rpc-error verdict — the Junos absence lane.
    verdict = _junos_error_verdict(ospf_value, _JUNOS_OSPF_NOTRUN_TOKENS, key)
    if verdict is not None:
        present, why = verdict
        state, reason = classify(read_ok=True, present=present)
        return Reading(key, state, payload=ospf_value, as_of=ts, reason=why)

    # 2. Locate the ospf-neighbor-information container.
    info = _first(ospf_value.get("ospf-neighbor-information"))
    if not isinstance(info, dict):
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=ospf_value, as_of=ts,
                       reason=f"{reason}: no ospf-neighbor-information container")

    neighbors = _jlist(info.get("ospf-neighbor"))
    # 3. Empty neighbor list. IF the not-running rpc-error is confirmed reliable
    #    (debt: Note 06 §5 item 1), an empty container here would mean OSPF is
    #    RUNNING with zero adjacencies — a real, alarming, PRESENT-with-zero
    #    state. Until that marker is lab-confirmed, empty stays UNREAD, never
    #    ABSENT and never a confident zero (C6): conservative, auditable.
    if not neighbors:
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=ospf_value, as_of=ts,
                       reason=f"{reason}: ospf-neighbor-information present, zero "
                              f"neighbors (running-idle vs not-running unprovable "
                              f"until the rpc-error marker is lab-confirmed)")

    # 4. PRESENT. Translate to the contract, frame OVER the contract list.
    records = _junos_ospf_to_contract(neighbors)
    down = sum(1 for n in records
               if str(n.get("adjacencyState", "")).lower() != _JUNOS_OSPF_UP)
    frame = Frame(label="adjacencies not Full", value=down, ceiling=0,
                  status=frame_status(down, 0))
    state, reason = classify(read_ok=True, present=True)
    detail = (f"{len(records)} adjacenc{'y' if len(records) == 1 else 'ies'}, "
              f"{down} not Full")
    return Reading(key, state, payload=records, frames=[frame], as_of=ts,
                   reason=f"{reason}: {detail}")


# ──────────────────────────────────────────────────────────────────────────
# lldp — the sharpest contract test of the flat caps (its names are the most
# EOS-flavored, Note 06 §4). Mirrors arista_lldp's determination posture:
# PRESENT | UNREAD; ABSENT reachable ONLY via a positively-matched rpc-error
# (arista can't reach it at all; Junos MIGHT error on a disabled agent —
# VERIFY-IN-LAB). Frameless, matching the widget: observation, not health.
# ──────────────────────────────────────────────────────────────────────────
# VERIFY-IN-LAB: whether Junos errors on `show lldp neighbors` with lldp
# unconfigured at all, or returns an empty list. Tokens deliberately narrow.
_JUNOS_LLDP_NOTRUN_TOKENS = ("not running", "not configured", "not enabled")


def _junos_lldp_to_contract(neighbors: list) -> list[dict]:
    """Junos lldp-neighbor-information records -> the cross-vendor lldp
    contract {port, neighborDevice, neighborPort}.

    Field map (legacy extractor + known Junos variance):
        lldp-local-interface | lldp-local-port-id -> port
            BOTH are real: which one Junos emits depends on version and
            port-id-subtype config. Try -interface first, fall back to
            -port-id — a lookup miss here would read as a contract failure
            (missing required field) for the wrong reason.
        lldp-remote-system-name                   -> neighborDevice
        lldp-remote-port-id | -port-description   -> neighborPort
            port-id can be a MAC on some remotes; when -port-id is empty the
            description is the human-useful value. When BOTH exist and differ,
            -port-id wins and the description rides along as an extra
            (neighborPortDesc) for a richer table/tooltip later.
    No ttl on the summary read — legitimately absent; the widget's esc()
    nullish-coalesces, so the TTL column dashes empty. Clean degradation IS
    the contract test passing."""
    out: list[dict] = []
    for n in neighbors:
        if not isinstance(n, dict):
            continue
        port = _jval(n.get("lldp-local-interface")) or _jval(n.get("lldp-local-port-id"))
        rport = _jval(n.get("lldp-remote-port-id"))
        rdesc = _jval(n.get("lldp-remote-port-description"))
        rec: dict[str, Any] = {
            "port":           port,
            "neighborDevice": _jval(n.get("lldp-remote-system-name")),
            "neighborPort":   rport or rdesc,
        }
        if rport and rdesc and rport != rdesc:
            rec["neighborPortDesc"] = rdesc       # extra; floor-not-ceiling
        out.append(rec)
    return out


def juniper_lldp(lldp_value: Any, as_of: float | None = None) -> Reading:
    """`show lldp neighbors | display xml` -> xml_to_juniper_dict -> here.

    Shape (post-parse, confirmed by the legacy extractor):
    {"lldp-neighbors-information": [{"lldp-neighbor-information": [
    {lldp-local-port-id | lldp-local-interface, lldp-remote-system-name,
    lldp-remote-port-id, lldp-remote-port-description, ...}, ... ]}]}."""
    ts = as_of if as_of is not None else time.time()
    key = "lldp"

    # 0. Producer/parse failure -> UNREAD (read_ok=False).
    gate = _junos_read_gate(lldp_value, key, ts)
    if gate is not None:
        return gate

    # 1. rpc-error verdict. Unlike arista_lldp (where ABSENT is unreachable by
    #    construction), Junos MAY answer a disabled agent with an rpc-error —
    #    if lab capture confirms it, that is lldp's only road to ABSENT.
    verdict = _junos_error_verdict(lldp_value, _JUNOS_LLDP_NOTRUN_TOKENS, key)
    if verdict is not None:
        present, why = verdict
        state, reason = classify(read_ok=True, present=present)
        return Reading(key, state, payload=lldp_value, as_of=ts, reason=why)

    # 2. Locate the lldp-neighbors-information container.
    info = _first(lldp_value.get("lldp-neighbors-information"))
    if not isinstance(info, dict):
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=lldp_value, as_of=ts,
                       reason=f"{reason}: no lldp-neighbors-information container")

    neighbors = _jlist(info.get("lldp-neighbor-information"))
    # 3. Empty neighbor list: enabled-but-alone vs disabled is indistinguishable
    #    at this read — the SAME ambiguity arista_lldp carries. UNREAD (C6).
    if not neighbors:
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=lldp_value, as_of=ts,
                       reason=f"{reason}: lldp-neighbors-information present, zero "
                              f"neighbors (enabled-idle vs disabled unprovable here)")

    # 4. PRESENT. Translate to the contract. Frameless — neighbor count is
    #    observation, not health (matches arista_lldp and the widget).
    records = _junos_lldp_to_contract(neighbors)
    state, reason = classify(read_ok=True, present=True)
    detail = f"{len(records)} lldp neighbor{'' if len(records) == 1 else 's'} observed"
    return Reading(key, state, payload=records, as_of=ts,
                   reason=f"{reason}: {detail}")


# ──────────────────────────────────────────────────────────────────────────
# version — the first Juniper MULTI-READ cap, and a different composition
# problem than arista's. On EOS one command carries the whole nameplate
# (`show version | json`: model, version, serial, uptime, mem). Junos splits
# it across three reads; the manifest entry is a dict of sub-commands (the
# same shape arista's environment uses; run_batch already handles it):
#
#     software -> show version                 (host-name, product-model, junos-version)
#     hardware -> show chassis hardware        (chassis serial-number, description)
#     re       -> show chassis routing-engine  (uptime, memory, mastership)
#
# STRICTNESS: unlike environment (STRICT — a partial read can't certify
# hardware health), version is frameless IDENTITY context. The `software` sub
# is the anchor: if it can't be read, there is no identity -> UNREAD. The
# hardware/re subs degrade tolerantly — their fields are simply omitted and
# the reason NAMES the degraded subs, so the nameplate dashes honestly
# instead of the cap going grey over a missing serial number.
#
# PAYLOAD: translated to the EOS nameplate names (modelName / version /
# serialNumber / uptime / memTotal / memFree) so WIDGETS.version stays fully
# vendor-blind. Junos-only truths ride as extras (hostName, reMastership,
# reCount, memUsedPct) — available to a future vendor-specific RE widget
# without a re-read.
# ──────────────────────────────────────────────────────────────────────────
def _junos_sub_err(sub: Any) -> str | None:
    """A sub-read's failure marker, or None if it parsed to a dict."""
    if isinstance(sub, dict) and "_error" in sub:
        return str(_jval(sub.get("_error")) or sub.get("_error"))
    if not isinstance(sub, dict):
        return f"non-dict sub-read ({type(sub).__name__})"
    return None


def _junos_software_info(sub: dict) -> dict | None:
    """Locate software-information, tolerating the dual-RE wrapper: MX boxes
    with two REs wrap `show version` in multi-routing-engine-results ->
    multi-routing-engine-item[] (VERIFY-IN-LAB on a dual-RE chassis; the
    unwrapped shape is the legacy-confirmed one). Returns the info dict or
    None if neither shape is found."""
    info = _first(sub.get("software-information"))
    if isinstance(info, dict):
        return info
    mre = _first(sub.get("multi-routing-engine-results"))
    if isinstance(mre, dict):
        item = _first(mre.get("multi-routing-engine-item"))
        if isinstance(item, dict):
            info = _first(item.get("software-information"))
            if isinstance(info, dict):
                return info
    return None


def juniper_version(value: Any, as_of: float | None = None) -> Reading:
    """{software, hardware, re} sub-reads -> the EOS nameplate payload.

    Sub-read shapes (transcribed from the legacy HUD extractors, live-gear
    proven for months):
      software: {"software-information": [{"host-name", "product-model",
                 "junos-version", "package-information", ...}]}
      hardware: {"chassis-inventory": [{"chassis": [{"serial-number",
                 "description", "chassis-module", ...}]}]}
      re:       {"route-engine-information": [{"route-engine": [{"slot",
                 "mastership-state", "up-time"(junos:seconds attr),
                 "memory-dram-size"(MB), "memory-buffer-utilization"(%used),
                 ...}, ...]}]}"""
    ts = as_of if as_of is not None else time.time()
    key = "version"

    # 0. Producer failure on the WHOLE batch -> UNREAD.
    gate = _junos_read_gate(value, key, ts)
    if gate is not None:
        return gate

    degraded: list[str] = []

    # 1. The anchor sub: software. No identity -> UNREAD (a box always answers
    #    `show version`; a read with no identifying field is a non-read, the
    #    same never-ABSENT posture as arista_version).
    sw = value.get("software")
    err = _junos_sub_err(sw)
    if err is not None:
        state, reason = classify(read_ok=False, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: software sub-read failed ({err})")
    info = _junos_software_info(sw)
    if info is None:
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: no software-information "
                              f"(direct or multi-RE shape)")
    model = _jval(info.get("product-model"))
    ver = _jval(info.get("junos-version"))
    host = _jval(info.get("host-name"))
    if not (model or ver):
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: no identifying version field")

    payload: dict[str, Any] = {}
    if model:
        payload["modelName"] = model
    if ver:
        payload["version"] = ver
    if host:
        payload["hostName"] = host          # extra; EOS version has no hostname

    # 2. hardware sub: serial. Tolerant — degrade, never grey the cap.
    hw = value.get("hardware")
    err = _junos_sub_err(hw)
    if err is not None:
        degraded.append(f"hardware ({err})")
    else:
        ci = _first(hw.get("chassis-inventory"))
        chassis = _first(ci.get("chassis")) if isinstance(ci, dict) else None
        if isinstance(chassis, dict):
            sn = _jval(chassis.get("serial-number"))
            if sn:
                payload["serialNumber"] = sn
            if not model:
                desc = _jval(chassis.get("description"))
                if desc:
                    payload["modelName"] = desc   # fallback nameplate model
        else:
            degraded.append("hardware (no chassis-inventory)")

    # 3. re sub: uptime + memory, from the MASTER RE (or the only one).
    #    Tolerant — same degradation rule.
    re_sub = value.get("re")
    err = _junos_sub_err(re_sub)
    if err is not None:
        degraded.append(f"re ({err})")
    else:
        rei = _first(re_sub.get("route-engine-information"))
        engines = _jlist(rei.get("route-engine")) if isinstance(rei, dict) else []
        engines = [e for e in engines if isinstance(e, dict)]
        if engines:
            master = next((e for e in engines
                           if _jval(e.get("mastership-state")).lower() == "master"),
                          engines[0])
            secs = _jsecs(master.get("up-time"))
            if secs is not None:
                payload["uptime"] = secs            # EOS-shaped: numeric seconds
            else:
                ut = _jval(master.get("up-time"))
                if ut:
                    payload["uptimeText"] = ut      # extra; attr missing -> honest text
            # memTotal/memFree derived: dram-size (MB) + buffer-utilization
            # (% used). (memTotal-memFree)/memTotal round-trips to the box's
            # own utilization number — a documented derivation, not invention.
            # VERIFY-IN-LAB: dram-size units/format across platforms.
            dram_mb = _jnum_lead(master.get("memory-dram-size"))  # "49096 MB"
            used_pct = _jnum(master.get("memory-buffer-utilization"))
            if dram_mb is not None:
                mem_total_kb = dram_mb * 1024
                payload["memTotal"] = mem_total_kb
                if used_pct is not None:
                    payload["memFree"] = round(mem_total_kb * (100 - used_pct) / 100)
                    payload["memUsedPct"] = used_pct     # extra; box's own number
            mstr = _jval(master.get("mastership-state"))
            if mstr:
                payload["reMastership"] = mstr           # extra; Junos-only truth
            payload["reCount"] = len(engines)            # extra
        else:
            degraded.append("re (no route-engine entries)")

    # 4. PRESENT, frameless. mem is CONTEXT (no §7 ceiling), same as arista.
    state, reason = classify(read_ok=True, present=True)
    bits = []
    if payload.get("modelName"):
        bits.append(payload["modelName"])
    if payload.get("version"):
        bits.append(f"JUNOS {payload['version']}")
    if "memTotal" in payload and "memFree" in payload:
        mt, mf = payload["memTotal"], payload["memFree"]
        bits.append(f"mem {100 * (mt - mf) / mt:.0f}% used (context, no §7 ceiling)")
    detail = ", ".join(bits) if bits else "identity read"
    if degraded:
        detail += f" — DEGRADED subs: {', '.join(degraded)}"
    return Reading(key, state, payload=payload, as_of=ts,
                   reason=f"{reason}: {detail}")


# ──────────────────────────────────────────────────────────────────────────
# environment — the deep-cap decider (Note 05 §4's SECOND REAL SHAPE; the
# milestone Note 07 §6 sequenced apart).
#
# STRUCTURE FINDING #1: the multi-read composite was an EOS-ism, not an
# environment-ism. EOS needed three converted sub-commands; Junos answers
# power + thermal + cooling in ONE flat `environment-item` list. So this is a
# SINGLE-READ cap — the STRICT-completeness posture Arista's environment
# needed has nothing to attach to here, and the postures table gains a data
# point: posture follows the vendor's command surface, not the capability.
#
# STRUCTURE FINDING #2 (the contract verdict): intersecting the two real
# shapes, a deep-cap contract candidate EXISTS —
#
#     {"sensors": [{name, status, fault, tempC?}],
#      "fans":    [{name, status, fault}],
#      "power":   [{name, status, fault}]}
#
#   name   — display name (verbatim)
#   status — the box's own word, verbatim, for display (never interpreted
#            by the widget)
#   fault  — bool, THE BOX flags trouble (the widget colors on this alone;
#            vendor status vocabularies never leak into JS)
#   tempC  — sensors only, only when measured (float; absent key == no
#            measurement, never 0)
#
# This is exactly the legacy HUD's grouped shape — the thermal-matrix /
# fan / PSU panels bind to it as-is. It is NOT yet registered in contract.py:
# ratifying a deep-cap contract implies an Arista-side translator pass
# (arista_environment's payload is raw EOS today), and gear-proven code does
# not churn in the commit that lands a new vendor cap (Note 07 §3). The
# registration + the Arista pass are the ratification amendment, its own diff.
# Until then this payload is the CANDIDATE, asserted shape-stable by self-test.
#
# CLASSIFICATION — the sticky-class walk. Junos's junos:style grouping emits
# `class` (Temp / Fans / Power) on the FIRST item of each group only;
# subsequent items omit it. The legacy extractor papered over this with name
# regexes (/fan/i, /power|pem/i) — heuristics that happened to work. Here the
# grouping semantics are read directly: class STICKS until the next class
# element, with the legacy heuristics + the celsius-attribute test kept as
# fallbacks for items arriving before any class (VERIFY-IN-LAB: whether
# eng-edge-1's real output ever leads with a class-less item). Unclassifiable
# items ride the payload as `other` — floor-not-ceiling, drop nothing.
#
# STATUS VOCABULARY (Junos-documented: OK / Testing / Check / Failed / Absent):
#   Failed, Check -> fault (the box itself flags trouble — the frame counts
#                    ONLY these; nothing is fabricated from temperature values,
#                    because this read carries NO thresholds. A §7 temp-ceiling
#                    policy could add one later; today hot-but-OK is context.)
#   Absent        -> vacancy (an empty PEM slot POSITIVELY reported — context
#                    in the reason, never a fault)
#   Testing       -> transient (boot-time state — context, never a fault)
# All tokens VERIFY-IN-LAB against the eng-edge-1 capture.
#
# ABSENCE: physically unreachable — every chassis has power, thermal, and
# cooling hardware (same posture as arista_environment). An rpc-error is a
# failed read of hardware that exists -> UNREAD. Empty/missing item list is a
# non-enumerating read -> UNREAD, never ABSENT.
# ──────────────────────────────────────────────────────────────────────────
_JUNOS_ENV_FAULT   = ("failed", "check")   # the box's own trouble words
_JUNOS_ENV_VACANT  = ("absent",)           # positively-reported empty slot
_JUNOS_ENV_CLASSES = {"temp": "sensors", "fans": "fans", "power": "power"}


def _junos_env_class(cls: str, name: str, has_temp: bool) -> str | None:
    """Map a (sticky) class word to a payload group. Falls back to the legacy
    heuristics when no class has been seen yet: a celsius attribute is a
    sensor by definition; /fan/, /power|pem|psm/ by name. None -> `other`."""
    group = _JUNOS_ENV_CLASSES.get(cls.lower())
    if group:
        return group
    if has_temp:
        return "sensors"
    low = name.lower()
    if "fan" in low:
        return "fans"
    if any(tok in low for tok in ("power", "pem", "psm")):
        return "power"
    return None


def _junos_env_to_groups(items: list) -> dict[str, list[dict]]:
    """environment-item[] -> the candidate deep-cap contract shape. The
    sticky-class walk lives here; `fault` is computed ONCE so the widget
    never reads vendor vocabulary."""
    groups: dict[str, list[dict]] = {"sensors": [], "fans": [], "power": [],
                                     "other": []}
    sticky = ""
    for raw in items:
        item = _first(raw)
        if not isinstance(item, dict):
            continue
        name = _jval(item.get("name"))
        if not name:
            continue
        cls = _jval(item.get("class"))
        if cls:
            sticky = cls                      # junos:style grouping: class sticks
        status = _jval(item.get("status"))
        temp_c = _jattr_float(item.get("temperature"), "junos:celsius")
        rec: dict[str, Any] = {
            "name": name,
            "status": status,
            "fault": status.lower() in _JUNOS_ENV_FAULT,
        }
        if status.lower() in _JUNOS_ENV_VACANT:
            rec["vacant"] = True   # positively-reported empty slot — the widget
                                   # dims on this flag, never by reading "Absent"
        if temp_c is not None:
            rec["tempC"] = temp_c             # measured only; never fabricated 0
        comment = _jval(item.get("comment"))
        if comment:
            rec["comment"] = comment          # extra (e.g. fan speed text)
        group = _junos_env_class(sticky, name, temp_c is not None)
        groups[group if group else "other"].append(rec)
    return groups


def _junos_env_anchor(value: Any, ts: float) -> Reading:
    """`show chassis environment` -> the hardware-health Reading in the
    ratified deep-cap contract shape. THE ANCHOR: it alone certifies health —
    every fault verdict, the frame, and the state come from this read and
    nothing else. Frame counts ONLY box-flagged faults (Failed/Check); Absent
    is vacancy context; Testing is transient context. ABSENT unreachable;
    empty enumeration or rpc-error -> UNREAD."""
    key = "environment"

    # 0. Producer/parse failure -> UNREAD.
    gate = _junos_read_gate(value, key, ts)
    if gate is not None:
        return gate

    # 1. rpc-error: hardware exists, the read of it failed -> UNREAD always
    #    (no not-configured token can exist for a chassis).
    verdict = _junos_error_verdict(value, notrun_tokens=(), cap=key)
    if verdict is not None:
        _, why = verdict
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: {why}")

    # 2. Locate environment-information -> environment-item[]. Missing or
    #    empty is a non-enumerating read (a chassis always has SOMETHING to
    #    report) -> UNREAD, never ABSENT.
    info = _first(value.get("environment-information"))
    if not isinstance(info, dict):
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: no environment-information container")
    items = _jlist(info.get("environment-item"))
    if not items:
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: environment-item empty "
                              f"(non-enumerating read; a chassis can't be ABSENT)")

    # 3. PRESENT. Translate to the candidate contract shape; frame the box's
    #    own trouble flags.
    groups = _junos_env_to_groups(items)
    payload: dict[str, Any] = {k: v for k, v in groups.items()
                               if k != "other" or v}   # `other` rides only if fed

    faults = sum(1 for g in ("sensors", "fans", "power")
                 for r in groups[g] if r["fault"])
    faults += sum(1 for r in groups["other"] if r["fault"])
    vacant = sum(1 for g in groups.values()
                 for r in g if r["status"].lower() in _JUNOS_ENV_VACANT)
    testing = sum(1 for g in groups.values()
                  for r in g if r["status"].lower() == "testing")

    frame = Frame(
        label="environment faults",
        value=faults,
        ceiling=0,
        status=frame_status(faults, 0),
    )
    state, reason = classify(read_ok=True, present=True)

    measured = [r for r in groups["sensors"] if "tempC" in r]
    if measured:
        hottest = max(measured, key=lambda r: r["tempC"])
        hot_txt = f", hottest {hottest['tempC']:.0f}C ({hottest['name']})"
    else:
        hot_txt = ", no measured temps"
    n_bad = lambda g: sum(1 for r in groups[g] if r["fault"])  # noqa: E731
    detail = (f"{len(groups['sensors'])} sensors ({n_bad('sensors')} flagged"
              f"{hot_txt}), {len(groups['fans'])} fans ({n_bad('fans')} bad), "
              f"{len(groups['power'])} power ({n_bad('power')} bad)"
              + (f", {vacant} vacant" if vacant else "")
              + (f", {testing} testing (transient, unframed)" if testing else "")
              + (f", {len(groups['other'])} unclassified" if groups["other"] else "")
              + " — temps are context, no §7 ceiling")
    return Reading(key, state, payload=payload, frames=[frame],
                   as_of=ts, reason=f"{reason}: {detail}")


def _junos_env_enrich_fans(groups: dict, sub: dict) -> bool:
    """`show chassis fan` (junos:style percent-rpm) joined onto the anchor's
    fan records BY NAME (capture-proven convergent on eng-edge-1). Adds
    speedPct; the RPM text rides as `comment` only where the anchor carried
    none (the anchor's own words are never overwritten). Returns False when
    the style/items aren't found — other platforms style this output
    differently (VERIFY-IN-LAB per platform class)."""
    info = _first(sub.get("fan-information"))
    if not isinstance(info, dict):
        return False
    items = _jlist(info.get("fan-information-percent-rpm-item"))
    if not items:
        return False
    by_name = {r["name"]: r for r in groups.get("fans", [])}
    hit = False
    for raw in items:
        item = _first(raw)
        if not isinstance(item, dict):
            continue
        rec = by_name.get(_jval(item.get("name")))
        if rec is None:
            continue                       # join miss: record stays floor-only
        hit = True
        pct = _jpct(item.get("rpm-percent"))
        if pct is not None:
            rec["speedPct"] = pct
        if "comment" not in rec:
            c = _jval(item.get("comment"))
            if c:
                rec["comment"] = c
    return hit


def _junos_env_enrich_power(groups: dict, sub: dict) -> tuple[bool, str | None]:
    """`show chassis power` joined onto the anchor's power records BY NAME.
    Adds watts (dc-power) · ampsOut (str3-dc-current) · volts
    (str3-dc-voltage) · capacityW (capacity-actual). Health verdicts are NOT
    touched — enrichment can only add optional contract fields, never state.
    Returns (joined_any, system_draw_text) — the power-usage-system block's
    actual-usage/capacity ride the REASON as context (the per-record watts
    already sum to the box's own usage figure; asserted in self-test)."""
    info = _first(sub.get("power-usage-information"))
    if not isinstance(info, dict):
        return False, None
    items = _jlist(info.get("power-usage-item"))
    by_name = {r["name"]: r for r in groups.get("power", [])}
    hit = False
    for raw in items:
        item = _first(raw)
        if not isinstance(item, dict):
            continue
        rec = by_name.get(_jval(item.get("name")))
        if rec is None:
            continue
        hit = True
        dc = _first(item.get("dc-output-detail"))
        if isinstance(dc, dict):
            w = _jnum(dc.get("dc-power"))
            if w is not None:
                rec["watts"] = float(w)
            a = _jfloat(dc.get("str3-dc-current"))
            if a is not None:
                rec["ampsOut"] = a
            v = _jfloat(dc.get("str3-dc-voltage"))
            if v is not None:
                rec["volts"] = v
        cap = _first(item.get("pem-capacity-detail"))
        if isinstance(cap, dict):
            c = _jnum(cap.get("capacity-actual"))
            if c is not None:
                rec["capacityW"] = float(c)
    sys_txt = None
    system = _first(info.get("power-usage-system"))
    if isinstance(system, dict):
        use = _jnum(system.get("capacity-actual-usage"))
        cap = _jnum(system.get("capacity-sys-actual"))
        if use is not None and cap is not None:
            sys_txt = f"draw {use}W/{cap}W"
    return hit, sys_txt


def _junos_env_enrich_thresholds(groups: dict, sub: dict) -> bool:
    """`show chassis temperature-thresholds` joined onto the anchor's sensor
    records by EXACT name (capture-proven per-sensor names on eng-edge-1:
    "CB 0 Exhaust Temp Sensor", "FPC 0 EA0 Chip", ...). Adds the box's OWN
    alarm scale: warnC = yellow-alarm, critC = red-alarm. Deliberately no
    prefix/fuzzy join — a threshold the box didn't bind to a sensor stays
    unbound, and unjoined sensors keep the widget's dim display-scale bar.
    bad-fan-* variants (degraded-cooling thresholds) and fire-shutdown are
    ignored: normal-condition yellow/red is the operative scale. Like every
    enrichment: presentational fields only — fault never derives from these
    (the anchor's status words remain the only fault source)."""
    info = _first(sub.get("temperature-threshold-information"))
    if not isinstance(info, dict):
        return False
    items = _jlist(info.get("temperature-threshold"))
    if not items:
        return False
    by_name = {r["name"]: r for r in groups.get("sensors", [])}
    hit = False
    for raw in items:
        item = _first(raw)
        if not isinstance(item, dict):
            continue
        rec = by_name.get(_jval(item.get("name")))
        if rec is None:
            continue                       # exact-join miss: dim bar stands
        hit = True
        y = _jnum(item.get("yellow-alarm"))
        if y is not None:
            rec["warnC"] = float(y)
        rd = _jnum(item.get("red-alarm"))
        if rd is not None:
            rec["critC"] = float(rd)
    return hit


def juniper_environment(value: Any, as_of: float | None = None) -> Reading:
    """The environment composite: {environment, fan, power, thresholds}
    sub-reads under
    the ANCHORED-TOLERANT posture (the third posture, named at version, now
    on its second composite): `environment` is the anchor — it alone
    certifies health, and its failure is the cap's failure — while `fan` and
    `power` are enrichment subs that DEGRADE: a timeout on `show chassis fan`
    dashes the speed column and names itself in the reason, never greys the
    cap. STRICT would be wrong here for the same reason it was wrong for
    version: a missing wattage figure cannot invalidate a health
    certification the box already gave.

    Also accepts the bare anchor shape (pre-enrichment manifests, and every
    anchor fixture) — a value carrying none of the sub keys IS the anchor."""
    ts = as_of if as_of is not None else time.time()
    key = "environment"

    gate = _junos_read_gate(value, key, ts)
    if gate is not None:
        return gate

    if not any(k in value for k in ("environment", "fan", "power",
                                    "thresholds")):
        return _junos_env_anchor(value, ts)          # bare-anchor compat

    # 1. The anchor. Its verdict IS the cap's verdict.
    anchor = value.get("environment")
    err = _junos_sub_err(anchor) if anchor is not None else "missing"
    if err is not None:
        state, reason = classify(read_ok=False, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: anchor sub 'environment' failed ({err})")
    r = _junos_env_anchor(anchor, ts)
    if str(r.state) != "PRESENT":
        return r

    # 2. Enrichment subs: add optional contract fields onto PRESENT records,
    #    degrade by name on any failure. Never state, never fault, never frame.
    degraded: list[str] = []
    extra_txt: list[str] = []

    fan_sub = value.get("fan")
    err = _junos_sub_err(fan_sub) if fan_sub is not None else "missing"
    if err is not None:
        degraded.append(f"fan ({err})")
    elif not _junos_env_enrich_fans(r.payload, fan_sub):
        degraded.append("fan (no percent-rpm items)")

    th_sub = value.get("thresholds")
    err = _junos_sub_err(th_sub) if th_sub is not None else "missing"
    if err is not None:
        degraded.append(f"thresholds ({err})")
    elif not _junos_env_enrich_thresholds(r.payload, th_sub):
        degraded.append("thresholds (no items joined)")

    pw_sub = value.get("power")
    err = _junos_sub_err(pw_sub) if pw_sub is not None else "missing"
    if err is not None:
        degraded.append(f"power ({err})")
    else:
        hit, sys_txt = _junos_env_enrich_power(r.payload, pw_sub)
        if sys_txt:
            extra_txt.append(sys_txt)
        if not hit:
            degraded.append("power (no usage items joined)")

    reason = r.reason
    if extra_txt:
        reason += ", " + ", ".join(extra_txt)
    if degraded:
        reason += f" — DEGRADED subs: {', '.join(degraded)}"
    return Reading(key, r.state, payload=r.payload, frames=r.frames,
                   as_of=ts, reason=reason)


# ──────────────────────────────────────────────────────────────────────────
# optics — the vendor-specific widget category's first rich member (Note 07
# §5's "inverted asymmetry": the Junos DOM shape is RICHER than EOS's
# inventory transceivers, so no contract governs this cap; it exists only in
# the juniper manifest and the widget renders only where the feed carries it).
#
# Shape (eng-edge-1 capture, JUNOS 23.2R1-S2.5, 100G QSFPs):
#   interface-information -> physical-interface[] -> name + optics-diagnostics
#     module level:  module-temperature (junos:celsius ATTR), module-voltage,
#                    per-metric alarm/warn FLAGS ("on"/"off"), and the box's
#                    OWN DOM thresholds inline (dBm variants for tx/rx power).
#     lane level:    optics-diagnostics-lane-values[] -> lane-index, bias mA,
#                    tx/rx power (dBm variants), per-metric flags + LOS/
#                    laser-disabled alarms.
#
# THE LAW APPLIED:
#   frame  = modules with ANY alarm flag "on" — the box's verdict, NEVER
#            re-derived from values against thresholds. Warns are context.
#   flags  = "on" -> tripped, "off" -> clear, missing -> unknown (uncounted;
#            e.g. this module has no per-lane TX-power flags — none invented).
#            VERIFY-IN-LAB: "on" spelling is Junos-documented, uncaptured on
#            our gear (all flags read "off" on a healthy box).
#   zeros  = the laser-temperature-* threshold family reads all-zero here —
#            an unsupported sensor reporting zeros. SKIPPED ENTIRELY: reading
#            it would be the isNaN->0 fabrication with the box as author.
#   empty  = interface-information with no physical-interface entries, or the
#            container missing -> UNREAD, never ABSENT. A DAC-only or
#            optic-less box's true emission is UNCAPTURED (VERIFY-IN-LAB);
#            enumerating nothing is not yet positive evidence of nothing.
#   dBm    = preferred over mW throughout (the operator's unit; the -dbm
#            threshold variants exist for exactly this).
#   ABSENT unreachable pending that capture; rpc-error -> UNREAD.
#
# Bidirectionality: optics fail LOW (the classic dying-rx) as well as high;
# the box gives low+high thresholds per metric. The widget's band gauge
# positions the value BETWEEN the box's low/high alarms — a fill-to-ceiling
# bar would render a dying lane as "relaxed".
# ──────────────────────────────────────────────────────────────────────────
_JUNOS_OPT_MOD_ALARMS = (
    "module-temperature-high-alarm", "module-temperature-low-alarm",
    "module-voltage-high-alarm", "module-voltage-low-alarm")
_JUNOS_OPT_MOD_WARNS = (
    "module-temperature-high-warn", "module-temperature-low-warn",
    "module-voltage-high-warn", "module-voltage-low-warn")
_JUNOS_OPT_LANE_ALARMS = (
    "laser-bias-current-high-alarm", "laser-bias-current-low-alarm",
    "laser-rx-power-high-alarm", "laser-rx-power-low-alarm",
    "tx-loss-of-signal-functionality-alarm", "rx-loss-of-signal-alarm",
    "tx-laser-disabled-alarm")
_JUNOS_OPT_LANE_WARNS = (
    "laser-bias-current-high-warn", "laser-bias-current-low-warn",
    "laser-rx-power-high-warn", "laser-rx-power-low-warn")


def _jflag(node: Any) -> bool | None:
    """A DOM flag leaf: "on" -> True, "off" -> False, anything else/missing ->
    None (unknown, uncounted — never fabricated clear OR tripped)."""
    s = _jval(node).lower()
    if s == "on":
        return True
    if s == "off":
        return False
    return None


def _junos_optics_module(pif: dict) -> dict | None:
    """One physical-interface -> one module record, or None if nameless."""
    name = _jval(pif.get("name"))
    if not name:
        return None
    diag = _first(pif.get("optics-diagnostics"))
    if not isinstance(diag, dict):
        # Enumerated without DOM (copper/DAC shape, uncaptured): the record
        # rides so nothing is dropped; the widget dims it.
        return {"name": name, "dom": False, "fault": False, "warn": False,
                "alarms": []}

    rec: dict[str, Any] = {"name": name, "dom": True, "alarms": []}
    t = _jattr_float(diag.get("module-temperature"), "junos:celsius")
    if t is not None:
        rec["tempC"] = t
    v = _jfloat(diag.get("module-voltage"))
    if v is not None:
        rec["volts"] = v

    # The box's own scales (for the widget's band gauges). Skipped when the
    # leaf is absent; the all-zero laser-temperature family is never read.
    for key, leaf, attr in (
        ("tempCritC",    "module-temperature-high-alarm-threshold", True),
        ("tempWarnC",    "module-temperature-high-warn-threshold",  True),
        ("tempLowCritC", "module-temperature-low-alarm-threshold",  True),
        ("tempLowWarnC", "module-temperature-low-warn-threshold",   True),
        ("voltCrit",     "module-voltage-high-alarm-threshold",     False),
        ("voltWarn",     "module-voltage-high-warn-threshold",      False),
        ("voltLowCrit",  "module-voltage-low-alarm-threshold",      False),
        ("voltLowWarn",  "module-voltage-low-warn-threshold",       False),
        ("rxCritDbm",    "laser-rx-power-high-alarm-threshold-dbm", False),
        ("rxWarnDbm",    "laser-rx-power-high-warn-threshold-dbm",  False),
        ("rxLowCritDbm", "laser-rx-power-low-alarm-threshold-dbm",  False),
        ("rxLowWarnDbm", "laser-rx-power-low-warn-threshold-dbm",   False),
        ("txCritDbm",    "laser-tx-power-high-alarm-threshold-dbm", False),
        ("txWarnDbm",    "laser-tx-power-high-warn-threshold-dbm",  False),
        ("txLowCritDbm", "laser-tx-power-low-alarm-threshold-dbm",  False),
        ("txLowWarnDbm", "laser-tx-power-low-warn-threshold-dbm",   False),
        ("biasCrit",     "laser-bias-current-high-alarm-threshold", False),
        ("biasWarn",     "laser-bias-current-high-warn-threshold",  False),
        ("biasLowCrit",  "laser-bias-current-low-alarm-threshold",  False),
        ("biasLowWarn",  "laser-bias-current-low-warn-threshold",   False),
    ):
        val = (_jattr_float(diag.get(leaf), "junos:celsius") if attr
               else _jfloat(diag.get(leaf)))
        if val is not None:
            rec[key] = val

    fault = warn = False
    for f in _JUNOS_OPT_MOD_ALARMS:
        if _jflag(diag.get(f)) is True:
            fault = True
            rec["alarms"].append(f)
    for f in _JUNOS_OPT_MOD_WARNS:
        if _jflag(diag.get(f)) is True:
            warn = True

    lanes: list[dict] = []
    for raw in _jlist(diag.get("optics-diagnostics-lane-values")):
        lv = _first(raw)
        if not isinstance(lv, dict):
            continue
        lane: dict[str, Any] = {}
        idx = _jnum(lv.get("lane-index"))
        lane["lane"] = idx if idx is not None else len(lanes)
        for k, leaf in (("biasMa", "laser-bias-current"),
                        ("txDbm", "laser-output-power-dbm"),
                        ("rxDbm", "laser-rx-optical-power-dbm")):
            x = _jfloat(lv.get(leaf))
            if x is not None:
                lane[k] = x
        lfault = lwarn = False
        # per-metric flags ride so the widget colors each gauge from the
        # box's own verdict for THAT metric, not a lane-wide rollup:
        lane["rxAlarm"] = any(_jflag(lv.get(f)) is True for f in
                              ("laser-rx-power-high-alarm",
                               "laser-rx-power-low-alarm"))
        lane["rxWarn"] = any(_jflag(lv.get(f)) is True for f in
                             ("laser-rx-power-high-warn",
                              "laser-rx-power-low-warn"))
        lane["biasAlarm"] = any(_jflag(lv.get(f)) is True for f in
                                ("laser-bias-current-high-alarm",
                                 "laser-bias-current-low-alarm"))
        lane["biasWarn"] = any(_jflag(lv.get(f)) is True for f in
                               ("laser-bias-current-high-warn",
                                "laser-bias-current-low-warn"))
        for f in _JUNOS_OPT_LANE_ALARMS:
            if _jflag(lv.get(f)) is True:
                lfault = True
                rec["alarms"].append(f"L{lane['lane']} {f}")
        for f in _JUNOS_OPT_LANE_WARNS:
            if _jflag(lv.get(f)) is True:
                lwarn = True
        lane["fault"] = lfault
        lane["warn"] = lwarn
        fault = fault or lfault
        warn = warn or lwarn
        lanes.append(lane)
    if lanes:
        rec["lanes"] = lanes
    rec["fault"] = fault
    rec["warn"] = warn and not fault
    return rec


def juniper_optics(value: Any, as_of: float | None = None) -> Reading:
    """`show interfaces diagnostics optics` -> per-module DOM Reading. Frame
    counts modules the BOX flagged (any alarm "on"); warns ride the reason as
    context. Empty enumeration or rpc-error -> UNREAD, never ABSENT (the
    optic-less shape is uncaptured)."""
    ts = as_of if as_of is not None else time.time()
    key = "optics"

    gate = _junos_read_gate(value, key, ts)
    if gate is not None:
        return gate

    verdict = _junos_error_verdict(value, notrun_tokens=(), cap=key)
    if verdict is not None:
        _, why = verdict
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: {why}")

    info = _first(value.get("interface-information"))
    if not isinstance(info, dict):
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: no interface-information container")
    pifs = _jlist(info.get("physical-interface"))
    if not pifs:
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: no physical-interface entries "
                              f"(optic-less shape uncaptured; never ABSENT "
                              f"on empty)")

    modules = [m for m in (_junos_optics_module(_first(p)) for p in pifs)
               if m is not None]
    if not modules:
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: entries parsed to no modules")

    dom = [m for m in modules if m.get("dom")]
    nondom = len(modules) - len(dom)
    alarmed = sum(1 for m in modules if m["fault"])
    warned = sum(1 for m in modules if m.get("warn"))
    lanes = sum(len(m.get("lanes", ())) for m in dom)
    frame = Frame(label="optic alarms", value=alarmed, ceiling=0,
                  status=frame_status(alarmed, 0))
    state, reason = classify(read_ok=True, present=True)
    meas = [m for m in dom if "tempC" in m]
    hot = (f", hottest {max(meas, key=lambda m: m['tempC'])['tempC']:.1f}C "
           f"({max(meas, key=lambda m: m['tempC'])['name']})") if meas else ""
    detail = (f"{len(modules)} modules ({lanes} lanes), {alarmed} alarmed, "
              f"{warned} warned{hot}"
              + (f", {nondom} without DOM" if nondom else "")
              + " — box's own flags; values never re-thresholded")
    return Reading(key, state, payload={"modules": modules}, frames=[frame],
                   as_of=ts, reason=f"{reason}: {detail}")


# ──────────────────────────────────────────────────────────────────────────
# interfaces — the third anchored-tolerant composite: {terse*, descriptions}.
# terse alone certifies (enumeration + admin/oper for every physical);
# descriptions enriches by name and DEGRADES (the description column dashes,
# the reason names the sub).
#
# THE TRANSLATION IS EOS-SHAPED (the version move): the payload lands as the
# dict the INTERFACES widget already binds — keyed by ifname, records carry
# linkStatus / lineProtocolStatus / description in the EOS vocabulary — so
# Junos reaches the screen with ZERO widget changes:
#     admin down            -> linkStatus "disabled"   (intentional, never fault)
#     admin up + oper up    -> linkStatus "connected", lineProtocolStatus "up"
#     admin up + oper down  -> linkStatus "notconnect", proto "down" (context)
# terse carries no link-vs-protocol split and no error-state vocabulary, so
# the one unambiguous EOS fault (connected + proto-down) has no terse analog;
# the frame mirrors the EOS §7 policy (not-connected is context unless the
# _NOTCONNECT knob opts in) and will read 0 on a healthy box with unpatched
# capacity — which is the honest reading. bandwidth/interfaceType are absent
# on terse (the `media` price we declined); the widget dashes them.
#
# NOISE (capture ruling): Junos terse enumerates the box's internal plumbing
# (lc-/pfe-/pfh-/fti/vtep/...) that EOS never surfaces in a status read.
# Excluded by the explicit prefix tuple below — read-surface parity, not data
# loss — and the exclusion is COUNTED in the reason so nothing vanishes
# silently. Prefix list is vendor knowledge, VERIFY per platform class.
#
# GHOSTS (capture ruling): a described-but-never-enumerated port (et-0/1/2 —
# configured, description "Available", no optic, absent from terse) rides as
# linkStatus "notpresent" — real EOS vocabulary that happens to be exactly
# Junos's truth — with no fabricated admin/oper, counted in the reason.
#
# §-debt: the notconnect policy knob duplicates arista's module constant;
# promoting it to law/session is a one-line follow-up, not this diff's churn.
# ──────────────────────────────────────────────────────────────────────────
JUNIPER_FRAME_INCLUDE_NOTCONNECT = False   # mirror the EOS §7 default

_JUNOS_IFACE_INTERNAL = (
    "lc-", "pfe-", "pfh-", "cbp", "demux", "dsc", "esi", "fti", "gre",
    "ipip", "jsrv", "lsi", "mif", "mtun", "pimd", "pime", "pip", "pp0",
    "rbeb", "tap", "vtep", "bme", "mams-", "cfm-", "vcp-")


def _junos_iface_internal(name: str) -> bool:
    return any(name.startswith(p) for p in _JUNOS_IFACE_INTERNAL)


def _junos_terse_records(info: dict) -> tuple[dict[str, dict], int]:
    """terse physical-interface[] -> (EOS-shaped records by name, excluded
    count). Logical units ride as a count extra; addresses stay behind —
    Tier-2 territory, not poll payload."""
    records: dict[str, dict] = {}
    excluded = 0
    for raw in _jlist(info.get("physical-interface")):
        pif = _first(raw)
        if not isinstance(pif, dict):
            continue
        name = _jval(pif.get("name"))
        if not name:
            continue
        if _junos_iface_internal(name):
            excluded += 1
            continue
        admin = _jval(pif.get("admin-status")).lower()
        oper = _jval(pif.get("oper-status")).lower()
        rec: dict[str, Any] = {}
        if admin == "down":
            rec["linkStatus"] = "disabled"
        elif oper == "up":
            rec["linkStatus"] = "connected"
            rec["lineProtocolStatus"] = "up"
        else:
            rec["linkStatus"] = "notconnect"
            rec["lineProtocolStatus"] = "down"
        units = len(_jlist(pif.get("logical-interface")))
        if units:
            rec["units"] = units
        records[name] = rec
    return records, excluded


def _junos_iface_desc_enrich(records: dict[str, dict], sub: dict) -> tuple[bool, int]:
    """descriptions joined by name onto the anchor's records. A described
    entry the anchor never enumerated is a config GHOST: appended as
    linkStatus "notpresent" with no fabricated admin/oper. Returns
    (joined_any, ghost_count)."""
    info = _first(sub.get("interface-information"))
    if not isinstance(info, dict):
        return False, 0
    hit = False
    ghosts = 0
    for raw in _jlist(info.get("physical-interface")):
        pif = _first(raw)
        if not isinstance(pif, dict):
            continue
        name = _jval(pif.get("name"))
        desc = _jval(pif.get("description"))
        if not name or not desc:
            continue
        rec = records.get(name)
        if rec is None:
            if _junos_iface_internal(name):
                continue
            records[name] = {"linkStatus": "notpresent", "description": desc}
            ghosts += 1
            continue
        rec["description"] = desc
        hit = True
    return hit or ghosts > 0, ghosts


def juniper_interfaces(value: Any, as_of: float | None = None) -> Reading:
    """{terse*, descriptions} -> the EOS-shaped interfaces Reading. terse
    anchors and alone certifies; descriptions degrades. Frame mirrors the EOS
    §7 policy. Empty enumeration -> UNREAD, never ABSENT (a box always has
    interfaces)."""
    ts = as_of if as_of is not None else time.time()
    key = "interfaces"

    gate = _junos_read_gate(value, key, ts)
    if gate is not None:
        return gate
    if not any(k in value for k in ("terse", "descriptions")):
        value = {"terse": value}           # bare-anchor tolerance (diag lane)

    anchor = value.get("terse")
    err = _junos_sub_err(anchor) if anchor is not None else "missing"
    if err is not None:
        state, reason = classify(read_ok=False, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: anchor sub 'terse' failed ({err})")
    verdict = _junos_error_verdict(anchor, notrun_tokens=(), cap=key)
    if verdict is not None:
        _, why = verdict
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: {why}")
    info = _first(anchor.get("interface-information"))
    if not isinstance(info, dict):
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: no interface-information container")
    records, excluded = _junos_terse_records(info)
    if not records:
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: no operator-facing interfaces "
                              f"enumerated (a box always has interfaces; "
                              f"never ABSENT)")

    degraded: list[str] = []
    ghosts = 0
    dsub = value.get("descriptions")
    err = _junos_sub_err(dsub) if dsub is not None else "missing"
    if err is not None:
        degraded.append(f"descriptions ({err})")
    else:
        hit, ghosts = _junos_iface_desc_enrich(records, dsub)
        if not hit:
            degraded.append("descriptions (no entries joined)")

    up = admin_down = not_connected = faulted = ghost_n = 0
    for rec in records.values():
        link = rec["linkStatus"]
        if link == "disabled":
            admin_down += 1
        elif link == "connected":
            up += 1
        elif link == "notpresent":
            ghost_n += 1
        else:
            not_connected += 1
            if JUNIPER_FRAME_INCLUDE_NOTCONNECT:
                faulted += 1

    frame = Frame(label="interfaces faulted", value=faulted, ceiling=0,
                  status=frame_status(faulted, 0))
    state, reason = classify(read_ok=True, present=True)
    detail = (f"{len(records)} interfaces: {up} up, {admin_down} "
              f"admin-disabled, {not_connected} not-connected, "
              f"{faulted} faulted"
              + ("" if JUNIPER_FRAME_INCLUDE_NOTCONNECT
                 else " (not-connected counted as context, not fault — §7)")
              + (f", {excluded} internal excluded" if excluded else "")
              + (f", {ghost_n} described-not-enumerated" if ghost_n else ""))
    if degraded:
        detail += f" — DEGRADED subs: {', '.join(degraded)}"
    return Reading(key, state, payload=records, frames=[frame],
                   as_of=ts, reason=f"{reason}: {detail}")


# ──────────────────────────────────────────────────────────────────────────
# proc / COMPUTE — single read off `show chassis routing-engine` (the RE is
# the compute plane). Capture-proven on eng-edge-1 (dual RE-S-1600x8):
#   cpu-user/background/system/interrupt/idle  = INSTANTANEOUS
#   cpu-*1 / cpu-*2 / cpu-*3                   = 1 / 5 / 15-minute averages
#   load-average-{one,five,fifteen} · temperature/cpu-temperature (celsius
#   ATTRS) · status · model · memory-buffer-utilization · up-time
#
# FRAME: 100 - cpu-idle of the MASTER RE, instantaneous — matching EOS `top
# once`'s sampling posture, with the 1-minute figure in the reason as
# de-spiking context (the live capture caught its own CLI: idle 80 now, 94
# 1-min). WARN/CRIT mirrors the EOS module policy (70/85 — tool policy, not
# box truth; knob-promotion §-debt shared with arista's).
#
# PAYLOAD is EOS-shaped so the COMPUTE widget renders untouched:
#   cpuInfo['%Cpu(s)'] = {user, system, idle, nice<-background,
#                         hwIrq<-interrupt}
#   processes = {}  — the process table is NOT structurally available on
#   Junos (`show system processes summary | display xml` returns a top(1)
#   text blob in <output>, capture-proven); the text-lane exclusion applies
#   and the widget's "no process data" state is the honest render. The
#   capture even shows WHY: top catches itself + the Netlapse collection
#   session — the self-sample _PROC_HIDE documents.
#   engines[] + loadAvg ride as extras — the RE-redundancy widget's seed
#   (Note 07 §5), free with this read.
# MEM: nothing here — the mem donut is version's populate seam.
# ABSENT unreachable (a box always has an RE); empty/error -> UNREAD.
# ──────────────────────────────────────────────────────────────────────────
JUNIPER_CPU_WARN = 70.0   # mirror EOS tool policy (§-debt: promote to law)
JUNIPER_CPU_CRIT = 85.0


def _junos_engines(info: dict) -> list[dict]:
    """route-engine[] -> per-RE records (the RE-redundancy seed)."""
    out: list[dict] = []
    for raw in _jlist(info.get("route-engine")):
        re_ = _first(raw)
        if not isinstance(re_, dict):
            continue
        rec: dict[str, Any] = {
            "slot": _jnum(re_.get("slot")),
            "mastership": _jval(re_.get("mastership-state")),
            "status": _jval(re_.get("status")),
        }
        model = _jval(re_.get("model"))
        if model:
            rec["model"] = model
        for k, leaf in (("tempC", "temperature"),
                        ("cpuTempC", "cpu-temperature")):
            v = _jattr_float(re_.get(leaf), "junos:celsius")
            if v is not None:
                rec[k] = v
        for k, leaf in (("idlePct", "cpu-idle"), ("idle1Pct", "cpu-idle1"),
                        ("userPct", "cpu-user"), ("systemPct", "cpu-system"),
                        ("interruptPct", "cpu-interrupt"),
                        ("backgroundPct", "cpu-background"),
                        ("memUsedPct", "memory-buffer-utilization")):
            v = _jnum(re_.get(leaf))
            if v is not None:
                rec[k] = v
        for k, leaf in (("loadAvg1", "load-average-one"),
                        ("loadAvg5", "load-average-five"),
                        ("loadAvg15", "load-average-fifteen")):
            v = _jfloat(re_.get(leaf))
            if v is not None:
                rec[k] = v
        up = _jsecs(re_.get("up-time"))
        if up is not None:
            rec["upSeconds"] = up
        out.append(rec)
    return out


def juniper_proc(value: Any, as_of: float | None = None) -> Reading:
    """`show chassis routing-engine` -> the COMPUTE Reading, EOS-shaped.
    Frame = instantaneous CPU used of the MASTER RE vs 100; backup REs and
    the 1-minute average ride as context. Process table deliberately absent
    (text-lane exclusion). Never ABSENT."""
    ts = as_of if as_of is not None else time.time()
    key = "proc"

    gate = _junos_read_gate(value, key, ts)
    if gate is not None:
        return gate
    verdict = _junos_error_verdict(value, notrun_tokens=(), cap=key)
    if verdict is not None:
        _, why = verdict
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: {why}")
    info = _first(value.get("route-engine-information"))
    if not isinstance(info, dict):
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: no route-engine-information container")
    engines = _junos_engines(info)
    if not engines:
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload=value, as_of=ts,
                       reason=f"{reason}: no route-engine entries "
                              f"(a box always has an RE; never ABSENT)")

    master = next((e for e in engines
                   if e.get("mastership", "").lower() == "master"), engines[0])
    idle = master.get("idlePct")
    if idle is None:
        state, reason = classify(read_ok=True, present=None)
        return Reading(key, state, payload={"engines": engines}, as_of=ts,
                       reason=f"{reason}: master RE carries no readable "
                              f"cpu-idle")
    used = round(100.0 - idle, 1)
    status = (Status.CRIT if used >= JUNIPER_CPU_CRIT
              else Status.WARN if used >= JUNIPER_CPU_WARN else Status.OK)
    frame = Frame(label="cpu utilization", value=used, ceiling=100,
                  status=status)

    line: dict[str, Any] = {"idle": idle}
    for eos_k, rec_k in (("user", "userPct"), ("system", "systemPct"),
                         ("hwIrq", "interruptPct"), ("nice", "backgroundPct")):
        v = master.get(rec_k)
        if v is not None:
            line[eos_k] = v
    payload: dict[str, Any] = {
        "cpuInfo": {"%Cpu(s)": line},
        "processes": {},           # text-lane exclusion — never scraped
        "engines": engines,
    }
    la = [master.get(k) for k in ("loadAvg1", "loadAvg5", "loadAvg15")]
    if all(v is not None for v in la):
        payload["loadAvg"] = la

    state, reason = classify(read_ok=True, present=True)
    avg1 = master.get("idle1Pct")
    detail = (f"cpu {used:.1f}% used (master RE{master.get('slot')}, "
              f"instantaneous"
              + (f"; 1-min {100 - avg1:.0f}%" if avg1 is not None else "")
              + ")"
              + (f", load {la[0]}/{la[1]}/{la[2]}"
                 if all(v is not None for v in la) else "")
              + f", {len(engines)} RE"
              + f", processes: text-only on Junos (excluded)")
    return Reading(key, state, payload=payload, frames=[frame],
                   as_of=ts, reason=f"{reason}: {detail}")


JUNIPER_DISCRIMINATORS: dict[tuple[str, str], Discriminator] = {
    ("juniper", "bgp"):  juniper_bgp,    # absence: rpc-error 'not running' (VERIFY-IN-LAB); empty-peers -> UNREAD
    ("juniper", "ospf"): juniper_ospf,   # absence: rpc-error 'instance is not running' (VERIFY-IN-LAB); empty -> UNREAD
    ("juniper", "lldp"): juniper_lldp,   # absence: rpc-error only if Junos errors on disabled agent (VERIFY-IN-LAB); empty -> UNREAD
    ("juniper", "version"): juniper_version,  # multi-read {software,hardware,re}; never ABSENT; software sub anchors, others degrade
    ("juniper", "environment"): juniper_environment,  # anchored-tolerant {environment*, fan, power, thresholds}: anchor alone certifies (frame = box-flagged Failed/Check); enrichment subs DEGRADE; ratified contract shape; ABSENT unreachable
    ("juniper", "optics"): juniper_optics,  # vendor-specific (Junos-only, no contract): per-module DOM; frame = modules the box alarm-flagged; warns context; empty -> UNREAD (optic-less shape uncaptured)
    ("juniper", "interfaces"): juniper_interfaces,  # anchored-tolerant {terse*, descriptions}: EOS-shaped payload (zero widget changes); frame mirrors the EOS §7 policy; internals excluded+counted; ghosts ride "notpresent"
    ("juniper", "proc"): juniper_proc,  # single read (routing-engine IS the compute plane): EOS-shaped cpuInfo; frame = master RE instantaneous used%; processes {} by text-lane exclusion; engines[] extras seed the RE-redundancy widget; never ABSENT
}


def run_selftests() -> None:
    def w(v: Any) -> list:                    # wrap a scalar Junos-style
        return [{"data": str(v)}]

    def peer(addr, state, rx=None, desc=None):
        p: dict[str, Any] = {"peer-address": w(addr), "peer-state": w(state),
                             "peer-as": w("65000")}
        if rx is not None:
            p["bgp-rib"] = [{"received-prefix-count": w(rx)}]
        if desc is not None:
            p["description"] = w(desc)
        return p

    print("juniper bgp — the contract test\n")
    fixtures: list[tuple[str, Any, str]] = [
        ("present / one down", {"bgp-information": [{"bgp-peer": [
            peer("192.0.2.192", "Established", rx=25),
            peer("198.51.100.128", "Established", rx=43441),
            peer("203.0.113.243", "Active", rx=0)]}]}, "PRESENT"),
        ("present / all up", {"bgp-information": [{"bgp-peer": [
            peer("10.0.0.1", "Established", rx=5)]}]}, "PRESENT"),
        ("unread / empty peers", {"bgp-information": [{}]}, "UNREAD"),
        ("unread / collector parse error", {"_error": "xml_parse_failed: bad token"}, "UNREAD"),
        ("absent / rpc-error not running", {"rpc-error": [
            {"message": w("BGP is not running")}]}, "ABSENT"),
        ("unread / other rpc-error", {"rpc-error": [
            {"message": w("permission denied")}]}, "UNREAD"),
    ]
    for name, val, expect in fixtures:
        r = juniper_bgp(val)
        ok = str(r.state) == expect
        fr = f"  {r.frames[0].label}={r.frames[0].value} [{r.frames[0].status}]" if r.frames else ""
        print(f"  {name:<34} -> {str(r.state):<8} {'✓' if ok else '✗ EXPECTED '+expect}{fr}")
        assert ok, f"{name}: expected {expect}, got {r.state}"
    print("  ✓ 6 fixtures: PRESENT / UNREAD(empty,parse,other-error) / ABSENT(not-running)\n")

    # ── THE VERDICT: does the Junos PRESENT payload satisfy the SAME contract
    #    the EOS translator emits? If conforms() == [], the bgp contract was
    #    written to BGP, not to EOS's BGP (Note 05 §4).
    try:
        from contract import conforms, CONTRACTS
    except ImportError:
        from uf.core.contract import conforms, CONTRACTS
    r = juniper_bgp({"bgp-information": [{"bgp-peer": [
        peer("192.0.2.192", "Established", rx=25, desc="PNI:PAYPAL"),
        peer("203.0.113.243", "Active", rx=584)]}]})
    viol = conforms("bgp", r.payload)
    assert viol == [], f"CONTRACT VIOLATION: {viol}"
    print(f"  contract required fields: {CONTRACTS['bgp'].required}")
    print(f"  junos PRESENT payload[0]: {r.payload[0]}")
    print("  ✓ CONTRACT HOLDS — junos bgp conforms to the cross-vendor contract")
    print("    (peerState reads 'Established' on both vendors; description "
          "optional-absent on summary, as the contract permits)")

    # ── ospf — the first cross-vendor test of a contract written from EOS
    #    alone (Note 06 §4). Fixtures transcribed from the legacy HUD
    #    extractor's field names (neighbor-id / ospf-neighbor-state /
    #    interface-name), which read live Junos gear for months.
    def onbr(nid, state, iface, addr=None):
        n: dict[str, Any] = {"neighbor-id": w(nid),
                             "ospf-neighbor-state": w(state),
                             "interface-name": w(iface)}
        if addr is not None:
            n["neighbor-address"] = w(addr)
        return n

    print("\njuniper ospf — the EOS-written contract meets its second vendor\n")
    ofix: list[tuple[str, Any, str]] = [
        ("present / one not Full", {"ospf-neighbor-information": [{"ospf-neighbor": [
            onbr("10.0.0.1", "Full", "et-0/1/1.0", addr="192.168.1.1"),
            onbr("10.0.0.2", "Full", "et-0/1/4.0"),
            onbr("10.0.0.3", "ExStart", "xe-0/0/2.0")]}]}, "PRESENT"),
        ("present / all Full", {"ospf-neighbor-information": [{"ospf-neighbor": [
            onbr("10.0.0.1", "Full", "et-0/1/1.0")]}]}, "PRESENT"),
        ("unread / empty neighbors", {"ospf-neighbor-information": [{}]}, "UNREAD"),
        ("unread / collector parse error", {"_error": "xml_parse_failed"}, "UNREAD"),
        ("absent / rpc-error not running", {"rpc-error": [
            {"message": w("OSPF instance is not running")}]}, "ABSENT"),
        ("unread / other rpc-error", {"rpc-error": [
            {"message": w("permission denied")}]}, "UNREAD"),
    ]
    for name, val, expect in ofix:
        r = juniper_ospf(val)
        ok = str(r.state) == expect
        fr = f"  {r.frames[0].label}={r.frames[0].value} [{r.frames[0].status}]" if r.frames else ""
        print(f"  {name:<34} -> {str(r.state):<8} {'✓' if ok else '✗ EXPECTED '+expect}{fr}")
        assert ok, f"{name}: expected {expect}, got {r.state}"
    print("  ✓ 6 fixtures: PRESENT / UNREAD(empty,parse,other-error) / ABSENT(not-running)\n")

    r = juniper_ospf({"ospf-neighbor-information": [{"ospf-neighbor": [
        onbr("10.0.0.1", "Full", "et-0/1/1.0", addr="192.168.1.1")]}]})
    viol = conforms("ospf", r.payload)
    assert viol == [], f"CONTRACT VIOLATION: {viol}"
    assert "drState" not in r.payload[0], "brief read must make NO role claim"
    print(f"  contract required fields: {CONTRACTS['ospf'].required}")
    print(f"  junos PRESENT payload[0]: {r.payload[0]}")
    print("  ✓ CONTRACT HOLDS — junos ospf conforms; adjacencyState reads 'Full' "
          "on both vendors; neighborAddress rides along as an extra")

    # drState derivation — the detail-read role, from election markers.
    # Fixture shape is the DOCUMENTED detail form; VERIFY-IN-LAB before the
    # manifest flips to `show ospf neighbor detail` (Note 07 debt 3).
    def onbr_det(nid, state, iface, addr, dr=None, bdr=None):
        n = onbr(nid, state, iface, addr=addr)
        if dr is not None:
            n["dr-address"] = w(dr)
        if bdr is not None:
            n["bdr-address"] = w(bdr)
        return n

    r = juniper_ospf({"ospf-neighbor-information": [{"ospf-neighbor": [
        onbr_det("10.0.0.1", "Full", "et-0/1/1.0", "192.168.1.1",
                 dr="192.168.1.1", bdr="192.168.1.2"),
        onbr_det("10.0.0.2", "Full", "et-0/1/1.0", "192.168.1.2",
                 dr="192.168.1.1", bdr="192.168.1.2"),
        onbr_det("10.0.0.3", "Full", "et-0/1/1.0", "192.168.1.3",
                 dr="192.168.1.1", bdr="192.168.1.2")]}]})
    roles = [n.get("drState") for n in r.payload]
    assert roles == ["DR", "BDR", "DROther"], roles
    print(f"  detail-read roles derived: {roles}")
    print("  ✓ drState derived from election markers (addr==dr -> DR, ==bdr -> "
          "BDR, else DROther); brief makes no claim")

    # ── lldp — the most EOS-named contract, the likeliest to give (Note 06
    #    §4). Fixtures cover BOTH local-port spellings and the MAC-as-port-id
    #    remote, so the fallback chains are exercised, not just asserted.
    def lnbr(local_key, local, rsys, rport=None, rdesc=None):
        n: dict[str, Any] = {local_key: w(local),
                             "lldp-remote-system-name": w(rsys)}
        if rport is not None:
            n["lldp-remote-port-id"] = w(rport)
        if rdesc is not None:
            n["lldp-remote-port-description"] = w(rdesc)
        return n

    print("\njuniper lldp — the sharpest contract test of the flat caps\n")
    lfix: list[tuple[str, Any, str]] = [
        ("present / legacy port-id spelling", {"lldp-neighbors-information": [
            {"lldp-neighbor-information": [
                lnbr("lldp-local-port-id", "et-0/1/1", "eng-edge-2",
                     rport="Ethernet49/1"),
                lnbr("lldp-local-port-id", "et-0/1/4", "eng-edge-3",
                     rport="Ethernet50/1", rdesc="CORE::peer1-01")]}]}, "PRESENT"),
        ("present / local-interface spelling", {"lldp-neighbors-information": [
            {"lldp-neighbor-information": [
                lnbr("lldp-local-interface", "xe-0/0/2", "eng-cr-1",
                     rport="xe-1/0/0")]}]}, "PRESENT"),
        ("present / MAC port-id, desc fallback", {"lldp-neighbors-information": [
            {"lldp-neighbor-information": [
                lnbr("lldp-local-port-id", "ge-0/0/0", "mgmt-sw01",
                     rport="", rdesc="GigabitEthernet0/12")]}]}, "PRESENT"),
        ("unread / empty neighbors", {"lldp-neighbors-information": [{}]}, "UNREAD"),
        ("unread / collector parse error", {"_error": "xml_parse_failed"}, "UNREAD"),
        ("absent / rpc-error not enabled", {"rpc-error": [
            {"message": w("LLDP is not enabled")}]}, "ABSENT"),
        ("unread / other rpc-error", {"rpc-error": [
            {"message": w("permission denied")}]}, "UNREAD"),
    ]
    for name, val, expect in lfix:
        r = juniper_lldp(val)
        ok = str(r.state) == expect
        assert not r.frames, f"{name}: lldp must be frameless, got {r.frames}"
        print(f"  {name:<34} -> {str(r.state):<8} {'✓' if ok else '✗ EXPECTED '+expect}")
        assert ok, f"{name}: expected {expect}, got {r.state}"
    print("  ✓ 7 fixtures: PRESENT(x3 spellings/fallbacks) / UNREAD / ABSENT; frameless\n")

    # Contract verdict + the fallback chains, asserted field-by-field.
    r = juniper_lldp({"lldp-neighbors-information": [{"lldp-neighbor-information": [
        lnbr("lldp-local-port-id", "et-0/1/1", "eng-edge-2",
             rport="Ethernet49/1", rdesc="CORE::peer1-01")]}]})
    viol = conforms("lldp", r.payload)
    assert viol == [], f"CONTRACT VIOLATION: {viol}"
    assert r.payload[0]["neighborPort"] == "Ethernet49/1", "port-id must win over desc"
    assert r.payload[0]["neighborPortDesc"] == "CORE::peer1-01", "desc rides as extra"
    r2 = juniper_lldp({"lldp-neighbors-information": [{"lldp-neighbor-information": [
        lnbr("lldp-local-interface", "xe-0/0/2", "eng-cr-1", rport="xe-1/0/0")]}]})
    assert r2.payload[0]["port"] == "xe-0/0/2", "local-interface spelling must map to port"
    r3 = juniper_lldp({"lldp-neighbors-information": [{"lldp-neighbor-information": [
        lnbr("lldp-local-port-id", "ge-0/0/0", "mgmt-sw01", rport="",
             rdesc="GigabitEthernet0/12")]}]})
    assert r3.payload[0]["neighborPort"] == "GigabitEthernet0/12", "desc fallback on empty port-id"
    print(f"  contract required fields: {CONTRACTS['lldp'].required}")
    print(f"  junos PRESENT payload[0]: {r.payload[0]}")
    print("  ✓ CONTRACT HOLDS — junos lldp conforms; both local-port spellings map "
          "to 'port'; desc falls back on empty port-id, rides as extra otherwise")
    print("    (no ttl on the summary read — optional-absent; the widget's esc() "
          "renders it empty. Zero widget changes.)")

    # ── version — the first Juniper multi-read cap. Fixtures transcribed from
    #    the legacy extractors (extractVersion/extractHardware/
    #    extractRoutingEngines). Exercises: the anchor rule, tolerant
    #    degradation, master-RE selection, the junos:seconds attribute read,
    #    and the mem derivation.
    def wsec(text, secs):
        return [{"data": text, "attributes": {"junos:seconds": str(secs)}}]

    SW = {"software-information": [{
        "host-name": w("peer1-01"), "product-model": w("mx10003"),
        "junos-version": w("21.4R3-S4.9")}]}
    HW = {"chassis-inventory": [{"chassis": [{
        "serial-number": w("JN1234567AFB"), "description": w("MX10003")}]}]}
    def re_sub(*engines):
        return {"route-engine-information": [{"route-engine": list(engines)}]}
    # capture-verbatim (eng-edge-1): memory-dram-size carries its UNIT inline.
    # A bare-number fixture here hid a live bug once; it stays ugly on purpose.
    RE_MASTER = {"slot": w("0"), "mastership-state": w("master"),
                 "up-time": wsec("41 days, 3:22", 3555720),
                 "memory-dram-size": w("49096 MB"),
                 "memory-buffer-utilization": w("34")}
    RE_BACKUP = {"slot": w("1"), "mastership-state": w("backup"),
                 "up-time": wsec("41 days, 3:20", 3555600),
                 "memory-dram-size": w("49096 MB"),
                 "memory-buffer-utilization": w("12")}

    print("\njuniper version — the first Juniper multi-read cap\n")
    vfix: list[tuple[str, Any, str]] = [
        ("present / full three subs",
         {"software": SW, "hardware": HW, "re": re_sub(RE_BACKUP, RE_MASTER)},
         "PRESENT"),
        ("present / hardware sub degraded",
         {"software": SW, "hardware": {"_error": "shell_timeout"},
          "re": re_sub(RE_MASTER)}, "PRESENT"),
        ("present / re sub degraded",
         {"software": SW, "hardware": HW,
          "re": {"_error": "xml_parse_failed"}}, "PRESENT"),
        ("unread / software sub failed",
         {"software": {"_error": "shell_timeout"}, "hardware": HW,
          "re": re_sub(RE_MASTER)}, "UNREAD"),
        ("unread / no identifying field",
         {"software": {"software-information": [{}]}, "hardware": HW,
          "re": re_sub(RE_MASTER)}, "UNREAD"),
        ("unread / whole batch _error", {"_error": "exec_failed"}, "UNREAD"),
    ]
    for name, val, expect in vfix:
        r = juniper_version(val)
        ok = str(r.state) == expect
        assert not r.frames, f"{name}: version must be frameless"
        print(f"  {name:<34} -> {str(r.state):<8} {'✓' if ok else '✗ EXPECTED '+expect}")
        if degradedflag := ("DEGRADED" in r.reason):
            print(f"        reason: {r.reason}")
        assert ok, f"{name}: expected {expect}, got {r.state}"
    print("  ✓ 6 fixtures: PRESENT(full, 2x degraded) / UNREAD(anchor-fail, "
          "no-identity, batch-error); frameless\n")

    # The nameplate payload, asserted field-by-field: EOS names, master-RE
    # selection (NOT the first-listed backup), seconds from the attribute,
    # and the mem derivation round-tripping the box's own utilization.
    r = juniper_version({"software": SW, "hardware": HW,
                         "re": re_sub(RE_BACKUP, RE_MASTER)})
    p = r.payload
    assert p["modelName"] == "mx10003" and p["version"] == "21.4R3-S4.9"
    assert p["serialNumber"] == "JN1234567AFB"
    assert p["uptime"] == 3555720, "must read the MASTER RE's junos:seconds"
    assert p["memTotal"] == 49096 * 1024, \
        "leading int of '49096 MB' — units tolerated (the live-gear spelling)"
    assert p["memFree"] == round(49096 * 1024 * 0.66)
    assert round(100 * (p["memTotal"] - p["memFree"]) / p["memTotal"]) == 34, \
        "mem derivation must round-trip the box's utilization number"
    assert p["reMastership"] == "master" and p["reCount"] == 2
    assert p["hostName"] == "peer1-01"
    print(f"  nameplate payload: {p}")
    print("  ✓ EOS nameplate names (modelName/version/serialNumber/uptime/"
          "memTotal/memFree)")
    print("    master RE selected over first-listed backup; uptime from the "
          "junos:seconds attribute;")
    print("    mem derivation round-trips memory-buffer-utilization; Junos "
          "truths (hostName, reMastership, reCount) ride as extras")

    # ── environment — the deep-cap decider. Fixtures transcribed from the
    #    legacy extractEnvironment shape (months of live Junos reads) with the
    #    sticky-class grouping the legacy regexes papered over made explicit.
    #    Exercises: single-read posture, sticky class, the junos:celsius
    #    attribute lane, never-fabricate-0, fault-vs-vacant-vs-testing, and
    #    the candidate contract shape.
    def envitem(name, status, cls=None, cel=None, comment=None):
        d: dict[str, Any] = {"name": w(name), "status": w(status)}
        if cls:
            d["class"] = w(cls)
        if cel is not None:
            d["temperature"] = [{"data": f"{cel} degrees C",
                                 "attributes": {"junos:celsius": str(cel)}}]
        if comment:
            d["comment"] = w(comment)
        return d

    def envfix(*items):
        return {"environment-information": [{"environment-item": list(items)}]}

    # An MX-like slice: class on the FIRST item of each group only — the
    # junos:style grouping the sticky walk exists for.
    MX_ENV = envfix(
        envitem("PEM 0", "OK", cls="Power"),
        envitem("PEM 1", "Absent"),                       # vacancy, class omitted
        envitem("Fan Tray 0 Fan 1", "OK", cls="Fans",
                comment="Spinning at normal speed"),
        envitem("Fan Tray 0 Fan 2", "OK",
                comment="Spinning at normal speed"),      # class omitted
        envitem("Routing Engine 0", "OK", cls="Temp", cel=42),
        envitem("Routing Engine 0 CPU", "OK", cel=55),    # class omitted
        envitem("LC 0 Intake", "Testing"),                # transient, no temp yet
    )

    print("\njuniper environment — the deep-cap second shape (single read)\n")
    efix: list[tuple[str, Any, str, int]] = [
        ("present / healthy MX slice", MX_ENV, "PRESENT", 0),
        ("present / failed fan -> 1 fault",
         envfix(envitem("Fan Tray 0 Fan 1", "Failed", cls="Fans")),
         "PRESENT", 1),
        ("present / Check PEM -> 1 fault",
         envfix(envitem("PEM 0", "Check", cls="Power")), "PRESENT", 1),
        ("present / Absent PEM -> vacancy, 0 faults",
         envfix(envitem("PEM 0", "OK", cls="Power"),
                envitem("PEM 1", "Absent")), "PRESENT", 0),
        ("unread / rpc-error (never ABSENT)",
         {"rpc-error": [{"message": w("command is not valid on this platform")}]},
         "UNREAD", -1),
        ("unread / empty item list",
         {"environment-information": [{}]}, "UNREAD", -1),
        ("unread / no container", {"unexpected": []}, "UNREAD", -1),
        ("unread / batch _error", {"_error": "exec_failed"}, "UNREAD", -1),
    ]
    for name, val, expect, want_faults in efix:
        r = juniper_environment(val)
        ok = str(r.state) == expect
        if want_faults >= 0:
            assert r.frames and r.frames[0].value == want_faults, \
                f"{name}: expected {want_faults} faults, got " \
                f"{r.frames[0].value if r.frames else 'no frame'}"
        else:
            assert not r.frames, f"{name}: non-PRESENT must carry no frame"
        print(f"  {name:<42} -> {str(r.state):<8} {'✓' if ok else '✗ EXPECTED '+expect}")
        assert ok, f"{name}: expected {expect}, got {r.state}"
    print("  ✓ 8 fixtures: PRESENT(healthy, fault, check, vacancy) / "
          "UNREAD(rpc-error, empty, no-container, batch-error)\n")

    # The candidate contract shape, asserted record-by-record: sticky class
    # carried the class-less items, celsius read from the attribute, the
    # temp-less sensor has NO tempC key (never a fabricated 0), fault computed
    # once, comment rides as an extra.
    r = juniper_environment(MX_ENV)
    p = r.payload
    assert set(p) == {"sensors", "fans", "power"}, "other must not ride when empty"
    assert [x["name"] for x in p["power"]] == ["PEM 0", "PEM 1"], \
        "sticky class must carry the class-less PEM 1"
    assert [x["name"] for x in p["fans"]] == ["Fan Tray 0 Fan 1",
                                              "Fan Tray 0 Fan 2"]
    assert [x["name"] for x in p["sensors"]] == ["Routing Engine 0",
                                                 "Routing Engine 0 CPU",
                                                 "LC 0 Intake"]
    assert p["sensors"][0]["tempC"] == 42.0 and p["sensors"][1]["tempC"] == 55.0, \
        "celsius must be read from the junos:celsius attribute"
    assert "tempC" not in p["sensors"][2], \
        "an unmeasured sensor must carry NO tempC — never a fabricated 0"
    assert p["power"][1]["fault"] is False and p["power"][1]["status"] == "Absent", \
        "Absent is vacancy context, never a fault"
    assert p["power"][1].get("vacant") is True and "vacant" not in p["power"][0], \
        "vacancy is the ENGINE's flag — the widget dims on it, never parses 'Absent'"
    assert p["sensors"][2]["fault"] is False, "Testing is transient, never a fault"
    assert p["fans"][0]["comment"] == "Spinning at normal speed"
    assert all(set(rec) >= {"name", "status", "fault"}
               for g in p.values() for rec in g), "contract floor on every record"
    assert "hottest 55C (Routing Engine 0 CPU)" in r.reason
    assert "1 vacant" in r.reason and "1 testing" in r.reason
    print(f"  payload: {p}")
    print(f"  reason:  {r.reason}")
    print("  ✓ candidate deep-cap contract shape {sensors,fans,power}, records "
          "{name,status,fault,tempC?}")
    print("    sticky class carries junos:style grouping; celsius from the "
          "attribute lane; no fabricated 0C;")
    print("    fault computed once (widget never reads vendor vocabulary); "
          "Absent=vacancy, Testing=transient — context, unframed")

    # Sticky-class fallbacks: items arriving BEFORE any class element classify
    # by the celsius-attribute test and the legacy name heuristics; a truly
    # unclassifiable item rides `other`, dropped never.
    r = juniper_environment(envfix(
        envitem("Mystery Sensor", "OK", cel=31),      # no class yet: attr test
        envitem("Fan Rear", "OK"),                    # name heuristic
        envitem("PSM 4", "OK"),                       # name heuristic
        envitem("Widget X", "OK"),                    # unclassifiable
    ))
    p = r.payload
    assert [x["name"] for x in p["sensors"]] == ["Mystery Sensor"]
    assert [x["name"] for x in p["fans"]] == ["Fan Rear"]
    assert [x["name"] for x in p["power"]] == ["PSM 4"]
    assert [x["name"] for x in p["other"]] == ["Widget X"]
    print("  ✓ pre-class fallbacks (celsius attr / name heuristics) hold; "
          "unclassifiable rides `other` — drop nothing")

    # ── The enrichment composite — fixtures transcribed from the eng-edge-1
    #    captures (JUNOS 23.2R1-S2.5, live 2026-07-01): `show chassis fan`
    #    junos:style=percent-rpm, `show chassis power` usage items + system
    #    block. Names converge across all three commands on real gear; the
    #    join is by name and the captures are the proof.
    def w2(v):
        return [{"data": str(v)}]

    ENV_ANCHOR = envfix(
        envitem("PEM 0", "OK", cls="Power"),
        envitem("PEM 1", "Absent"),
        envitem("Fan Tray 0 Fan 0", "OK", cls="Fans",
                comment="Spinning at normal speed"),
        envitem("Fan Tray 0 Fan 1", "OK"),
        envitem("Routing Engine 0", "OK", cls="Temp", cel=42),
    )
    FAN_SUB = {"fan-information": [{
        "attributes": {"junos:style": "percent-rpm"},
        "fan-information-percent-rpm-item": [
            {"name": w2("Fan Tray 0 Fan 0"), "status": w2("OK"),
             "rpm-percent": w2("30%"), "comment": w2("6528 RPM")},
            {"name": w2("Fan Tray 0 Fan 1"), "status": w2("OK"),
             "rpm-percent": w2("30%"), "comment": w2("5504 RPM")},
        ]}]}
    POWER_SUB = {"power-usage-information": [{
        "power-usage-item": [
            {"name": w2("PEM 0"), "state": w2("Online"),
             "pem-capacity-detail": [{"capacity-actual": w2("1600"),
                                      "capacity-max": w2("1600")}],
             "dc-output-detail": [{"dc-power": w2("24"), "zone": w2("0"),
                                   "str3-dc-current": w2("2.00"),
                                   "str3-dc-voltage": w2("12.31"),
                                   "dc-load": w2("1")}]},
        ],
        "power-usage-system": [{
            "power-usage-zone-information": [{"zone": w2("0")}],
            "capacity-sys-actual": w2("9600"),
            "capacity-sys-max": w2("9600"),
            "capacity-actual-usage": w2("24"),
        }]}]}

    print("\njuniper environment enrichment — anchored-tolerant composite\n")
    THRESH_SUB = {"temperature-threshold-information": [{
        "temperature-threshold": [
            {"name": w2("Routing Engine 0"),
             "yellow-alarm": w2("85"), "red-alarm": w2("100"),
             "fire-shutdown": w2("102"),
             "bad-fan-yellow-alarm": w2("85"), "bad-fan-red-alarm": w2("100")},
            {"name": w2("CB 0 Exhaust Temp Sensor"),
             "yellow-alarm": w2("75"), "red-alarm": w2("85"),
             "fire-shutdown": w2("95")},
        ]}]}

    r = juniper_environment({"environment": ENV_ANCHOR, "fan": FAN_SUB,
                             "power": POWER_SUB, "thresholds": THRESH_SUB})
    assert str(r.state) == "PRESENT" and r.frames[0].value == 0
    p = r.payload
    fans = {x["name"]: x for x in p["fans"]}
    assert fans["Fan Tray 0 Fan 0"]["speedPct"] == 30.0, "rpm-percent '%'-strip"
    assert fans["Fan Tray 0 Fan 0"]["comment"] == "Spinning at normal speed", \
        "the anchor's own words are never overwritten"
    assert fans["Fan Tray 0 Fan 1"]["comment"] == "5504 RPM", \
        "RPM text rides as comment where the anchor carried none"
    pems = {x["name"]: x for x in p["power"]}
    assert pems["PEM 0"]["watts"] == 24.0 and pems["PEM 0"]["ampsOut"] == 2.0 \
        and pems["PEM 0"]["volts"] == 12.31 and pems["PEM 0"]["capacityW"] == 1600.0
    assert "watts" not in pems["PEM 1"] and pems["PEM 1"].get("vacant") is True, \
        "a vacant PEM is absent from the power sub: join miss, floor-only record"
    assert "draw 24W/9600W" in r.reason and "DEGRADED" not in r.reason
    # The box's self-consistency receipt (mirrors version's mem round-trip):
    # on the live capture, per-PEM dc-power summed to 1215 == the system
    # block's capacity-actual-usage. The fixture keeps the invariant.
    assert sum(x["watts"] for x in p["power"] if "watts" in x) == 24.0
    try:
        from contract import conforms as _conf2
    except ImportError:
        from uf.core.contract import conforms as _conf2
    assert _conf2("environment", p) == []
    sens = {x["name"]: x for x in p["sensors"]}
    assert sens["Routing Engine 0"]["warnC"] == 85.0 \
        and sens["Routing Engine 0"]["critC"] == 100.0, \
        "thresholds join: yellow->warnC, red->critC, exact name"
    assert sens["Routing Engine 0"]["fault"] is False, \
        "thresholds are presentational — fault still comes from the anchor alone"
    print("  ✓ full batch: speedPct/watts/ampsOut/volts/capacityW joined by name; "
          "thresholds -> warnC/critC (box's own alarm scale); vacant PEM stays "
          "floor-only; draw rides the reason; contract holds")

    r = juniper_environment({"environment": ENV_ANCHOR,
                             "fan": {"_error": "shell_timeout"},
                             "power": POWER_SUB, "thresholds": THRESH_SUB})
    assert str(r.state) == "PRESENT" and r.frames[0].value == 0
    assert "DEGRADED subs: fan (shell_timeout)" in r.reason
    r2 = juniper_environment({"environment": ENV_ANCHOR, "fan": FAN_SUB,
                              "power": POWER_SUB})
    assert str(r2.state) == "PRESENT" \
        and "thresholds (missing)" in r2.reason \
        and all("critC" not in x for x in r2.payload["sensors"]), \
        "an absent thresholds sub degrades by name; sensors keep the dim bar"
    assert all("speedPct" not in x for x in r.payload["fans"])
    print("  ✓ fan sub timeout: cap stays PRESENT, speed column degrades, "
          "reason names the sub — a missing wattage can't invalidate a health "
          "certification the box already gave")

    r = juniper_environment({"environment": {"_error": "shell_timeout"},
                             "fan": FAN_SUB, "power": POWER_SUB})
    assert str(r.state) == "UNREAD" and "anchor sub 'environment' failed" in r.reason
    r = juniper_environment({"fan": FAN_SUB, "power": POWER_SUB})
    assert str(r.state) == "UNREAD" and "(missing)" in r.reason
    print("  ✓ anchor failure or absence is the CAP's failure — enrichment "
          "can never certify")

    r = juniper_environment(ENV_ANCHOR)
    assert str(r.state) == "PRESENT" and r.frames[0].value == 0
    assert all("speedPct" not in x for x in r.payload["fans"])
    print("  ✓ bare-anchor compat: a pre-enrichment value still reads — every "
          "anchor fixture above ran through this path")

    # ── optics — fixtures transcribed from the eng-edge-1 capture (et-0/1/0,
    #    100G QSFP, trimmed to 2 lanes; real numbers). Flag vocabulary
    #    fabricated ONLY for the tripped cases ("on" is Junos-documented,
    #    uncaptured on healthy gear — the VERIFY-IN-LAB the section notes).
    def optflags(**over):
        base = {f: w2("off") for f in (
            "laser-bias-current-high-alarm", "laser-bias-current-low-alarm",
            "laser-bias-current-high-warn", "laser-bias-current-low-warn",
            "laser-rx-power-high-alarm", "laser-rx-power-low-alarm",
            "laser-rx-power-high-warn", "laser-rx-power-low-warn",
            "tx-loss-of-signal-functionality-alarm",
            "rx-loss-of-signal-alarm", "tx-laser-disabled-alarm")}
        base.update({k: w2(v) for k, v in over.items()})
        return base

    def optlane(idx, bias, tx, rx, **over):
        d = {"lane-index": w2(str(idx)), "laser-bias-current": w2(str(bias)),
             "laser-output-power-dbm": w2(str(tx)),
             "laser-rx-optical-power-dbm": w2(str(rx))}
        d.update(optflags(**over))
        return d

    def optmod(name, cel, volts, lanes, **modover):
        diag = {
            "module-temperature": [{"data": f"{cel} degrees C",
                                    "attributes": {"junos:celsius": str(cel)}}],
            "module-voltage": w2(str(volts)),
            "module-temperature-high-alarm-threshold":
                [{"data": "75 degrees C", "attributes": {"junos:celsius": "75.0"}}],
            "module-temperature-high-warn-threshold":
                [{"data": "70 degrees C", "attributes": {"junos:celsius": "70.0"}}],
            "module-temperature-low-alarm-threshold":
                [{"data": "-5 degrees C", "attributes": {"junos:celsius": "-5.0"}}],
            "module-voltage-high-alarm-threshold": w2("3.6300"),
            "module-voltage-low-alarm-threshold": w2("2.9700"),
            "laser-rx-power-high-alarm-threshold-dbm": w2("5.50"),
            "laser-rx-power-low-alarm-threshold-dbm": w2("-14.60"),
            "laser-rx-power-high-warn-threshold-dbm": w2("4.50"),
            "laser-rx-power-low-warn-threshold-dbm": w2("-10.60"),
            "laser-tx-power-high-alarm-threshold-dbm": w2("7.50"),
            "laser-tx-power-low-alarm-threshold-dbm": w2("-8.30"),
            "laser-bias-current-high-alarm-threshold": w2("54.999"),
            "laser-bias-current-low-alarm-threshold": w2("24.999"),
            # the all-zero unsupported family — must never be read:
            "laser-temperature-high-alarm-threshold":
                [{"data": "0 degrees C", "attributes": {"junos:celsius": "0.0"}}],
            "module-temperature-high-alarm": w2("off"),
            "module-temperature-low-alarm": w2("off"),
            "module-temperature-high-warn": w2("off"),
            "module-temperature-low-warn": w2("off"),
            "module-voltage-high-alarm": w2("off"),
            "module-voltage-low-alarm": w2("off"),
            "module-voltage-high-warn": w2("off"),
            "module-voltage-low-warn": w2("off"),
            "optics-diagnostics-lane-values": lanes,
        }
        diag.update({k: w2(v) for k, v in modover.items()})
        return {"name": w2(name), "optics-diagnostics": [diag]}

    OPT_HEALTHY = {"interface-information": [{"physical-interface": [
        optmod("et-0/1/0", 30.5, 3.2240,
               [optlane(0, 41.675, 2.20, -4.04), optlane(1, 41.414, 2.48, -3.22)]),
        optmod("et-0/1/1", 30.2, 3.2280,
               [optlane(0, 41.756, 3.12, -9.33)]),
    ]}]}

    print("\njuniper optics — vendor-specific DOM cap (Note 07 §5's first rich member)\n")
    r = juniper_optics(OPT_HEALTHY)
    assert str(r.state) == "PRESENT" and r.frames[0].value == 0
    p = r.payload["modules"]
    m0 = p[0]
    assert m0["tempC"] == 30.5 and m0["volts"] == 3.224, "attr + float lanes"
    assert m0["tempCritC"] == 75.0 and m0["tempWarnC"] == 70.0 \
        and m0["tempLowCritC"] == -5.0
    assert m0["rxCritDbm"] == 5.5 and m0["rxLowCritDbm"] == -14.6 \
        and m0["rxLowWarnDbm"] == -10.6
    assert "laser-temperature" not in str(set(m0)), \
        "the all-zero unsupported family must never be read"
    l0 = m0["lanes"][0]
    assert l0["rxDbm"] == -4.04 and l0["txDbm"] == 2.2 and l0["biasMa"] == 41.675
    assert l0["fault"] is False and l0["rxAlarm"] is False
    assert "3 lanes" in r.reason and "hottest 30.5C (et-0/1/0)" in r.reason
    print("  ✓ healthy: numbers off the real capture; box thresholds mapped "
          "(dBm variants); zero-family skipped; frame 0")

    # A dying rx lane + a module warn: the box flags, the engine counts.
    OPT_ALARM = {"interface-information": [{"physical-interface": [
        optmod("et-0/1/0", 30.5, 3.2240,
               [optlane(0, 41.675, 2.20, -16.2,
                        **{"laser-rx-power-low-alarm": "on",
                           "rx-loss-of-signal-alarm": "on"}),
                optlane(1, 41.414, 2.48, -3.22)]),
        optmod("et-0/1/1", 30.2, 3.2280,
               [optlane(0, 41.756, 3.12, -9.33)],
               **{"module-temperature-high-warn": "on"}),
    ]}]}
    r = juniper_optics(OPT_ALARM)
    assert str(r.state) == "PRESENT" and r.frames[0].value == 1, \
        "frame counts MODULES the box alarmed, not tripped flags"
    p = r.payload["modules"]
    assert p[0]["fault"] is True and p[0]["lanes"][0]["fault"] is True \
        and p[0]["lanes"][0]["rxAlarm"] is True
    assert "L0 laser-rx-power-low-alarm" in p[0]["alarms"] \
        and "L0 rx-loss-of-signal-alarm" in p[0]["alarms"]
    assert p[0]["lanes"][1]["fault"] is False, "flags are per-lane, no smear"
    assert p[1]["fault"] is False and p[1]["warn"] is True, \
        "a warn is context, never a fault"
    assert "1 alarmed, 1 warned" in r.reason
    print("  ✓ alarm: dying-rx lane flags roll up by NAME; warn-only module "
          "stays unframed; frame=1 module")

    # Enumerated without DOM rides dimmed, dropped never.
    r = juniper_optics({"interface-information": [{"physical-interface": [
        optmod("et-0/1/0", 30.5, 3.2240, [optlane(0, 41.675, 2.20, -4.04)]),
        {"name": w2("xe-0/0/7")},
    ]}]})
    assert str(r.state) == "PRESENT" and r.frames[0].value == 0
    assert r.payload["modules"][1] == {"name": "xe-0/0/7", "dom": False,
                                       "fault": False, "warn": False,
                                       "alarms": []}
    assert "1 without DOM" in r.reason
    print("  ✓ DOM-less entry (copper/DAC shape, uncaptured) rides dimmed — "
          "drop nothing")

    for name, val in (
        ("unread / empty enumeration",
         {"interface-information": [{}]}),
        ("unread / no container", {"unexpected": []}),
        ("unread / rpc-error",
         {"rpc-error": [{"message": w2("syntax error")}]}),
        ("unread / batch _error", {"_error": "shell_timeout"}),
    ):
        r = juniper_optics(val)
        assert str(r.state) == "UNREAD" and not r.frames, name
    print("  ✓ empty/error -> UNREAD, never ABSENT (optic-less shape awaits "
          "its capture)")

    # ── interfaces — fixtures transcribed from the eng-edge-1 terse +
    #    descriptions captures: the breakout zoo, the internal plumbing, the
    #    et-0/1/2 ghost, the vendor description conventions.
    def tif(name, admin, oper, units=0):
        d: dict[str, Any] = {"name": w2(name), "admin-status": w2(admin),
                             "oper-status": w2(oper)}
        if units:
            d["logical-interface"] = [
                {"name": w2(f"{name}.{i}"), "admin-status": w2("up"),
                 "oper-status": w2(oper)} for i in range(units)]
        return d

    def dif(name, desc, admin=None, oper=None):
        d: dict[str, Any] = {"name": w2(name), "description": w2(desc)}
        if admin:
            d["admin-status"] = w2(admin)
        if oper:
            d["oper-status"] = w2(oper)
        return d

    TERSE = {"interface-information": [{"physical-interface": [
        tif("lc-0/0/0", "up", "up", units=1),      # internal -> excluded
        tif("pfe-0/0/0", "up", "up", units=1),     # internal -> excluded
        tif("xe-0/0/0:0", "up", "down"),           # unpatched breakout
        tif("et-0/1/0", "up", "up", units=1),      # TRANSIT, up
        tif("xe-1/0/0:0", "down", "down", units=1),# admin-disabled
        tif("fxp0", "up", "down", units=1),        # mgmt, notconnect
        tif("vtep", "up", "up", units=3),          # internal -> excluded
    ]}]}
    DESCS = {"interface-information": [{"physical-interface": [
        dif("xe-0/0/0:0", "Available", "up", "down"),
        dif("et-0/1/0", "TRANSIT:Cogent:AS174:PP:", "up", "up"),
        dif("et-0/1/2", "Available"),               # the GHOST: never in terse
    ]}]}

    print("\njuniper interfaces — anchored-tolerant {terse*, descriptions}, "
          "EOS-shaped\n")
    r = juniper_interfaces({"terse": TERSE, "descriptions": DESCS})
    assert str(r.state) == "PRESENT" and r.frames[0].value == 0, \
        "not-connected is context under the EOS §7 default"
    p = r.payload
    assert set(p) == {"xe-0/0/0:0", "et-0/1/0", "xe-1/0/0:0", "fxp0",
                      "et-0/1/2"}, f"got {set(p)}"
    assert p["et-0/1/0"] == {"linkStatus": "connected",
                             "lineProtocolStatus": "up", "units": 1,
                             "description": "TRANSIT:Cogent:AS174:PP:"}
    assert p["xe-0/0/0:0"]["linkStatus"] == "notconnect" \
        and p["xe-0/0/0:0"]["lineProtocolStatus"] == "down" \
        and p["xe-0/0/0:0"]["description"] == "Available"
    assert p["xe-1/0/0:0"]["linkStatus"] == "disabled"
    assert p["et-0/1/2"] == {"linkStatus": "notpresent",
                             "description": "Available"}, \
        "the ghost rides with NO fabricated admin/oper"
    assert "3 internal excluded" in r.reason \
        and "1 described-not-enumerated" in r.reason \
        and "context, not fault — §7" in r.reason
    assert "DEGRADED" not in r.reason
    print("  ✓ full pair: EOS vocabulary (connected/disabled/notconnect); "
          "internals excluded AND counted; ghost rides 'notpresent'; "
          "vendor descriptions joined")

    r = juniper_interfaces({"terse": TERSE,
                            "descriptions": {"_error": "shell_timeout"}})
    assert str(r.state) == "PRESENT" \
        and "DEGRADED subs: descriptions (shell_timeout)" in r.reason
    assert all("description" not in rec for rec in r.payload.values())
    print("  ✓ descriptions timeout: cap stays PRESENT, description column "
          "degrades — the posture's third use")

    r = juniper_interfaces({"terse": {"_error": "shell_timeout"},
                            "descriptions": DESCS})
    assert str(r.state) == "UNREAD" and "anchor sub 'terse' failed" in r.reason
    r = juniper_interfaces({"terse": {"interface-information": [
        {"physical-interface": [tif("lc-0/0/0", "up", "up")]}]}})
    assert str(r.state) == "UNREAD" and "never ABSENT" in r.reason, \
        "an all-internal enumeration certifies nothing"
    r = juniper_interfaces(TERSE)   # bare-anchor diag lane
    assert str(r.state) == "PRESENT" and "DEGRADED" in r.reason
    print("  ✓ anchor failure is the cap's failure; all-internal -> UNREAD; "
          "bare terse tolerated (diag lane)")

    # ── proc/COMPUTE — fixture transcribed from the eng-edge-1 capture
    #    verbatim (dual RE-S-1600x8, master idle 80 / 1-min 94, backup idle
    #    100): the instantaneous-vs-averaged split, the master pick, the
    #    EOS shaping, the text-lane exclusion.
    def rex(slot, master, idle, idle1=None, user=0, system=0, la=None,
            temp=None):
        d: dict[str, Any] = {"slot": w2(str(slot)),
                             "mastership-state": w2(master),
                             "status": w2("OK"), "model": w2("RE-S-1600x8"),
                             "cpu-idle": w2(str(idle)),
                             "cpu-user": w2(str(user)),
                             "cpu-system": w2(str(system)),
                             "cpu-interrupt": w2("0"),
                             "cpu-background": w2("0"),
                             "memory-buffer-utilization": w2("4")}
        if idle1 is not None:
            d["cpu-idle1"] = w2(str(idle1))
        if la:
            d["load-average-one"] = w2(str(la[0]))
            d["load-average-five"] = w2(str(la[1]))
            d["load-average-fifteen"] = w2(str(la[2]))
        if temp is not None:
            d["temperature"] = [{"data": f"{temp} degrees C",
                                 "attributes": {"junos:celsius": str(temp)}}]
        return d

    RE_FIX = {"route-engine-information": [{"route-engine": [
        rex(0, "master", 80, idle1=94, user=11, system=10,
            la=(0.53, 0.43, 0.37), temp=31),
        rex(1, "backup", 100, la=(0.17, 0.16, 0.10), temp=32),
    ]}]}

    print("\njuniper proc/COMPUTE — single read; the RE is the compute plane\n")
    r = juniper_proc(RE_FIX)
    assert str(r.state) == "PRESENT"
    assert r.frames[0].label == "cpu utilization" and r.frames[0].value == 20.0 \
        and r.frames[0].ceiling == 100 and str(r.frames[0].status) == "OK", \
        "frame = 100 - MASTER instantaneous idle, EOS policy status"
    line = r.payload["cpuInfo"]["%Cpu(s)"]
    assert line == {"idle": 80, "user": 11, "system": 10, "hwIrq": 0,
                    "nice": 0}, f"EOS shaping: {line}"
    assert r.payload["processes"] == {}, "text-lane exclusion: never scraped"
    assert r.payload["loadAvg"] == [0.53, 0.43, 0.37]
    eng = r.payload["engines"]
    assert len(eng) == 2 and eng[0]["mastership"] == "master" \
        and eng[0]["tempC"] == 31.0 and eng[1]["idlePct"] == 100
    assert "upSeconds" not in eng[0], \
        "fixture carries no up-time; the record must not fabricate one"
    assert "cpu 20.0% used (master RE0, instantaneous; 1-min 6%)" in r.reason
    assert "load 0.53/0.43/0.37" in r.reason and "2 RE" in r.reason \
        and "text-only on Junos (excluded)" in r.reason
    print("  ✓ capture verbatim: master picked over backup; instantaneous "
          "frames, 1-min de-spikes the reason; EOS cpuInfo shape; engines[] "
          "seed the RE-redundancy widget")

    # Backup-first ordering must not steal the frame; single-RE falls back.
    r = juniper_proc({"route-engine-information": [{"route-engine": [
        rex(1, "backup", 100), rex(0, "master", 80)]}]})
    assert r.frames[0].value == 20.0, "master picked regardless of order"
    r = juniper_proc({"route-engine-information": [{"route-engine": [
        rex(0, "master", 8)]}]})
    assert r.frames[0].value == 92.0 and str(r.frames[0].status) == "CRIT", \
        "EOS policy thresholds mirrored (92 >= 85 -> CRIT)"
    for name, val in (
        ("unread / no container", {"unexpected": []}),
        ("unread / empty engines", {"route-engine-information": [{}]}),
        ("unread / rpc-error", {"rpc-error": [{"message": w2("error")}]}),
        ("unread / batch _error", {"_error": "shell_timeout"}),
        ("unread / no cpu-idle", {"route-engine-information": [{
            "route-engine": [{"slot": w2("0"),
                              "mastership-state": w2("master"),
                              "status": w2("OK")}]}]}),
    ):
        r = juniper_proc(val)
        assert str(r.state) == "UNREAD" and not r.frames, name
    print("  ✓ master-pick order-proof; policy mirrored; UNREAD lanes "
          "(no container / empty / rpc-error / no readable idle) — never ABSENT")

    # The RATIFIED deep-cap contract verdict (contract.py "environment"): the
    # shape this cap emitted as a candidate is now law; conforms() is the
    # receipt, exactly as it was for bgp/ospf/lldp.
    try:
        from contract import conforms as _conf
    except ImportError:
        from uf.core.contract import conforms as _conf
    r = juniper_environment(MX_ENV)
    viol = _conf("environment", r.payload)
    assert viol == [], f"environment contract violations: {viol}"
    print("  ✓ CONTRACT HOLDS — junos environment conforms to the ratified "
          "deep-cap contract (groups + record floor); `other`/`comment` ride "
          "as extras under floor-not-ceiling")


if __name__ == "__main__":
    run_selftests()
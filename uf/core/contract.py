"""
uglyfruit / core — the cross-vendor payload contract.

Design Note 05 §4, made executable: the per-cap PRESENT payload table promoted
from "what the Arista discriminator happens to return" to a CONTRACT every
vendor's translator must satisfy. It lives in core, beside the law — NOT in any
vendor module. It is the shared shape, not one box's evidence.

The vocabulary is deliberately EOS-named ("eos being mostly cosmetic"). That is
the whole bet of the EOS-as-contract move: the gear-proven Arista widgets bind to
these names, the EOS translator is near-identity, and every later vendor becomes
a self-contained translator INTO this shape. The contract is the FLOOR, not the
ceiling — a record MUST carry the required fields; vendor-specific extras may ride
along untouched (a widget reads only the contract fields).

The load-bearing rule (Note 05 §3, and the reason this is a file, not a
docstring): when a new vendor cannot map onto a required field, THE CONTRACT
GIVES, NOT THE TRANSLATOR. A translator forced to invent or discard a required
field is the tell that an EOS-ism leaked in. `conforms()` makes that tell
mechanical — it runs in each vendor's self-test, so the leak surfaces the first
time a second shape is measured against the contract, not after widgets depend on
it.

Absence does NOT normalize (Note 05 §3): this contract governs the PRESENT
payload only. ABSENT / UNREAD determination stays vendor-aware in each
discriminator, off that vendor's own markers. Nothing here touches the law.

Stdlib only. Python 3.10+.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapContract:
    cap: str
    required: tuple[str, ...]   # fields every record MUST carry
    optional: tuple[str, ...]   # fields a widget may read if present
    doc: str

    def violations(self, payload: object) -> list[str]:
        """Return the ways `payload` fails this contract. Empty list == conforms.

        Checks the PRESENT payload shape only: a list of records, each a dict
        carrying the required fields. Extras are allowed (floor, not ceiling).
        A non-list payload, or a record missing a required field, is reported
        per-record so a failing vendor translator names exactly what it could
        not produce — which is the whole diagnostic value of the check.
        """
        if not isinstance(payload, list):
            return [f"{self.cap}: payload is {type(payload).__name__}, "
                    f"expected list of records"]
        out: list[str] = []
        for i, rec in enumerate(payload):
            if not isinstance(rec, dict):
                out.append(f"{self.cap}[{i}]: record is {type(rec).__name__}, "
                           f"expected dict")
                continue
            for f in self.required:
                if f not in rec:
                    out.append(f"{self.cap}[{i}]: missing required field {f!r}")
        return out


@dataclass(frozen=True)
class GroupedCapContract:
    """A DEEP-cap contract: the payload is a dict of named record GROUPS, not
    one flat list. Ratified from the first real two-shape intersection
    (arista environment × juniper environment — Note 05 §4's waited-for second
    shape): both vendors' env decompose into the same three displayable groups
    even though their command surfaces (3 sub-reads vs 1 flat list) and their
    absence vocabularies do not converge at all.

    Floor-not-ceiling holds at BOTH levels: every declared group must exist
    (an empty list is a fine answer; a missing group is a translator hole),
    and extra groups (juniper's `other`) and extra top-level scalars
    (ambientC, coolingStatus) ride untouched. Absence still does not
    normalize — this governs the PRESENT payload only."""
    cap: str
    groups: tuple[str, ...]     # groups every PRESENT payload MUST carry
    required: tuple[str, ...]   # fields every record in every group MUST carry
    optional: tuple[str, ...]   # per-group enrichments a widget MAY read
    doc: str

    def violations(self, payload: object) -> list[str]:
        if not isinstance(payload, dict):
            return [f"{self.cap}: payload is {type(payload).__name__}, "
                    f"expected dict of record groups"]
        out: list[str] = []
        for g in self.groups:
            recs = payload.get(g)
            if not isinstance(recs, list):
                out.append(f"{self.cap}.{g}: missing or non-list group")
                continue
            for i, rec in enumerate(recs):
                if not isinstance(rec, dict):
                    out.append(f"{self.cap}.{g}[{i}]: record is "
                               f"{type(rec).__name__}, expected dict")
                    continue
                for f in self.required:
                    if f not in rec:
                        out.append(f"{self.cap}.{g}[{i}]: missing required "
                                   f"field {f!r}")
        return out


# ──────────────────────────────────────────────────────────────────────────
# The contracts. EOS-named by the cosmetic-convergence decision (Note 05).
#
# FLAT PROTOCOL CAPS: their EOS shape IS the protocol shape wearing EOS field
# names, so adopting it as canon is safe and a second vendor maps clean.
#
# DEEP CAPS wait for a second real shape to intersect against (Note 05 §4) —
# registering from EOS alone would enshrine an EOS-ism we can't yet see.
# `environment` GRADUATED: the juniper shape landed, the intersection exists,
# and the contract below is that intersection (the RATIFICATION AMENDMENT the
# main README queued). `transceivers` and `proc` still wait — and `optics`
# may run the contract question BACKWARD (the Junos DOM shape is richer than
# EOS inventory; Note 07 §5).
# ──────────────────────────────────────────────────────────────────────────
CONTRACTS: dict[str, CapContract | GroupedCapContract] = {
    "bgp": CapContract(
        cap="bgp",
        required=("peerAddress", "peerState"),
        optional=("description", "prefixReceived"),
        doc="one record per BGP peer; peerState is the EOS up/down word "
            "('Established' == up).",
    ),
    "ospf": CapContract(
        cap="ospf",
        required=("routerId", "adjacencyState", "interfaceName"),
        optional=("details",),
        doc="one record per OSPF neighbor; adjacencyState 'full' == up.",
    ),
    "lldp": CapContract(
        cap="lldp",
        required=("port", "neighborDevice", "neighborPort"),
        optional=("ttl",),
        doc="one record per LLDP neighbor; 'port' is the LOCAL interface.",
    ),
    "environment": GroupedCapContract(
        cap="environment",
        groups=("sensors", "fans", "power"),
        required=("name", "status", "fault"),
        optional=("tempC", "critC", "warnC",          # sensors
                  "speedPct", "comment",              # fans
                  "model", "watts", "capacityW",      # power
                  "ampsIn", "ampsOut", "volts",
                  "vacant"),                          # positively-empty slot —
                                                      # dimmed, never green/red
        doc="the FIRST deep-cap contract, ratified from the arista×juniper "
            "intersection. Three displayable groups of {name, status, fault}: "
            "`status` is the BOX'S OWN WORD verbatim (display only — a widget "
            "never interprets vendor vocabulary), `fault` is the one "
            "pre-computed verdict a widget colors on, `tempC` exists only "
            "when measured (an absent key is 'no measurement', NEVER 0). "
            "Enrichments are optional per vendor: EOS carries thresholds and "
            "PSU electricals, Junos carries fan comments; a widget renders "
            "what exists and dashes what doesn't. Top-level optional scalars "
            "(ambientC, coolingStatus, tempStatus) and extra groups "
            "(juniper's `other`) ride as extras.",
    ),
}


def conforms(cap: str, payload: object) -> list[str]:
    """Violations of `cap`'s contract by `payload`; [] == conforms.

    An unknown cap (no contract yet — e.g. a deep cap still riding raw) returns
    [] rather than raising: absence of a contract is 'not yet governed', not a
    failure. Only a REGISTERED contract that a payload breaks is a violation.
    """
    c = CONTRACTS.get(cap)
    return c.violations(payload) if c is not None else []
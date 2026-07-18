"""
uglyfruit / Tier 1.5 — the session model, in code.

Design Note 02 made physical. The headline invariant (§1):

    Exactly one session per trust posture, at most three concurrent per device.
    The ceiling isn't a resource limit; it's the trust model at the transport
    layer — "who is asking, and what may they do" has three answers, and each
    gets its own connection so the answer is legible on the wire.

        BROKER       1.5        read-only BY CONSTRUCTION   persistent
        INTERACTIVE  engineer   full CLI, ungated           persistent (own transport)
        GATED        2          allow-listed, per-cmd audit grant-scoped (in mcpssh)

What this module owns, and what it deliberately does NOT:

  * BROKER — built in full. One transport, two read consumers (widgets + AI
    reads), a coordinator that runs the UNION of their manifests as ONE batch
    per poll and slices results back (§2). Read-only is structural: a consumer
    can hand the broker capability *keys*, never command strings — there is no
    method by which a read consumer could run `conf t`. That is "the cockpit
    holds no path to an ungated command" (§3) enforced in the type system.

  * INTERACTIVE — a declared slot. Opening the engineer's own transport is the
    terminal pane's job; here it is a posture the ceiling tracks, stubbed at
    that seam.

  * GATED — a grant-scoped handle. The real gate (validate-before-connect,
    per-command audit, sessionless-per-command) lives in mcpssh's own process
    and already runs in production; rebuilding it here would defeat §3. What
    the cockpit owns is the *lifecycle*: a grant's existence IS the session,
    the iteration budget is its TTL-in-commands (§4), and every command still
    passes the (injected) validator — the held handle is efficiency, not
    authorization.

Stdlib only. Python 3.10+. Imports `reading` (Note 01) for the AI-reads output.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol

import uf.core.reading  # Note 01 — the AI-reads consumer turns its slice into Readings


# ──────────────────────────────────────────────────────────────────────────
# §1  Postures — the three answers to "who is asking, and what may they do"
# ──────────────────────────────────────────────────────────────────────────
class Posture(str, Enum):
    BROKER = "BROKER"            # 1.5, read-only by construction
    INTERACTIVE = "INTERACTIVE"  # engineer, ungated
    GATED = "GATED"             # Tier 2, grant-scoped, owned by mcpssh

    def __str__(self) -> str:
        return self.value


# ──────────────────────────────────────────────────────────────────────────
# The capability manifest = the existing collectors' command tables. A
# consumer selects *keys* from this; it never authors a command. This is the
# "fixed read manifest" (Note 02 §1) and the structural floor of read-only.
#
# uf manifest ⊇ arista/collector.py JSON_COMMANDS+TEXT_COMMANDS: it MAY declare
# capabilities the nethuds collector doesn't (e.g. `mlag` — confirmed live: the
# uf broker resolves the key and runs `show mlag | json` straight down the held
# shell, independent of the collector). Manifest is the source of truth for what
# uf polls; the collector is one downstream producer of the same shapes, not a
# ceiling on them.
# ──────────────────────────────────────────────────────────────────────────
VENDOR_MANIFESTS: dict[str, dict[str, Any]] = {
    "arista": {
        "version":    "show version | json",
        "bgp":        "show ip bgp summary | json",
        "ospf":       "show ip ospf neighbor | json",
        "lldp":       "show lldp neighbors | json",
        "mlag":       "show mlag | json",
        "routes":     "show ip route summary | json",
        "interfaces": "show interfaces status | json",
        # Optic INVENTORY (model/serial/mfg per slot), read off the same
        # `show inventory | json` that carries card/PSU/fan inventory — the
        # discriminator pulls only xcvrSlots. This is NOT optic health; DOM
        # measurements (rx/tx dBm, temp) are a separate `show interfaces
        # transceiver | json` lane that would fold in as a second sub-read.
        "transceivers": "show inventory | json",
        # COMPUTE: CPU load + process table (the old collector `proc` read). MEM
        # stays in `version` (one cap = one command); this lane owns CPU/procs.
        "proc": "show processes top once | json",
        # Multi-read capability: `show environment all | json` is an unconverted
        # command on real EOS (returns an errors envelope), so environment is
        # read as two structured sub-commands and combined by its discriminator —
        # no text parser. The reference frame (overheatThreshold, inAlertState,
        # systemStatus) arrives straight from the box.
        "environment": {
            "power":       "show environment power | json",
            "temperature": "show system environment temperature | json",
            "cooling":     "show system environment cooling | json",
        },
    },
    # ── juniper — Junos, `| display xml` (universal across JunOS versions). The
    #    transport parses these to the same array-wrapped dict `| display json`
    #    yields, so the discriminator judges structure, never raw XML (Note 05
    #    §5). Only the caps with a landed juniper discriminator are declared;
    #    add a line as each cap lands. `| no-more` suppresses the pager.
    "juniper": {
        "bgp": "show bgp summary | display xml | no-more",
        "ospf": "show ospf neighbor | display xml | no-more",
        "lldp": "show lldp neighbors | display xml | no-more",
        "version": {
             "software": "show version | display xml | no-more",
             "hardware": "show chassis hardware | display xml | no-more",
             "re":       "show chassis routing-engine | display xml | no-more",
         },
        # environment: the anchor is the SINGLE health read (the multi-read
        # composite was an EOS-ism); `fan` and `power` are ENRICHMENT subs
        # (speedPct / watts·amps·volts·capacity) under the anchored-tolerant
        # posture — they degrade, never certify. Joins are by name; the
        # eng-edge-1 captures prove PEM/fan names converge exactly across all
        # three commands. `show chassis environment pem` is DELIBERATELY
        # skipped: its DC block duplicates `show chassis power`, its temps are
        # display strings with no junos:celsius attribute, and it's one more
        # command per poll on a production edge for nothing new.
        "environment": {
            "environment": "show chassis environment | display xml | no-more",
            "fan":         "show chassis fan | display xml | no-more",
            "power":       "show chassis power | display xml | no-more",
            # thresholds: the box's OWN yellow/red alarm scale, per-sensor by
            # exact name (eng-edge-1 capture) -> warnC/critC on sensor records.
            # Near-static config data; a poll-cache is a future optimization.
            "thresholds":  "show chassis temperature-thresholds | display xml | no-more",
        },
        # optics: the vendor-specific widget category's first rich member
        # (Note 07 §5) — per-module DOM with the box's OWN per-metric
        # alarm/warn flags AND its own thresholds inline (single read; no
        # sibling-command hunt — environment's three-capture chase collapses
        # to one command here). The inverted asymmetry recorded there stands:
        # this is richer than EOS inventory-shaped transceivers, so the cap
        # appears only in this manifest and the widget renders only where the
        # feed carries it.
        "optics": "show interfaces diagnostics optics | display xml | no-more",
        # interfaces: the command-choice weigh-in Note 07 queued, settled by
        # capture — terse anchors (admin/oper for every physical, compact even
        # with the breakout zoo), descriptions ENRICHES and degrades (the
        # anchored-tolerant posture's third use). Full `show interfaces` and
        # `media` deliberately rejected: enormous on an MX, and the one field
        # they'd add (speed) the widget already dashes gracefully.
        "interfaces": {
            "terse":        "show interfaces terse | display xml | no-more",
            "descriptions": "show interfaces descriptions | display xml | no-more",
        },
        # proc/COMPUTE: single read — the routing engine IS the compute plane
        # on Junos. CPU (instantaneous, matching EOS `top once`'s sampling) +
        # 1/5/15-min averages + load + per-RE health ride one command. The
        # process table is deliberately NOT built: `show system processes *`
        # is a top(1) TEXT BLOB in <output> (capture-proven) — the text-lane
        # exclusion that killed the logging cap applies; Tier 2 fetches it on
        # escalation. MEM needs nothing here: the COMPUTE mem donut is the
        # version populate seam, already flowing.
        # §-debt: this command duplicates version's `re` sub each poll;
        # session-level command dedup is a future optimization, not churn here.
        "proc": "show chassis routing-engine | display xml | no-more",
    },
}


# ──────────────────────────────────────────────────────────────────────────
# Wiring coherence — the manifest and the discriminator registry are TWO
# registration points for one capability, and they can drift apart silently.
# This makes the drift auditable instead of latent. Three regions:
#
#   WIRED        = manifest ∩ discriminators   pollable AND interpretable.
#   UNDETERMINED = manifest − discriminators   pollable, but determine() will
#                  collapse it to UNREAD ("no discriminator"). Degrades SAFE —
#                  this is fine and expected (a key can be read for the HUD's
#                  raw payload before anyone writes its discriminator).
#   DEAD         = discriminators − manifest    a discriminator nothing can
#                  feed: it never fires on a real poll, with NO runtime signal.
#                  This is the dangerous direction — not unsafe (it can't paint
#                  a false green; it simply never runs), but it's silent dead
#                  code, the exact "added the reader, forgot the command" gap.
#
# Only DEAD is asserted against (see self-test): UNDETERMINED is a legitimate
# transitional state, DEAD is always a wiring mistake.
# ──────────────────────────────────────────────────────────────────────────
def manifest_coherence(vendor: str) -> dict[str, set[str]]:
    """Classify a vendor's capability keys by how completely they're wired."""
    manifest = set(VENDOR_MANIFESTS.get(vendor, {}).keys())
    discriminators = reading.capabilities(vendor)
    return {
        "wired":        manifest & discriminators,
        "undetermined": manifest - discriminators,   # pollable, safe-UNREAD
        "dead":         discriminators - manifest,    # interpretable, never fed
    }


# ──────────────────────────────────────────────────────────────────────────
# Transport — the single held connection. The broker resolves keys to commands
# and hands the transport *resolved commands*; the transport never sees a
# consumer. FakeTransport lets the whole model be tested with no gear.
# ──────────────────────────────────────────────────────────────────────────
class Transport(Protocol):
    def authenticate(self) -> None: ...
    def run_batch(self, commands: dict[str, Any]) -> dict[str, Any]: ...
    def close(self) -> None: ...


@dataclass
class FakeTransport:
    """In-memory transport with canned per-key output. Counts auth and batch
    calls so tests can assert 'one session, one wire' (§2)."""
    host: str
    canned: dict[str, Any]
    auth_calls: int = 0
    batch_calls: int = 0
    last_batch_keys: frozenset[str] = frozenset()
    closed: bool = False

    def authenticate(self) -> None:
        self.auth_calls += 1

    def run_batch(self, commands: dict[str, Any]) -> dict[str, Any]:
        self.batch_calls += 1
        self.last_batch_keys = frozenset(commands)
        # Returns canned output by key (a multi-read capability's canned value is
        # already the assembled {sub: value} dict — the fake skips the wire).
        return {key: self.canned.get(key, {"_error": "no canned output"})
                for key in commands}

    def close(self) -> None:
        self.closed = True


# ──────────────────────────────────────────────────────────────────────────
# §2  Read consumers. Both attach to the SAME broker session. A consumer
# declares a manifest (a set of capability keys it wants) and receives its
# slice. It has NO method that accepts a command string — read-only by
# construction, the same "can't build a thing that lies" move as Reading.
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class ReadConsumer:
    name: str
    manifest: frozenset[str]                       # capability keys, not commands
    deliver: Callable[[dict[str, Any]], None] = lambda slice_: None

    def wants(self) -> frozenset[str]:
        return self.manifest


def widget_consumer(name: str, keys: set[str], sink: list) -> ReadConsumer:
    """A HUD widget: renders raw vendor-shaped payload. Here it just records
    the slice it was handed so a test can inspect it."""
    return ReadConsumer(name, frozenset(keys),
                        deliver=lambda slice_: sink.append(slice_))


def ai_reads_consumer(name: str, keys: set[str], vendor: str,
                      sink: list) -> ReadConsumer:
    """The AI-reads consumer (Note 01). Its slice is run through capability
    determination so the model receives Readings — PRESENT/ABSENT/UNREAD — not
    raw output. Every key dispatches through `reading.determine`, so a key with
    no discriminator comes back UNREAD (undetermined), never raw-and-unjudged.
    Adding a capability or a vendor is a registration in reading.DISCRIMINATORS,
    not a new branch here."""
    def deliver(slice_: dict[str, Any]) -> None:
        sink.append({key: reading.determine(vendor, key, value)
                     for key, value in slice_.items()})
    return ReadConsumer(name, frozenset(keys), deliver=deliver)


# ──────────────────────────────────────────────────────────────────────────
# The broker — one transport, the union-batch coordinator (§2).
# ──────────────────────────────────────────────────────────────────────────
class Broker:
    """Holds one read-only session to one device and fans it out.

    Discrete in composition (widgets, AI reads) must NOT leak into discrete on
    the wire: every poll is the UNION of active consumers' manifests, run as a
    single batch, sliced back. (§2)
    """

    def __init__(self, vendor: str, transport: Transport):
        if vendor not in VENDOR_MANIFESTS:
            raise ValueError(f"no manifest for vendor {vendor!r}")
        self.vendor = vendor
        self._manifest = VENDOR_MANIFESTS[vendor]
        self._transport = transport
        self._consumers: list[ReadConsumer] = []
        self._authed = False

    def attach(self, consumer: ReadConsumer) -> None:
        # A consumer can only ask for keys the vendor actually reads. Unknown
        # key -> rejected at attach; another structural guard, not a runtime
        # surprise three polls later.
        unknown = consumer.manifest - self._manifest.keys()
        if unknown:
            raise ValueError(
                f"{consumer.name}: not in {self.vendor} read manifest: "
                f"{sorted(unknown)}"
            )
        self._consumers.append(consumer)

    def detach(self, consumer: ReadConsumer) -> None:
        self._consumers.remove(consumer)

    def _union(self) -> frozenset[str]:
        u: set[str] = set()
        for c in self._consumers:
            u |= c.manifest
        return frozenset(u)

    def poll(self) -> frozenset[str]:
        """Run one batch for the union, fan slices back. Returns the union it
        ran (for inspection/audit). One authenticate ever; one batch per poll."""
        if not self._authed:
            self._transport.authenticate()
            self._authed = True

        union = self._union()
        if not union:
            return union

        # Resolve keys -> commands HERE. Consumers never touched a command.
        commands = {k: self._manifest[k] for k in union}
        results = self._transport.run_batch(commands)

        for c in self._consumers:
            c.deliver({k: results[k] for k in c.manifest})
        return union

    def close(self) -> None:
        self._transport.close()


# ──────────────────────────────────────────────────────────────────────────
# §3–§6  The gated handle. NOT a transport the cockpit owns — a lifecycle the
# cockpit drives against mcpssh's process. Existence == an active grant (§4).
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class GatedGrant:
    """A grant made physical (§4). Born at escalation, dead at cap or close.

    The validator stands in for mcpssh's validate-before-connect, which in
    production runs in mcpssh's OWN process (§3) — injected here so this module
    models the *lifecycle*, not the gate. The held grant is efficiency: every
    command still passes the validator and is still audited. One approval does
    not buy free rein — it buys a window each command must still earn (§4).
    """
    device: str
    principal: str                                  # distinct agent principal (§6)
    iterations: int                                 # the cap = TTL-in-commands
    validate: Callable[[str], bool]                 # mcpssh's gate (injected)
    audit: list[tuple[str, str, str]] = field(default_factory=list)
    _open: bool = True

    @property
    def active(self) -> bool:
        return self._open and self.iterations > 0

    def request(self, command: str, correlation_id: str = "") -> dict:
        """Cockpit asks mcpssh to run one command inside the grant window."""
        if not self.active:
            return {"outcome": "DENIED", "reason": "grant closed / cap reached"}
        # Gate still applies — grant-scoped scopes the session, never the gate.
        if not self.validate(command):
            self.audit.append((correlation_id, command, "DENIED"))
            return {"outcome": "DENIED", "reason": "validate_command"}
        self.iterations -= 1                         # spend one unit of budget
        self.audit.append((correlation_id, command, "ALLOWED"))
        result = {"outcome": "ALLOWED", "remaining": self.iterations}
        if self.iterations == 0:                     # cap reached -> grant expires
            self._open = False
        return result

    def close(self) -> None:                         # revoke by teardown (§4)
        self._open = False


# ──────────────────────────────────────────────────────────────────────────
# §1  The ceiling — one session per posture, three per device.
# ──────────────────────────────────────────────────────────────────────────
class DeviceSessions:
    """Per-device posture registry. The ceiling is the invariant: ask for a
    posture and you get the one session for it, or it is created — never a
    second of the same posture, never a fourth posture."""

    def __init__(self, device: str, vendor: str):
        self.device = device
        self.vendor = vendor
        self._slots: dict[Posture, Any] = {}

    def broker(self, transport: Transport | None = None) -> Broker:
        """Acquire the (single) broker session. Idempotent: a second call
        returns the same broker, not a new one."""
        b = self._slots.get(Posture.BROKER)
        if b is None:
            if transport is None:
                raise ValueError("first broker() call needs a transport")
            b = Broker(self.vendor, transport)
            self._slots[Posture.BROKER] = b
        return b

    def interactive(self) -> str:
        """Declared slot. Opening the engineer's transport is the terminal
        pane's job (its own posture, own credential) — stubbed at that seam.
        §7 open question: one-per-device vs one-per-tab is unresolved."""
        if Posture.INTERACTIVE not in self._slots:
            self._slots[Posture.INTERACTIVE] = "<interactive: terminal pane seam>"
        return self._slots[Posture.INTERACTIVE]

    def grant(self, principal: str, iterations: int,
              validate: Callable[[str], bool]) -> GatedGrant:
        """Issue an escalation grant. The grant's existence IS the gated
        session; there is no gated slot without one (§4)."""
        if self.gated is not None:
            raise ValueError("a gated grant is already open for this device")
        g = GatedGrant(self.device, principal, iterations, validate)
        self._slots[Posture.GATED] = g
        return g

    @property
    def gated(self) -> GatedGrant | None:
        g = self._slots.get(Posture.GATED)
        if g is not None and not g.active:           # expired -> slot frees itself
            self._slots.pop(Posture.GATED, None)
            return None
        return g

    def postures(self) -> set[Posture]:
        # Touch .gated so an expired grant doesn't show as occupying a slot.
        _ = self.gated
        return set(self._slots)


# ──────────────────────────────────────────────────────────────────────────
# Self-test — proves the Note 02 invariants with no gear. python session.py
# ──────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    OK, BAD = "\u2713", "\u2717"

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"  {OK if cond else BAD} {name}" + (f"  — {detail}" if detail else ""))

    def _try_attach(b: Broker) -> bool:
        """True iff attaching a junk-key consumer is refused at attach."""
        try:
            b.attach(ReadConsumer("rogue", frozenset({"conf_t"})))
            return False
        except ValueError:
            return True

    # Canned device state: an ospf reading with one neighbor down, plus a couple
    # of other keys so the union has something to slice.
    canned = {
        "ospf": {"vrfs": {"default": {"instList": {"1": {"ospfNeighborEntries": [
            {"routerId": "10.0.0.1", "adjacencyState": "full"},
            {"routerId": "10.0.0.2", "adjacencyState": "2-Way"},
        ]}}}}},
        "bgp": {"vrfs": {"default": {"peers": {"172.16.7.4": {"peerState": "Active"}}}}},
        "lldp": {"lldpNeighbors": [{"neighborDevice": "eng-spine2"}]},
    }
    tx = FakeTransport(host="eng-spine1", canned=canned)
    dev = DeviceSessions(device="eng-spine1", vendor="arista")

    print("§2  broker fan-out — two consumers, one session, one wire")
    broker = dev.broker(transport=tx)
    widget_sink: list = []
    ai_sink: list = []
    widget = widget_consumer("hud", {"bgp", "ospf", "lldp"}, widget_sink)
    ai = ai_reads_consumer("ai", {"ospf"}, "arista", ai_sink)
    broker.attach(widget)
    broker.attach(ai)

    union = broker.poll()
    check("one batch ran the UNION of manifests",
          tx.batch_calls == 1 and tx.last_batch_keys == frozenset({"bgp", "ospf", "lldp"}),
          f"batch_calls={tx.batch_calls} keys={sorted(tx.last_batch_keys)}")
    check("widget got its slice (3 keys)", set(widget_sink[-1]) == {"bgp", "ospf", "lldp"})
    check("ai got its slice (ospf only)", set(ai_sink[-1]) == {"ospf"})
    r = ai_sink[-1]["ospf"]
    check("ai's ospf came back as a Reading, not raw",
          isinstance(r, reading.Reading) and str(r.state) == "PRESENT",
          f"{r.state}, frame {r.frames[0].label}={r.frames[0].value} [{r.frames[0].status}]")

    broker.poll()
    check("second poll did NOT re-authenticate (attach, not reconnect)",
          tx.auth_calls == 1, f"auth_calls={tx.auth_calls}")

    print("\nread-only by construction")
    check("ReadConsumer exposes no method that takes a command string",
          not any(hasattr(ReadConsumer, m) for m in ("run", "exec", "send", "command")))
    check("a key outside the read manifest is refused at attach",
          _try_attach(broker))

    print("\n§1  the ceiling — one per posture, three per device")
    check("broker() is idempotent (same session, never a second)",
          dev.broker() is broker)
    dev.interactive()
    check("interactive is a distinct posture slot", Posture.INTERACTIVE in dev.postures())

    print("\n§3/§4  gated grant — existence is the session, gate still applies")
    deny_conf = lambda cmd: not cmd.strip().startswith(("conf", "reload", "write"))
    g = dev.grant(principal="agent-ro", iterations=3, validate=deny_conf)
    check("gated slot exists only because a grant is open", dev.gated is g)
    check("ungated command is denied even inside the grant",
          g.request("configure terminal")["outcome"] == "DENIED")
    check("allowed command spends one unit of budget",
          g.request("show ip bgp neighbors 172.16.7.4")["remaining"] == 2)
    g.request("show ip ospf neighbor")
    last = g.request("show version")
    check("cap reached -> grant closes -> session dies (slot frees)",
          last["remaining"] == 0 and dev.gated is None)
    check("three postures coexisted under the ceiling, no fourth",
          {Posture.BROKER, Posture.INTERACTIVE} <= dev.postures() | {Posture.GATED}
          and len(Posture) == 3)

    print("\nwiring coherence — manifest ↔ discriminator registry stay in sync")
    coh = manifest_coherence("arista")
    check(f"wired (pollable + interpretable): {sorted(coh['wired'])}",
          coh["wired"] == {"ospf", "bgp", "lldp", "mlag", "interfaces",
                           "version", "environment", "routes", "transceivers", "proc"},
          f"{len(coh['wired'])} of 10 fully wired — manifest fully determined")
    check(f"undetermined (pollable, safe-UNREAD): {sorted(coh['undetermined'])}",
          not coh["undetermined"],
          "none — every manifest key now has a discriminator")
    check("NO dead discriminators (a reader nothing can feed)",
          not coh["dead"],
          "clean" if not coh["dead"] else f"DEAD: {sorted(coh['dead'])} — add to manifest")
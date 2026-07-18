"""
uglyfruit / host — the routing policy. The gate, as a standalone library.

This is the net-new code the whole architecture is pointing at (Vision §7): the
gate that decides Tier 1 → 1.5 → 2, holds the per-investigation iteration cap,
and enforces that **escalation is an explicit, logged tool call that the USER
approves** — so the escalation decision and the audit trail are the same artifact,
and the model can never grant itself a higher tier.

It is a *library*, not a feature welded inside a host. Binding it to nChat or a
cockpit would rebuild the one thing this architecture argues against — a layer
that can't be lifted out. So this module knows nothing about Netlapse, mcpssh,
Ollama, or Qt. It knows three abstractions — `ToolSpec`, `Tier`, `GateSession` —
plus an injected `approver` the host wires to a human. Same posture as
`identity.py`: host infrastructure, not a tier.

The claim it exists to make provable is the *negative* (Vision §8.1: host-gated,
not model-coaxed). A model cannot reach a higher tier's tools by being clever
with a prompt, because the gate enforces the boundary in THREE places:

  1. **Withheld at the registry.** `advertised_tools()` returns ONLY the tools of
     currently-granted tiers (plus the `request_escalation` door). A not-yet-earned
     tool is absent from what the model is shown, not merely discouraged.
  2. **Refused at invocation.** Even if a model fabricates a call to a tool it was
     never shown, `invoke()` checks the granted set BEFORE dispatch and returns a
     structured refusal. The tier's callable is never reached.
  3. **Approved by the user, not the model.** `request_escalation` does not grant —
     it *asks*. The grant happens only if the injected `approver` (a human, or a
     host UI standing in for one) says yes. No approver → fail-closed: the request
     is denied and logged. The model holds the request; the user holds the grant.

A refusal/denial is a *value the model receives*, not an exception — the same
envelope-not-crash discipline Tier 1 carries (`tier1_netlapse._envelope`). A
well-behaved model reads "denied" and reports its Tier 1 finding as unconfirmed,
rather than crashing; and the trail records that it was told no.

Note the division of labour: the user approves the *tier escalation* (the jump to
live access); mcpssh's allow-list still bounds *which commands* are even possible
once there. Two gates, different scopes — the human authorizes the door, the
allow-list shapes the room.

Stdlib only. Python 3.10+. Self-test: `python -m uf.host.routing`.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Union


# The one meta-tool the gate injects into every turn. Always callable (you can
# always *ask*); whether the ask is granted is the USER's decision, and either
# way it is logged. The model reaches a higher tier through this door or not at all.
REQUEST_ESCALATION = "request_escalation"


def _escalation_sentinel(**_kw: Any) -> Any:  # pragma: no cover - never dispatched
    raise RuntimeError("request_escalation is handled by the gate, not dispatched")


# ──────────────────────────────────────────────────────────────────────────
# The abstractions a consumer wires into. A tier is an ordered rung on the
# escalation gradient; its tools are opaque callables the gate guards but never
# interprets. `tier_level` is a float so 1.5 sits between 1 and 2 with no special
# casing — the gradient is just a sort key.
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    tier_level: float
    fn: Callable[..., Any]
    # Optional JSON-Schema for the tool's parameters — a HOST HINT for the model's
    # tool registry (e.g. an enum of valid capabilities). The gate never reads it;
    # dispatch is unchanged. Absent -> the host introspects the callable's signature.
    params: Optional[dict] = None


@dataclass(frozen=True)
class Tier:
    level: float
    name: str
    tools: tuple[ToolSpec, ...]


@dataclass(frozen=True)
class EscalationRequest:
    """What the approver is handed to make a human decision. Carries what the gate
    knows — the requested rung and the model's stated reason; a host approver can
    pull richer context (recent commands, the device) from its own side."""
    from_levels: tuple[float, ...]
    to_level: float
    to_name: str
    reason: str
    iteration: int


# approver(request) -> (approved, note). The note rides into the audit trail, so
# a human can say *why* they denied. Mirrors identity.liveness()'s (ok, detail).
Approver = Callable[[EscalationRequest], "tuple[bool, str]"]


@dataclass
class AuditEvent:
    seq: int
    kind: str            # advertise · escalation_request · escalation_grant ·
                         # escalation_denied · refusal · invoke · result · error ·
                         # cap_exceeded · final
    detail: str
    tier_level: Optional[float] = None
    tool: Optional[str] = None
    correlation_id: Optional[str] = None
    ts: float = 0.0


@dataclass
class InvokeResult:
    """What the model gets back from a tool call. `refused` distinguishes a gate
    decision (tier not granted, denied by user, cap reached, unknown tool) from a
    genuine tool error — the model can recover from the former, not the latter."""
    ok: bool
    refused: bool
    value: Any
    reason: str
    correlation_id: str
    tier_level: Optional[float]


class GateSession:
    """One investigation. Holds the granted-tier set, the iteration cap, the
    user-approval hook, and the single ordered audit trail.

    The floor of the gradient (lowest tier) is granted at open; everything above
    is earned one rung per `request_escalation`, and each rung must be approved by
    the injected `approver`. With `approver=None` the gate is fail-closed: it can
    read the floor tier and nothing else, because no human is attached to say yes.
    """

    def __init__(self, tiers: list[Tier], cap: int = 12,
                 approver: Optional[Approver] = None,
                 clock: Callable[[], float] = time.time):
        if not tiers:
            raise ValueError("a gate needs at least one tier")
        self._tiers: list[Tier] = sorted(tiers, key=lambda t: t.level)
        self._cap = cap
        self._approver = approver
        self._clock = clock

        # name -> (spec, owning tier), across ALL tiers, so a refusal can say
        # *which* tier a tool lives at. Duplicate names would make the gradient
        # ambiguous — reject them.
        self._index: dict[str, tuple[ToolSpec, Tier]] = {}
        for t in self._tiers:
            for s in t.tools:
                if s.name in self._index or s.name == REQUEST_ESCALATION:
                    raise ValueError(f"duplicate/reserved tool name across tiers: {s.name!r}")
                self._index[s.name] = (s, t)

        self._granted: set[float] = {self._tiers[0].level}
        self._audit: list[AuditEvent] = []
        self._iters = 0
        self._seq = itertools.count(1)
        self._cor = itertools.count(1)
        self._closed = False

        self._log("advertise",
                  f"session open · gradient {[t.level for t in self._tiers]} · "
                  f"granted floor Tier {self._tiers[0].level} ({self._tiers[0].name}) · "
                  f"cap {cap} · approver={'set' if approver else 'NONE (fail-closed)'}",
                  tier_level=self._tiers[0].level)

    # ── logging ──────────────────────────────────────────────────────────
    def _log(self, kind: str, detail: str, *, tier_level: Optional[float] = None,
             tool: Optional[str] = None, correlation_id: Optional[str] = None) -> AuditEvent:
        ev = AuditEvent(seq=next(self._seq), kind=kind, detail=detail,
                        tier_level=tier_level, tool=tool,
                        correlation_id=correlation_id, ts=self._clock())
        self._audit.append(ev)
        return ev

    # ── pure queries (no side effects) — what the harness asserts against ──
    def visible_tool_names(self) -> set[str]:
        """Exactly the names the model is shown this turn. The negative claim is a
        statement about this set: an unearned tool is NOT in it."""
        return {s.name for s, t in self._index.values() if t.level in self._granted} \
            | {REQUEST_ESCALATION}

    def can_invoke(self, tool_name: str) -> bool:
        if tool_name == REQUEST_ESCALATION:
            return True
        hit = self._index.get(tool_name)
        return hit is not None and hit[1].level in self._granted

    def granted_levels(self) -> set[float]:
        return set(self._granted)

    def advertised_tools(self) -> list[ToolSpec]:
        """The tool registry the model sees this turn — granted tiers only, plus
        the escalation door. This list *is* the withholding: a host that renders
        these as the model's MCP toolset cannot expose a higher tier by accident,
        because the spec isn't here to render."""
        live = [s for s, t in self._index.values() if t.level in self._granted]
        live.sort(key=lambda s: (s.tier_level, s.name))
        nxt = self._next_tier()
        door = ToolSpec(
            REQUEST_ESCALATION,
            ("Request the next tier of access — a HUMAN approves it, not you. "
             "Higher tiers are more powerful and costly: "
             + (f"next is Tier {nxt.level} ({nxt.name})." if nxt
                else "you are already at the top tier.")),
            self._tiers[0].level, _escalation_sentinel)
        return live + [door]

    # ── the chokepoint — every model tool call goes through here ──────────
    def invoke(self, tool_name: str, **kwargs: Any) -> InvokeResult:
        cid = f"cid-{next(self._cor):04d}"

        # The cap counts every call, escalation requests included — it is the
        # runaway backstop for the whole investigation. A model that spams the
        # human with escalation asks should still hit it.
        self._iters += 1
        if self._iters > self._cap:
            self._log("cap_exceeded",
                      f"iteration cap {self._cap} exceeded on '{tool_name}'",
                      tool=tool_name, correlation_id=cid)
            return InvokeResult(False, True, None,
                                f"iteration cap {self._cap} reached for this investigation",
                                cid, None)

        if tool_name == REQUEST_ESCALATION:
            return self._escalate(str(kwargs.get("reason", "")), cid)

        hit = self._index.get(tool_name)
        if hit is None:
            self._log("refusal", f"unknown tool '{tool_name}'",
                      tool=tool_name, correlation_id=cid)
            return InvokeResult(False, True, None, f"unknown tool '{tool_name}'", cid, None)

        spec, tier = hit
        if tier.level not in self._granted:
            # The teeth: a tool that was never advertised, called anyway, dies here
            # — before its callable runs. The executable form of "the model could
            # not reach Tier 2 until the user let it."
            self._log("refusal",
                      f"'{tool_name}' is Tier {tier.level} ({tier.name}); not granted",
                      tier_level=tier.level, tool=tool_name, correlation_id=cid)
            return InvokeResult(
                False, True, None,
                f"tier_not_granted: '{tool_name}' is Tier {tier.level} ({tier.name}); "
                f"call {REQUEST_ESCALATION} and have the user approve it", cid, tier.level)

        # Granted. The tier's callable runs ONLY past this line.
        self._log("invoke", f"call '{tool_name}'{self._kw(kwargs)}",
                  tier_level=tier.level, tool=tool_name, correlation_id=cid)
        try:
            value = spec.fn(**kwargs)
        except Exception as e:  # a tool error is NOT a refusal — can't escalate past it
            self._log("error", f"'{tool_name}' raised {type(e).__name__}: {e}",
                      tier_level=tier.level, tool=tool_name, correlation_id=cid)
            return InvokeResult(False, False, None, f"tool error: {e}", cid, tier.level)
        self._log("result", f"'{tool_name}' -> {self._brief(value)}",
                  tier_level=tier.level, tool=tool_name, correlation_id=cid)
        return InvokeResult(True, False, value, "", cid, tier.level)

    def finish(self, answer: str) -> None:
        """Close the investigation. One `final` event seals the single trail."""
        self._closed = True
        self._log("final", f"investigation closed: {self._brief(answer)}")

    # ── escalation — requested by the model, APPROVED by the user ─────────
    def _next_tier(self) -> Optional[Tier]:
        return next((t for t in self._tiers if t.level not in self._granted), None)

    def _escalate(self, reason: str, cid: str) -> InvokeResult:
        # The ask is its own audit event (Vision §7: the escalation decision and
        # the trail are the same artifact). Then the USER's decision.
        self._log("escalation_request",
                  "model requested escalation" + (f" · reason: {reason}" if reason else ""),
                  correlation_id=cid)
        nxt = self._next_tier()
        if nxt is None:
            self._log("escalation_denied", "already at the top tier; nothing higher to grant",
                      correlation_id=cid)
            return InvokeResult(False, True, None, "no higher tier exists", cid, None)

        req = EscalationRequest(tuple(sorted(self._granted)), nxt.level, nxt.name,
                                reason, self._iters)

        # Fail-closed: a gate with no human attached cannot grant live access.
        if self._approver is None:
            self._log("escalation_denied",
                      f"Tier {nxt.level} ({nxt.name}) NOT granted — no approver "
                      f"configured (fail-closed)",
                      tier_level=nxt.level, correlation_id=cid)
            return InvokeResult(False, True, None,
                                f"escalation to Tier {nxt.level} needs human approval; "
                                f"none is configured", cid, nxt.level)

        # ── liveness pre-check seam (Vision §5; identity.liveness) ──
        # A host MAY consult Tier 1's last-collection verdict here and pre-empt or
        # caveat the prompt before bothering the human — "FYI this box failed its
        # last collection." Left as a documented seam, not a silent default.

        approved, note = self._approver(req)
        if not approved:
            self._log("escalation_denied",
                      f"Tier {nxt.level} ({nxt.name}) DENIED by user"
                      + (f": {note}" if note else ""),
                      tier_level=nxt.level, correlation_id=cid)
            return InvokeResult(False, True, None,
                                f"escalation to Tier {nxt.level} denied by the user"
                                + (f": {note}" if note else ""), cid, nxt.level)

        self._granted.add(nxt.level)
        self._log("escalation_grant",
                  f"Tier {nxt.level} ({nxt.name}) APPROVED by user"
                  + (f": {note}" if note else "")
                  + f"; now reachable: {[s.name for s in nxt.tools]}",
                  tier_level=nxt.level, correlation_id=cid)
        return InvokeResult(True, False,
                            {"granted_tier": nxt.level, "tier_name": nxt.name,
                             "approved": True, "note": note,
                             "now_available": [s.name for s in nxt.tools]},
                            "", cid, nxt.level)

    # ── audit access / rendering ──────────────────────────────────────────
    def audit(self) -> list[AuditEvent]:
        return list(self._audit)

    def render(self) -> str:
        sym = {"advertise": "·", "escalation_request": "?", "escalation_grant": "↑",
               "escalation_denied": "✗", "refusal": "⛔", "invoke": "→", "result": "←",
               "error": "!", "cap_exceeded": "⚠", "final": "■"}
        lines = []
        for ev in self._audit:
            tier = f"T{ev.tier_level}" if ev.tier_level is not None else "  "
            cid = ev.correlation_id or ""
            lines.append(f"  {ev.seq:>2} {sym.get(ev.kind, ' '):<2} {tier:<5} "
                         f"{cid:<9} {ev.detail}")
        return "\n".join(lines)

    # ── small formatters ──────────────────────────────────────────────────
    @staticmethod
    def _kw(kwargs: dict) -> str:
        return f" {kwargs}" if kwargs else ""

    @staticmethod
    def _brief(value: Any, n: int = 80) -> str:
        s = repr(value)
        return s if len(s) <= n else s[:n] + "…"


# ──────────────────────────────────────────────────────────────────────────
# Self-test — the gate mechanics in isolation, no model loop, no network. Proves
# the negative at all three layers (withheld + refused + user-approval), the
# fail-closed default, deny vs approve, the cap, and the single trail.
#   python -m uf.host.routing
# ──────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    OK, BAD = "\u2713", "\u2717"
    fails = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        global fails
        if not cond:
            fails += 1
        print(f"  {OK if cond else BAD} {name}" + (f"  — {detail}" if detail else ""))

    # Two canned tiers, 1.5 omitted — the money-shot gradient is
    # historical(read-only) -> live(arbitrary command). Each tool bumps a counter
    # so "the callable never ran" is checkable, not inferred. The closures are
    # shared across the gates below; only an APPROVED live call ever dispatches t2.
    calls = {"t1": 0, "t2": 0}

    def fake_diff(device: str, capability: str = "config") -> dict:
        calls["t1"] += 1
        return {"available": True, "changed": [{"peer": "198.51.100.130", "to": 375}]}

    def fake_show(device: str, command: str) -> dict:
        calls["t2"] += 1
        return {"output": f"[{device}] {command}\n  198.51.100.130 ... 375 accepted"}

    t1 = Tier(1.0, "historical/netlapse",
              (ToolSpec("historical_diff", "what changed since last capture", 1.0, fake_diff),))
    t2 = Tier(2.0, "live/mcpssh",
              (ToolSpec("send_show_command", "run an allow-listed show command live", 2.0, fake_show),))

    approve = lambda req: (True, "ok, verify it")     # the standing-in-for-a-human approver
    deny = lambda req: (False, "not during the maintenance window")

    print("the gradient floor is granted; everything above is earned AND user-approved")
    g = GateSession([t1, t2], cap=12, approver=approve)
    check("Tier 1 visible at open", "historical_diff" in g.visible_tool_names())
    check("Tier 2 WITHHELD at the registry (negative, layer 1)",
          "send_show_command" not in g.visible_tool_names(),
          f"visible={sorted(g.visible_tool_names())}")

    print("\na call to an unearned tool is REFUSED before its callable runs (negative, layer 2)")
    r = g.invoke("send_show_command", device="eng-rtr-1", command="show ip bgp summary")
    check("refused, not dispatched", r.refused and not r.ok)
    check("the Tier 2 callable NEVER RAN (counter still 0)", calls["t2"] == 0, f"t2={calls['t2']}")

    print("\nuser-approval is the third layer — the model asks, the user decides")
    gd = GateSession([t1, t2], cap=12)                       # NO approver
    gd.invoke("request_escalation", reason="verify live")
    check("no approver -> fail-closed, escalation denied, Tier 2 still withheld",
          "send_show_command" not in gd.visible_tool_names()
          and any(e.kind == "escalation_denied" for e in gd.audit()))

    gn = GateSession([t1, t2], cap=12, approver=deny)        # user says no
    gn.invoke("request_escalation", reason="verify live")
    check("user DENIES -> Tier 2 stays withheld, callable unreachable",
          "send_show_command" not in gn.visible_tool_names()
          and any(e.kind == "escalation_denied" for e in gn.audit()))
    check("the denial carries the user's note into the trail",
          any("maintenance window" in e.detail for e in gn.audit() if e.kind == "escalation_denied"))

    print("\nuser APPROVES -> the rung is granted and logged as the user's decision")
    e = g.invoke("request_escalation", reason="diff shows a peer changed; verify it is still live")
    check("grant succeeded, marked approved", e.ok and e.value["granted_tier"] == 2.0 and e.value["approved"])
    check("the ask AND the user-approved grant are both logged",
          any(x.kind == "escalation_request" for x in g.audit())
          and any(x.kind == "escalation_grant" for x in g.audit()))
    check("Tier 2 now visible", "send_show_command" in g.visible_tool_names())

    print("\nearned + approved: the same call now reaches the callable")
    r2 = g.invoke("send_show_command", device="eng-rtr-1", command="show ip bgp summary")
    check("now ok, dispatched", r2.ok and not r2.refused)
    check("the Tier 2 callable ran exactly once across all gates", calls["t2"] == 1, f"t2={calls['t2']}")
    grant_seq = next(x.seq for x in g.audit() if x.kind == "escalation_grant")
    t2_results = [x.seq for x in g.audit() if x.kind == "result" and x.tool == "send_show_command"]
    check("no live result preceded the grant (the boundary held in time)",
          all(s > grant_seq for s in t2_results), f"grant@{grant_seq} results@{t2_results}")

    print("\nthe per-investigation cap is the runaway backstop")
    gc = GateSession([t1], cap=3, approver=approve)
    outs = [gc.invoke("historical_diff", device="eng-rtr-1") for _ in range(5)]
    check("calls past the cap are refused", outs[3].refused and outs[4].refused)
    check("cap_exceeded is logged", any(x.kind == "cap_exceeded" for x in gc.audit()))

    print("\nthe whole investigation is ONE ordered trail")
    g.finish("peer 198.51.100.130 changed prefixAccepted 374->375; confirmed live.")
    seqs = [x.seq for x in g.audit()]
    check("audit seq is monotonic and gapless", seqs == list(range(1, len(seqs) + 1)),
          f"{len(seqs)} events")
    check("trail is sealed with a final event", g.audit()[-1].kind == "final")
    print("\n  ── trail (the approved investigation) ──")
    print(g.render())

    print()
    print(f"  {'all gate assertions held' if not fails else str(fails) + ' FAILED'}")
    raise SystemExit(1 if fails else 0)
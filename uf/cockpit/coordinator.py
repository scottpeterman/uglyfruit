"""
uf/cockpit/coordinator.py — the orchestration layer (INTERFACE SKETCH).

The per-tab gatekeeper the mockup needs, expressed as seams the four surfaces
bind to. This is a shape to argue with, not a finished module: the Qt signals,
timer, and modal are real; the SSH-credential source and the mcpssh MCP client
are marked SEAM and left for the wiring pass.

The load-bearing decision this file encodes: the gatekeeper is TWO objects, not
one. `DeviceSessions` (core.session) owns the physical connections and the
three-posture ceiling; `GateSession` (host.routing) owns the trust/escalation
boundary and the audit trail. The coordinator holds one of each and keeps the
wall between them — collapse them and the gate stops being a standalone library.

Four surfaces, four attach points:
    session tree   -> Fleet.devices()          (Netlapse identity, app-wide)
    terminal pane  -> coord.interactive(creds)  (INTERACTIVE posture, UNGATED)
    tier-1.5 pane  -> coord.broker_polled  signal (BROKER posture, read-only)
    llm pane       -> coord.step()/audit_event  (GateSession + GATED posture)

Two policy decisions baked in and commented at their sites:
  * the COORDINATOR owns the poll cadence, not the 1.5 pane (see start_broker)
  * one human "yes" grants the tier AND opens the gated transport (see _approve)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from uf.core import reading, session, transport, ssh_client
from uf.cockpit.bridge import McpsshBridge
from uf.cockpit.formatting import feed_to_html
from uf.host.identity import IdentityResolver
from uf.host.routing import (
    GateSession, Tier, ToolSpec, EscalationRequest, InvokeResult)
from uf.servers.tier1_netlapse import (
    NetlapseClient, CAPABILITY_TO_CAPTURE, historical_diff_impl,
    historical_snapshot_impl, historical_inventory_impl)


# ── the tree row: everything the left rail needs, straight off IdentityResolver ──
@dataclass(frozen=True)
class DeviceView:
    name: str
    vendor: str          # already translated arista_eos -> arista by the resolver
    group: str
    ip: str
    live: bool           # collect_ok — drives the green/red dot
    last: str            # collect_last — "success" / "shell_timeout" / …


# credentials are the app's to supply, per posture, per device. The coordinator
# never stores them past the connect; how the app sources them (vault, prompt,
# TetherSSH-style per-device profile) is a SEAM.
#
# DECISION (one device per tab; ceiling of 3, all from the SAME credentials):
#   broker (1.5, persistent)  ·  interactive (terminal, persistent)  ·  gated (MCP,
#   when needed). The `posture` arg is kept even though today's provider returns
#   the SAME cfg for all three — so distinct per-posture principals stay a config
#   change, not a signature change, if you ever want the device's own AAA log to
#   tell the AI's gated commands apart from the engineer's shell (see notes).
CredentialProvider = Callable[[str, str], "ssh_client.SSHClientConfig"]  # (device, posture) -> cfg


# ──────────────────────────────────────────────────────────────────────────
# Fleet — app-wide, one instance. The Netlapse view that feeds the tree, plus
# the Tier 1 tools every tab's gate will mount. Netlapse is touched HERE and
# nowhere else; a tab's live 1.5 reads go direct to the box, not through this.
# ──────────────────────────────────────────────────────────────────────────
class Fleet:
    def __init__(self, netlapse_url: str, token: Optional[str], scheme: str = "bearer"):
        # SEAM (VPN): IdentityResolver fetches over urllib, which verifies TLS by
        # default. If your local Netlapse is fronted with an internal-CA cert,
        # thread a verify-off / CA-bundle flag through make_fetch here — otherwise
        # the tree 401s-or-worse before it paints. Plain http://…:8888 (your lab
        # default) needs nothing.
        self._resolver = IdentityResolver(netlapse_url, token=token, scheme=scheme)
        self._nl = NetlapseClient(netlapse_url, token=token, scheme=scheme)

    def devices(self) -> list[DeviceView]:
        """The tree model. Grouped by .group; the dot is .live."""
        return [DeviceView(d.name, d.vendor, d.group, d.ip, d.collect_ok, d.collect_last)
                for d in self._resolver.all_devices()]

    def open(self, device_name: str) -> "SessionCoordinator":
        """Tree double-click -> a per-tab coordinator. Resolves identity now so a
        box Netlapse doesn't know fails fast, before any pane mounts."""
        ident = self._resolver.resolve(device_name)
        if ident is None:
            raise KeyError(f"{device_name!r} unknown to Netlapse")
        return SessionCoordinator(self._resolver, self._nl, ident.name, ident.vendor)

    def _exposable_capabilities(self) -> list[str]:
        """The Tier 1 capability vocabulary the model may pass — the mapped caps
        intersected with what Netlapse ACTUALLY has stored (its live capture_types
        endpoint, the shakeout's proven probe). Falls back to the full mapped set if
        the live probe fails (auth/VPN) so the tools still advertise a usable enum."""
        mapped = set(CAPABILITY_TO_CAPTURE)
        try:
            live = set(self._nl.capture_types())
            exposable = {cap for cap in mapped if CAPABILITY_TO_CAPTURE[cap] in live}
            if exposable:
                return sorted(exposable)
        except Exception:
            pass
        return sorted(mapped)

    # Tier 1 tools, bound to this fleet's Netlapse — handed to each tab's gate as
    # the read-only floor. Zero device contact; safe to mount at open.
    def tier1(self) -> Tier:
        r, c = self._resolver, self._nl
        caps = self._exposable_capabilities()
        cap_list = ", ".join(caps)
        # a real parameter schema so the model SEES the valid capabilities as an enum,
        # instead of guessing at a bare string (the gap that made it reason 'lldp is
        # not available' rather than just calling for it).
        cap_params = {
            "type": "object",
            "properties": {
                "device": {"type": "string",
                           "description": "device name (this tab's device, e.g. from the title)"},
                "capability": {"type": "string", "enum": caps,
                               "description": f"which captured capability to read — one of: {cap_list}"},
            },
            "required": ["device"],
        }
        no_params = {"type": "object", "properties": {}}
        return Tier(1.0, "historical/netlapse", (
            ToolSpec("historical_snapshot",
                     "Tier 1 (no device contact): the last CAPTURED state (raw + parsed) of "
                     f"ONE capability for a device. Valid capabilities: {cap_list}. To find "
                     "out if Tier 1 holds something, CALL this and read `available` in the "
                     "result — do NOT assume a capability is unavailable without calling.",
                     1.0, lambda device, capability="config":
                         historical_snapshot_impl(r, c, device, capability),
                     params=cap_params),
            ToolSpec("historical_diff",
                     "Tier 1 (no device contact): what was added/removed/changed for a "
                     f"capability between its two most recent captures. Valid capabilities: "
                     f"{cap_list}. `available=false` (with a reason) means Tier 1 has <2 "
                     "captures of it yet, not that the device is unreachable.",
                     1.0, lambda device, capability="config":
                         historical_diff_impl(r, c, device, capability),
                     params=cap_params),
            ToolSpec("historical_inventory",
                     "Tier 1: list every device Netlapse knows, each with its last-collection "
                     "liveness. Call this if you are unsure a device exists or is being collected.",
                     1.0, lambda: historical_inventory_impl(r),
                     params=no_params),
        ))


# ──────────────────────────────────────────────────────────────────────────
# SessionCoordinator — one per open device tab. Holds ONE DeviceSessions and
# ONE GateSession, and exposes the seams the four panes bind to.
# ──────────────────────────────────────────────────────────────────────────
class SessionCoordinator(QObject):
    # 1.5 pane connects here. Emitted once per poll with the SINGLE feed dict the
    # widgets eat — one determine_all(vendor, slice) projected to
    # {cap: {**Reading.to_dict(), payload: <payload on PRESENT else None>}}. The
    # panels (digest+payload) and the header chips (digest) read the SAME poll, so
    # they cannot disagree — the shape live_harness proved against gear.
    broker_polled = pyqtSignal(dict)            # feed: {cap: {state,frames,reason,age_s,payload}}
    broker_error = pyqtSignal(str)              # auth/poll failure — surfaced, not swallowed
    # llm pane connects here — the investigation trail in the mockup's right rail.
    audit_event = pyqtSignal(object)            # host.routing.AuditEvent
    # the header / ceiling indicator.
    posture_changed = pyqtSignal(set)           # {Posture, …} currently open

    def __init__(self, resolver: IdentityResolver, nl: NetlapseClient,
                 device: str, vendor: str, poll_ms: int = 5000, cap: int = 12):
        super().__init__()
        self.device, self.vendor = device, vendor
        self._sessions = session.DeviceSessions(device, vendor)
        self._broker: Optional[session.Broker] = None
        self._worker: Optional[_PollWorker] = None      # SSH poll runs off the GUI thread
        self._poll_ms = poll_ms

        # the gate is built with the app's approver injected — same (approved, note)
        # contract as the harness's cli_approver, only the surface differs. Tier 2
        # is mounted at construction but WITHHELD by the gate until _approve grants
        # it; mounting != advertising.
        self._approver_ui: Optional[Callable[[EscalationRequest], tuple[bool, str]]] = None
        # the gate is not built until wire() injects the real [tier1, tier2],
        # the credential source, and the approval modal (see wire()).
        self._gate: Optional[GateSession] = None
        self._cred: Optional[CredentialProvider] = None
        # the tab-lived Tier-2 transport. Constructed in wire() (cheap, no I/O),
        # OPENED lazily in _approve at the first grant, held for the tab, torn down
        # in close(). None until wire() runs (demo tabs never wire).
        self._bridge: Optional[McpsshBridge] = None

        # the latest Tier-1.5 broker feed, cached so the investigation pane can read
        # live device health as AMBIENT context (never a tool call — it is below the
        # trust boundary). The broker already emits it to the telemetry widgets; we
        # tap the SAME signal so the HUD and the AI cannot disagree about health.
        self._last_feed: dict = {}
        self.broker_polled.connect(self._cache_feed)

    def _cache_feed(self, feed: dict) -> None:
        # runs on the GUI thread (queued from the poll worker); the investigation
        # worker only ever reads the reference, so a plain assignment is safe.
        self._last_feed = feed

    def ambient_html(self) -> str:
        """Deterministic HTML tables of the CURRENT live health, rendered from the
        cached feed — the numbers are the gear's, not a model's transcription. The
        pane shows this on demand (the TABLES button) with the model out of the loop:
        correct values, no truncation, three-state honesty preserved."""
        return feed_to_html(self._last_feed, self.device, self.vendor)

    def ambient_summary(self) -> Optional[str]:
        """A one-line state digest for the transcript, so the operator can SEE that
        Tier 1.5 health is in play and which caps answered."""
        feed = self._last_feed
        if not feed:
            return None
        return " · ".join(f"{c} {(feed[c] or {}).get('state', '?')}"
                          for c in sorted(feed))

    def ambient_context(self) -> Optional[str]:
        """Render the current Tier-1.5 health as a model-readable block — the ambient
        context the investigation injects each turn. This is WHY the AI should not
        escalate for temps/optics/neighbors: it is already holding them.

        Two guarantees, learned the hard way: (1) EVERY cap's state line is always
        present — a big payload can never crowd out another cap's existence, so the
        model never reads a PRESENT cap as absent. (2) Payloads degrade per-cap under
        a fair budget; a cap whose detail won't fit says so and points at the tool /
        TABLES, rather than vanishing. Three-state honesty is verbatim throughout."""
        feed = self._last_feed
        if not feed:
            return None
        PER_CAP = 1200          # max payload chars per capability (fair share)
        DETAIL_BUDGET = 7000    # total payload chars across all caps
        out = [
            f"CURRENT LIVE HEALTH DATA for {self.device} ({self.vendor}) — Tier 1.5, "
            "already retrieved for you (read-only). When asked about health or "
            "environment, ANSWER FROM THE ACTUAL VALUES in the lines below: quote the "
            "concrete readings you see (a specific temperature in C, a wattage, a peer "
            "count and state). Never reply with placeholders or field names — e.g. say "
            "'peak 49C', not 'temperature: [value]'. Do not escalate to Tier 2 to "
            "re-fetch anything already shown here.",
            "Each line is 'cap: STATE' then its data. STATE is PRESENT (read succeeded), "
            "ABSENT (positively none), or UNREAD (no answer — never assume healthy). "
            "EVERY capability present in this device's live health is listed below; if a "
            "cap is not listed, it was not polled.",
        ]
        spent = 0
        for cap in sorted(feed):
            r = feed[cap] or {}
            state = r.get("state", "?")
            age = r.get("age_s")
            # (1) the state line ALWAYS goes in — cheap, and it is the thing the model
            # reasons over. Frames (the normalized digest) ride with it.
            line = f"- {cap}: {state}" + (f" (age {age}s)" if age is not None else "")
            frames = r.get("frames") or []
            if frames:
                fr = "; ".join(
                    f"{f.get('label')}={f.get('value')}"
                    + (f"/{f.get('ceiling')}" if f.get("ceiling") is not None else "")
                    + f" [{f.get('status')}]"
                    for f in frames[:8])
                line += f"  frames: {fr}"
            if state != "PRESENT" and r.get("reason"):
                line += f" — {r['reason']}"
            out.append(line)
            # (2) the payload is best-effort under the shared budget, per-cap capped.
            if state == "PRESENT" and r.get("payload") is not None:
                try:
                    pj = json.dumps(r["payload"], separators=(",", ":"))
                except (TypeError, ValueError):
                    pj = str(r["payload"])
                if spent >= DETAIL_BUDGET:
                    out.append("    payload: (omitted — budget spent; click TABLES or "
                               f"call historical_snapshot for {cap} detail)")
                    continue
                room = min(PER_CAP, DETAIL_BUDGET - spent)
                if len(pj) > room:
                    pj = pj[:room] + f"…(truncated; TABLES / historical_snapshot has full {cap})"
                out.append(f"    payload: {pj}")
                spent += len(pj)
        return "\n".join(out)

    def set_credentials(self, creds: CredentialProvider) -> None:
        """Give the coordinator its credential source WITHOUT the gate. Tier 1.5 is
        below the trust boundary (read-only by construction), so the broker can run
        with just this — no tiers, no approver. `wire()` is only needed to reach
        Tier 2. This is the seam that turns a live 1.5 poll on."""
        self._cred = creds

    # ── tiers / credentials injected by the app at open ──────────────────────
    def wire(self, tier1: Tier, creds: CredentialProvider,
             approver_ui: Callable[[EscalationRequest], tuple[bool, str]],
             mcpssh_url: str, mcpssh_token: Optional[str] = None,
             verify_tls: bool = True) -> None:
        """The app hands the coordinator its Tier-2 dependencies: the floor tier
        (Netlapse, app-wide), a credential source, the Qt approval modal, and the
        mcpssh endpoint. The COORDINATOR (not the app) owns the Tier-2 transport:
        it builds its own tab-lived bridge and wraps mcpssh as Tier 2 here, so the
        gated session is a device posture next to broker/interactive, not a thing
        the render pane holds.

        VPN: verify_tls=False threads verify-off through the bridge's httpx factory
        (mcpssh's TLS surface). Inert on http://; the switch for an internal-CA cert.

        Note the bridge is NOT opened here — only constructed. Nothing contacts the
        external mcpssh process until _approve opens it at the first human grant."""
        self._cred = creds
        self._approver_ui = approver_ui
        self._bridge = McpsshBridge(mcpssh_url, mcpssh_token, verify_tls)
        tier2 = build_tier2_mcpssh(self._bridge, self._sessions, self.device)
        self._gate = GateSession([tier1, tier2], cap=12, approver=self._approve)

    # ── TIER 1.5 pane: the broker. Poll runs on a WORKER THREAD. ─────────────
    def start_broker(self, widget_keys: set[str]) -> None:
        """Bring up the read-only 1.5 session and start polling on a worker thread.

        The broker's whole value is `poll() == one authenticate, one union batch,
        fanned out` (session.py §2) — so ONE loop owns the transport, and the
        coordinator owns the cadence. It runs OFF the GUI thread: the first poll
        authenticates (connect → prompt → un-paginate), which blocks for a second
        or two on a real box, and that must not freeze the window. The pane owns
        RENDER (connect to broker_polled); the worker owns WHEN and does the I/O.
        """
        if self._cred is None:
            self.broker_error.emit("no credentials — call set_credentials() or wire() first")
            return
        # Clamp the requested widgets to what THIS vendor actually reads. The
        # broker's attach() rejects any key outside the vendor manifest (a
        # deliberate structural guard) — so a caller that hands an Arista key set
        # to a Juniper box would abort the tab. The coordinator owns the vendor,
        # so it is the right place to intersect: poll the keys this box supports,
        # and SURFACE the ones it doesn't (they stay unread in the HUD, never
        # faked green) rather than crash or swallow.
        manifest_keys = set(session.VENDOR_MANIFESTS.get(self.vendor, {}).keys())
        requested = set(widget_keys)
        usable = requested & manifest_keys
        dropped = requested - manifest_keys
        if dropped:
            self.broker_error.emit(
                f"{self.vendor}: no read for {sorted(dropped)} — "
                f"those widgets stay unread")
        if not usable:
            self.broker_error.emit(
                f"{self.vendor}: none of the requested widgets are in the "
                f"manifest {sorted(manifest_keys)} — broker not started")
            return
        cfg = self._cred(self.device, "broker")
        tx = transport.ShellTransport(ssh_client.SSHClient(cfg), self.vendor)
        self._broker = self._sessions.broker(tx)
        # ONE consumer, the shape live_harness proved: determine_all over the poll
        # slice, projected to digest + payload-on-PRESENT. Writes into `holder` so
        # the worker can emit the exact feed it just built — no cross-thread read.
        vendor = self.vendor
        holder: dict = {}
        def deliver(slice_: dict) -> None:
            readings = reading.determine_all(vendor, slice_)
            holder["feed"] = {
                k: {**r.to_dict(),
                    "payload": r.payload if r.state is reading.State.PRESENT else None}
                for k, r in readings.items()
            }
        self._broker.attach(session.ReadConsumer("cockpit", frozenset(usable), deliver))
        self._worker = _PollWorker(self._broker, holder, self._poll_ms)
        self._worker.ticked.connect(self.broker_polled)     # feed rides the signal to the GUI
        self._worker.failed.connect(self.broker_error)
        self._worker.start()
        self.posture_changed.emit(self._sessions.postures())

    def set_widget_keys(self, keys: set[str]) -> None:
        """Mounting/unmounting a widget changes which keys the union polls.
        Detach the cockpit consumer and re-attach with the new key set; the next
        poll reflects it. (SEAM: trivial — detach by name, re-run the attach above.)"""
        ...

    # ── TERMINAL pane: INTERACTIVE posture — ungated, the gate never sees it ──
    def interactive(self) -> Any:
        """Hand the WebEngine/xterm bridge its own SSH shell, its own credential.
        The coordinator only records the posture is open (for the ceiling); it
        does NOT mediate these bytes. 'trust: UNGATED — you already own the CLI.'
        Per-tab, not per-device (resolves session.py §7): a shell is a tab."""
        cfg = self._cred(self.device, "interactive")       # SEAM
        self._sessions.interactive()                        # mark the posture open
        self.posture_changed.emit(self._sessions.postures())
        return ssh_client.SSHClient(cfg)                    # the pane drives it

    # ── LLM pane: drive the investigation through the gate ───────────────────
    def step(self, action_name: str, **kwargs) -> InvokeResult:
        """One turn's tool call, through the single chokepoint. The llm pane shows
        exactly self.advertised() and calls this; the gate withholds/refuses/grants
        as in the harness. Every call emits its audit events to the right rail."""
        n_before = len(self._gate.audit())
        res = self._gate.invoke(action_name, **kwargs)
        for ev in self._gate.audit()[n_before:]:
            self.audit_event.emit(ev)
        return res

    def advertised(self) -> list[ToolSpec]:
        """What the model is shown this turn — granted tiers + the escalation door.
        The 1.5 broker feed is NOT in here: 1.5 is below the trust boundary, so the
        AI reads it as ambient context, never as a gated tool call."""
        return self._gate.advertised_tools()

    # ── the hinge: one human "yes" grants the tier AND opens the transport ───
    def _approve(self, req: EscalationRequest) -> tuple[bool, str]:
        """The gate calls this on request_escalation. It shows the Qt modal
        (self._approver_ui), and on approval ALSO opens the GATED posture against
        mcpssh — so the trust decision and the transport grant are one act, at one
        audit point. Denial opens nothing. This is where the two budgets meet:
        GateSession.cap bounds the whole investigation; the GatedGrant's iteration
        budget bounds THIS live window. They run alongside — when the grant's
        budget is spent the tool below returns 'grant closed', which the model
        reads and can re-request (re-prompting you), rather than ending the run.
        """
        approved, note = self._approver_ui(req)
        if not approved:
            return approved, note
        # One human "yes" = trust decision AND transport grant, at one audit point.
        # Open the tab-lived bridge NOW (lazy, idempotent — the handshake is paid
        # once per tab, not per grant). A dead transport DENIES the escalation
        # rather than granting against nothing.
        try:
            self._bridge.ensure_open()
        except Exception as e:
            return False, f"mcpssh transport failed to open: {e}"
        # The GATED posture is the live window. `validate` DEFERS to mcpssh — the
        # command allow-list is mcpssh's job across the process boundary, not a
        # second copy here (two gates, different scopes: this window meters HOW
        # MANY; mcpssh's server-side allow-list meters WHICH). Re-grant after the
        # window closes is a documented follow-up (the gate keeps Tier 2 advertised
        # for the investigation cap; the window's "grant closed" is the model's cue).
        if self._sessions.gated is None:
            self._sessions.grant(
                principal="ai-investigator",
                iterations=6,                               # live-window TTL-in-commands
                validate=_defer_to_mcpssh)
        self.posture_changed.emit(self._sessions.postures())
        return True, note

    def close(self) -> None:
        if self._worker is not None:
            self._worker.stop()                 # signals the loop, joins the thread
        if self._broker is not None:
            self._broker.close()
        if self._bridge is not None:
            self._bridge.close()                # tear down the Tier-2 loop-thread


# ──────────────────────────────────────────────────────────────────────────
# The poll worker — owns the SSH transport off the GUI thread. The first poll
# authenticates (blocking); every poll runs the union batch and emits the feed
# the consumer built. Qt delivers `ticked`/`failed` queued to the coordinator's
# (GUI) thread, so the pane's applyFeed and runJavaScript run where they must.
# Only this thread ever touches the session (session.py §2; live_harness rule).
# ──────────────────────────────────────────────────────────────────────────
class _PollWorker(QThread):
    ticked = pyqtSignal(dict)                   # the feed the deliver() just built
    failed = pyqtSignal(str)

    def __init__(self, broker: "session.Broker", holder: dict, poll_ms: int, parent=None):
        super().__init__(parent)
        self._broker = broker
        self._holder = holder
        self._poll_ms = poll_ms
        self._stop = False

    def run(self) -> None:
        while not self._stop:
            try:
                self._broker.poll()             # first poll authenticates; then union batch
                self.ticked.emit(dict(self._holder.get("feed", {})))
            except Exception as e:              # auth or transport failure — surface it
                self.failed.emit(f"{type(e).__name__}: {e}")
            waited = 0
            while waited < self._poll_ms and not self._stop:
                self.msleep(100)
                waited += 100

    def stop(self) -> None:
        self._stop = True
        self.wait(4000)


# ── small helpers / seams ────────────────────────────────────────────────────
def _defer_to_mcpssh(_command: str) -> bool:
    """The GatedGrant's per-command validator. It returns True on purpose: the
    command allow-list is mcpssh's authority, enforced server-side in mcpssh's OWN
    process across the boundary (deny-precedence, pipe/injection blocking). Copying
    it here would be a second, drifting source of truth — the exact conflation the
    architecture warns against (co-location is organization, not authorization).
    So this window meters HOW MANY commands; mcpssh meters WHICH."""
    return True


def build_tier2_mcpssh(bridge: McpsshBridge, sessions: session.DeviceSessions,
                       device: str) -> Tier:
    """Wrap the running mcpssh server as Tier 2. The tool's device is BOUND to the
    tab (only `command` is the model's to choose), so a gated live command can only
    touch THIS device — tighter than Tier 1, which may read any box's history.

    Dispatch order per call: the GatedGrant window (below the gate) meters the live
    budget and can return 'grant closed'; only an in-budget command crosses the MCP
    boundary to mcpssh, whose server-side allow-list is the real command gate. The
    bridge is a coordinator-owned client to a SEPARATE PROCESS — never an import.
    Proven end-to-end against the real gate + real GatedGrant (fake bridge only)."""
    def send_show_command(command: str) -> dict:
        grant = sessions.gated
        if grant is None:                                    # window expired / never opened
            return {"outcome": "grant closed",
                    "reason": "escalation window expired — call request_escalation again"}
        verdict = grant.request(command)                     # local window meter + validate
        if verdict.get("outcome") != "ALLOWED":
            return verdict                                   # grant closed / cap — model re-requests
        out = bridge.call_tool(                              # across the process boundary
            "send_show_command", {"device_name": device, "command": command})
        out["remaining"] = verdict.get("remaining")          # let the model pace itself
        return out

    return Tier(2.0, "live/mcpssh",
                (ToolSpec("send_show_command",
                          "Tier 2: run one allow-listed show command live on this device "
                          "(read-only; passes mcpssh's server-side allow-list)",
                          2.0, send_show_command),))
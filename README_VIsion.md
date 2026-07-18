# uglyfruit

> The fruit of three years of building. Solo.

A **reference architecture** for AI-assisted network investigation: tiered retrieval —
**historical**, **structured-live**, and **arbitrary-live** device state — behind a single
escalation loop where the tier boundary *is* a trust boundary. The argument is proven by
primitives that already run in production; the only net-new code is the policy that relates
them.

![type](https://img.shields.io/badge/type-reference%20architecture-blue)
![tiers](https://img.shields.io/badge/tier%20tools-in%20production-brightgreen)
![gate](https://img.shields.io/badge/routing%20gate-in%20progress-orange)
![license](https://img.shields.io/badge/license-GPL--3.0-green)

### At a glance

| Tier | Answers | Tool | State |
|---|---|---|---|
| **1 — Historical** | "What changed since last capture?" | Netlapse | In production |
| **1.5 — Structured live** | "What's its health right now?" | nethuds / nhd | Collectors in production · agent surface WIP |
| **2 — Arbitrary live** | "Run this specific command." | mcpssh | In production |
| **Routing policy** | Gates the escalation, owns the trust boundary | *(net-new library)* | The work |

The three tier tools exist and run today against production gear. What this repo argues — and
demonstrates — is that retrieval for AI network operations should be **tiered**, that the tier
boundary should be a **trust boundary**, and that the missing middle everyone skips
(structured-live health on uninstrumented gear) is the piece that makes the whole thing safe
and cheap. The net-new code is a small routing-policy library and a harness that proves the
gate holds. Everything else is already shipped.

---

## What this is — and what it isn't

This is a **reference architecture**, not a product. Its claims are meant to survive
reimplementation: if someone rebuilds the whole thing in Go with different tools and the
boundaries still hold, the architecture won. The three production tools below are proof the
layers are buildable, not the thing being sold.

The transferable claims, none of which depend on a line of this code:

1. **Retrieval should be tiered**, and an investigation should escalate only as far up the
   gradient as the question requires.
2. **The tier boundary is a trust boundary** — separate processes, separate credentials,
   separate audit — and the host must *enforce* it, not merely encourage it.
3. **Structured-live on uninstrumented gear (Tier 1.5) is the missing middle.** It is what the
   commercial live-only products lack and can't cheaply bolt on.

Everything past this point is the case for those three claims, the tools that instantiate each
layer, and an honest account of which seams are proven and which are sketched.

---

## 1. Thesis

An AI assistant investigating a production network needs an accurate picture of device state.
The naive way to get it — connect over SSH and let the model run commands — is simultaneously
the most expensive, the least safe, and the slowest path to that picture. The opposite extreme
— answer only from stored snapshots — is cheap and safe but blind to the present moment.

The right design is neither. It is a **gradient of retrieval tiers** where data freshness,
device contact, query freedom, context cost, and trust-cost all rise together, and an
investigation escalates only as far up the gradient as the question actually requires. Most
questions never reach the top. The ones that do arrive there already narrowed by the cheaper
tiers below — and they cross an explicit, enforced boundary to get there.

---

## 2. The three tiers

| | **Tier 1 — Historical** | **Tier 1.5 — Structured live** | **Tier 2 — Arbitrary live** |
|---|---|---|---|
| **Question it answers** | "Was it like this before? What changed?" | "What is the device's health *right now*?" | "Run this specific command to dig into the anomaly." |
| **Data freshness** | Last snapshot (stale by design) | Live, this second | Live, this second |
| **Device contact** | None | One SSH login, fixed reads | SSH, model-directed commands |
| **Query surface** | Stored index | **Fixed, curated per vendor** | **Model-chosen** (allow-listed) |
| **Output shape** | Parsed + diffed | Parsed, normalized, reference-framed | Raw output (parsed where templates exist) |
| **Context cost** | Low | Low (pre-digested) | High (scrollback) |
| **Trust posture** | Read-only, no contact | **Read-only by construction** | Gated: allow-list + audit |
| **Instrumentation needed** | Device must be snapshotted | **None — IP + creds** | Topology-aware, works cold |

### Escalation logic

```
Question arrives
  │
  ├─ Tier 1   (Netlapse)   "what did it look like / what changed?"   ── often sufficient
  │         │ insufficient → need current state
  ├─ Tier 1.5 (nethuds)    "structured health, live, normalized"      ── usually sufficient
  │         │ anomaly surfaced → need to drill into a specific subsystem
  │   ═══════ trust boundary ═══════ escalation must be requested and granted
  └─ Tier 2   (mcpssh)     "arbitrary allow-listed show command"      ── last resort, gated
```

The loop stops at the first tier that answers the question. Tiers 1 and 1.5 are read-only and
safe; the ordering between them is a cost preference. The double line is the part that has to
be *enforced*: the host does not silently let the model reach Tier 2. The model must call
`request_escalation`, and only then does the host advertise Tier 2's tools for subsequent
turns. A hard iteration cap protects the top tier from runaway loops against live gear.

---

## 3. Why the gradient matters — and where the work actually is

It is tempting to sell this on **cost discipline**: most investigations resolve at Tier 1 or
1.5, cheap and bounded, and never pay the token and trust cost of arbitrary live commands.
That is true, and it is the benefit most exposed to erosion — token costs fall, context windows
grow, and the marginal cost of just letting a capable model SSH in and look around drops every
quarter. Stake the architecture there and you're betting against the trend line.

What doesn't erode is the **trust axis**. Gating arbitrary command execution on production gear
behind an explicit escalation is correct regardless of what tokens cost — the blast radius of a
model running config-adjacent commands on a live edge router is not a function of price. So the
cost argument is the surface and the trust boundary is the substance. **The gradient exists to
keep most investigation on the safe side of a trust boundary, and to arrive at the unsafe side
already narrowed.** That premise is sound permanently; the cost one is sound for now.

Which tells you where the architectural work is concentrated. This is, honestly, **two cheap
read surfaces and one gated one.** The cleverness is not in the rungs between the cheap two —
both are read-only, so their ordering is a preference, not a boundary. The cleverness is in
**the gate** (the enforced Tier-2 boundary) and in **Tier 1.5 existing at all.** That's not a
knock on the design; it's a map of where to spend rigor. Almost all of it goes to the gate and
the middle tier.

---

## 4. The tools that prove each layer

The architecture is not aspirational. Each tier maps to an existing, working tool. What's
missing is the connective tissue, not the layers.

### Tier 1 — Netlapse *(in production)*

Versioned network-state snapshots with a semantic diff engine, git-backed dual-artifact storage
(raw + parsed JSON), and an Oxidized-compatible REST API. Running against ~109 production
devices across IAD/FRA/NRT, published to PyPI. It answers "what did this device look like at the
last capture, and what diffed since" with zero device contact. That is Tier 1, complete.

### Tier 1.5 — nethuds / nhd *(collectors in production; agent surface is the gap)*

`nethuds` ships each vendor dashboard as a FastAPI server that proxies a live SSH session and
emits structured telemetry — routing engines, BGP, thermals, optics, interfaces, OSPF
adjacencies, the event log — normalized across Arista, Juniper, Cisco IOS, and Linux. `nhd` is
the desktop cockpit that wraps those servers with a session tree, an encrypted vault, and
key-capable auth. Two properties make it the natural Tier 1.5:

- **No prior instrumentation.** It needs nothing but an IP and credentials — not an inventory,
  not streaming telemetry, not a pre-built dashboard. This is the exact blind spot Tiers 1 and
  2 share (both assume the device is known), so Tier 1.5 is also the cold-open fallback for
  uninstrumented gear: the box you've never seen, mid-incident, with no baseline in your head.
- **Self-referencing readings.** Every value carries its own reference frame — *46.0 °C against
  a ceiling*, *51 peers / 0 down* — so a number means something on a box the model has never
  met. The model receives a judgement-ready health object, not raw output it must interpret.

This is also what makes a local model viable as the investigator: it reasons over pre-digested,
reference-framed health objects instead of burning context parsing scrollback. (See §6.)

### Tier 2 — mcpssh *(in production, registered, live)*

A FastMCP server wrapping a paramiko SSH client, with a Secure Cartography topology map as
inventory. Eight tools, a server-side read-only command allow-list, bearer-token auth,
structured audit logging, connect-candidate fallback (mgmt IP → interface IP), group
concurrency, and TextFSM-based structured parsing. Registered in Claude Code, validated against
live Arista/Cisco gear. Arbitrary-but-gated live diagnostics, reached only on escalation.

---

## 5. Tier 1.5 is the missing middle — and its read-only guarantee is stronger than Tier 2's

Tier 1.5 is the genuinely novel layer, so it earns its own section.

Without it, an investigation that needs current health has to jump straight from stale snapshots
to arbitrary command execution — paying full token and trust cost to ask a bounded question
("is this box hot? are its optics clean? are its neighbors up?"). Tier 1.5 answers exactly those
bounded questions live, with a fixed query surface and pre-digested output, so the model never
chooses a command and never parses raw text to learn the device is healthy.

**Read-only by construction — literally.** mcpssh's allow-list is a *filter on a command-string
surface that exists*: there's a `run_command(str)` shape, and a guard rejects the bad ones.
nethuds' collectors expose **no command-string surface to the caller at all.** The commands are
baked into each vendor collector; the caller gets structured telemetry back. So when a widget is
wrapped as an MCP tool, its signature is `get_optics()`, not `run("show … optics")` — there is
no parameter through which an arbitrary command can travel. The dangerous surface doesn't exist
to be guarded, rather than existing-but-filtered. **Tier 1.5 is therefore *more* trustworthy
than Tier 2 by the shape of its interface, not less** — which is exactly why it's safe to leave
always-available while only the top tier is gated.

**But it orients; it does not conclude.** The normalization that makes Tier 1.5 cheap is lossy.
"51 peers / 0 down" is a perfect summary right up until the peer that matters is the one the
count masks — and an investigation is precisely the situation where the digested-away detail is
sometimes the answer. That's not a flaw, it's a job description: **Tier 1.5 narrows the question
and hands a pre-narrowed question across the gate to Tier 2, which concludes.** The failure mode
is letting decisions get *made* at 1.5 rather than *framed* there. The division of labor is the
feature.

---

## 6. The SSH widget — the unit that makes Tier 1.5 composable

A **widget** is one SSH-backed collector for one device subsystem — optics, thermals, routing
engines, BGP/OSPF adjacencies, interfaces, alarms, the event log — that reads its slice,
normalizes the result into a vendor-independent shape, and attaches each value's reference frame
(threshold, ceiling, expected count). The vendor-specific knowledge lives *inside* the widget;
every consumer sees the same normalized object.

**Dual consumer, one seed.** The same widget serves two audiences with no runtime coupling
between them:

- **Rendered**, it is a HUD panel — what a human reads at a glance in nhd.
- **Exposed**, it is an MCP tool — what an agent calls at Tier 1.5.

This is the seed-artifact pattern at panel granularity. Add a widget and *both* the human
cockpit and the agent gain the capability at once. The agent pulls only the slice the question
needs — `get_optics`, not `get_everything` — keeping Tier 1.5 context cost bounded and the
fixed-command-set security property intact per widget.

### Transport is a seam, not an assumption

The widget is named for its first transport, not its only one. Optics health is optics health
whether it arrived via `show interfaces diagnostics optics` (SSH), an
`openconfig-platform/transceiver` GET (RESTCONF/gNMI), or a transceiver-MIB walk (SNMP). The
transports have fundamentally different shapes, so a single `fetch(command) -> string` interface
fits SSH and breaks on the other two. What is genuinely common is the *output*: the normalized,
reference-framed health object. **Abstract at the widget's populate level, not at a uniform
raw-fetch primitive.**

Recommendation: build the seam now, defer the adapters. Define the output contract
transport-agnostically, stand up a thin populate-from-transport adapter interface with SSH as the
first and only implementation, and leave RESTCONF and SNMP as declared-but-empty slots. The
expensive thing to retrofit is a contract that bakes in SSH assumptions; the cheap thing to defer
is the adapters, which need not be written until a device population justifies them.

---

## 7. The net-new code: a routing policy and a harness

Everything above already runs. The connective tissue does not — and it is deliberately *small*,
because most of what a full host would do is presentation over primitives that already exist.

**The routing policy is a standalone library — the seed.** It owns the gate: how the host
decides Tier 1 → 1.5 → 2, the per-investigation iteration cap, the enforcement that the tier
boundary is a token/trust boundary (separate processes, tokens, audit), and the principle that
escalation is an explicit, logged tool call rather than a silent decision. Because escalation
*is* a tool call, every tier transition becomes a first-class audit event — the escalation
decision and the audit trail are the same artifact.

Critically, the policy is a *library*, not a feature welded inside a host. Binding it to a
specific app would rebuild the one thing this architecture argues against — a layer that can't
be lifted out. As a standalone seed, every consumer attaches to the same contract with no
coupling between them.

**The harness proves the gate — headless.** The validation artifact is not an app. It's a
script that holds the three tier tools as MCP sources, advertises only Tier 1 to start, feeds
the model one real question against real gear, and *asserts*: the model could not reach SSH
until it called `request_escalation`; the host added Tier 2 only then; the cap held; the whole
sequence is in one log. That demonstrates the only un-shown claim in the architecture — the gate
— at the exact point of skepticism. The three tiers lighting up is what's already proven; the
gate refusing the escalation until it's earned is the money shot.

**The cockpit is one face — and it already mostly exists.** A human running an investigation
wants a terminal, the widgets, the conversation, and Netlapse results in one pane of glass.
That's an extension of **nhd**, not a new app: nhd already hosts `QWebEngineView` panels, an
editable session tree, the vault, and an embedded terminal under window controls. The widgets
are native to it; the terminal is a known panel type; Netlapse is a REST call into a panel; the
conversation pane is the one new surface. The discipline that keeps this honest: **the
conversation panel talks to the policy library, never to the panels beside it.** Co-location in
the window is layout; it must not become authorization. The human's terminal is ungated because
*you* are trusted; the agent's reach is the policy's tool set. Two command surfaces, adjacent in
the layout, governed completely differently — the trust boundary rendered as window panes.

**nChat is one possible consumer, not the host.** A FastAPI/React app proxying a local Ollama
model could attach to the same policy library. So could a commercial-model host later. None of
them are required, and none couple to each other.

---

## 8. Open seams

A reference architecture is allowed to show which boundaries are proven and which are sketched.
In rough dependency order:

1. **Escalation enforcement — host-gated, not model-coaxed.** The host advertises only the
   reachable tiers per turn and adds the next on an explicit `request_escalation` call. This is
   the resolved design; tool descriptions and a system prompt are encouragement, not
   enforcement, and the iteration cap is only a runaway backstop. *Proven by the harness.*

2. **Widget output contract (transport-agnostic).** A stable JSON schema for agent consumption
   with the reference frame promoted to explicit fields (`value`, `unit`, `threshold`, `state`),
   defined independent of transport so RESTCONF/SNMP adapters can populate it later. *Sketched.*

3. **Tier-1.5 MCP server.** A thin FastMCP wrapper — parallel to mcpssh — exposing each widget
   as its own tool, reusing the nhd vault and SSH path. **Watch-item:** the read-only-by-
   construction guarantee survives only while widget parameters never interpolate model-supplied
   strings into commands. A useful `get_optics(interface)` must validate `interface` against the
   inventory the collector already holds — constrained types, never free strings reaching a
   command. Lose that on one convenience parameter and Tier 1.5 quietly becomes Tier 2 without
   the audit trail. *Sketched; the discipline is the spec.*

4. **Poll vs. on-demand for Tier 1.5.** A genuine fork. A *standing poll* (collector sweeps the
   cockpit on an interval; the MCP server reads slices from the last sweep) makes reads free and
   bounds freshness by the poll interval — right for a warm device already in a tab. But it
   doesn't reduce device contact, so slice-addressability is real at the *read* layer, not the
   *fetch* layer. The cold-open case wants *on-demand slice fetch* (connect → run only the
   requested slice → return). Likely both, keyed on whether the device is warm. The caching
   policy (TTL, force-refresh) is per-mode. *Open.*

5. **Cross-tier device identity.** SC topology is already the seed feeding mcpssh's inventory, so
   promote it to the canonical identity authority: the question narrows to how Netlapse hostnames
   and cold-open IPs map onto SC node identities. Cold-open devices SC doesn't know live in a
   provisional-identity space until reconciled. *Sketched.*

6. **Unified audit.** Tier 2 already emits structured audit. Tier 1.5 reads and the routing
   layer's escalation decisions write to the same trail, so an entire investigation — every tier
   touched, every command, every model turn — is reconstructable. Mostly falls out of escalation-
   as-tool-call. *Partly proven.*

7. **Credential redaction before context** (borrowed from Transit AI). Strip PEM blocks,
   encrypted passwords, keys, and tokens out of *any* tier's output before it reaches the model,
   substituting per-conversation ordinals. The model reasons about credential equivalence without
   seeing bytes. *Sketched.*

---

## 9. External validation — demand vs. differentiation

**Transit AI**, a commercial cross-platform SSH client with a read-only AI agent (launched by
Knox Hutchinson), is worth reading in two ways. First, **demand**: an independent party arrived
at the same read-only-AI-over-SSH problem and is shipping it 


# Core Principles

These are the claims the architecture exists to defend — the ones meant to
survive reimplementation. Rebuild every tool below in another language with
other libraries; if these still hold, the architecture won. They are stated
before the tiers and the tools because everything downstream is in service of
them, not the other way around.

> **Proven vs sketched.** Three tools instantiate these principles and run in
> production today; the boundaries that relate them are specified and partly
> sketched. Where something is *built* and where it is still *argued* is marked
> throughout — and naming that line is itself one of the principles below.

---

**1. Retrieval is tiered.** An investigation escalates only as far up the
gradient — historical, structured-live, arbitrary-live — as the question
actually requires. Most questions never reach the top; the ones that do arrive
there already narrowed by the cheaper tiers below. The loop stops at the first
tier that answers.

**2. The tier boundary is a trust boundary — and that, not the tiering, is the
substance.** Crossing from read-only device state to model-directed commands on
live gear is an escalation the host *enforces* — separate process, separate
credentials, separate audit — never merely encourages. The cost argument for
tiering erodes as tokens cheapen and context windows grow; the trust argument
does not. The blast radius of a model running config-adjacent commands on a live
edge router is not a function of price. The architecture is staked on the axis
that doesn't move.

**3. The missing middle is the safety mechanism, not a fence in front of it.**
Structured-live health on uninstrumented gear — Tier 1.5 — is what keeps the
model off the CLI: a tier rich enough that most questions resolve before the
boundary is ever reached. It follows that the middle must be **capability-rich
and vendor-shaped**, not normalized to a lowest common denominator. A flattened
middle starves itself and drives the very escalation it exists to prevent. The
normalization that's allowed is the *envelope* — what can be asked, and whether
a reading is in frame — never the payload.

**4. Three states, not two.** Every reading is **present**, **absent**, or
**unread** — and *absent* ("the device answered: it has none of this") is never
collapsed with *unread* ("the device did not answer"). A tier that cannot tell
those apart cannot be trusted to be where an investigation stops, because a
false negative read as clean absence becomes a confident wrong conclusion — the
exact failure this design exists to prevent. **Absence must be positively
evidenced; silence defaults to unread.** This guarantee is only as strong as
every collector's ability to hold the distinction — per vendor, per subsystem,
indefinitely — which is where the rigor is spent.

**5. Tier 1.5 orients; it does not conclude.** Digested state surfaces what is
out of frame and carries what it *could not read* up into the answer as a
bounded caveat, rather than dropping it. The tier's job is to narrow the
question and hand it up honestly — not to close it. A conclusion that silently
omits an unread subsystem is a worse failure than one that names its own blind
spot.

**6. Say what is built and what is argued.** The architecture's credibility is
its honesty about its own seams. Proven primitives and sketched boundaries are
marked as such, always — in the docs, in the demos, in the talk. A claim that
*sounds* shipped before it is built is the one failure mode that discredits
everything around it, and it is the easiest one to commit by accident in a
persuasive artifact.

---

<!-- Principles are additive. New ones extend this list; they do not revise the
     six above without an explicit note on what changed and why. Keep each one
     transferable — a claim a reimplementation would inherit — not an
     implementation detail. Those belong in the design notes. -->
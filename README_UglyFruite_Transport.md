# uglyfruit — The Transport & Read Subsystem

### Design document and build map · the SSH piece as its own project

> **The SSH layer is not plumbing under uglyfruit; it is a project with its own
> scope, its own gaps, and its own finish line.** It is the granular SSH client,
> the parse/error-contract adapter that feeds the determination engine, the
> three-posture session model, and the per-vendor read strategies that turn one
> shell into trustworthy structured state. This document is its spine: the single
> place that holds current state, the gap register, and the build order — so
> picking the work back up is reading one status block, not replaying a chain of
> notes. The notes remain; their role changes. They become dated build-log
> artifacts that move named rows in this document, not the substrate the project
> is reconstructed from.

![type](https://img.shields.io/badge/type-subsystem%20design%20%2B%20build%20map-blue)
![role](https://img.shields.io/badge/role-continuity%20spine-purple)
![transport](https://img.shields.io/badge/read%20strategies-2%20of%204%20wired%20%C2%B7%20json%20%2B%20xml-yellow)
![arista](https://img.shields.io/badge/arista-10%20built%20%C2%B7%208%20gear--proven%20%2B%20live-brightgreen)
![juniper](https://img.shields.io/badge/juniper-4%20of%2011%20%C2%B7%20gear--proven-brightgreen)
![contract](https://img.shields.io/badge/cross--vendor%20contract-flat%20caps%20governed-brightgreen)
![auth](https://img.shields.io/badge/per--posture%20creds-unwired-red)
![hostkey](https://img.shields.io/badge/host%20key-AutoAddPolicy%20project--wide-red)

---

## 0. Status — start here

> *This block is the canonical state of the subsystem. It is the one thing to
> read when resuming. When a note lands or a gate run completes, this block is
> updated; the note records **what changed here**, dated. If the block and a note
> disagree, the block is wrong and gets fixed — it is never left stale.*

**Proven on gear (the subsystem's PRESENT set)**
- Transport bring-up holds on live Arista EOS and Cisco IOS: connect → prompt →
  un-paginated → held shell stable across polls (no stale-desync). Read
  serialization on the held shell is now hardened by a **bounded `resync()`**
  between reads — drain residue, one newline, read at most `settle`s for the
  *already-known* prompt, no re-detection, no `find_prompt`-style 3 s sleep. (See
  §3; the heavy `find_prompt` is fine once at auth, pathological per-read.)
- The brace-slice recovers clean JSON from echo+prompt-wrapped EOS `| json` output.
- The collector error contract is reproduced in the adapter: parse-fail / no-json /
  empty → `_error` → **UNREAD**, never a green frame.
- **All eight Arista manifest capabilities are gear-proven for PRESENT/ABSENT**
  on live boxes (`eng-tor-1`, DCS-7280SRA-48C6-F, EOS 4.33.1.1F, and others):
  `ospf`, `bgp`, `lldp`, `interfaces`, `version`, `routes`, `environment` returned
  PRESENT; `mlag` returned ABSENT via the explicit `state:"disabled"` string. Each
  confronts a different epistemic shape — empty container (ospf), errors envelope +
  process indicator (bgp), explicit status string (mlag), absence-undeterminable
  (lldp), absence-physically-impossible (interfaces/version/routes/environment),
  vrfs-shape-with-inverted-absence (routes). See Note 03 §2–3.
- **`environment` is a multi-read capability** — `power` + `temperature` + `cooling`,
  each its own `| json`, combined by one discriminator under **strict completeness**
  (any sub unread → UNREAD, never a partial green). This **retired the text parser**:
  the planned text-parser strategy for environment was replaced by structured JSON
  sub-reads, deleting the regex parser's empty-skeleton false-green entirely. §4
- **Wiring coherence is guarded.** `manifest_coherence(vendor)` partitions every key
  into wired / undetermined / dead, and the session self-test fails on any *dead*
  discriminator (a reader nothing can feed). Manifest ⊇ collector table — the manifest
  may declare capabilities the old collector didn't (e.g. `mlag`, run straight down the
  held shell).
- **Auth is vendor-conditional, and key-file auth is now gear-proven isolated.**
  `allow_agent` / `look_for_keys` are **config fields** now (defaults preserve prior
  behavior — `True`/`False`), so the agent question is answered per-run, not
  hardcoded. On Arista EOS, **key-file auth is proven with the agent explicitly off
  and isolated** — `eng-tor-1`, RSA `id_rsa`, 6/6 gates green, the gate column
  recording `Auth under test: key-file` (a green Gate 1 now certifies the *key*, not
  an ambient agent rescuing a throwaway password). The private-key loader walks
  ed25519 → ECDSA → RSA and reports a missing passphrase distinctly from an
  unsupported format. Strict Cisco IOS still wants agent-first off for *password*
  auth (§5.1) — and that default is the open fork, not yet settled. `pkey` set still
  bypasses the agent question entirely.
- The broker holds one read-only session across consumers; reads are the UNION of
  manifests, one batch, sliced back; read-only is structural (consumers hand keys,
  never command strings). A manifest key may now resolve to a **dict of named
  sub-commands** (the multi-read seam); the transport assembles `{sub: value}` and the
  broker's union/slice logic is untouched — and has *stayed* untouched across the XML
  strategy, a second vendor, and composite caps landing on top of it. The broker is
  the quiet load-bearing piece: read-only-by-construction + union-batch absorbed all
  of it without a line changed.
- **Juniper is a live second vendor — `bgp`, `ospf`, `lldp`, `version` gear-proven
  PRESENT** on `eng-edge-1` (MX10003, dual-RE, JUNOS 23.2R1-S2.5, 610 d uptime).
  The **XML read strategy is wired**: `transport._read_one` forks json | xml | text;
  `| display xml | no-more` → `_extract_juniper_xml` → `_xml_to_juniper_dict` yields the
  same parsed structure the JSON lane does, so the discriminator judges a parsed dict,
  never raw angle brackets. Parse lane forks three ways; the law forks zero. The bgp
  namespace bug (declaration-stripping unbinds `junos:` prefixes → `ParseError`) is
  closed — attributes are preserved intact, which the `version` cap then *depends* on
  (`junos:seconds` for numeric uptime). See Notes 06–07.
- **The module split landed.** `uf/core/law.py` is the vendor-agnostic leaf (State/
  Status/Frame/Reading/classify — imports nothing from the project, so no vendor can
  cycle through it); `uf/core/reading.py` holds the registry + `determine`/
  `determine_all`/`capabilities`; `uf/core/vendors/{arista,juniper}.py` own their
  discriminators, translators, absence helpers, and fixtures. **"Add a vendor = a
  module + 2 lines" is proven, not aspirational** — `reading.py` is one import + one
  `DISCRIMINATORS.update(...)`; `session.py` is one manifest block.
- **The cross-vendor payload contract exists and is enforced.** `uf/core/contract.py`
  holds `CapContract` + `conforms()`; the flat protocol caps (`bgp`/`ospf`/`lldp`) are
  governed by EOS-named canonical shapes every vendor's translator must satisfy on
  PRESENT. `conforms(cap, payload) == []` runs in each vendor's self-test, so an
  EOS-ism leaking into the contract fires mechanically before a widget depends on it.
  **The deep caps (`environment`/`transceivers`/`proc`) are deliberately UNGOVERNED** —
  no contract, waiting for a second real shape to intersect against (Note 05 §4). The
  load-bearing rule holds: when a vendor can't map a required field, **the contract
  gives, not the translator** — but only after no per-vendor fallback chain reaches it
  (Note 07 §1's refinement, forced by lldp).
- **Multi-read now carries two strictness postures, not one.** STRICT completeness
  (`arista environment` — any sub UNREAD → whole cap UNREAD, health can't be
  partially certified) and **anchored-tolerant** (`juniper version` — one sub is the
  identity anchor and fails to UNREAD; the rest degrade honestly and the reason NAMES
  the degraded subs). The dict-spec transport seam serves both with zero changes;
  `run_batch`'s dict lane handled `version` — the first Junos composite — untouched.

*Run log (2026-07-01) — the Juniper crossing. Second vendor live on `eng-edge-1`
(MX10003, JUNOS 23.2R1-S2.5, dual-RE): BGP · OSPF · LLDP chips green, 64/64 BGP peers
Established, nameplate rendered from the `version` PRESENT payload (`RE master (2)`),
all through the **Arista widgets with zero widget changes** — Junos payloads land in
exactly the shape the widgets bind. This session moved §0/§4/§6/§8/§9/§10: it landed
the XML read strategy, the module split (`law.py` + `vendors/`), the cross-vendor
`contract.py`, the anchored-tolerant posture, and settled the vendor-normalized fork
for the flat caps. Remaining debt on these four caps: per-cap ABSENT markers are
educated (Junos "not running" wording) but **gear-unconfirmed** — only PRESENT is
proven; one capture each against a no-protocol box closes it (Note 07 debt §1).*

*Run log — full 8-of-8 manifest determined live on `eng-tor-1` (Arista, EOS
4.33.1.1F), 6/6 gates green. The `environment` holdout was a **command-surface**
finding, not a discriminator bug: `show environment temperature|json` is deprecated
on 4.33.1.1F (returns a 145-byte non-JSON error); `show system environment
temperature|json` is the live form. So the `| json` command surface is
platform/version-variable, not just per-capability — a new Note 03 finding (the command surface, to slot beside §5). Gate 3 (absent
marker) and Gate 6 (host key) carry forward as gear-pending blockers.*

*Auth run (2026-06-29) — key-file auth validated end-to-end on `eng-tor-1` with
the agent isolated off: 6/6 gates green, `authenticated via: key-file` in the column.
This session moved §0/§5.1/§5.2/§9-item-0: it landed the `allow_agent`/`look_for_keys`
config knob, the harness `--agent` isolation + posture recording, and the multi-type
key loader (ed25519/ECDSA/RSA) with a passphrase-vs-format error split. It did **not**
land the §5.1 agent-off *default* — that is now an explicit open fork (§5.1), because a
password run still carries the agent under `--agent auto`.*

**Live, blocking, or unwired (the subsystem's work)**
- **Read strategies: 2 of 4.** Arista invoke-shell `| json` and Juniper invoke-shell
  `| display xml` are both wired and gear-proven (the multi-read seam lives inside the
  JSON strategy; the XML strategy reuses it whole). Cisco parser and Linux shell+`/proc`
  paths remain seams — return `{"_raw":…}` or nothing. §4
- **Arista determination: 10 discriminators registered.** Eight are gear-proven and
  widget-live (`bgp`/`ospf`/`lldp`/`mlag`/`routes`/`interfaces`/`version`/`environment`);
  `proc` and `transceivers` are built and registered but sit in the **producer lane** —
  their widgets (COMPUTE, inventory) are the deferred consumer-side work (Note 07 §5).
  Two old-collector reads remain unbuilt: `counters` (rate frame, forces the poll fork
  §10) and `logging` (wants the text strategy §4). §6
- **Juniper determination: 4 of 11, gear-proven.** `bgp`/`ospf`/`lldp`/`version` land
  and conform; `routing_engine` (feeds COMPUTE) is the next cap by the standing
  sequence, then `interfaces`, then `routes`. `environment` is its own milestone — the
  second real shape the deep-cap contracts have waited for since Note 05 §4. Absence
  markers for the four landed caps are gear-unconfirmed (PRESENT-only proven). §6
- **Per-posture credentials: unwired.** The client *can* take keys and now exposes the
  `allow_agent` knob the seam will set; nothing constructs the client from a vault, and
  broker/interactive/gated do not yet carry distinct credentials. The harness wires a
  single credential per run (`--agent {auto,on,off}`, `_resolve_auth`) and proves the
  client path; the vault→posture seam that would do this in the cockpit does not exist. §5
- **Host-key verification: open, project-wide.** `AutoAddPolicy()` in the new
  client and every old nhd server. Pre-production blocker on "trustworthy frames."
  §5, Note 03 §8

**Module layout:** §8. **Next, in order:** §9. **Open forks:** §10.

---

## 1. Scope — what this document owns, and what it does not

The temptation is to let this doc absorb the whole architecture. It must not. It
owns the **mechanism that turns credentials + an IP into trustworthy structured
readings**, and the coverage of that mechanism across vendors. It cites the law;
it does not restate it.

**In scope.** The granular SSH client (invoke-shell, legacy algos, prompt
detection, pagination, ANSI filtering, drain/desync handling); the transport
adapter (parse strategy per vendor, the `_error` contract, encapsulation of the
execute path); the session model (three postures, the read-only-by-construction
broker, the union-batch coordinator, the gated grant lifecycle); authentication
and host-key trust; and the **per-vendor read-strategy + capability coverage map**.

**Out of scope — owned elsewhere, cited here.**
- The determination *law* (PRESENT/ABSENT/UNREAD, the frame invariant, the
  discriminator contract) — **Note 01**. This doc consumes it.
- The per-capability absence *finding* (absence is per-(vendor, capability), no
  vendor-wide rule) — **Note 03**. This doc's coverage map is governed by it.
- The escalation *gate* and trust-boundary enforcement (the Tier-2 boundary,
  `request_escalation`, the harness) — the routing-policy library and the vision
  doc §7. This doc owns only the transport *under* the gate, not the gate.
- Tier 1 (Netlapse) and the agent host (nChat / cockpit). Consumers, not this.

The one-line test: if a claim is about *how a reading is obtained and made
trustworthy on the wire*, it lives here. If it is about *what a reading means* or
*who is allowed to ask*, it lives in a note or the routing library.

---

## 2. Architecture — the four layers the shell passes through

One held SSH connection becomes a judged reading by descending four layers, each
with a single responsibility and a guarded seam to the next.

```
  credentials + IP                                          [§5  AUTH — unwired]
        │  (vault → per-posture credential → client config)
        ▼
  SSHClient            invoke-shell, legacy algos, prompt detect,   [PROVEN]
  (granular)           pagination kill, ANSI filter, drain + bounded resync
        │  execute_command(cmd) -> shell-DIRTY text (echo + payload + prompt)
        ▼
  ShellTransport       per-vendor parse strategy; the _error          [§4  2 of 4:
  (adapter)            contract; ENCAPSULATES the execute path;        json + xml]
                       resync() BETWEEN reads; MULTI-READ sub-commands
        │  run_batch(keys→cmd|{sub:cmd}) -> {key: value | {sub: value}}
        ▼
  Broker / session     one session per posture, ≤3 per device;        [PROVEN
  (postures)           union-batch; read-only BY CONSTRUCTION          for broker]
        │  slice -> consumer.deliver()
        ▼
  determine()          (vendor, key) -> discriminator -> Reading      [§6  arista 8/12
  (Note 01 law)        PRESENT / ABSENT / UNREAD + frame               juniper 4/11]
```

Two properties this stack must never lose, both enforced by construction rather
than discipline:

- **The execute path is private.** `ShellTransport._ssh` is hidden; only
  `authenticate / run_batch / close` are exposed. Nothing downstream of the broker
  holds a path to `execute_command`. This is the footing under "read-only by
  construction" — a read consumer cannot run a command because it has no method
  that accepts one.
- **A failed read cannot wear a frame.** The adapter maps every non-answer to
  `_error`; the discriminator maps `_error` to UNREAD; a frame is constructible
  only on PRESENT. The silent-failure mode (C6) is closed at two layers, not one.

---

## 3. The granular SSH client — why it is the seed of the subsystem

The client (adapted from the SCNG discovery client) is the part that earns
"a project in itself." It is not a thin paramiko wrapper; it is the accumulated
answer to *what real gear does when you open a shell*:

- **Invoke-shell only.** No exec mode — most network devices reject it. Every
  read rides one interactive channel.
- **Legacy algorithm support, per-transport.** Old KEX/cipher/key sets applied via
  a `transport_factory` on the individual Transport instance, never by mutating
  paramiko globally — so an ancient box and a modern one can be talked to in the
  same process without cross-contamination. SHA2-RSA retry on a *fresh* client
  (a stale transport can't renegotiate).
- **Prompt detection and expect-prompt arming** — the thing that makes serial
  command execution reliable instead of sleep-and-hope.
- **Pagination kill by shotgun** — fire every vendor's disable-paging command;
  wrong ones error harmlessly.
- **ANSI/control-sequence filtering** and **drain-before-send** desync handling —
  the unglamorous correctness that keeps poll N from parsing poll N−1's output.
- **A bounded `resync()` between reads.** Re-anchoring on a clean prompt between
  serial reads matters, but the full `find_prompt()` is the wrong tool to do it
  with per-read: it carries an unconditional `time.sleep(3)`, a collection loop, up
  to five 5 s retries, and it *re-detects* the prompt (falling back to `#`) — fine
  once at authenticate, pathological between every read (≈3–6 s × N reads of dead
  sleep, plus a prompt reset that corrupts later reads). `resync()` is the bounded
  version: drain residue, one newline, read at most `settle`s for the *already-known*
  prompt — no unconditional sleep, no re-detection, no reset. The transport calls it
  between reads (the first skips it; auth already left the shell clean), best-effort
  so a failed resync degrades one read to UNREAD rather than wedging the poll.

This is precisely the layer worth treating as a standalone library with its own
tests and its own host-key story (§10 spinout fork), because every vendor read
strategy in §4 sits directly on top of it.

---

## 4. The read-strategy seam — half-closed (2 of 4)

The transport adapter decides parse per vendor. Arista commands ending in `json` are
brace-sliced and `json.loads`-ed; Juniper commands carrying `display xml` are routed to
`_extract_juniper_xml` and parsed to the same array-wrapped dict shape via
`_xml_to_juniper_dict`; everything else is handed back raw under `_raw`. Two of the four
strategies are now wired and gear-proven. The other two vendors in nhd read by
fundamentally different shapes, and each remains an unbuilt populate-from-transport
adapter.

| Vendor | Read strategy (old nhd) | New transport status |
|---|---|---|
| **arista** | invoke-shell + `\| json` → brace-slice → `json.loads` | **wired** — gear-proven, 8 caps |
| **juniper** | invoke-shell + `\| display xml \| no-more` → `xml.etree` parse | **wired** — gear-proven on `eng-edge-1`, 4 caps; attributes preserved (the `junos:` namespace bug is closed) |
| **cisco_ios** | Netmiko + per-command parsers (`parse_version`, `parse_cpu`, …) | **seam** — `_raw`; no parser layer; `\| json` is rejected by IOS (Note 03 §7) |
| **linux** | shell + `/proc` reads + capability probe (`CAP:` markers) + `ip -j`/`sensors -j` | **seam** — `_raw`; mixed JSON/text/proc, none wired |

The architectural decision the vision doc already reached (§6, "transport is a
seam, not an assumption") held on contact with the second vendor: **abstract at the
widget's *populate* level, not at a uniform raw-fetch primitive.** The output contract is
the normalized, reference-framed reading; the populate-from-transport step is
per-strategy. The XML crossing proved the strategy set is real, not just a branch — the
adapter now holds:

- a **JSON** strategy (Arista today; Linux's `ip -j`/`sensors -j`/FRR JSON reuse it) —
  **wired**,
- an **XML** strategy (Juniper; stdlib `xml.etree`, the old collector's tool) —
  **wired**, forking three ways (json | xml | text) in `_read_one` while the law forks
  zero,
- a **text-parser** strategy (Cisco IOS, Arista `logging`, Linux text), porting the
  existing per-vendor `parsers.py` behind the same `_error` contract — **unbuilt**,
- a **proc/sysfs** strategy (Linux `/proc`, `/sys/class/thermal`) — file reads, not
  command output, but the same "answered / didn't answer → UNREAD" discipline —
  **unbuilt**.

> **Update (gear-driven): the text-parser strategy is no longer on Arista's path.**
> `environment` was the doc's headline text-parse cell (`show system environment all`
> → regex `parse_environment`). On live gear that command is *unconverted*, and the
> per-subsystem forms convert to JSON instead — so `environment` became a **multi-read
> JSON capability** (`power` + `temperature` + `cooling`, each `| json`), combined by
> one discriminator. This **deleted** the text parser for Arista and, with it, the
> parser's worst property: it returned a full *empty skeleton* on a non-matching
> parse, a green built on a non-read — the most dangerous false-negative surface in
> the matrix because it concerned thermal and power. Structured JSON has no such trap.
> The text-parser strategy is now needed only for Cisco IOS, Arista `logging`, and
> Linux text — not for Arista environment. A new Note 03 finding (multi-read / parser-retired).

**The multi-read seam.** A manifest key may resolve to a **dict of named
sub-commands** rather than a single command string. `run_batch` runs each sub and
returns `{sub: value}`; the discriminator combines them under one of **two strictness
postures**. **STRICT completeness** — PRESENT only if *all* sub-reads are structured,
else UNREAD (a partial read can't certify health, and a green over the sub that happened
to answer is the C6 trap one level up); this is `arista environment`. **Anchored-tolerant**
— one sub is the identity anchor (fails → UNREAD, and no-identity is a non-read, never
ABSENT), the rest degrade honestly and the `reason` NAMES the degraded subs; this is
`juniper version`, whose nameplate dashes a missing serial rather than greying the whole
cap over one timeout in three. Both postures ride the same seam: the broker's union/slice
logic never learns about sub-reads, and `version` — the first Junos composite — reused
the dict-spec lane `environment` paid for with zero transport changes.

**The non-negotiable:** every strategy reproduces the `_error` contract before it
reaches a discriminator. A parse that fails, an XML body that doesn't validate, a
`/proc` file that's missing — all become `_error`, never a partial structure a
discriminator might read as PRESENT. This is where a careless strategy port would
re-open the C6 hole the Arista path closed.

**Linux's `CAP:` probe is worth stealing, not just porting.** The old Linux
collector runs a capability probe first (`command -v podman && echo CAP:has_podman`,
`test -f /.dockerenv && echo CAP:is_container`, …) and only collects what the box
reports it has. That is *determination by probe* — the live-detection sibling of
the registry's static determination. It is a candidate model for how Tier 1.5
decides which capabilities a given box can even be asked about, before judging the
answers.

---

## 5. Authentication & host-key trust — the gap named, in full

The auth story is three distinct gaps that get conflated. All three bear on
production; one is now gear-proven on two vendors.

### 5.1 Auth *method* is vendor-conditional — `allow_agent` is not free (gear-proven)

The client now exposes `allow_agent` / `look_for_keys` as **config fields**, defaulting
to `True` / `False` — the prior hardcoded values, kept so nothing changes unless a caller
sets them. **With that default**, on Arista EOS this is harmless and useful — agent key
auth connects cleanly. On strict
Cisco IOS (AAA / keyboard-interactive) it is a **live failure**: paramiko's auth
order is pkey → agent keys → local keys → password, so every agent key is offered as
a separate failed attempt *before* the password, and IOS's per-session auth-attempt
cap tears the transport down before password is reached — surfacing as
`AuthenticationException('… transport shut down or saw EOF')` even though the
password is correct (manual `ssh` with the same credential succeeds). The
parenthesized `(user@host) Password:` prompt is the tell: it is keyboard-interactive,
the picky path.

This was reproduced and resolved in an earlier session: password→IOS works only with
agent-first auth suppressed; agent/key→EOS works as-is. **What has since landed**, and
**what is still open**, in the credential seam (§5.2) this feeds:

*Landed (gear-proven on EOS this session):*
- **The knob exists.** `allow_agent` is a config field; the harness drives it through
  `--agent {auto,on,off}` and a `_resolve_auth()` resolver that isolates exactly one
  credential per run and records the posture in the banner, Gate 1, and the results
  header. A key-file run under `auto` sets agent **off** and clears the password, so a
  green Gate 1 certifies the key — proven on `eng-tor-1`.
- **An explicit key sets `pkey`**, which paramiko uses regardless of `allow_agent`, so
  key auth and agent-off coexist with no conflict.
- **The loader is multi-type and legible.** ed25519 → ECDSA → RSA, with a missing
  passphrase (`PasswordRequiredException`) reported distinctly from an unsupported
  format, and per-type misses logged as "trying next type," not as failures.

*Open fork — the `auto` default for the password path:*
- The client default is still `allow_agent=True`, and `_resolve_auth` under `auto`
  turns the agent **off only for a key-only run** — a **password** run still carries the
  agent (the posture string says so: "agent ALSO enabled — may mask"). So password→strict
  IOS still needs an explicit `--agent off`; it is **not** safe by default the way §5.1
  originally prescribed.
- Two ways to settle it: **(a) principled** — `auto` means agent-off whenever *any*
  explicit credential (key or password) is given, agent-on only when nothing else is;
  IOS-safe by default, and it retires the `-p x`/agent-rescue trick (which was itself a
  green-for-the-wrong-reason). **(b) compatible** — keep today's behavior (agent off only
  for key isolation), IOS password runs pass `--agent off` explicitly.
- A quick unblock without code change either way: empty `SSH_AUTH_SOCK` for the
  invocation so paramiko finds no agent and skips to password.

The lesson generalizes Note 03's read-path finding to the *auth* path: the
transport is a per-vendor seam in **how you log in**, not only in **how you parse**.
Arista tolerates agent-first; strict IOS does not. Do not assume one vendor's auth
posture carries to another.

> One adjacent trap, same root: attempt 1 sets
> `disabled_algorithms: {'pubkeys': ['rsa-sha2-512','rsa-sha2-256']}`, which forces
> `ssh-rsa` (SHA-1) for *user pubkey auth as well as* host keys. An RSA user key is
> then signed with SHA-1, which modern EOS may reject — handled by the SHA2 retry
> (attempt 2), but an ed25519 key sidesteps it entirely. The disable is right for
> legacy host keys; just know it also taxes RSA user keys.

### 5.2 Per-posture credentials are unwired

The client config already supports the full matrix — `password`, `key_content`
(in-memory PEM), `key_file`, `key_passphrase`, and now `allow_agent` / `look_for_keys`
as explicit fields (defaulting `True` / `False`). The old
nhd project wires key auth through a vault (`vaultctl` stores `ssh_key` +
`ssh_key_passphrase`; sessions carry `key_file`). **The new subsystem wires none
of it.** `ShellTransport` receives an already-constructed client; nothing in the
new core resolves a credential from a vault and builds that client.

That is not a small omission, because Note 02 §6 makes credentials *load-bearing*:
each posture carries its own.

| Posture | Credential it should carry | Status |
|---|---|---|
| **BROKER** (1.5) | a read-only / least-privilege credential | unwired |
| **INTERACTIVE** (engineer) | the engineer's own credential, ungated | unwired (terminal-pane seam) |
| **GATED** (Tier 2) | mcpssh's credential, in mcpssh's process | lives in mcpssh; not this client |

The work: a credential-resolution seam between the vault and the client config,
keyed by posture, supporting key auth (in-memory PEM preferred over on-disk),
passphrase-protected keys, agent (opt-in, §5.1), and the `allow_agent=False`
default. Until it exists, "the broker holds a read-only session" is true about
*commands* (structural) but not yet about *credentials* (the broker can run as
anyone you hand it).

### 5.3 Host-key verification — `AutoAddPolicy`, project-wide

The client opens with `AutoAddPolicy()`: it trusts any host key. So does every old
nhd server (`linux/server.py`, `cisco_ios/server.py`, …). For a tier whose failure
mode is *silent*, a MITM yields faithfully-green frames over an attacker's box —
the standing hole under "trustworthy frames."

**Decision (Note 03 §8, restated here as the subsystem's blocker):** host-key
verification is a **pre-production blocker** on the broker transport. The fix is not
new invention — it is the TOFU host-key handling already shipped in **TetherSSH** and
half-landed in **secure-cartography-kt**, brought onto this client before the broker
is anything but a lab toy. Because this client is the seed every read strategy sits
on (§3), fixing it here fixes it for all four vendors at once. Until then, every
parity win in §6 still rides a transport that trusts any key — coverage and trust
advance on separate tracks.

---

## 6. Capability coverage — the build map across all four vendors

This is the surface to match. The old nhd collectors define it; the new design has
to grow a determination cell — *(read strategy ready · discriminator · frame)* —
for each capability it chooses to carry. A cell is "done" only when all three exist
and a real box has shown its shapes (§7).

> Legend: **✓** built, gear-proven & widget-live · **b** built & registered, but
> producer-lane (widget/gear-proof deferred) · **m** in new manifest, no discriminator
> (→ honest UNREAD) · **·** in old project, not yet in new design · **✗** excluded by
> decision.

### Arista (invoke-shell · JSON) — 10 built · 8 gear-proven & live

| Capability | Old read | New status | What "done" needed |
|---|---|---|---|
| ospf | `show ip ospf neighbor \| json` | **✓** | done (empty-`vrfs` ABSENT, adj-not-Full frame) |
| bgp | `show ip bgp summary \| json` | **✓** | done (`errors`-envelope ABSENT, process-indicator PRESENT) |
| mlag | `show mlag \| json` | **✓** | done — **ABSENT gear-proven** via `state:"disabled"`; `Inactive/Reload` guarded as PRESENT (not in old collector) |
| version | `show version \| json` | **✓** | done — PRESENT-by-existence, frameless identity (no fabricated mem ceiling) |
| interfaces | `show interfaces status \| json` | **✓** | done — PRESENT-always; frame = true faults only (errdisabled / link-up-protocol-down), not-connected is context |
| routes | `show ip route summary \| json` | **✓** | done — nhd-grade RIB composition; frame = degenerate (zero-route) RIB only; empty `vrfs` is UNREAD not ABSENT (inverted from ospf/bgp) |
| lldp | `show lldp neighbors \| json` | **✓** | done — PRESENT/UNREAD only; absence undeterminable from the read (no admin indicator), never ABSENT |
| environment | `power`+`temperature`+`cooling` `\| json` | **✓** | done — **multi-read JSON** (text parser retired); strict completeness; box's own `inAlertState`/`systemStatus`/PSU `state` as the frame |
| proc (cpu) | `show processes top once \| json` | **b** | built & registered; CPU%-vs-ceiling frame — **producer-lane**, COMPUTE widget deferred (Note 07 §5) |
| transceivers | `show inventory \| json` (xcvrSlots) | **b** | built & registered (optic inventory: model/serial/mfg per slot; NOT DOM health) — **producer-lane**, deep-cap contract still open (Note 05 §4) |
| counters | `show interfaces counters \| json` | **·** | manifest + discriminator; **rate frame** (two reads) → ties to poll fork §10 |
| logging | `show logging last 50` (text) | **·** | manifest + **text strategy** + discriminator; recent-CRIT/ERR count frame |

The eight live cells each confront a *different* epistemic shape — that distinctness
is the intellectual content, not repetition (Note 03 §2–3 and the "no ABSENT" taxonomy).
`proc` and `transceivers` are built and registered but wait on the producer lane — their
COMPUTE and inventory widgets are the deferred consumer-side work (Note 07 §5), and
`transceivers` is one of the deep caps whose cross-vendor contract is still open (Note 05
§4). What remains genuinely unbuilt is thin: `logging` (needs the text strategy §4) and
`counters` (forces the poll-vs-on-demand fork §10, since a rate needs two reads). Note
`mlag` is **net-new** — not in the old collector, run straight down the held
shell — so the manifest is a superset of the collector table, not a subset.

### Juniper (invoke-shell · XML) — 4 of 11 · strategy wired

The XML populate strategy exists (§4) and four caps are landed and gear-proven on
`eng-edge-1`. The remaining seven are the recipe run again (Note 06 §2):
discriminator + translator + two one-line registrations, absence markers VERIFY-IN-LAB.

| Capability | Read (`\| display xml \| no-more`) | New status | Notes |
|---|---|---|---|
| bgp | `show bgp summary` | **✓** | conforms; 64/64 Established live; `_JUNOS_BGP_UP="established"` — the same word EOS uses, by protocol not by EOS leaking in |
| ospf | `show ospf neighbor` | **✓** | conforms cleanly (`neighbor-id`→`routerId`, state→`adjacencyState`); `Full` matches EOS |
| lldp | `show lldp neighbors` | **✓** | conforms via **fallback chains** (`lldp-local-interface`→`-port-id`; `-remote-port-id`→`-description`) — the required-field chain is translator knowledge, not a contract give |
| version | `software`+`hardware`+`re` | **✓** | first Junos composite; **anchored-tolerant** posture; dual-RE wrapper + master-RE selection gear-closed (`RE master (2)`) |
| routing_engine | `show chassis routing-engine` | **m** | next by sequence — feeds COMPUTE donuts and the RE-redundancy widget; legacy extractor already transcribed cpu/load/temps |
| interfaces | `show interfaces …` | **·** | command choice first — full `show interfaces` is enormous on an MX; weigh `media` + terse+descriptions |
| routes | `show route summary` | **·** | after interfaces |
| environment | (deep cap) | **·** | its own milestone — the **second real shape** the deep-cap contracts have waited for since Note 05 §4; the run that decides whether a deep-cap contract exists at all |
| optics | (Junos-only) | **·** | net-new; the legacy Junos optics panel (per-module DOM) is *richer* than Arista transceivers — contract runs backward here |
| alarms | `show system alarms` | **·** | vendor-specific widget (no Arista analog); must-be-zero frame; nearly free |
| logging | (text) | **✗** | EXCLUDED by decision (Note 07 §5) — text lane exists, cap not built |

Absence vocabulary is **wholly its own** (`rpc-error`, empty XML containers) — Note 03's
lesson holds hard: EOS's `errors`-envelope and empty-map shapes do not carry across, and
the file boundary enforces it (§8). The four landed caps are PRESENT-proven; their ABSENT
branches ride educated "not running" tokens that stay UNREAD until a no-protocol box
confirms them (Note 07 debt §1). `optics` and `alarms` are net-new frames, not ports —
vendor-specific widgets that render only where the manifest feeds them.

### Cisco IOS (Netmiko · text parsers) — 0 of 12 · needs the parser strategy

`version · environment · temperature · cpu · interfaces · interfaces_desc · lldp ·
routes · logging · inventory · stp · mac_count`. IOS rejects `| json` (Note 03 §7),
so this is the **text-parser** strategy: port the existing per-command parsers
behind the `_error` contract. `stp` and `mac_count` are L2 capabilities with no
Arista/Juniper analogue — new frames.

### Linux (shell + `/proc` · capability-probed) — 0 of ~22 · the richest, most distinct

`system · cpu · memory · storage · interfaces · routes · connections · journal ·
thermal · lldp` and the **probed** set `services(systemd/rc) · docker · podman ·
nomad · gpu(nvidia/amd) · frr · bird · proxmox · libvirt · zfs · lvm · smart`.
This vendor is least like the others: capability *existence* is discovered by probe
(§4), not assumed from a manifest, and reads mix JSON (`ip -j`), sysfs files, and
text. It is also where "PRESENT/ABSENT/UNREAD" gets the most interesting — a probe
that says `CAP:has_nvidia=false` is positively-evidenced ABSENT for the GPU
capability, a clean fit for the law.

**The shape of the whole job:** four vendors, **two now proven on gear** (Arista 8
cells, Juniper 4), ~50 capability cells total, and **two of four read strategies wired**
(JSON + XML). The Arista column was the template; Juniper proved it ports as a recipe,
not a rediscovery — a discriminator + a translator + two one-line registrations per cap,
with the widget layer untouched. Cisco and Linux remain per-vendor designs governed by
the same law and the same flat-cap contract, awaiting their strategies (text-parser,
proc/sysfs).

---

## 7. The contract for filling a cell — what "done" means

Every capability cell is the same artifacts, and the two middle ones carry the
project's hardest lessons — Note 03's (absence is per-vendor) and Note 05's (the
PRESENT payload is a cross-vendor contract). This is the rule that keeps coverage
honest as it grows.

1. **A read** — a manifest entry (or, for Linux, a probe + read) the broker
   resolves. Cheap. The consumer never authors it.
2. **An absence discriminator** — *this capability's own* answer to "what does this
   box emit when the feature is gone?" **No absence branch is written from
   prediction.** EOS does not share an absence convention across its own features;
   Junos does not share EOS's; Linux's probe is a third model. For capabilities that
   are always present on a live box (version, routes, interfaces, inventory) there is
   **no absence question** — they are PRESENT-or-UNREAD, the cheap wins. For the rest,
   the discriminator's ABSENT branch is added only after a no-feature box shows its
   shape — and a dead branch written for a shape no box emits (the old empty-`instList`
   assumption) is removed, not kept "just in case" where it can fire a false verdict.
3. **A translator to the cross-vendor contract (flat caps)** — the PRESENT payload
   maps to the EOS-named canonical shape in `contract.py`, and `conforms(cap, payload)
   == []` runs in the vendor's self-test. A required field may need a per-vendor
   **fallback chain** (Junos lldp's `-port-id`/`-interface`); the chain is translator
   knowledge, and the contract *gives* only when no chain reaches the field (Note 07
   §1). Deep caps (environment/transceivers/proc) carry **no** contract yet — they ride
   raw until a second real shape arrives, and `conforms` returns `[]` for them (ungoverned,
   not failing). Absence never normalizes; this artifact governs PRESENT only.
4. **A present-frame** — the reference-framed health (`value`, `ceiling`, `status`)
   that makes the reading a *judgement*, not raw output, and keeps the capability
   Tier-1.5 (orients) rather than a passthrough. Informational capabilities
   (version, inventory) legally carry no frame; the invariant only forbids a frame on
   a *non-PRESENT* reading.

A cell with a read but no discriminator is **honest UNREAD**, not a bug (§0). The
work is to *retire* UNREADs one proven cell at a time — never to silence them.

---

## 8. Module layout — landed: the law and the vendors kept apart

**This split shipped** (2026-07-01, with the Juniper crossing). The diagnosis that drove
it held: the unwieldiness of a single `reading.py` was never "too many vendors" — it was
that vendor knowledge (manifest, discriminators) shared a file with the law (`classify`,
`Frame`, the invariant), two things that change at different rates for different reasons.
They are now separated, and adding Juniper touched neither the law nor the session core.

**The wrong fix — nhd's per-vendor-server pattern — was avoided.** nhd gave each vendor a
whole stack (collect, serve, render) because its per-vendor issues were end-to-end.
Re-monolithing here would have thrown away this design's one asset nhd lacked: `classify`
plus the registry, a **law already separate from the vendor-shaped reading**. The split
protects that separation rather than undoing it.

**What shipped — a thin vendor library behind a stable core:**

```
uf/core/
  law.py        # THE LAW ONLY — State, Status, Frame, frame_status, Reading, classify,
                #   Discriminator type. A LEAF: imports nothing from uf, so no vendor
                #   module can cycle through it. (~120 lines; changes rarely, never for
                #   a new vendor.) [renamed from the doc's planned reading.py-is-law]
  reading.py    # THE REGISTRY + mechanics — determine / determine_all / capabilities;
                #   imports the law and the vendor modules, assembles DISCRIMINATORS.
                #   (The doc planned a separate registry.py; the mechanics + re-export
                #   shim landed here instead — one fewer file, same seam.)
  contract.py   # THE CROSS-VENDOR PAYLOAD CONTRACT — CapContract, conforms(). NEW since
                #   this doc was written (Note 05 §4). Lives in core beside the law
                #   because it is shared shape, not one box's evidence. Absence does not
                #   normalize; it governs PRESENT only.
  session.py    # postures · broker · VENDOR_MANIFESTS  (manifest stayed HERE — see below)
  transport.py  # the parse strategies: _read_one forks json | xml | text; _error contract
  ssh_client.py
  vendors/
    __init__.py # explicit: from . import arista, juniper
    arista.py   # ARISTA_DISCRIMINATORS · discriminators · _eos_* absence helpers ·
                #   translators-to-contract · frames · gear-captured fixtures
    juniper.py  # JUNIPER_DISCRIMINATORS · _jval/_jnum/_first/_jlist/_jsecs family ·
                #   discriminators · translators · fixtures
```

`frames.py` is **not** extracted yet — the lazy call the doc prescribed was correct; no
shared frame builder has earned a second user. The breach-count-against-zero frame is the
first candidate when it does.

**Registration: explicit dicts, not a decorator.** The doc floated an `@discriminator`
decorator; what shipped is plainer and, for a solo maintainer on a no-magic ethos,
better — each vendor module exports one dict, and `reading.py` assembles them:

```python
# vendors/arista.py
from uf.core.law import Reading, State, Frame, frame_status, classify
ARISTA_DISCRIMINATORS: dict[tuple[str, str], Discriminator] = {
    ("arista", "ospf"): arista_ospf,
    ("arista", "bgp"):  arista_bgp,
    # …
}

# core/reading.py — the whole vendor seam, two lines per vendor:
DISCRIMINATORS: dict[tuple[str, str], Discriminator] = {}
DISCRIMINATORS.update(arista.ARISTA_DISCRIMINATORS)
DISCRIMINATORS.update(juniper.JUNIPER_DISCRIMINATORS)   # <- vendor 2
```

One `DISCRIMINATORS` dict (so `determine_all` judges a mixed poll and `capabilities(vendor)`
stays trivial); `__init__.py` imports vendors by explicit list, never entry-point scanning.
The cost is one import + one `.update()` per vendor; the benefit is that `reading.py` alone
tells you exactly what is loaded — deterministic registration, no decorator indirection.
This reaches the same "add a vendor = a module + a couple lines" the decorator promised,
without the decorator.

Two design calls held, and one diverged into an open fork:

- **The vendor module owns its absence vocabulary, and the file boundary is the
  guardrail — SHIPPED.** `_eos_errors_verdict`, `_junos_error_verdict`, the
  `full`/`established` up-predicates live inside their vendor module and are not exported.
  A Junos discriminator physically cannot reach EOS's `errors` envelope on faith — it is
  behind a boundary it would have to deliberately cross. The layout *enforces* Note 03's
  hardest lesson; Juniper's crossing proved it does (its absence markers are wholly its
  own).
- **The contract lives in core, the translators in the vendors — SHIPPED (net-new).**
  The `contract.py` split the doc never anticipated: canonical schema in core, field-mapping
  per vendor module, `conforms()` firing in each vendor's self-test. This is what makes the
  widget vendor-blind while the ABSENT decision stays vendor-aware (§7, Note 05 §3).
- **The manifest stayed in `session.py`, NOT in the vendor modules — DIVERGENCE, now a
  fork.** The doc prescribed moving `MANIFEST` into each vendor module with a **strategy
  tag** travelling per cell (`("show … | json", "json")`), which would have made the
  parse strategy a property of the cell. That did not ship: `VENDOR_MANIFESTS` is still in
  `session.py`, and `transport._read_one` **infers** strategy from the command surface
  (`| json` → JSON, `| display xml` → XML). This works cleanly today because every wired
  command self-signals its strategy — but it becomes a real question the moment a cap needs
  a strategy the command string can't signal (a Linux `/proc`/sysfs read has no `| json`
  suffix; Arista `logging` is a text read that looks like any other). At that point the
  strategy tag has to live *somewhere* explicit. **Open fork (→ §10): manifest-in-session
  with command-inferred strategy vs manifest-in-vendor-module with an explicit strategy
  tag.** Deferred cleanly until the text/proc strategies force it.

The migration was low-risk as predicted: the public surface (`determine`, `Reading`,
`State`) was preserved, code moved to where it wanted to live, and each vendor's fixtures
followed their discriminators into the vendor module. A full plugin / entry-point design
was correctly *not* built — that ceremony waits for vendors shipping separately (the
spinout fork §10), and graduating to it later rewrites no discriminator, only who imports
them.

---

## 9. Build order

Sequenced so cheap, low-risk wins land first and blockers are visible.

0. **Landed earlier (gear-validated, trivial to commit):** (a) Gate 4 reads true
   on a no-feature box — `--no-ospf` flips its expectation to ABSENT and it passes; the
   earlier `✗` was always a box-expectation mismatch, now confirmed. (b) The
   **`allow_agent` knob + harness credential isolation** (§5.1): `allow_agent`/`look_for_keys`
   are config fields; the harness's `--agent {auto,on,off}` + `_resolve_auth` isolates one
   credential per run and records the posture; the loader walks ed25519/ECDSA/RSA with a
   passphrase-vs-format error split. Key-file auth is gear-proven on EOS with the agent off.
   **Not** landed: the agent-off *default* §5.1 originally prescribed — under `auto` a
   password run still carries the agent, so password→strict IOS still needs `--agent off`.
   That default is no longer "trivial, fold in"; it is an **open fork** (§5.1), kept here as
   a pointer, not a pending commit.
1. **✓ Arista cheap wins** — `version`, `routes`, `interfaces` — **done, gear-proven.**
   `routes` shipped at nhd-grade RIB composition, not just a count. The old `inventory`
   straggler became **`transceivers`** (built & registered, producer-lane) and `proc`
   also landed built; `counters` and `logging` are the only genuinely unbuilt Arista reads.
2. **✓ Arista `lldp`** — **done.** Landed PRESENT/UNREAD-only as planned; ABSENT is
   structurally unreachable (no admin indicator in the read), not merely deferred.
3. **✓ Arista `environment` — done, but NOT via the text-parser strategy.** On gear
   the text command is unconverted; environment became a **multi-read JSON capability**
   (`power`/`temperature`/`cooling`), which **retired** the planned parser and its
   empty-skeleton false-green (§4, the multi-read finding in Note 03). The text-parser strategy is now scoped
   to Cisco IOS, Arista `logging`, and Linux text — `logging` is its remaining Arista
   consumer.
6. **✓ The module-layout refactor (§8) — LANDED.** Law split to `law.py`, registry in
   `reading.py`, vendors under `uf/core/vendors/`, explicit-dict registration (no
   decorator). Shipped with the Juniper crossing, so "add a vendor = a module + 2 lines"
   is proven, not aspirational.
6a. **✓ The cross-vendor payload contract (`contract.py`) — LANDED** (net-new, not in
   this doc's original plan). Flat caps (`bgp`/`ospf`/`lldp`) governed; `conforms()` in
   every vendor self-test; deep caps deliberately ungoverned (Note 05 §4). This was the
   decision §8's original "before vendor 2" note gestured at without naming.
7. **✓ The second vendor — Juniper (XML) — LANDED, gear-proven.** `bgp`/`ospf`/`lldp`/
   `version` on `eng-edge-1`, the XML strategy real, the recipe validated ×3 (Notes
   06–07). The read-strategy abstraction (§4) stopped being one branch and became a
   strategy set — validated. **Next Juniper caps by standing sequence:** `routing_engine`
   (feeds COMPUTE) → `interfaces` (command-choice first — MX `show interfaces` is huge) →
   `routes`, then **`environment`** as its own deep-cap milestone.
8. **Host-key verification (§5.3)** — the pre-production blocker, still open. Lives in the
   client all four strategies sit on, so it advances in parallel with coverage. Nothing
   ships to production until this lands. (Gate 6 still records `AutoAddPolicy` permissive.)
9. **Per-posture credential wiring (§5.2)** — the vault→config→posture seam, carrying
   whichever `auto`-agent default the §5.1/§10 fork settles on. Required before the
   broker's "read-only" is also true of *who it logs in as*.
10. **Cisco IOS / Linux — the remaining two strategies.** Cisco forces the text-parser
   strategy (§4); Linux the proc/sysfs + capability-probe model, which may feed back into
   how the other vendors decide *which* caps a box can be asked. Both also force the §8
   manifest-strategy-tag fork (a proc read has no `| json` to infer from).

Items 1–3, 6, 6a, 7 are **landed and gear-proven** (two vendors live, module split and
contract shipped, recipe proven); 8–9 are trust; 10 is the remaining coverage. Coverage
and trust advance independently — neither blocks the other, and production needs both. The
throughline is unchanged: retire UNREADs one gear-proven cell at a time, never silence
them.

---

## 10. Open forks

- **Vendor-normalized vs vendor-shaped payloads — SETTLED for the flat caps, OPEN for
  the deep caps.** The Juniper crossing settled it: **normalized-on-PRESENT** holds
  (`bgp`/`ospf`/`lldp` map to the EOS-named contract, widget goes fully vendor-blind),
  while **absence stays vendor-aware** in each discriminator (C6 forbids normalizing away
  the absence evidence — Note 05 §3). The deep caps (`environment`/`transceivers`/`proc`)
  remain **open**: their EOS shape encodes EOS assumptions with no protocol to anchor, so
  their contract waits for a second real shape — `juniper environment` is the run that
  decides whether a deep-cap contract exists at all.
- **Manifest location + strategy tag** (§8 divergence). `VENDOR_MANIFESTS` stayed in
  `session.py` and `transport._read_one` **infers** parse strategy from the command surface
  (`| json` vs `| display xml`). Clean today because every wired command self-signals — but
  the text and proc/sysfs strategies break the inference (a `/proc` read has no suffix to
  read). Fork: keep manifest-in-session with an added explicit strategy field, or move the
  manifest into the vendor module with a strategy tag travelling per cell (§8's original
  plan). *Open; the Cisco/Linux strategies force it (§9 item 10).*
- **The `auto`-agent default for password auth** (§5.1). Key-only runs isolate to
  agent-off and are gear-proven; password runs still carry the agent under `auto`, so
  strict IOS needs an explicit `--agent off`. Principled (agent-off whenever any explicit
  credential is given, retiring the `-p x`/agent-rescue trick) vs compatible (today's
  key-only isolation, IOS opts out by flag). *Open; settle before §5.2 wires posture creds,
  since the seam carries the chosen default.*
- **Spin the SSH client out as its own library?** It is self-contained (§3), reused
  from SCNG, and every read strategy sits on it. A standalone package with its own
  host-key story and tests would make the "transport is a seam" claim literal and let
  mcpssh / TetherSSH / this subsystem share one hardened client. The cost is a
  versioning boundary. *Open — leaning yes, after §5.3 lands so the spinout ships with
  host-key verification, not without it.*
- **Poll vs on-demand for Tier 1.5** (vision §8.4). A standing poll makes reads free and
  bounds freshness by interval (right for a warm device in a tab); cold-open wants
  on-demand slice fetch. `counters` (rate frames, §6) forces the question early —
  a rate needs two reads, which a poll provides for free and on-demand does not.
- **Probe-determination vs registry-determination.** Linux's `CAP:` probe (§4) and the
  static `DISCRIMINATORS` registry are two answers to "what can this box be asked?"
  Do they unify — a probe phase that populates a per-box capability set the registry
  then judges against? *Open; Linux forces it.*
- **UNDETERMINED as a fourth surface?** Today `determine` collapses the tool-gap to
  UNREAD with a reason. As coverage spans vendors, is a consumer ever owed
  "capability-not-supported-by-tooling" vs "device-didn't-answer" as distinct states?
  The reason string carries it; no state does. (Note 03 §9.)

---

## 11. Working agreement — how notes attach to this spine

This is the "more complete approach" to session transitions, stated as a rule so it
holds without re-deciding it each time.

- **This document is canonical state.** §0 is the truth of where the subsystem stands.
  A new work session reads §0 first and trusts it.
- **Notes are dated build-log artifacts, not the spine.** A note records one
  investigation, gear finding, or decision — *why* a cell moved, what the box actually
  returned, what got falsified. Note 03 is the model: it found a shape, killed a
  prediction, and recorded both.
- **Every note ends by naming what it moved here.** "This note moves Arista `lldp` from
  `m` to `✓` and adds the no-LLDP capture as its absence shape" — and §0 / §6 are
  edited to match in the same change. A note that doesn't move a spine row is a
  scratchpad, not a note.
- **The spine never goes stale against gear.** If a run contradicts §0, §0 is wrong and
  is fixed immediately; the contradiction is the note. The law (Note 01) is untouched
  by this churn — only state and coverage move.
- **The vision doc stays canonical for the architecture; the notes stay canonical for
  the findings; this doc is canonical for the subsystem's state and build order.**
  Three layers, no overlap: principles that survive reimplementation, evidence that
  taught the code, and the map of what is wired.

---

> The notes bootstrapped the project by accreting findings one investigation at a
> time — and that was right while the shape was still unknown. The shape is known
> now: a granular SSH client, four read strategies (two wired), a three-posture session
> model, an auth seam, a cross-vendor payload contract, and ~50 capability cells governed
> by one absence law — two vendors across it on live gear. That is enough to stop
> reconstructing the project from its notes and start building it against a spine — with
> the notes doing the one thing they're best at, which is recording what the gear taught
> on the day it taught it.
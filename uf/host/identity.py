"""
uglyfruit / host — the cross-tier identity resolver.

The three tiers key devices three different ways:

    Tier 1  (Netlapse)  device_id (int) for structured slices · node name (oxidized compat)
    Tier 1.5 (broker)   IP + credentials + a uf vendor string ("arista")
    Tier 2  (mcpssh)    the SC-topology device_name

A device named in one tier must resolve in the others. This module owns that
mapping and NOTHING ELSE — it is **host infrastructure, not a tier**. It is
imported by each tier's tool layer; it does not call, and is not called by, any
tier's *process*. That is the line that keeps "the tier boundary is a process
boundary" intact: a shared library is fine; one server reaching into another to
ask "who is this device" is the runtime coupling the architecture forbids.

The seed is Netlapse's `GET /nodes` — already a denormalized identity table
(name · ip · model · group · liveness). The one fact it lacks is the int
`device_id` the structured `capture_type`-sliced endpoints need, so the resolver
joins `GET /api/v1/devices` to pick that up. The int stays *inside* this layer;
no tool above the resolver is handed a device_id.

Canonical-identity note: SC topology is the intended long-run authority
(Vision §8.5). `/nodes` is the POC seed. The public surface here
(`resolve` / `liveness`) does not change when that swap happens — only `_load`.

Stdlib only (urllib for the default fetch). Python 3.10+.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional


# ──────────────────────────────────────────────────────────────────────────
# Vendor vocabulary — the resolver is the single source of truth for it.
# Netmiko/Netlapse/mcpssh speak `arista_eos`, `cisco_ios`, …; uf's determination
# engine (reading.VENDOR_MANIFESTS) keys on `arista`. Translate ONCE, here, so a
# tier never has to know another tier's spelling. Unknown models pass through
# unchanged AND are flagged (`vendor_known=False`) rather than silently guessed.
# ──────────────────────────────────────────────────────────────────────────
NETMIKO_TO_UF: dict[str, str] = {
    "arista_eos": "arista",
    "cisco_ios": "cisco_ios",     # uf has no IOS discriminators yet; keep the name honest
    "cisco_nxos": "cisco_nxos",
    "cisco_xr": "cisco_xr",
    "juniper_junos": "juniper",
    "linux": "linux",
}


def _normalize_vendor(model: str) -> tuple[str, bool]:
    """(uf_vendor, mapped). `mapped` means this model had an explicit entry in the
    translation table — a *translation-confidence* signal, NOT a claim that uf can
    determine the vendor. Whether a vendor is determinable is `reading.capabilities(
    vendor)`'s answer, asked at the Tier 1.5 layer, never smuggled in here: the
    resolver translates identity, it does not speak for the determination engine.
    Unmapped models pass through lowercased so they stay legible, flagged mapped=False."""
    m = (model or "").strip().lower()
    if m in NETMIKO_TO_UF:
        return NETMIKO_TO_UF[m], True
    return m or "unknown", False


@dataclass(frozen=True)
class DeviceIdentity:
    """One device, resolved across tiers. `device_id` is None when the
    `/api/v1/devices` join didn't find it — structured-slice tools then degrade
    to the name-keyed oxidized path instead of failing."""
    name: str                       # the lingua franca key (Netlapse node / mcpssh device_name)
    ip: str                         # what Tier 1.5 connects to
    vendor: str                     # uf vendor string ("arista"), for determine()
    vendor_mapped: bool             # had an explicit translation entry (NOT 'determinable')
    group: str                      # site slug
    device_id: Optional[int]        # Netlapse int id (structured slices); None if unjoined
    collect_status: str             # last Netlapse collection: "success" / "failure" / ...
    collect_last: str               # the failure reason if any: "shell_timeout", "success", …
    collect_time: str               # ISO timestamp of that last collection

    @property
    def collect_ok(self) -> bool:
        return self.collect_status.strip().lower() == "success"


# fetch_json(url) -> parsed JSON. Injectable so the resolver tests with no network.
FetchJson = Callable[[str], Any]


def make_fetch(token: Optional[str] = None, scheme: str = "bearer",
               timeout: float = 10.0) -> FetchJson:
    """Build a fetch that carries a Netlapse machine token on every call.

    Netlapse's auth gate puts the NATIVE API (/api/v1/*) behind a session; only
    the oxidized-compat reads (/nodes, /node/*) get the unauthenticated bypass,
    and only when `oxidized_public` is set. A token listed under
    `auth.api_tokens` is checked BEFORE the cookie path and clears the whole API,
    so presenting it on every request is the one uniform credential — no
    dependence on oxidized_public, works whether auth is on or off.

    scheme: 'bearer' -> Authorization: Bearer <token>;  'apikey' -> X-API-Key.
    token None -> no header (fine when Netlapse auth is disabled, or for the
    oxidized-public /nodes read).
    """

    def _fetch(url: str) -> Any:
        req = urllib.request.Request(url)
        if token:
            if scheme == "apikey":
                req.add_header("X-API-Key", token)
            else:
                req.add_header("Authorization", f"Bearer {token}")
        ctx = None
        if url.lower().startswith("https://") and not verify_tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False  # must precede CERT_NONE, or it raises
            ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:  # noqa: S310
            return json.loads(r.read().decode("utf-8"))

    return _fetch


# Back-compat default: a no-token fetch (works against an auth-disabled instance
# or the oxidized-public /nodes read). Prefer make_fetch(token) for the real API.
_urllib_fetch = make_fetch(None)


class IdentityResolver:
    """Resolve a device (by name OR ip) to a cross-tier DeviceIdentity.

    Seeded from `/nodes`, joined with `/api/v1/devices` for the int id. Cached
    with a TTL; `refresh()` forces a reload (wire it to Netlapse's `/reload` or
    a clock). Read-only: it only ever GETs identity, never mutates anything.
    """

    def __init__(self, base_url: str, fetch_json: Optional[FetchJson] = None,
                 token: Optional[str] = None, scheme: str = "bearer",
                 ttl: float = 300.0):
        self.base_url = base_url.rstrip("/")
        # Injected fetch wins (tests); otherwise build a token-aware one. The
        # token clears Netlapse's native-API auth gate uniformly (see make_fetch).
        self._fetch = fetch_json if fetch_json is not None else make_fetch(token, scheme)
        self.ttl = ttl
        self._by_name: dict[str, DeviceIdentity] = {}
        self._by_ip: dict[str, DeviceIdentity] = {}
        self._loaded_at: float = 0.0

    # ── load / cache ─────────────────────────────────────────────────────
    def _stale(self) -> bool:
        return not self._by_name or (time.time() - self._loaded_at) > self.ttl

    def refresh(self) -> None:
        self._load()

    def _ensure(self) -> None:
        if self._stale():
            self._load()

    def _load(self) -> None:
        nodes = self._fetch(f"{self.base_url}/nodes")
        # The id join is BEST-EFFORT: if /api/v1/devices is unreachable or shaped
        # unexpectedly, we still resolve everything else and leave device_id None.
        id_by_name: dict[str, int] = {}
        id_by_ip: dict[str, int] = {}
        try:
            devices = self._fetch(f"{self.base_url}/api/v1/devices")
            for d in devices or []:
                if not isinstance(d, dict):
                    continue
                did = d.get("id")
                if not isinstance(did, int):
                    continue
                nm = d.get("name")
                ip = d.get("primary_ip4") or d.get("primary_ip") or d.get("ip")
                if isinstance(nm, str):
                    id_by_name[nm] = did
                if isinstance(ip, str):
                    id_by_ip[ip.split("/")[0]] = did   # tolerate CIDR form
        except Exception:
            pass   # degrade to name-keyed paths; device_id stays None

        by_name: dict[str, DeviceIdentity] = {}
        by_ip: dict[str, DeviceIdentity] = {}
        for n in nodes or []:
            if not isinstance(n, dict):
                continue
            name = n.get("name") or n.get("full_name")
            ip = n.get("ip", "")
            if not isinstance(name, str) or not name:
                continue
            vendor, mapped = _normalize_vendor(n.get("model", ""))
            did = id_by_name.get(name) or id_by_ip.get(ip)
            ident = DeviceIdentity(
                name=name,
                ip=ip if isinstance(ip, str) else "",
                vendor=vendor,
                vendor_mapped=mapped,
                group=n.get("group", "") or "",
                device_id=did,
                collect_status=n.get("status", "") or "",
                collect_last=n.get("last", "") or "",
                collect_time=n.get("time", "") or "",
            )
            by_name[name] = ident
            if ident.ip:
                by_ip[ident.ip] = ident

        self._by_name, self._by_ip = by_name, by_ip
        self._loaded_at = time.time()

    # ── public surface ───────────────────────────────────────────────────
    def resolve(self, device: str) -> Optional[DeviceIdentity]:
        """By name first, then by IP. None if unknown — callers must handle a
        miss (a cold-open box Netlapse has never seen is exactly this case)."""
        self._ensure()
        return self._by_name.get(device) or self._by_ip.get(device)

    def liveness(self, device: str) -> tuple[bool, str]:
        """(ok, detail) from Netlapse's LAST collection of this box. The gate can
        read this BEFORE escalating to Tier 2 — 'last contact failed (shell_timeout)'
        is worth knowing before sending a model into a live-SSH timeout. Unknown
        device -> (False, 'unknown device')."""
        ident = self.resolve(device)
        if ident is None:
            return False, "unknown device"
        return ident.collect_ok, ident.collect_last or ident.collect_status

    def all_devices(self) -> list[DeviceIdentity]:
        self._ensure()
        return list(self._by_name.values())


# ──────────────────────────────────────────────────────────────────────────
# Self-test — proves the resolver against the REAL /nodes payload, no network.
#   python -m uf.host.identity     (or: python identity.py)
# ──────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    OK, BAD = "\u2713", "\u2717"

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"  {OK if cond else BAD} {name}" + (f"  — {detail}" if detail else ""))

    # The exact /nodes body from the live box, plus a plausible /api/v1/devices
    # for the id join (the spec's device list is opaque; we assume id+name+ip).
    NODES = [
        {"name": "eng-rtr-1", "full_name": "eng-rtr-1", "ip": "172.16.128.2",
         "model": "cisco_ios", "group": "eng", "status": "success",
         "time": "2026-06-30T00:57:30Z", "last": "success"},
        {"name": "eng-spine-1", "full_name": "eng-spine-1", "ip": "172.16.2.2",
         "model": "arista_eos", "group": "eng", "status": "success",
         "time": "2026-06-30T00:57:34Z", "last": "success"},
        {"name": "eng-spine-2", "full_name": "eng-spine-2", "ip": "172.16.2.6",
         "model": "arista_eos", "group": "eng", "status": "failure",
         "time": "2026-06-30T00:54:09Z", "last": "shell_timeout"},
        {"name": "usa-rtr-1", "full_name": "usa-rtr-1", "ip": "172.16.100.2",
         "model": "cisco_ios", "group": "usa", "status": "failure",
         "time": "2026-06-30T00:57:09Z", "last": "shell_timeout"},
        {"name": "wan-core-1", "full_name": "wan-core-1", "ip": "172.16.1.2",
         "model": "cisco_ios", "group": "wan", "status": "success",
         "time": "2026-06-30T00:57:25Z", "last": "success"},
    ]
    DEVICES = [
        {"id": 11, "name": "eng-rtr-1", "primary_ip4": "172.16.128.2"},
        {"id": 12, "name": "eng-spine-1", "primary_ip4": "172.16.2.2"},
        {"id": 13, "name": "eng-spine-2", "primary_ip4": "172.16.2.6"},
        {"id": 14, "name": "usa-rtr-1", "primary_ip4": "172.16.100.2"},
        {"id": 15, "name": "wan-core-1", "primary_ip4": "172.16.1.2"},
    ]

    def fake_fetch(url: str) -> Any:
        if url.endswith("/nodes"):
            return NODES
        if url.endswith("/api/v1/devices"):
            return DEVICES
        raise AssertionError(f"unexpected url {url}")

    r = IdentityResolver("http://0.0.0.0:8888", fetch_json=fake_fetch)

    print("resolve by name — the lingua franca")
    s1 = r.resolve("eng-spine-1")
    check("eng-spine-1 -> arista, ip + id joined",
          s1 and s1.vendor == "arista" and s1.vendor_mapped
          and s1.ip == "172.16.2.2" and s1.device_id == 12,
          f"{s1.vendor} {s1.ip} id={s1.device_id}")

    print("\nresolve by ip — Tier 1.5 cold-open hands an IP, not a name")
    s2 = r.resolve("172.16.2.2")
    check("172.16.2.2 resolves to the same record", s2 is not None and s2.name == "eng-spine-1")

    print("\nvendor vocab — TRANSLATED once; determinability is a separate question")
    s1b = r.resolve("eng-spine-1")
    check("arista_eos -> arista, mapped=True", s1b.vendor == "arista" and s1b.vendor_mapped)
    ios = r.resolve("eng-rtr-1")
    check("cisco_ios -> cisco_ios, mapped=True (translated, even if uf can't determine it)",
          ios.vendor == "cisco_ios" and ios.vendor_mapped,
          f"{ios.vendor} mapped={ios.vendor_mapped}")
    # Determinability — the thing the resolver does NOT claim — lives here, at the
    # 1.5 layer, via reading.capabilities(). arista has discriminators; cisco_ios
    # does not (yet). The resolver stays silent on this by design.
    # determinability is reading.capabilities()'s call, not the resolver's — the
    # resolver stays SILENT on it. Package-absolute import; no path surgery.
    from uf.core import reading

    check("determinability is reading.capabilities()'s call, not the resolver's",
          bool(reading.capabilities("arista")) and not reading.capabilities("cisco_ios"),
          f"arista={sorted(reading.capabilities('arista'))[:3]}…  cisco_ios=∅")

    print("\nliveness — Tier 1's last-collection verdict, for the gate to read")
    ok_live, why = r.liveness("eng-spine-1")
    check("eng-spine-1 last collection ok", ok_live and why == "success")
    bad_live, why2 = r.liveness("eng-spine-2")
    check("eng-spine-2 last collection FAILED (shell_timeout) — gate can pre-empt",
          (not bad_live) and why2 == "shell_timeout", why2)

    print("\nmiss — a cold-open box Netlapse never saw")
    check("unknown device -> None (caller must handle, never fabricated)",
          r.resolve("ghost-box-99") is None)
    check("liveness of unknown -> (False, 'unknown device')",
          r.liveness("ghost-box-99") == (False, "unknown device"))

    print("\nid join degrades safely")
    def fetch_no_devices(url: str) -> Any:
        if url.endswith("/nodes"):
            return NODES
        raise ConnectionError("devices endpoint down")
    r2 = IdentityResolver("http://0.0.0.0:8888", fetch_json=fetch_no_devices)
    s3 = r2.resolve("eng-spine-1")
    check("no /api/v1/devices -> still resolves, device_id=None (name-keyed fallback)",
          s3 is not None and s3.device_id is None and s3.vendor == "arista")
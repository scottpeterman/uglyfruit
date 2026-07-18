"""
uglyfruit / Tier 1.5 — the shell transport adapter.

Wraps the SCNG SSHClient (invoke-shell paramiko client) to the broker's
`Transport` protocol (session.py): authenticate / run_batch / close.

The point of this file is the *seam*. The SSHClient is a general-purpose
network-device shell — by design it will run any command you hand it, for any
of the three postures. That is correct: read-only is the BROKER's guarantee,
enforced one layer up by resolving consumer *keys* to commands, never the
client's. So the same client class backs broker, interactive, and gated; they
differ only in what wraps them and which credential they carry (Note 02 §6).

Two things this adapter owns that the client does not, and must not get wrong:

  1. The collector's error contract.  reading.py's discriminator reads
     `_error` to mean "failed to answer" -> UNREAD. The SSHClient returns raw
     text and never speaks that vocabulary, so the adapter reproduces it:
     parse ok -> dict; decode fail -> {"_raw":…, "_error":"json_decode_failed"};
     no output / exception -> {"_error": …}. Get this wrong and a read that
     merely failed to parse becomes a silent green frame — the exact C6 failure.

  2. Echo + prompt stripping.  Unlike Netmiko's send_command, this client's
     execute_command returns shell-DIRTY output: the echoed command line at the
     front and the prompt at the tail are inside the string. `| json` output
     must be sliced out of that before json.loads, or valid device state
     decodes as a spurious UNREAD.

Encapsulation is the discipline the read-only guarantee rests on: the SSHClient
is private (`_ssh`); this adapter exposes ONLY the protocol surface. Nothing
downstream of the broker is handed a path to `execute_command`.

Stdlib only (+ the duck-typed ssh client). Python 3.10+.
"""

from __future__ import annotations

import json
import time
import xml.etree.ElementTree as ET
from typing import Any, Protocol

import uf.core.reading  # noqa: F401  (used by the self-test to close the loop)


# The slice of SSHClient's surface this adapter actually depends on. Declared so
# the adapter can be tested against a fake with no paramiko and no socket.
class ShellClient(Protocol):
    def connect(self) -> None: ...
    def find_prompt(self) -> str: ...
    def set_expect_prompt(self, prompt: str) -> None: ...
    def disable_pagination(self) -> None: ...
    def execute_command(self, command: str, timeout: float | None = None) -> str: ...
    def disconnect(self) -> None: ...


def _match_brace(raw: str, start: int) -> int:
    """Index of the '}' that closes the '{' at `start`, or -1 if it never closes
    (truncated read). String-aware: braces inside JSON string values don't count."""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(raw)):
        c = raw[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _extract_json(raw: str) -> Any:
    """Pull the JSON object out of shell-dirty output.

    execute_command returns `<echoed cmd>\\n<json>\\n<prompt>`, and the box may
    prepend a banner (a `/var/core` filesystem warning, an MOTD) or append a note.
    A naive first-'{' to last-'}' slice corrupts on a stray brace in that junk, or
    on a truncated read where the last '}' is an inner one. Instead: scan from the
    first '{' to its MATCHING close (string-aware), and if that candidate isn't
    valid JSON, try the next '{' — so a leading brace-bearing banner can't defeat
    it. No complete object anywhere -> the caller records _error (UNREAD), never
    ABSENT; a balanced-but-invalid object -> json_decode_failed with _raw for
    inspection.
    """
    start = raw.find("{")
    if start == -1:
        raise ValueError("no json object in output")
    saw_balanced = False
    i = start
    while i != -1:
        end = _match_brace(raw, i)
        if end != -1:
            saw_balanced = True
            try:
                return json.loads(raw[i:end + 1])
            except json.JSONDecodeError:
                pass                       # balanced but not valid here — try the next '{'
        i = raw.find("{", i + 1)
    if saw_balanced:                       # something balanced but none parsed
        raise json.JSONDecodeError("no valid json object among candidates", raw, 0)
    raise ValueError("incomplete json object (truncated read?)")


# ── Junos `| display xml` -> the same array-wrapped dict `| display json` yields.
# Ported from the legacy Juniper HUD collector (xml_to_juniper_dict +
# parse_xml_output): the PRESENT-path parse that was correct and is reused. Its
# _error contract is honoured here — a parse failure returns an _error dict, which
# the discriminator reads as a READ failure (UNREAD), never absence (the C6 line
# the HUD renderer erased). Vendor-agnostic in placement: the transport owns the
# parse lane (json | xml | text); the discriminator judges the parsed value.
def _xml_to_juniper_dict(element: "ET.Element") -> Any:
    """Convert an ElementTree element to Junos-JSON shape: leaf ->
    [{"data": text, "attributes": {...}}], branch -> [{child_tag: [...]}],
    repeated siblings appended to one list, namespace URI stripped from tags."""
    children = list(element)
    if not children:
        entry: dict[str, Any] = {"data": (element.text or "").strip()}
        if element.attrib:
            attrs = {}
            for k, v in element.attrib.items():
                if k.startswith("{"):
                    attrs[f"junos:{k[k.index('}') + 1:]}"] = v
                else:
                    attrs[k] = v
            entry["attributes"] = attrs
        return [entry]
    result: dict[str, Any] = {}
    for child in children:
        tag = child.tag
        if tag.startswith("{"):
            tag = tag[tag.index("}") + 1:]
        converted = _xml_to_juniper_dict(child)
        if tag in result and isinstance(result[tag], list):
            result[tag].extend(converted)
        else:
            result[tag] = converted
    return [result]


def _extract_juniper_xml(raw: str) -> dict:
    """Pull the XML out of shell-dirty output and parse to the Junos dict.

    Mirrors _extract_json's role for `| display xml`: slice the echoed command
    and prompt off, strip xmlns declarations (so tags are prefix-free), parse,
    and unwrap the <rpc-reply> envelope to its inner content. Raises on no-XML or
    a parse error so the caller records _error -> UNREAD."""
    xml_start = raw.find("<?xml")
    if xml_start < 0:
        xml_start = raw.find("<rpc-reply")
    if xml_start < 0:
        xml_start = raw.find("<")
    if xml_start < 0:
        raise ValueError("no xml in output")
    raw = raw[xml_start:]
    for end_tag in ("</rpc-reply>", "</output>"):
        i = raw.rfind(end_tag)
        if i >= 0:
            raw = raw[:i + len(end_tag)]
            break
    # Parse WITH namespaces intact. Junos DECLARES its prefixes (xmlns:junos on
    # rpc-reply, a default xmlns on bgp-information), so ElementTree binds them and
    # _xml_to_juniper_dict strips the {uri} off tags AND attribute keys. Stripping
    # the xmlns declarations first (as the legacy collector did) would UNBIND the
    # junos: attributes that pepper real output (junos:style / junos:format /
    # junos:seconds) and raise 'unbound prefix' — so we do NOT strip. The xnm:error
    # no-bgp reply parses fine here too ({uri}error -> error in the unwrap below).
    root = ET.fromstring(raw)                            # ParseError -> caller -> _error
    tag = root.tag
    if tag.startswith("{"):
        tag = tag[tag.index("}") + 1:]
    if tag == "rpc-reply":                               # unwrap the envelope
        result: dict[str, Any] = {}
        for child in root:
            ctag = child.tag
            if ctag.startswith("{"):
                ctag = ctag[ctag.index("}") + 1:]
            result[ctag] = _xml_to_juniper_dict(child)
        return result
    return {tag: _xml_to_juniper_dict(root)}


class ShellTransport:
    """Adapts an SSHClient to the broker's Transport protocol.

    Constructed with a connected-capable ShellClient and the vendor name (so it
    knows the parse strategy). One authenticate() does the client's required
    bring-up dance; run_batch() runs each resolved command and parses it back
    into the *same shape the collector emitted*, so reading.py is unchanged.
    """

    def __init__(self, ssh: ShellClient, vendor: str):
        self._ssh = ssh                 # PRIVATE — never exposed past this object
        self.vendor = vendor
        self._up = False
        self._reads_done = 0            # gates the between-reads resync (skip first)

    # ── Transport protocol ──────────────────────────────────────────────
    def authenticate(self) -> None:
        """connect -> detect prompt -> arm expect -> kill pagination. Once.

        This is the expensive bring-up (sleeps, prompt probing). Paying it once
        per held session and amortizing it across every consumer and every poll
        is the entire argument for the broker holding the session — 'attach,
        not reconnect.'
        """
        if self._up:
            return
        self._ssh.connect()
        prompt = self._ssh.find_prompt()
        self._ssh.set_expect_prompt(prompt)
        self._ssh.disable_pagination()
        self._up = True

    def run_batch(self, commands: dict[str, Any]) -> dict[str, Any]:
        """Run each resolved command; return {key: collector-shaped value}.

        A spec is either a command string (single read) or a dict of named
        sub-commands (a multi-read capability — e.g. environment = power +
        temperature, each its own `| json`). Multi-read returns {sub: value},
        and the per-capability discriminator combines them. This is what lets a
        capability drop a fragile text parser in favor of several structured
        reads: the shape arrives pre-parsed per sub-read, nothing to regex.

        Logical batch, not a wire batch: one held shell, commands serial. The
        union upstream bounds how many; this just runs what it's handed.
        """
        out: dict[str, Any] = {}
        for key, spec in commands.items():
            if isinstance(spec, dict):
                out[key] = {sub: self._read_one(cmd) for sub, cmd in spec.items()}
            else:
                out[key] = self._read_one(spec)
        return out

    def close(self) -> None:
        try:
            self._ssh.disconnect()
        finally:
            self._up = False

    # ── parse: reproduce the collector's error contract ─────────────────
    def _read_one(self, cmd: str) -> Any:
        # Re-anchor on a clean prompt BETWEEN reads (skip the first — authenticate
        # already left the shell clean). Without this, a prior command's trailing
        # bytes can satisfy the next read's `prompt in output` check early, so a
        # large/slow read (environment temperature/cooling) returns truncated ->
        # no_json_found. This uses the bounded resync(), NOT find_prompt(): the
        # latter sleeps 3s unconditionally and re-detects the prompt, so calling
        # it per-read serializes ~3-6s × N reads (the hang) and can reset the
        # prompt to '#'. resync() is hard-bounded and never resets state.
        if self._reads_done:
            self._resync()
        self._reads_done += 1
        try:
            raw = self._ssh.execute_command(cmd)
        except Exception as e:                       # transport-level failure
            return {"_error": f"exec_failed: {e}"}

        if not raw or not raw.strip():               # answered with nothing readable
            return {"_error": "empty_output"}

        # XML commands (juniper `| display xml`) -> parse to the Junos array-
        # wrapped dict, so the discriminator judges structure, never raw angle
        # brackets. Parse failure -> _error (UNREAD), mirroring the json lane.
        if "display xml" in cmd:
            try:
                return _extract_juniper_xml(raw)
            except ET.ParseError as e:
                return {"_raw": raw[:500], "_error": f"xml_parse_failed: {e}"}
            except ValueError as e:
                return {"_raw": raw[:500], "_error": f"no_xml_found: {e}"}

        # JSON commands (arista `| json`, linux/frr `... json`) -> parse.
        if cmd.rstrip().endswith("json"):
            try:
                return _extract_json(raw)
            except json.JSONDecodeError:             # subclass of ValueError —
                return {"_raw": raw, "_error": "json_decode_failed"}
            except ValueError:                       # …so this MUST come second
                return {"_raw": raw, "_error": "no_json_found"}

        # Text commands -> raw under a stable key. NOTE: environment used to be
        # the one text read (regex-parsed by parsers.py); it moved to JSON
        # sub-reads (show environment power/temperature | json) so the parser is
        # retired and this branch is currently unused. Kept for any future read a
        # vendor exposes only as text.
        return {"_raw": raw}

    def _resync(self) -> None:
        """Best-effort, bounded prompt re-anchor between reads. Calls the client's
        lightweight resync() (NOT find_prompt) so it can't sleep for seconds or
        reset the prompt. Guarded so a client/fake without resync simply skips,
        and a failed resync never crashes the read — a single garbled command
        should degrade to UNREAD on its own, not take the whole poll down.
        """
        if not self._up:
            return
        fn = getattr(self._ssh, "resync", None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass


# ──────────────────────────────────────────────────────────────────────────
# Self-test — proves the adapter without paramiko or a socket: a fake client
# returns shell-DIRTY output, and we watch it become a Reading. python transport.py
# ──────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    OK, BAD = "\u2713", "\u2717"

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"  {OK if cond else BAD} {name}" + (f"  — {detail}" if detail else ""))

    PROMPT = "eng-spine1#"

    # A fake SSHClient with the same surface, returning output exactly as dirty
    # as the real client does: echoed command line in front, prompt at the tail.
    class FakeSSHClient:
        def __init__(self, responses: dict[str, str]):
            self.responses = responses
            self.connects = 0
            self.prompt_calls = 0
            self.pagination_calls = 0
            self.resync_calls = 0

        def connect(self): self.connects += 1
        def find_prompt(self): self.prompt_calls += 1; return PROMPT
        def set_expect_prompt(self, p): pass
        def disable_pagination(self): self.pagination_calls += 1
        def resync(self): self.resync_calls += 1   # bounded; here just counted

        def execute_command(self, command, timeout=None):
            body = self.responses.get(command, "% Invalid input")
            return f"{command}\n{body}\n{PROMPT}"   # dirty: echo + payload + prompt

        def disconnect(self): pass

    ospf_present = json.dumps({"vrfs": {"default": {"instList": {"1": {
        "ospfNeighborEntries": [
            {"routerId": "10.0.0.1", "adjacencyState": "full"},
            {"routerId": "10.0.0.2", "adjacencyState": "2-Way"},
        ]}}}}})
    ospf_malformed = '{"vrfs": {"default": {"instList": }}}'   # braced but invalid

    print("authenticate — the bring-up dance, once")
    fake = FakeSSHClient({
        "show ip ospf neighbor | json": ospf_present,
        "show ip bgp summary | json": '{"vrfs":{"default":{"peers":{}}}}',
    })
    tx = ShellTransport(fake, vendor="arista")
    tx.authenticate(); tx.authenticate()             # second call is a no-op
    check("connect/find_prompt/disable_pagination each ran exactly once",
          fake.connects == 1 and fake.prompt_calls == 1 and fake.pagination_calls == 1)

    print("\nrun_batch — dirty output parsed back to collector shape")
    res = tx.run_batch({
        "ospf": "show ip ospf neighbor | json",
        "bgp": "show ip bgp summary | json",
    })
    check("echo+prompt stripped, ospf parsed to a dict",
          isinstance(res["ospf"], dict) and "_error" not in res["ospf"])
    r = reading.determine("arista", "ospf", res["ospf"])
    check("parsed ospf -> Reading PRESENT with the down frame",
          str(r.state) == "PRESENT" and r.frames and r.frames[0].value == 1,
          f"{r.state}, {r.frames[0].label}={r.frames[0].value} [{r.frames[0].status}]")

    print("\nerror contract — a failed parse must become UNREAD, never green")
    bad = ShellTransport(FakeSSHClient(
        {"show ip ospf neighbor | json": ospf_malformed}), vendor="arista")
    bad.authenticate()
    v = bad.run_batch({"ospf": "show ip ospf neighbor | json"})["ospf"]
    check("malformed json -> {_error: json_decode_failed}",
          v.get("_error") == "json_decode_failed", v.get("_error"))
    check("…and reading.py reads that as UNREAD (the C6 trap, avoided)",
          str(reading.determine("arista", "ospf", v).state) == "UNREAD")

    missing = ShellTransport(FakeSSHClient({}), vendor="arista")
    missing.authenticate()
    v2 = missing.run_batch({"ospf": "show ip ospf neighbor | json"})["ospf"]
    check("error banner (no json) -> UNREAD, not ABSENT",
          str(reading.determine("arista", "ospf", v2).state) == "UNREAD", v2.get("_error"))

    print("\nmulti-read capability — a dict spec runs each sub and assembles {sub: value}")
    multi_fake = FakeSSHClient({
        "show environment power | json": '{"powerSupplies":{"1":{"state":"ok"}}}',
        "show environment temperature | json": '{"systemStatus":"temperatureOk","tempSensors":[]}',
        "show environment cooling | json": '{"systemStatus":"coolingOk","fanTraySlots":[]}',
    })
    multi = ShellTransport(multi_fake, vendor="arista")
    multi.authenticate()
    env = multi.run_batch({"environment": {
        "power": "show environment power | json",
        "temperature": "show environment temperature | json",
        "cooling": "show environment cooling | json"}})["environment"]
    check("environment ran as sub-reads, combined to {power, temperature, cooling}",
          set(env) == {"power", "temperature", "cooling"} and "_error" not in env["power"],
          f"subs={sorted(env)}")
    check("bounded resync ran BETWEEN reads, not before the first (3 reads -> 2 resyncs)",
          multi_fake.resync_calls == 2,
          f"{multi_fake.resync_calls} resyncs for 3 sub-reads (first skipped)")
    er = reading.determine("arista", "environment", env)
    check("combined sub-reads -> Reading PRESENT (one read, one verdict)",
          str(er.state) == "PRESENT", f"{er.state}: {er.reason}")

    print("\nencapsulation — the read-only guarantee's footing")
    check("adapter exposes only authenticate/run_batch/close (no execute path)",
          not any(hasattr(ShellTransport, m)
                  for m in ("execute_command", "send", "run", "exec")))
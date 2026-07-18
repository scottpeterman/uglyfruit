"""
uf/cockpit/formatting.py — deterministic feed -> HTML tables.

The renderer of record for Tier-1.5 health in the investigation transcript. It
turns the coordinator's cached feed into formatted tables IN-PROCESS, from the
real structured payload — so the numbers are the gear's, not a language model's
transcription of them. This is the presentation-layer expression of the whole
never-fake-green discipline: the model narrates and reasons; the cockpit renders
facts. A model that retypes sensor values can truncate, reorder, or comma-mangle
them (and did); this path cannot, because it never leaves the source dict.

Three-state honesty is carried into the render: PRESENT gets its table, ABSENT and
UNREAD are shown with their reason and NOT dropped — a blank row is never a green.

Generic by construction (no per-cap/per-vendor code):
  * every Reading's `frames` are already normalized (label/value/ceiling/status),
    so one uniform frames table serves every capability and vendor.
  * a PRESENT payload that is a list-of-dicts renders as a columned table; a dict
    of scalars renders as a key/value table; anything deeper falls back to compact
    JSON in a <pre>. No shape is assumed, nothing is silently omitted.

Qt note: QTextBrowser renders a subset of HTML4 rich text. We stay inside it —
<table border cellpadding>, <th>/<td>, inline color styles — no CSS grid, no JS.

Stdlib only. Self-test: python -m uf.cockpit.formatting
"""

from __future__ import annotations

import html
import json
from typing import Any, Optional


_STATE_COLOR = {"PRESENT": "#2ee66a", "ABSENT": "#166b34", "UNREAD": "#e0b64a"}
_MONO = "font-family:'IBM Plex Mono',monospace"


def _esc(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    head = "".join(
        f'<th align="left" style="color:#166b34;padding:2px 8px">{_esc(h)}</th>'
        for h in headers)
    body = []
    for row in rows:
        tds = "".join(
            f'<td style="color:#2ee66a;padding:2px 8px">{_esc(c)}</td>' for c in row)
        body.append(f"<tr>{tds}</tr>")
    return (f'<table border="1" cellspacing="0" cellpadding="2" '
            f'style="border-color:#0f3d20;{_MONO}">'
            f"<tr>{head}</tr>{''.join(body)}</table>")


def _frames_table(frames: list[dict]) -> str:
    rows = []
    for f in frames:
        ceil = f.get("ceiling")
        val = f.get("value")
        vc = f"{val}/{ceil}" if ceil is not None else val
        rows.append([f.get("label"), vc, f.get("status")])
    return _table(["signal", "value", "status"], rows)


def _payload_table(payload: Any) -> str:
    # list of dicts -> columned table (union of keys, in first-seen order)
    if isinstance(payload, list) and payload and all(isinstance(x, dict) for x in payload):
        cols: list[str] = []
        for row in payload:
            for k in row:
                if k not in cols:
                    cols.append(k)
        rows = [[_scalar(row.get(c)) for c in cols] for row in payload]
        return _table(cols, rows)
    # flat dict of scalars -> key/value table
    if isinstance(payload, dict) and all(not isinstance(v, (dict, list)) for v in payload.values()):
        return _table(["field", "value"], [[k, _scalar(v)] for k, v in payload.items()])
    # anything deeper -> compact JSON, never truncated silently
    try:
        pj = json.dumps(payload, separators=(",", ":"))
    except (TypeError, ValueError):
        pj = str(payload)
    return (f'<pre style="color:#2ee66a;{_MONO};white-space:pre-wrap;margin:2px 0">'
            f"{_esc(pj)}</pre>")


def _scalar(v: Any) -> str:
    # keep numbers as the gear reported them — no thousands separators, no rounding.
    if isinstance(v, (dict, list)):
        try:
            return json.dumps(v, separators=(",", ":"))
        except (TypeError, ValueError):
            return str(v)
    return "" if v is None else str(v)


def feed_to_html(feed: Optional[dict], device: str, vendor: str) -> str:
    """The full render: a heading, then one block per capability in the feed. Every
    cap is shown with its state; PRESENT caps get frames and/or a payload table;
    ABSENT/UNREAD show their reason so a blank is never mistaken for healthy."""
    if not feed:
        return ('<i style="color:#166b34">no live health yet — '
                'waiting for the first Tier 1.5 poll</i>')
    parts = [f'<div style="color:#2ee66a;{_MONO}"><b>LIVE HEALTH</b> · '
             f'{_esc(device)} ({_esc(vendor)}) · Tier 1.5</div>']
    for cap in sorted(feed):
        r = feed[cap] or {}
        state = r.get("state", "?")
        color = _STATE_COLOR.get(state, "#888")
        age = r.get("age_s")
        head = (f'<div style="{_MONO};margin-top:6px">'
                f'<b style="color:{color}">{_esc(cap)} · {_esc(state)}</b>'
                + (f' <span style="color:#166b34">age {_esc(age)}s</span>'
                   if age is not None else ""))
        if state != "PRESENT" and r.get("reason"):
            head += f' <span style="color:#888">— {_esc(r.get("reason"))}</span>'
        head += "</div>"
        parts.append(head)
        frames = r.get("frames") or []
        if frames:
            parts.append(_frames_table(frames))
        if state == "PRESENT" and r.get("payload") is not None:
            parts.append(_payload_table(r["payload"]))
    return "".join(parts)


# ──────────────────────────────────────────────────────────────────────────────
# Self-test — the render is correct and LOSSLESS on real payload shapes. No Qt,
# no network. Proves: real values appear verbatim (no truncation, no comma-mangling),
# list-of-dicts -> columned table, dict -> key/value, frames uniform, and ABSENT/
# UNREAD are shown (never dropped to a blank green).
#   python -m uf.cockpit.formatting
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    OK, BAD = "\u2713", "\u2717"
    fails = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        global fails
        if not cond:
            fails += 1
        print(f"  {OK if cond else BAD} {name}" + (f"  — {detail}" if detail else ""))

    feed = {
        "environment": {"state": "PRESENT", "age_s": 3,
            "frames": [{"label": "environment fault", "value": 0, "ceiling": None, "status": "OK"}],
            "payload": {"peakTemp": 49, "ambient": 19, "cooling": "coolingOk"}},
        "bgp": {"state": "PRESENT", "age_s": 3, "frames": [],
            "payload": [
                {"peer": "192.0.2.192", "asn": 17012, "state": "Established", "prefixes": 25, "uptime_s": 177087226},
                {"peer": "192.0.2.188", "asn": 15830, "state": "Established", "prefixes": 1516, "uptime_s": 176552469}]},
        "lldp": {"state": "ABSENT", "age_s": 3, "reason": "lldp not configured", "frames": [], "payload": None},
        "transceivers": {"state": "UNREAD", "age_s": 45, "reason": "timed out after 45s", "frames": [], "payload": None},
    }
    out = feed_to_html(feed, "eng-peer-1", "arista")

    check("big integer is verbatim, NOT comma-formatted", "177087226" in out and "177,087,226" not in out)
    check("all bgp rows rendered (nothing 'omitted for brevity')",
          "192.0.2.192" in out and "192.0.2.188" in out and "brevity" not in out.lower())
    check("bgp columns present (list-of-dicts -> table)",
          all(c in out for c in ("peer", "asn", "state", "prefixes", "uptime_s")))
    check("environment payload rendered as key/value", "peakTemp" in out and "coolingOk" in out)
    check("frames table uniform (signal/value/status)",
          "environment fault" in out and "signal" in out)
    check("ABSENT shown with reason (not dropped)", "lldp · ABSENT" in out and "not configured" in out)
    check("UNREAD shown with reason (never a blank green)", "transceivers · UNREAD" in out and "timed out" in out)
    check("empty feed is an honest notice, not a blank",
          "waiting for the first" in feed_to_html({}, "d", "v"))
    check("output is HTML tables Qt can render", out.count("<table") == 3, f"tables={out.count('<table')}")

    print()
    print(f"  {'all formatting assertions held' if not fails else str(fails) + ' FAILED'}")
    raise SystemExit(1 if fails else 0)
"""
uf/cockpit/panes/investigation.py — the LLM investigation pane (GATED).

The bottom-right slot in a device tab: a chat surface where a local Ollama model
investigates the box. It keeps its own Ollama tool loop (proven), but no longer
holds an MCP session of its own — every tool call now flows through the tab's
SessionCoordinator's single chokepoint:

    model picks a tool  ->  coord.advertised() is what it may pick from
    model calls a tool  ->  coord.step(name, **args)  (the gate)
        Tier 1 (netlapse history)  : granted at the floor, runs in-process
        Tier 1.5 (live health)     : NOT a tool — ambient context, read below the boundary
        Tier 2 (mcpssh live cmds)  : WITHHELD until the operator approves an escalation

So the transcript surface is unchanged from the ungated spike; only the dispatcher
moved (session.call_tool -> coord.step). The negative the whole architecture points
at now holds here: the model cannot reach a live show command until a human says yes.

  Threading (unchanged discipline, one new hop):
  * loop B — this worker's asyncio loop — runs the Ollama HTTP calls.
  * coord.step is SYNC; it is pushed to the default executor via run_in_executor so
    loop B stays free while the gate runs on a pool thread. A granted Tier-2 call
    then bounces from that pool thread onto the bridge's loop C. Three threads, no
    same-loop await, no deadlock; the GUI thread only renders.
  * the approval modal is the one place the pool thread must reach the GUI thread:
    request_approval() (pool thread) emits a signal and BLOCKS on an Event; the GUI
    thread pops the dialog and sets it. This is where the human owns the grant.

  VPN/TLS: this pane no longer has a TLS surface — the mcpssh client (its verify-off
  switch) moved to the coordinator's bridge, wired in app.py. Ollama is plain HTTP.

  Deps beyond Qt: `httpx` for the Ollama calls. The `mcp` client dep moved to the
  bridge; the pane no longer imports it.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor, QTextCursor
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTextBrowser,
    QVBoxLayout, QWidget)

from uf.host.routing import REQUEST_ESCALATION


# ──────────────────────────────────────────────────────────────────────────
# Config — sourced by the app. The mcpssh/verify_tls fields are read by the app
# and handed to coord.wire() (they configure the coordinator's bridge, not this
# pane); the ollama_*/model/system_prompt fields are the pane's. One object, two
# consumers, so app.py keeps a single INVEST_CFG.
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class InvestigationConfig:
    mcpssh_url: str = "http://127.0.0.1:8000/mcp"   # -> coord.wire() (the bridge)
    mcpssh_token: Optional[str] = None              # -> coord.wire()
    ollama_url: str = "http://127.0.0.1:11434"      # your 0.0.0.0-bound Ollama
    model: str = "qwen2.5:14b"                      # a TOOL-CAPABLE model
    verify_tls: bool = True                         # -> coord.wire() (mcpssh TLS, VPN switch)
    max_tool_iters: int = 8                         # runaway guard (echoes the gate cap)
    system_prompt: str = field(default=(
        "You are a network investigation assistant embedded in a device cockpit. "
        "The device under investigation is '{device}' (vendor: {vendor}). You work a "
        "gradient of tiers, cheapest first:\n"
        "- Tier 1.5 LIVE HEALTH arrives every turn as an ambient data block in your "
        "context. It is read-only and already retrieved. Answer health and "
        "environmental questions DIRECTLY FROM ITS ACTUAL VALUES — quote the concrete "
        "numbers and states you see in it (an actual temperature, wattage, peer state). "
        "Do NOT reply with placeholders or bare field names, and do NOT escalate to "
        "Tier 2 for anything already shown there. Respect its three states — UNREAD "
        "means no answer, never assume healthy. When asked to FORMAT or TABULATE "
        "health, do NOT retype every row or reformat numbers — give a concise summary "
        "of the notable readings and tell the operator the cockpit's TABLES button "
        "renders the full, exact tables from source. Never write '[value]' or "
        "'omitted for brevity'.\n"
        "- Tier 1 TOOLS (historical_snapshot / historical_diff / historical_inventory) "
        "answer 'what did it look like before / what changed', with no device contact. "
        "Their schema lists the exact capabilities Netlapse captures (including bgp, "
        "ospf, lldp, interfaces, routes, arp, mac, config). To check any of these, CALL "
        "the tool with that capability and read `available` in the result — do NOT "
        "reason about whether Tier 1 holds it, and NEVER conclude it is unavailable "
        "without calling. Prefer calling a tool over speculating about what it returns.\n"
        "- Tier 2 (request_escalation, then send_show_command) is LIVE arbitrary "
        "commands, and only for something neither ambient health NOR a Tier 1 tool "
        "call can answer — a specific show command. It needs HUMAN approval; do not "
        "expect that tool to exist until an escalation is approved. Live show commands "
        "pass a server-side allow-list; if one is refused, report it, do not retry "
        "blind variants. EXCEPTION: if the operator EXPLICITLY asks for a live command, "
        "real-time verification from the device itself, or 'Tier 2', HONOR it — call "
        "request_escalation even when similar data is already in ambient. The operator "
        "may want ground truth from the box, not the cached read; that is their call to "
        "make, not yours to refuse.\n"
        "Note which tier each fact came from (e.g. 'from Tier 1.5 ambient').\n"
        "For a full, exact table of live health, tell the operator to click the TABLES "
        "button — it renders every value from source. You may still give a brief HTML "
        "or text summary of the notable readings when asked; never claim you cannot "
        "format, and never emit '[value]' placeholders or 'omitted for brevity'."))


# ──────────────────────────────────────────────────────────────────────────
_QSS = """
QTextBrowser { background:#050805; color:#2ee66a; border:1px solid #0f3d20;
               font-family:'IBM Plex Mono',monospace; font-size:11px; padding:6px; }
QLineEdit { background:#080e08; color:#2ee66a; border:1px solid #0f3d20; padding:5px;
            font-family:'IBM Plex Mono',monospace; }
QLineEdit:disabled { color:#166b34; }
QPushButton { background:#0a130c; color:#2ee66a; border:1px solid #0f3d20; padding:5px 12px; }
QPushButton:disabled { color:#166b34; }
QLabel#status { color:#166b34; font-size:10px; letter-spacing:.12em; }
"""

_ROLE_COLOR = {
    "you":   "#5fd0ff",
    "ai":    "#2ee66a",
    "tool":  "#c8a44a",
    "sys":   "#166b34",
    "gate":  "#e0b64a",
    "error": "#ff3b3b",
}


class InvestigationPane(QWidget):
    """The bottom-right investigation slot, gated through the tab coordinator.

        pane = InvestigationPane("eng-edge-1", "juniper", coord, cfg)

    The pane owns render AND the approval modal; the worker owns Ollama I/O and
    dispatches tools through coord.step(). A None coord (demo tabs) makes the pane
    inert with a notice, rather than pretending to investigate."""

    # pool-thread -> GUI-thread hop for the approval modal.
    _approval_needed = pyqtSignal(object)     # EscalationRequest

    def __init__(self, device: str, vendor: str, coord: Optional[Any],
                 config: InvestigationConfig, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setStyleSheet(_QSS)
        self._device = device
        self._coord = coord

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        head = QHBoxLayout()
        title = QLabel("INVESTIGATION")
        title.setStyleSheet("color:#2ee66a;font-size:11px;letter-spacing:.14em;")
        self._tables_btn = QPushButton("TABLES")
        self._tables_btn.setToolTip(
            "Render current Tier 1.5 health as tables — from source, no model")
        self._tables_btn.clicked.connect(self._render_health_tables)
        self._tables_btn.setEnabled(coord is not None)
        self._status = QLabel("starting…" if coord is not None else "demo — no gate")
        self._status.setObjectName("status")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self._tables_btn)
        head.addWidget(self._status)
        lay.addLayout(head)

        self._log = QTextBrowser()
        self._log.setOpenExternalLinks(False)
        lay.addWidget(self._log, 1)

        row = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setPlaceholderText(
            f"ask about {device} — Tier 1 live, escalate for Tier 2")
        self._input.setEnabled(False)
        self._input.returnPressed.connect(self._submit)
        self._send = QPushButton("SEND")
        self._send.setEnabled(False)
        self._send.clicked.connect(self._submit)
        row.addWidget(self._input, 1)
        row.addWidget(self._send)
        lay.addLayout(row)

        # approval-modal plumbing (GUI thread pops the dialog; pool thread blocks)
        self._approval_holder: dict = {}
        self._approval_evt = threading.Event()
        self._approval_needed.connect(self._show_approval_dialog)

        self._worker: Optional[_InvestigationWorker] = None
        if coord is None:
            self._append("sys", "no coordinator wired (demo tab) — investigation inert")
            return

        # Built here, STARTED by start() — the app calls start() AFTER coord.wire()
        # so the worker's first coord.advertised() can't race an unbuilt gate.
        self._worker = _InvestigationWorker(device, vendor, coord, config)
        self._worker.appended.connect(self._append)
        self._worker.ready.connect(self._on_ready)
        self._worker.busy.connect(self._on_busy)

    def start(self) -> None:
        """Begin the model loop. Call after the coordinator is wired. Idempotent."""
        if self._worker is not None and not self._worker.isRunning():
            self._worker.start()

    # ── the approver the coordinator calls (via coord.wire(approver_ui=...)) ────
    def request_approval(self, req: Any) -> tuple[bool, str]:
        """Called on the GATE's thread (a pool thread inside coord.step). Marshals
        to the GUI thread to pop the modal, then BLOCKS here until the operator
        answers. Returns (approved, note) straight back into GateSession._escalate."""
        self._approval_evt.clear()
        self._approval_holder = {}
        self._approval_needed.emit(req)               # queued -> GUI thread
        self._approval_evt.wait()                     # block the pool thread for the human
        return (self._approval_holder.get("approved", False),
                self._approval_holder.get("note", ""))

    def _show_approval_dialog(self, req: Any) -> None:
        """GUI thread. The human owns the grant — this is the trust boundary made
        a dialog. Default is No (fail-safe if dismissed)."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Tier escalation requested")
        box.setText(f"The assistant requests Tier {req.to_level} ({req.to_name}).")
        box.setInformativeText(
            f"Reason: {req.reason or '(none given)'}\n\n"
            f"Approve LIVE read-only command access to {self._device}?")
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        approved = box.exec() == QMessageBox.StandardButton.Yes
        self._approval_holder = {
            "approved": approved,
            "note": "" if approved else "denied by operator"}
        self._append("gate", f"escalation {'APPROVED' if approved else 'DENIED'} by operator")
        self._approval_evt.set()                      # unblock the pool thread

    # ── render seams ─────────────────────────────────────────────────────────
    def _render_health_tables(self) -> None:
        """TABLES button: render the current Tier-1.5 feed as HTML tables from
        source. The model is NOT involved — correct numbers, no truncation. This is
        cockpit-generated HTML, so it is rendered rich (unlike model text, which is
        always escaped)."""
        if self._coord is None:
            return
        try:
            table_html = self._coord.ambient_html()
        except Exception as e:
            self._append("error", f"table render failed: {type(e).__name__}: {e}")
            return
        self._append("sys", "live health — rendered from source (Tier 1.5):")
        self._append_html(table_html)

    def _append_html(self, raw_html: str) -> None:
        """Append cockpit-generated HTML, rendered rich. ONLY for content the cockpit
        produced (deterministic tables) — never model output, which stays escaped in
        _append, so a local model cannot inject markup into the transcript."""
        self._log.append(raw_html)
        self._log.moveCursor(QTextCursor.MoveOperation.End)

    def _append(self, role: str, text: str) -> None:
        color = _ROLE_COLOR.get(role, "#2ee66a")
        tag = {"you": "you", "ai": "ai", "tool": "⚙", "sys": "·",
               "gate": "⛿", "error": "!"}.get(role, role)
        safe = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    .replace("\n", "<br>"))
        self._log.append(
            f'<span style="color:{color}">[{tag}]</span> '
            f'<span style="color:{color}">{safe}</span>')
        self._log.moveCursor(QTextCursor.MoveOperation.End)

    def _on_ready(self, tool_names: list) -> None:
        self._status.setText(f"{len(tool_names)} floor tools · gated")
        self._input.setEnabled(True)
        self._send.setEnabled(True)
        self._input.setFocus()

    def _on_busy(self, busy: bool) -> None:
        self._input.setEnabled(not busy)
        self._send.setEnabled(not busy)
        if busy:
            self._status.setText("investigating…")

    def _submit(self) -> None:
        text = self._input.text().strip()
        if not text or self._coord is None:
            return
        self._input.clear()
        self._append("you", text)
        self._worker.submit(text)

    def shutdown(self) -> None:
        """Tab closing — tear the worker's loop down. (The coordinator's close()
        tears down the mcpssh bridge; the pane no longer owns an MCP session.)"""
        if self._coord is not None:
            self._worker.stop()


# ──────────────────────────────────────────────────────────────────────────
# The worker — owns one asyncio loop (loop B) for Ollama I/O. Tools are the
# coordinator's; each call is dispatched through coord.step on the executor.
# ──────────────────────────────────────────────────────────────────────────
class _InvestigationWorker(QThread):
    appended = pyqtSignal(str, str)          # (role, text)
    ready = pyqtSignal(list)                  # floor tool names, once up
    busy = pyqtSignal(bool)                   # a turn is in flight

    def __init__(self, device: str, vendor: str, coord: Any,
                 cfg: InvestigationConfig, parent=None):
        super().__init__(parent)
        self._device = device
        self._vendor = vendor
        self._coord = coord
        self._cfg = cfg
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue] = None
        self._ready_evt = threading.Event()
        self._stop = False
        self._system = ""                       # base system prompt (set in _serve)
        self._history: list[dict] = []          # durable conversation (no system, no ambient)

    # ── GUI-thread entry points ───────────────────────────────────────────────
    def submit(self, text: str) -> None:
        if not self._ready_evt.wait(timeout=2.0) or self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._queue.put_nowait, text)

    def stop(self) -> None:
        self._stop = True
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
        self.wait(6000)

    # ── the loop ──────────────────────────────────────────────────────────────
    def run(self) -> None:
        try:
            asyncio.run(self._serve())
        except Exception as e:
            self.appended.emit("error", f"investigation loop died: {type(e).__name__}: {e}")

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._ready_evt.set()
        self._system = self._cfg.system_prompt.format(
            device=self._device, vendor=self._vendor)
        self._history = []
        try:
            names = [s.name for s in self._coord.advertised()]
        except Exception as e:      # gate not wired -> say so, don't pretend
            self.appended.emit("error", f"gate not wired: {type(e).__name__}: {e}")
            names = []
        self.ready.emit(names)
        self.appended.emit(
            "sys", f"gated · floor tools: {', '.join(names)} · model {self._cfg.model}")
        await self._drain()

    async def _drain(self) -> None:
        while not self._stop:
            text = await self._queue.get()
            if text is None:
                return
            self.busy.emit(True)
            try:
                await self._turn(text)
            except Exception as e:
                self.appended.emit("error", f"turn failed: {type(e).__name__}: {e}")
            finally:
                self.busy.emit(False)

    async def _turn(self, user_text: str) -> None:
        """One user turn: chat -> dispatch tool calls through coord.step -> repeat
        until the model answers in text. Each round: (1) refresh the Tier-1.5 ambient
        block from the coordinator so live health rides in context (this is what
        stops the model escalating to fetch temps/optics it already holds), and
        (2) re-read the toolset from the gate so a mid-turn grant surfaces Tier 2."""
        self._history.append({"role": "user", "content": user_text})
        loop = asyncio.get_running_loop()
        try:
            summ = self._coord.ambient_summary()
        except Exception:
            summ = None
        if summ:
            self.appended.emit("sys", f"Tier 1.5 ambient in context: {summ}")
        async with httpx.AsyncClient(timeout=120.0) as ollama:
            for _ in range(self._cfg.max_tool_iters):
                specs = self._coord.advertised()
                tier_by_name = {s.name: s.tier_level for s in specs}
                tools = [self._to_ollama_tool(s) for s in specs]
                messages = self._assemble_messages()
                msg = await self._chat(ollama, messages, tools)
                self._history.append(msg)
                calls = msg.get("tool_calls") or []
                if not calls:
                    if msg.get("content"):
                        self.appended.emit("ai", msg["content"])
                    return
                for call in calls:
                    fn = call.get("function", {})
                    name = fn.get("name", "")
                    args = fn.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            args = {}
                    self.appended.emit(
                        "tool", f"{self._tier_tag(name, tier_by_name)} "
                                f"{name}({json.dumps(args, separators=(',', ':'))})")
                    # THE dispatch swap: through the gate, on the executor so loop B
                    # stays free. A granted Tier-2 call bounces to the bridge's loop.
                    res = await loop.run_in_executor(
                        None, functools.partial(self._coord.step, name, **args))
                    if res.refused:
                        self.appended.emit("gate", res.reason)
                    # tell the model which tier answered, so its provenance is honest
                    tier = res.tier_level if res.tier_level is not None else "?"
                    self._history.append(
                        {"role": "tool", "content": f"(Tier {tier}) {self._flatten(res)}"})
            self.appended.emit(
                "sys", f"stopped at {self._cfg.max_tool_iters}-tool cap without a final answer")

    def _assemble_messages(self) -> list[dict]:
        """Base prompt + FRESH Tier-1.5 ambient + durable history. Ambient is rebuilt
        every round (never persisted into history) so it is always current and never
        accumulates. A tab with no poll yet simply omits it."""
        msgs: list[dict] = [{"role": "system", "content": self._system}]
        ambient = None
        try:
            ambient = self._coord.ambient_context()
        except Exception:
            ambient = None
        if ambient:
            msgs.append({"role": "system", "content": ambient})
        msgs.extend(self._history)
        return msgs

    @staticmethod
    def _tier_tag(name: str, tier_by_name: dict) -> str:
        if name == REQUEST_ESCALATION:
            return "⚙esc"
        t = tier_by_name.get(name)
        return f"⚙T{t}" if t is not None else "⚙"

    async def _chat(self, ollama: httpx.AsyncClient, messages: list[dict],
                    tools: list[dict]) -> dict:
        resp = await ollama.post(
            f"{self._cfg.ollama_url}/api/chat",
            json={"model": self._cfg.model, "messages": messages,
                  "tools": tools, "stream": False})
        resp.raise_for_status()
        return resp.json().get("message", {}) or {}

    # ── InvokeResult -> tool-message text the model reads ──────────────────────
    @staticmethod
    def _flatten(res: Any) -> str:
        if res.refused:
            return f"[refused] {res.reason}"
        if not res.ok:
            return f"[error] {res.reason}"
        try:
            return json.dumps(res.value)
        except (TypeError, ValueError):
            return str(res.value)

    # ── ToolSpec -> Ollama tool schema. No inputSchema on a ToolSpec, so the
    # parameters are read off the callable's signature; request_escalation is the
    # one meta-tool whose `reason` isn't in its sentinel signature, special-cased. ─
    @staticmethod
    def _to_ollama_tool(spec: Any) -> dict:
        if spec.name == REQUEST_ESCALATION:
            params = {
                "type": "object",
                "properties": {"reason": {
                    "type": "string",
                    "description": "why you need the next tier (shown to the human approver)"}},
                "required": ["reason"]}
        elif getattr(spec, "params", None) is not None:
            # the tool carries its own schema (e.g. the Tier-1 capability enum) — use
            # it verbatim so the model sees exactly what values are legal.
            params = spec.params
        else:
            props: dict = {}
            required: list[str] = []
            for pname, p in inspect.signature(spec.fn).parameters.items():
                if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                    continue
                props[pname] = {"type": "string"}
                if p.default is inspect._empty:
                    required.append(pname)
            params = {"type": "object", "properties": props}
            if required:
                params["required"] = required
        return {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description or "",
                "parameters": params,
            },
        }
"""
uf/cockpit/app.py — the cockpit shell.

The native PyQt6 chrome the mockup's CSS comment names ("tree nav + tab strip are
native PyQt6 chrome — not in this page; this page IS one session tab"). It owns:

  * the SESSION TREE (left) — devices grouped, each with a liveness dot, populated
    from Netlapse via Fleet.devices() (which is just IdentityResolver.all_devices()).
  * the TAB STRIP — one tab per open device; a tab is one SessionCoordinator.
  * each DEVICE TAB — the mockup layout: terminal (left) | telemetry (top-right) /
    investigation (bottom-right). Telemetry is the real pane; the other two are
    placeholders in their correct slots until built.

Two run modes:
    python -m uf.cockpit.app                      # DEMO — canned tree + canned feed, no gear
    python -m uf.cockpit.app --netlapse URL [--token T]   # live tree from your lab Netlapse

In live mode the tree is real; feeding a tab's telemetry off a live broker needs the
credential seam (start_broker), so that call is marked and left for the cred pass —
opening a device loads the HUD chrome, and --demo-feed can populate it meanwhile.

Qt-layer module. Imports coordinator (which pulls uf.core.ssh_client -> paramiko), so
it needs PyQt6 + PyQt6-WebEngine + paramiko in the venv. Nothing below cockpit does.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QBrush, QColor
from PyQt6.QtWidgets import (
    QApplication, QLabel, QMainWindow, QSplitter, QTabWidget, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget)

from uf.cockpit.coordinator import DeviceView, Fleet, SessionCoordinator
from uf.cockpit.panes.console import ConsolePane
from uf.cockpit.panes.investigation import InvestigationConfig, InvestigationPane
from uf.cockpit.panes.telemetry import TelemetryPane
from uf.core.ssh_client import SSHClientConfig

# ──────────────────────────────────────────────────────────────────────────
# LAB CREDENTIALS — HARDCODED, on purpose, to see the first live paint. This is
# a lab you already expose creds for; swap to the vault/env CredentialProvider
# before this is anything but a spike. Fields are the exact set live_harness
# proved against your gear.
# ──────────────────────────────────────────────────────────────────────────
LAB_USER       = "speterman"
LAB_PASS       = None                 # set one of PASS / KEY_FILE
LAB_KEY_FILE   = "~/.ssh/id_rsa"
LAB_KEY_PASS   = None
# ── Investigation pane (UNGATED spike) — same hardcoded-spike posture as the lab
# creds above. mcpssh runs as its own process (streamable-http); this is only where
# the pane REACHES it, never an import. verify_tls is inert on http:// — it is the
# VPN verify-off switch for the day mcpssh is fronted with an internal-CA cert. If
# the cockpit runs on a different box than Ollama, point ollama_url at the Ollama
# host's VPN address (the 0.0.0.0 bind is what makes that reachable).
INVEST_CFG = InvestigationConfig(
    mcpssh_url   = "http://127.0.0.1:8000/mcp",   # the running mcpssh streamable-http endpoint
    mcpssh_token = None,                          # set iff MCPSSH_HTTP_AUTH_ENABLED=true
    ollama_url   = "http://127.0.0.1:11434",      # default; override with --ollama / OLLAMA_URL
    model        = "qwen3:30b-a3b-q8_0",                 # must be a TOOL-capable model
    verify_tls   = True,                          # False = VPN verify-off (mcpssh TLS only)
)
LAB_PORT       = 22
LAB_LEGACY     = False                # True if a box needs legacy SSH algos
DEFAULT_ARISTA_KEYS = {"bgp", "ospf", "lldp", "interfaces", "environment",
                       "transceivers", "proc", "version"}
LIVE_KEYS   = set(DEFAULT_ARISTA_KEYS)
def make_lab_provider(name_to_ip: dict[str, str]):
    """A (device, posture) -> SSHClientConfig for the cockpit's OWN device SSH
    (terminal + Tier 1.5 broker) — SEPARATE from mcpssh's creds. Same creds for
    every posture (the settled tradeoff); host comes from the tree's IP.

    Credentials are read from the environment so nothing lab-specific lives in
    source (publishing-safe). Set whichever auth your gear uses:

        LAB_SSH_USERNAME   (default: admin)
        LAB_SSH_PASSWORD   (password auth)
        LAB_SSH_KEY_FILE   (key auth; expanduser'd)
        LAB_SSH_KEY_PASS   (key passphrase, if the key is encrypted)
        LAB_SSH_LEGACY     (true/false; enable legacy SSH algos for old gear)

    Password and key may both be set (key tried first, password fallback). At
    least one is required, exactly like mcpssh's own resolver.
    """
    import os
    username = os.environ.get("LAB_SSH_USERNAME", "admin")
    password = os.environ.get("LAB_SSH_PASSWORD")            # None -> key-only
    key_file = os.environ.get("LAB_SSH_KEY_FILE")            # None -> password-only
    key_pass = os.environ.get("LAB_SSH_KEY_PASS")
    legacy = os.environ.get("LAB_SSH_LEGACY", "false").lower() == "true"

    def provider(device: str, _posture: str) -> SSHClientConfig:
        return SSHClientConfig(
            host=name_to_ip[device], username=username, port=LAB_PORT,
            password=password,
            key_file=key_file,
            key_passphrase=key_pass,
            legacy_mode=legacy)
    return provider


# palette lifted from the mockup so the native chrome reads as one surface with the HUD
_QSS = """
QMainWindow, QWidget { background:#060a06; color:#2ee66a; font-family:'IBM Plex Mono',monospace; font-size:12px; }
QTreeWidget { background:#080e08; border:none; outline:0; }
QTreeWidget::item { padding:3px 2px; }
QTreeWidget::item:selected { background:#0f3d20; }
QTabWidget::pane { border:1px solid #0f3d20; }
QTabBar::tab { background:#080e08; color:#166b34; padding:5px 12px; border:1px solid #0f3d20; }
QTabBar::tab:selected { color:#2ee66a; background:#0a130c; }
QSplitter::handle { background:#0f3d20; }
"""

_DEMO_FEED = {
    "bgp": {"key": "bgp", "state": "PRESENT", "reason": "", "age_s": 2, "frames": [],
            "payload": [{"peerAddress": "172.16.7.1", "peerState": "Established",
                         "description": "spine2", "prefixReceived": 412},
                        {"peerAddress": "172.16.7.4", "peerState": "Active",
                         "description": "leaf-2", "prefixReceived": 0}]},
    "ospf": {"key": "ospf", "state": "ABSENT", "age_s": 2, "frames": [],
             "reason": "routing protocol not configured", "payload": None},
    "transceivers": {"key": "transceivers", "state": "UNREAD", "age_s": 45, "frames": [],
                     "reason": "timed out after 45s — no answer", "payload": None},
}

_DEMO_DEVICES = [
    DeviceView("eng-spine-1", "arista", "eng-lab", "172.16.2.2", True, "success"),
    DeviceView("eng-spine-2", "arista", "eng-lab", "172.16.2.6", False, "shell_timeout"),
    DeviceView("lab-edge-1", "juniper", "prod-lab", "10.0.0.1", True, "success"),
]


def _placeholder(tag: str, sub: str) -> QWidget:
    """A pane slot not yet built — labelled with its posture so the layout reads true."""
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setAlignment(Qt.AlignmentFlag.AlignCenter)
    t = QLabel(tag); t.setStyleSheet("color:#2ee66a;font-size:13px;letter-spacing:.14em;")
    s = QLabel(sub); s.setStyleSheet("color:#166b34;font-size:10px;")
    for x in (t, s):
        x.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(x)
    return w


class DeviceTab(QSplitter):
    """One device = one tab. Layout mirrors the mockup: terminal | (telemetry / investigation)."""

    def __init__(self, coord: Optional[SessionCoordinator], device: str, vendor: str,
                 invest_cfg: InvestigationConfig):
        super().__init__(Qt.Orientation.Horizontal)
        self.coord = coord
        # left half — the ungated engineer terminal (INTERACTIVE posture). Its own
        # SSH session to the tab's host via coord.interactive(); demo/gear-free
        # tabs (no coordinator) get an inert console that says so.
        self.console = ConsolePane(coord.interactive if coord is not None else None)
        self.addWidget(self.console)
        # right half — telemetry (top) over investigation (bottom)
        right = QSplitter(Qt.Orientation.Vertical)
        self.telemetry = TelemetryPane(device)
        right.addWidget(self.telemetry)
        # GATED: the pane dispatches every tool call through coord.step() (the gate).
        # Tier 1 (netlapse) is the floor; Tier 2 (mcpssh) is withheld until the
        # operator approves an escalation. A None coord (demo tab) makes it inert.
        self.investigation = InvestigationPane(device, vendor, coord, invest_cfg)
        right.addWidget(self.investigation)
        right.setSizes([560, 300])
        self.addWidget(right)
        self.setSizes([600, 640])
        # the real feed wire: coordinator's poll -> the pane. In demo mode there is
        # no coordinator; MainWindow pushes _DEMO_FEED to self.telemetry directly.
        if coord is not None:
            coord.broker_polled.connect(self.telemetry.apply_feed)
            # SEAM (cred pass): coord.start_broker(<widget keys>) once a
            # CredentialProvider is wired — then this pane paints off a live poll.

    def shutdown(self) -> None:
        """Tab is closing — kill the interactive shell so it doesn't outlive its
        tab (one tab = one interactive session)."""
        self.console.shutdown()
        self.investigation.shutdown()       # tears down the MCP session + asyncio loop


class MainWindow(QMainWindow):
    def __init__(self, devices: list[DeviceView], fleet: Optional[Fleet], demo_feed: bool):
        super().__init__()
        self.setWindowTitle("uglyfruit — cockpit")
        self.resize(1500, 950)
        self._fleet = fleet
        self._demo_feed = demo_feed
        self._open: dict[str, int] = {}          # device name -> tab index
        # name -> ip, so the hardcoded lab provider can fill SSHClientConfig.host
        self._name_to_ip = {d.name: d.ip for d in devices}
        self._name_to_vendor = {d.name: d.vendor for d in devices}   # for the investigation pane
        self._provider = make_lab_provider(self._name_to_ip)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.tree = QTreeWidget(); self.tree.setHeaderHidden(True)
        self.tree.setMinimumWidth(230); self.tree.setMaximumWidth(360)
        self.tree.itemDoubleClicked.connect(self._on_pick)
        self.tabs = QTabWidget(); self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._on_close)
        split.addWidget(self.tree); split.addWidget(self.tabs)
        split.setSizes([260, 1240])
        self.setCentralWidget(split)

        self._populate(devices)

    def _populate(self, devices: list[DeviceView]) -> None:
        groups: dict[str, QTreeWidgetItem] = {}
        for d in sorted(devices, key=lambda x: (x.group, x.name)):
            g = groups.get(d.group)
            if g is None:
                g = QTreeWidgetItem(self.tree, [d.group]); g.setExpanded(True)
                g.setForeground(0, QBrush(QColor("#166b34")))
                groups[d.group] = g
            item = QTreeWidgetItem(g, [f"● {d.name}"])
            item.setData(0, Qt.ItemDataRole.UserRole, d.name)
            item.setForeground(0, QBrush(QColor("#2ee66a" if d.live else "#ff3b3b")))
            item.setToolTip(0, f"{d.vendor} · {d.ip} · last: {d.last}")

    def _on_pick(self, item: QTreeWidgetItem, _col: int) -> None:
        name = item.data(0, Qt.ItemDataRole.UserRole)
        if not name:                              # a group header, not a device
            return
        if name in self._open:                    # already open — focus it
            self.tabs.setCurrentIndex(self._open[name])
            return
        coord = self._fleet.open(name) if self._fleet else None
        if coord is not None:
            # both the console (INTERACTIVE) and the broker (BROKER) pull creds
            # from here, so set them before either pane can call interactive()/
            # start_broker() — including under --demo-feed, where the HUD is canned
            # but the terminal is still a real shell to the box.
            coord.set_credentials(self._provider)
        vendor = coord.vendor if coord is not None else self._name_to_vendor.get(name, "arista")
        tab = DeviceTab(coord, name, vendor, INVEST_CFG)
        if coord is not None:
            # wire the gate now that the pane exists (its modal is the approver),
            # THEN start the pane's model loop so it can't race an unbuilt gate.
            # Tier 1 is the app-wide Netlapse floor; the coordinator builds its own
            # tab-lived mcpssh bridge for Tier 2 from the INVEST_CFG endpoint.
            # verify_tls=False is the VPN verify-off switch (mcpssh TLS surface).
            coord.wire(self._fleet.tier1(), self._provider,
                       tab.investigation.request_approval,
                       mcpssh_url=INVEST_CFG.mcpssh_url,
                       mcpssh_token=INVEST_CFG.mcpssh_token,
                       verify_tls=INVEST_CFG.verify_tls)
            tab.investigation.start()
        idx = self.tabs.addTab(tab, name)
        self._open[name] = idx
        self.tabs.setCurrentIndex(idx)
        if self._demo_feed:                       # gear-free HUD: paint the canned feed
            QTimer.singleShot(400, lambda t=tab: t.telemetry.apply_feed(_DEMO_FEED))
        elif coord is not None:                   # LIVE: hardcoded creds -> real poll
            coord.broker_error.connect(lambda msg, n=name: print(f"[{n}] broker: {msg}"))
            coord.start_broker(LIVE_KEYS)         # coordinator clamps to the vendor manifest

    def _on_close(self, idx: int) -> None:
        tab = self.tabs.widget(idx)
        if isinstance(tab, DeviceTab):
            tab.shutdown()                       # kill the interactive shell first
            if tab.coord is not None:
                tab.coord.close()
        name = next((n for n, i in self._open.items() if i == idx), None)
        if name:
            del self._open[name]
        self.tabs.removeTab(idx)
        self._open = {n: (i if i < idx else i - 1) for n, i in self._open.items()}


def main() -> int:
    p = argparse.ArgumentParser(description="uglyfruit cockpit shell")
    p.add_argument("--netlapse", help="Netlapse base URL for the live tree (e.g. http://0.0.0.0:8888)")
    p.add_argument("--token", help="Netlapse api_token (or env NETLAPSE_API_TOKEN)")
    p.add_argument("--scheme", choices=("bearer", "apikey"), default="bearer")
    p.add_argument("--ollama",
                   help="Ollama base URL for the investigation model "
                        "(or env OLLAMA_URL / OLLAMA_HOST). Default http://127.0.0.1:11434. "
                        "A bare host:port is accepted and gets http:// prepended.")
    p.add_argument("--demo-feed", action="store_true",
                   help="push a canned feed into opened tabs (paints the HUD without a broker)")
    args = p.parse_args()

    # Resolve the Ollama endpoint: CLI > OLLAMA_URL > OLLAMA_HOST > the built-in
    # default. So the cockpit host and the Ollama host can differ (split over the
    # tunnel) without editing source. A schemeless host:port (Ollama's own
    # OLLAMA_HOST convention) is normalised to a URL.
    import os
    import dataclasses
    global INVEST_CFG
    _ollama = (args.ollama or os.environ.get("OLLAMA_URL")
               or os.environ.get("OLLAMA_HOST") or INVEST_CFG.ollama_url)
    if _ollama and not _ollama.startswith(("http://", "https://")):
        _ollama = "http://" + _ollama
    INVEST_CFG = dataclasses.replace(INVEST_CFG, ollama_url=_ollama)

    from uf.cockpit.preflight import resolve_netlapse_token, run_preflight
    args.token, _tok_src = resolve_netlapse_token(args)  # <-- populates the token
    if not run_preflight(args, INVEST_CFG,
                         strict=False, skip=False,
                         token_source=_tok_src):
        return 2

    app = QApplication(sys.argv)
    app.setStyleSheet(_QSS)

    if args.netlapse:
        import os
        fleet = Fleet(args.netlapse, args.token or os.environ.get("NETLAPSE_API_TOKEN"), args.scheme)
        devices = fleet.devices()                 # live from Netlapse
        win = MainWindow(devices, fleet, demo_feed=args.demo_feed)
    else:
        win = MainWindow(_DEMO_DEVICES, fleet=None, demo_feed=True)  # fully gear-free

    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
"""
uf/cockpit/panes/telemetry.py — the Tier 1.5 HUD pane.

Loads web/nethuds.html into a QWebEngineView and pushes each broker feed into the
page by calling window.applyFeed(feed) — the single seam cockpit.js advertises.

Direction: pure Python -> JS (runJavaScript). No QWebChannel yet, deliberately.
A QWebChannel earns its keep only when the page must call BACK into Python — the
"click a panel to drill into the investigation" gesture. Until that exists, a
channel is setup cost with no payload; runJavaScript maps one-to-one onto the
applyFeed seam and needs no change to the carved web assets.

Feed shape is exactly what SessionCoordinator.broker_polled emits and what the
widgets eat — one determine_all(vendor, slice) per poll, projected to
    {cap: {state, frames, reason, age_s, payload}}   (payload on PRESENT only)
so the panels (payload) and the header chips (state) read the same poll and
cannot disagree. The pane never interprets the feed; it hands it to the page.

Qt-layer module: imports PyQt6, so it lives under uf/cockpit/ and imports only
once PyQt6 + PyQt6-WebEngine are in the venv. Nothing below the cockpit does.
"""

from __future__ import annotations

import json
import pathlib
from typing import Optional

from PyQt6.QtCore import QUrl
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings

# panes/ -> cockpit/ -> web/nethuds.html
_WEB = pathlib.Path(__file__).resolve().parent.parent / "web"
_INDEX = _WEB / "nethuds.html"


class TelemetryPane(QWebEngineView):
    """The 1.5 HUD surface. Wire it to a coordinator in one line:

        pane = TelemetryPane()
        coordinator.broker_polled.connect(pane.apply_feed)

    The coordinator owns the poll cadence; this pane owns render. It buffers the
    latest feed until the page has finished loading, so a poll that lands before
    load is not dropped — it paints on loadFinished.
    """

    def __init__(self, device: str = "", parent=None):
        super().__init__(parent)
        # the resolver device identity, seeded into the nameplate at load (not from
        # any health read). Empty -> the neutral placeholder stands.
        self._device = device
        # local file:// loading the sibling <script src> modules needs this, or
        # the page loads blank with no error.
        self.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self._loaded = False
        self._pending: Optional[dict] = None       # latest feed seen before ready
        self.loadFinished.connect(self._on_loaded)
        self.load(QUrl.fromLocalFile(str(_INDEX)))

    # ── the seam: connect SessionCoordinator.broker_polled here ──────────────
    def apply_feed(self, feed: dict) -> None:
        if not self._loaded:
            self._pending = feed                   # coalesce to latest; flush on load
            return
        self._push(feed)

    def _on_loaded(self, ok: bool) -> None:
        self._loaded = bool(ok)
        if ok and self._device:
            # seed the nameplate identity BEFORE any feed paints the health line
            self.page().runJavaScript(
                f"window.setIdentity && window.setIdentity({json.dumps(self._device)})")
        if ok and self._pending is not None:
            self._push(self._pending)
            self._pending = None

    def _push(self, feed: dict) -> None:
        # JSON is a subset of JS object-literal syntax; ensure_ascii keeps the
        # injected string transport-clean. The page's applyFeed patches per-cap
        # containers in place, so re-pushing every poll never stacks duplicates.
        self.page().runJavaScript(f"window.applyFeed({json.dumps(feed, default=str)})")


# ──────────────────────────────────────────────────────────────────────────
# Standalone demo — watch the panels light up with NO gear and NO Netlapse.
#   python -m uf.cockpit.panes.telemetry
# Pushes one canned feed exercising all three states, proving the Python->JS
# applyFeed path end to end before the broker is wired. Needs only PyQt6 +
# PyQt6-WebEngine (no paramiko, no mcp).
# ──────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import QTimer

    DEMO_FEED = {
        "bgp": {"key": "bgp", "state": "PRESENT", "reason": "", "age_s": 2, "frames": [],
                "payload": [
                    {"peerAddress": "172.16.7.1", "peerState": "Established",
                     "description": "spine2", "prefixReceived": 412},
                    {"peerAddress": "172.16.7.4", "peerState": "Active",
                     "description": "leaf-2", "prefixReceived": 0},
                ]},
        "ospf": {"key": "ospf", "state": "ABSENT", "age_s": 2, "frames": [],
                 "reason": "routing protocol not configured", "payload": None},
        "transceivers": {"key": "transceivers", "state": "UNREAD", "age_s": 45,
                         "frames": [], "reason": "timed out after 45s — no answer",
                         "payload": None},
    }

    app = QApplication(sys.argv)
    pane = TelemetryPane()
    pane.setWindowTitle("uf · telemetry pane · demo feed")
    pane.resize(560, 900)
    pane.show()
    # apply_feed buffers until loadFinished, so an immediate call is safe; the
    # short delay just makes the paint visibly follow the load.
    QTimer.singleShot(400, lambda: pane.apply_feed(DEMO_FEED))
    sys.exit(app.exec())
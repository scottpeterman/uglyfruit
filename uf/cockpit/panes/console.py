"""
uf/cockpit/panes/console.py — the interactive console pane (INTERACTIVE posture).

The left half of a device tab: a real xterm.js terminal on the engineer's OWN SSH
session to the same host the tab's 1.5 widgets read. It is the mockup's
"TERMINAL · interactive shell · UNGATED — you own the CLI" slot, made live.

Why this pane exists as its own session, not a view onto the broker:

  * The broker (Tier 1.5) is read-only BY CONSTRUCTION — a consumer hands it
    capability KEYS, never command strings (session.py §3). A terminal is the
    opposite: arbitrary keystrokes. So it CANNOT ride the broker; it must be its
    own transport, its own posture. That is exactly the INTERACTIVE slot the
    ceiling already tracks (session.py §1), reached through
    SessionCoordinator.interactive(), which the coordinator marks open but never
    mediates: "these bytes are UNGATED — you already own the CLI."

  * One tab = one interactive shell (coordinator's stated resolution of the
    session.py §7 open question). Close the tab, close this shell.

The byte path, drawn once so the threading is legible:

    keystroke  xterm.onData ─(QWebChannel)─▶ bridge.send ─▶ worker.write ─▶ channel.send
    device out channel.recv ─▶ worker(thread) ─▶ received ─(queued)─▶ bridge.output ─▶ term.write

The blocking I/O — the SSH connect (which on a legacy box negotiates ciphers and
may retry) and the recv loop — runs on _ConsoleWorker (a QThread), never the GUI
thread, the same rule the broker's _PollWorker follows. Only that thread reads the
channel; writes/resizes are single calls paramiko permits from another thread.

Qt-layer module: PyQt6 + PyQt6-WebEngine, and it drives a uf.core.ssh_client
SSHClient. Nothing below cockpit/ imports it.

VPN/TLS note: this pane has NO TLS surface. It is pure SSH (host-key TOFU via the
core client's AutoAddPolicy, same as the read path). The cockpit's only
verify-off knob is the Netlapse tree fetch (Fleet/IdentityResolver), which is a
separate seam — there is nothing to disable here.
"""

from __future__ import annotations

import pathlib
import socket
from typing import Callable, Optional

from PyQt6.QtCore import Qt, QObject, QThread, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineSettings

from uf.core.ssh_client import SSHClient

# panes/ -> cockpit/ -> web/
_WEB = pathlib.Path(__file__).resolve().parent.parent / "web"
_INDEX = _WEB / "terminal.html"

# The pane calls session_factory() to get its OWN unconnected SSHClient. In the
# cockpit this is SessionCoordinator.interactive (a zero-arg bound method that
# builds the INTERACTIVE-posture config, marks the posture open, and returns a
# fresh SSHClient). Kept as a bare callable so this pane never imports the
# coordinator — the same one-way dependency the telemetry pane keeps.
SessionFactory = Callable[[], SSHClient]


def _qwebchannel_script() -> "Optional[object]":
    """Read qwebchannel.js out of the Qt resource bundle and wrap it as a
    QWebEngineScript that runs at document-creation in the MAIN world.

    The page loads from file://; pulling qwebchannel.js from the qrc:/// scheme
    across that origin — or from a copied file — is the one brittle spot that
    varies by WebEngine build and by whether the install dir is writable.
    Injecting the resource's bytes directly sidesteps both: window.QWebChannel is
    defined, in the same world as the page's script, before that script runs, and
    it is exactly the version shipped with the running PyQt6. Nothing on disk,
    nothing from the network — the property the VPN lab needs.
    """
    from PyQt6.QtCore import QFile, QIODevice
    from PyQt6.QtWebEngineCore import QWebEngineScript

    src = QFile(":/qtwebchannel/qwebchannel.js")
    if not src.open(QIODevice.OpenModeFlag.ReadOnly):
        return None                                # unusual; page will show a clear status
    try:
        code = bytes(src.readAll()).decode("utf-8", errors="replace")
    finally:
        src.close()

    script = QWebEngineScript()
    script.setName("qwebchannel.js")
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
    script.setRunsOnSubFrames(False)
    script.setSourceCode(code)
    return script


# ──────────────────────────────────────────────────────────────────────────
# The bridge — the single QObject QWebChannel exposes to the page as "bridge".
# Signals are Py->JS (the page connects them); slots are JS->Py (the page calls
# them). The slots re-emit plain Qt signals so the pane owns the behaviour and
# this object stays a thin transport.
# ──────────────────────────────────────────────────────────────────────────
class _TerminalBridge(QObject):
    output = pyqtSignal(str)          # Py -> JS : device bytes -> term.write
    status = pyqtSignal(str)          # Py -> JS : "cls|text" -> the status strip

    # internal (Py <- JS, re-emitted for the pane to wire)
    dataIn = pyqtSignal(str)          # keystrokes
    resized = pyqtSignal(int, int)    # cols, rows
    pageReady = pyqtSignal()          # page + terminal up, safe to connect SSH

    @pyqtSlot(str)
    def send(self, data: str) -> None:
        self.dataIn.emit(data)

    @pyqtSlot(int, int)
    def resize(self, cols: int, rows: int) -> None:
        self.resized.emit(cols, rows)

    @pyqtSlot()
    def notifyReady(self) -> None:
        self.pageReady.emit()


# ──────────────────────────────────────────────────────────────────────────
# The worker — owns the SSH channel off the GUI thread. Connects, then loops
# recv -> received. write()/resize_pty() are single calls the GUI thread makes
# directly (paramiko permits send/resize concurrent with recv on one channel);
# a lock + connected flag guard the window before connect and after teardown.
# ──────────────────────────────────────────────────────────────────────────
class _ConsoleWorker(QThread):
    received = pyqtSignal(str)        # decoded device bytes
    statusChanged = pyqtSignal(str)   # "cls|text"

    def __init__(self, client: SSHClient, cols: int, rows: int, parent=None):
        super().__init__(parent)
        self._client = client
        self._cols, self._rows = cols, rows
        self._chan = None
        self._stop = False
        import threading
        self._io_lock = threading.Lock()

    def run(self) -> None:
        try:
            # reuses the core client's proven negotiation (legacy KEX, SHA2 retry);
            # RAW channel — no drain, no ANSI filter — see ssh_client.open_interactive_channel.
            chan = self._client.open_interactive_channel(self._cols, self._rows)
        except Exception as e:                       # auth / transport failure
            self.statusChanged.emit(f"down|connect failed — {type(e).__name__}: {e}")
            return
        self._chan = chan
        chan.settimeout(0.2)                          # so the loop can see _stop
        self.statusChanged.emit("up|connected")

        while not self._stop:
            try:
                data = chan.recv(4096)
            except socket.timeout:
                continue                              # no bytes this tick — re-check _stop
            except Exception as e:
                self.statusChanged.emit(f"down|read error — {type(e).__name__}: {e}")
                break
            if not data:                              # b'' == remote closed the shell
                self.statusChanged.emit("down|session closed")
                break
            # xterm is a real VT emulator: hand it the escapes intact. errors=replace
            # keeps a stray non-UTF-8 byte from killing the stream.
            self.received.emit(data.decode("utf-8", errors="replace"))

        self._teardown()

    # ── GUI-thread calls (guarded) ───────────────────────────────────────────
    def write(self, data: str) -> None:
        with self._io_lock:
            if self._chan is None or self._stop:
                return
            try:
                self._chan.send(data.encode("utf-8"))
            except Exception:
                pass                                  # closing/closed — the loop will surface it

    def resize_pty(self, cols: int, rows: int) -> None:
        self._cols, self._rows = cols, rows
        with self._io_lock:
            if self._chan is None or self._stop:
                return
            try:
                self._chan.resize_pty(width=cols, height=rows)
            except Exception:
                pass

    def stop(self) -> None:
        self._stop = True
        self.wait(4000)

    def _teardown(self) -> None:
        with self._io_lock:
            try:
                if self._chan is not None:
                    self._chan.close()
            except Exception:
                pass
            self._chan = None
        try:
            self._client.disconnect()
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────
# ConsolePane — the widget the DeviceTab mounts in the TERMINAL slot.
#
#   pane = ConsolePane(coord.interactive)   # live: real shell to the tab's host
#   pane = ConsolePane(None)                 # demo/gear-free: inert placeholder
# ──────────────────────────────────────────────────────────────────────────
class ConsolePane(QWebEngineView):
    def __init__(self, session_factory: Optional[SessionFactory], parent=None):
        super().__init__(parent)
        self._factory = session_factory
        self._worker: Optional[_ConsoleWorker] = None
        self._started = False

        self.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)

        # define window.QWebChannel before the page's own script runs
        qwc = _qwebchannel_script()
        if qwc is not None:
            self.page().scripts().insert(qwc)

        self._bridge = _TerminalBridge()
        self._channel = QWebChannel(self.page())
        self._channel.registerObject("bridge", self._bridge)
        self.page().setWebChannel(self._channel)

        # JS -> Py wiring
        self._bridge.pageReady.connect(self._on_page_ready)
        self._bridge.dataIn.connect(self._on_data)
        self._bridge.resized.connect(self._on_resize)

        self.load(QUrl.fromLocalFile(str(_INDEX)))

    # ── page lifecycle ────────────────────────────────────────────────────────
    def _on_page_ready(self) -> None:
        """Fired by the page once xterm is open and fitted. Only now — with a
        terminal ready to paint — do we start the SSH connect, so no banner byte
        can race ahead of a page that can't yet render it."""
        if self._started:
            return
        self._started = True
        if self._factory is None:
            self._bridge.status.emit("wait|demo — no live session for this tab")
            return
        try:
            client = self._factory()                  # coord.interactive(): fresh SSHClient
        except Exception as e:
            self._bridge.status.emit(f"down|no session — {type(e).__name__}: {e}")
            return
        # start with the page's fitted geometry if we have it, else a sane default
        cols, rows = getattr(self, "_last_size", (80, 24))
        self._worker = _ConsoleWorker(client, cols, rows)
        # device bytes / status -> the page. QueuedConnection: the worker emits
        # from its own thread; delivery must hop to the GUI thread where the
        # bridge lives and QWebChannel can serialize to JS.
        self._worker.received.connect(self._bridge.output, Qt.ConnectionType.QueuedConnection)
        self._worker.statusChanged.connect(self._bridge.status, Qt.ConnectionType.QueuedConnection)
        self._worker.start()

    # ── JS -> Py handlers ─────────────────────────────────────────────────────
    def _on_data(self, data: str) -> None:
        if self._worker is not None:
            self._worker.write(data)

    def _on_resize(self, cols: int, rows: int) -> None:
        self._last_size = (cols, rows)
        if self._worker is not None:
            self._worker.resize_pty(cols, rows)

    # ── teardown: DeviceTab/close calls this so the shell dies with the tab ───
    def shutdown(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self._worker = None


# ──────────────────────────────────────────────────────────────────────────
# Standalone smoke test — a real shell with NO cockpit, NO Netlapse, NO broker.
#   python -m uf.cockpit.panes.console user@host [password]
# Proves the whole byte path (xterm <-> QWebChannel <-> core SSHClient) end to
# end against one box before it is mounted in a tab.
# ──────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    from PyQt6.QtWidgets import QApplication
    from uf.core.ssh_client import SSHClientConfig

    if len(sys.argv) < 2 or "@" not in sys.argv[1]:
        print("usage: python -m uf.cockpit.panes.console user@host [password]")
        raise SystemExit(2)
    user, host = sys.argv[1].split("@", 1)
    pw = sys.argv[2] if len(sys.argv) > 2 else None

    def factory() -> SSHClient:
        cfg = SSHClientConfig(
            host=host, username=user, password=pw,
            key_file=None if pw else "~/.ssh/id_rsa",
            legacy_mode=True)
        return SSHClient(cfg)

    app = QApplication(sys.argv)
    app.setStyleSheet("QWidget{background:#060a06}")
    pane = ConsolePane(factory)
    pane.setWindowTitle(f"uf · console · {user}@{host}")
    pane.resize(900, 560)
    pane.show()
    rc = app.exec()
    pane.shutdown()
    sys.exit(rc)

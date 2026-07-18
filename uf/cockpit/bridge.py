"""
uf/cockpit/bridge.py — the sync-faced, tab-lived MCP client to mcpssh.

The Tier-2 transport, made a coordinator-owned object. mcpssh stays a SEPARATE
PROCESS reached over streamable-http; this is only the *client* side of that one
MCP hop — never an import of mcpssh, never a second MCP layer. Co-location is
organization, not authorization: the process boundary is the credential/audit
isolation, and this bridge only carries bytes across it.

Why it exists as its own object (not left in the investigation pane):

  * OWNERSHIP.  The gated MCP session is the third device posture (BROKER /
    INTERACTIVE / GATED). It belongs next to the other two, in the coordinator's
    DeviceSessions neighbourhood — not split off in a render pane.
  * LIFETIME.  Tab-lived: opened LAZILY on the first escalation grant (so nothing
    contacts mcpssh until a human has approved once), then held for the tab so the
    MCP initialize+list_tools handshake is paid once, not on every re-grant — the
    round-trip you do NOT want to eat over the VPN at the moment the operator has
    just said yes.
  * THREADING.  It owns its OWN asyncio loop on its OWN thread (loop C). The gate
    runs on a threadpool thread (coord.step via run_in_executor) and reaches the
    session by scheduling call_tool back onto loop C with run_coroutine_threadsafe
    — a cross-thread bounce, never a same-loop await, so it cannot deadlock the
    pane's Ollama loop (loop B).

VPN / TLS (the split-box-over-tunnel switch): `verify_tls=False` threads verify-off
through the httpx factory in the default opener. Inert on http://; the switch you
flip the day mcpssh is fronted with an internal-CA cert. mcpssh is the only TLS
surface here — Ollama and Netlapse are elsewhere.

The MCP session `opener` is injected (defaulting to the real streamable-http client)
so the loop/thread/close mechanics are self-testable with a fake session — no mcp,
no httpx, no gear. Run the proof:  python -m uf.cockpit.bridge
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from typing import Any, AsyncContextManager, Callable, Optional


# An opener is a zero-arg callable returning an async context manager that yields a
# session object exposing async .initialize(), .list_tools(), .call_tool(name,args).
# The real one wraps mcpssh's streamable-http client; a fake one drives the tests.
Opener = Callable[[], AsyncContextManager[Any]]


def _default_opener(url: str, token: Optional[str], verify_tls: bool) -> Opener:
    """The production opener: streamable-http -> ClientSession, held open. Imports
    mcp/httpx LAZILY so importing this module needs only stdlib (keeps the self-test
    dependency-free). verify_tls=False is the VPN verify-off switch (mcpssh TLS)."""
    @asynccontextmanager
    async def _open():
        import httpx  # lazy: only when a real session is actually opened
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {"Authorization": f"Bearer {token}"} if token else {}

        def _factory(headers=None, timeout=None, auth=None):
            return httpx.AsyncClient(
                headers=headers, timeout=timeout, auth=auth,
                verify=verify_tls, follow_redirects=True)

        async with streamablehttp_client(
                url, headers=headers, httpx_client_factory=_factory) as (read, write, _sid):
            async with ClientSession(read, write) as session:
                yield session
    return _open


def _result_to_dict(result: Any) -> dict:
    """Flatten an MCP CallToolResult to a plain dict the model can read. Text blocks
    concatenated; else structuredContent; else a no-content marker."""
    parts: list[str] = []
    for block in (getattr(result, "content", None) or []):
        txt = getattr(block, "text", None)
        if txt is not None:
            parts.append(txt)
    if parts:
        return {"output": "\n".join(parts)}
    sc = getattr(result, "structuredContent", None)
    if sc is not None:
        return sc if isinstance(sc, dict) else {"output": sc}
    return {"output": "[tool returned no content]"}


class McpsshBridge:
    """One tab's client to the external mcpssh server. Lazy-open, tab-lived, sync-faced.

    Not thread-safe for concurrent opens; the coordinator opens it from a single
    escalation approval and tears it down at tab close. call_tool is safe to call
    from any thread once open (it only schedules onto loop C).
    """

    def __init__(self, url: str, token: Optional[str] = None,
                 verify_tls: bool = True, *, connect_timeout: float = 20.0,
                 opener: Optional[Opener] = None):
        self._opener = opener or _default_opener(url, token, verify_tls)
        self._connect_timeout = connect_timeout

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._session: Any = None
        self._close_evt: Optional[asyncio.Event] = None
        self._session_ready = threading.Event()   # set on open success OR failure
        self._err: Optional[str] = None
        self._tool_names: list[str] = []
        self._closing = False

    # ── the coordinator's _approve calls this, idempotently, at grant time ──────
    def ensure_open(self, timeout: Optional[float] = None) -> None:
        """Open the MCP session if not already open; block until it is (or fail).
        Idempotent and tab-lived: a second call is a no-op while the session lives.
        Raises on connect failure so _approve can DENY on a dead transport rather
        than grant against nothing."""
        if self._session_ready.is_set():
            if self._err:
                raise RuntimeError(f"mcpssh bridge failed to open: {self._err}")
            return
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name="mcpssh-bridge", daemon=True)
            self._thread.start()
        if not self._session_ready.wait(timeout or self._connect_timeout):
            raise TimeoutError("mcpssh bridge: session did not open in time")
        if self._err:
            raise RuntimeError(f"mcpssh bridge failed to open: {self._err}")

    @property
    def is_open(self) -> bool:
        return self._session is not None

    def tool_names(self) -> list[str]:
        return list(self._tool_names)

    # ── the gate's Tier-2 ToolSpec.fn calls this (on a pool thread) ─────────────
    def call_tool(self, name: str, args: dict, timeout: float = 60.0) -> dict:
        """Run one mcpssh tool call synchronously by bouncing the async call onto
        loop C. A DEAD transport or an MCP error comes back as a dict the model can
        read — never an exception up into the gate (envelope-not-crash)."""
        loop, session = self._loop, self._session
        if loop is None or session is None:
            return {"outcome": "error", "reason": "mcpssh session not open"}
        try:
            fut = asyncio.run_coroutine_threadsafe(session.call_tool(name, args), loop)
            result = fut.result(timeout)
        except Exception as e:   # transport/timeout — surfaced, not raised
            return {"outcome": "error", "reason": f"{type(e).__name__}: {e}"}
        return _result_to_dict(result)

    # ── tab teardown ────────────────────────────────────────────────────────────
    def close(self) -> None:
        """Signal loop C to exit the session context and stop; join the thread.
        Safe to call more than once and safe if never opened."""
        if self._closing:
            return
        self._closing = True
        loop, evt = self._loop, self._close_evt
        if loop is not None and evt is not None:
            loop.call_soon_threadsafe(evt.set)
        if self._thread is not None:
            self._thread.join(6.0)

    # ── loop C ──────────────────────────────────────────────────────────────────
    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._hold())
        finally:
            loop.close()

    async def _hold(self) -> None:
        """Open the session, publish it + its tool names, then hold the context
        open until close() sets the event. All session I/O for the tab happens
        inside this single `async with`, on this single loop."""
        self._close_evt = asyncio.Event()
        try:
            async with self._opener() as session:
                await session.initialize()
                self._tool_names = [t.name for t in (await session.list_tools()).tools]
                self._session = session
                self._session_ready.set()      # ensure_open unblocks: success
                await self._close_evt.wait()    # tab-lived hold
        except Exception as e:
            self._err = f"{type(e).__name__}: {e}"
            self._session_ready.set()          # ensure_open unblocks: failure (checks _err)
        finally:
            self._session = None


# ──────────────────────────────────────────────────────────────────────────────
# Self-test — the loop/thread/close mechanics with a FAKE session. No mcp, no
# httpx, no gear: proves lazy-open, sync call across the loop boundary, the
# dead-transport envelope, tab-lived reuse, and clean teardown.
#   python -m uf.cockpit.bridge
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    OK, BAD = "\u2713", "\u2717"
    fails = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        global fails
        if not cond:
            fails += 1
        print(f"  {OK if cond else BAD} {name}" + (f"  — {detail}" if detail else ""))

    # ---- fakes: a session with the three async methods the bridge uses ----------
    class _Tool:
        def __init__(self, name): self.name = name

    class _Block:
        def __init__(self, text): self.text = text

    class _CallResult:
        def __init__(self, text): self.content = [_Block(text)]; self.structuredContent = None

    class _FakeSession:
        def __init__(self): self.calls = []
        async def initialize(self): pass
        async def list_tools(self):
            class _R: tools = [_Tool("send_show_command")]
            return _R()
        async def call_tool(self, name, args):
            self.calls.append((name, args))
            return _CallResult(f"[{args.get('device_name')}] {args.get('command')} -> ok")

    made = {"n": 0}
    fake = _FakeSession()

    @asynccontextmanager
    async def fake_opener():
        made["n"] += 1
        yield fake

    print("lazy-open: constructing the bridge does NOT open a session")
    b = McpsshBridge("http://unused", opener=fake_opener)
    check("no session before ensure_open (nothing contacted)", not b.is_open and made["n"] == 0)

    print("\nensure_open opens once and publishes tool names")
    b.ensure_open(timeout=5)
    check("session open after ensure_open", b.is_open)
    check("opener ran exactly once", made["n"] == 1, f"n={made['n']}")
    check("tool names came back", b.tool_names() == ["send_show_command"], str(b.tool_names()))

    print("\nre-ensure_open is a no-op (tab-lived: handshake paid once)")
    b.ensure_open(timeout=5)
    check("opener still ran only once across re-ensure", made["n"] == 1, f"n={made['n']}")

    print("\ncall_tool runs synchronously across the loop boundary")
    out = b.call_tool("send_show_command",
                      {"device_name": "eng-peer-1", "command": "show version"})
    check("got the flattened result dict", out.get("output", "").endswith("-> ok"), str(out))
    check("the fake session actually received the call", len(fake.calls) == 1, str(fake.calls))

    print("\nclose tears the loop-thread down cleanly")
    b.close()
    check("thread joined / not alive", b._thread is not None and not b._thread.is_alive())
    check("call after close is an envelope, not a crash",
          b.call_tool("send_show_command", {})["outcome"] == "error")

    print("\na failing opener surfaces as an ensure_open error, not a hang")
    @asynccontextmanager
    async def bad_opener():
        raise ConnectionRefusedError("mcpssh down")
        yield  # unreachable
    b2 = McpsshBridge("http://unused", opener=bad_opener, connect_timeout=3)
    raised = ""
    try:
        b2.ensure_open()
    except Exception as e:
        raised = f"{type(e).__name__}: {e}"
    check("dead transport -> ensure_open RAISES (so _approve can deny)",
          "ConnectionRefusedError" in raised, raised)

    print()
    print(f"  {'all bridge assertions held' if not fails else str(fails) + ' FAILED'}")
    raise SystemExit(1 if fails else 0)
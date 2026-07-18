/* ════════════════════════════════════════════════════════════════════════
   hud/widgets/interfaces.js — port status. PRESENT | UNREAD, never ABSENT.

   payload = the interfaceStatuses DICT, keyed by ifname (payload=statuses).
   Dict-walk: the key IS the interface name, preserved (no .values() drop).
   Fields read off the real interfaceStatuses shape: linkStatus ·
   lineProtocolStatus? · description · bandwidth · interfaceType. (bandwidth and
   interfaceType are in the status read itself — SPEED/TYPE need no cross-cap
   join, so the widget stays single-capability.)

   Frame "interfaces faulted" ceiling 0 — value is the ENGINE's, rendered ABOVE
   the tabs so it is the always-visible fault sentinel even when the ACTIVE view
   filters spare ports out. JS never re-thresholds.

   Two views via the shared tabs primitive, matching the nethuds look:
     • ACTIVE — connected ports only (cuts spare-port noise): status dot, bold
       name, dimmed description, SPEED (fmtBw), TYPE (interfaceType). A
       connected-but-protocol-down port shows here with a red blinking dot;
       errdisabled ports aren't "connected" so they live in ALL + the frame.
     • ALL — every port: link · proto · description, faults tinted.

   Row fault tint echoes reading.py (disabled=admin context, connected+proto-
   down=fault, errdis*=fault, bare not-connected=context) for presentation only;
   the frame value is the engine's truth.
   ════════════════════════════════════════════════════════════════════════ */

const IFACE_UP = 'connected', IFACE_ADMIN = 'disabled', IFACE_FAULT_TOK = 'errdis';

function ifaceClass(s){
  const link  = String(s.linkStatus || '').toLowerCase();
  const proto = String(s.lineProtocolStatus || '').toLowerCase();
  if (link === IFACE_ADMIN)            return 'admin';
  if (link === IFACE_UP)               return proto === 'down' ? 'fault' : 'up';
  if (link.includes(IFACE_FAULT_TOK))  return 'fault';
  return 'down';                       /* not-connected: context, not fault */
}

WIDGETS.interfaces = function(payload, frames){
  const st   = (payload && typeof payload === 'object') ? payload : {};
  const rows = Object.entries(st).map(([name, s]) => ({ name, ...(s || {}), _cls: ifaceClass(s || {}) }));

  const up    = rows.filter(r => r._cls === 'up').length;
  const admin = rows.filter(r => r._cls === 'admin').length;
  const fault = (frames && frames[0] && typeof frames[0].value === 'number')
                  ? frames[0].value
                  : rows.filter(r => r._cls === 'fault').length;

  /* ACTIVE: connected ports, numeric-sorted — the nethuds rich view. */
  const active = rows
    .filter(r => String(r.linkStatus || '').toLowerCase() === IFACE_UP)
    .sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));

  const activeTable = table(
    [{ h: 'PORT', w: '1%', cell: r => `<span class="if-port">${dot(r._cls !== 'fault', r._cls === 'fault')}<b>${esc(r.name)}</b></span>` },
     { h: 'DESCRIPTION', cell: r => `<span class="if-desc">${esc(r.description || '—')}</span>` },
     { h: 'SPEED', r: 1, w: '1%', cell: r => esc(fmtBw(r.bandwidth)) },
     { h: 'TYPE',  r: 1, w: '1%', cell: r => `<span class="if-type">${esc(r.interfaceType || '—')}</span>` }],
    active.map(r => ({ ...r, _crit: r._cls === 'fault' })));

  const allTable = table(
    [{ h: 'PORT',  w: '1%', cell: r => esc(r.name) },
     { h: 'LINK',  w: '1%', cell: r => `<span class="pstate ${r._cls === 'up' ? 'up' : ''}">${esc(r.linkStatus ?? '—')}</span>` },
     { h: 'PROTO', w: '1%', cell: r => esc(r.lineProtocolStatus ?? '—') },
     { h: 'DESCRIPTION', cell: r => esc(r.description || '—') }],
    rows.map(r => ({ ...r, _crit: r._cls === 'fault' })));

  const meta = `${rows.length} ports · ${up} up · ${admin} admin · `
             + `<b class="${fault ? 'crit' : ''}">${fault}</b> faulted`;

  return (frames || []).map(frameBar).join('')
    + tabBar('interfaces', [['active', `● active ${active.length}`], ['all', '▤ all']], meta)
    + pane('interfaces', 'active', activeTable)
    + pane('interfaces', 'all', allTable);
};
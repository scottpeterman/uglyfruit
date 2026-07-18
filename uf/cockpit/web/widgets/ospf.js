/* ════════════════════════════════════════════════════════════════════════
   hud/widgets/ospf.js — OSPF adjacencies. PRESENT | ABSENT | UNREAD.

   payload = the CONVERGED contract list (both vendors translate INTO it).
   Required, §4: routerId · adjacencyState ('full' == up) · interfaceName.
   Framed: `adjacencies not Full` (ceiling 0) — the frame comes from the
   engine; the widget renders it with frameBar and never re-thresholds.

   ── Extras: the converged vocabulary (Note 07 debt 3) ─────────────────────
   Rendered when present, dashed when not — a widget never fabricates a field
   to look complete (Principle 6):

       neighborAddress   the neighbor's interface address
       priority          DR-election priority (0 == never-DR)
       drState           DR | BDR | DROther — EOS reports it directly; Junos
                         DERIVES it (translator compares neighbor-address to
                         the detail-read's dr/bdr election markers). Absent on
                         a Junos brief read and on p2p links: no claim, dash.
       area              OSPF area
       upTime            adjacency age. CONVERGED SEMANTIC: duration
                         SECONDS as a number (EOS derives now-stateTime —
                         stateTime is an epoch, live-confirmed; Junos reads
                         the junos:seconds attribute). Numbers render via
                         fmtUptime; a string fallback renders raw.

   ── Layout: stacked cells ─────────────────────────────────────────────────
   The ospf panel is NOT span2, so completeness comes from two-line cells
   (primary + dim .sub) rather than more columns:

       NEIGHBOR  routerId / neighborAddress
       IFACE     interfaceName / area
       ROLE      drState / pri N
       STATE     adjacencyState / up <time>

   A sub-line renders ONLY when its field exists — required-only payloads
   (today's brief reads) collapse to clean single-line rows, and the table
   fills in as translator lanes deepen. Zero vendor conditionals here.
   ════════════════════════════════════════════════════════════════════════ */

WIDGETS.ospf = function(payload, frames){
  const up = n => String(n.adjacencyState || '').toLowerCase() === 'full';
  const down = payload.filter(n => !up(n)).length;
  const stk = (main, sub) => sub ? `${main}<span class="sub">${sub}</span>` : main;
  const roles = payload.filter(n => n.drState).length;

  return frames.map(frameBar).join('') +
    `<div class="summary-line"><b>${payload.length}</b> adjacencies &nbsp;
      <b class="${down ? 'crit' : ''}">${down}</b> not Full${
      roles ? ` &nbsp;<span style="color:var(--pri-dim)">· roles on ${roles}</span>` : ''}</div>` +
    table(
      [{h:'NEIGHBOR', cell: n => stk(esc(n.routerId), esc(n.neighborAddress ?? ''))},
       {h:'IFACE',    cell: n => stk(esc(n.interfaceName),
                                     n.area != null ? `area ${esc(n.area)}` : '')},
       {h:'ROLE',     cell: n => stk(esc(n.drState ?? '—'),
                                     n.priority != null ? `pri ${esc(n.priority)}` : '')},
       {h:'STATE', r:1, cell: n => stk(
           `<span class="pstate ${up(n) ? 'up' : ''}">${esc(n.adjacencyState)}</span>`,
           n.upTime != null
             ? `up ${esc(typeof n.upTime === 'number' ? (fmtUptime(n.upTime) ?? n.upTime) : n.upTime)}`
             : '')}],
      payload.map(n => ({...n, _crit: !up(n)})));
};

/* Requires (shell-owned, like every widget): esc, table, frameBar, fmtUptime
   (the nameplate's seconds->'41d 3h' humanizer), and the .tbl .sub CSS rule:
     .tbl .sub{display:block;font-size:9px;color:var(--pri-dim);opacity:.8;
               margin-top:1px;letter-spacing:.03em}
     .tbl tr.crit .sub{color:var(--red);opacity:.65}                        */
/* ════════════════════════════════════════════════════════════════════════
   hud/widgets/routes.js — RIB summary. PRESENT | UNREAD, never ABSENT.

   payload = vrfs.default (the discriminator returns `default`, NOT the whole
   routes_value). Field names are the ones arista_routes reads off
   `show ip route summary | json` — confirmed against real vEOS 4.33
   (eng-spine-1):
     totalRoutes · connected · attached · internal · static (+ staticNexthopGroup)
     ospfCounts{} · ospfv3Counts{} · bgpCounts{} · isisCounts{}  (see ribSum)
     rip · gribi · aggregate · vcs  (rendered only when nonzero)
     maskLen{}  (mask -> count)

   ── Two things the *Total-less fixtures hid, and real gear exposed ──────────
   1. The *Counts dicts carry BOTH a `<proto>Total` subtotal AND the component
      breakdown in the SAME dict (ospfCounts: ospfTotal 13 + ospfIntraArea 12 +
      ospfExternal2 1). Summing the whole dict double-counts (26, not 13). ribSum
      prefers the *Total, mirroring reading._sum_counts — engine and panel must
      agree or the rail and the grid disagree.
   2. The source counts are NOT a partition of totalRoutes. connected/attached/
      internal/ospf overlap (attached /32s live under connected subnets, etc.),
      so Σ(sources) ≠ totalRoutes in general. The widget therefore does NOT draw
      a proportion-of-total stack over the sources (that oversubscribes and
      lies); it shows each source as an independent magnitude bar, keeps TOTAL as
      the one authoritative figure, and DISCLOSES the overlap when Σ ≠ total.
      maskLen IS a true partition (it sums to totalRoutes), so the honest stacked
      bar lives in the PREFIXES pane.

   Frame "empty routing table" ceiling 0 — a DEGENERATE (zero-route) RIB is the
   only honest fault; a populated RIB frames 0/OK. Engine's value, pinned ABOVE
   the tabs. JS never re-thresholds. connected-only is the discriminator's SOFT
   signal (frame stays 0/OK) -> a faint note, never a red.

   Helpers are rib* / RIB_* namespaced (concatenated with environment.js, which
   owns `_num`/`dot`).
   ════════════════════════════════════════════════════════════════════════ */

const ribInt = v => {
  const n = (typeof v === 'number') ? v : (v != null && !isNaN(+v) ? +v : 0);
  return n > 0 ? Math.round(n) : 0;
};
/* Total routes from an EOS *Counts dict. EOS puts a `<proto>Total` subtotal in
   the SAME dict as its component breakdown, so summing everything double-counts.
   Prefer the *Total; fall back to summing components only when there's none.
   Mirrors reading._sum_counts exactly. */
const ribSum = d => {
  if (!d || typeof d !== 'object') return 0;
  for (const k of Object.keys(d))
    if (k.endsWith('Total') && typeof d[k] === 'number') return Math.round(d[k]);
  return Math.round(Object.values(d).reduce((s, v) => s + (typeof v === 'number' ? v : 0), 0));
};

/* canonical source order. core:true rows always show (dimmed at 0); the rest
   surface only when nonzero, so an unusual source is never hidden but the
   common view stays clean. static folds staticNexthopGroup; the static* /
   Persistent sub-types are breakdowns of `static` and are NOT re-added. */
const RIB_SOURCES = [
  { key: 'connected', label: 'connected', core: true,  get: d => ribInt(d.connected) },
  { key: 'attached',  label: 'attached',  core: true,  get: d => ribInt(d.attached) },
  { key: 'ospf',      label: 'ospf',      core: true,  get: d => ribSum(d.ospfCounts) },
  { key: 'ospfv3',    label: 'ospfv3',    core: false, get: d => ribSum(d.ospfv3Counts) },
  { key: 'bgp',       label: 'bgp',       core: true,  get: d => ribSum(d.bgpCounts) },
  { key: 'isis',      label: 'isis',      core: false, get: d => ribSum(d.isisCounts) },
  { key: 'internal',  label: 'internal',  core: true,  get: d => ribInt(d.internal) },
  { key: 'static',    label: 'static',    core: true,  get: d => ribInt(d.static) + ribInt(d.staticNexthopGroup) },
  { key: 'rip',       label: 'rip',       core: false, get: d => ribInt(d.rip) },
  { key: 'gribi',     label: 'gribi',     core: false, get: d => ribInt(d.gribi) },
  { key: 'aggregate', label: 'aggregate', core: false, get: d => ribInt(d.aggregate) },
  { key: 'vcs',       label: 'vcs',       core: false, get: d => ribInt(d.vcs) },
];

function ribComposition(d, total){
  const rows = RIB_SOURCES.map(s => ({ label: s.label, core: s.core, n: s.get(d) }))
    .filter(s => s.core || s.n > 0);
  const mx = Math.max(1, ...rows.map(r => r.n));
  const shownSum = rows.reduce((a, r) => a + r.n, 0);
  const learned = ribSum(d.ospfCounts) + ribSum(d.ospfv3Counts) + ribSum(d.bgpCounts)
                + ribSum(d.isisCounts) + ribInt(d.internal);

  const body = rows.map(r => `<div class="rib-row${r.n > 0 ? '' : ' rib-zero'}">
      <span class="rib-label">${esc(r.label)}</span>
      <span class="rib-track"><span class="rib-fill" style="width:${(r.n / mx * 100).toFixed(1)}%"></span></span>
      <span class="rib-count${r.n > 0 ? '' : ' rib-dim'}">${r.n.toLocaleString()}</span>
    </div>`).join('');

  const totalLine = `<div class="rib-total"><span class="rib-dim">TOTAL</span> <b>${total.toLocaleString()}</b> routes</div>`;

  /* honest disclosure — source counters overlap; they need not sum to total. */
  const overlap = (shownSum !== total)
    ? `<div class="rib-note">sources shown sum to ${shownSum.toLocaleString()}, not ${total.toLocaleString()} — EOS route-summary counters overlap. The prefix table is the true partition.</div>`
    : '';
  const soft = (learned === 0 && total > 0)
    ? `<div class="rib-note">connected-only — no learned routes (soft signal, not a fault)</div>`
    : '';

  return `<div class="rib-rows">${body}</div>${totalLine}${overlap}${soft}`;
}

function ribPrefixes(masks, total){
  const rows = Object.entries((masks && typeof masks === 'object') ? masks : {})
    .map(([m, c]) => [String(m), ribInt(c)])
    .filter(([, c]) => c > 0)
    .sort((a, b) => (+a[0]) - (+b[0]));          /* CIDR order — distribution shape */
  if (!rows.length) return '<div class="rib-empty">no prefix-length distribution reported</div>';
  const sum = rows.reduce((a, [, c]) => a + c, 0);
  const mx = Math.max(...rows.map(([, c]) => c));

  /* maskLen IS a true partition of totalRoutes, so a proportion-of-Σ stacked bar
     is honest here (unlike the source view). Alternating opacity keeps adjacent
     segments legible without a categorical color claim. */
  const stack = `<div class="rib-stack">` + rows.map(([m, c], i) =>
    `<span class="rib-seg" style="width:${(c / sum * 100).toFixed(2)}%;opacity:${i % 2 ? 0.5 : 0.72}" title="/${esc(m)}: ${c}"></span>`).join('') + `</div>`;

  const body = rows.map(([m, c]) => `<div class="rib-prow">
      <span class="rib-plabel">/${esc(m)}</span>
      <span class="rib-track"><span class="rib-fill" style="width:${(c / mx * 100).toFixed(1)}%"></span></span>
      <span class="rib-count">${c.toLocaleString()}</span>
    </div>`).join('');

  const recon = `<div class="rib-recon"><span class="rib-dim">Σ</span> <b>${sum.toLocaleString()}</b> `
    + (sum === total ? `<span class="rib-ok">= total ✓</span>`
                     : `<span class="rib-dim">(total ${total.toLocaleString()})</span>`) + `</div>`;

  return stack + `<div class="rib-pfx">${body}</div>` + recon;
}

WIDGETS.routes = function(payload, frames){
  const d = (payload && typeof payload === 'object') ? payload : {};
  const total = ribInt(d.totalRoutes);
  const meta = `${total.toLocaleString()} routes`;

  return (frames || []).map(frameBar).join('')
    + tabBar('routes', [['composition', '◫ composition'], ['prefixes', '⊞ prefixes']], meta)
    + pane('routes', 'composition', ribComposition(d, total))
    + pane('routes', 'prefixes', ribPrefixes(d.maskLen, total));
};
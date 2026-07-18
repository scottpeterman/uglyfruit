/* ════════════════════════════════════════════════════════════════════════
   hud/widgets/lldp.js — LLDP neighbors. PRESENT | UNREAD, never ABSENT.

   payload = the flat lldpNeighbors list (payload=neighbors). Fields, §4:
   port · neighborDevice · neighborPort · ttl. Legitimately FRAMELESS.

   Two views via the shared tabs primitive: RADAR (topology sonar) and TABLE.

   ── Tiering is a SEAM, not a constant ──────────────────────────────────────
   The archetype bakes in NO hostname convention. By default every neighbor is
   an untiered contact on one ring. A deployment MAY inject:
       LLDP_CLASSIFY = (neighbor) => 'leaf' | 'edge' | 'other';
   to opt into the tiered layout; that classifier lives in deployment config,
   never here. Built: untiered sonar. Wired-but-inert: the tiered layout.
   ════════════════════════════════════════════════════════════════════════ */

let LLDP_CLASSIFY = null;          /* (neighbor) => 'leaf'|'edge'|'other' | null */

const lldpShort = d => (String(d || '').split('.')[0] || '—');
const lldpTier  = n => (LLDP_CLASSIFY ? (LLDP_CLASSIFY(n) || 'other') : null);

/* one labelled contact: link from center, node, caption. */
function lldpContact(s, cx, cy, px, py, opts){
  s.v += `<line x1="${cx}" y1="${cy}" x2="${px.toFixed(1)}" y2="${py.toFixed(1)}" stroke="${opts.link}" stroke-width="${opts.lw}"/>`;
  if (opts.box){
    s.v += `<rect x="${(px - 17).toFixed(1)}" y="${(py - 5).toFixed(1)}" width="34" height="10" fill="var(--bg-panel)" stroke="${opts.stroke}" stroke-width="1" rx="1"/>`;
    s.v += `<text x="${px.toFixed(1)}" y="${(py + 2.5).toFixed(1)}" fill="${opts.stroke}" font-size="5" text-anchor="middle" font-weight="bold">${esc(opts.label)}</text>`;
  } else {
    s.v += `<circle cx="${px.toFixed(1)}" cy="${py.toFixed(1)}" r="${opts.r}" fill="var(--bg-panel)" stroke="${opts.stroke}" stroke-width="1"/>`;
    s.v += `<text x="${px.toFixed(1)}" y="${(py - 6).toFixed(1)}" fill="var(--pri-dim)" font-size="${opts.fs}" text-anchor="middle">${esc(opts.label)}</text>`;
  }
}

function lldpRadar(nei, size){
  if (!nei.length) return '<div class="lldp-empty">— NO LLDP DATA —</div>';
  const cx = size / 2, cy = size / 2, r = size / 2 - 30;
  const center = ((document.querySelector('.dev-id') || {}).textContent || 'THIS')
                   .split('.')[0].slice(0, 9);
  const s = { v: '' };

  s.v += `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" class="lldp-radar">`;
  s.v += `<defs><radialGradient id="lldpRg">`
       + `<stop offset="0%" stop-color="var(--pri)" stop-opacity="0.06"/>`
       + `<stop offset="100%" stop-color="var(--pri)" stop-opacity="0"/></radialGradient></defs>`;
  s.v += `<circle cx="${cx}" cy="${cy}" r="${r}" fill="url(#lldpRg)"/>`;
  for (const sc of [0.4, 0.7, 1])
    s.v += `<circle cx="${cx}" cy="${cy}" r="${(r * sc).toFixed(1)}" fill="none" stroke="var(--hud-ring)" stroke-width="0.6" stroke-dasharray="2,3"/>`;
  s.v += `<line x1="${cx - r}" y1="${cy}" x2="${cx + r}" y2="${cy}" stroke="var(--hud-ring)" stroke-width="0.4"/>`;
  s.v += `<line x1="${cx}" y1="${cy - r}" x2="${cx}" y2="${cy + r}" stroke="var(--hud-ring)" stroke-width="0.4"/>`;
  const sweep = w => `<line x1="${cx}" y1="${cy}" x2="${cx + r}" y2="${cy}" `
    + `stroke="rgba(46,230,106,${w === 12 ? '.13' : '.32'})" stroke-width="${w}" `
    + `style="transform-origin:${cx}px ${cy}px;animation:radarSweep 6s linear infinite"/>`;
  s.v += sweep(12) + sweep(1);

  if (LLDP_CLASSIFY){
    const edges  = nei.filter(n => lldpTier(n) === 'edge');
    const leafs  = nei.filter(n => lldpTier(n) === 'leaf');
    const others = nei.filter(n => lldpTier(n) !== 'edge' && lldpTier(n) !== 'leaf');
    edges.forEach((n, i) => {
      const a = Math.PI + (i / Math.max(edges.length, 1)) * Math.PI * 0.5 - Math.PI * 0.25;
      lldpContact(s, cx, cy, cx + Math.cos(a) * r * 0.42, cy + Math.sin(a) * r * 0.42,
        { link: 'var(--hud-edge-link)', lw: 1.5, box: true, stroke: 'var(--hud-edge)', label: lldpShort(n.neighborDevice).slice(0, 9) });
    });
    leafs.forEach((n, i) => {
      const a = (i / Math.max(leafs.length, 1)) * Math.PI * 2 - Math.PI / 2;
      lldpContact(s, cx, cy, cx + Math.cos(a) * r * 0.80, cy + Math.sin(a) * r * 0.80,
        { link: 'var(--hud-leaf-link)', lw: 0.8, r: 3.5, fs: 5, stroke: 'var(--pri-mid)', label: lldpShort(n.neighborDevice).slice(0, 8) });
    });
    others.forEach((n, i) => {
      const a = (i / Math.max(others.length, 1)) * Math.PI * 0.5 + Math.PI * 0.70;
      lldpContact(s, cx, cy, cx + Math.cos(a) * r * 0.58, cy + Math.sin(a) * r * 0.58,
        { link: 'color-mix(in srgb, var(--pri-dim) 30%, transparent)', lw: 0.8, r: 3, fs: 4.5, stroke: 'var(--pri-dim)', label: lldpShort(n.neighborDevice).slice(0, 10) });
    });
    s.v += `<text x="${cx}" y="13" fill="var(--pri-dim)" font-size="6" text-anchor="middle" letter-spacing="1">LEAF TIER</text>`;
    s.v += `<text x="${cx}" y="${size - 5}" fill="var(--hud-edge-text)" font-size="6" text-anchor="middle" letter-spacing="1">EDGE TIER</text>`;
  } else {
    nei.forEach((n, i) => {
      const a = (i / nei.length) * Math.PI * 2 - Math.PI / 2;
      lldpContact(s, cx, cy, cx + Math.cos(a) * r * 0.78, cy + Math.sin(a) * r * 0.78,
        { link: 'var(--hud-leaf-link)', lw: 0.8, r: 3.5, fs: 5, stroke: 'var(--pri-mid)', label: lldpShort(n.neighborDevice).slice(0, 9) });
    });
  }

  s.v += `<rect x="${cx - 22}" y="${cy - 9}" width="44" height="18" fill="var(--bg-panel)" stroke="var(--pri)" stroke-width="1.5" rx="2"/>`;
  s.v += `<text x="${cx}" y="${cy + 3}" fill="var(--pri)" font-size="6.5" text-anchor="middle" font-weight="bold">${esc(center)}</text>`;
  s.v += `</svg>`;
  return s.v;
}

/* PRESENT body. frameless — `frames` unused. Tabs via the shared primitive. */
WIDGETS.lldp = function(payload /*, frames */){
  const nei = Array.isArray(payload) ? payload : [];
  let meta;
  if (LLDP_CLASSIFY){
    const edge = nei.filter(n => lldpTier(n) === 'edge').length;
    const leaf = nei.filter(n => lldpTier(n) === 'leaf').length;
    meta = `${edge} edge · ${leaf} leaf · ${nei.length} total`;
  } else {
    meta = `${nei.length} neighbor${nei.length === 1 ? '' : 's'}`;
  }
  return tabBar('lldp', [['radar', '◎ radar'], ['table', '▤ table']], meta)
    + pane('lldp', 'radar', `<div class="lldp-radar-wrap">${lldpRadar(nei, 248)}</div>`)
    + pane('lldp', 'table', table(
        [{ h: 'LOCAL',       cell: n => esc(n.port) },
         { h: 'NEIGHBOR',    cell: n => `<span title="${esc(n.neighborDevice)}">${esc(lldpShort(n.neighborDevice))}</span>` },
         { h: 'REMOTE PORT', cell: n => esc(n.neighborPort) },
         { h: 'TTL', r: 1,   cell: n => esc(n.ttl) }],
        nei));
};
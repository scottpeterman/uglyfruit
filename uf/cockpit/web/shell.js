/* ========================================================================
   hud/shell.js — one widget = shell + 3-state dispatch.
   ONE place decides which body renders, by digest.state. Frames read only on
   PRESENT. Loaded AFTER the widgets (needs WIDGETS populated).
   ======================================================================== */
// NOTE: add routes/version here only if you want them as PANELS. version is a
// nameplate cap (rides the feed, no panel); routes is extracted but not yet
// in TITLES/ORDER — add both together when its layout is proven.
const TITLES = { bgp:'BGP Peering', ospf:'OSPF Adjacencies', transceivers:'Transceivers', proc:'Compute', lldp:'LLDP Topology', interfaces:'Interfaces', environment:'Environment', optics:'Optics DOM' };

/* ════════════════════════════════════════════════════════════════════════
   hud/shell.js — one widget = shell + 3-state dispatch. ONE place decides
   which body renders, by digest.state. Frames are only ever read on PRESENT.
   ════════════════════════════════════════════════════════════════════════ */
function bodyFor(r){
  if (r.state === 'PRESENT')
    return `<div class="panel-body">${WIDGETS[r.key](r.payload, r.frames || [])}</div>`;
  if (r.state === 'ABSENT')
    return `<div class="absent-body">
      <div class="mark">— ${esc(TITLES[r.key])} not present —</div>
      <div class="why">device answered: ${esc(r.reason)}</div>
      <div class="seal">absence positively evidenced · not a read gap</div></div>`;
  return `<div class="unread-body">
      <div class="mark">⚠ NOT READ</div>
      <div class="why">${esc(r.reason)}</div>
      <div class="why" style="color:#7a5b14">distinct from absent: presence unknown, not denied</div></div>`;
}
function renderPanel(r){
  const sc = r.state.toLowerCase();
  return `<div class="panel ${sc==='unread'?'unread':''}${WIDESPAN.has(r.key)?' span2':''}" data-cap="${r.key}">
    <div class="panel-title">${esc(TITLES[r.key])}
      <span class="age">${r.age_s ?? 0}s</span>
      <span class="state ${sc}">${esc(r.state)}</span></div>
    ${bodyFor(r)}
  </div>`;
}
const WIDESPAN = new Set(['bgp','lldp','interfaces','environment','optics']);

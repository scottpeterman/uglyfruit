/* ========================================================================
   hud/primitives.js — render-only shared helpers.
   Loaded FIRST. Declares the WIDGETS registry each widget file attaches to,
   and the helpers widgets/shell/cockpit all call (esc·table·frameBar·fmtBw·
   fmtUptime·dot·tab helpers). fmtUptime lives here, not cockpit: the version
   widget calls it too. Nothing here reads a payload; render-only.
   ======================================================================== */
/* ════════════════════════════════════════════════════════════════════════
   hud/primitives.js — render-only, payload-agnostic where possible.
   ════════════════════════════════════════════════════════════════════════ */
const esc = s => String(s ?? '').replace(/[&<>"]/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const cls = status => ({OK:'', WARN:'warn', CRIT:'crit'}[status] || '');

/* frame-bar — straight from a digest frame {label,value,ceiling,status}.
   ceiling 0 is a "must be zero" frame (down peers/adjacencies): any breach
   fills red. ceiling > 0 is proportional. status is the engine's, not ours. */
function frameBar(f){
  const k = cls(f.status), zero = f.ceiling === 0;
  const pct = zero ? (f.value > 0 ? 100 : 0)
                   : Math.max(0, Math.min(100, (f.value / f.ceiling) * 100));
  const ceilTxt = zero ? '⟂ 0' : `/${f.ceiling}`;
  return `<div class="frame-row${zero?' zero':''}">
    <span class="frame-label">${esc(f.label)}</span>
    <span class="frame-track">
      <span class="frame-fill ${k}" style="width:${pct}%"></span>
      <span class="frame-tick ${k}" style="left:${pct}%"></span></span>
    <span class="frame-val ${k}">${f.value} <span class="ceil">${ceilTxt}</span></span>
  </div>`;
}
const table = (cols, rows) =>
  `<table class="tbl"><thead><tr>${cols.map(c=>`<th class="${c.r?'r':''}"${c.w?` style="width:${c.w};white-space:nowrap"`:''}>${c.h}</th>`).join('')}</tr></thead>
   <tbody>${rows.map(r=>`<tr class="${r._crit?'crit':''}">${cols.map(c=>
     `<td class="${c.r?'r':''}"${c.w?` style="width:${c.w};white-space:nowrap"`:''}>${c.cell ? c.cell(r) : esc(r[c.k])}</td>`).join('')}</tr>`).join('')}</tbody></table>`;

/* ── hud/primitives.js : tabs — graduated from lldp when interfaces became the
   second widget to want a sub-view. Per-cap active view lives in PANE_VIEW so it
   survives the applyFeed re-render; paneTab toggles in place. ── */
const PANE_VIEW = {};
function tabBar(cap, tabs, meta){
  if (!PANE_VIEW[cap]) PANE_VIEW[cap] = tabs[0][0];
  const active = PANE_VIEW[cap];
  return `<div class="tabs">`
    + tabs.map(([id,label]) =>
        `<button data-tab="${id}" class="${id===active?'on':''}" onclick="paneTab('${cap}','${id}')">${label}</button>`).join('')
    + (meta ? `<span class="tabs-meta">${meta}</span>` : '')
    + `</div>`;
}
function pane(cap, id, html){
  return `<div class="pane ${id===PANE_VIEW[cap]?'active':''}" data-pane="${id}">${html}</div>`;
}
function paneTab(cap, which){
  PANE_VIEW[cap] = which;
  const root = document.querySelector('[data-cap="'+cap+'"]');
  if (!root) return;
  root.querySelectorAll('.tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === which));
  root.querySelectorAll('.pane').forEach(p => p.classList.toggle('active', p.dataset.pane === which));
}
const dot = (ok, blink) => `<span class="dot ${ok?'dot-ok':'dot-err'}${blink?' dot-blink':''}"></span>`;
const fmtBw = bw => (!bw || bw <= 0) ? '\u2014'
  : bw >= 1e11 ? '100G' : bw >= 1e10 ? '10G' : bw >= 1e9 ? '1G' : (bw/1e6).toFixed(0)+'M';

/* ════════════════════════════════════════════════════════════════════════
   hud/widgets/*.js — each exports ONLY a PRESENT body. ABSENT/UNREAD are shell.
   Input: (payload, frames). Frames already carry their own status.
   ════════════════════════════════════════════════════════════════════════ */

// the registry every widgets/*.js attaches to (was an inline object literal)
const WIDGETS = {};

// shared: used by cockpit's applyNameplate AND the version widget
function fmtUptime(s){
  if (typeof s !== 'number' || !isFinite(s) || s < 0) return null;
  const d = Math.floor(s/86400), h = Math.floor(s%86400/3600), m = Math.floor(s%3600/60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`;
}

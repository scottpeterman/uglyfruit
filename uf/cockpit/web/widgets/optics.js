/* ════════════════════════════════════════════════════════════════════════
   hud/widgets/optics.js — per-module DOM. PRESENT | UNREAD, never ABSENT.
   VENDOR-SPECIFIC (Note 07 §5): the cap exists only in the juniper manifest;
   this panel renders only where the feed carries the key. No contract — the
   Junos DOM shape is richer than EOS inventory transceivers (the inverted
   asymmetry), and no second shape exists to intersect against.

   payload = {modules: [{name, dom, fault, warn, alarms[], tempC?, volts?,
     <box's own thresholds: tempCritC/tempWarnC/tempLow*, volt*, rx*Dbm,
      tx*Dbm, bias*>,
     lanes?: [{lane, biasMa?, txDbm?, rxDbm?, fault, warn,
               rxAlarm, rxWarn, biasAlarm, biasWarn}]}]}

   THE BAND GAUGE: optics fail LOW as well as high (the classic dying rx),
   so values render as a POSITION between the box's low/high alarm bounds
   with warn ticks both sides — a fill-to-ceiling bar would draw a dying
   lane as "relaxed". Geometry is presentational; COLOR keys only on the
   box's own per-metric flags (rxAlarm/rxWarn etc.) — JS never re-thresholds.
   Enrichments render when present and drop when not (§7): a module without
   DOM (copper/DAC) rides dimmed; a metric without box bounds shows its
   value with no gauge.

   Frame "optic alarms" ceiling 0 — modules the BOX flagged, pinned above
   the tabs. Uses the env CSS primitives (env-srow/track/fill/mark); no new
   stylesheet.
   ════════════════════════════════════════════════════════════════════════ */

const _optN = v => (typeof v === 'number' && isFinite(v) ? v : null);

/* value positioned between the box's low/high alarm bounds; warn ticks at
   the box's warn levels; marker color = the box's flag verdict, passed in. */
function optBand(val, loCrit, hiCrit, loWarn, hiWarn, cls, title){
  const v = _optN(val), lo = _optN(loCrit), hi = _optN(hiCrit);
  if (v == null) return `<span class="env-track" style="visibility:hidden"></span>`;
  if (lo == null || hi == null || hi <= lo)
    return `<span class="env-track" style="visibility:hidden"></span>`;
  const pos = Math.max(0, Math.min(100, (v - lo) / (hi - lo) * 100));
  const tick = x => { const p = _optN(x);
    return p == null ? null : Math.max(0, Math.min(100, (p - lo) / (hi - lo) * 100)); };
  const wl = tick(loWarn), wh = tick(hiWarn);
  return `<span class="env-track" title="${esc(title)} · band ${lo}..${hi} (box alarm bounds)">
    ${wl != null ? `<span class="env-mark" style="left:${wl.toFixed(0)}%"></span>` : ''}
    ${wh != null ? `<span class="env-mark" style="left:${wh.toFixed(0)}%"></span>` : ''}
    <span class="env-fill ${cls}" style="left:${pos.toFixed(1)}%;width:2px;opacity:.95"></span>
  </span>`;
}

function optModules(mods){
  if (!mods.length) return '<div class="env-empty">no optic modules reported</div>';
  return mods.map(m => {
    if (!m.dom)
      return `<div class="env-srow">
        <span class="env-slabel">${esc(m.name)}</span>
        <span class="env-track" style="visibility:hidden"></span>
        <span class="env-sval"><span class="env-crit">no DOM</span></span>
      </div>`;
    const cls = m.fault ? 'crit' : m.warn ? 'warn' : 'ok';
    const t = _optN(m.tempC), v = _optN(m.volts);
    const chips = (m.alarms && m.alarms.length)
      ? `<div class="env-fans"><span class="env-bad">⚠ ${m.alarms.map(esc).join(' · ')}</span></div>` : '';
    return `<div class="env-srow">
      <span class="env-slabel">${dot(!m.fault, m.fault)}${esc(m.name)}</span>
      ${optBand(t, m.tempLowCritC, m.tempCritC, m.tempLowWarnC, m.tempWarnC, cls, `${m.name} temp C`)}
      <span class="env-sval ${cls}">${t != null ? t.toFixed(1) + 'C' : '—'}<span class="env-crit">${v != null ? ' ' + v.toFixed(2) + 'V' : ''}</span></span>
    </div>${chips}`;
  }).join('');
}

function optLanes(mods){
  const rows = [];
  for (const m of mods){
    for (const l of (m.lanes || [])){
      const rxCls = l.rxAlarm ? 'crit' : l.rxWarn ? 'warn' : 'ok';
      const laneCls = l.fault ? 'crit' : l.warn ? 'warn' : 'ok';
      const bias = _optN(l.biasMa);
      rows.push(`<div class="env-srow">
        <span class="env-slabel">${dot(!l.fault, l.fault)}${esc(m.name)} L${esc(l.lane)}</span>
        ${optBand(l.rxDbm, m.rxLowCritDbm, m.rxCritDbm, m.rxLowWarnDbm, m.rxWarnDbm, rxCls, `rx dBm`)}
        <span class="env-sval ${laneCls}">${_optN(l.rxDbm) != null ? l.rxDbm.toFixed(2) : '—'}<span class="env-crit"> rx</span>${_optN(l.txDbm) != null ? ` ${l.txDbm.toFixed(2)}<span class="env-crit"> tx</span>` : ''}${bias != null ? ` ${bias.toFixed(1)}<span class="env-crit"> mA</span>` : ''}</span>
      </div>`);
    }
  }
  return rows.length ? rows.join('') : '<div class="env-empty">no lanes reported</div>';
}

WIDGETS.optics = function(payload, frames){
  const mods = (payload && Array.isArray(payload.modules)) ? payload.modules : [];
  const dom = mods.filter(m => m.dom);
  const lanes = dom.reduce((n, m) => n + (m.lanes ? m.lanes.length : 0), 0);
  const alarmed = mods.filter(m => m.fault).length;
  const meta = `${mods.length} mod · ${lanes} lane · ${alarmed} alarm`;
  return (frames || []).map(frameBar).join('')
    + tabBar('optics', [['lanes', '☄ lanes'], ['modules', '▤ modules']], meta)
    + pane('optics', 'lanes', `<div class="env-scroll">${optLanes(dom)}</div>`)
    + pane('optics', 'modules', `<div class="env-scroll">${optModules(mods)}</div>`);
};
/* ════════════════════════════════════════════════════════════════════════
   hud/widgets/environment.js — hardware health. PRESENT | UNREAD, never ABSENT.

   payload = the RATIFIED environment deep-cap contract (uf/core/contract.py
   "environment" — the first deep cap governed, from the arista×juniper
   intersection). VENDOR-BLIND: this module knows no vendor and no vendor
   status vocabulary.

     sensors[] · fans[] · power[]   — each record {name, status, fault, …}
       status — the box's own word VERBATIM, display only, never interpreted
       fault  — the engine's per-record verdict; the ONLY thing color keys on
       sensors: tempC? critC? warnC?   (tempC absent == not measured, never 0)
       fans:    speedPct? comment?
       power:   model? watts? capacityW? ampsIn? ampsOut?
     top-level extras: ambientC? coolingStatus? tempStatus?

   Enrichments render when present and dash when not (a widget never
   fabricates a field to look complete — §7): EOS carries thresholds and PSU
   electricals so its heatmap gets warn bands and its power tab gets watts;
   Junos env carries neither, so its tiles color on fault alone and its fan
   comments ride as tooltips. Same module, zero conditionals — the extras
   mechanism doing per-vendor UI for free (Note 07 §4).

   Frame "environment faults" ceiling 0 — the ENGINE's count, pinned above the
   tabs, and it is authoritative: box-LEVEL verdicts (a bad tempStatus/
   coolingStatus) count there even though they are no single record's fault.
   JS never re-thresholds; the amber heatmap band is presentational only
   (warnC, or 85% of critC), never a fault.

   Three views via the shared tabs primitive:
     • HEATMAP — measured sensors as tiles; red = engine fault, amber = warn band.
     • SENSORS — labelled rows; a bar when critC exists, plain figure when not.
     • POWER   — PSU rows (state·model·watts·amps as measured) + total draw
                 when watts exist + fan dots (+speed%) + ambient/cooling status.
   ════════════════════════════════════════════════════════════════════════ */

const _envN = v => (typeof v === 'number' && isFinite(v) ? v : null);
const _envBad = s => { const v = String(s ?? 'ok').toLowerCase();
  return !(v === 'ok' || v.endsWith('ok')); };   /* top-level status words only */

/* per-tile verdict — red is the ENGINE's fault flag, never recomputed here;
   amber is a presentational warning band off warnC (or 85% of critC). */
function envThermCell(s){
  const t    = _envN(s.tempC);
  const crit = _envN(s.critC);
  const warn = _envN(s.warnC) ?? (crit != null ? crit * 0.85 : null);
  const hot  = s.fault === true;
  const amber = !hot && t != null && warn != null && t >= warn;
  const oddStatus = s.status && String(s.status).toLowerCase() !== 'ok';
  return {
    t: t != null ? Math.round(t) : '–',
    cls: hot ? 'crit' : amber ? 'warn' : 'ok',
    title: `${s.name || 'sensor'}: ${t ?? '–'}C`
         + (crit != null ? ` / crit ${crit}C` : '')
         + (warn != null ? ` / warn ${Math.round(warn)}C` : '')
         + (oddStatus ? ` [${s.status}]` : ''),
  };
}

function envHeatmap(sensors){
  const meas = sensors.filter(s => _envN(s.tempC) != null);
  if (!meas.length) return '<div class="env-empty">no temperature sensors reported</div>';
  const cells = meas.map(envThermCell);
  const mx = Math.max(...cells.map(c => (typeof c.t === 'number' ? c.t : -Infinity)));
  return `<div class="env-heat">`
    + `<div class="therm-grid">`
    + cells.map(c => `<div class="therm-cell ${c.cls}" title="${esc(c.title)}">${c.t}</div>`).join('')
    + `</div>`
    + `<div class="env-max">peak <b>${isFinite(mx) ? mx : '–'}C</b></div></div>`;
}

function envSensorsList(sensors){
  const meas = sensors.filter(s => _envN(s.tempC) != null);
  if (!meas.length) return '<div class="env-empty">no temperature sensors reported</div>';
  return meas.map(s => {
    const c = envThermCell(s);
    const t = _envN(s.tempC), crit = _envN(s.critC);
    const warn = _envN(s.warnC) ?? (crit != null ? crit * 0.85 : null);
    if (crit == null)
      /* threshold-less vendor: the measurement + the box's word, NO bar — a
         scale the box never declared would be a fabricated reference frame.
         The track stays for column alignment but hides its chrome: an empty
         OUTLINED bar reads as a render failure, not as "no scale". */
      return `<div class="env-srow">
        <span class="env-slabel">${esc(s.name || 'sensor')}</span>
        <span class="env-track" style="visibility:hidden"></span>
        <span class="env-sval ${c.cls}">${t.toFixed(1)}<span class="env-crit"> ${esc(s.status || '')}</span></span>
      </div>`;
    const pct  = Math.max(0, Math.min(100, (t / crit) * 100));
    const wpct = warn != null ? Math.max(0, Math.min(100, (warn / crit) * 100)) : null;
    return `<div class="env-srow">
      <span class="env-slabel">${esc(s.name || 'sensor')}</span>
      <span class="env-track">
        <span class="env-fill ${c.cls}" style="width:${pct.toFixed(0)}%"></span>
        ${wpct != null ? `<span class="env-mark" style="left:${wpct.toFixed(0)}%"></span>` : ''}</span>
      <span class="env-sval ${c.cls}">${t.toFixed(1)}<span class="env-crit">/${crit}</span></span>
    </div>`;
  }).join('');
}

function envPower(power, fans, env){
  let totalW = 0, totalCap = 0, anyW = false;

  const psuRows = power.map(p => {
    const w  = _envN(p.watts), cap = _envN(p.capacityW);
    const ic = _envN(p.ampsIn), oc = _envN(p.ampsOut);
    if (w != null)   { totalW += w; anyW = true; }
    if (cap != null) totalCap += cap;
    const pct = (w != null && cap) ? ` <span class="env-dim">(${Math.round(w / cap * 100)}%)</span>` : '';
    const vv = _envN(p.volts);
    const bits = [];
    if (vv != null) bits.push(`${vv.toFixed(2)}V`);
    if (ic != null) bits.push(`<span class="env-dim">IN</span> ${ic.toFixed(2)}A`);
    if (oc != null) bits.push(`<span class="env-dim">OUT</span> ${oc.toFixed(2)}A`);
    const amps = bits.length ? bits.join(' ')
      : `<span class="env-dim">${esc(p.status || '')}</span>`;
    const mark = p.vacant ? '<span class="env-dim">○ </span>' : dot(!p.fault, false);
    return `<div class="psu-row">
      <span class="psu-id">${mark}${esc(p.name)}</span>
      <span class="psu-model">${esc(p.model || '—')}</span>
      <span class="psu-w">${w != null ? `<b>${w.toFixed(1)}W</b>` : '<b>—</b>'}<span class="env-dim">${cap != null ? `/${cap}W` : ''}${pct}</span></span>
      <span class="psu-a">${amps}</span>
    </div>`;
  }).join('');

  const total = anyW
    ? `<div class="env-total"><span class="env-dim">TOTAL DRAW</span> <b>${totalW.toFixed(1)}W</b>${totalCap ? ` <span class="env-dim">of ${totalCap}W (${(totalW / totalCap * 100).toFixed(1)}%)</span>` : ''}</div>`
    : '';

  const fanDots = fans.map(f => {
    const spd = _envN(f.speedPct);
    const fmark = f.vacant ? '<span class="env-dim">○ </span>' : dot(!f.fault, false);
    return `<span class="fan"${f.comment ? ` title="${esc(f.comment)}"` : ''}>${fmark}${esc(f.name)}${spd != null ? ` ${spd}%` : ''}</span>`;
  }).join('');

  const amb = _envN(env.ambientC);
  return `${psuRows || '<div class="env-empty">no power supplies reported</div>'}${total}`
    + (fanDots ? `<div class="env-fans"><span class="env-dim">FANS</span> ${fanDots}</div>` : '')
    + (amb != null || env.coolingStatus
        ? `<div class="env-amb">${amb != null ? `<span class="env-dim">AMBIENT</span> <b>${amb}C</b>` : ''}`
          + (env.coolingStatus ? ` <span class="env-dim">·</span> <span class="${_envBad(env.coolingStatus) ? 'env-bad' : 'env-dim'}">cooling ${esc(env.coolingStatus)}</span>` : '')
          + `</div>`
        : '');
}

WIDGETS.environment = function(payload, frames){
  const env     = (payload && typeof payload === 'object') ? payload : {};
  const sensors = Array.isArray(env.sensors) ? env.sensors : [];
  const fans    = Array.isArray(env.fans)    ? env.fans    : [];
  const power   = Array.isArray(env.power)   ? env.power   : [];

  const measured = sensors.filter(s => _envN(s.tempC) != null).length;
  const meta = `${power.length} psu · ${measured} sensor · ${fans.length} fan`;

  return (frames || []).map(frameBar).join('')
    + tabBar('environment', [['heatmap', '▦ heatmap'], ['sensors', '▤ sensors'], ['power', '⚡ power']], meta)
    + pane('environment', 'heatmap', envHeatmap(sensors))
    + pane('environment', 'sensors', `<div class="env-scroll">${envSensorsList(sensors)}</div>`)
    + pane('environment', 'power',   envPower(power, fans, env));
};
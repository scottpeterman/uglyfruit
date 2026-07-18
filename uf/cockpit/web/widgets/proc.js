/* ════════════════════════════════════════════════════════════════════════
   hud/widgets/proc.js — COMPUTE. CPU + process table. PRESENT | UNREAD, never
   ABSENT. Ported from the nethuds monolith renderCompute + gaugeRing
   (arista/static/index.html ~L685/587), retinted to the slice palette.

   payload = the raw `show processes top once | json` dict:
     cpuInfo['%Cpu(s)'].{user,system,nice,idle,ioWait,hwIrq,…}  (the top line)
     processes{<pid>:{cmd,cpuPct,residentMem,…}}                 (pid-keyed)
   frames = [ the engine's CPU-utilization frame ] — value=used%, ceiling=100.

   CPU donut is driven by the ENGINE FRAME (fill=value, color=status) — the
   widget renders the frame in a richer primitive than frameBar; it never
   re-thresholds. (That is why there is no frameBar sentinel here: it would be
   the same number twice.)

   ── MEM is a POPULATE SEAM, not a cross-cap read ──────────────────────────
   memTotal/memFree belong to `version` (one cap = one command). The shell
   injects them via setComputeMem() the way role/identity is injected (cf.
   LLDP_CLASSIFY) — the widget reads the injected COMPUTE_MEM, never version's
   payload. version frameless -> the MEM donut is neutral CONTEXT, not a health
   verdict (a mem-pressure frame is a `version` §7 add, not a fabrication here).
   COMPUTE_MEM null (version not read) -> the MEM donut dashes, honestly.
   ════════════════════════════════════════════════════════════════════════ */

let COMPUTE_MEM = null;   /* {total, free} injected from version at the seam */

/* Called by the shell's populate step when version is PRESENT. NOT a widget
   cross-read — mem is version's, handed in the way identity/role is. */
function setComputeMem(versionPayload){
  const v = versionPayload;
  COMPUTE_MEM = (v && typeof v === 'object'
    && typeof v.memTotal === 'number' && typeof v.memFree === 'number' && v.memTotal > 0)
    ? { total: v.memTotal, free: v.memFree } : null;
}

const _num = v => (typeof v === 'number' ? v : (v != null && !isNaN(+v) ? +v : null));
const _statusColor = s => s === 'CRIT' ? 'var(--red)' : s === 'WARN' ? 'var(--amber)' : 'var(--pri)';

/* the top `%Cpu(s)` line — tolerant of the wrapper key and a flat cpuInfo. */
function cpuLine(payload){
  const ci = payload && payload.cpuInfo;
  if (!ci || typeof ci !== 'object') return {};
  const line = (ci['%Cpu(s)'] && typeof ci['%Cpu(s)'] === 'object') ? ci['%Cpu(s)'] : ci;
  return (line && typeof line === 'object') ? line : {};
}

/* the self-sample: `show processes top once` catches ITSELF running, so on an
   idle box the profiler is the busiest process. Filtered at PRESENTATION only —
   the discriminator still emits the honest raw table. */
const _PROC_HIDE = new Set(['top']);

function topProcs(payload, n){
  const procs = (payload && payload.processes && typeof payload.processes === 'object') ? payload.processes : {};
  return Object.entries(procs)
    .map(([pid, p]) => ({ pid, ...(p && typeof p === 'object' ? p : {}) }))
    .filter(p => _num(p.cpuPct) != null)
    .filter(p => p.cmd == null || !_PROC_HIDE.has(String(p.cmd).trim().toLowerCase()))
    .sort((a, b) => (_num(b.cpuPct) ?? 0) - (_num(a.cpuPct) ?? 0))
    .slice(0, n || 5);
}

/* gaugeRing — ported from the monolith (~L587), retinted. Built local; graduate
   to a shared primitive when a second widget wants it (§5). Driven by a value +
   a color var; presentation only, never re-thresholds. */
function gaugeRing(pct, size, colorVar, label){
  const p = Math.max(0, Math.min(100, Number(pct) || 0));
  const r = size / 2 - 3, c = Math.PI * 2 * r, off = c - (p / 100) * c;
  return `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" class="gauge">`
    + `<circle cx="${size/2}" cy="${size/2}" r="${r}" fill="none" stroke="var(--pri-faint)" stroke-width="3"/>`
    + `<circle cx="${size/2}" cy="${size/2}" r="${r}" fill="none" stroke="${colorVar}" stroke-width="3" `
      + `stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${off.toFixed(1)}" `
      + `transform="rotate(-90 ${size/2} ${size/2})" stroke-linecap="round"/>`
    + `<text x="${size/2}" y="${size/2-1}" fill="${colorVar}" font-size="12" text-anchor="middle" font-weight="bold">${p.toFixed(0)}%</text>`
    + `<text x="${size/2}" y="${size/2+9}" fill="var(--pri-dim)" font-size="7" text-anchor="middle" letter-spacing="1">${esc(label)}</text>`
    + `</svg>`;
}

WIDGETS.proc = function(payload, frames){
  const p = (payload && typeof payload === 'object') ? payload : {};
  const f = (frames && frames[0]) ? frames[0] : null;

  /* CPU donut from the engine frame; fall back to cpuInfo only for the value
     (never a JS status) if a frame is somehow absent. */
  const cl = cpuLine(p);
  const cpuPct = f ? (_num(f.value) ?? 0)
                   : (_num(cl.idle) != null ? 100 - _num(cl.idle) : 0);
  const cpuCol = _statusColor(f ? String(f.status) : 'OK');

  /* MEM donut from injected version mem — neutral context (version frameless). */
  const mem = COMPUTE_MEM;
  const memPct = (mem && mem.total > 0) ? (mem.total - mem.free) / mem.total * 100 : null;

  const gauges = `<div class="compute-gauges">`
    + gaugeRing(cpuPct, 56, cpuCol, 'CPU')
    + (memPct != null
        ? gaugeRing(memPct, 56, 'var(--pri)', 'MEM')
        : `<div class="gauge-dash" title="mem not supplied by the version read">MEM<br>—</div>`)
    + `</div>`;

  const brk = `<div class="compute-brk">`
    + `usr ${_num(cl.user) ?? '–'} · sys ${_num(cl.system) ?? '–'} · irq ${_num(cl.hwIrq) ?? '–'}`
    + ` · io ${_num(cl.ioWait) ?? '–'} · nice ${_num(cl.nice) ?? '–'}</div>`;

  const ram = mem
    ? `<div class="compute-ram">RAM <b>${(mem.total/1048576).toFixed(1)}G</b> total · ${(mem.free/1048576).toFixed(1)}G free</div>`
    : `<div class="compute-ram compute-dim">RAM — <span class="compute-faint">(not in version read)</span></div>`;

  const procs = topProcs(p, 5);
  const list = procs.length
    ? table(
        [{ h: 'PROC',        cell: o => `<b>${esc(String(o.cmd ?? '—'))}</b>` },
         { h: 'CPU%', r: 1,  cell: o => (_num(o.cpuPct) ?? 0).toFixed(1) },
         { h: 'RES',  r: 1,  cell: o => esc(String(o.residentMem ?? '–')) },
         { h: 'PID',  r: 1,  cell: o => esc(String(o.pid ?? '–')) }],
        procs)
    : `<div class="compute-dim">no process data</div>`;
  const proclist = `<div class="compute-proc">${list}</div>`;

  return gauges + brk + ram + proclist;
};
/* ════════════════════════════════════════════════════════════════════════
   hud/widgets/version.js — device identity. PRESENT | UNREAD, never ABSENT.

   version is the ONE cap that is NOT a telemetry panel. It is the HEADER
   NAMEPLATE. So this module does NOT register WIDGETS.version and is NOT in the
   grid ORDER — bodyFor never sees it. Instead it exposes:

       applyNameplate(reading)   // reading = {state, payload, reason, age_s, …}

   which the cockpit calls for the `version` key from the same feed the grid
   reads. In the real slice the QWebChannel bridge carries this identity dict
   Python→JS and calls applyNameplate identically — the demo just feeds it from
   CANS. Same contract, different transport (§4).

   payload = the raw `show version | json` dict. Fields (arista_version reads
   modelName/version|internalVersion/memTotal; the rest are rendered defensively
   off real gear, dashed when absent — §7):
     modelName · version | internalVersion · serialNumber · hardwareRevision
     uptime (seconds) | bootupTimestamp (epoch) · memTotal · memFree
     hostname? (usually the resolver record supplies identity; payload wins if
     present)

   IDENTITY vs HEALTH split (§ Principle 6): the hostname/role is IDENTITY from
   the resolver (the bridge populates #np-host); model/EOS/uptime/serial/mem are
   the version READ, which can be UNREAD. So on UNREAD the widget dims the
   *version-derived* line to an explicit "identity unread" marker and NEVER
   shows a stale nameplate — but it does not blank the resolver hostname, which
   is a separate assertion. mem is CONTEXT only (the discriminator carries no
   §7 ceiling), so it renders as a plain figure, never a gauge/threshold.

   Helpers are np* namespaced (concatenated into one <script> with the other
   widgets; a bare `fmtUptime`/`mem` could collide).
   ════════════════════════════════════════════════════════════════════════ */

const npNum = v => (typeof v === 'number' ? v : (v != null && !isNaN(+v) ? +v : null));

/* uptime: EOS carries `uptime` (seconds) on newer builds, `bootupTimestamp`
   (epoch seconds) on others. Prefer uptime; derive from bootup if that's all
   there is; dash if neither. */
function npUptime(v){
  let s = npNum(v.uptime);
  if (s == null){
    const boot = npNum(v.bootupTimestamp);
    if (boot != null) s = (Date.now() / 1000) - boot;
  }
  if (s == null || s < 0) return '\u2014';
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return `${d}d ${h}h ${m}m`;
}

/* memory is CONTEXT (no ceiling). Show total/free + used% as a figure only. */
function npMem(v){
  const mt = npNum(v.memTotal), mf = npNum(v.memFree);
  if (mt == null || !mt || mf == null) return '';
  const g = kb => (kb / 1024 / 1024).toFixed(1) + 'G';
  const usedPct = Math.round((mt - mf) / mt * 100);
  return `<span class="np-dim">MEM</span> ${g(mt)} <span class="np-dim">·</span> ${usedPct}% used`;
}

/* fill the header identity region for the version reading. */
function applyNameplate(r){
  const meta = document.getElementById('nameplate');
  const host = document.getElementById('np-host');
  if (!meta) return;                          /* header without a nameplate seam — nothing to do */
  meta.classList.remove('np-unread');

  if (!r || r.state !== 'PRESENT' || !r.payload || typeof r.payload !== 'object'){
    /* UNREAD (never ABSENT): do NOT show a stale/fabricated nameplate. Dim to
       an explicit marker. Resolver hostname (#np-host) is left as-is — that is
       a separate identity assertion, not this read. */
    meta.classList.add('np-unread');
    meta.innerHTML = `<span class="np-mark">\u26a0 identity unread</span>`
      + `<span class="np-why">${esc((r && r.reason) || 'version read failed')}</span>`;
    return;
  }

  const v = r.payload;
  if (host && v.hostname) host.textContent = String(v.hostname).toUpperCase();

  const model  = v.modelName || '\u2014';
  const eos    = v.version || v.internalVersion || '\u2014';
  const serial = v.serialNumber || '\u2014';
  const hwRev  = v.hardwareRevision || '';
  const up     = npUptime(v);
  const mem    = npMem(v);

  meta.innerHTML =
      `<span class="np-model">${esc(model)}</span>`
    + `<span class="np-eos"><span class="np-dim">EOS</span> ${esc(eos)}</span>`
    + `<span class="np-field"><span class="np-dim">S/N</span> ${esc(serial)}</span>`
    + (hwRev ? `<span class="np-field"><span class="np-dim">HW</span> ${esc(hwRev)}</span>` : '')
    + `<span class="np-field"><span class="np-dim">UP</span> ${up}</span>`
    + (mem ? `<span class="np-field">${mem}</span>` : '');
}
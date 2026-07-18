/* ════════════════════════════════════════════════════════════════════════
   hud/widgets/transceivers.js — optic inventory. PRESENT | UNREAD, never ABSENT.

   Ported from the nethuds monolith renderTransceiverInventory
   (arista/static/index.html ~L760): the SLOT STRIP + SLOT/MODEL/SERIAL/MFG grid,
   retinted to the slice palette (--pri2→--pri-mid, --pri3/--dim→--pri-dim,
   --dimmer→--pri-faint; Courier→var(--mono)).

   FRAMELESS — an inventory read carries no box-asserted fault. A slot reading
   'Not Present' is EMPTY, not failed, so there is nothing to threshold; framing
   an empty slot would be the fabricated alarm the engine law forbids. (Same
   posture as lldp/version.) Optical HEALTH (rx dBm, module temp) lives in a
   SEPARATE measurement read — `show interfaces transceiver | json` — which, if
   that lane lands, carries its own frame and folds in here as a second sub-read
   exactly the way power/temperature/cooling combine in environment.

   payload = xcvrSlots — the `show inventory | json` sub-object, keyed by slot #:
     { "1": {mfgName, modelName, serialNum}, "2": {mfgName:'Not Present'}, … }
   `mfgName === 'Not Present'` marks an empty slot. populated/total are DERIVED
   here (one cap, no cross-join — §7). Reads defensively: dash on an absent
   field, never fabricate one the inventory didn't carry, never throw on a
   shape it didn't expect (a widget bug must gray one panel, not the feed).
   ════════════════════════════════════════════════════════════════════════ */

const _xcvrEmpty = x => !x || typeof x !== 'object'
  || !x.mfgName || String(x.mfgName).toLowerCase() === 'not present';

/* Accept the raw xcvrSlots dict OR a {xcvrSlots:…} wrapper OR nothing → {}.
   Keeps the widget robust to whether the discriminator emits the sub-object
   raw (it does) or wrapped, and to a malformed feed. */
function xcvrSlots(payload){
  if (!payload || typeof payload !== 'object') return {};
  const raw = (payload.xcvrSlots && typeof payload.xcvrSlots === 'object')
    ? payload.xcvrSlots : payload;
  return (raw && typeof raw === 'object') ? raw : {};
}

/* the slot strip — one tile per slot, active (populated) or empty. */
function xcvrStrip(slots){
  const keys = Object.keys(slots).map(Number).filter(n => !isNaN(n)).sort((a, b) => a - b);
  if (!keys.length) return '';
  return `<div class="xcvr-strip">`
    + keys.map(slot => {
        const x   = slots[slot] || slots[String(slot)] || {};
        const pop = !_xcvrEmpty(x);
        const title = pop
          ? `Slot ${slot}: ${x.modelName || '—'} S/N:${x.serialNum || '—'}`
          : `Slot ${slot}: empty`;
        return `<div class="port-slot ${pop ? 'active' : 'empty'}" title="${esc(title)}">${slot}</div>`;
      }).join('')
    + `</div>`;
}

/* the inventory table — SLOT / MODEL / SERIAL / MFG, populated slots only. */
function xcvrTable(populated){
  return table(
    [{ h: 'SLOT',   w: '1%', cell: o => esc('Et' + o.slot + '/1') },
     { h: 'MODEL',           cell: o => `<span class="xcvr-model">${esc(o.modelName || '—')}</span>` },
     { h: 'SERIAL',          cell: o => `<span class="xcvr-serial">${esc(o.serialNum || '—')}</span>` },
     { h: 'MFG',             cell: o => `<span class="xcvr-mfg">${esc(o.mfgName || '—')}</span>` }],
    populated);
}

WIDGETS.transceivers = function(payload /*, frames — frameless */){
  const slots = xcvrSlots(payload);
  const total = Object.keys(slots).length;

  if (!total) return `<div class="xcvr-empty">— no inventory data —</div>`;

  const populated = Object.entries(slots)
    .filter(([, x]) => !_xcvrEmpty(x))
    .map(([slot, x]) => ({ slot: parseInt(slot, 10), ...x }))
    .sort((a, b) => a.slot - b.slot);

  return `<div class="summary-line">populated <b>${populated.length}</b> / ${total}</div>`
    + xcvrStrip(slots)
    + (populated.length
        ? `<div class="xcvr-scroll">${xcvrTable(populated)}</div>`
        : `<div class="xcvr-empty">all slots empty</div>`);
};
/* ========================================================================
   hud/cockpit.js — applyFeed(feed) patches per-cap containers in place.
   The seam the real transport calls. ORDER lives here (production render
   list), NOT the demo. applyNameplate is NOT here — version.js owns it (it is
   the version cap's renderer). Loaded LAST of the hud set, after version.js.
   ======================================================================== */
// render order of PANELS (version omitted — nameplate cap, not a panel)
const ORDER = ['bgp','ospf','lldp','interfaces','environment','optics','transceivers','proc'];

const MOUNT = document.getElementById('telemetry');
// applyFeed patches stable per-cap containers IN PLACE, keyed by the data-cap
// read back from the DOM (not a closure map). Idempotent by construction: a
// cap's panel is REPLACED when present, appended only when absent — so re-entry
// (every SSE poll, a reconnect, a re-eval) can never stack duplicates.
function applyFeed(feed){
  // populate seam (Python→JS injection, cf. §7 nameplate/classifier): route
  // version's mem into COMPUTE_MEM so the proc widget can draw a MEM donut
  // WITHOUT reading version's payload itself. version is a nameplate cap, not a
  // panel — it rides the feed but isn't in ORDER, so no panel renders for it.
  if (typeof setComputeMem === 'function')
    setComputeMem(feed.version && feed.version.state === 'PRESENT' ? feed.version.payload : null);
  applyNameplate(feed.version);   // the nameplate renders from the version READING,
                                  // not the whole feed (r.state/r.payload/r.reason)
  for (const key of ORDER){
    const r = feed[key];
    if (!r) continue;
    const tmp = document.createElement('div');
    tmp.innerHTML = renderPanel(r).trim();
    const fresh = tmp.firstElementChild;
    const existing = MOUNT.querySelector('[data-cap="' + key + '"]');
    if (existing) existing.replaceWith(fresh);
    else MOUNT.appendChild(fresh);
  }
  renderChips(feed);
}
function renderChips(feed){
  // Feed-driven, not ORDER-driven: a vendor polls only its REGISTERED caps
  // (juniper: bgp/ospf/lldp/version today), so ORDER entries absent from the
  // feed are skipped — a missing cap is "not polled here", not a state, and
  // must not throw. version rides the feed but is a nameplate cap, not a chip.
  document.getElementById('chips').innerHTML = ORDER.filter(k => feed[k]).map(k =>
    `<span class="chip ${feed[k].state.toLowerCase()}">${k}</span>`).join('');
}

/* ── nameplate seam (Python→JS injection, cf. §7): the header renders from the
   VERSION cap's PRESENT payload — the same populate-seam pattern as COMPUTE's
   mem. Vendor-blind by the contract names (modelName/version/serialNumber/
   uptime); vendor TRUTHS enrich when their extras exist (hostName,
   reMastership — Junos emits them, EOS simply doesn't, no conditionals).
   Non-PRESENT version leaves the header as served (harness-injected identity)
   rather than blanking it: the nameplate degrades to "who I dialed", never to
   nothing. ── */

// the production feed seam: the PyQt6 telemetry pane exposes a QWebChannel
// bridge and calls window.applyFeed(feed) on each broker_polled push. In the
// browser preview, demo/mock_feed.js calls applyFeed directly instead.
window.applyFeed = applyFeed;
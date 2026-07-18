/* ════════════════════════════════════════════════════════════════════════
   hud/widgets/bgp.js — BGP peering. PRESENT | ABSENT | UNREAD.

   Extracted from the §6 vertical-slice shell literal (where it and ospf were
   hand-written inline as the first proof, each already tagged with the file it
   was meant to become) into its own bolt-on module — so the shell owns only
   chrome and every cap is a widget (§8).

   payload = the peers list the discriminator emits on PRESENT:
       [{**peerDict, "peerAddress": <neighbor-ip>} for k, v in peers.items()]
   i.e. list(peers.values()) with the peers-dict KEY (neighbor IP) re-injected as
   `peerAddress`. arista_bgp does this on the PRESENT return — CONFIRMED in the
   discriminator, no longer the assumption the old inline comment flagged.
   up iff peerState lowercases to 'established'.

   Fields: peerAddress (always, re-injected) · peerState · description? ·
   prefixReceived?  (description/prefixReceived are real EOS summary fields,
   rendered defensively — dashed until a live capture on THIS box confirms them).

   Frame "peers not Established" ceiling 0 — engine's must-be-zero count, pinned
   above the summary. JS never re-thresholds; row tint echoes !up (presentation).
   ════════════════════════════════════════════════════════════════════════ */

const BGP_UP = 'established';   /* peerState lowercases to this iff the session is up */

WIDGETS.bgp = function(payload, frames){
  const peers = Array.isArray(payload) ? payload : [];
  const up = p => String(p.peerState || '').toLowerCase() === BGP_UP;
  const down = peers.filter(p => !up(p)).length;

  return (frames || []).map(frameBar).join('')
    + `<div class="summary-line" style="margin-top:9px">established
        <b>${peers.length - down}</b> &nbsp; not Established
        <b class="${down ? 'crit' : ''}">${down}</b></div>`
    + table(
        [{ h: 'NEIGHBOR', cell: p => esc(p.peerAddress ?? '—') },
         { h: 'DESC',     cell: p => esc(p.description ?? '—') },
         { h: 'STATE',    cell: p => `<span class="pstate ${up(p) ? 'up' : ''}">${esc(p.peerState)}</span>` },
         { h: 'PFX', r: 1, cell: p => esc(p.prefixReceived ?? '—') }],
        peers.map(p => ({ ...p, _crit: !up(p) })));
};
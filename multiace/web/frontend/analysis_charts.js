/* multiACE print-analysis charts - hand-rolled inline SVG (plan §4.3).
 *
 * No charting library: no build step exists, vendor/ is one file
 * (vue.global.prod.js), this ships to printer flash, and the UI has to
 * work with no internet. Inline SVG is also the only approach that
 * inherits the page's own CSS custom properties (`style="fill:var(--accent)"`
 * resolves against whichever theme is active), which matters because of
 * the palette rule below.
 *
 * Palette rule (style.css:15-47): the theme is dark-only, a four-step
 * grey ladder plus one accent, and filament colour is the only vivid
 * thing on screen otherwise. So hue is never the only cue here - every
 * band that needs to be told apart from its neighbour also gets a hatch
 * fill (the same `url(#ac-hatch)` pattern used throughout) or a label.
 *
 * Same pattern as gcode_preview.js: one global, exposing pure functions
 * that take (data, opts) and return an SVG string for `v-html`. Nothing
 * here touches the DOM directly or holds state between calls.
 */
"use strict";

const MultiAceAnalysisCharts = (() => {
  const NS = "http://www.w3.org/2000/svg";

  function esc(s) {
    return String(s === undefined || s === null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // One <defs> block, repeated per chart so each returned string is a
  // fully self-contained SVG (v-html replaces the whole fragment, so a
  // shared <defs> elsewhere in the document cannot be relied on).
  function hatchDefs(id) {
    return `<defs><pattern id="${id}" width="6" height="6"
      patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
      <line x1="0" y1="0" x2="0" y2="6" stroke="rgba(255,255,255,.35)" stroke-width="2"/>
    </pattern></defs>`;
  }

  function fmtClock(ts) {
    if (!ts) return "–";
    const d = new Date(ts * 1000);
    return d.toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
  }

  // ---- 1. Timeline (plan §4.3.1) ---------------------------------------
  // Time-proportional, one row per track. Degenerate cases this has to
  // survive without dividing by zero: a span of 0 (no end yet), a print
  // where one pause is most of the width, and a stitched print whose
  // segment count is known but whose exact break timestamp is not
  // carried in the payload - shown as a labelled note instead of a false
  // precise dashed line.
  function timeline(data, opts) {
    const o = Object.assign({width: 760, rowH: 22}, opts || {});
    const pr = (data && data.print) || {};
    const t0 = pr.first_start, t1 = pr.last_end;
    if (!t0 || !t1 || t1 <= t0) {
      return `<svg width="${o.width}" height="40" viewBox="0 0 ${o.width} 40">
        <text x="8" y="24" fill="var(--muted)" font-size="12">not enough data for a timeline</text>
      </svg>`;
    }
    const span = t1 - t0;
    const x = (t) => ((t - t0) / span) * o.width;
    const events = (data && data.events) || [];

    const pauses = [];
    let openPause = null;
    for (const ev of events) {
      if (ev.kind === "pause") openPause = ev.t;
      else if (ev.kind === "resume" && openPause !== null) {
        pauses.push([openPause, ev.t]); openPause = null;
      }
    }
    if (openPause !== null) pauses.push([openPause, t1]);

    const swaps = events.filter(e => e.kind === "swap")
      .map(e => [e.t - (e.s || 0), e.t]);
    const toolheads = events.filter(e => e.kind === "toolhead_change")
      .map(e => [e.t - (e.s || 0), e.t]);
    const errors = events.filter(e => e.kind === "error");

    const tracks = [
      {label: "Printing", cls: "ac-tl-printing", ivs: [[t0, t1]]},
      {label: "Paused", cls: "ac-tl-paused", ivs: pauses},
      {label: "Filament swap", cls: "ac-tl-swap", ivs: swaps},
      {label: "Toolhead swap", cls: "ac-tl-toolhead", ivs: toolheads, hatch: true},
    ];
    const rowGap = 4;
    const height = tracks.length * (o.rowH + rowGap) + 22;
    const labelW = 110;

    let svg = `<svg width="${o.width}" height="${height}" viewBox="0 0 ${o.width} ${height}"
      xmlns="${NS}" role="img" aria-label="print timeline">`;
    svg += hatchDefs("ac-hatch-tl");
    tracks.forEach((tr, i) => {
      const y = i * (o.rowH + rowGap) + 16;
      svg += `<text x="0" y="${y + o.rowH / 2 + 4}" fill="var(--muted)" font-size="11">${esc(tr.label)}</text>`;
      svg += `<rect x="${labelW}" y="${y}" width="${o.width - labelW}" height="${o.rowH}"
              fill="none" stroke="var(--border)" rx="3"/>`;
      for (const [s, e] of tr.ivs) {
        const bx = labelW + Math.max(0, x(s));
        const bw = Math.max(1.5, Math.min(o.width - labelW, x(e)) - Math.max(0, x(s)));
        svg += `<rect x="${bx.toFixed(1)}" y="${y + 1}" width="${bw.toFixed(1)}" height="${o.rowH - 2}"
                rx="2" class="${tr.cls}"><title>${esc(tr.label)}: ${esc(fmtClock(s))} → ${esc(fmtClock(e))}</title></rect>`;
        if (tr.hatch) {
          svg += `<rect x="${bx.toFixed(1)}" y="${y + 1}" width="${bw.toFixed(1)}" height="${o.rowH - 2}"
                  rx="2" fill="url(#ac-hatch-tl)"/>`;
        }
      }
    });
    // Error ticks above the printing row.
    for (const ev of errors) {
      const ex = labelW + x(ev.t);
      svg += `<line x1="${ex.toFixed(1)}" y1="4" x2="${ex.toFixed(1)}" y2="16"
              class="ac-tl-error-tick"><title>error ${esc(ev.code)}: ${esc(ev.msg || "")}</title></line>`;
    }
    const axisY = height - 6;
    svg += `<text x="${labelW}" y="${axisY}" fill="var(--muted)" font-size="10">${esc(fmtClock(t0))}</text>`;
    svg += `<text x="${o.width}" y="${axisY}" fill="var(--muted)" font-size="10" text-anchor="end">${esc(fmtClock(t1))}</text>`;
    if ((pr.segments || 1) > 1) {
      svg += `<text x="${labelW}" y="${axisY - 12}" fill="var(--warn)" font-size="10">
              ⚠ ${esc(pr.segments)} segments stitched (crash-resumed) - break shown as "Paused/Crash" time, not a precise mark</text>`;
    }
    svg += `</svg>`;
    return svg;
  }

  // ---- 2. Per-head bars (plan §4.3.3) -----------------------------------
  // What localises a dock fault to one head: toolchanges and time, per
  // head, side by side.
  function headBars(data, opts) {
    const o = Object.assign({width: 760, rowH: 20}, opts || {});
    const events = (data && data.events) || [];
    const byHead = new Map();
    const bump = (h, secs) => {
      if (h === undefined || h === null) return;
      if (!byHead.has(h)) byHead.set(h, {swaps: 0, seconds: 0});
      const b = byHead.get(h);
      b.swaps += 1; b.seconds += secs || 0;
    };
    for (const ev of events) {
      if (ev.kind === "swap") bump(ev.head, ev.s);
      else if (ev.kind === "toolhead_change") bump(ev.to, ev.s);
    }
    const heads = [...byHead.keys()].sort((a, b) => a - b);
    if (!heads.length) {
      return `<svg width="${o.width}" height="32"><text x="8" y="20" fill="var(--muted)" font-size="12">no per-head swap data for this print</text></svg>`;
    }
    const maxSeconds = Math.max(1, ...heads.map(h => byHead.get(h).seconds));
    const labelW = 60;
    const height = heads.length * (o.rowH + 4) + 8;
    let svg = `<svg width="${o.width}" height="${height}" viewBox="0 0 ${o.width} ${height}" xmlns="${NS}">`;
    heads.forEach((h, i) => {
      const b = byHead.get(h);
      const y = i * (o.rowH + 4) + 4;
      const w = Math.max(2, (o.width - labelW - 60) * (b.seconds / maxSeconds));
      svg += `<text x="0" y="${y + o.rowH - 5}" fill="var(--fg)" font-size="11">T${esc(h)}</text>`;
      svg += `<rect x="${labelW}" y="${y}" width="${w.toFixed(1)}" height="${o.rowH - 2}" rx="2" class="ac-headbar">
              <title>T${esc(h)}: ${esc(b.swaps)} toolchanges, ${esc(Math.round(b.seconds))}s</title></rect>`;
      svg += `<text x="${labelW + w + 6}" y="${y + o.rowH - 5}" fill="var(--muted)" font-size="10">${esc(b.swaps)}×</text>`;
    });
    svg += `</svg>`;
    return svg;
  }

  // ---- 3. Host-health strip (plan §4.3.4) -------------------------------
  // Only ever called when data.log carries Layer-3 samples - the caller
  // checks availability and renders "not recorded for this print" itself
  // rather than this function doing double duty as its own empty state.
  function healthStrip(data, opts) {
    const o = Object.assign({width: 760, height: 60}, opts || {});
    const log = (data && data.log) || {};
    const samples = log.health_samples || [];
    if (!samples.length) {
      return `<svg width="${o.width}" height="32"><text x="8" y="20" fill="var(--muted)" font-size="12">no host-health samples in the retained log</text></svg>`;
    }
    const ts = samples.map(s => s.t).filter(Boolean);
    const t0 = Math.min(...ts), t1 = Math.max(...ts);
    const span = Math.max(1, t1 - t0);
    const x = (t) => ((t - t0) / span) * o.width;
    const maxBuf = Math.max(0.1, ...samples.map(s => s.buffer_time || 0));
    const y = (v) => o.height - 4 - (Math.min(v, maxBuf) / maxBuf) * (o.height - 20);

    let path = "";
    samples.forEach((s, i) => {
      const cmd = i === 0 ? "M" : "L";
      path += `${cmd}${x(s.t).toFixed(1)},${y(s.buffer_time || 0).toFixed(1)} `;
    });

    let svg = `<svg width="${o.width}" height="${o.height}" viewBox="0 0 ${o.width} ${o.height}" xmlns="${NS}">`;
    svg += `<path d="${path.trim()}" fill="none" class="ac-health-line"/>`;
    for (const s of (log.print_stalls || [])) {
      svg += `<line x1="${x(s.t).toFixed(1)}" y1="${o.height - 16}" x2="${x(s.t).toFixed(1)}" y2="${o.height - 4}"
              class="ac-health-stall-tick"><title>print_stall=${esc(s.count)}</title></line>`;
    }
    for (const h of (log.host_stalls || [])) {
      svg += `<circle cx="${x(h.t).toFixed(1)}" cy="6" r="3" class="ac-health-timer-close">
              <title>Timer too close: ${esc(h.lateness_s)}s late</title></circle>`;
    }
    for (const sd of (log.mcu_shutdowns || [])) {
      svg += `<line x1="${x(sd.t).toFixed(1)}" y1="0" x2="${x(sd.t).toFixed(1)}" y2="${o.height}"
              class="ac-health-shutdown"><title>MCU shutdown: ${esc(sd.reason)}</title></line>`;
    }
    svg += `<text x="2" y="${o.height - (o.height - 20)}" fill="var(--muted)" font-size="9">buffer_time</text>`;
    svg += `</svg>`;
    return svg;
  }

  return {timeline, headBars, healthStrip};
})();

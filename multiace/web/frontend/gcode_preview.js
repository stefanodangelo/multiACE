/* multiACE g-code preview — main-thread glue + Canvas2D renderer.
 *
 * Pairs with gcode_preview_worker.js, which does the parsing off the UI
 * thread. This file owns the worker lifecycle and the drawing; app.js only
 * calls MultiAceGcodePreview.parse() and constructs a Renderer.
 *
 * Deliberately Canvas2D and no library: this file is loaded as a plain
 * script next to app.js (no bundler), and a top-down toolpath view is a
 * few thousand line segments - nothing here needs WebGL.
 */
"use strict";

const MultiAceGcodePreview = (() => {
  let worker = null;

  function ensureWorker() {
    if (!worker) {
      worker = new Worker("gcode_preview_worker.js?v=1");
    }
    return worker;
  }

  // One parse in flight at a time - a second call while the first is still
  // running would cross-talk on the shared worker's onmessage handler.
  let inFlight = null;

  function parse(file, {onProgress} = {}) {
    if (inFlight) {
      return Promise.reject(new Error("a preview parse is already running"));
    }
    const w = ensureWorker();
    inFlight = new Promise((resolve, reject) => {
      const onMsg = (ev) => {
        const msg = ev.data || {};
        if (msg.type === "progress") {
          if (onProgress) onProgress(msg.percent);
          return;
        }
        w.removeEventListener("message", onMsg);
        inFlight = null;
        if (msg.type === "done") resolve(msg.data);
        else reject(new Error(msg.message || "parse failed"));
      };
      w.addEventListener("message", onMsg);
      w.postMessage({type: "parse", file});
    });
    return inFlight;
  }

  class Renderer {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.data = null;
      this.colorForT = () => "#888888";
      this.layer = 0;
      this.cumulative = true;
      this.showTravel = false;
    }

    setData(data, colorForT) {
      this.data = data;
      if (colorForT) this.colorForT = colorForT;
    }
    setLayer(n) { this.layer = n; }
    setCumulative(v) { this.cumulative = v; }
    setShowTravel(v) { this.showTravel = v; }

    // Call after the canvas element's on-screen size changes (dialog
    // resize, layout reflow) - devicePixelRatio-aware so it stays sharp on
    // HiDPI screens without redoing the whole scale math per draw call.
    resize() {
      const c = this.canvas;
      const rect = c.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      const w = Math.max(1, Math.round(rect.width * dpr));
      const h = Math.max(1, Math.round(rect.height * dpr));
      if (c.width !== w || c.height !== h) {
        c.width = w;
        c.height = h;
      }
    }

    draw() {
      const {ctx, canvas, data} = this;
      if (!data) return;
      const W = canvas.width, H = canvas.height;
      ctx.save();
      ctx.setTransform(1, 0, 0, 1, 0, 0);
      ctx.fillStyle = "#0c0c0e";
      ctx.fillRect(0, 0, W, H);

      const {minX, minY, maxX, maxY} = data.bounds;
      const bw = Math.max(1e-6, maxX - minX), bh = Math.max(1e-6, maxY - minY);
      const pad = 0.92;
      const scale = Math.min((W * pad) / bw, (H * pad) / bh);
      const offX = (W - bw * scale) / 2 - minX * scale;
      // Canvas Y grows downward, g-code Y grows upward (toward the back of
      // the bed) - flip so the print reads the same way it prints.
      const offY = H - ((H - bh * scale) / 2 - minY * scale);

      const toCanvas = (x, y) => [x * scale + offX, offY - y * scale];

      const last = Math.max(0, Math.min(this.layer, data.layers.length - 1));
      const from = this.cumulative ? 0 : last;

      ctx.lineCap = "round";
      ctx.lineJoin = "round";

      for (let li = from; li <= last; li++) {
        const layer = data.layers[li];
        if (!layer) continue;
        const isCurrent = li === last;
        const lineWidth = isCurrent ? 1.6 : 1.0;

        if (this.showTravel && layer.travels.length) {
          ctx.strokeStyle = "rgba(255,255,255,0.12)";
          ctx.lineWidth = 0.6;
          ctx.beginPath();
          this._pathSegs(ctx, layer.travels, toCanvas);
          ctx.stroke();
        }

        for (const run of layer.runs) {
          ctx.strokeStyle = isCurrent
            ? this.colorForT(run.t)
            : this._dim(this.colorForT(run.t));
          ctx.lineWidth = lineWidth;
          ctx.beginPath();
          this._pathSegs(ctx, run.segs, toCanvas);
          ctx.stroke();
        }
      }

      // Toolchange markers on the layer being shown - a small ring in the
      // TARGET colour, so a swap's location in the print is visible at a
      // glance without cross-referencing the swim-lane timeline.
      ctx.lineWidth = 1.5;
      for (const tc of data.toolchanges) {
        if (tc.layer !== last) continue;
        const [cx, cy] = toCanvas(tc.x, tc.y);
        ctx.beginPath();
        ctx.arc(cx, cy, 4, 0, Math.PI * 2);
        ctx.strokeStyle = this.colorForT(tc.t);
        ctx.fillStyle = "rgba(0,0,0,0.55)";
        ctx.fill();
        ctx.stroke();
      }

      ctx.restore();
    }

    _pathSegs(ctx, segs, toCanvas) {
      for (let i = 0; i < segs.length; i += 4) {
        const [x0, y0] = toCanvas(segs[i], segs[i + 1]);
        const [x1, y1] = toCanvas(segs[i + 2], segs[i + 3]);
        ctx.moveTo(x0, y0);
        ctx.lineTo(x1, y1);
      }
    }

    _dim(hex) {
      // Ghost past layers to ~35% brightness against the dark background,
      // rather than a flat gray - keeps the colour identifiable (which
      // colour printed where) while still reading as "not the current
      // layer".
      const h = String(hex || "#888888").replace("#", "");
      if (h.length < 6) return "rgba(140,140,140,0.35)";
      const r = parseInt(h.slice(0, 2), 16);
      const g = parseInt(h.slice(2, 4), 16);
      const b = parseInt(h.slice(4, 6), 16);
      return `rgba(${r},${g},${b},0.35)`;
    }
  }

  return {parse, Renderer};
})();

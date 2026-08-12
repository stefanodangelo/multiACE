/* multiACE g-code preview — parser worker.
 *
 * Runs entirely client-side, on the file the user already selected. No
 * upload, no Moonraker, no printer required - the previous design uploaded
 * the file to the printer's own gcodes folder and embedded a URL fragment
 * into that printer's web UI, which needs an actual printer behind it and
 * does nothing on a laptop with no printer attached. This does not.
 *
 * Message contract:
 *   <- {type:"parse", file}
 *   -> {type:"progress", percent}
 *   -> {type:"done", data: {layers, toolchanges, bounds}}
 *   -> {type:"error", message}
 *
 * `layers[i]` = {z, runs: [{t, segs:[x0,y0,x1,y1, ...]}], travels:[x0,y0,x1,y1,...]}
 * `toolchanges` = [{t, x, y, layer}], one per toolchange in file order.
 * `bounds` = {minX, minY, maxX, maxY} over extrusion moves only.
 *
 * Toolchange detection mirrors post_process_virtual_toolheads.parse_toolchanges
 * exactly: the "; Change Tool X -> Tool Y" comment is authoritative (it
 * survives the multiACE rewrite unchanged, so this parses either a raw
 * slicer file or an already-rewritten one), and bare `T<n>` lines are only
 * trusted until the first such comment appears anywhere in the file.
 */
"use strict";

const TOOLCHANGE_RE = /^;\s*Change Tool\s*\d+\s*->\s*Tool\s*(\d+)/i;
const BARE_T_RE = /^T(\d{1,2})\b/;
const LAYER_RE = /^;LAYER_CHANGE\b/i;
const ZCOMMENT_RE = /^;Z:\s*([-\d.]+)/i;
const MOVE_RE = /^G[0-3]\b/i;   // G2/G3 arcs are drawn as a straight chord -
                                // rare in FDM slicer output, and treating
                                // them as a move keeps X/Y/E tracking correct
                                // even though the curve itself is simplified.

function paramNum(line, letter) {
  const m = new RegExp(letter + "(-?[0-9.]+)", "i").exec(line);
  return m ? parseFloat(m[1]) : null;
}

function parseGcode(text, onProgress) {
  const lines = text.split("\n");
  const n = lines.length;

  let x = 0, y = 0, e = 0;
  let absXY = true, absE = true;
  let curT = 0;
  let sawChange = false;
  let layerIdx = 0;

  const layers = [{z: null, runs: [], travels: []}];
  const toolchanges = [];
  const bounds = {minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity};

  function ensureLayer(idx) {
    while (layers.length <= idx) layers.push({z: null, runs: [], travels: []});
    return layers[idx];
  }
  function bound(px, py) {
    if (px < bounds.minX) bounds.minX = px;
    if (px > bounds.maxX) bounds.maxX = px;
    if (py < bounds.minY) bounds.minY = py;
    if (py > bounds.maxY) bounds.maxY = py;
  }

  let lastProgressAt = 0;
  for (let i = 0; i < n; i++) {
    const line = lines[i].trim();
    if (!line) continue;

    let m = TOOLCHANGE_RE.exec(line);
    if (m) {
      sawChange = true;
      curT = parseInt(m[1], 10);
      toolchanges.push({t: curT, x, y, layer: layerIdx});
      continue;
    }

    if (line.charCodeAt(0) === 59 /* ';' */) {
      if (LAYER_RE.test(line)) {
        layerIdx++;
        ensureLayer(layerIdx);
      } else {
        const zm = ZCOMMENT_RE.exec(line);
        if (zm) ensureLayer(layerIdx).z = parseFloat(zm[1]);
      }
      continue;
    }

    if (!sawChange) {
      m = BARE_T_RE.exec(line);
      if (m) {
        curT = parseInt(m[1], 10);
        toolchanges.push({t: curT, x, y, layer: layerIdx});
        continue;
      }
    }

    if (MOVE_RE.test(line)) {
      const nx = paramNum(line, "X");
      const ny = paramNum(line, "Y");
      const nz = paramNum(line, "Z");
      const ne = paramNum(line, "E");

      const x0 = x, y0 = y;
      if (nx !== null) x = absXY ? nx : x + nx;
      if (ny !== null) y = absXY ? ny : y + ny;
      if (nz !== null) {
        const layer = ensureLayer(layerIdx);
        if (layer.z === null) layer.z = absXY ? nz : layer.z + nz;
      }

      let extruding = false;
      if (ne !== null) {
        const e0 = e;
        e = absE ? ne : e + ne;
        extruding = (e - e0) > 1e-5;
      }

      if (nx !== null || ny !== null) {
        const layer = ensureLayer(layerIdx);
        if (extruding) {
          const runs = layer.runs;
          const last = runs.length ? runs[runs.length - 1] : null;
          if (last && last.t === curT) {
            last.segs.push(x0, y0, x, y);
          } else {
            runs.push({t: curT, segs: [x0, y0, x, y]});
          }
          bound(x0, y0); bound(x, y);
        } else {
          layer.travels.push(x0, y0, x, y);
        }
      }
    } else if (/^G90\b/i.test(line)) {
      absXY = true;
    } else if (/^G91\b/i.test(line)) {
      absXY = false;
    } else if (/^M82\b/i.test(line)) {
      absE = true;
    } else if (/^M83\b/i.test(line)) {
      absE = false;
    } else if (/^G92\b/i.test(line)) {
      const ne = paramNum(line, "E");
      if (ne !== null) e = ne;
    }

    if (onProgress && i - lastProgressAt > 20000) {
      lastProgressAt = i;
      onProgress(Math.round((i / n) * 100));
    }
  }

  if (!isFinite(bounds.minX)) {
    // No extrusion moves at all (empty/travel-only file) - a zero-size
    // box would divide by zero when the renderer computes a fit scale.
    bounds.minX = bounds.minY = 0;
    bounds.maxX = bounds.maxY = 1;
  }
  return {layers, toolchanges, bounds};
}

self.onmessage = async (ev) => {
  const msg = ev.data || {};
  if (msg.type !== "parse") return;
  try {
    const text = await msg.file.text();
    const data = parseGcode(text, (pct) => {
      self.postMessage({type: "progress", percent: pct});
    });
    self.postMessage({type: "done", data});
  } catch (err) {
    self.postMessage({
      type: "error",
      message: err && err.message ? err.message : String(err),
    });
  }
};

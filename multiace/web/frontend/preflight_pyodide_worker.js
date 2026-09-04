/* multiACE in-browser preflight — Pyodide worker.
 *
 * Runs the UNMODIFIED Python post-processor + preflight_core in the browser
 * (CPython-WASM via Pyodide), in a Web Worker so the UI thread stays free
 * during the ~20 s parse/rewrite of large files.
 *
 * WHY Pyodide and not a JS port: a JavaScript re-implementation of the matcher
 * (material-strict matching, swap-aware/Belady layout, head-mode pinning,
 * ACE_SWAP_HEAD injection, the structural auto-load anchor) is a SECOND source
 * of truth that silently drifts from the Python the printer backend runs. By
 * loading the same .py here we keep ONE implementation — backend and browser
 * compute byte-identical results, no differ, no drift.
 *
 * Message contract (matches the frontend wiring):
 *   <- {type:"init", pyodideIndexURL, postprocessSrc, coreSrc, swapCostSrc,
 *                        costParams, calibration}
 *   -> {type:"ready"}                                   (or {type:"error"})
 *   <- {type:"analyze", jobId, file, liveSlots, headCtx, spoolPrices,
 *                        costParams, calibration}
 *   -> {type:"analyze-done", jobId, report}             (+ {type:"progress"})
 *   <- {type:"rewrite", jobId, file, liveSlots, headCtx, mode, remapOverride,
 *                        headAssignment, headPlan, costParams, bedMesh, camera}
 *   -> {type:"rewrite-chunk", jobId, chunk}   (Transferable ArrayBuffer, one
 *                        or more, streamed as the output file is read back
 *                        out of MEMFS - never one whole-file JS string)
 *   -> {type:"rewrite-done", jobId, resolvedSlots}      (+ {type:"progress"})
 *   <- {type:"estimate", jobId, mapping, targetIds}
 *   -> {type:"estimate-done", jobId, estimate, timeline}
 *   <- {type:"clear", jobId}        ->  {type:"cleared", jobId}
 *
 * liveSlots / headCtx are produced by the main thread from /multiace/api/state
 * (the printer is still the source of live ACE/slot identity). headCtx =
 * {mode, ace_head, feeders:[{head,material,color}]}.
 */
"use strict";

let pyodide = null;
let ready = false;
let initPromise = null;

// Cache the uploaded File per job so a later "rewrite" reuses it without the
// main thread re-sending the (possibly 100+ MB) blob.
const files = new Map();
const slotsByJob = new Map();
const ctxByJob = new Map();

// The section-1 cost model's inputs. Seeded at init from
// /api/preflight/pysrc so the browser estimate uses the printer's real
// ace.cfg rather than the shipped defaults, then REFRESHED on every
// "analyze"/"rewrite" message (from /api/preflight/livedata, already
// re-fetched there each run) - the worker and its Pyodide runtime outlive
// many preflight runs in one tab, so without this a swap_retract_length /
// swap_purge_length edit in Settings would silently keep using whatever
// was true when the tab first opened the preflight dialog.
let costParams = null;
let calibration = null;

function progress(jobId, stage, percent) {
  self.postMessage({type: "progress", jobId, stage, percent});
}

// Stream a File into a MEMFS path in fixed-size chunks instead of decoding
// the whole thing into one JS string first (file.text() - UTF-16, ~2x the
// on-disk size for ASCII gcode) and then copying THAT into Pyodide's heap
// again. MEMFS only wants raw bytes; the Python side later opens the path
// with encoding="utf-8" and decodes it as one continuous byte stream, so it
// doesn't matter that the write side split the bytes at arbitrary chunk
// boundaries (including mid-multibyte-character) - no TextDecoder/boundary
// handling is needed here, only on the Python side, which already does it
// correctly via its own file decoding.
async function streamFileIntoMemfs(file, path, onFraction) {
  const total = file.size || 0;
  const stream = pyodide.FS.open(path, "w");
  let done = 0;
  try {
    if (typeof file.stream === "function") {
      const reader = file.stream().getReader();
      for (;;) {
        const r = await reader.read();
        if (r.done) break;
        const chunk = r.value;
        if (chunk && chunk.length) {
          pyodide.FS.write(stream, chunk, 0, chunk.length);
          done += chunk.length;
          if (total > 0 && onFraction) onFraction(done / total);
        }
      }
    } else {
      // Fallback for a runtime with no File.stream() - slice-and-read
      // instead of one whole-file text()/arrayBuffer() call.
      const CHUNK = 8 * 1024 * 1024;
      for (let off = 0; off < total; off += CHUNK) {
        const buf = new Uint8Array(
          await file.slice(off, Math.min(off + CHUNK, total)).arrayBuffer());
        pyodide.FS.write(stream, buf, 0, buf.length);
        done += buf.length;
        if (total > 0 && onFraction) onFraction(done / total);
      }
    }
  } finally {
    pyodide.FS.close(stream);
  }
  return done;
}

// The output-side counterpart: read a MEMFS path back out in fixed-size
// chunks, handing each to onChunk, instead of open(path).read() + one big
// json.dumps({"text": ...})/JSON.parse round trip - the pair that used to
// mean the FULL rewritten file was alive as a Python string, a JSON
// string, and (via structured clone) a second JS string all at once.
function readMemfsFileInChunks(path, onChunk) {
  const CHUNK = 4 * 1024 * 1024;
  const total = pyodide.FS.stat(path).size;
  const stream = pyodide.FS.open(path, "r");
  let done = 0;
  try {
    while (done < total) {
      const want = Math.min(CHUNK, total - done);
      const buf = new Uint8Array(want);
      const got = pyodide.FS.read(stream, buf, 0, want, done);
      if (got <= 0) break;
      onChunk(got === want ? buf : buf.subarray(0, got));
      done += got;
    }
  } finally {
    pyodide.FS.close(stream);
  }
  return done;
}

// One-time Pyodide bring-up: load the runtime, then write the unmodified .py
// sources into the in-memory FS and import them. All of them are pure stdlib
// (no pip/micropip needed), so a bare Pyodide can run them as-is.
//
// Adding a module here means adding it to /api/preflight/pysrc too - the two
// lists have to name the same files, or the browser silently runs an older
// shape than the backend.
async function ensureInit(msg) {
  if (ready) return;
  if (!initPromise) {
    initPromise = (async () => {
      const indexURL = msg.pyodideIndexURL ||
        "https://cdn.jsdelivr.net/pyodide/v0.26.2/full/";
      // pyodide.js defines loadPyodide() on the worker global.
      importScripts(indexURL + "pyodide.js");
      pyodide = await self.loadPyodide({indexURL});
      // Drop the two modules onto the FS and import them. preflight_core takes
      // the post-processor module as a parameter, so we only import both and
      // hand pp into the core functions — no cross-import between the files.
      pyodide.FS.mkdirTree("/multiace");
      pyodide.FS.writeFile(
        "/multiace/post_process_virtual_toolheads.py", msg.postprocessSrc);
      pyodide.FS.writeFile("/multiace/preflight_core.py", msg.coreSrc);
      // swap_cost is optional: an install that predates it still gets a
      // working preflight, just without the estimate. preflight_core imports
      // it softly for exactly that reason.
      if (msg.swapCostSrc) {
        pyodide.FS.writeFile("/multiace/swap_cost.py", msg.swapCostSrc);
      }
      costParams = msg.costParams || null;
      calibration = msg.calibration || null;
      pyodide.runPython(`
import sys, json
sys.path.insert(0, "/multiace")
import post_process_virtual_toolheads as _pp
import preflight_core as _core
# Per-job estimate capture (SwapCostModel + header + colours/materials),
# populated by doAnalyze and consumed by a later "estimate" recompute -
# see preflight_core.recompute_estimate.
_captures = {}
`);
      ready = true;
    })();
  }
  await initPromise;
}

// Analyze: meta-parse + build the report (multi or head-mode preview).
async function doAnalyze(jobId, file, liveSlots, headCtx, spoolPrices,
                         freshCostParams, freshCalibration) {
  if (freshCostParams !== undefined) costParams = freshCostParams;
  if (freshCalibration !== undefined) calibration = freshCalibration;
  files.set(jobId, file);
  slotsByJob.set(jobId, liveSlots);
  ctxByJob.set(jobId, headCtx);

  const py = pyodide;
  py.FS.mkdirTree("/preflight");
  const srcPath = "/preflight/analyze.gcode";
  progress(jobId, "analyze", 1);
  await streamFileIntoMemfs(file, srcPath, (frac) => {
    progress(jobId, "analyze", 1 + frac * 34);
  });

  py.globals.set("_live", JSON.stringify(liveSlots || []));
  py.globals.set("_hctx", JSON.stringify(headCtx || {mode: "multi"}));
  py.globals.set("_fname", file.name || "upload.gcode");
  py.globals.set("_fsize", file.size || 0);
  py.globals.set("_cost", JSON.stringify(costParams || {}));
  py.globals.set("_calib", JSON.stringify(calibration || {}));
  py.globals.set("_prices", JSON.stringify(spoolPrices || {}));
  py.globals.set("_job_id", jobId);
  py.globals.set("_srcpath", srcPath);
  progress(jobId, "analyze", 40);
  // parse_meta/build_report only ever touch a small bounded buffer (a
  // couple thousand head/tail lines plus the filtered plan proxy) rather
  // than the raw file - so once ingestion above is done, this phase is
  // fast regardless of file size, and one coarse jump to "done" is
  // honest rather than a lie the way the old fixed 40%->100% jump was
  // while a multi-hundred-MB file was still being duplicated in memory.
  const reportJson = py.runPython(`
_live_slots = json.loads(_live)
_head_ctx   = json.loads(_hctx)
_colors, _types, _naces, _used, _plan, _meta, _hdr = _core.parse_meta(
    _pp, open(_srcpath, "r", encoding="utf-8", errors="replace"),
    with_header=True)
_capture = {}
_report = _core.build_report(
    _pp,
    slicer_colors=_colors, slicer_types=_types, num_aces=_naces,
    plan_proxy=_plan, live_slots=_live_slots, head_ctx=_head_ctx,
    token="", filename=_fname, size=int(_fsize),
    header_text=_hdr, cost_params=json.loads(_cost),
    calibration=json.loads(_calib), meta=_meta,
    spool_prices=json.loads(_prices), estimate_capture=_capture)
_captures[_job_id] = _capture
json.dumps(_report)
`);
  try { py.runPython("del _plan, _hdr, _meta\n"); } catch (e) {}
  try { py.FS.unlink(srcPath); } catch (e) {}
  progress(jobId, "done", 100);
  return JSON.parse(reportJson);
}

// Estimate: recompute {estimate, timeline} for an edited mapping/target
// assignment, reusing the ctx captured by doAnalyze - no gcode re-parse.
async function doEstimate(jobId, mapping, targetIds) {
  const py = pyodide;
  py.globals.set("_job_id", jobId);
  py.globals.set("_mapping", JSON.stringify(mapping || null));
  py.globals.set("_target_ids", JSON.stringify(targetIds || null));
  const resultJson = py.runPython(`
_result = _core.recompute_estimate(
    _captures.get(_job_id),
    mapping=json.loads(_mapping), target_ids=json.loads(_target_ids))
json.dumps(_result)
`);
  return JSON.parse(resultJson);
}

// Rewrite: run the full pipeline in MEMFS, then stream the print-ready
// gcode back out in chunks (postMessage callback, see doRewrite's caller)
// instead of returning it as one JS string.
async function doRewrite(jobId, msg) {
  const file = files.get(jobId) || msg.file;
  const liveSlots = slotsByJob.get(jobId) || msg.liveSlots || [];
  const headCtx = ctxByJob.get(jobId) || msg.headCtx || {mode: "multi"};
  const mode = msg.mode || "slicer";
  if (!file) throw new Error("missing file");
  if (msg.costParams !== undefined) costParams = msg.costParams;

  const py = pyodide;
  py.FS.mkdirTree("/preflight");
  const srcPath = "/preflight/src.gcode";
  progress(jobId, "analyze", 1);
  await streamFileIntoMemfs(file, srcPath, (frac) => {
    progress(jobId, "analyze", 1 + frac * 9);
  });

  py.globals.set("_live", JSON.stringify(liveSlots));
  py.globals.set("_hctx", JSON.stringify(headCtx));
  py.globals.set("_mode", mode);
  // Re-set here rather than trust whatever doAnalyze left behind: the
  // rewrite (purge amount injected into the gcode) must use the SAME
  // fresh config the estimate just showed the user, not a stale copy from
  // whenever this job was last analyzed.
  py.globals.set("_cost", JSON.stringify(costParams || {}));
  py.globals.set("_remap", JSON.stringify(msg.remapOverride || null));
  py.globals.set("_hassign", JSON.stringify(msg.headAssignment || null));
  py.globals.set("_hplan", msg.headPlan || "loadout");
  // The SET_PRINT_PREFERENCES prepend used to be a whole-string JS
  // .replace() applied to the fully-materialized output text; now that the
  // output is streamed back out in chunks (never one JS string), it runs
  // here instead, via the SAME streaming helper the backend uses
  // (preflight_core.prepend_print_prefs) - one implementation, not a
  // second JS-only copy of the same rule.
  py.globals.set("_bed_mesh", !!msg.bedMesh);
  py.globals.set("_camera", !!msg.camera);

  // Bridge the streaming-stage progress out to the main thread. set_stage
  // maps a coarse (stage, percent) within the pipeline's own 0-100 span;
  // rescaled here into the 10-85% band this function reserves for it,
  // since ingestion (above) and output streaming (below) take the rest.
  const onStage = (stage, percent) =>
    progress(jobId, stage, 10 + Math.max(0, Math.min(100, percent)) * 0.75);
  py.globals.set("_on_stage", onStage);

  progress(jobId, "rewrite", 10);
  const metaJson = py.runPython(`
_live_slots = json.loads(_live)
_head_ctx   = json.loads(_hctx)
_remap_ov   = json.loads(_remap)
_hassign_ov = json.loads(_hassign)
_colors, _types, _naces, _used, _plan, _meta = _core.parse_meta(
    _pp, open("/preflight/src.gcode", "r", encoding="utf-8", errors="replace"))
_final, _resolved = _core.rewrite_pipeline(
    _pp,
    src_path="/preflight/src.gcode",
    tmp_a="/preflight/a.gcode", tmp_b="/preflight/b.gcode",
    slicer_colors=_colors, slicer_types=_types, num_aces=_naces,
    live_slots=_live_slots, head_ctx=_head_ctx, mode=_mode,
    remap_override=_remap_ov, head_assignment=_hassign_ov, head_plan=_hplan,
    cost_params=json.loads(_cost),
    meta=_meta,
    set_stage=lambda s, p: _on_stage(s, p))
_out_path = _final
if _bed_mesh or _camera:
    _out_path = "/preflight/prefs.gcode"
    _core.prepend_print_prefs(_final, _out_path, _bed_mesh, _camera)
json.dumps({"out_path": _out_path, "resolved_slots": _resolved})
`);
  const outMeta = JSON.parse(metaJson);
  progress(jobId, "upload", 88);

  let chunkCount = 0;
  readMemfsFileInChunks(outMeta.out_path, (buf) => {
    // Copy out of the read buffer before transferring ownership away -
    // the underlying ArrayBuffer must not be touched again on this side
    // once postMessage's transfer list hands it to the main thread.
    const copy = new Uint8Array(buf.length);
    copy.set(buf);
    chunkCount += 1;
    self.postMessage({type: "rewrite-chunk", jobId, chunk: copy.buffer},
                     [copy.buffer]);
  });

  // tidy MEMFS so a second print doesn't accumulate
  for (const p of ["/preflight/src.gcode", "/preflight/a.gcode",
                    "/preflight/b.gcode", "/preflight/prefs.gcode"]) {
    try { py.FS.unlink(p); } catch (e) {}
  }
  progress(jobId, "done", 100);
  return {chunkCount, resolvedSlots: outMeta.resolved_slots || []};
}

self.onmessage = async (ev) => {
  const msg = ev.data || {};
  const jobId = msg.jobId || "job";
  try {
    if (msg.type === "init") {
      await ensureInit(msg);
      self.postMessage({type: "ready"});
      return;
    }
    if (msg.type === "analyze") {
      await ensureInit(msg);
      const report = await doAnalyze(
        jobId, msg.file, msg.liveSlots, msg.headCtx, msg.spoolPrices,
        msg.costParams, msg.calibration);
      self.postMessage({type: "analyze-done", jobId, report});
      return;
    }
    if (msg.type === "rewrite") {
      await ensureInit(msg);
      const {resolvedSlots} = await doRewrite(jobId, msg);
      self.postMessage({type: "rewrite-done", jobId, resolvedSlots});
      return;
    }
    if (msg.type === "estimate") {
      await ensureInit(msg);
      const r = await doEstimate(jobId, msg.mapping, msg.targetIds);
      self.postMessage({
        type: "estimate-done", jobId,
        estimate: r && r.estimate, timeline: r && r.timeline,
      });
      return;
    }
    if (msg.type === "clear") {
      files.delete(jobId);
      slotsByJob.delete(jobId);
      ctxByJob.delete(jobId);
      if (pyodide) {
        pyodide.globals.set("_job_id", jobId);
        pyodide.runPython(`_captures.pop(_job_id, None)`);
      }
      self.postMessage({type: "cleared", jobId});
      return;
    }
  } catch (err) {
    self.postMessage({
      type: "error", jobId,
      message: err && err.message ? err.message : String(err),
    });
  }
};

# multiACE: Real Estimates, Loadout Planning, History & Push-Updates

**Status:** Draft
**Date:** 2026-08-11
**Scope:** 9 requested items (estimates, offline/mock preflight, loadout planner + gcode-level
editor, order/position-aware purge & swap efficiency, print history, one-click update push,
neighbour-retract on load retry, late ACE detection, live print control, gcode preview pane)
**Predecessor:** [.github/plans/feature-ux-improvements.md](feature-ux-improvements.md) — mostly
landed (mock mode, dev UI, firmware compat, config-diff restarts, auto-retry, console/webcam).

---

## 0. What already exists (verified in tree)

Reading before planning changed the shape of several items — three of the six are partly built:

| Requested | Already in tree | Real gap |
|---|---|---|
| 1. Time/filament estimate | `plan_loadout` swap counts; `head_mode_bg_stats`; hardcoded costs `BG_SWAP_COST_INLINE_S=210`, `BG_SWAP_COST_BG_S=30` ([post_process_virtual_toolheads.py:885-887](../../multiace/tools/post_process_virtual_toolheads.py#L885-L887)), `swaps * 3.8` min in the CLI report ([:1960](../../multiace/tools/post_process_virtual_toolheads.py#L1960)) | No config-derived cost model, no purge/filament mass accounting, nothing surfaced as time/grams/metres in the UI, no M73/metadata rewrite |
| 2. Mock print in UI | `MOCK_MODE` + `_mock_enabled()` + `tests/fixtures/mock_*.json`; full in-browser preflight via Pyodide ([preflight_pyodide_worker.js](../../multiace/web/frontend/preflight_pyodide_worker.js)) | `/api/preflight`, `/api/preflight/livedata`, `/api/preflight/print` are **not** mock-aware (`409 no slots are loaded`); no virtual-loadout editor; no "download rewritten gcode instead of printing" |
| 3. Loadout recommendation | `compute_head_mode_optimize` (bg-aware, `event_times` from M73, Belady/layer variants), three plans, editable `target_id` per colour, rewrite already consumes `head_assignment` | Objective is swap **count**/proxy seconds, not modelled seconds; no per-event timeline the user can reason about or edit |
| 3.1 Order/position-aware purge | Source classes exist implicitly (feeder pin vs ACE slot vs bg head); `BG_UNLOAD_MIN_WINDOW_MIN=1` | Purge is one global `swap_purge_length`; no colour-pair awareness; bg window is a constant, not derived from real swap duration |
| 4. Print history | nothing (only a console backfill from Moonraker's gcode store) | Whole feature |
| 5. Update button | **Done**: `GET /api/update/check`, `POST /api/update/apply` ([main.py:1311-1328](../../multiace/web/backend/main.py#L1311-L1328)), `multiace_update.sh`, UI in the Config tab ([app.js:1971-2032](../../multiace/web/frontend/app.js#L1971-L2032)) | It only installs **published releases**. No path to push *your own working tree*. That is the actual ask |
| 6. Neighbour retract on retry | Retry loop at [ace.py:7403-7504](../../multiace/klipper/extras/ace.py#L7403-L7504); slot-level `self._retract(index, length, speed)` → `unwind_filament` ([ace.py:4950](../../multiace/klipper/extras/ace.py#L4950)) | Retry currently only resets the feed channel; no neighbour clearance |
| 7. ACE powered on after the printer | A **late-join** path already exists in `_refresh_ace_devices` ([ace.py:766-788](../../multiace/klipper/extras/ace.py#L766-L788)) | It is guarded by `if self._ace_canonical is not None:` — precisely the state startup soft-fail never reaches, because it `return`s early. See §7 |
| 8. Live print control | Raw gcode passthrough (`/api/console`, `/api/plugin-api/gcode` [main.py:2951](../../multiace/web/backend/main.py#L2951)); `print_stats` already subscribed ([main.py:189](../../multiace/web/backend/main.py#L189)); sidebar pane machinery ([app.js:3097-3145](../../multiace/web/frontend/app.js#L3097-L3145)) | No readback of `gcode_move` factors, no clamped verb API, no controls UI. See §8 |
| 9. Gcode preview | Upload-to-Moonraker exists but hardcodes `print: "true"` ([main.py:1440](../../multiace/web/backend/main.py#L1440)); `_webcam_base()` already builds the printer's web root ([main.py:2578](../../multiace/web/backend/main.py#L2578)); the Pyodide worker already holds the rewritten text | No preview pane; the printer's own viewer can only show the *slicer's* tools, not multiACE's plan. See §9 |

**Note on item 5 up front:** the `.bin`-on-a-USB-stick flow is *Snapmaker's own firmware*
update and stays out of scope — multiACE cannot replace it. multiACE's own code already
updates over the network; what's missing is pushing **unreleased local changes**, which
§5 covers with an SSH push script plus an optional upload-a-tarball endpoint.

---

## 1. Config-accurate print time & filament estimate

### 1.1 Where the numbers must come from

The slicer's `; estimated printing time` / `M73` are normal-mode: one head, no ACE swap, no
purge. multiACE adds, per mid-print swap:

```
tip-form  +  retract to ACE  +  (ACE spool change)  +  load to nozzle  +  seat/press  +  purge  +  heat settle
```

Every term has a config parameter today ([ace.cfg](../../multiace/config/extended/ace.cfg)):
`feed_speed: 80`, `retract_speed: 80`, `load_length: 2100`, `retract_length: 1950`,
`seat_overshoot_length: 20`, `swap_retract_length: 900`, `swap_purge_length: 0`,
`swap_anti_ooze_retract: 10`, `swap_default_temp: 250`, plus the per-ACE / per-slot
overrides already resolved by `get_retract_length` / `get_swap_retract_length`
([ace.py:1586-1603](../../multiace/klipper/extras/ace.py#L1586-L1603)) and the tip-form block.

### 1.2 New module: `multiace/tools/swap_cost.py`

Pure-stdlib (it must run under Pyodide, and the printer's Python), no imports from `ace.py`.

```python
class SwapCostModel:
    @classmethod
    def from_params(cls, main_params, per_ace_params=None): ...   # ace.cfg scalars
    def swap_seconds(self, kind, *, ace=None, slot=None,
                     from_color=None, to_color=None, material=None): ...
    def purge_mm(self, from_color, to_color, material): ...
```

* `kind ∈ {"feeder_pin", "same_ace", "cross_ace_inline", "cross_ace_bg", "first_load"}` —
  §3.1 explains why the class matters.
* Mechanical terms are computed (`length / speed`, mm/s → s); the terms we cannot compute
  (ACE internal spool change, sensor waits, tool pickup) stay **named constants** seeded
  from today's `BG_SWAP_COST_*` numbers, so day-one output matches current behaviour and
  every later change is a deliberate constant edit.
* **Real swap timings are not measured yet** (open question 2), and the decision is: ship on
  the modelled constants, don't block Phase A on a stopwatch. That makes the accuracy ceiling
  explicit — every estimate carries `confidence: "modelled"` and the UI says *estimated*, never
  *measured*, until §4.3's history calibrates it from this machine. Consequence: the constants
  must be in one named table with a comment saying they are unmeasured guesses, so nobody
  later mistakes them for calibrated values.
* Purge volume → length via nozzle diameter + max volumetric rate for the extrude time.

### 1.3 Filament accounting

**Header format confirmed** (real output from the Snapmaker Orca fork — this settles open
question 1):

```gcode
; filament used [mm] = 293.56, 0.00, 0.00, 0.00
; filament used [cm3] = 0.71, 0.00, 0.00, 0.00
; filament used [g] = 0.88, 0.00, 0.00, 0.00
; filament cost = 0.02, 0.00, 0.00, 0.00
; total filament used [g] = 0.88
; total filament cost = 0.02
; total layers count = 205
; estimated printing time (normal mode) = 8m 30s
; estimated first layer printing time (normal mode) = 19s
```

Consequences for the parser — all four matter, and none of them were the assumed shape:

* **One line per metric, comma-separated per extruder index** — *not* one line per tool. Index
  in the list = slicer tool index, so `[g][2]` is T2. Trailing `0.00` entries are unused
  extruders and must be kept as zeros, not dropped, or tool indices shift.
* `[g]` and `[cm3]` are both present, so the `π·(d/2)²·ρ` fallback is a **fallback only**
  (still implement it — `filament_density`/`filament_diameter` are not in this header, so a
  file without `[g]` has no density to work from either; use the per-material table then).
* `; total filament used [g]` is an independent cross-check: assert
  `sum(per_tool_g) ≈ total_g` and mark the estimate `confidence: "degraded"` on mismatch
  rather than silently trusting one of them.
* `; estimated printing time (normal mode) = 8m 30s` is a **duration string**, not seconds —
  parse `\d+d`/`\d+h`/`\d+m`/`\d+s` in any combination. `; total layers count` gives a free
  sanity bound for the layer-based plan variant; `; estimated first layer printing time` is
  the natural width of the pre-print bg-preload window in §3.1.

Reuse the existing header scan (`parse_color_names` / `parse_filament_types`,
[post_process_virtual_toolheads.py:2343](../../multiace/tools/post_process_virtual_toolheads.py#L2343)).

> Still capture one real 4-colour file into `tests/fixtures/` and drive the parser from it —
> the block above is a 1-colour print, so the multi-value rows are unexercised. Colour/type
> header spellings in the same file are still unconfirmed.

Output block, per plan, added to the preflight report:

```json
"estimate": {
  "base_s": 18240, "added_s": 2530, "total_s": 20770,
  "inline_swaps": 11, "bg_swaps": 7, "unknown_window_swaps": 0,
  "purge": {"mm": 1840, "g": 5.5},
  "per_color": [{"t": 0, "print_mm": 41200, "print_g": 123.1,
                 "purge_mm": 620, "purge_g": 1.9, "total_m": 41.8}],
  "totals": {"mm": 92500, "m": 92.5, "g": 276.4},
  "confidence": "modelled" | "calibrated",
  "assumptions": ["swap_purge_length=0 → purge from colour-pair model", ...]
}
```

`base_s` comes from the slicer metadata; `added_s` from the model; `confidence` flips to
`calibrated` once §4's history has ≥5 completed jobs to regress against.

### 1.4 Surfacing

* `preflight_core.build_report` / `head_mode_preview` attach `estimate` to **each** plan, so
  loadout vs optimize vs layer are comparable in minutes and grams, not swap counts.
* Frontend: an estimate card under the plan table — total time (Δ vs slicer), filament
  total in m and g, waste in g with a percentage, and a per-colour breakdown.
* **Optional, phase 2:** rewrite `M73` R-values and the `; estimated printing time`
  metadata in the output gcode so the printer's own ETA and Moonraker's file metadata are
  right. Gate behind `preflight_rewrite_eta: true` — it touches every M73 line, and a
  mistake here silently corrupts progress reporting for every print.

### 1.5 Tests

`tests/unit/test_swap_cost.py`: term-by-term arithmetic from a known cfg; monotonicity
(longer `load_length` ⇒ larger estimate); bg swaps cost less than inline; purge=0 config
still yields non-zero waste when colour-aware purge is on; header-parse cases from the
fixture, including missing-density fallback.

### 1.6 Where the purge actually goes (double-counting guard)

Open question 3 is answered, and the answer is "it depends, so read it from the file":
multicolour prints normally run **with a prime tower and with flush-into-support enabled**.
Both of those mean the purged filament is *already extruded by the slicer*, so its time and
its grams are already inside `base_s` and inside `; filament used [g]`. Adding a modelled
purge on top would double-count the biggest single term in the estimate.

So the model must classify each swap's purge destination, from the gcode itself, never from a
config default:

| Detected in the file | Purge destination | Estimate treatment |
|---|---|---|
| Prime/wipe tower present (tower config keys in the header, or tower geometry between toolchanges) | tower | time and grams **already in `base_s`/`[g]`** — count as `waste_g` for reporting, add **nothing** to the total |
| Flush into infill / into support enabled | infill/support | same — already counted, and it is *not* waste, so report it separately from tower waste |
| Neither, and `swap_purge_length > 0` (or §3.4's colour-aware purge) | multiACE purge bin / wiper | genuinely additional — **add** its extrude time to `added_s` and its grams to waste |

Implement as `purge_destination(header) -> {"tower"|"flush"|"bin"|"mixed"}` in `swap_cost.py`,
surfaced in `estimate.assumptions` (*"prime tower detected — purge already in slicer total"*)
so the number on screen is always explainable. When the header is ambiguous, report
`"unknown"` and show both totals (with and without added purge) rather than picking one.

> **Verify with the fixture:** which exact header keys the Snapmaker fork writes for prime
> tower and for flush-into-support. This is the one parse that changes the headline number.

---

## 2. Mock / offline preflight ("mock print")

Goal: upload a gcode on a laptop with no printer, get the full plan + §1 estimates, and
optionally download the rewritten file.

1. **Mock-gate the three endpoints.** `/api/preflight/livedata` and `/api/preflight` read
   `live_slots` / `head_ctx` from `mock_state.json` when `_mock_enabled(request)`; drop the
   `409 no slots are loaded` in mock. `/api/preflight/print` returns `503 mock mode` — a
   mocked run must never look like it queued a real print.
2. **Virtual loadout editor.** In mock mode the "live slots" grid becomes editable: pick
   ACE count, per-slot material/colour, per-head feeder, which heads are bg-capable. Persist
   to `localStorage` and offer "load from `mock_state.json`" / "export". This is also the
   what-if tool for §3 (*"would 2 ACEs beat 1?"*) and works against the real printer too,
   as a read-only overlay that never writes slot state.
3. **Download instead of print.** The Pyodide worker already returns the rewritten text
   (`rewrite-done`); wire a "Download rewritten gcode" button — useful offline *and* on a
   real printer for slicer-side workflows.
4. **Fixtures.** `tests/fixtures/sample_4color.gcode` (small, real headers, ~20 toolchanges,
   M73 present) + `mock_state.json` variants for 1-ACE, 2-ACE and head-mode.

Tests: extend `tests/unit/test_web_api.py` — preflight in mock returns a report with
`estimate`; `preflight/print` refuses; livedata serves mock slots.

---

## 3. Loadout planner + plan editor

### 3.1 Cost by source position (this is the lever)

Where a colour sits decides what a change to it costs:

| Position of the next colour | What happens | Cost |
|---|---|---|
| **Stock feeder**, pinned to its own head | tool pickup only | ~0 added (no ACE work) |
| **Same ACE, different slot**, same head | that head must unload → ACE spool change → reload; the ACE is busy | full inline cost |
| **Another ACE on another head** | the *other* head loads while the current head keeps printing (existing background swap) | ~`BG_SWAP_COST_BG_S`, i.e. mostly free |
| Another ACE, no time window before use | degrades to inline | full inline cost |

That is exactly the user's blue-from-head-2 / green-preloaded-in-head-1 case, and the
machinery exists (`compute_head_mode_optimize(..., event_times=, bg_heads=)`,
`head_mode_bg_stats`). Two changes make it pay off:

1. **Cost model injection.** Add an optional `cost_model=` kwarg to
   `compute_head_mode_optimize` / `head_mode_bg_stats` / `compute_swap_aware_layout`, and
   replace the `BG_SWAP_COST_*` literals with model lookups when it is supplied. Keep the
   constants as the default and keep the existing `except TypeError:` older-signature
   fallback pattern used in `preflight_core` — an older installed post-processor must keep
   working.
2. **Derive the bg window.** `BG_UNLOAD_MIN_WINDOW_MIN = 1` becomes
   `model.bg_window_minutes(ace, slot)` (real unload+load duration + margin). A 1-minute
   constant will call a window feasible that a 2100 mm load cannot fit.

### 3.2 `explain` output → the editor

Have the optimizer emit a per-event trace, and return it in the report:

```json
"timeline": [{"i": 14, "t": 3, "head": 1, "ace": 0, "slot": 2,
              "kind": "cross_ace_bg", "window_min": 6.2,
              "seconds": 30, "purge_mm": 90, "note": "preloaded during T2"}]
```

### 3.3 Plan editor UI

A **Plan** panel in the preflight step:

* Swim-lane per head, one block per toolchange along the print, coloured by filament;
  inline swaps drawn as a red stall block, bg swaps as a hatched block on the idle head.
* Header line: total added time, purge grams, inline vs bg counts — recomputed live.
* Click a colour → assign to any target (feeder / ACE+slot). This reuses the **existing**
  `head_assignment` → `assignment_from_target_ids` → `rewrite_head_mode_to_file` path, so
  "the code translates the higher-level decision into gcode" is already true; the editor
  only produces `{t: target_id}`.
* "Apply recommendation" buttons for `optimize` / `layer`, and a diff vs the current
  physical load: *"move green from ACE 0 slot 3 → ACE 1 slot 0, saves ~9 min"*.
* Recompute runs in the Pyodide worker → instant, works offline (§2).
* **Opens together with the preview pane (§9)** — same upload, one screen. The planner answers
  "which slot should each colour be in"; the preview answers "and what will that actually
  print". Splitting them into two screens would mean uploading the same file twice.

**Explicitly out of scope:** reordering the *print* order of colours. That is the slicer's
layer geometry, not something a post-processor can safely permute. What we optimise is
placement (which slot/head/ACE a colour occupies) and purge per transition.

### 3.4 Order- and colour-aware purge

`swap_purge_length` is one global number today. Transition cost is not symmetric — white
after black needs far more purge than black after white.

* New config: `purge_color_aware: false` (default off), `swap_purge_min`, `swap_purge_max`,
  and optional `purge_material_matrix` for cross-material transitions.
* Per-transition purge = interpolate between min/max on perceptual distance from the
  outgoing to the incoming colour (reuse the existing Lab-ish colour distance already used
  by the fuzzy matcher), floored per material pair.
* Emit it into the gcode: `ACE_SWAP_HEAD HEAD=1 ACE=0 SLOT=2 PURGE=140`. **Already
  compatible** — the plan-line regex tolerates extra `KEY=VAL`
  ([post_process_virtual_toolheads.py:75](../../multiace/tools/post_process_virtual_toolheads.py#L75)) — and
  `cmd_ACE_SWAP_HEAD` learns an optional `PURGE=` that overrides the config for that swap.
* Feeds §1's waste number directly: the estimate is the sum of the per-swap purge the
  rewrite actually emits, not an average.

Tests: `tests/unit/test_plan_cost.py` — bg-reachable placement beats same-ACE placement on
an event stream with wide windows; equal cost falls back to today's swap-count tie-break;
`PURGE=` round-trips through rewrite and parse; colour-aware purge is symmetric-monotone.

---

## 4. Print history

### 4.1 Data

Moonraker already keeps job history (`/server/history/list`, `/server/history/totals`) — the
same data the printer's own web UI shows at
`http://<printer>/?printer=<id>#/history`. That route is a client-side view over those two
endpoints, so multiACE talks to Moonraker directly and does **not** scrape or embed it; the
UI route is useful as the reference for which fields exist and what they mean, and as an
"open in printer UI" link from the History tab. Do not duplicate the data — **join** to it:

* `ace.py` appends a record to `printer_data/multiace/jobs.jsonl` at print start and
  completion: filename, plan used (`slicer|optimize|layer|loadout`), the resolved
  assignment, estimated `added_s`/purge from the preflight, actual toolchange count, actual
  swap durations, inline vs bg realised, load retries and neighbour retracts (§6), final
  state. Bounded file with rotation (keep last 200 jobs / 2 MB).
* Preflight stashes its `estimate` block under the token so the job record can carry
  estimated-vs-actual without recomputing.

### 4.2 API + UI

* `GET /api/history?limit=50` → merged list (Moonraker job + multiACE record). **Join key is
  filename + start time** (open question 4, decided): do not wait on Moonraker's `job_id`
  being reachable from the Klipper side at print start. Concretely — match on
  `basename(filename)` plus start times within a ±90 s window, take the nearest candidate,
  and when two jobs of the same file start inside that window, mark both `ambiguous` and show
  the multiACE record unjoined rather than guessing. multiACE's own record is authoritative
  for everything multiACE knows (plan, assignment, swaps); Moonraker's is authoritative for
  duration and result. If a `job_id` does turn out to be reachable later, it becomes a
  preferred key with this matcher as the fallback — no schema change.
* Mock-aware via `mock_history.json`.
* `GET /api/history/{id}` → detail: timeline, per-colour filament, estimate vs actual.
* `DELETE /api/history/{id}`, `POST /api/history/clear` (debug-mode gated).
* UI: a **History** tab — table (date, file, duration, colours as swatches, swaps,
  filament g, result) with a detail drawer showing the §3.2 timeline replayed against what
  actually happened, and an "estimate accuracy" line.

### 4.3 Closing the loop

Aggregate actual swap durations per `kind` across jobs into
`printer_data/multiace/swap_stats.json`; `SwapCostModel` prefers measured medians (n ≥ 5)
over its constants and reports `confidence: "calibrated"`. This is what makes §1 accurate
on *this* machine rather than merely plausible.

---

## 5. Update from the UI, including your own unreleased changes

### 5.1 Verified current state

Already working end to end: Config tab → check/apply → `multiace_update.sh` fetches the
GitHub release (or `update_url_base` static index), verifies sha256, runs
`install_multiace.sh`, restarts Klipper via Moonraker, asks for a reboot. Persistent
updates require debug mode ([main.py:1316-1328](../../multiace/web/backend/main.py#L1316-L1328)) because a
non-debug install is wiped on reboot. README documents the SSH install
(`scp -r multiace/ root@<ip>:/tmp/multiace/` → `install_multiace.sh`) — that part checks out.

So the work here is **not** "add an update button" but "make my local tree installable
without a release".

### 5.2 `scripts/push-to-printer.sh` / `.ps1` (primary)

One command from the dev machine:

1. tar the working tree's `multiace/` (respecting `.gitignore`, excluding `__pycache__`),
   stamp a dev version (`MULTIACE_VERSION = '<tag>+dev.<shortsha>[-dirty]'`),
2. `scp` to `/tmp/multiace-dev.tar.gz`, extract, run
   `install_multiace.sh --install-web`,
3. `FIRMWARE_RESTART` via Moonraker, then tail `/api/health` until it answers.

Flags: `--host` / `$MULTIACE_PRINTER_HOST`, `--dry-run`, `--no-restart`, `--web-only`
(frontend/backend only — no Klipper restart needed, the fast inner loop). Both scripts share
one behaviour spec; the `.ps1` is a real port, not a stub — this is a Windows dev machine.

### 5.3 `POST /api/update/upload` (optional, the true "no USB stick" path)

Multipart tarball → same install path as `apply`. Because this **executes uploaded code on
the printer**, it must be: debug-mode gated (already the rule for persistent updates), size
capped, sha256 echoed back for confirmation, refused when a print is active, and documented
as "trusted-LAN only". Frontend: an "Install from file" drop zone next to the existing
update panel, streaming the same `STATUS:` log the release path already renders.

### 5.4 Polish on the existing button

* Dashboard badge when a check finds an update (auto-check once per day, cached).
* Show `from → to` plus a release-notes link, and the reboot-required hint inline.
* Report the dev suffix in `/api/version` so "old code running despite update" (a README
  troubleshooting entry) becomes diagnosable at a glance.

---

## 6. Neighbour retract before each load retry

### 6.1 Behaviour

When a load fails and a retry remains, the filament in the *other* slots of the same ACE can
be crowding the shared path. Before each retry, retract every other slot in that ACE by
10 cm; over the default 3 retries that is up to 30 cm per neighbour — enough clearance for
the active filament to advance, and harmless because a later load of those slots re-feeds
from the spool anyway.

**ACE slots only** (open question 5, decided). A head on a stock feeder has exactly one
filament and no shared path, so there is no neighbour to clear and the concept does not
apply. The helper therefore takes an `ace_index` and only ever iterates that ACE's slots; a
retry on a stock-feeder head skips neighbour clearance entirely and logs why
(`reason: "stock_feeder"`), so the absence is visible in the audit rather than looking like a
bug. This is a guard as much as a scope decision — it removes any path by which the feature
could command a feeder that has no ACE behind it.

### 6.2 Implementation

In the retry loop ([ace.py:7403-7504](../../multiace/klipper/extras/ace.py#L7403-L7504)), immediately
before the existing `self._reset_feed_channel(ff, ff_module, channel)` on the retry path:

```python
self._retry_clear_neighbors(ace_index, slot, head, attempt)
```

The helper, per other slot `s` in `ace_index`:

* **skip** the target slot; slots reporting empty; and any slot currently loaded into a head
  (`self._head_source` reverse lookup) — retracting a printing filament is the one way this
  feature could ruin a job;
* `stop_feed_assist` on `s` first (same order as [ace.py:7184-7186](../../multiace/klipper/extras/ace.py#L7184-L7186)),
  then `self._retract(s, step_mm, retract_speed)` (`unwind_filament`);
* track cumulative retraction per `(ace, slot)` for this load attempt-chain and stop at the
  cap; reset the tracker when the load finally succeeds or is abandoned;
* wrap each slot in `try/except` — a neighbour that refuses must not abort the retry;
* `_audit_state('LOAD_RETRY_NEIGHBOR_RETRACT', {...})` and add
  `neighbor_retract: {"2": 100, "3": 200}` to `_retry_state_publish` so the dashboard banner
  can say *"clearing slots 3, 4 (10 cm)"*.

### 6.3 Config

```ini
load_retry_neighbor_retract: 100        # mm per retry, 0 = off
load_retry_neighbor_retract_max: 300    # cumulative cap per slot
#load_retry_neighbor_retract_0: 100     # per-head override
```

Defaults give exactly the requested 10 cm × 3 = 30 cm. `0` restores today's behaviour.
Distinct from the existing `load_retry_retract: 50`, which retracts the *active* filament
inside the low-level feed retry — mention both in the README so they are not confused.

### 6.4 Tests

Extend `tests/unit/test_load_retry.py` with a fake ACE recording dispatched requests:
first attempt sends no `unwind_filament`; each retry sends one per eligible neighbour at
100 mm; the cap holds at 300 mm; loaded and empty slots are skipped; `0` disables it; a
raising neighbour still lets the retry proceed; `retry_state` carries the per-slot mm.

---

## 7. Detect an ACE that is powered on *after* the printer

### 7.1 The observed error, and why it is terminal

```
USB unstable, expected 1 ACEs, found 0 - ACE inactive, Klipper continues.
FIRMWARE_RESTART required after reconnecting.
```

That string is `msg.usb_unstable` ([i18n/en.json:398](../../multiace/i18n/en.json#L398)), raised at
[ace.py:1470-1478](../../multiace/klipper/extras/ace.py#L1470-L1478). The handler waits up to 20 s for
`ace_device_count` devices; if the count is still short it sets `_ace_startup_failed = True`,
logs, and **`return`s early** — before `_ace_canonical` is assigned (it stays `None`), before
any `_open_ace()`, before `_set_active_idx()`. Nothing ever re-enters that code, so
`FIRMWARE_RESTART` really is the only recovery today. Three findings shape the fix:

1. **The recovery mechanism is already written.** `_refresh_ace_devices` detects a unit that
   "missed the startup scan", appends it, and connects it
   ([ace.py:766-788](../../multiace/klipper/extras/ace.py#L766-L788)) — but only
   `if self._ace_canonical is not None`. Startup soft-fail leaves it `None`, so the one code
   path that would handle your case is the one it disables. This is a small fix, not a feature.
2. **`_ace_startup_failed` is written twice and never read** (only lines 171, 1472, 1499). It
   gates nothing and is invisible to the API — the flag exists purely for the log line.
3. **`_hotplug_monitor` ([ace.py:1527](../../multiace/klipper/extras/ace.py#L1527)) is never
   registered** with `reactor.register_timer` — no call site exists. If that holds up on a
   closer read, the disconnect/reconnect watcher is dead code, and unplug→replug has no
   recovery path either. **Confirm this before building on it** (it is a strong claim, and a
   registration hidden behind a config flag is easy to miss); reviving that timer is the
   natural home for the rescan below.

### 7.2 Fix: deferred startup completion + a rescan timer

1. **Extract the startup tail.** Move everything from
   [ace.py:1480](../../multiace/klipper/extras/ace.py#L1480) to the end of `_handle_connect`
   (`_ace_canonical`/`_ace_present` assignment, saved-active-device restore, `self._queue`,
   the `CONNECT_ATTEMPTS` open loop, `_set_active_idx`) into `_complete_startup()`.
   **Extract-only, no logic change** — both the normal and the deferred path then call one
   function, so there is no second startup implementation to drift.
2. **Soft-fail enters a `waiting` state instead of returning.** Register a slow rescan timer
   (`ace_rescan_interval: 5.0`, `0` = off) that calls `_refresh_ace_devices('rescan')` and,
   once the expected count is present, calls `_complete_startup()`, clears the flag, logs
   "ACE N found and connected — no restart needed", and unregisters itself.
3. **Only lock indices at the full expected count.** This is *why* the current code gives up
   rather than continuing, and it must survive the fix. Index identity comes from the sorted
   USB path (`_ace_path_sort_key`); locking `_ace_canonical` while 1 of 3 units is up makes
   the late arrivals append at the end, so index ≠ physical order and slots silently
   mismap — worse than the current error. So: full count ⇒ lock and complete; short ⇒ stay
   unlocked and keep waiting. Leave a comment saying so, or someone will "simplify" the wait
   away.
4. **`ACE_RESCAN` command**, registered alongside the others at
   [ace.py:700-750](../../multiace/klipper/extras/ace.py#L700-L750): rescan now, report
   `found N/expected M`, complete startup if satisfied. `LOCK=1` deliberately locks with
   fewer units for single-ACE users who unplug on purpose — an explicit act, never automatic.
5. **Timer discipline.** `_scan_ace_devices` is a directory listing, so 5 s is cheap, but the
   timer must skip while `self._auto_feed_enabled or self._swap_in_progress` (the guard
   `_hotplug_monitor` already uses at [ace.py:1529-1530](../../multiace/klipper/extras/ace.py#L1529-L1530))
   and while `_homing_active()`; a `_open_ace()` in the middle of a swap is the one way this
   could break a running print. Back the interval off after ~24 h of no device.
6. **Say the true thing.** Replace the "FIRMWARE_RESTART required" wording with *"waiting for
   1 ACE — power it on and it is picked up within ~5 s, or run `ACE_RESCAN`"*, and expose
   `{"ace_startup": "waiting", "found": 0, "expected": 1}` in `/api/state` so the dashboard
   shows a banner with a **Rescan** button. Users should not need to know a macro exists.
   Needs the string updated in all three of `i18n/{en,de,zh}.json`.

### 7.3 Tests

`tests/unit/test_late_ace_detect.py`, driving a fake `_scan_ace_devices` (the Klipper env is
already stubbed in [tests/conftest.py](../../tests/conftest.py)): 0-of-1 startup leaves a live
degraded object and raises nothing; a later scan returning the device triggers exactly one
`_complete_startup()` with canonical locked and the active index set; 1-of-2 never completes;
`ACE_RESCAN LOCK=1` does complete at 1-of-2; the timer unregisters after completion; no
rescan fires while `_swap_in_progress`; and the normal full-count startup path still routes
through the same `_complete_startup()`.

---

## 8. Live print control (the printer's display, in the browser)

Everything you can do by tapping the printer's screen mid-print: speed, flow, fan, temps,
Z babystep, pause/resume/cancel.

### 8.1 What already exists

`/api/console` and `/api/plugin-api/gcode` already pass arbitrary gcode to Moonraker's
`/printer/gcode/script` ([main.py:2951-2971](../../multiace/web/backend/main.py#L2951-L2971)), and
`print_stats` is already in the subscribed object list
([main.py:189](../../multiace/web/backend/main.py#L189)). So the transport and half the state are
there. What is missing is the *readback* of the live factors and a UI that is not a text box.

### 8.2 State

Extend the Moonraker subscription with `gcode_move` (`speed_factor`, `extrude_factor`,
`homing_origin[2]` = the live Z offset), `fan`, `extruder`/`heater_bed`, `virtual_sdcard`
(progress) and `display_status` (message). These arrive on the websocket the log listener
already runs ([main.py:2421](../../multiace/web/backend/main.py#L2421)) — no new poll loop, and the
UI reflects changes made from the physical display too, which is the point: one truth, two
front-ends.

### 8.3 `POST /api/print-control` — verbs, not gcode

A whitelisted verb + value, never a raw string from the UI:

| Verb | Emits | Clamp |
|---|---|---|
| `speed` | `M220 S<pct>` | 25–300 % |
| `flow` | `M221 S<pct>` | 75–125 % |
| `fan` | `M106 S<0-255>` | 0–100 % in, scaled out |
| `nozzle` / `bed` | `SET_HEATER_TEMPERATURE` | per-heater `min_temp`/`max_temp`, read from Moonraker's config, not hardcoded |
| `babystep` | `SET_GCODE_OFFSET Z_ADJUST=<mm> MOVE=1` | ±0.05 mm per press, ±0.5 mm cumulative per job |
| `pause` / `resume` / `cancel` | `/printer/print/*` | confirm dialog on cancel |

Rationale for the whitelist: the raw-gcode passthrough stays for power users, but a
one-tap control that can emit any gcode is a control that can emit `M104 S300` off a slider
bug. Clamping server-side (not in JS) means the printer is protected regardless of what the
browser sends. Mock-aware: in mock mode the verbs mutate `mock_state.json` and return `ok`.

### 8.4 UI

A third **Print** pane in the existing sidebar next to webcam and console — reuse
`sidebar.pane` ([app.js:3097-3145](../../multiace/web/frontend/app.js#L3097-L3145)) rather than adding a
tab, so it is one click away from the dashboard while a print runs. Sliders with a debounce
(one command per ~250 ms of dragging, and always a final command on release — an undelivered
last event is how a slider lies about the machine's state), current-value readback from §8.2,
and a "reset to 100 %" affordance for each factor.

Plus the three controls Fluidd *cannot* offer, which is the real justification for building
this here rather than linking out:

* **Pause at next toolchange** (rather than "now, mid-perimeter") — arm a flag the swap path
  checks.
* **Retry now / cancel retry** on a stalled load — the endpoint already exists
  ([main.py:2742-2751](../../multiace/web/backend/main.py#L2742-L2751)); surface it as a button on the
  banner instead of only in the console.
* **Purge more now** — a bounded manual purge when a colour comes out muddy, clamped by the
  same §13.2 ceiling as the automatic purge.

### 8.5 Tests

`tests/unit/test_print_control.py`: each verb emits the expected gcode; out-of-range values
are clamped, not rejected silently (return the applied value so the UI can snap back);
cumulative babystep cap holds across calls and resets on a new job; heater limits come from
config; mock mode never contacts Moonraker.

---

## 9. G-code preview pane

Replicate the slicer's preview *before* launching the print, in the same screen as the
loadout planner — both need the same uploaded file, so they are one flow, not two.

### 9.1 Two viewers, and why both

The reference is the printer's own viewer at
`http://<printer>/?printer=<id>#/preview`. It is worth being precise about what that route
can and cannot do here, because it decides the design:

* It renders a file **already present in Moonraker's `gcodes` root** — it is a client-side
  viewer over `/server/files/...`, not a stream we can point at an arbitrary buffer.
* It colours by the **slicer's** tool index. It therefore shows the *input* file, and cannot
  show multiACE's plan — which head, which ACE, which slot each colour ends up on is exactly
  what the rewrite decides and exactly what the user wants to see before printing.

So:

1. **Phase 1 — embed the printer's viewer.** Near-zero code, exact slicer parity, and it
   answers "does this file look right at all". Upload the file with `print: "false"` (the
   existing endpoint hardcodes `"true"` at
   [main.py:1440](../../multiace/web/backend/main.py#L1440) — parameterise it), then iframe
   `{web_root}/?printer=<id>#/preview` where `web_root` is `_webcam_base()`
   ([main.py:2578-2589](../../multiace/web/backend/main.py#L2578-L2589)), which already strips
   Moonraker's port off correctly for exactly this reason.
2. **Phase 2 — our own canvas viewer**, over the rewritten text the Pyodide worker already
   holds (`rewrite-done`). Layer slider, toolpaths coloured by the **assigned** filament,
   §3.2's timeline markers on the layer scrubber (*"inline swap here, 3.5 min"*), prime tower
   and purge blocks highlighted so §1.6's waste number has a picture attached. This is the
   one that makes the loadout planner reviewable, and it works offline (§2).

Ship 1 first; keep both behind one toggle in the same pane (*Slicer view* / *multiACE plan*),
because the diff between them is itself informative.

### 9.2 Constraints worth designing for up front

* **A preview upload must never start a print.** `print: "false"`, a distinct
  `multiace-preview/` subfolder in the `gcodes` root, and delete-on-close. A stray file in
  the print list that looks printable but is a rewrite candidate is a real footgun.
* **Nesting.** multiACE's own panel is registered as a Fluidd iframe camera
  ([main.py:2598-2614](../../multiace/web/backend/main.py#L2598-L2614)). Embedding Fluidd inside
  multiACE inside Fluidd is a mirror tunnel; when `panelMode` is active, offer "open preview
  in a new tab" instead of the iframe.
* **File size.** Parse to a layer index in a worker, incrementally, with a size cap and
  decimation — a large multicolour gcode held whole in browser memory kills the tab, and the
  Pyodide worker is already holding one copy.
* **Printer identity.** The `?printer=<id>` query is that UI's multi-printer key; read it from
  its own config or let the user paste the URL once and store it, rather than hardcoding the
  hash seen today.

### 9.3 Tests

`tests/unit/test_preview.py`: upload-for-preview passes `print: "false"` and the preview
subfolder; cleanup removes the file; the built iframe URL is derived from `_webcam_base()`
and carries the printer id; panel mode returns a link instead of an embed. Frontend: the
layer index built from `tests/fixtures/sample_4color.gcode` has the expected layer count
(cross-checked against `; total layers count`, §1.3) and the expected toolchange layers.

---

## 10. Phasing

**Phase 0 — do this first: §7 late ACE detection**
Independent of everything else, a contained fix to a path that already exists, and it removes
a restart from every single test cycle of every other phase. Ship it alone.

**Phase A — foundation (unblocks 1, 3, 3.1)**
`swap_cost.py`; filament-header parsing against a real fixture; `estimate` block in the
report; estimate card in the UI. Ship §6 in parallel — it is independent and small.

**Phase B — offline**
Mock-gate the preflight endpoints; virtual loadout editor; download-rewritten-gcode; serve
`swap_cost.py` from `/api/preflight/pysrc` and write it into the Pyodide FS (**the worker
currently loads exactly two modules — adding a third means touching both
[main.py:1160-1184](../../multiace/web/backend/main.py#L1160-L1184) and the worker's init**).

**Phase C — planner**
`cost_model=` injection into the optimizers; model-derived bg window; `timeline` output;
plan editor UI; colour-aware purge + `PURGE=` on `ACE_SWAP_HEAD`.

**Phase D — history & calibration**
`jobs.jsonl`; `/api/history*`; History tab; `swap_stats.json` feeding the cost model.

**Phase E — push updates**
`push-to-printer.sh/.ps1`; optional upload endpoint; update-badge polish; README.

**Phase F — live print control (§8)**
Independent of A→E and independently useful: extend the subscription, `POST
/api/print-control`, the Print sidebar pane. Can be pulled forward at any time; the only
sequencing note is that "purge more now" wants §13.2's clamp to exist first.

**Phase G — preview (§9)**
Phase 1 (embed the printer's viewer) can ship with Phase B, since it is the same upload flow
as the loadout planner and the two open together. Phase 2 (plan-aware canvas) needs Phase C's
`timeline`, so it lands after the planner.

Phase A→C are sequential (each consumes the previous one's output). D, E and F are
independent and can be pulled forward if the update-push loop is what hurts most day to day —
it probably is, since every other phase is tested by pushing code to the printer. Phase 0 and
E together are the whole inner dev loop: push code, no restart, no power-cycle dance.

---

## 11. Files touched

| File | Change |
|---|---|
| `multiace/tools/swap_cost.py` | **new** — cost model, purge model, calibration loading |
| `multiace/tools/post_process_virtual_toolheads.py` | filament-header parse; `cost_model=` kwarg; `timeline`/`explain`; `PURGE=` emission; model-derived bg window |
| `multiace/web/backend/preflight_core.py` | `estimate` + `timeline` per plan; pass the cost model through |
| `multiace/web/backend/main.py` | mock-gate preflight endpoints; serve `swap_cost.py` in `/api/preflight/pysrc`; `/api/history*`; optional `/api/update/upload`; extended Moonraker subscription + `/api/print-control` (§8); parameterised upload `print:` flag + preview URL builder (§9) |
| `multiace/web/frontend/preflight_pyodide_worker.js` | load the third module |
| `multiace/web/frontend/app.js` + `style.css` + `index.html` | estimate card, plan editor, virtual loadout, History tab, update polish, Print sidebar pane (§8), preview pane (§9) |
| `multiace/web/frontend/gcode_preview.js` | **new** — incremental layer indexer + canvas viewer (§9 phase 2) |
| `multiace/klipper/extras/ace.py` | `_retry_clear_neighbors` + config; `PURGE=` on `ACE_SWAP_HEAD`; job records; `_complete_startup()` extraction + rescan timer + `ACE_RESCAN` (§7) |
| `multiace/config/extended/ace.cfg` | `load_retry_neighbor_retract*`, `purge_color_aware`, `swap_purge_min/max`, `preflight_rewrite_eta`, `ace_rescan_interval` |
| `multiace/i18n/{en,de,zh}.json` | new strings; **rewrite `msg.usb_unstable`** (§7.2.6) |
| `scripts/push-to-printer.{sh,ps1}` | **new** |
| `tests/unit/test_swap_cost.py`, `test_plan_cost.py`, `test_history.py`, `test_late_ace_detect.py`, `test_print_control.py`, `test_preview.py` | **new** |
| `tests/unit/test_load_retry.py`, `test_web_api.py` | extended |
| `tests/fixtures/sample_4color.gcode`, `mock_history.json` | **new** |
| `README.md`, `docs/FILAMENT_SWAP_GUIDE.md` | document all of the above |

---

## 12. Risks (correctness / usability)

Risks of *permanent hardware damage* are tracked separately, and their guards are mandatory —
see §13.

| Risk | Mitigation |
|---|---|
| Estimate looks authoritative but is modelled | Show `confidence` and the assumption list; calibrate from history (§4.3); never present a range-free number as measured |
| Cost-model change silently alters chosen plans | Constants default to today's values; `cost_model=None` reproduces current output byte-for-byte; add a regression test pinning the current plan for the fixture |
| Older installed post-processor lacks new kwargs | Keep the existing `except TypeError:` fallback pattern; soft-degrade to swap counts, as `_bg_context` already does |
| M73 rewrite corrupts progress reporting | Off by default behind `preflight_rewrite_eta` |
| `/api/update/upload` = remote code execution on the printer | Debug-mode gated, size capped, refused mid-print, documented trusted-LAN only; the SSH push script is the recommended path |
| Neighbour retract pulls a *printing* filament | Skip anything in `_head_source`; skip empty slots; per-slot try/except; `0` disables |
| Purge matrix over-purges and wastes more than it saves | `purge_color_aware` off by default; report the estimated waste delta before applying |
| History file grows unbounded on the Pi | Cap at 200 jobs / 2 MB with rotation; append-only JSONL, never rewritten in place |
| §7: locking a partial ACE set mismaps slot indices | Lock only at the full expected count; partial locking requires an explicit `ACE_RESCAN LOCK=1` |
| §7: a rescan/connect mid-print disturbs the USB bus | Skip while `_auto_feed_enabled`/`_swap_in_progress`/homing — the guard `_hotplug_monitor` already uses |
| §7: extracting the `_handle_connect` tail regresses working single-ACE startup | Pure extraction, no logic change, plus a test asserting the normal path routes through the same `_complete_startup()` |
| §8: UI and printer display disagree about the live factors | Read `gcode_move` back from the shared websocket rather than tracking a local copy; always send a final command on slider release |
| §9: a preview upload appears in the print list and gets printed | `print: "false"`, dedicated `multiace-preview/` subfolder, delete-on-close, and a distinct badge in the UI |
| §9: large gcode kills the browser tab | Incremental worker parse with a size cap and decimation; fall back to the embedded printer viewer above the cap |
| §9: embedded printer UI inside the panel that is itself an iframe | Detect `panelMode` and offer an "open in new tab" link instead of the embed |

---

## 13. Hardware-damage risks (must-fix guards, not optional polish)

§12 is about the plan being *wrong*. This section is about the plan being *destructive*. Four
items introduce paths that can permanently damage hardware, and one pre-existing gap sits in a
path §5 makes easier to trigger. Nothing here blocks the plan — each has a cheap guard — but
none of these guards is optional, and each must land **in the same commit** as the feature.

Each subsection ends with a **Recommended solution**: the single approach to build, not a menu.

### 13.1 §5 push/upload — the serious one

`install_multiace.sh` does not merely drop in multiACE files. It **replaces stock Klipper
code** — `filament_feed.py`, `filament_switch_sensor.py`, `extruder.py`
([install_multiace.sh:82-92](../../multiace/install_multiace.sh#L82-L92), :370-374) — and
`sed`-patches `mcu.py` to raise `TRSYNC_TIMEOUT` from 0.050 to 0.350
([:129-136](../../multiace/install_multiace.sh#L129-L136)). `TRSYNC_TIMEOUT` governs
**multi-MCU homing**. A bad value or a half-applied patch is a homing-accuracy problem, and
homing failures are how a toolhead drives into the bed: bent probe, cracked build plate,
smashed nozzle, damaged toolhead. Wrong extruder kinematics is how you grind filament and
stress the extruder gear.

Today that risk is bounded by friction — you have to SSH in deliberately. §5 removes the
friction by design (that is the point), so the guards have to replace it:

* **Never push a tree that hasn't passed tests.** `push-to-printer` runs `pytest` locally and
  aborts on failure. `--skip-tests` must exist, must be typed explicitly, and must print what
  it is bypassing.
* **Refuse while a print is active** — see §13.4; this applies to the push script, the upload
  endpoint, *and* the existing `apply`.
* **Never let the dev push touch `mcu.py`.** The `TRSYNC_TIMEOUT` patch belongs to a
  deliberate full install, not a 30-second inner-loop push. `--web-only` (backend/frontend,
  no Klipper restart) should be the default for UI work, and the script should say which class
  of push it is doing before it does it.
* **Verify recovery, don't assume it.** After the restart: poll `/api/health`, confirm Klipper
  reports ready, confirm the expected ACE count reconnected (now observable thanks to §7),
  and report failure loudly. A push that half-lands is exactly the "half-stock,
  half-multiACE" state the README warns about at line 265.
* **Rollback command.** The installer already keeps `extruder_pre_multiace.py` and
  `mcu.py.pre_multiace`; add `push-to-printer --rollback` that restores them and restarts, so
  recovery does not depend on remembering the filenames while the printer is down.
* **No auto-update, ever.** Update *checks* may be automatic (§5.4); applying is always an
  explicit human action.

#### Recommended solution

**Two push classes, and make `--web-only` the default.** The guard that does the most work is
the one that removes Klipper from the loop entirely for the 90 % case:

1. `push-to-printer` **defaults to `--web-only`**: syncs `multiace/web/` only, restarts the
   panel service, never touches `mcu.py`, `extruder.py` or the Klipper extras, never restarts
   Klipper. This is the inner loop and it is mechanically inert — the worst case is a broken
   web page. Print-state check still applies (a broken panel mid-print is bad UX, not damage),
   but it is a warning, not a refusal.
2. `--full` is the deliberate class: it syncs `multiace/klipper/` and runs the installer.
   Gated by, in this order — `pytest` passes locally (`--skip-tests` prints exactly which
   files it is bypassing); `print_stats.state ∉ {printing, paused}` and no override flag;
   the target's `mcu.py` sha256 matches either stock or the expected post-patch value, and
   **aborts on a third value** rather than re-patching an already-patched or hand-edited file;
   post-restart verification (`/api/health` ready, expected ACE count reconnected via §7's
   new state) with a loud failure and an immediate offer to `--rollback`.
3. **`--rollback` is written and tested in the same commit as the push script**, not after.
   It restores `extruder_pre_multiace.py` / `mcu.py.pre_multiace` and restarts. A recovery
   path that has never been executed is not a recovery path.
4. `/api/update/upload` (§5.3) is **deferred, not built in Phase E.** The SSH script covers
   the actual need without adding an authenticated-RCE endpoint to a LAN service. Build it
   only if a concrete "no dev machine available" case shows up, and then only in the
   `--web-only` shape — an upload endpoint that can rewrite `mcu.py` is not worth it.

Sequence: land the print-state check (§13.4) first, then `--web-only`, then `--full` with
`--rollback` in the same commit.

### 13.2 §3.4 colour-aware purge — blob and grind risk

Purge is the one thing in this plan that commands the hotend to extrude a computed,
config-derived volume. Two failure modes, both able to end a hotend:

* **Exceeding purge-bin/wiper capacity.** Overflow accumulates on the nozzle and heater block
  — the classic blob — which rips the silicone sock, tears out thermistor/heater leads, and
  routinely means a new hotend. Note the README records a **v2 larger purge bin**: capacity is
  *hardware-version dependent*, so a purge volume that is safe on one machine is not on
  another. Never derive purge from colour distance alone without a machine capacity ceiling.
* **Purging faster than the hotend can melt.** Back-pressure grinds the filament flat, skips
  the extruder, and can pop the PTFE coupler.

Required guards: a hard `swap_purge_max` with a `config.getint(..., maxval=...)` ceiling
(match the existing `swap_retract_length` style, `minval=0, maxval=2000`); rate-limit purge by
**volumetric flow** for the material, not raw feedrate; require `heater.can_extrude` before
purging (the pattern already used at [ace.py:5949-5955](../../multiace/klipper/extras/ace.py#L5949-L5955)
and [:6779-6786](../../multiace/klipper/extras/ace.py#L6779-L6786)) — an injected `PURGE=` must
not bypass it; and clamp the value **inside `cmd_ACE_SWAP_HEAD`**, never trusting the number
in the gcode, since that file may have been generated against a different machine.

Ship §3.4 as **estimate-only first**: compute and display per-swap purge for a few real prints
and compare against the bin before letting it emit `PURGE=` at all. `purge_color_aware`
defaults off for this reason.

#### Recommended solution

**Prefer the prime tower; treat bin purge as the exception.** §1.6 established that this
machine normally prints multicolour with a prime tower and flush-into-support — which means
the slicer is already handling purge, in a place with unbounded capacity, at a flow rate it
computed itself. That reframes the whole feature:

1. **When a prime tower or flush target is detected, colour-aware purge does not emit
   `PURGE=` at all.** It only *reports* (§1.6's waste line). There is no hardware risk in this
   mode because multiACE commands no extrusion. This is the common case, and it is free.
2. **`PURGE=` emission is only for the no-tower case**, and there it is clamped by a single
   authority: a `purge_bin_capacity_mm` config value (per hardware revision — the README's v1
   vs v2 bin) enforced **inside `cmd_ACE_SWAP_HEAD`**, after a `min()` against
   `swap_purge_max` (`config.getint(minval=0, maxval=2000)`, matching the
   `swap_retract_length` style). The number in the gcode is an upper *request*, never an
   instruction — that file may have been sliced against a different machine.
3. **Track cumulative purge since the last bin empty** in `save_variables`, warn at 80 % of
   capacity and refuse above 100 % (pausing with a clear message beats blobbing). This is the
   guard that actually prevents the blob; a per-swap ceiling alone does not, because ten safe
   purges still overflow a bin.
4. **Rate-limit by volumetric flow**, derived from the material's max flow (default a
   conservative 8 mm³/s, config-overridable), never by a raw feedrate, and require
   `heater.can_extrude` immediately before every purge — including the `PURGE=`-injected path.
5. **Staged rollout, gated on evidence:** `purge_color_aware: false` → estimate-only for ≥5
   real prints → compare reported grams against what the bin actually caught → only then allow
   emission. Record the comparison in §4's history so the decision is data, not memory.

### 13.3 §6 neighbour retract — tug-of-war and hub jams

Three ways 30 cm of unwind on a neighbour slot hurts:

* **Retracting filament that is actually loaded and printing.** ACE feeder pulling against the
  extruder gear strips the filament and loads the feeder gearbox. §6.2 skips slots found in
  `self._head_source` — but that map is *persisted state* and can be stale after a crash or a
  failed load. **Cross-check the head's filament sensor** before retracting, don't trust the
  saved map alone. This is the single most important guard in §6.
* **Pulling a deformed tip back into the ACE hub.** The reason the load failed may be a blob
  or hook on the tip; dragging that back into the shared hub can jam it hard enough to need
  disassembly. Prefer clearance retracts that stay within the ACE's own path limits
  (`get_retract_length` semantics) and stop on any error rather than pushing through.
* **Slack and tangling.** 30 cm unwound without spool take-up is slack inside the unit, and
  tangles are a known ACE failure. Log cumulative mm per slot (already planned) and surface it
  so a user can see the unit needs attention.

Also: never run neighbour clearance while **another head is actively feeding from that same
ACE** — the same reason §7 gates its rescan on `_swap_in_progress` / `_auto_feed_enabled`.

#### Recommended solution

**One `_neighbor_eligible(ace, slot)` predicate, defaulting to "not eligible", and a smaller
first retract.** Two design choices carry the safety:

1. **Eligibility is a single function with a whitelist shape** — a slot is cleared only if
   *all* of these hold, and any exception or unknown reads as not eligible:
   `ace == the retrying head's ACE` (per §6.1: ACE slots only, never a stock feeder);
   not the target slot; the ACE reports filament present; the slot is **not** in
   `_head_source`; **and** the head that `_head_source` would associate with it reports its
   filament sensor clear. That last clause is the important one — it cross-checks persisted
   state against a live sensor, so a stale map after a crash cannot cause a tug-of-war. One
   function, one place to audit, called from nowhere else.
2. **Escalate, don't start at the cap.** 50 mm on retry 1, 100 mm on retry 2, 150 mm on
   retry 3 (same 300 mm cumulative cap, same "up to 30 cm" total). A jam is usually cleared by
   the first small retract; starting small means the deformed-tip-into-the-hub case drags
   5 cm of filament back, not 10 cm, and the cheapest attempt is the most likely one to run.
3. **Stop on first error, per slot and for the whole pass** — if a neighbour refuses to
   retract, that slot is done for this attempt-chain (record it), and the retry proceeds
   without it. Never re-drive a slot that already failed; that is how a hub jam becomes a hard
   jam.
4. **Report slack.** Cumulative mm per slot goes into `_retry_state_publish` *and* the §4 job
   record, with a dashboard hint at >200 mm on one slot (*"ACE 0 slot 3 has ~25 cm of slack —
   check for tangles before the next print"*). The mechanical risk here is not the retract, it
   is the unattended accumulation.

Default `load_retry_neighbor_retract: 100` as specified; `0` disables. Ship with §6's tests
extended to cover the stale-`_head_source` case explicitly — a loaded slot whose map entry was
lost must still be skipped because of the sensor cross-check.

### 13.4 Pre-existing gap: no print-state check on update

`update_apply` gates only on debug mode ([main.py:1316-1328](../../multiace/web/backend/main.py#L1316-L1328))
— **there is no check that a print is running.** Applying mid-print replaces the extruder
kinematics under a live job and restarts Klipper: the print aborts, heaters go off, and the
nozzle is left parked in molten plastic that then solidifies around it. Freeing a
welded-in nozzle is how people damage a toolhead.

This is not introduced by this plan, but §5 puts a bigger, friendlier button on that exact
path, so fix it as part of §5: query `print_stats.state` and refuse on
`printing`/`paused` unless explicitly forced, in **all three** entry points (`apply`,
`upload`, push script).

#### Recommended solution

**One shared `require_printer_idle()` dependency, and ship it before anything else in §5** —
it is a dozen lines and it closes a live gap that exists today, independent of whether the
rest of §5 ever lands.

* A single async helper in `main.py` that reads `print_stats.state` from the already-subscribed
  status and raises `409 printer is <state>` for `printing`, `paused`, and anything it does
  not recognise (**fail closed** — a Moonraker error or an unknown state must block, not
  allow; "cannot tell" and "idle" are not the same answer).
* Applied as a FastAPI dependency on `update_apply`, any future `update/upload`, and mirrored
  in the push script via `/api/state`. One implementation, three call sites — not three checks
  that drift.
* **No `force` flag on the HTTP endpoints.** There is no legitimate reason to reinstall
  Klipper extras mid-print from a browser, and a flag that exists will eventually be passed by
  a UI bug. The push script may keep `--force-mid-print` because it requires a human typing an
  unmistakable flag on a shell.
* Frontend: disable the Update button with the reason inline (*"printing — available when the
  job finishes"*) rather than letting the click fail. The server check stays regardless; the
  UI state is convenience, not the guard.

### 13.5 §8 live print control — babystep and thermal limits

New in this revision, and the two dangerous verbs are not the obvious ones.

* **Z babystep is the damaging control.** `SET_GCODE_OFFSET Z_ADJUST` moves the nozzle
  relative to the bed *while printing*. Held-down or repeated negative steps drive the nozzle
  into the plate: gouged PEI sheet, damaged nozzle, and on a probe-equipped toolhead a bent
  probe. A slider (as opposed to discrete buttons) makes it trivially easy to send a large
  negative delta in one gesture.
* **Heater setpoints.** A temperature above the material's range mid-print carbonises filament
  in the melt zone and clogs the hotend; above the hotend's limit degrades the PTFE. Klipper's
  own `max_temp` catches the extreme, but the *material* limit is tighter and Klipper knows
  nothing about it.
* **Flow.** `M221` at a high value is a sustained over-extrusion the printer will happily
  attempt — blob and grind, the same failure mode as §13.2.
* Speed, fan, pause/resume and cancel are not hardware risks (worst case: a ruined print).

#### Recommended solution

**Discrete steps for babystep, material-aware clamps for heaters, and server-side clamping
for everything.**

1. **Babystep is buttons, never a slider**: `−0.01 / −0.05 / +0.05 / +0.01 mm`, one command
   per press, with the live `homing_origin[2]` shown next to them. Cumulative movement is
   capped at ±0.5 mm per job server-side, and further negative steps are refused with an
   explicit message (*"−0.50 mm reached; re-level instead"*) — because the situation where a
   user keeps pressing "down" is precisely the situation where the next press does damage.
   Reset the accumulator on `print_stats` job change, not on page reload.
2. **Heater limits come from two sources, `min()`-ed**: Klipper's configured
   `min_temp`/`max_temp` (read from Moonraker's config, never hardcoded), and the loaded
   material's range from the existing filament/material table. Clamp server-side and return
   the applied value so the UI snaps to the truth.
3. **Clamp in the endpoint, not in JS**, for every verb — the whitelist in §8.3 is the
   contract, and it must hold against a stale page, a fat-fingered `curl`, or a slider that
   fires an out-of-range value on touch.
4. **"Purge more now" reuses §13.2's clamp path verbatim** — same bin-capacity accounting,
   same `can_extrude` check, same flow limit. It must not become a second, unguarded purge
   implementation.
5. **Confirm destructive verbs**: `cancel` gets a dialog; `pause` does not (pausing is
   recoverable, and a confirm dialog on the control you reach for in a hurry is its own
   hazard).

### 13.6 Assessed and *not* a hardware risk

Stated so these don't get re-litigated later: §1 estimates and the §1.4 M73/ETA rewrite are
read/annotate-only (worst case: a wrong progress bar); §2 mock mode never contacts the
printer; §4 history is bounded append-only JSONL (flash wear is negligible at a few KB per
job, and the cap prevents filling the filesystem); §7's rescan is a directory listing plus a
gated `_open_ace`, whose worst case is a disturbed USB bus and a lost print, not damaged
hardware; §9's preview is read-only rendering — the only thing it *writes* is an uploaded file
that must never be printable (§13.6's recommendation below).

One non-mechanical hazard worth naming: if §2's **virtual loadout ever leaked into the real
rewrite path**, the printer would swap to slots holding a different material than planned and
run it at the wrong temperature — PLA at PETG temps carbonises and clogs. Keep
`check_material_availability` unconditional on the real path
([preflight_core.py:529-532](../../multiace/web/backend/preflight_core.py#L529-L532)) and keep the
virtual loadout a read-only overlay that can never write slot state.

#### Recommended solution

Two small structural rules, both cheap and both worth writing down so this section stays true
as the code grows:

1. **The virtual loadout lives in a separate field from live slot state**, all the way through
   (`virtual_slots` vs `live_slots`), and the real rewrite path reads only `live_slots`. Not
   "a flag that says which one to use" — a flag gets inverted by a bug, a field that the real
   path never reads cannot leak. Add a test that asserts the real preflight path produces an
   identical report with an arbitrary `virtual_slots` payload attached.
2. **A preview upload is not a printable file.** `print: "false"`, uploaded to a
   `multiace-preview/` subfolder, deleted when the planner closes, and swept on startup — plus
   the §13.4 idle check on the upload itself. The failure to prevent is not mechanical; it is
   a user printing an un-rewritten file from the file list a week later and watching the
   printer run four colours off one head.

---

## 14. Open questions

**Answered in this revision:** 1 (header format, §1.3), 2 (swap timings — deliberately deferred,
modelled constants for now, §1.2), 3 (purge geometry — prime tower + flush is the norm, detect
per file, §1.6), 4 (history join — filename + start time, §4.2), 5 (neighbour retract — ACE
slots only, §6.1). Remaining:

6. **Is the `/dev/serial/by-path/` entry stable across an ACE power-cycle?** Worth a
   `ls -l /dev/serial/by-path/` before and after power-cycling one unit. If the path per USB
   port is stable, the canonical index list can be persisted via `save_variables` and a
   *partial* set can lock at the **correct** indices — which would let multi-ACE users start
   printing on the units that are up. That is the real fix for §7.2.3's restriction, and it
   only needs one command's worth of evidence.
7. **Is `_hotplug_monitor` genuinely never registered?** §7.1 finding 3. If so, unplug→replug
   recovery is also missing, and the same rescan timer covers both — but confirm before
   treating it as dead code.

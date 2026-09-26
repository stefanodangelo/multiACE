# Upstream merge analysis — decay71/multiACE V1.00.1b into this fork

**Date:** 2026-09-26
**This fork:** `0.99.16b`, branch `chore/version-increment` @ `92c5e83`
**Upstream:** `decay71/multiACE` @ `cdda33b` (tag `V1.00.1b`, "Resupply Run", 2026-09-07)
**Merge base:** `daade71` (upstream `V0.99.8b`, 2026-08-19)

Analysis only — no code was changed. Conflict numbers below come from two
throwaway trial merges in temporary worktrees, both discarded.

---

## 1. Scope of the divergence

| | commits since base | files touched |
|---|---|---|
| Upstream (`daade71..upstream/main`) | 32 | 29 |
| This fork (`daade71..HEAD`) | 56 | 78 |

Upstream's 32 commits are misleading: 30 are `Update README.md` and the real
payload arrives in two "Add files via upload" drops — `cf4e538` (V1.00b) and
`0c0c735` (V1.00.1b hotfix). There is **no granular upstream history**, so a
cherry-pick strategy is unavailable; everything must be diffed file-by-file.

Raw churn is `9718 +/ 2563 -`, but upstream re-uploads whole files, so a large
slice is cosmetic. Discounting whitespace and blank lines: `9108 +/ 1181 -`,
and excluding docs/i18n the actual code delta is **7474 insertions, 261
deletions**. Three files change *only* in blank lines and line-continuation
style and can be ignored outright:

- `klipper/extras/ace_bg_swap.py` (reads as `4 +/ 98 -`, substantively zero)
- `klipper/extras/ace_tipform.py`
- `klipper/extras/filament_switch_sensor_ace.py`

### The drop is additive, not breaking (at the API surface)

| Surface | Upstream added | Upstream removed | Collides with fork additions |
|---|---|---|---|
| Klipper G-code commands | 16 | 0 | 0 |
| Web API endpoints | 4 | 0 | 0 |
| `[ace]` config options | 4 | 0 | 0 |
| i18n keys (flattened `en.json`) | 251 | 0 | 0 |

No upstream deletion of anything the fork depends on. Every conflict below is a
*collision of adjacent additions*, not a contract break — with two exceptions
called out in §4 (planner signatures, `nozzle_volume_type`).

---

## 2. What upstream actually shipped

Five clusters, in descending order of blast radius.

### A. Snapmaker firmware 1.6.0 adaptation — mixed nozzles
`kinematics/extruder_ace.py` (+166), `preflight_core.py`, `post_process_virtual_toolheads.py`, `main.py`

Stock firmware 1.6.0 added high-flow nozzles. Upstream rebased its vendored
`extruder_ace.py` onto that: `volume_type` (`standard`/`high_flow`),
`diameter_v160` with a boot-time migration that **rewrites
`*_nozzle_config.json` on first start**, `SET_NOZZLE_PROPERTIES`, a
`control/nozzle_properties` endpoint, and `INNER_HEAT_TO_LOADED_FILAMENT_TEMP`.
Downstream, a mixed-nozzle *gate* (`allowed_heads` / `nozzle_groups`) is
threaded into the loadout planners so a filament is never assigned to a
wrong-diameter nozzle.

The V1.00.1b hotfix is the tell: V1.00b called `filament_parameters` with the
1.6.0 signature and broke on 1.5.x; `0c0c735` added a runtime
`inspect.signature` probe (`_db_takes_nozzle_args`) to degrade gracefully. **This
fork's `firmware_compat.py` table tops out at 1.5.2 and has no 1.6.x entry at
all** — the fork has not been characterised against the firmware this release
targets.

### B. Path-length calibration wizard — shipped dark
`ace.py` (9 new `ACE_CALIBRATION_*` commands + `ACE_UNLOAD_ALL_CANCEL`), 4 new
endpoints (`/api/calibration`, `/start`, `/action`, `/unload-cancel`), ~700 lines
of Vue.

A guided feed/mark/retract/verify routine to measure real tube lengths per
ACE/slot/toolhead. **The UI is feature-flagged off**: `app.js` line 11 is
`const CALIBRATION_TAB = false;`. The Klipper command set and the HTTP endpoints
are live regardless, and they drive motors.

### C. RC522 RFID tag line — experimental, ACE 2 only
New `klipper/extras/ace_rc522.py` (1270 lines), `ACE_TAG_READ` / `ACE_TAG_WRITE`
/ `ACE_SET_TAG_WRITE`, `[ace] rc522` / `tag_write_format` / `tag_write_uid_sku`,
picker UI, Spoolman card-UID push.

Rotates a slot until its tag faces the antenna, reads it, binds the spool; can
also *write* tags in `openspool` or `anycubic` format. Default off (`rc522`
commented out). Reading non-Anycubic tags requires item E.

### D. Per-spool pressure advance
`ACE_PA_CALIBRATE`, `ACE_SPOOL_PA`, `ACE_SET_PA_SYNC`, `[ace] pa_sync: true`
(**on by default**), a `pa` dialog, Spoolman `extra` field push, and
`FLOW_CALIBRATE` capture.

With `pa_sync` on, PA is auto-captured from the stock flow routine onto the bound
spool and auto-applied on toolchange. This is the one new default that changes
print behaviour silently.

### E. ACE2-Open firmware patch
`ace2_ota.py` `apply_uid_patch()` + `ace2_uid_patch.json`

A pure-Python binary patcher that converts a stock ACE 2 Pro `V1.1.31` image
into `V1.1.3O` (third-party UID passthrough, ported from Simon-CR's research
repo). Self-verifying against base md5 and per-hook expected bytes.
`KNOWN_FIRMWARE` marks it `"tested": "pending - first HW flash"`.

### Minor
`ace_protocol_v2.py` decodes slot-info field 12 as `code`. `install_multiace.sh`
and the `post-commit` hook add `ace_rc522.py`. `merge_ace_cfg.py` gains a
shebang. `multiace/docs/{ENGINE_API,LOADOUT_API,SEND_TO_MULTIACE}.md` are new
(518 lines, no conflict). `multiace/README_DE.md` is **deleted** upstream — a
naive merge silently drops the fork's copy.

---

## 3. Measured conflict surface

Trial merge of `upstream/main` into `HEAD`: **13 files conflicted, 82 hunks**.
Re-run with `-X ignore-all-space`: **77 hunks** — so whitespace noise accounts
for only ~6%, and the conflicts are real.

| File | hunks | ~lines | Enclosing scopes |
|---|---:|---:|---|
| `klipper/extras/ace.py` | 23 | 896 | `cmd_ACE_SWAP_HEAD` ×6, `cmd_ACE_LOAD_HEAD` ×4, `get_status` ×3, `cmd_ACE_UNLOAD_HEAD`, `cmd_ACE_SET_HEAD_FEEDER`, `_load_slip_details`, `__init__`, module header |
| `web/frontend/index.html` | 4 | 957 | tab strip, config page, picker dialog |
| `i18n/en.json` / `de.json` | 8 + 8 | 1025 | positional only — 0 key collisions |
| `web/backend/main.py` | 9 | 267 | `_parse_state`, `preflight` ×2, `_run_preflight_pipeline`, `preflight_print`, `update_config` ×2 |
| `tools/post_process_virtual_toolheads.py` | 6 | 253 | `compute_head_mode_optimize` ×2, `compute_swap_aware_layout`, gcode `repl` ×3 |
| `web/frontend/app.js` | 6 | 132 | `configForm`, command queue, firmware panel |
| `web/backend/preflight_core.py` | 6 | 69 | `build_one_plan`, `head_mode_preview` ×2, `build_report`, `rewrite_pipeline` ×2 |
| `klipper/extras/filament_feed_ace.py` | 7 | 50 | `_do_feed` ×3, `_phase3_a_cb` ×3 |
| `kinematics/extruder_ace.py` | 2 | 36 | module constants, `__init__` |
| `README.md` | 1 | 817 | whole file |
| `VERSION`, `style.css` | 2 | 21 | trivial |

Clean auto-merges worth noting: `config/extended/ace.cfg`, `install_multiace.sh`,
`tools/git-hooks/post-commit`, `ace_protocol_v2.py`, all new `docs/`.

**The conflicts land precisely on the fork's differentiators.**
`cmd_ACE_LOAD_HEAD`, `cmd_ACE_UNLOAD_HEAD` and `cmd_ACE_SWAP_HEAD` hold the
fork's load-retry loop, PLA tip-forming choreography, snapped-tip recovery and
combo-head feeder work (commits #15–#21). Not all 23 `ace.py` hunks are
semantic — sampling the two largest found one pure adjacency (fork's inline
`FIRMWARE_COMPAT` table sitting where upstream inserted `_stop_fa_for_head`;
both sides keepable) and one genuine overlap (competing `_head_source` handling
on the feeder path, where the fork's combo `FEEDER_TAP_SOURCE` sentinel and
upstream's `self._head_source[head] = None` are mutually exclusive).

### Existing safety net
`python -m pytest tests -q` → **464 passed** on the current tree. Coverage maps
well onto the risk areas: `test_load_retry.py` (60 tests) exercises the Klipper
`ace.py` load/retry machine with stubs, `test_web_api.py` (131),
`test_swap_cost.py` (73) and `test_plan_cost.py` (29) cover the backend and
planners. This suite is the single most valuable asset for this merge — nothing
in it covers the calibration wizard, RC522, or PA. Upstream ships no tests at
all.

---

## 4. Risk assessment

Risk = probability of breaking working fork behaviour × cost of detecting it.
Hardware-damaging outcomes are rated H regardless of probability.

### High risk

| # | Item | Why H |
|---|---|---|
| H1 | **ACE2-Open firmware patch (E)** | Writes patched firmware to ACE 2 Pro hardware; upstream itself marks it untested on real hardware (`"tested": "pending - first HW flash"`). A bad flash is not revertible in software. The code is defensive (md5 + per-hook byte checks), but the *outcome* has no rollback. |
| H2 | **`ace.py` load/unload/swap conflicts** | 13 of 23 hunks sit inside the three commands carrying the fork's retry/tip-forming/combo work. These paths move filament with a hot nozzle; a mis-resolution produces jams, snapped tips, or a head that reports loaded while empty. Interleaved state (`_head_source`, `_last_load_ok`, `load_failed`) means "take theirs" is not available per-hunk. |
| H3 | **`extruder_ace.py` + firmware 1.6.0 coupling (A)** | Upstream's version presumes stock firmware 1.6.0 and **rewrites `*_nozzle_config.json` on boot** (the `diameter_v160` migration). The fork's `firmware_compat.py` has no 1.6.x entry, so the fork has no evidence it runs on that firmware. Fork and upstream independently added `nozzle_volume_type` for different reasons — the fork as a crash fix for stock `FLOW_RESET_K` (commit `ac0c9fe`), upstream as a real feature — so the conflict must be resolved toward upstream *without* losing the crash fix's guarantee that the attribute always exists. |
| H4 | **Planner signature changes** | `compute_head_mode_optimize` upstream makes `ace_heads`, `ace_num_of_head`, `num_slots` **required positional**; the fork keeps them optional and added `ace_head`, `ace_slots`, `objective`, `cost_model`. `compute_swap_aware_layout` gained `allowed_heads` upstream and `cost_model` in the fork — same slot, different purpose. Upstream's nozzle gate must be woven into the fork's cost-model search body, not bolted on. This is where `test_swap_cost.py` / `test_plan_cost.py` / `test_head_mode_16slot.py` will fail loudly, which is the good case. |

### Medium risk

| # | Item | Why M |
|---|---|---|
| M1 | **`pa_sync: true` default (D)** | Ships enabled and silently overrides slicer PA on every toolchange. Behavioural, not structural — visible as surface-quality regressions, hard to attribute. Mitigation is a one-line default flip to `false`. |
| M2 | **`main.py` preflight conflicts** | 9 hunks, 5 inside the preflight pipeline the fork rewrote (`+2106/-160` on this file). Well covered by `test_web_api.py` (131 tests), which bounds the risk. |
| M3 | **`preflight_core.py` nozzle gate** | 6 hunks; upstream's `_swap_aware()` wrapper and the `file_body_detectable` guard must be re-applied against the fork's rewritten `build_one_plan` / `rewrite_pipeline`. Contained and unit-testable. |
| M4 | **`filament_feed_ace.py`** | 7 hunks (3 real after whitespace) in `_do_feed` / `_phase3_a_cb`. Includes the V1.00.1b signature probe, which the fork *wants* — it is exactly the firmware-version tolerance the fork's compat table exists for. |
| M5 | **`post_process_virtual_toolheads.py` gcode rewriter** | 3 hunks in `repl` — the gcode-emission path. Wrong output is caught by preflight tests and the `sample_4color.gcode` fixture, but a subtle mis-merge only shows at print time. |
| M6 | **Calibration wizard, Klipper + backend half (B)** | UI is dark, so low exposure by default — but `ACE_CALIBRATION_*` and the four endpoints are live and drive motors. Merging B means shipping ~1000 lines of reachable, untested-by-this-fork motion code. Rated M only because `CALIBRATION_TAB = false` keeps it off the normal path. |
| M7 | **`app.js` / `index.html` conflicts** | 10 hunks / ~1090 lines across the tab strip, `configForm` and the picker. Both sides rewrote these heavily (fork: `+2340/-288` on `app.js`, `+1397/-619` on `index.html`). Purely cosmetic failure mode, and immediately visible — hence M not H. |

### Low risk

| # | Item | Why L |
|---|---|---|
| L1 | Whitespace-only files — `ace_bg_swap.py`, `ace_tipform.py`, `filament_switch_sensor_ace.py` | Zero substantive change. Discard upstream's version entirely. |
| L2 | New `multiace/docs/*.md` (518 lines) | No conflict, documentation only. |
| L3 | i18n — 16 hunks, ~1025 lines | 251 new upstream keys, **0 key collisions, 0 removals**. Conflicts are positional; resolvable by a scripted key-union. Side effect: fork-maintained `zh.json` will lack the 251 new keys and fall back to `en`. |
| L4 | `ace_protocol_v2.py` field-12 `code` decode | Auto-merges; additive read of a previously-unparsed field. |
| L5 | `install_multiace.sh`, `post-commit` hook | Auto-merge cleanly. `scripts/build-paxx-mod.sh` globs `multiace/klipper/extras/*.py`, so `ace_rc522.py` is bundled with no change needed. |
| L6 | `README.md`, `VERSION`, `style.css` | Fork-owned identity; keep ours, re-apply anything wanted by hand. `VERSION` → whatever the fork's next number is. |
| L7 | RC522 module itself (C), as a *file* | New file, no conflict, lazily imported, inert without `[ace] rc522: true`. Its integration points inside `ace.py` are counted in H2. |
| L8 | `multiace/README_DE.md` deletion | Cosmetic, but a naive `git merge` drops it without asking. Decide explicitly. |

---

## 5. Cross-cutting concerns

**No upstream history to bisect.** Two whole-file uploads mean that if the
merged tree misbehaves, `git bisect` cannot narrow it to an upstream change.
Every upstream feature adopted should land as its own fork commit, re-derived
from the diff, so the fork *creates* the granularity upstream didn't provide.

**Divergence is now structural, not incidental.** The fork carries 78 changed
files against upstream's 29, including whole subsystems upstream has no notion
of (`swap_cost.py`, `job_history.py`, `firmware_compat.py`, `config_changes.py`,
the `gcode_preview` worker, `scripts/paxx-overlay/`, 464 tests). Repeated
whole-tree merges will get *more* expensive, not less — each drop re-uploads
files the fork has since rewritten again.

**Firmware-version assumption is the root dependency.** Cluster A only makes
sense on stock firmware 1.6.0; the fork is characterised to 1.5.2. Adopting A
without first adding a 1.6.x row to `firmware_compat.py` — and testing on it —
means shipping a compat table that lies to users.

---

## 6. Recommended sequencing

Not a single merge commit. Take clusters in this order, each as its own branch
with `pytest` green before the next:

1. **L-only tier, no risk** (L1, L2, L4, L5, L8) — `ace_protocol_v2.py` field 12,
   `ace_rc522.py` as a dormant file, installer/hook entries, new docs. Discard
   upstream's whitespace-only files. Decide `README_DE.md` explicitly.
2. **L3 i18n** — script a key-union of upstream's 251 additions over the fork's
   839, keeping fork values on any shared key. Backfill `zh.json` or accept the
   `en` fallback.
3. **M4 firmware-tolerance probe** — take `_db_takes_nozzle_args` from the
   V1.00.1b hotfix. This is the smallest, highest-value piece of the drop: it
   makes the fork tolerant of both 1.5.x and 1.6.0 `filament_parameters`
   signatures, which the fork wants independent of everything else.
4. **H3 → H4 → M3/M5 as one vertical slice** — `nozzle_volume_type` /
   `diameter_v160`, then the `allowed_heads` gate through both planners, then
   preflight and the rewriter. These are one feature and cannot be split;
   `test_swap_cost.py`, `test_plan_cost.py`, `test_head_mode_16slot.py` and
   `test_hybrid_head_mode*.py` are the gate. Add a 1.6.x row to
   `firmware_compat.py` in the same slice.
5. **H2 `ace.py`** — hunk by hunk, classifying each as adjacency (keep both) or
   overlap (decide, and write a test). `test_load_retry.py`'s 60 tests must stay
   green; extend it for any overlap resolved in favour of upstream.
6. **M2/M7 web layer** — `main.py`, then `app.js` / `index.html` / `style.css`.
7. **M1 PA (D)** — adopt with `pa_sync` defaulted to **`false`**, opt-in.
8. **M6 calibration (B)** — last, keeping `CALIBRATION_TAB = false`, or defer.

### Defer or decline
- **H1 (ACE2-Open patch)** — do not ship until upstream reports a successful
  hardware flash. The patcher code can be merged dormant; the `KNOWN_FIRMWARE`
  entry is what makes it reachable, and that is the line to leave out.
- **M6 (calibration)** — upstream ships it dark. There is no reason for this fork
  to carry ~1000 lines of live motion code that upstream itself doesn't expose.
  Revisit when upstream flips the flag.
- **C (RC522 tag *writing*)** — reading is cheap and inert. Writing tags is
  destructive to user hardware (spool tags) and gated behind H1 for
  non-Anycubic formats. Take the read path, leave `ACE_TAG_WRITE` behind the
  default-off config.

---

## 7. Verification gates

| Gate | Applies to |
|---|---|
| `python -m pytest tests -q` = 464+ passed | every step |
| No new divergence between `firmware_compat.py` and the inlined `FIRMWARE_COMPAT` mirror in `ace.py` | steps 4, 5 |
| Preflight a known 4-colour file (`tests/fixtures/sample_4color.gcode`) and diff the emitted gcode against pre-merge output | steps 4, 6 |
| Dry-run load / unload / swap on each head, plus one deliberate failed load, on a real printer | step 5 |
| `scripts/build-paxx-mod.sh` produces a bin and `scripts/push-to-printer.sh` deploys it | step 1 onward |
| Boot Klipper once and confirm the `*_nozzle_config.json` migration wrote sane values | step 4 |

---

## 8. Bottom line

The drop is **additive at every API surface** — 16 new G-code commands, 4 new
endpoints, 4 new config options, 251 new i18n keys, and **nothing removed**.
That is the good news, and it means there is no forced breakage.

The bad news is that the 77 real conflict hunks sit almost exactly on the code
this fork exists to change: the load/unload/swap state machine and the loadout
planners. A single `git merge` is not viable — it would require adjudicating
~900 conflicted lines inside filament-motion code in one sitting.

The genuinely valuable content for this fork is narrow: the **firmware-signature
probe** (step 3) and the **mixed-nozzle gate** (step 4). Both are small, both
are covered by the existing 464-test suite, and together they are perhaps 400
lines of the 7474. The rest — calibration, RFID, PA, the firmware patch — is
either shipped dark upstream, untested on hardware, or off by default, and can
be adopted incrementally or declined without cost.

Recommended posture: **selective adoption in the eight steps above, not a merge.**
Budget the effort against steps 4 and 5; everything else is mechanical.

---

## 9. Adoption log

Executed on branch `feature/upstream-v1.00.1b-selective-adoption`, one commit
per step, `pytest` green (464 → 488 passed) after each:

| Step | Status | Notes |
|---|---|---|
| 1. L-tier | **Done** | Field-12 `code` decode, dormant `ace_rc522.py`, new docs, installer/hook entries. `README_DE.md` kept explicitly. |
| 2. i18n (L3) | **Done** | Scripted key-union, 270/255 new keys (en/de), fork wording kept on the 5 shared-key overlaps per language. `zh.json` untouched — already falls back through `main.py`'s existing `get_i18n()`. |
| 3. Firmware probe (M4) | **Done** | `_db_takes_nozzle_args` in `filament_feed_ace.py`, narrowly scoped to just the signature probe. |
| 4. Mixed-nozzle gate (H3/H4/M3/M5) | **Done** | `nozzle_volume_type`/`diameter_v160` reconciled with the fork's own crash-fix attribute; `allowed_heads` threaded through `compute_swap_aware_layout`; `file_body_detectable` guard; `firmware_compat.py` 1.6.x row marked **untested**, not supported. |
| 5. `ace.py` (H2) | **Done** | Real trial merge in a disposable worktree to get the true 23-hunk conflict set. Only genuinely new content: `_stop_fa_for_head()`. Everything else was either adjacency (fork already has it), pre-existing shared code the diff falsely flagged as new, or the combo-mode overlap resolved in the fork's favour (combo is disabled but the gating stays for a future re-enable). |
| 6. Web layer (M2/M7) | **Done, narrow** | `_color_to_hex` declared-black fix; `head_nozzle_types` field (backend-only, no UI consumer yet — the fork's existing UI already has full diameter-mismatch awareness that the step-4 gate protects transparently). Upstream's calibration/PA/RC522 UI and the two-sided app.js/index.html rewrite were not adopted. |
| 7. PA (D) | **Deferred** (user decision, 2026-09-26) | Hooks stock Klipper's flow-calibrator at runtime (`_install_flow_calibrator_hook`/`_wrap_flow_calibrate`); its new commands would be live regardless of `pa_sync`'s default, with zero test coverage and no hardware validation path — same risk shape as the calibration wizard. Revisit with real-hardware testing available. |
| 8. Calibration (B) | **Declined** | Per the plan's own recommendation: ~1000 lines of live, motor-driving code upstream itself ships dark (`CALIBRATION_TAB = false`). No reason for this fork to carry it. |
| H1 (ACE2-Open patch) | **Declined, more conservative than the plan** | The plan allowed landing the patcher file dormant; this pass declined even that, since it adds no value without a hardware-validated `KNOWN_FIRMWARE` entry and the outcome of a bad flash has no rollback. |
| C (RC522 write) | **Declined** | Per the plan. The dormant `ace_rc522.py` module (step 1) is unwired — read-path wiring itself was also declined in step 5 because `cmd_ACE_TAG_READ` gates on the declined ACE2-Open firmware. |

Not verifiable from this environment (no target hardware): a real boot on
1.6.0 firmware to confirm the `*_nozzle_config.json` migration, and dry-run
load/unload/swap on each head. Both remain open per §7's verification gates
before treating the 1.6.x firmware row as anything but "untested".

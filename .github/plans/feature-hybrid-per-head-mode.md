# Hybrid Per-Head Mode (ACE + stock feeder combined on one head via a Y-splitter)

**Status:** Draft
**Date:** 2026-08-25
**Scope:** Let a single physical head be fed by *both* an ACE unit (up to 4 slots)
*and* its stock side feeder, spliced together with a Y-splitter, so that head can
swap between all of them mid-print. Existing per-head ("head") mode already lets
each head be *either* ACE-driven *or* stock-feeder-driven; this adds the *both, on
the same head* case, plus the config knobs the added splitter tube length requires.
**Predecessor:** [feature-ux-improvements.md](feature-ux-improvements.md) (head
mode itself), [feature-estimates-loadout-history.md](feature-estimates-loadout-history.md)
(§3.1's `feeder_pin` cost kind and the `ace_slots`/combiner-head planner primitive
this plan reuses).

---

## 0. The physical setup, and why it changes the colour budget

Today, "head" mode wires each of the 4 print heads to **at most one** source:

* an ACE, via `head_ace[head] = ace_index` (`ACE_SET_HEAD_ACE`,
  [ace.py:10905-10949](../../multiace/klipper/extras/ace.py#L10905-L10949)) - up to
  4 colours (its ACE's 4 slots), swappable mid-print via `ACE_SWAP_HEAD`; or
* the stock side feeder, via `head_feeder[head] = True` (`ACE_SET_HEAD_FEEDER`,
  [ace.py:10838-10903](../../multiace/klipper/extras/ace.py#L10838-L10903)) - exactly
  1 colour, loaded once, **never** swapped mid-print (`head_uses_ace(head)` returns
  `False` the moment `head_is_feeder(head)` is `True`,
  [ace.py:10665-10671](../../multiace/klipper/extras/ace.py#L10665-L10671); the
  planner encodes the same rule as "F feeder heads, capacity 1, pinned, NEVER
  swaps" in `compute_head_mode_optimize`
  [post_process_virtual_toolheads.py:775-777](../../multiace/tools/post_process_virtual_toolheads.py#L775-L777)).

These two are mutually exclusive **per head** today. You wired one ACE Pro 2 to
one side of a Y-splitter and the stock feeder to the other side of the same
splitter, both feeding the same head. That is a third, currently unrepresented
state: **a head that is ACE-driven AND has the stock feeder spliced onto the same
path**, so it can swap between its 4 ACE slots *and* the feeder spool, all on one
physical head.

Colour budget, generalizing your two examples (4 physical heads, S=4 slots/ACE):

| Config | Heads with ACE+feeder combo | Feeder-only heads | Colours |
|---|---|---|---|
| 1 ACE, combo on head 1 | 1 × (4+1) | 3 × 1 | **8** |
| 2 ACEs, combo on heads 1-2 | 2 × (4+1) | 2 × 1 | **12** |
| 3 ACEs, combo on heads 1-3 | 3 × (4+1) | 1 × 1 | 16 |
| 4 ACEs, combo on all 4 | 4 × (4+1) | 0 | 20 |

General form with `N` combo heads out of `H=4`: `colours = N*(S+1) + (H-N)`, i.e.
`N*5 + (4-N)` for S=4, which simplifies to **`4N + 4`** when every ACE head opts
into the combo. §5 explains why the last row (20) may not be reachable without a
slicer-side change, and why 16 (3 combo ACEs) is the safe target for phase 1.

This is a **per-head opt-in**, not a fifth top-level mode: the config below adds a
new independent flag next to `head_manual` / `head_feeder` / `head_ace`, so a
printer can mix plain ACE heads, plain feeder heads, and combo heads freely, and
`_ace_mode` stays exactly `'normal' | 'multi' | 'head'` as it is today.

---

## 1. New per-head state: `head_feeder_combo`

### 1.1 Config and persistence (mirrors `head_manual` / `head_feeder` exactly)

```ini
[ace]
# Head mode only. A head that is ACE-driven (head_ace) AND has the stock
# feeder spliced onto the SAME path via a Y-splitter, so it can swap between
# its ACE's slots and the feeder spool mid-print. Requires head_ace to be set
# and head_feeder/head_manual to be false for that head.
# Live: web "Combo (ACE + feeder)" checkbox / ACE_SET_HEAD_FEEDER_COMBO
#head_feeder_combo_0: False
#head_feeder_combo_1: False
#head_feeder_combo_2: False
#head_feeder_combo_3: False
```

* `self.head_feeder_combo = {}` populated in `__init__` next to
  `self.head_feeder`/`self.head_manual`
  ([ace.py:469-490](../../multiace/klipper/extras/ace.py#L469-L490)):
  `config.getboolean('head_feeder_combo_%d' % i, False)`.
* `VARS_ACE_HEAD_FEEDER_COMBO = 'ace__head_feeder_combo'`, next to
  `VARS_ACE_HEAD_MANUAL`/`VARS_ACE_HEAD_FEEDER`
  ([ace.py:258-260](../../multiace/klipper/extras/ace.py#L258-L260)); `_save_head_feeder_combo`
  / `_restore_head_feeder_combo` are copy-paste of
  [ace.py:11051-11079](../../multiace/klipper/extras/ace.py#L11051-L11079) (`_save_head_feeder`/
  `_restore_head_feeder`).
* New command, same shape as the three existing `ACE_SET_HEAD_*` setters
  ([ace.py:10806-10949](../../multiace/klipper/extras/ace.py#L10806-L10949)):

  ```
  ACE_SET_HEAD_FEEDER_COMBO HEAD=0..3 ENABLE=0|1
  ```

  Guard rails, checked in this order (reuse `_head_is_loaded` /
  `_head_loaded_refusal_info` exactly as the existing setters do):
  1. Refuse if the head is currently loaded (either source) - same loaded-check
     as `ACE_SET_HEAD_FEEDER`/`ACE_SET_HEAD_ACE`.
  2. Refuse `ENABLE=1` unless `head_uses_ace(head)` is already true (i.e.
     `head_ace` is set and the head is neither manual nor feeder-only) - a combo
     head is defined as "ACE head, plus a feeder tap", not a replacement for
     `head_feeder`.
  3. `ENABLE=0` just drops the tap; if the head's *current* source was the
     feeder tap (§2), force an unload first (same pattern as the stale
     `head_source` clear on `ACE_SET_HEAD_MANUAL`/`ACE_SET_HEAD_FEEDER`,
     [ace.py:10822-10827](../../multiace/klipper/extras/ace.py#L10822-L10827)).

### 1.2 Mode-switch interaction

`cmd_ACE_RUN_MODE_SWITCH` ([ace.py:14567+](../../multiace/klipper/extras/ace.py#L14567))
only ever sets `head_feeder[h] = (h != legacy_head)` on **entry** to head mode
([ace.py:14587-14589](../../multiace/klipper/extras/ace.py#L14587-L14589) and
:14620-14624) and never touches `head_feeder_combo` - correct, since combo is an
opt-in refinement the user applies *after* head mode is already configured, not
part of the mode-entry default. On **exit** to `'multi'`, `head_feeder_combo`
should be cleared the same way `_convert_feeder_to_manual` unwinds `head_feeder`
([ace.py:14526-14557](../../multiace/klipper/extras/ace.py#L14526-L14557)) - multi
mode has no concept of a per-head feeder tap at all, so a stale combo flag must
not survive the round trip back into head mode (mirror the `_heads_manual_conv`
memo pattern only if a round-trip-preserving UX is wanted; simplest correct
behaviour is "combo always resets to off on `MODE=multi`", called out as an
explicit line in `SET_ACE_MODE`'s help text).

---

## 2. Addressing the feeder tap: `_head_source` sentinel + `ACE_SWAP_HEAD SOURCE=`

### 2.1 The sentinel

`_head_source[head]` is `None` or `{'ace_index': int, 'slot': int, ...}` today.
Add one sentinel value for "this head is currently fed from its combo feeder tap,
not an ACE slot":

```python
FEEDER_TAP_SOURCE = {'ace_index': None, 'slot': 'feeder'}
```

`slot: 'feeder'` (a string, not an int) rather than a magic int like `-1` is
deliberate: every `isinstance(x, int)` guard already scattered through the
codebase (e.g. `_ace_slot_for_head`,
[ace.py:6858-6885](../../multiace/klipper/extras/ace.py#L6858-L6885)) then fails
closed instead of silently treating `-1` as slot -1 on some ACE.

**This sentinel is the one change with the widest blast radius in the whole
plan.** Every reader of `_head_source[head]` that assumes `source['ace_index']`
and `source['slot']` are both ints must be audited before this ships. Confirmed
call sites from this pass (not exhaustive - grep `_head_source` fresh before
implementing, this file is 15k+ lines and grows):

| Site | What it does today | Fix |
|---|---|---|
| `_ensure_active_ace_for_head` [:10685-10708](../../multiace/klipper/extras/ace.py#L10685-L10708) | reads `src.get('ace_index')` as target ACE to activate | short-circuit: no ACE to activate when source is the feeder tap, return the already-active index unchanged |
| `_ace_slot_for_head` [:6858-6885](../../multiace/klipper/extras/ace.py#L6858-L6885) | `isinstance(s, int)` guard already present | **already safe** - falls through to the ACE-armed-slot path, which is wrong for a feeder-sourced head; add an explicit early `if src is FEEDER_TAP_SOURCE (or src.get('slot')=='feeder'): return 'feeder'` and make every caller of this function handle a `'feeder'` return (see the `filament_feed_ace.py` unload path below) |
| `cmd_ACE_SWAP_HEAD` "already on ACE/slot" no-op check [:13154-13158](../../multiace/klipper/extras/ace.py#L13154-L13158) | compares `source['ace_index'] == ace_index and source['slot'] == slot` | naturally `False` when source is the feeder sentinel and target is a real ACE (correct - it's a real swap); needs a parallel check on the `SOURCE=FEEDER` branch (§2.2) for "already on the feeder tap" |
| swap-kind classification [:13291-13294](../../multiace/klipper/extras/ace.py#L13291-L13294) (`_job_swap_kind`) | `'same_ace' if prev_ace_src == ace_index else 'cross_ace_inline'` | add a `'feeder_to_ace'` / `'ace_to_feeder'` kind - both are same-head, foreground, no other head takes over, closest existing analogue is `'same_ace'` cost-wise (§5 keeps this consistent with the estimates plan's cost-kind enum) |
| `_head_is_loaded` [:10763-10804](../../multiace/klipper/extras/ace.py#L10763-L10804) | `src = self._head_source.get(head) if self.head_uses_ace(head) else None` | a combo head is still `head_uses_ace(head) == True`, so this already reads `_head_source` correctly for it; the sentinel just needs to be truthy (it is - a non-empty dict), so no change needed here, only verify by test |
| ghost-head detection, ghost-head refusal message [:13145-13152](../../multiace/klipper/extras/ace.py#L13145-L13152) | keys off `_head_source` presence | unaffected - a ghost head has *no* recorded source at all, sentinel or otherwise |
| `_save_head_source`/`_restore_head_source` [:10633-10648](../../multiace/klipper/extras/ace.py#L10633-L10648) | `json.dumps`/`literal_eval` round-trip of the raw dict | the sentinel is plain JSON-safe (`{"ace_index": null, "slot": "feeder"}`) - **no format change**, confirmed by the function's own comment that on-disk format already tolerates arbitrary dict shapes |
| `filament_feed_ace.py` FEED_ACT_UNLOAD "doing" stage [:2207-2222](../../multiace/klipper/extras/filament_feed_ace.py#L2207-L2222) (and the mirrored block around :2510-2525) | `source = self.ace._head_source.get(head_idx)`; `if source and source['ace_index'] != active: switch ACE`; `_full_retract = self.ace._resolve_retract_length(_ace_slot)` | must special-case the sentinel: skip the ACE-switch entirely, and use the new `feeder_retract_length` (§3.2) instead of `_resolve_retract_length` |
| `main.py` wiring/state builder [:1144-1170](../../multiace/web/backend/main.py#L1144-L1170) | reads `head_ace` to build the wiring overlay | needs a `head_feeder_combo` pass-through and a per-head "currently on: ace slot N / feeder" readout, sourced from `_head_source` the same way `head_source_known`/`load_failed` are already surfaced (`main.py:570` per the existing `_head_is_loaded` docstring) |

### 2.2 `ACE_SWAP_HEAD` gets a `SOURCE=` parameter

```
ACE_SWAP_HEAD HEAD=<0..3> SOURCE=ACE ACE=<0..3> SLOT=<0..3>   ; unchanged default
ACE_SWAP_HEAD HEAD=<0..3> SOURCE=FEEDER                       ; new
```

`SOURCE` defaults to `ACE` so every existing call site, every already-sliced
gcode file, and every Fluidd macro keeps working unmodified - this is additive,
not a breaking change to the command's signature.

Rejected alternative: overload `ACE=-1` as "use the feeder". `ACE` is validated
with `_ensure_ace_available(ace_index)` and `ace_index < 0` immediately at
[ace.py:13128](../../multiace/klipper/extras/ace.py#L13128), so a sentinel int
would need a special-case *before* that call anyway - `SOURCE=` is no more code
and is self-documenting in saved gcode and in the audit log.

`cmd_ACE_SWAP_HEAD` changes ([ace.py:13079+](../../multiace/klipper/extras/ace.py#L13079)):

1. Parse `source_kind = gcmd.get('SOURCE', 'ACE').upper()`.
2. `SOURCE=FEEDER` requires `self.head_feeder_combo.get(head)` - else
   `_ace_error(..., 'head %d has no feeder tap - enable with '
   'ACE_SET_HEAD_FEEDER_COMBO first', code=209)` (next free code after the
   existing 207/208 used two lines up).
3. Skip every `ACE=`/`SLOT=` validation block
   ([ace.py:13128-13144](../../multiace/klipper/extras/ace.py#L13128-L13144)) on
   the feeder branch - there is no ACE index or gate status to check.
4. The "already there" no-op check becomes: already-feeder-sourced +
   `SOURCE=FEEDER` → log-and-return (mirrors
   [:13155-13158](../../multiace/klipper/extras/ace.py#L13155-L13158)); already
   ACE-sourced + `SOURCE=FEEDER` → real swap.
5. The unload phase (retracting whatever is currently in the head) already
   branches on `prev_source` implicitly via `_ace_slot_for_head` /
   `_resolve_retract_length`; route it through the sentinel-aware fix from the
   table above instead of assuming an ACE slot.
6. The load phase: instead of the ACE serial protocol's feed sequence, invoke
   the **existing** native side-feed loop
   ([filament_feed_ace.py:1159-1230](../../multiace/klipper/extras/filament_feed_ace.py#L1159-L1230),
   already reached for any head where `head_uses_ace(head)` is `False`) via a new
   `self.ace._request_feeder_tap_load(head)` → `FEED_AUTO LOAD` mux command on
   the owning `filament_feed` module (same command `head_is_feeder` heads already
   use for their one-time initial load; the new part is calling it as the *load*
   half of a *mid-print* swap, with the surrounding heat/position/pause ceremony
   `ACE_SWAP_HEAD` already provides for ACE targets).
7. On success, `self._head_source[head] = dict(FEEDER_TAP_SOURCE)`;
   `_save_head_source()`.

### 2.3 Background swaps are out of scope for the feeder leg

The experimental parked-position background swap (README: "Parked position
background swaps (per Head mode only)") preloads a colour into an **idle** head's
park position while another head prints. A feeder-tap swap on a combo head does
not change which head is idle - it changes which of that *same* head's two
sources is feeding it - so it is always a foreground, blocking swap, same tier as
today's same-ACE slot change. `head_mode_bg_stats` /
`_wait_bg_op` need no new logic here; just make sure the new `'feeder_to_ace'` /
`'ace_to_feeder'` swap kinds are **excluded** from whatever set the bg planner
treats as background-eligible (an explicit test, not an assumption - see §7).

---

## 3. `filament_feed_ace.py`: the splitter adds tube length on the feeder side too

### 3.1 Why `load_length` for the stock feeder is not configurable today

The stock/native feed loop already used for every plain feeder head's initial
load ([filament_feed_ace.py:1159-1230](../../multiace/klipper/extras/filament_feed_ace.py#L1159-L1230))
is sensor-stopped (`runout_sensor[ch]`, `port_detect`), exactly like the ACE's own
load - overshoot is safe. But it is bounded by a **single hardcoded module
constant**, not a config value:

```python
FEED_LOAD_LENGTH_MAX = 1100.0   # filament_feed_ace.py:108
...
self._feed_load_counts_max = int(FEED_LOAD_LENGTH_MAX / FEED_WHEEL_CIRCUMFERENCE * 2)  # :579, computed ONCE in __init__
...
if (wheel_cnt_a_2 - wheel_cnt_a_0) / self.wheel[ch].ppr > self._feed_load_counts_max or ...:
    self.channel_error[ch] = FEED_ERR_DISTANCE   # :1216-1220
```

That ceiling exists to catch a genuine jam/no-filament condition, not to hit a
precise stop (the sensor does that) - but it is one number shared by **every**
feeder channel on the printer, computed once at startup from a constant that has
no config knob anywhere in `ace.cfg` or the stock `[filament_feed ...]` section.
A Y-splitter on a combo head adds real tube length between the stock spool holder
and the splice point, and the previously-fine 1100 mm ceiling can now trip
`FEED_ERR_DISTANCE` before the runout sensor ever sees the tip - a load that
would have succeeded now hard-fails.

**Important:** `[filament_feed ...]` is a *stock* Snapmaker Klipper config
section (each instance covers two physical channels via `filament_ch_1`/
`filament_ch_2`, [filament_feed_ace.py:441-443](../../multiace/klipper/extras/filament_feed_ace.py#L441-L443));
multiACE does not ship or own that file, and per the README's own warning
(`filament_feed.py` is one of the files a firmware update overwrites), any new
key added there would be silently lost on the next Snapmaker firmware update.
The config must live in **`ace.cfg`**, which multiACE does own, and be read
cross-module the same way `filament_feed_ace.py` already reaches into `ace.py`
for other things (`self.ace = self.printer.lookup_object('ace')` at `_ready()`,
[filament_feed_ace.py:597-600](../../multiace/klipper/extras/filament_feed_ace.py#L597-L600);
it already calls `self.ace.head_uses_ace(...)`, `self.ace._resolve_retract_length(...)`,
`self.ace._v2_arm_fa_for_unload(...)`, etc.).

### 3.2 New config, in `ace.cfg`, read via `self.ace`

```ini
[ace]
# Native stock-feeder load distance ceiling (mm). Only matters for
# stock-feeder-only heads (head_feeder) and combo heads' feeder tap
# (head_feeder_combo) - a Y-splitter adds real tube length here, and the
# stock default (1100mm) can now trip a false "distance exceeded" error
# before the runout sensor ever triggers. Sensor-stopped like ACE loads;
# overshoot is safe, this is only the abort ceiling. Per-head override
# below matters once a splitter is added to only SOME heads.
feeder_load_length: 1100
#feeder_load_length_0: 1350
#feeder_load_length_1: 1350

# Retract distance (mm) to pull the feeder-side filament clear of the
# Y-splitter junction before the ACE feeds through the same shared path -
# the feeder-side equivalent of retract_length/swap_retract_length, but a
# physically different (usually much shorter) run since the splice on a
# combo head is normally near the head, not back at the ACE. Defaults to
# retract_length for backward compatibility (today's incidental reuse via
# _resolve_retract_length, kept as the fallback so an upgrade with no new
# config changes no existing behaviour).
#feeder_retract_length: 150
#feeder_retract_length_0: 80
feeder_swap_retract_length: 150
#feeder_swap_retract_length_0: 80
```

New `ace.py` accessors, mirroring `get_retract_length`/`get_swap_retract_length`
([ace.py:2314-2334](../../multiace/klipper/extras/ace.py#L2314-L2334)) exactly -
global → per-head override, same precedence order:

```python
def feeder_load_length_for(self, head): ...       # feeder_load_length_N -> feeder_load_length
def feeder_retract_length_for(self, head): ...     # feeder_retract_length_N -> feeder_retract_length -> retract_length (compat fallback)
def get_feeder_swap_retract_length(self, head): ... # feeder_swap_retract_length_N -> feeder_swap_retract_length
```

### 3.3 Code changes in `filament_feed_ace.py`

* [:579](../../multiace/klipper/extras/filament_feed_ace.py#L579): stop
  precomputing a single `self._feed_load_counts_max` for the whole module
  instance (it currently serves 2 channels / 2 heads with one shared ceiling).
  Compute it **per channel, at load time**, from
  `self.ace.feeder_load_length_for(self.filament_ch[ch])` when `self.ace is not
  None`, falling back to the existing `FEED_LOAD_LENGTH_MAX` constant when
  `self.ace` is unset (defensive - this module can theoretically load before
  `ace` in some install orders; `_ready()` already guards other `self.ace`
  uses the same way).
* [:2207-2222](../../multiace/klipper/extras/filament_feed_ace.py#L2207-L2222)
  and the mirrored block near :2510-2525: when `source` is the `FEEDER_TAP_SOURCE`
  sentinel (§2.1), the retract being computed here is the feeder leg pulling back
  from the splitter, not the ACE bowden - use
  `self.ace.get_feeder_swap_retract_length(head_idx)` /
  `self.ace.feeder_retract_length_for(head_idx)` instead of
  `self.ace._resolve_retract_length(_ace_slot)`. When `source` is `None` (a plain,
  non-combo feeder head, today's only case reaching this branch) - unchanged,
  keep resolving through `_resolve_retract_length` exactly as today, so this is
  additive for existing single-purpose feeder heads.
* No change to `FEED_UNLOAD_PROBE_RETRACT` itself (it stays the "short probe"
  fraction of whichever full-retract value is now in play - the `min(...)`
  logic at [:2222](../../multiace/klipper/extras/filament_feed_ace.py#L2222) is
  unaffected).

---

## 4. Planner / post-processor: the feeder tap becomes a *swappable* bin, not a pin

### 4.1 What already generalizes, almost for free

`compute_head_mode_optimize` already models "F feeder heads, capacity 1, pinned,
never swap" plus "K ACE heads, capacity `num_slots`, swappable" as two bin
classes, and already has an `ace_head`/`ace_slots` convention for **combining
multiple physical ACE units' slots onto one logical head**
([post_process_virtual_toolheads.py:764-826](../../multiace/tools/post_process_virtual_toolheads.py#L764-L826),
exercised today by `tests/unit/test_head_mode_16slot.py` for the "N ACE units
hardware-combined onto one head" case - a different hardware topology than this
plan, but the same *planner* abstraction: a head's capacity is `len(ace_slots)`
physical slots it can pre-load and swap between).

**Recommended solution:** model a combo head's feeder tap as **one more slot in
that same pool**, with a fixed, non-reassignable identity (whatever spool is
physically in the stock feeder), rather than inventing a fourth top-level
assignment kind:

* `ace_slots` for a combo head gains one synthetic entry:
  `{'ace': None, 'slot': 'feeder', 'material': ..., 'color': ...}` sourced from
  the head's *pinned* identity (exactly the `pinned_heads` colour/material a
  plain feeder head already provides,
  [post_process_virtual_toolheads.py:571-577](../../multiace/tools/post_process_virtual_toolheads.py#L571-L577)) -
  so combo heads keep using the *same* "what's physically loaded" input as
  plain feeder heads, just folded into the ACE-slot pool instead of the
  pinned-head pool.
* Capacity for a combo head's bin becomes `num_slots + 1` instead of
  `num_slots`; matching happens by colour exactly as any other slot, with the
  one hard constraint that the `slot='feeder'` entry's colour/material is fixed
  input (matches `pinned_heads` today), never assigned a *different* colour -
  it is not one more ACE colour you get to choose, it is "whatever spool is on
  the stock holder right now."
* Assignment entries keep `'kind': 'ace'` and just carry `'slot': 'feeder'`
  instead of an int - `head_mode_swap_count`
  ([:656-673](../../multiace/tools/post_process_virtual_toolheads.py#L656-L673))
  already keys changes off `(ace, slot)` tuples, so `(None, 'feeder') != (a, s)`
  falls out correctly with **no change** to the swap-counting logic itself.
* `rewrite_head_mode_to_file`
  ([:1425-1430](../../multiace/tools/post_process_virtual_toolheads.py#L1425-L1430))
  needs exactly one new branch at the point it emits `ACE_SWAP_HEAD HEAD=.. ACE=..
  SLOT=..`: when `entry['slot'] == 'feeder'`, emit
  `ACE_SWAP_HEAD HEAD=<head> SOURCE=FEEDER` instead (§2.2).
* `head_mode_bg_stats` must explicitly exclude any transition touching a
  `slot == 'feeder'` entry from the bg-eligible set (§2.3) - add this as an
  assertion in the new test, not just a comment, since a silent future change to
  the bg-eligibility predicate could otherwise re-admit it.
* Cost model (`swap_cost.SwapCostModel`, from the estimates plan): add a
  `'feeder_swap'` kind alongside `'same_ace'` - same tier (foreground, no other
  head takes over), separate name so `swap_seconds()` can eventually be tuned
  independently once real data exists (the feeder's local drive gear vs. the
  ACE's long bowden are physically different swap durations).

### 4.2 Addressing ceiling: T-number space, not a code limit

The slicer emits virtual toolheads `T4..T15` today (16 total, README:342) -
head mode remaps *whichever* of these the slicer used to a physical head + ACE
slot by colour match, it does not use the `T = head + 4*ace` arithmetic formula
(that formula is `'multi'` mode's addressing, see
[:2543](../../multiace/tools/post_process_virtual_toolheads.py#L2543) and
[:3631](../../multiace/tools/post_process_virtual_toolheads.py#L3631) vs.
`compute_head_mode_layout`'s colour-matching approach,
[:561-560](../../multiace/tools/post_process_virtual_toolheads.py#L561)). So the
real ceiling on total logical colours in head mode is however many distinct
`T`s the slicer's virtual-tool profile emits - **16**, per the documented
`T4..T15` convention.

From §0's table: 3 combo ACEs = 16 colours exactly (fits); 4 combo ACEs = 20
(does not fit `T0..T15` without the slicer emitting `T16..T19`, which needs a
bigger virtual-extruder profile on the slicer side - **unverified whether Orca /
Snapmaker Orca support that**, flagged as an open question, §9). **Phase 1
targets configurations that fit within `T0..T15`** (up to 3 combo ACEs, or 4
combo ACEs with at least one head plain-feeder-only); expanding past 16 is a
later phase gated on confirming slicer support, not a code blocker in
`post_process_virtual_toolheads.py` itself (the `bare_t` regex already accepts
2-digit tool numbers, [:1456](../../multiace/tools/post_process_virtual_toolheads.py#L1456)).

---

## 5. Web UI / API

### 5.1 Backend (`main.py`)

* `head_feeder_combo` surfaced next to `head_feeder`/`head_ace` in the state
  builder ([main.py:357-358, 718-719](../../multiace/web/backend/main.py#L357-L719))
  and in the wiring-info block ([:1144-1170](../../multiace/web/backend/main.py#L1144-L1170)).
* New endpoint mirroring `head_feeder_set`
  ([main.py:4914-4920](../../multiace/web/backend/main.py#L4914-L4920)):
  `POST /api/head-feeder-combo` → `ACE_SET_HEAD_FEEDER_COMBO HEAD=%d ENABLE=%d`.
* Config panel exposes `feeder_load_length[_N]`, `feeder_retract_length[_N]`,
  `feeder_swap_retract_length[_N]` the same way per-ACE `load_length_N` overrides
  are already editable (write-through to `ace.cfg`, per the file's own "Feature
  toggles... WRITE their config line here directly" convention,
  [ace.cfg:30-33](../../multiace/config/extended/ace.cfg#L30-L33)).

### 5.2 Frontend (`app.js`)

* Per-head-ACE-card: a **"Combo: also feed from stock feeder"** checkbox next
  to the existing ACE dropdown (`setHeadAce` /
  `aceOptionsForHead`, [app.js:2349-2366](../../multiace/web/frontend/app.js#L2349-L2366)),
  enabled only once that head has an ACE wired and is not manual/feeder-only.
* Wiring overlay (`wiringPaths`/`state.wiring`,
  [app.js:584-626](../../multiace/web/frontend/app.js#L584-L626)): draw a second
  line into a combo head from a small "feeder" glyph, distinct from the existing
  ACE-slot lines, so the Y-splitter is visually obvious rather than looking like
  a plain feeder head that also happens to have an ACE dropdown.
* Reuse `loadFeederHead` ([app.js:2438](../../multiace/web/frontend/app.js#L2438),
  "load a feeder head via its native stock side feeder (no ACE)") for a combo
  head's feeder-tap load action - the button needs to become available on combo
  heads too, not gated to `head_feeder`-only heads as it presumably is today
  (verify the exact gating condition before changing it).
* Plan editor / loadout UI (from the estimates plan, §3.3 there): a combo
  head's feeder-tap colour shows as one more swatch in that head's lane,
  distinguishable from its ACE slots (e.g. a small feeder icon on the swatch),
  since §4.1 makes it participate in the same swap timeline as the ACE slots.

---

## 6. Fluidd macros / console

* `SET_ACE_MODE` help text and `ACE_RUN_MODE_SWITCH` help
  ([ace.cfg:212-217](../../multiace/config/extended/ace.cfg#L212-L217),
  [ace.py:14498](../../multiace/klipper/extras/ace.py#L14498)) gain one line
  pointing at the new setter, matching how `ACE_SET_HEAD_MANUAL`/
  `ACE_SET_HEAD_FEEDER`/`ACE_SET_HEAD_ACE` are already cross-referenced from
  `ACE_SPOOL_ASSIGN`'s help text
  ([ace.py:9840](../../multiace/klipper/extras/ace.py#L9840)).
* No new Fluidd macro button is needed for `ACE_SET_HEAD_FEEDER_COMBO` itself
  (it is a one-time-per-head config action, same class as `ACE_SET_HEAD_ACE`,
  which also has no dedicated macro - console/web only). `ACE_SWAP_HEAD
  SOURCE=FEEDER` is only ever emitted by the rewrite, same as today's plain
  `ACE_SWAP_HEAD` calls.

---

## 7. Tests

* `tests/unit/test_head_mode_16slot.py` (extend): a combo head's synthetic
  `slot='feeder'` entry participates in the existing capacity/feasibility
  assertions - add a case with `N` combo ACEs + `(4-N)` feeder heads hitting
  exactly 16 total colours (§0's row 3) and confirm it is feasible, and a
  17th colour on the same layout is not.
* New `tests/unit/test_hybrid_head_mode.py`:
  - `compute_head_mode_optimize`/`compute_head_mode_layout`: a combo head's
    feeder-slot entry never gets assigned a colour that doesn't match the
    pinned identity (it is fixed input, not a free slot); swap count treats a
    feeder↔ACE-slot transition on the same head as one swap, like any other
    `(ace,slot)` change.
  - `rewrite_head_mode_to_file`: a `slot='feeder'` assignment entry emits
    `ACE_SWAP_HEAD HEAD=<h> SOURCE=FEEDER`, never a bare `T<h>` (that would be
    indistinguishable from a plain pinned feeder head and silently skip the
    unload-the-ACE-slot step).
  - `head_mode_bg_stats`: assert a feeder-leg transition is never classified as
    bg-eligible, regardless of `bg_heads`/`event_times` inputs (§2.3).
* Extend `tests/unit/test_config_changes.py` (or new): `feeder_load_length_for`
  / `feeder_retract_length_for` / `get_feeder_swap_retract_length` precedence
  (per-head override → global →, for retract only, `retract_length` compat
  fallback) - mirror the existing `get_retract_length`/`get_swap_retract_length`
  tests' shape if they exist, or add them alongside this feature since they
  appear to be untested today too.
* `ace.py` / Klipper-stub tests (`tests/conftest.py` already stubs the Klipper
  env, per the late-ACE-detect plan's precedent): `ACE_SET_HEAD_FEEDER_COMBO`
  refuses on a loaded head, refuses when the head has no ACE wired, and clears
  cleanly when `head_ace` changes out from under it.
* `filament_feed_ace.py`: a fake channel exercising the native load loop with a
  per-head `feeder_load_length_0` override larger than the module-wide default,
  proving the per-channel ceiling (not the old single precomputed value) is what
  gates `FEED_ERR_DISTANCE`.
* Manual/hardware test plan (this cannot be simulated): with an actual Y-splitter
  and one ACE + one stock feeder on the same head, run a print exercising both
  `ACE_SWAP_HEAD HEAD=h ACE=a SLOT=s` and `SOURCE=FEEDER` swaps back and forth
  multiple times, watching for cross-contamination at the splice point (the
  README's own experimental-bg-swap caveat about "contamination from the park
  position" is the closest existing precedent for what to look for here, at a
  different junction).

---

## 8. Risks

| Risk | Mitigation |
|---|---|
| `_head_source` sentinel breaks a reader that assumes `int` slot (§2.1's table is not exhaustive - 15k-line file) | Grep `_head_source` fresh at implementation time, not just from this plan's table; add a `isinstance(source.get('slot'), int)` guard at every arithmetic use, fail closed (treat as "unknown source", not "ACE slot 0") rather than silently misrouting |
| `feeder_load_length` too small for the added splitter tube on a combo head, but the *global* default is raised to compensate → every plain feeder head (no splitter) now tolerates a much longer false jam before erroring | Prefer the per-head `_N` override for combo heads specifically; document that raising the global default trades away some jam-detection sensitivity on non-combo heads |
| `feeder_retract_length` too short leaves feeder filament tip protruding past the Y-splitter, physically blocking the ACE's incoming filament on the next swap → jam or grind at the junction | Default conservative (mirror `retract_length`'s existing "measured distance minus ~100mm" guidance, adapted: the feeder-side run is normally much shorter, so default small and require the user to measure and raise it, not the other way around); document a calibration procedure (retract, then attempt a small manual ACE feed and watch for resistance) in the README's Y-splitter section, same style as the existing PTFE-splitter tip |
| `feeder_retract_length` too long pulls the tip back past the feeder's own drive gear, losing the grip reference for the next native load | Cap via `config.getfloat(..., maxval=...)`, matching the `swap_retract_length` style (`ace.cfg:169` / the `swap_purge_max` pattern at `ace.cfg:192-193`) |
| Combo head's feeder tap gets silently treated as one more *assignable* ACE colour by a planner change that forgets the fixed-identity constraint (§4.1) | Explicit test asserting the feeder-slot entry's colour never changes across plan variants for the same physical loadout |
| A user enables `head_feeder_combo` on a head with no physical Y-splitter installed | `ACE_SWAP_HEAD ... SOURCE=FEEDER` only ever runs the *existing* native-feed code path already used by plain feeder heads today - worst case on a head with no combo hardware is the same failure mode a botched feeder head already has (load timeout / no filament), not new hardware risk. No new guard needed beyond the existing `head_feeder_combo` gate at the command level (§2.2 step 2) |
| Mode round-trip (`head` → `multi` → `head`) leaves `head_feeder_combo` stale, re-enabling a combo tap the user meant to disable, or vice versa | §1.2's explicit "always reset combo to off on `MODE=multi`" rule, tested the same way `_convert_manual_to_feeder`/`_convert_feeder_to_manual` round-trips are (implicitly, by the existing manual/feeder tests - add an explicit one) |

---

## 9. Open questions

1. **Slicer support for >16 virtual tools.** Confirm whether Snapmaker Orca (or
   plain Orca) can be configured with more than 16 virtual extruders/tools before
   promising 4-combo-ACE (20-colour) support; if not, phase 1's 16-colour ceiling
   (§4.2) is the practical maximum until/unless that changes upstream.
2. **Exact `loadFeederHead` gating in `app.js`.** Verify whether the existing
   feeder-load button is gated on `head_feeder` specifically (excluding combo
   heads by construction) or on `!head_uses_ace` (which would already exclude
   combo heads too, since they *do* use an ACE) - either way it needs an explicit
   `|| head_feeder_combo` addition, but the exact line needs re-reading against
   the shipped version at implementation time, not this pass's grep.
3. **Physical splice point and default `feeder_retract_length`.** This plan
   assumes the Y-splitter on a combo head sits close to the toolhead (short
   feeder-side run), based on how a stock feeder is normally routed - if a given
   printer's splitter sits further back, the safe default may need to be larger
   than the placeholder 150mm above. Needs a real measurement from an actual
   combo-head installation before picking a shipped default.
4. **Whether `head_feeder_combo` should require `save_variables` at all**, or
   whether (like `head_manual_N`/`head_feeder_N`) a config-file boot default is
   sufficient and the live web toggle is the only place that needs persistence -
   follow whichever precedent `head_feeder`'s own implementation actually uses
   end-to-end (this plan assumed parity with it; confirm before diverging).

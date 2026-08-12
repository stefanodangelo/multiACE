# Filament swap mechanics: audit findings & troubleshooting

**Scope of this document.** multiACE's software side of a swap is now
well instrumented: retries, tip forming, seat press, swap temperature and
per-material tables are all tunable, and failures are logged with reasons.
What is *not* software is the filament path itself, and that is where the
remaining jams come from. This guide records what the code actually does
during a swap, what a user can inspect on their own machine, and what to
change when a specific symptom shows up.

**Honesty about method.** The findings below come from reading the swap
code paths in `multiace/klipper/extras/ace.py` and from the failure modes
users report. This is a code + community audit, not a hands-on teardown
of a reference unit with a load cell — where a number would need
measuring equipment to confirm, it is marked as such rather than invented.

---

## 1. What a swap actually does

Reading the load/unload paths, one swap is:

1. **Unload**: heat to the material's unload temperature (`unloadtemp:` in
   the tip-forming table, default 250 °C), run the tip-forming table, then
   retract by `swap_retract_length` back toward the ACE.
2. **Load**: `FEED_AUTO … LOAD=1` drives filament to the toolhead sensor,
   then a short **seat press** (`seat_overshoot_length`) pushes it home,
   then `swap_purge_length` purges the previous colour.
3. **Verify**: `load_finish` on the feed channel is checked; if it is not
   set, the load counts as failed (and, since this release, is retried —
   see the README).

Two things follow from that sequence, and they matter for the sections
below:

- The nozzle is **hot** for both the unload pull and the load push. Cold
  pulls are not part of the flow.
- Load success is judged by a **sensor**, not by force. A path that binds
  but still reaches the sensor is invisible to the firmware — it shows up
  later as under-extrusion or as a load that takes several attempts.

---

## 2. Mechanical path audit

### 2.1 PTFE tubes and splitters

| Check | What "good" looks like | Symptom when wrong |
|-------|------------------------|--------------------|
| Inner diameter | ~1.9–2.0 mm for 1.75 mm filament | Too tight: binding, missed loads. Too loose: buckling on push, especially with TPU |
| Cut quality | Square, deburred, no inward lip | Filament tip catches on the lip → load stalls at the same place every time |
| Alignment at splitters | Tubes coaxial, no step at the joint | Intermittent failures that follow one specific slot |
| Bend radius | No bend tighter than ~60 mm radius | Rising force with distance; worst on the longest tube run |
| Length | As short as routing allows | Every extra 100 mm adds drag the ACE must overcome |

The splitters are the usual culprit: a 0.5 mm step where two tubes meet is
enough to catch a filament tip that was cut at an angle.

### 2.2 Drive wheels and pinch force

- Inspect the hobbed surfaces for packed filament dust; a glazed groove
  slips long before it looks worn out.
- Uneven wear on one side of the groove indicates a misaligned idler.
- Pinch force should be the **minimum** that does not slip. Too high
  flattens the filament, which then binds in the PTFE downstream — a
  failure that appears to be a tube problem but is not.

Measuring pinch force properly needs a load cell; without one, the
practical test is comparative: if one slot needs visibly more push than
the others on the same spool, the difference is in the drive, not the
filament.

### 2.3 Filament diameter variance

Nominal 1.75 mm filament from budget brands measures 1.68–1.82 mm in
practice. The wide end binds in tight PTFE; the narrow end slips in the
drive. multiACE cannot compensate for this — it has no diameter sensor —
so a brand that measures inconsistently will show up as an elevated retry
count on every slot it is loaded into.

---

## 3. Temperature and material behaviour

The default swap temperature is 220 °C for PLA (raised from 200 °C on the
strength of forum user Popstar's testing) and that change, together with
the seat press, is the single largest reliability improvement in recent
releases.

Suggested starting points per material — set these in the tip-forming
table (`unloadtemp:`), not globally:

| Material | Swap / unload temp | Notes |
|----------|--------------------|-------|
| PLA | 220–230 °C | Below 210 °C the tip drags and leaves a hook |
| PLA-CF | 230 °C | Abrasive: expect faster drive-wheel wear |
| PETG | 240–250 °C | Prone to stringing; a longer tip-forming retract helps |
| ABS / ASA | 250–260 °C | Keep the chamber warm or the tip snaps off in the tube |
| TPU | 215–225 °C | The binding-prone one: soft filament buckles rather than pushes |
| PA / PC | 260–280 °C | Dry the filament first; wet nylon foams and jams |

**Wet filament is a mechanical problem, not a print-quality one.** Steam
in the melt zone makes the tip irregular, and an irregular tip is what
catches in a splitter. If jams cluster on one spool, dry it before
adjusting anything else.

---

## 4. Symptom → action

**Load fails repeatedly on one slot, others are fine**
Path problem, not tuning. Check that slot's tube for a burr or a step at
the splitter, then swap the tube with a working slot's — if the failure
follows the tube, you have found it.

**Loads fail on every slot after a filament change**
Suspect the spool: diameter variance or moisture. Measure the filament in
a few places, and dry it if it has been open for a while.

**Filament sticks in the toolhead after unload (Sunlu, Jayo and similar)**
Raise the unload temperature by 10 °C for that material and lengthen the
tip-forming retract. This is a known trait of some brands, called out in
the README.

**Loads succeed only on the second or third attempt**
This is exactly what auto-retry is for, and the print will now survive it
— but a rising retry count is a maintenance signal. Clean the drive
wheels and check the seat-press length before raising the retry limit.

**Jams during a background (parked) swap only**
Background swaps run at the park position with different cooling; treat
them separately and confirm the material's tip-forming table works at
that temperature before blaming the path.

**Print pauses with "load failed after N attempts"**
The recovery flow: clear the path at the ACE end, confirm the slot has
filament, then RESUME. multiACE deliberately pauses rather than aborting
so the job is recoverable.

---

## 5. Recommendations, in priority order

1. **Mechanical audit first (high impact).** Tubes, splitter alignment,
   drive-wheel cleanliness. Most reported jams are here, and none of them
   are fixable in software.
2. **Verify the load sequence is doing its job (medium).** Confirm
   `seat_overshoot_length` is non-zero and the swap temperature matches
   the material — the two changes that measurably improved reliability.
3. **Per-material temperature profiles (medium).** Use the tip-forming
   table's `unloadtemp:` rather than one global swap temperature.
4. **Pinch-force tuning (medium).** Minimum force that does not slip.
   Needs comparative feel or a load cell.
5. **Retry limits last (low).** Auto-retry buys an unattended print time
   to recover; it does not fix a path that binds. Raising it above 3 is a
   workaround, not a repair.

**On lubrication:** users occasionally report success with dry silicone on
the filament. It is not recommended here — it migrates into the melt zone
and contaminates subsequent prints. Fix the path instead.

---

## 6. Telemetry: what multiACE already records

Every load failure is audited with its reason, and with this release the
attempt counter as well. In `klippy.log`:

```
[multiACE] LOAD_HEAD_FAILED … reason=load_not_finished attempt=2 max_attempts=4
[multiACE] head 0: load succeeded on attempt 2/4
```

and on the engine event stream (`multiace_event`):

```
multiace_event load_retry head=0 ace=0 slot=2 attempt=1 max_attempts=3 reason=load_not_finished
multiace_event load_failed head=0 ace=0 slot=2 attempts=4 reason=load_not_finished cancelled=0
```

Grepping those two lines over a few weeks of logs is the cheapest way to
find out *which slot* is really the problem — the answer is often not the
one that feels worst.

### Neighbour clearance

Before each retry, multiACE retracts the *other* slots of the same ACE, on
the theory that filament crowding the shared path is why the load failed.
That is audited too:

```
[multiACE] LOAD_RETRY_NEIGHBOR_RETRACT … attempt=2 step_mm=100 cumulative={'1': 150, '3': 150}
[multiACE] LOAD_RETRY_NEIGHBOR_SKIP … reason=stock_feeder
```

Two things to read out of `cumulative`:

* **Which slots keep getting cleared.** If the same neighbour appears every
  time head 0 struggles, that slot's filament is the one crowding the hub —
  look at its spool tension and its PTFE run, not at the head that failed.
* **How much slack has accumulated.** More than ~200 mm on one slot is
  around 20 cm of filament unwound inside the unit with no spool take-up,
  and slack is a known cause of ACE tangles. The dashboard hints at it and
  the print history keeps the number per job.

`load_retry_neighbor_retract: 0` disables the whole behaviour if you would
rather diagnose without it.

### The print history

Beyond the log, `printer_data/multiace/jobs.jsonl` keeps one record per job
with the plan used, the resolved assignment, every swap's real duration and
kind, the load-retry count and the neighbour clearance. It is bounded (200
jobs / 2 MB) and append-only, and the **History** tab renders it joined
against Moonraker's own job list.

For jam-hunting it answers a question the log does not: *did this get
worse?* A slot whose `same_ace` swaps have crept from 210 s to 280 s over a
month is telling you something mechanical is degrading, well before it
starts failing outright.

---

## 7. Reporting a jam usefully

Include: the material and brand, which ACE and slot, whether it was a load
or an unload, the swap temperature in use, the `LOAD_HEAD_FAILED` lines
from `klippy.log`, and whether the same spool fails in a different slot.
That last one splits "path problem" from "spool problem" faster than
anything else.

"""multiACE print analysis: segment stitching and the actual-duration
breakdown (plan §1-§2.4).

Pure stdlib, no Klipper and no FastAPI imports, same contract as
job_history.py: the web backend imports this directly and tests import it
directly.

Three traps this module exists to get right (plan §1):

  * `print_duration` is not "moving time" - it is total_duration minus
    time_paused, nothing else. Toolchange and filament-swap time sit
    INSIDE print_duration; "total - print" is paused time, exactly, and
    nothing but paused time.
  * Overlapping intervals (a T-switch inside an ACE swap) must be merged
    into a non-overlapping cover, never summed - the swap wins.
  * A power-loss resume is two Moonraker jobs with CUMULATIVE counters.
    Reading the last job alone undercounts; summing both double-counts.
    Segments must be stitched and their counters taken as max(), never
    summed.
"""
from __future__ import annotations

import os

#: A crash-resumed segment must start within this long of the previous
#: one's end to be considered the SAME print (plan §1.3).
SEGMENT_STITCH_WINDOW_S = 3600.0

#: Moonraker statuses from which a resume is plausible.
_RESUMABLE_STATUSES = ("klippy_shutdown", "error", "cancelled")

#: A pause is attributed to an error that preceded it within this long;
#: absent one, to the user (plan §2.1).
PAUSE_CAUSE_WINDOW_S = 10.0

#: The seven buckets a Breakdown always carries, whether or not Layer 1
#: had anything to say about them.
BUCKET_KEYS = ("depositing", "filament_swap", "toolhead_swap", "warmup",
               "paused_error", "paused_user", "crash_gap")


def _basename(name):
    return os.path.basename(str(name or "").replace("\\", "/"))


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# §1.3 - segment stitching
# ---------------------------------------------------------------------------

class Print:
    """One logical print: one or more job_history.join_history() rows
    stitched together. A plain, un-crashed print is a Print of one
    segment - every row carries this wrapper so breakdown() has a single
    input shape."""

    __slots__ = ("segments",)

    def __init__(self, segments):
        self.segments = list(segments)

    @property
    def filename(self):
        return self.segments[-1].get("filename")

    @property
    def status(self):
        return self.segments[-1].get("status")

    @property
    def first_start(self):
        return min(_f(s.get("start_time")) for s in self.segments)

    @property
    def last_end(self):
        ends = [_f(s.get("end_time")) for s in self.segments
                if s.get("end_time")]
        return max(ends) if ends else None

    @property
    def wall_total(self):
        """last_end - first_start, spanning every segment AND the gaps
        between them. None while the print is still running."""
        end = self.last_end
        return None if end is None else end - self.first_start

    @property
    def print_duration(self):
        """Cumulative counters are taken as max() across segments, never
        summed (§1.3) - a restored print_stats already carries the full
        cumulative value forward, so the largest segment IS the total."""
        vals = [_f(s.get("print_duration")) for s in self.segments]
        return max(vals) if vals else 0.0

    @property
    def total_duration(self):
        vals = [_f(s.get("duration")) for s in self.segments]
        return max(vals) if vals else 0.0

    @property
    def crash_gap(self):
        """Sum of the gaps BETWEEN stitched segments - the inter-segment
        gap a power-loss resume leaves behind."""
        if len(self.segments) < 2:
            return 0.0
        ordered = sorted(self.segments, key=lambda s: _f(s.get("start_time")))
        gap = 0.0
        for prev, nxt in zip(ordered, ordered[1:]):
            if prev.get("end_time") is not None and nxt.get("start_time") is not None:
                gap += max(0.0, _f(nxt.get("start_time")) - _f(prev.get("end_time")))
        return gap

    @property
    def events(self):
        """Layer-1 events across every segment's multiace record, time
        ordered."""
        out = []
        for seg in self.segments:
            rec = seg.get("multiace") or {}
            out.extend(rec.get("events") or [])
        out.sort(key=lambda e: _f(e.get("t")))
        return out

    @property
    def meta(self):
        """Layer-2 Moonraker metadata. A stitched print's earlier segment
        may not carry it (the job was still printing when it crashed), so
        this prefers the most recent segment that has any."""
        for seg in reversed(self.segments):
            m = seg.get("meta")
            if m:
                return m
        return {}


def stitch_segments(rows, window_s=SEGMENT_STITCH_WINDOW_S):
    """Group job_history.join_history() rows into logical Prints (§1.3).

    Each row must carry filename/start_time/end_time/status/print_duration
    as join_history() produces them; any other keys (multiace, meta, id,
    job_id) are carried through untouched on the segment.

    Returns Prints newest-first, matching join_history()'s own ordering.
    """
    ordered = sorted(rows or [], key=lambda r: _f(r.get("start_time")))
    prints = []
    for row in ordered:
        if prints:
            prev_seg = prints[-1].segments[-1]
            if (_basename(prev_seg.get("filename")) == _basename(row.get("filename"))
                    and prev_seg.get("status") in _RESUMABLE_STATUSES
                    and prev_seg.get("end_time") is not None
                    and row.get("start_time") is not None
                    and _f(row.get("start_time")) - _f(prev_seg.get("end_time")) <= window_s
                    and _f(row.get("print_duration")) > _f(prev_seg.get("print_duration"))):
                prints[-1].segments.append(row)
                continue
        prints.append(Print([row]))
    prints.sort(key=lambda p: p.first_start, reverse=True)
    return prints


# ---------------------------------------------------------------------------
# §1.1 / §1.2 - the breakdown
# ---------------------------------------------------------------------------

class Bucket:
    __slots__ = ("key", "seconds", "source")

    def __init__(self, key, seconds, source):
        self.key = key
        self.seconds = seconds
        self.source = source

    def as_dict(self, wall_total):
        pct = round(100.0 * self.seconds / wall_total, 1) if wall_total else 0.0
        return {"key": self.key, "seconds": self.seconds,
                "pct_of_wall": pct, "source": self.source}


class Breakdown:
    def __init__(self, wall_total, buckets, warnings=None):
        self.wall_total = wall_total
        self.buckets = buckets
        self.warnings = list(warnings or [])

    def as_dict(self):
        return {
            "wall_total": self.wall_total,
            "buckets": {k: b.as_dict(self.wall_total)
                        for k, b in self.buckets.items()},
            "warnings": list(self.warnings),
        }


def _pause_windows(events):
    """[(start, end, cause)], time ordered, from raw pause/resume events.
    A pause with no matching resume (the record ends mid-pause) is closed
    at the last known event rather than dropped - a pause whose time
    vanishes is worse than one whose end is approximate."""
    out = []
    open_start = None
    for ev in events:
        kind = ev.get("kind")
        if kind == "pause":
            open_start = _f(ev.get("t"))
        elif kind == "resume" and open_start is not None:
            out.append((open_start, _f(ev.get("t"))))
            open_start = None
    if open_start is not None and events:
        out.append((open_start, _f(events[-1].get("t"), open_start)))
    return out


def _pause_cause(events, pause_start):
    """§2.1: a pause is attributed to an error seen within
    PAUSE_CAUSE_WINDOW_S before it; absent one, to the user."""
    best = None
    for ev in events:
        if ev.get("kind") != "error":
            continue
        et = _f(ev.get("t"))
        if 0 <= pause_start - et <= PAUSE_CAUSE_WINDOW_S:
            if best is None or et > best:
                best = et
    return "error" if best is not None else "user"


def _clip_overlap(outer_ivs, inner_ivs):
    """§1.2: subtract from inner_ivs whatever outer_ivs already covers.
    Returns (outer_total, inner_remaining_total) - the merged,
    non-overlapping cover, outer wins."""
    outer_total = sum(e - s for s, e in outer_ivs)
    inner_total = 0.0
    for s, e in inner_ivs:
        remaining = [(s, e)]
        for os_, oe in outer_ivs:
            nxt = []
            for rs, re in remaining:
                if oe <= rs or os_ >= re:
                    nxt.append((rs, re))
                    continue
                if os_ > rs:
                    nxt.append((rs, os_))
                if oe < re:
                    nxt.append((oe, re))
            remaining = nxt
        inner_total += sum(e2 - s2 for s2, e2 in remaining)
    return outer_total, inner_total


def breakdown(p):
    """The §1.1/§1.2 partition of wall_total. Always returns a Breakdown,
    even for a print with zero Layer-1 events - the buckets just carry
    `source="derived"` instead of "measured", and depositing/filament_swap
    fall back to the multiace record's own (always-present) swap list."""
    wall_total = p.wall_total
    if wall_total is None or wall_total <= 0:
        wall_total = p.print_duration

    paused = max(0.0, p.total_duration - p.print_duration)
    crash_gap = p.crash_gap
    print_duration = p.print_duration
    events = p.events
    warnings = []

    swap_ivs = []
    toolhead_ivs = []
    toolhead_agg_s = 0.0
    for ev in events:
        kind = ev.get("kind")
        t = _f(ev.get("t"))
        s = _f(ev.get("s"))
        if kind == "swap":
            swap_ivs.append((t - s, t))
        elif kind == "toolhead_change":
            toolhead_ivs.append((t - s, t))
        elif kind == "toolhead_change_agg":
            toolhead_agg_s += s

    if swap_ivs:
        swap_s, toolhead_overlap_s = _clip_overlap(swap_ivs, toolhead_ivs)
        toolhead_s = toolhead_overlap_s + toolhead_agg_s
    else:
        # No Layer-1 swap events (an older job, or Layer 1 never wrote
        # one) - fall back to the multiace record's own swap list, which
        # has existed since before this feature.
        swap_s = 0.0
        for seg in p.segments:
            for sw in (seg.get("multiace") or {}).get("swaps") or []:
                swap_s += _f(sw.get("seconds"))
        toolhead_s = sum(e - s for s, e in toolhead_ivs) + toolhead_agg_s

    warmup_s = 0.0
    start_t = min((_f(s.get("start_time")) for s in p.segments), default=None)
    warmup_end_t = next((_f(e.get("t")) for e in events
                         if e.get("kind") == "warmup_end"), None)
    if start_t is not None and warmup_end_t is not None and warmup_end_t > start_t:
        warmup_s = warmup_end_t - start_t

    depositing = print_duration - swap_s - toolhead_s - warmup_s
    if depositing < 0:
        warnings.append("negative_residual")
        depositing = 0.0

    pauses = [(s, e, _pause_cause(events, s)) for s, e in _pause_windows(events)]
    paused_error_s = sum(e - s for s, e, c in pauses if c == "error")
    paused_user_s = sum(e - s for s, e, c in pauses if c == "user")
    split_total = paused_error_s + paused_user_s

    had_layer1 = bool(events)
    if split_total > 0 and abs(split_total - paused) > 0.5:
        # The exact Moonraker number always wins over the Layer-1 split
        # when the two disagree (clock vs 5 s-poll drift, §1's "3 minutes
        # over 51 hours") - scale the split rather than show two numbers
        # that do not add up.
        scale = paused / split_total
        paused_error_s *= scale
        paused_user_s *= scale
    elif split_total == 0 and paused > 0:
        paused_user_s = paused

    buckets = {
        "depositing":     Bucket("depositing", round(depositing, 1), "residual"),
        "filament_swap":  Bucket("filament_swap", round(swap_s, 1),
                                  "measured" if events else "derived"),
        "toolhead_swap":  Bucket("toolhead_swap", round(toolhead_s, 1),
                                  "measured" if events else "derived"),
        "warmup":         Bucket("warmup", round(warmup_s, 1),
                                  "measured" if warmup_s else "derived"),
        "paused_error":   Bucket("paused_error", round(paused_error_s, 1),
                                  "measured" if had_layer1 else "derived"),
        "paused_user":    Bucket("paused_user", round(paused_user_s, 1),
                                  "measured" if had_layer1 else "derived"),
        "crash_gap":      Bucket("crash_gap", round(crash_gap, 1), "measured"),
    }
    return Breakdown(round(wall_total, 1), buckets, warnings)

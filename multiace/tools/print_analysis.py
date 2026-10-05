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

import datetime
import html
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


# ---------------------------------------------------------------------------
# §4.4 - the findings engine
#
# Three of plan §4.4's eleven rules (spool_unbound_heads, spool_ledger_drift,
# rfid_never_read) are deliberately NOT implemented here: they need live
# spool-binding/RFID state that only ever exists in ace.py's runtime - it
# is not part of the Layer-1 event schema this job record carries, and
# adding it is a separate, larger change to the printer-side module, not a
# gap in this function. The other eight/nine rules below are the ones this
# module's own inputs (events, meta, and an optional klippy_analysis.py
# payload) can actually support.
# ---------------------------------------------------------------------------

def _code(ev):
    try:
        return int(ev.get("code"))
    except (TypeError, ValueError):
        return None


def _finding(id_, severity, confidence, cost_s=0.0, count=None, heads=None,
             evidence=None):
    d = {"id": id_, "severity": severity, "confidence": confidence,
         "cost_s": round(float(cost_s), 1),
         "title_key": "ui.analysis.find.%s.title" % id_,
         "action_key": "ui.analysis.find.%s.action" % id_}
    if count is not None:
        d["count"] = count
    if heads:
        d["heads"] = sorted({h for h in heads if h is not None})
    if evidence:
        d["evidence"] = [e for e in evidence if e]
    return d


def _pause_cost_for_errors(events, err_events):
    """How much PAUSED time followed these specific error events - the
    per-code cost a generic paused_error bucket can't give, because that
    bucket doesn't know which error caused which pause."""
    err_ts = [_f(e.get("t")) for e in err_events]
    total = 0.0
    for s, e in _pause_windows(events):
        if any(0 <= s - et <= PAUSE_CAUSE_WINDOW_S for et in err_ts):
            total += e - s
    return total


#: (code, finding id, log-line phrase) - Layer 1's error code and Layer
#: 3's text for the SAME physical fault, per extruder_ace.py's pickup
#: retry loop. Checked together so this finding also covers an old print
#: that predates Layer-1 instrumentation but still has its klippy.log
#: (plan §2.3's signals table: "confirms Layer 1, covers old prints").
_PICKUP_CODES = (
    (45, "pickup_pogopin", "pogopin not connected"),
    (46, "pickup_detached", "is detached"),
    (47, "pickup_conflict", "conflicting status"),
)


def _pickup_finding(events, log, id_, code, phrase):
    errs = [e for e in events if e.get("kind") == "error" and _code(e) == code]
    if errs:
        return _finding(
            id_, "high", "measured",
            cost_s=_pause_cost_for_errors(events, errs), count=len(errs),
            heads=[e.get("head") for e in errs],
            evidence=[e.get("msg") or e.get("coded") for e in errs[:5]])
    lines = [pf.get("line") for pf in ((log or {}).get("pickup_failures") or [])
             if phrase in (pf.get("line") or "")]
    if lines:
        return _finding(id_, "high", "measured", count=len(lines),
                         evidence=lines[:5])
    return None


def _host_stall_findings(log, p):
    if not log:
        return []
    out = []
    shutdowns = log.get("mcu_shutdowns") or []
    stalls = log.get("host_stalls") or []
    print_stalls = log.get("print_stalls") or []
    if shutdowns:
        out.append(_finding(
            "host_stall_fatal", "high", "measured",
            cost_s=p.crash_gap if len(p.segments) > 1 else 0.0,
            count=len(shutdowns),
            evidence=[s.get("line") for s in shutdowns[:5]]))
    elif stalls or print_stalls:
        out.append(_finding(
            "host_stall_warning", "medium", "measured",
            count=len(stalls) + len(print_stalls),
            evidence=[s.get("line") for s in stalls[:5]]))
    return out


def _log_flood_findings(log):
    if not log or not log.get("log_flood"):
        return []
    flood = log["log_flood"]
    out = [_finding("log_flood", "low", "measured", count=flood.get("count"),
                     evidence=[flood.get("sample")])]
    if log.get("host_stalls") or log.get("print_stalls"):
        # The flood itself is measured; that it CAUSED the stall is only
        # ever a correlation (plan §4.4) - its own finding, its own
        # (lower) confidence, so the UI cannot read it as proven.
        out.append(_finding("log_flood_stall_link", "medium", "suspected",
                             evidence=[flood.get("sample")]))
    return out


def _ace_comms_finding(log):
    if not log:
        return []
    comms = log.get("ace_comms") or []
    if len(comms) < 2:
        return []
    return [_finding("ace_comms_flap", "medium", "measured", count=len(comms),
                      evidence=[c.get("line") for c in comms[:5]])]


def _estimate_drift_finding(p, bd):
    meta = p.meta or {}
    est = meta.get("estimated_time")
    if not est:
        for seg in p.segments:
            e = (seg.get("multiace") or {}).get("estimate") or {}
            if e.get("total_s"):
                est = e["total_s"]
                break
    est = _f(est, 0.0)
    if est <= 0 or not bd.wall_total:
        return []
    drift = bd.wall_total - est
    pct = 100.0 * drift / est
    if abs(pct) < 20.0:
        return []
    return [_finding("estimate_drift", "low", "inferred", cost_s=abs(drift),
                      evidence=["estimated %.0fs, actual %.0fs (%+.1f%%)"
                                % (est, bd.wall_total, pct)])]


def _log_coverage_finding(log):
    if not log or not log.get("rotated_away"):
        return []
    files = log.get("files") or []
    return [_finding("log_coverage_partial", "low", "measured",
                      evidence=["retained log covers: %s" % ", ".join(files)]
                      if files else ["no retained log covers this window"])]


def findings(p, log=None):
    """Ordered, typed findings (plan §4.4) - sorted by `cost_s` descending
    so the list answers "what cost me the most", not "what's first
    alphabetically". `log` is a klippy_analysis.analyse_logs() payload, or
    None when Layer 3 was skipped."""
    out = []
    for code, id_, phrase in _PICKUP_CODES:
        f = _pickup_finding(p.events, log, id_, code, phrase)
        if f:
            out.append(f)
    out.extend(_host_stall_findings(log, p))
    out.extend(_log_flood_findings(log))
    out.extend(_ace_comms_finding(log))
    out.extend(_estimate_drift_finding(p, breakdown(p)))
    out.extend(_log_coverage_finding(log))
    out.sort(key=lambda f: f["cost_s"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# §2.4 / §2.5 - the API payload
# ---------------------------------------------------------------------------

def analyse(rows, log=None):
    """The full Analysis payload (plan §2.4/§2.5) for the logical print
    `rows` belong to. `rows` is one print's own segment rows exactly as
    job_history.join_history() produces them (a stitched print's rows,
    or just one row for a plain print) - the caller is responsible for
    having already picked out the right rows; this function stitches
    them (a no-op when there is only one) rather than re-deriving which
    rows belong together from a whole history list.
    """
    prints = stitch_segments(rows)
    p = prints[0] if prints else Print(list(rows or []))
    bd = breakdown(p)
    return {
        "print": {
            "filename": p.filename,
            "status": p.status,
            "segments": len(p.segments),
            "first_start": p.first_start,
            "last_end": p.last_end,
        },
        "meta": p.meta,
        "breakdown": bd.as_dict(),
        "findings": findings(p, log),
        "events": p.events,
        "log": log if log is not None else {"available": False},
    }


# ---------------------------------------------------------------------------
# §4.5 - standalone HTML, server-rendered from the SAME Analysis payload
# ---------------------------------------------------------------------------

_BUCKET_LABELS = {
    "depositing": "Depositing", "filament_swap": "Filament changes",
    "toolhead_swap": "Toolhead changes", "warmup": "Heating / homing",
    "paused_error": "Paused — errors", "paused_user": "Paused — by you",
    "crash_gap": "Crash recovery",
}
#: Matches style.css's --accent/--ink-700/--ink-800/--error/--warn/--ink-900
#: (plan §4.3's palette constraint) so the standalone document doesn't
#: invent its own colour language.
_BUCKET_COLORS = {
    "depositing": "#15bdc6", "filament_swap": "#272c30",
    "toolhead_swap": "#272c30", "warmup": "#1e2225",
    "paused_error": "#ff7070", "paused_user": "#fb6", "crash_gap": "#121416",
}
#: Hue is never the only cue (plan §4.3) - these two get the hatch
#: pattern .comp-seg-toolhead_swap / .comp-seg-crash_gap already use.
_HATCHED = {"toolhead_swap", "crash_gap"}


def _fmt_hm(seconds):
    if seconds is None:
        return "–"
    try:
        s = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "–"
    sign = "-" if s < 0 else ""
    s = abs(s)
    h, rem = divmod(s, 3600)
    m, _s = divmod(rem, 60)
    return "%s%dh %02dm" % (sign, h, m)


def _fmt_local(ts):
    if not ts:
        return "–"
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return "–"


def _esc(value):
    return html.escape(str(value if value is not None else "–"))


def _comp_bar_svg(buckets):
    """The same 100%-wide composition bar as the History popover and the
    modal (plan §4.3: "shared with §3.2"), rendered server-side here
    since the standalone document has zero JS to build one with."""
    total = sum(max(0.0, (buckets.get(k) or {}).get("seconds", 0.0))
                for k in BUCKET_KEYS) or 1.0
    width = 760.0
    x = 0.0
    parts = [
        '<svg width="%d" height="14" viewBox="0 0 %d 14" '
        'xmlns="http://www.w3.org/2000/svg" role="img" '
        'aria-label="time composition">' % (int(width), int(width)),
        '<defs><pattern id="hatch" width="6" height="6" '
        'patternTransform="rotate(45)" patternUnits="userSpaceOnUse">'
        '<line x1="0" y1="0" x2="0" y2="6" stroke="rgba(255,255,255,.35)" '
        'stroke-width="2"/></pattern></defs>']
    for key in BUCKET_KEYS:
        seconds = max(0.0, (buckets.get(key) or {}).get("seconds", 0.0))
        if seconds <= 0:
            continue
        w = width * seconds / total
        fill = _BUCKET_COLORS.get(key, "#666")
        parts.append('<rect x="%.2f" y="0" width="%.2f" height="14" fill="%s"/>'
                     % (x, w, fill))
        if key in _HATCHED:
            parts.append('<rect x="%.2f" y="0" width="%.2f" height="14" '
                         'fill="url(#hatch)"/>' % (x, w))
        x += w
    parts.append('</svg>')
    return "".join(parts)


def render_html(analysis):
    """Plan §4.5: the SAME Analysis payload analyse() built, rendered
    server-side into one self-contained document - inlined style, inlined
    SVG, zero JS, zero external references, for printing or attaching to
    a bug report.

    English-only by design: a diagnostic document meant to be handed to
    whoever sold you the toolhead is not the localized UI surface the
    i18n catalogs cover, and the plan does not ask for one. Carries the
    outcome table and the composition bar (the two sections that are pure
    numbers/colour, cheap to agree with the JSON exactly); the timeline,
    per-head and host-health SVGs stay modal-only - this document's job
    is "the numbers, verifiably", not a second chart renderer.
    """
    pr = analysis.get("print") or {}
    meta = analysis.get("meta") or {}
    bd = analysis.get("breakdown") or {}
    buckets = bd.get("buckets") or {}
    fs = analysis.get("findings") or []
    log = analysis.get("log") or {}

    outcome_rows = [
        ("File", pr.get("filename")),
        ("Status", pr.get("status")),
        ("Segments", pr.get("segments", 1)),
        ("First start", _fmt_local(pr.get("first_start"))),
        ("Finish", _fmt_local(pr.get("last_end"))),
        ("Wall total", _fmt_hm(bd.get("wall_total"))),
        ("Depositing", _fmt_hm((buckets.get("depositing") or {}).get("seconds"))),
        ("Estimated", _fmt_hm(meta.get("estimated_time"))),
        ("Layer count", meta.get("layer_count")),
        ("Filament used (mm)", meta.get("filament_used_mm")),
    ]
    outcome_html = "".join(
        "<tr><th>%s</th><td>%s</td></tr>" % (_esc(k), _esc(v))
        for k, v in outcome_rows)

    bucket_html = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s%%</td><td class=\"muted\">%s</td></tr>"
        % (_esc(_BUCKET_LABELS.get(k, k)),
           _esc(_fmt_hm((buckets.get(k) or {}).get("seconds"))),
           _esc((buckets.get(k) or {}).get("pct_of_wall", 0)),
           _esc((buckets.get(k) or {}).get("source", "")))
        for k in BUCKET_KEYS if (buckets.get(k) or {}).get("seconds", 0) > 0)

    if fs:
        findings_html = "".join(
            "<li><b>[%s/%s]</b> %s — cost %s%s</li>"
            % (_esc(f.get("severity")), _esc(f.get("confidence")),
               _esc(f.get("id")), _esc(_fmt_hm(f.get("cost_s"))),
               ("<br><span class=\"muted\">"
                + "<br>".join(_esc(e) for e in f.get("evidence", []))
                + "</span>") if f.get("evidence") else "")
            for f in fs)
    else:
        findings_html = "<li class=\"muted\">No findings for this print.</li>"

    coverage = ""
    if log.get("available", True) is not False:
        coverage = "Log coverage: %s &rarr; %s%s" % (
            _esc(_fmt_local(log.get("from"))), _esc(_fmt_local(log.get("to"))),
            " (rotated away beyond this window)" if log.get("rotated_away") else "")

    return """<!doctype html>
<html><head><meta charset="utf-8">
<title>Print analysis - %s</title>
<style>
body { background:#121416; color:#e8e8e8;
       font:14px/1.5 -apple-system,"Segoe UI",sans-serif; margin:0; padding:1.5rem; }
h1 { font-size:1.2rem; margin:0 0 .3rem; }
h2 { font-size:1rem; margin:1.4rem 0 .4rem; color:#15bdc6; }
table { border-collapse:collapse; width:100%%; max-width:760px; }
th, td { text-align:left; padding:.2rem .7rem .2rem 0; vertical-align:top; }
th { color:#8b9297; font-weight:400; width:11rem; }
.muted { color:#8b9297; font-size:.85em; }
ul { padding-left:1.1rem; }
</style></head><body>
<h1>Print analysis — %s</h1>
<p class="muted">%s</p>
<h2>Outcome</h2>
<table>%s</table>
<h2>Where the time went</h2>
%s
<table>%s</table>
<h2>Findings</h2>
<ul>%s</ul>
</body></html>""" % (_esc(pr.get("filename")), _esc(pr.get("filename")), coverage,
                      outcome_html, _comp_bar_svg(buckets), bucket_html,
                      findings_html)

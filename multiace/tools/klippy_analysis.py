"""multiACE print analysis: klippy.log enrichment (plan §2.3, Layer 3).

Pure stdlib, no Klipper and no FastAPI imports - same contract as
job_history.py and print_analysis.py: the web backend imports this by
path, tests import it directly.

This is the lowest-trust, best-effort layer. Everything it finds is
`confidence: "measured"` about the LOG LINE itself ("this message appeared
at this time") but the log rotates away, so absence of a signal here never
means absence of the thing - it may just mean the window isn't retained
any more. `analyse_logs()` always reports its own coverage for exactly
that reason (plan §2.3.5).

Performance is the design problem, not a detail: one real printer directory
held 160 MB across 16 rotated files for a single 51-hour print. The two
cheap wins taken here:

  1. Index, don't read. Only the first timestamped line of each file is
     read to place it on the timeline, so picking the one or two files
     that cover a report's window costs ~16 small reads, not 160 MB.
  2. Prefilter before parsing. A plain `str` substring test rejects the
     ~96% of lines (repeated internal chatter, routine Stats heartbeats)
     that carry none of the patterns below, before any regex ever runs.

What this module deliberately does NOT do: binary-search by byte offset
within a single file to skip straight to the window (plan §2.3 step 2).
Once the file-level index has picked the one or two files that matter, a
straight prefiltered scan of those files is fast enough in practice (the
real-world case above is dominated by which files to read, not where in
them to start) - true seeking was cut as a scoping call. The `max_lines`/
`max_seconds` budgets below are what bound the cost if that call turns out
to be wrong on a slower SoC.
"""
from __future__ import annotations

import os
import re
import time

#: Returned when nothing can be read at all - never an exception.
EMPTY = {
    "from": None, "to": None, "files": [], "rotated_away": False,
    "truncated": False,
    "host_stalls": [], "mcu_shutdowns": [], "reactor_stalls": [],
    "print_stalls": [], "health_samples": [],
    "pickup_failures": [], "sd_positions": [], "ace_comms": [],
    "spool_ledger": [], "log_flood": None,
}

#: Budgets, declared in the payload (plan §2.3.4) rather than silently
#: eaten - a report that ran out of budget says so.
DEFAULT_MAX_LINES = 2_000_000
DEFAULT_MAX_SECONDS = 6.0

#: Cheap substring prefilter (plan §2.3.3) - checked before any regex.
#: Order doesn't matter for correctness, only for the (tiny) average-case
#: speed of the `in` scan, so the hottest fragments are not special-cased.
_INTERESTING = (
    "Timer too close", "shutdown:", "print_stall=", "Stats ",
    "pogopin not connected", "is detached", "conflicting status",
    "comms lost", "Try connecting ACE", "reconnect[", "SD card print",
    "[spool] print",
)

_RE_TIMER_CLOSE = re.compile(
    r"Timer too close: waketime=([\d.]+),\s*timer_read_time=([\d.]+)")
_RE_MCU_SHUTDOWN = re.compile(r"MCU '([^']+)' shutdown:\s*(.+)")
_RE_PRINT_STALL = re.compile(r"print_stall=(\d+)")
_RE_BUFFER_TIME = re.compile(r"buffer_time=([\d.]+)")
_RE_SYSLOAD = re.compile(r"sysload=([\d.]+)")
_RE_MEMAVAIL = re.compile(r"memavail=(\d+)")
_RE_STATS_PREFIX = re.compile(r"\bStats\s+([\d.]+):")
_RE_SD_POSITION = re.compile(
    r"(Starting|Finished|Exiting) SD card print.*?position\s+(\d+)")
_RE_RECONNECT = re.compile(r"reconnect\[(\d+)\]")
_RE_TS = re.compile(r"^(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})\.(\d{3})")

#: A line repeating faster than this is a flood, not routine chatter
#: (plan §2.3's "76% of lines were one repeated message" observation).
_FLOOD_HZ = 0.5

#: Expected gap between Klipper's own `Stats` heartbeat lines; anything
#: noticeably longer is the host reactor itself stalling, not the MCU.
_STATS_PERIOD_S = 1.0
_REACTOR_STALL_THRESHOLD_S = 5.0


def _flood_signature(text):
    """A cheap (no regex) signature for flood detection: the message body
    with digits blanked out, truncated. Two Stats lines a second apart
    differ only in their numbers, so blanking digits is what makes
    "the same message" mean the same thing twice."""
    return re.sub(r"\d", "0", text[:72])


def _parse_ts(line, year_hint):
    """Klippy logs `MM-DD HH:MM:SS.mmm` with no year (plan §2.3.6).
    `year_hint` is the year to assume; the caller is responsible for
    picking the right one per file (see `_file_first_ts`) and this
    function never has to guess."""
    m = _RE_TS.match(line)
    if not m:
        return None
    mo, d, h, mi, s, ms = (int(g) for g in m.groups())
    try:
        return time.mktime((year_hint, mo, d, h, mi, s, 0, 0, -1)) + ms / 1000.0
    except (ValueError, OverflowError):
        return None


def _line_month(line):
    """The MM from a `MM-DD HH:MM:SS.mmm` prefix, by slicing rather than
    regex - this runs on every line (rollover tracking needs it even on
    lines the interest prefilter rejects), so it has to be cheaper than
    the already-cheap substring prefilter, not on top of it."""
    if len(line) >= 6 and line[2] == "-" and line[0:2].isdigit():
        try:
            return int(line[0:2])
        except ValueError:
            return None
    return None


def _file_first_ts(path):
    """The timestamp of the first timestamped line in `path`, resolving
    the missing year from the file's own mtime - a file whose first line
    is in December could have been written in January of the NEXT year's
    mtime, so the resolution has to consider both candidates and pick the
    one that doesn't put the line in the future."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    mtime_year = time.localtime(mtime).tm_year
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for _, line in zip(range(50), f):
                for year in (mtime_year, mtime_year - 1):
                    ts = _parse_ts(line, year)
                    if ts is not None and ts <= mtime + 86400:
                        return ts
    except OSError:
        return None
    return None


def _list_log_files(log_dir):
    """Every klippy.log* file in `log_dir`, oldest first. Klipper names the
    live file `klippy.log` and rotates to `klippy.log.1`, `klippy.log.2`,
    ... (highest suffix = oldest) - sorted here by first timestamp rather
    than by name, so the ordering is correct even if a file was copied in
    with a misleading suffix."""
    try:
        names = [n for n in os.listdir(log_dir) if n.startswith("klippy.log")]
    except OSError:
        return []
    indexed = []
    for name in names:
        path = os.path.join(log_dir, name)
        if not os.path.isfile(path):
            continue
        first_ts = _file_first_ts(path)
        if first_ts is None:
            continue
        indexed.append((first_ts, path))
    indexed.sort(key=lambda t: t[0])
    return indexed


def _select_files(indexed, window_start, window_end):
    """Which indexed (first_ts, path) files actually overlap
    [window_start, window_end]. A file's coverage runs from its own
    first_ts to the NEXT file's first_ts (or "now" for the last file)."""
    out = []
    for i, (first_ts, path) in enumerate(indexed):
        file_end = indexed[i + 1][0] if i + 1 < len(indexed) else float("inf")
        if file_end < window_start or first_ts > window_end:
            continue
        out.append((first_ts, path))
    return out


def analyse_logs(log_dir, window_start, window_end,
                  max_lines=DEFAULT_MAX_LINES,
                  max_seconds=DEFAULT_MAX_SECONDS):
    """Everything plan §2.3 can pull out of `log_dir` for the window
    [window_start, window_end] (unix seconds). Never raises - a missing
    directory, an unreadable file or a budget exhaustion all degrade to a
    partial (or empty) result with that fact recorded in the payload
    rather than failing the report (plan §2.3's own rule, same reasoning
    as job_history.py's "history is never worth failing a print over").
    """
    result = {k: (list(v) if isinstance(v, list) else v)
              for k, v in EMPTY.items()}
    result["from"], result["to"] = window_start, window_end

    indexed = _list_log_files(log_dir)
    if not indexed:
        result["rotated_away"] = os.path.isdir(log_dir)
        return result

    selected = _select_files(indexed, window_start, window_end)
    result["rotated_away"] = window_start < indexed[0][0]
    if not selected:
        return result
    result["files"] = [os.path.basename(p) for _, p in selected]

    start_wall = time.monotonic()
    lines_seen = 0
    truncated = False

    flood_counts = {}
    flood_first_t = {}
    last_stats_t = None

    for first_ts, path in selected:
        # first_ts was already resolved against the file's own mtime
        # (_file_first_ts), so its YEAR is correct for this file's first
        # line - but a file spanning a New Year's rollover needs that
        # year bumped partway through, tracked via _line_month below
        # rather than re-resolved against mtime a second time.
        year_hint = time.localtime(first_ts).tm_year
        last_month = time.localtime(first_ts).tm_mon
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                lines_seen += 1
                if lines_seen > max_lines:
                    truncated = True
                    break
                if (lines_seen % 2000 == 0
                        and time.monotonic() - start_wall > max_seconds):
                    truncated = True
                    break

                sig = _flood_signature(line)
                cnt = flood_counts.get(sig, 0) + 1
                flood_counts[sig] = cnt
                if cnt == 1:
                    flood_first_t[sig] = lines_seen

                mo = _line_month(line)
                if mo is not None:
                    if last_month is not None and mo < last_month - 6:
                        year_hint += 1
                    last_month = mo

                if not any(frag in line for frag in _INTERESTING):
                    continue

                t = _parse_ts(line, year_hint)
                if t is not None and (t < window_start - 3600
                                       or t > window_end + 3600):
                    # Prefiltered-in line, but well outside the window
                    # (another print's noise in the same file) - still
                    # worth it over a per-line regex that found nothing.
                    continue

                m = _RE_TIMER_CLOSE.search(line)
                if m:
                    wake, read = float(m.group(1)), float(m.group(2))
                    result["host_stalls"].append(
                        {"t": t, "lateness_s": round(wake - read, 6),
                         "line": line.rstrip()})
                    continue

                m = _RE_MCU_SHUTDOWN.search(line)
                if m:
                    result["mcu_shutdowns"].append(
                        {"t": t, "mcu": m.group(1), "reason": m.group(2).strip(),
                         "line": line.rstrip()})
                    continue

                sm = _RE_STATS_PREFIX.search(line)
                if sm:
                    stats_t = t if t is not None else float(sm.group(1))
                    if (last_stats_t is not None
                            and stats_t - last_stats_t > _REACTOR_STALL_THRESHOLD_S):
                        result["reactor_stalls"].append(
                            {"t": last_stats_t,
                             "blocked_for_s": round(stats_t - last_stats_t, 1)})
                    last_stats_t = stats_t
                    pm = _RE_PRINT_STALL.search(line)
                    if pm and int(pm.group(1)) > 0:
                        result["print_stalls"].append(
                            {"t": t, "count": int(pm.group(1))})
                    bm = _RE_BUFFER_TIME.search(line)
                    if bm:
                        sample = {"t": t, "buffer_time": float(bm.group(1))}
                        lm = _RE_SYSLOAD.search(line)
                        if lm:
                            sample["sysload"] = float(lm.group(1))
                        mm = _RE_MEMAVAIL.search(line)
                        if mm:
                            sample["memavail"] = int(mm.group(1))
                        result["health_samples"].append(sample)
                    continue

                if ("pogopin not connected" in line or "is detached" in line
                        or "conflicting status" in line):
                    result["pickup_failures"].append(
                        {"t": t, "line": line.rstrip()})
                    continue

                m = _RE_SD_POSITION.search(line)
                if m:
                    result["sd_positions"].append(
                        {"t": t, "phase": m.group(1).lower(),
                         "position": int(m.group(2))})
                    continue

                if "comms lost" in line or "Try connecting ACE" in line:
                    result["ace_comms"].append({"t": t, "line": line.rstrip()})
                    continue
                m = _RE_RECONNECT.search(line)
                if m:
                    result["ace_comms"].append(
                        {"t": t, "attempt": int(m.group(1)), "line": line.rstrip()})
                    continue

                if "[spool] print" in line:
                    result["spool_ledger"].append({"t": t, "line": line.rstrip()})
                    continue
        if truncated:
            break

    # Downsample health samples (plan §2.3's budget spirit applies to the
    # payload size too - a 51 h print at 1 Hz Stats lines is ~180k samples).
    if len(result["health_samples"]) > 600:
        step = len(result["health_samples"]) // 600 or 1
        result["health_samples"] = result["health_samples"][::step]

    # The flood signal: the single fastest-repeating line signature,
    # measured over the SCAN (lines_seen), not wall time - a signature
    # seen 400 times across 800 lines is "every other line", regardless
    # of how long the scan itself took.
    if flood_counts:
        sig, cnt = max(flood_counts.items(), key=lambda kv: kv[1])
        span_lines = max(1, lines_seen - flood_first_t[sig])
        rate = cnt / span_lines
        if cnt >= 20 and rate > _FLOOD_HZ / 50.0:
            # rate here is "repeats per line scanned", not per second (no
            # per-line timestamp guarantee on every line) - every ~50
            # lines is already the kind of flood plan §2.3 called out.
            result["log_flood"] = {"count": cnt, "sample": sig.strip()}

    result["truncated"] = truncated
    return result

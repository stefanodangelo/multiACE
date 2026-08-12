"""multiACE print history: the store, the Moonraker join, and calibration
(plan §4).

Moonraker already keeps job history, and it is authoritative for duration
and result. multiACE keeps the half Moonraker cannot know: which plan ran,
where each colour was, how many swaps really happened, how long they took,
which of them backgrounded, and how many load retries and neighbour
retracts it cost. This module holds both halves and the rule for joining
them - it does NOT duplicate Moonraker's data.

Pure stdlib, no Klipper and no FastAPI imports: ace.py appends to it from
the printer side and the web backend reads it, and both have to be able to
without dragging the other's dependencies in.
"""
from __future__ import annotations

import json
import os
import time

#: Bounded on purpose - this lives on the printer's flash.
MAX_JOBS = 200
MAX_BYTES = 2 * 1024 * 1024

#: ±90 s around the start time, per §4.2. Wide enough for the lag between
#: Klipper starting the job and Moonraker recording it; narrow enough that
#: two unrelated prints of the same file rarely collide.
JOIN_WINDOW_S = 90.0

#: §4.3 will not call a kind calibrated on fewer samples than this.
MIN_CALIBRATION_SAMPLES = 5


def _jobs_path(data_dir):
    return os.path.join(data_dir, "jobs.jsonl")


def _stats_path(data_dir):
    return os.path.join(data_dir, "swap_stats.json")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def append_record(data_dir, record):
    """Append one job record. Append-only JSONL, never rewritten in place:
    a partial rewrite of a history file on a printer that lost power is a
    lost history file.

    Returns the record (with `id` filled in) or None when it could not be
    written - history is never worth failing a print over.
    """
    try:
        os.makedirs(data_dir, exist_ok=True)
        rec = dict(record)
        rec.setdefault("id", "%d-%s" % (int(time.time() * 1000),
                                        os.urandom(3).hex()))
        rec.setdefault("ts", time.time())
        with open(_jobs_path(data_dir), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
        rotate(data_dir)
        return rec
    except Exception:
        return None


def update_record(data_dir, job_id, **fields):
    """Close out a record by appending a follow-up entry with the same id.

    Append rather than edit: the reader folds entries sharing an id into
    one, so a completion never has to seek into a file that another process
    may be writing.
    """
    if not job_id:
        return None
    return append_record(data_dir, dict(fields, id=job_id))


def rotate(data_dir):
    """Trim the file to MAX_JOBS records / MAX_BYTES, newest kept."""
    path = _jobs_path(data_dir)
    try:
        if os.path.getsize(path) <= MAX_BYTES:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) <= MAX_JOBS * 2:
                return
        else:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
    except OSError:
        return
    keep = lines[-(MAX_JOBS * 2):]
    while keep and sum(len(l.encode("utf-8")) for l in keep) > MAX_BYTES:
        keep = keep[len(keep) // 4 or 1:]
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def load_records(data_dir, limit=None):
    """Every multiACE job record, newest first, follow-ups folded in.

    A corrupt line (half-written when the printer lost power) is skipped
    rather than failing the read - one bad line must not hide the history.
    """
    try:
        with open(_jobs_path(data_dir), "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    merged = {}
    order = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        key = rec.get("id")
        if key is None:
            continue
        if key in merged:
            merged[key].update(rec)
        else:
            merged[key] = rec
            order.append(key)
    out = [merged[k] for k in reversed(order)]
    return out[:limit] if limit else out


def delete_record(data_dir, job_id):
    """Drop every entry for one job. Returns True when something went."""
    path = _jobs_path(data_dir)
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return False
    keep = []
    removed = False
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            keep.append(line)
            continue
        if isinstance(rec, dict) and rec.get("id") == job_id:
            removed = True
            continue
        keep.append(line)
    if not removed:
        return False
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(keep)
    os.replace(tmp, path)
    return True


def clear_records(data_dir):
    try:
        os.remove(_jobs_path(data_dir))
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# The join (§4.2)
# ---------------------------------------------------------------------------

def _basename(name):
    return os.path.basename(str(name or "").replace("\\", "/"))


def join_history(moonraker_jobs, multiace_records, window_s=JOIN_WINDOW_S):
    """Merge Moonraker's job list with multiACE's own records.

    The join key is basename(filename) + start time within ±window_s, taking
    the nearest candidate. When two jobs of the same file start inside that
    window, both are marked `ambiguous` and the multiACE record is shown
    UNJOINED rather than attached to a guess - a plan shown against the
    wrong print is worse than a plan shown against no print.

    Moonraker is authoritative for duration and result; multiACE is
    authoritative for everything multiACE knows (plan, assignment, swaps).
    If a job_id ever becomes reachable from the Klipper side it becomes the
    preferred key with this matcher as the fallback - no schema change.
    """
    remaining = list(multiace_records or [])
    out = []
    used = set()

    for job in (moonraker_jobs or []):
        name = _basename(job.get("filename"))
        start = float(job.get("start_time") or 0.0)
        cands = []
        for i, rec in enumerate(remaining):
            if i in used:
                continue
            if _basename(rec.get("filename")) != name:
                continue
            rec_start = float(rec.get("start_time") or rec.get("ts") or 0.0)
            delta = abs(rec_start - start)
            if delta <= window_s:
                cands.append((delta, i))
        cands.sort()
        row = {
            "source":     "moonraker",
            "job_id":     job.get("job_id"),
            "filename":   job.get("filename"),
            "start_time": job.get("start_time"),
            "end_time":   job.get("end_time"),
            "duration":   job.get("total_duration"),
            "print_duration": job.get("print_duration"),
            "status":     job.get("status"),
            "filament_used": job.get("filament_used"),
            "multiace":   None,
            "ambiguous":  False,
        }
        if len(cands) > 1 and cands[0][0] == cands[1][0]:
            row["ambiguous"] = True
        elif cands:
            idx = cands[0][1]
            used.add(idx)
            row["multiace"] = remaining[idx]
            row["id"] = remaining[idx].get("id")
        out.append(row)

    # multiACE records Moonraker never saw (a print started before its
    # history was recording, or a mocked run) still belong in the list.
    for i, rec in enumerate(remaining):
        if i in used:
            continue
        out.append({
            "source":     "multiace",
            "job_id":     None,
            "id":         rec.get("id"),
            "filename":   rec.get("filename"),
            "start_time": rec.get("start_time") or rec.get("ts"),
            "end_time":   rec.get("end_time"),
            "duration":   rec.get("duration"),
            "status":     rec.get("result"),
            "multiace":   rec,
            "ambiguous":  False,
        })

    out.sort(key=lambda r: float(r.get("start_time") or 0.0), reverse=True)
    return out


# ---------------------------------------------------------------------------
# Calibration (§4.3)
# ---------------------------------------------------------------------------

def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return None
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def aggregate_swap_stats(records):
    """Measured medians per swap kind, across completed jobs.

    Medians rather than means: one 40-minute swap where somebody walked up
    and cleared a jam by hand should not move the model.
    """
    by_kind = {}
    for rec in records or []:
        for swap in rec.get("swaps") or []:
            kind = swap.get("kind")
            seconds = swap.get("seconds")
            if not kind or seconds is None:
                continue
            try:
                by_kind.setdefault(kind, []).append(float(seconds))
            except (TypeError, ValueError):
                continue
    out = {}
    for kind, values in by_kind.items():
        out[kind] = {
            "median_s": _median(values),
            "n":        len(values),
            "min_s":    min(values),
            "max_s":    max(values),
            "calibrated": len(values) >= MIN_CALIBRATION_SAMPLES,
        }
    return out


def write_swap_stats(data_dir, stats):
    try:
        os.makedirs(data_dir, exist_ok=True)
        tmp = _stats_path(data_dir) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2, sort_keys=True)
        os.replace(tmp, _stats_path(data_dir))
        return True
    except Exception:
        return False


def refresh_swap_stats(data_dir):
    """Recompute swap_stats.json from the history. Returns the stats."""
    stats = aggregate_swap_stats(load_records(data_dir))
    write_swap_stats(data_dir, stats)
    return stats


def estimate_accuracy(record):
    """How the estimate did against reality, for one job.

    Returns None when the job carries no estimate or never finished - an
    accuracy line invented from a missing number would be worse than no
    line at all.
    """
    est = (record or {}).get("estimate") or {}
    predicted = est.get("total_s")
    actual = (record or {}).get("duration")
    if not predicted or not actual:
        return None
    try:
        predicted, actual = float(predicted), float(actual)
    except (TypeError, ValueError):
        return None
    if predicted <= 0:
        return None
    return {
        "predicted_s": predicted,
        "actual_s":    actual,
        "error_s":     actual - predicted,
        "error_pct":   round(100.0 * (actual - predicted) / predicted, 1),
    }

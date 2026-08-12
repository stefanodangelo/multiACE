"""Config-accurate print time & filament estimate for multiACE (plan §1).

The slicer's `; estimated printing time` and its `M73` lines describe a
NORMAL-mode print: one head, no ACE swap, no purge. multiACE adds, per
mid-print swap:

    tip-form + retract to ACE + (ACE spool change) + load to nozzle
             + seat/press + purge + heat settle

Every mechanical term above has a config parameter in ace.cfg, so it is
COMPUTED here (length / speed). The terms we cannot compute - the ACE's
internal spool change, sensor waits, tool pickup - stay named constants in
`UNMEASURED_S`, seeded so the ace.cfg defaults reproduce today's
`BG_SWAP_COST_INLINE_S` / `BG_SWAP_COST_BG_S`.

Those constants are UNMEASURED GUESSES. They are not calibrated values,
and §4.3's history is what turns them into measured medians. Until then
every estimate this module produces carries `confidence: "modelled"` and
the UI must say *estimated*, never *measured*.

Pure stdlib on purpose: this module runs unchanged in the backend, in the
printer's Python and inside the browser's Pyodide worker, and it must not
import ace.py or the post-processor.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Unmeasured constants
# ---------------------------------------------------------------------------
#
# READ THIS BEFORE CHANGING A NUMBER.
#
# None of these have been measured on a real machine. They were chosen so
# that, with the shipped ace.cfg defaults (feed_speed/retract_speed 80,
# load_length 2100, swap_retract_length 900, seat_overshoot_length 20,
# swap_anti_ooze_retract 10), the modelled cost of an inline swap lands on
# the 210 s that post_process_virtual_toolheads.BG_SWAP_COST_INLINE_S has
# always used, and a background swap on its 30 s. Day one therefore behaves
# exactly as before and every later change is a deliberate constant edit.
#
# Anything derived from a length and a speed is NOT in this table - it is
# computed from the config, because that is the part we actually know.
UNMEASURED_S = {
    # Tip forming: heat, wiggle, cool, break off. A whole macro sequence.
    "tip_form":         55.0,
    # The ACE's own spool change: unwind the outgoing filament past the hub
    # and feed the incoming one up to it. Internal to the unit, not visible
    # as any length/speed pair we control.
    "ace_spool_change": 62.0,
    # Seat/press dwell after the overshoot move (the move itself is computed).
    "seat_press":       6.0,
    # Waiting for the nozzle to recover the setpoint after a cold-ish load.
    "heat_settle":      24.0,
    # Toolhead pickup/dock at the changer. Paid by every toolchange,
    # including a pinned stock-feeder head that does no ACE work at all.
    "tool_pickup":      12.0,
    # Slot/hub filament sensors settling before the load is trusted.
    "sensor_wait":      12.0,
    # The inline remainder of a background swap: the load already happened
    # on the idle head, so only the handover is on the critical path.
    "bg_handover":      18.0,
    # First load of a head before the print body - no tip-form, no spool
    # change, because there is nothing to remove yet.
    "first_load_extra": 0.0,
}

SWAP_KINDS = ("feeder_pin", "same_ace", "cross_ace_inline", "cross_ace_bg",
              "first_load")

#: Kinds whose cost lands on the print's critical path in full.
INLINE_KINDS = ("same_ace", "cross_ace_inline")

# Fallback densities in g/cm3, used only when the slicer wrote no
# `; filament used [g]` line (see parse_header - the Snapmaker Orca fork
# does write it, so this really is a fallback).
MATERIAL_DENSITY = {
    "PLA": 1.24, "PLA+": 1.24, "PLA-CF": 1.29, "SILK": 1.24,
    "PETG": 1.27, "PET": 1.27, "PETG-CF": 1.30,
    "ABS": 1.04, "ASA": 1.07, "PC": 1.20, "PA": 1.14, "PA-CF": 1.20,
    "TPU": 1.21, "PVA": 1.23, "HIPS": 1.04, "PP": 0.90,
}
DEFAULT_DENSITY = 1.24
DEFAULT_DIAMETER_MM = 1.75

#: Conservative default max volumetric flow (mm3/s). Purging faster than
#: the hotend can melt grinds the filament flat and can pop the PTFE
#: coupler (§13.2), so purge time is rate-limited by THIS, never by a raw
#: feedrate.
DEFAULT_MAX_FLOW_MM3_S = 8.0

MATERIAL_MAX_FLOW_MM3_S = {
    "PLA": 12.0, "PETG": 8.0, "ABS": 10.0, "ASA": 10.0,
    "TPU": 3.5, "PC": 8.0, "PA": 8.0, "PVA": 4.0,
}


# ---------------------------------------------------------------------------
# Small colour helpers (self-contained: this module has no dependencies)
# ---------------------------------------------------------------------------

def hex_to_rgb(value):
    """'#ff8800' / 'FF8800' -> (255, 136, 0); None when unparseable."""
    s = (value or "").strip().lower().lstrip("#")
    if len(s) < 6:
        return None
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return None


def _luma(rgb):
    r, g, b = rgb
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def color_distance(from_color, to_color):
    """Perceptual-ish distance between two colours, normalised to 0..1.

    Weighted RGB (the "redmean" approximation) rather than plain Euclidean:
    it costs nothing and it ranks a black->white transition far above a
    red->orange one, which is exactly what the purge model needs. Returns
    None when either colour is unknown - the caller must then fall back to
    the configured constant rather than guess.
    """
    a = hex_to_rgb(from_color)
    b = hex_to_rgb(to_color)
    if a is None or b is None:
        return None
    rmean = (a[0] + b[0]) / 2.0
    dr, dg, db = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    d2 = ((2 + rmean / 256.0) * dr * dr
          + 4 * dg * dg
          + (2 + (255 - rmean) / 256.0) * db * db)
    # Max possible is black<->white; normalise against it so the result is
    # a stable 0..1 regardless of the weights above.
    max_d2 = (2 + 127.5 / 256.0) * 255 ** 2 + 4 * 255 ** 2 + \
             (2 + 127.5 / 256.0) * 255 ** 2
    return min(1.0, (d2 / max_d2) ** 0.5)


def transition_severity(from_color, to_color):
    """0..1 "how hard is this transition to flush", direction-aware.

    Purge is NOT symmetric: white after black needs far more flushing than
    black after white, because a trace of the old colour is invisible in
    the dark one and glaring in the light one. So the symmetric distance is
    the base and going from dark to light adds on top of it.
    Returns None when either colour is unknown.
    """
    d = color_distance(from_color, to_color)
    if d is None:
        return None
    a, b = hex_to_rgb(from_color), hex_to_rgb(to_color)
    lighten = max(0.0, _luma(b) - _luma(a))
    return max(0.0, min(1.0, 0.7 * d + 0.3 * lighten * (1.0 + d)))


# ---------------------------------------------------------------------------
# Header parsing (§1.3)
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([dhms])", re.I)

# One line per metric, comma-separated PER EXTRUDER INDEX - not one line
# per tool. Index in the list = slicer tool index, so [g][2] is T2, and
# trailing 0.00 entries are unused extruders that must be kept as zeros or
# every tool index after them shifts.
_USED_RE = re.compile(
    r"^;\s*filament used\s*\[(mm|cm3|g)\]\s*[:=]\s*(.+)$", re.I)
_TOTAL_G_RE = re.compile(
    r"^;\s*total filament used\s*\[g\]\s*[:=]\s*([\d.]+)", re.I)
_LAYERS_RE = re.compile(r"^;\s*total layers count\s*[:=]\s*(\d+)", re.I)
_TIME_RE = re.compile(
    r"^;\s*estimated printing time(?:\s*\([^)]*\))?\s*[:=]\s*(.+)$", re.I)
_FIRST_LAYER_TIME_RE = re.compile(
    r"^;\s*estimated first layer printing time(?:\s*\([^)]*\))?\s*[:=]\s*(.+)$",
    re.I)
_DENSITY_RE = re.compile(r"^;\s*filament_density\s*[:=]\s*(.+)$", re.I)
_DIAMETER_RE = re.compile(r"^;\s*filament_diameter\s*[:=]\s*(.+)$", re.I)
_TYPE_RE = re.compile(r"^;\s*filament_type\s*[:=]\s*(.+)$", re.I)

#: Header keys that mean the slicer built a prime/wipe tower. Purged
#: material then lands on the tower, which the slicer already extruded -
#: so its time and grams are ALREADY inside base_s and [g].
_TOWER_KEYS = (
    "enable_prime_tower", "prime_tower_enable", "wipe_tower",
    "prime_tower_width", "prime_tower_brim_width", "wipe_tower_x",
    "wipe_tower_width",
)
#: Header keys that mean the slicer flushed into infill/support/objects.
_FLUSH_KEYS = (
    "flush_into_infill", "flush_into_support", "flush_into_objects",
)


def parse_duration(text):
    """'1d 2h 8m 30s' / '8m 30s' / '19s' -> seconds. None if unparseable.

    The slicer writes a DURATION STRING, not seconds, and any combination
    of the four units in any order.
    """
    if text is None:
        return None
    total = 0.0
    found = False
    for value, unit in _DURATION_RE.findall(str(text)):
        found = True
        mult = {"d": 86400.0, "h": 3600.0, "m": 60.0, "s": 1.0}[unit.lower()]
        total += float(value) * mult
    return total if found else None


def _split_floats(text):
    out = []
    for part in re.split(r"[,;]", text):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(float(part))
        except ValueError:
            out.append(0.0)
    return out


def _any_key_truthy(text, keys):
    """True when one of `keys` appears in the header with a non-empty,
    non-zero, non-false value.

    Accepts `=` OR `:` as the separator, matching what parse_color_names
    in the post-processor already had to learn: slicer forks are not
    consistent about it, and a separator we do not accept reads as "key
    absent" - which here would mean "no prime tower" and would add a purge
    the slicer had already paid for.
    """
    for key in keys:
        m = re.search(r"^;\s*%s\s*[:=]\s*(.+)$" % re.escape(key), text,
                      re.I | re.M)
        if not m:
            continue
        for part in re.split(r"[,;]", m.group(1)):
            part = part.strip().lower()
            if part in ("", "0", "0.0", "false", "no", "nil", "none"):
                continue
            return True
    return False


def purge_destination(header_text):
    """Where the purged filament ends up: 'tower' | 'flush' | 'mixed' |
    'bin' | 'unknown'.

    This is the one parse that changes the headline number. A prime tower
    or a flush-into-support target means the slicer ALREADY extruded the
    purge - its seconds are in `base_s` and its grams are in
    `; filament used [g]`. Adding a modelled purge on top would
    double-count the biggest single term in the estimate.

    Read it from the file, never from a config default. When the header
    carries no recognisable slicer settings at all, say 'unknown' and let
    the caller show both totals rather than pick one.
    """
    text = header_text or ""
    tower = _any_key_truthy(text, _TOWER_KEYS)
    flush = _any_key_truthy(text, _FLUSH_KEYS)
    if tower and flush:
        return "mixed"
    if tower:
        return "tower"
    if flush:
        return "flush"
    # No tower and no flush. Only trust that if the header looks like it
    # carries slicer settings at all; an arbitrary gcode tells us nothing.
    if re.search(r"^;\s*[a-z_]+\s*[:=]\s*", text, re.I | re.M):
        return "bin"
    return "unknown"


class HeaderInfo(object):
    """Everything §1 needs out of the slicer's header block."""

    def __init__(self):
        self.per_tool_mm = []
        self.per_tool_cm3 = []
        self.per_tool_g = []
        self.total_g = None
        self.layers = None
        self.base_s = None
        self.first_layer_s = None
        self.densities = []
        self.diameters = []
        self.types = []
        self.purge_destination = "unknown"
        #: 'ok' when the per-tool grams agree with the slicer's own total;
        #: 'degraded' when they do not (we then trust neither silently).
        self.confidence = "ok"
        self.notes = []

    def density_for(self, tool, material=None):
        if tool < len(self.densities) and self.densities[tool]:
            return self.densities[tool]
        if material:
            key = str(material).strip().upper()
            if key in MATERIAL_DENSITY:
                return MATERIAL_DENSITY[key]
        return DEFAULT_DENSITY

    def diameter_for(self, tool):
        if tool < len(self.diameters) and self.diameters[tool]:
            return self.diameters[tool]
        return DEFAULT_DIAMETER_MM

    def grams_for(self, tool, material=None):
        """Grams printed by `tool`, from `[g]` when the slicer wrote it and
        from pi*(d/2)^2*rho otherwise."""
        if tool < len(self.per_tool_g):
            return self.per_tool_g[tool]
        return mm_to_grams(self.mm_for(tool), self.diameter_for(tool),
                           self.density_for(tool, material))

    def mm_for(self, tool):
        if tool < len(self.per_tool_mm):
            return self.per_tool_mm[tool]
        return 0.0

    def as_dict(self):
        return {
            "per_tool_mm": list(self.per_tool_mm),
            "per_tool_g": list(self.per_tool_g),
            "total_g": self.total_g,
            "layers": self.layers,
            "base_s": self.base_s,
            "first_layer_s": self.first_layer_s,
            "purge_destination": self.purge_destination,
            "confidence": self.confidence,
            "notes": list(self.notes),
        }


def mm_to_mm3(length_mm, diameter_mm=DEFAULT_DIAMETER_MM):
    r = (diameter_mm or DEFAULT_DIAMETER_MM) / 2.0
    return 3.141592653589793 * r * r * float(length_mm or 0.0)


def mm_to_grams(length_mm, diameter_mm=DEFAULT_DIAMETER_MM,
                density=DEFAULT_DENSITY):
    return mm_to_mm3(length_mm, diameter_mm) * (density or DEFAULT_DENSITY) \
        / 1000.0


def parse_header(header_text):
    """Parse the slicer's metadata block into a HeaderInfo.

    Tolerant by design: a missing line means the corresponding field stays
    None and the estimate degrades, never raises.
    """
    info = HeaderInfo()
    text = header_text or ""
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith(";"):
            continue
        m = _USED_RE.match(s)
        if m:
            values = _split_floats(m.group(2))
            unit = m.group(1).lower()
            if unit == "mm":
                info.per_tool_mm = values
            elif unit == "cm3":
                info.per_tool_cm3 = values
            else:
                info.per_tool_g = values
            continue
        m = _TOTAL_G_RE.match(s)
        if m:
            info.total_g = float(m.group(1))
            continue
        m = _LAYERS_RE.match(s)
        if m:
            info.layers = int(m.group(1))
            continue
        m = _FIRST_LAYER_TIME_RE.match(s)
        if m:
            info.first_layer_s = parse_duration(m.group(1))
            continue
        m = _TIME_RE.match(s)
        if m:
            info.base_s = parse_duration(m.group(1))
            continue
        m = _DENSITY_RE.match(s)
        if m:
            info.densities = _split_floats(m.group(1))
            continue
        m = _DIAMETER_RE.match(s)
        if m:
            info.diameters = _split_floats(m.group(1))
            continue
        m = _TYPE_RE.match(s)
        if m:
            info.types = [p.strip() for p in re.split(r"[,;]", m.group(1))
                          if p.strip()]
            continue

    if not info.per_tool_g and info.per_tool_mm:
        # No [g] line: fall back to pi*(d/2)^2*rho. A file without [g]
        # normally has no filament_density either, so the per-material
        # table is what actually carries this path.
        info.per_tool_g = [
            mm_to_grams(mm, info.diameter_for(i),
                        info.density_for(i, info.types[i]
                                         if i < len(info.types) else None))
            for i, mm in enumerate(info.per_tool_mm)]
        info.notes.append("no `filament used [g]` - grams derived from "
                          "diameter and the material density table")

    # `; total filament used [g]` is an INDEPENDENT cross-check. On a
    # mismatch mark the estimate degraded rather than silently trusting
    # one of the two numbers.
    if info.total_g is not None and info.per_tool_g:
        summed = sum(info.per_tool_g)
        tol = max(0.05, 0.02 * max(summed, info.total_g))
        if abs(summed - info.total_g) > tol:
            info.confidence = "degraded"
            info.notes.append(
                "per-tool grams (%.2f) disagree with the slicer's total "
                "(%.2f)" % (summed, info.total_g))

    info.purge_destination = purge_destination(text)
    return info


# ---------------------------------------------------------------------------
# The cost model
# ---------------------------------------------------------------------------

class SwapCostModel(object):
    """Seconds and millimetres per swap, from ace.cfg + a constants table.

    `from_params` takes the same scalars ace.py reads out of `[ace]`, so
    the model stays honest when a user changes `load_length` - a longer
    bowden really is a longer swap.
    """

    def __init__(self, params=None, per_ace=None, constants=None,
                 calibration=None):
        p = dict(params or {})
        self.params = p
        self.per_ace = {int(k): dict(v) for k, v in (per_ace or {}).items()}
        self.constants = dict(UNMEASURED_S)
        self.constants.update(constants or {})
        #: {kind: {"median_s": float, "n": int}} from §4.3's swap_stats.json.
        self.calibration = dict(calibration or {})
        self.min_calibration_samples = 5

        self.feed_speed = float(p.get("feed_speed", 80) or 80)
        self.retract_speed = float(p.get("retract_speed", 80) or 80)
        self.load_length = float(p.get("load_length", 2100) or 0)
        self.retract_length = float(p.get("retract_length", 1950) or 0)
        self.swap_retract_length = float(p.get("swap_retract_length", 900) or 0)
        self.seat_overshoot_length = float(
            p.get("seat_overshoot_length", 20) or 0)
        self.swap_anti_ooze_retract = float(
            p.get("swap_anti_ooze_retract", 10) or 0)
        self.swap_purge_length = float(p.get("swap_purge_length", 0) or 0)

        # §3.4 colour-aware purge. Off by default: over-purging wastes more
        # than it saves, and an overflowing purge bin is a hotend-ending
        # blob (§13.2).
        self.purge_color_aware = bool(p.get("purge_color_aware", False))
        self.swap_purge_min = float(p.get("swap_purge_min", 0) or 0)
        self.swap_purge_max = float(p.get("swap_purge_max", 150) or 0)
        self.purge_material_matrix = dict(p.get("purge_material_matrix") or {})
        self.purge_bin_capacity_mm = float(
            p.get("purge_bin_capacity_mm", 0) or 0)
        self.filament_diameter = float(
            p.get("filament_diameter", DEFAULT_DIAMETER_MM)
            or DEFAULT_DIAMETER_MM)

    # -- construction ---------------------------------------------------

    @classmethod
    def from_params(cls, main_params, per_ace_params=None, constants=None,
                    calibration=None):
        return cls(main_params, per_ace_params, constants, calibration)

    @classmethod
    def default(cls):
        """The shipped ace.cfg defaults - the model a caller gets when the
        printer's real config is not reachable (e.g. offline mock mode)."""
        return cls({
            "feed_speed": 80, "retract_speed": 80,
            "load_length": 2100, "retract_length": 1950,
            "swap_retract_length": 900, "seat_overshoot_length": 20,
            "swap_anti_ooze_retract": 10, "swap_purge_length": 0,
        })

    def with_calibration(self, swap_stats):
        """Return a copy that prefers measured medians over the constants.

        `swap_stats` is §4.3's aggregate: {kind: {"median_s", "n"}}. Only
        kinds with at least `min_calibration_samples` completed jobs are
        trusted - three swaps of anecdote is not a calibration.
        """
        clone = SwapCostModel(self.params, self.per_ace, self.constants,
                              swap_stats)
        return clone

    # -- per-ACE / per-slot resolution -----------------------------------

    def _p(self, name, ace=None, slot=None, default=None):
        """Per-slot override wins over per-ACE, which wins over the global -
        the same precedence ace.py's get_swap_retract_length uses."""
        if ace is not None:
            sec = self.per_ace.get(int(ace)) or {}
            if slot is not None:
                v = sec.get("%s_%s" % (name, int(slot)))
                if v is not None:
                    return float(v)
            v = sec.get(name)
            if v is not None:
                return float(v)
        v = getattr(self, name, None)
        return float(v) if v is not None else default

    # -- the model ------------------------------------------------------

    def _mechanical_seconds(self, kind, ace=None, slot=None):
        """The part we can actually compute: length / speed."""
        feed = max(1.0, self._p("feed_speed", ace, slot, 80.0))
        retract = max(1.0, self._p("retract_speed", ace, slot, 80.0))
        load_len = self._p("load_length", ace, slot, 0.0)
        seat = self._p("seat_overshoot_length", ace, slot, 0.0)
        if kind == "feeder_pin":
            return 0.0
        if kind == "first_load":
            return load_len / feed + (2.0 * seat) / feed
        if kind == "cross_ace_bg":
            # The unload and the reload happen on the idle head while the
            # other one keeps printing, so none of their mechanical time is
            # on the critical path.
            return 0.0
        pull = (self._p("swap_retract_length", ace, slot, 0.0)
                + self._p("swap_anti_ooze_retract", ace, slot, 0.0))
        return pull / retract + load_len / feed + (2.0 * seat) / feed

    def _constant_seconds(self, kind):
        c = self.constants
        if kind == "feeder_pin":
            return c["tool_pickup"]
        if kind == "cross_ace_bg":
            return c["tool_pickup"] + c["bg_handover"]
        if kind == "first_load":
            return (c["tool_pickup"] + c["seat_press"] + c["heat_settle"]
                    + c["sensor_wait"] + c["first_load_extra"])
        return (c["tip_form"] + c["ace_spool_change"] + c["seat_press"]
                + c["heat_settle"] + c["tool_pickup"] + c["sensor_wait"])

    def calibrated_kinds(self):
        return sorted(
            k for k, v in self.calibration.items()
            if isinstance(v, dict)
            and int(v.get("n", 0) or 0) >= self.min_calibration_samples
            and v.get("median_s") is not None)

    def confidence(self):
        return "calibrated" if self.calibrated_kinds() else "modelled"

    def swap_seconds(self, kind, ace=None, slot=None, from_color=None,
                     to_color=None, material=None):
        """Seconds this swap adds to the print's critical path.

        `kind` is one of SWAP_KINDS - §3.1's whole point is that where a
        colour sits decides what a change to it costs, so the caller must
        classify the swap before asking.
        """
        if kind not in SWAP_KINDS:
            raise ValueError("unknown swap kind: %r" % (kind,))
        measured = self.calibration.get(kind)
        if (isinstance(measured, dict)
                and int(measured.get("n", 0) or 0) >= self.min_calibration_samples
                and measured.get("median_s") is not None):
            base = float(measured["median_s"])
        else:
            base = self._mechanical_seconds(kind, ace, slot) \
                + self._constant_seconds(kind)
        return base + self.purge_seconds(
            self.purge_mm(from_color, to_color, material), material)

    def bg_window_minutes(self, ace=None, slot=None):
        """How much idle time a background swap actually needs, in minutes.

        `BG_UNLOAD_MIN_WINDOW_MIN = 1` was a constant, and a 1-minute window
        would call feasible what a 2100 mm load cannot possibly fit. This is
        the real unload+load duration plus a margin.
        """
        seconds = (self._mechanical_seconds("cross_ace_inline", ace, slot)
                   + self.constants["tip_form"]
                   + self.constants["ace_spool_change"]
                   + self.constants["sensor_wait"])
        return round((seconds * 1.25) / 60.0, 2)

    # -- purge ----------------------------------------------------------

    def purge_mm(self, from_color=None, to_color=None, material=None):
        """Millimetres of filament to purge for this transition.

        With `purge_color_aware` off (the default) this is just the
        configured `swap_purge_length`. With it on, interpolate between
        swap_purge_min and swap_purge_max on the direction-aware transition
        severity, floored by the material-pair minimum.
        """
        floor = self._material_floor(material)
        if not self.purge_color_aware:
            return max(float(self.swap_purge_length), floor)
        severity = transition_severity(from_color, to_color)
        if severity is None:
            # Unknown colour: fall back to the configured constant rather
            # than guess a volume for the hotend to extrude.
            return max(float(self.swap_purge_length), floor)
        lo, hi = float(self.swap_purge_min), float(self.swap_purge_max)
        if hi < lo:
            lo, hi = hi, lo
        return max(lo + (hi - lo) * severity, floor)

    def _material_floor(self, material):
        if not material:
            return 0.0
        key = str(material).strip().upper()
        try:
            return float(self.purge_material_matrix.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def max_flow_mm3_s(self, material=None):
        if material:
            key = str(material).strip().upper()
            if key in MATERIAL_MAX_FLOW_MM3_S:
                return MATERIAL_MAX_FLOW_MM3_S[key]
        try:
            return float(self.params.get("max_flow_mm3_s")
                         or DEFAULT_MAX_FLOW_MM3_S)
        except (TypeError, ValueError):
            return DEFAULT_MAX_FLOW_MM3_S

    def purge_seconds(self, purge_mm, material=None):
        """Extrude time for `purge_mm`, rate-limited by VOLUMETRIC FLOW.

        Never by a raw feedrate: purging faster than the hotend can melt is
        how the extruder grinds the filament flat (§13.2).
        """
        if not purge_mm:
            return 0.0
        mm3 = mm_to_mm3(purge_mm, self.filament_diameter)
        return mm3 / max(0.1, self.max_flow_mm3_s(material))

    def purge_grams(self, purge_mm, material=None):
        key = str(material or "").strip().upper()
        density = MATERIAL_DENSITY.get(key, DEFAULT_DENSITY)
        return mm_to_grams(purge_mm, self.filament_diameter, density)

    def clamp_purge_mm(self, requested_mm, already_used_mm=0.0):
        """Clamp a purge request to what this machine can physically take.

        The number in the gcode is an upper REQUEST, never an instruction:
        the file may have been sliced against a different machine, with a
        different purge bin. Returns (applied_mm, reason) where reason is
        '' when nothing was clamped.
        """
        try:
            value = max(0.0, float(requested_mm or 0.0))
        except (TypeError, ValueError):
            return 0.0, "unparseable purge request"
        reason = ""
        ceiling = float(self.swap_purge_max or 0.0)
        if ceiling > 0 and value > ceiling:
            value, reason = ceiling, "clamped to swap_purge_max"
        cap = float(self.purge_bin_capacity_mm or 0.0)
        if cap > 0:
            remaining = max(0.0, cap - max(0.0, float(already_used_mm or 0.0)))
            if value > remaining:
                value = remaining
                reason = ("purge bin full - clamped to remaining capacity"
                          if remaining > 0 else "purge bin full - refused")
        return value, reason


# ---------------------------------------------------------------------------
# The estimate block (§1.3)
# ---------------------------------------------------------------------------

def _round(value, digits=1):
    return None if value is None else round(float(value), digits)


def build_estimate(model, header, timeline, *, materials=None, colors=None,
                   used_tools=None, extra_assumptions=None):
    """Assemble the `estimate` block the report attaches to each plan.

    `timeline` is §3.2's per-event trace: a list of dicts with at least
    `kind`, and optionally `t`, `seconds`, `purge_mm`. Everything else -
    grams, per-colour breakdown, the purge double-counting decision - is
    derived here so backend and browser produce identical numbers.
    """
    materials = materials or {}
    colors = colors or {}
    assumptions = list(extra_assumptions or [])

    counts = {"inline": 0, "bg": 0, "pin": 0, "unknown_window": 0,
              "first_load": 0}
    added_s = 0.0
    purge_mm_total = 0.0
    per_tool_purge_mm = {}

    for ev in timeline or []:
        kind = ev.get("kind")
        seconds = ev.get("seconds")
        if seconds is None:
            seconds = model.swap_seconds(
                kind if kind in SWAP_KINDS else "cross_ace_inline",
                ace=ev.get("ace"), slot=ev.get("slot"),
                material=materials.get(ev.get("t")))
        purge = ev.get("purge_mm")
        if purge is None:
            purge = model.purge_mm(colors.get(ev.get("from_t")),
                                   colors.get(ev.get("t")),
                                   materials.get(ev.get("t")))
        added_s += float(seconds or 0.0)
        purge_mm_total += float(purge or 0.0)
        t = ev.get("t")
        if t is not None:
            per_tool_purge_mm[t] = per_tool_purge_mm.get(t, 0.0) + float(
                purge or 0.0)
        if kind == "cross_ace_bg":
            counts["bg"] += 1
        elif kind == "feeder_pin":
            counts["pin"] += 1
        elif kind == "first_load":
            counts["first_load"] += 1
        else:
            counts["inline"] += 1
        if ev.get("window_min") is None and kind in ("cross_ace_bg",
                                                     "cross_ace_inline"):
            counts["unknown_window"] += 1

    dest = header.purge_destination if header else "unknown"
    purge_is_extra = dest in ("bin", "unknown")
    if dest in ("tower", "flush", "mixed"):
        # The slicer already extruded this material, in a place with
        # unbounded capacity, at a flow rate it computed itself. Its
        # seconds are in base_s and its grams are in [g]. Counting it
        # again would double-count the biggest term in the estimate.
        label = {"tower": "prime tower", "flush": "flush into infill/support",
                 "mixed": "prime tower + flush"}[dest]
        assumptions.append(
            "%s detected - purge is already in the slicer total, reported "
            "but not added" % label)
        added_purge_s = 0.0
    else:
        added_purge_s = sum(
            model.purge_seconds(mm, materials.get(t))
            for t, mm in per_tool_purge_mm.items())
        if dest == "unknown":
            assumptions.append(
                "no prime-tower or flush setting found in the header - "
                "purge destination unknown, both totals shown")
        else:
            assumptions.append(
                "no prime tower and no flush target - purge goes to the "
                "multiACE bin and is added to the total")

    if not model.purge_color_aware and model.swap_purge_length == 0:
        assumptions.append(
            "swap_purge_length=0 and purge_color_aware off - no purge modelled")

    base_s = header.base_s if header else None
    total_s = None if base_s is None else base_s + added_s

    per_color = []
    totals_mm = 0.0
    totals_g = 0.0
    tools = sorted(used_tools) if used_tools else sorted(
        set(list(per_tool_purge_mm.keys())
            + list(range(len(header.per_tool_mm) if header else 0))))
    for t in tools:
        material = materials.get(t)
        print_mm = header.mm_for(t) if header else 0.0
        print_g = header.grams_for(t, material) if header else 0.0
        pmm = per_tool_purge_mm.get(t, 0.0)
        pg = model.purge_grams(pmm, material)
        totals_mm += print_mm + (pmm if purge_is_extra else 0.0)
        totals_g += print_g + (pg if purge_is_extra else 0.0)
        per_color.append({
            "t": t,
            "print_mm": _round(print_mm), "print_g": _round(print_g, 2),
            "purge_mm": _round(pmm), "purge_g": _round(pg, 2),
            "total_m": _round((print_mm + (pmm if purge_is_extra else 0.0))
                              / 1000.0, 2),
        })

    purge_g_total = sum(model.purge_grams(mm, materials.get(t))
                        for t, mm in per_tool_purge_mm.items())

    confidence = model.confidence()
    if header is not None and header.confidence == "degraded":
        confidence = "degraded"
        assumptions.extend(header.notes)

    out = {
        "base_s": _round(base_s, 0),
        "added_s": _round(added_s + added_purge_s, 0),
        "total_s": _round(None if total_s is None
                          else total_s + added_purge_s, 0),
        "inline_swaps": counts["inline"],
        "bg_swaps": counts["bg"],
        "pin_swaps": counts["pin"],
        "unknown_window_swaps": counts["unknown_window"],
        "purge": {"mm": _round(purge_mm_total), "g": _round(purge_g_total, 2),
                  "destination": dest, "counted_in_total": purge_is_extra},
        "per_color": per_color,
        "totals": {"mm": _round(totals_mm), "m": _round(totals_mm / 1000.0, 2),
                   "g": _round(totals_g, 2)},
        "layers": header.layers if header else None,
        "first_layer_s": _round(header.first_layer_s, 0) if header else None,
        "confidence": confidence,
        "assumptions": assumptions,
    }
    if dest == "unknown":
        # Show both rather than pick one: 'unknown' means we genuinely
        # cannot tell whether the slicer already paid for the purge.
        out["total_s_without_purge"] = _round(
            None if total_s is None else total_s, 0)
    return out

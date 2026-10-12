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
              "first_load", "feeder_swap")

#: Kinds whose cost lands on the print's critical path in full.
INLINE_KINDS = ("same_ace", "cross_ace_inline", "feeder_swap")

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

#: €/kg assumed for a tool whose colour has no priced spool bound (no spool
#: at all, or one added before the price field existed) - same default
#: ACE_SPOOL_ADD and _spool_prices_from_state already apply, so the
#: headline number is always a real estimate the user can correct in
#: Settings -> Spools rather than silently going quiet for that tool.
DEFAULT_PRICE_PER_KG = 20.0

#: Conservative default max volumetric flow (mm3/s). Purging faster than
#: the hotend can melt grinds the filament flat and can pop the PTFE
#: coupler (§13.2), so purge time is rate-limited by THIS, never by a raw
#: feedrate.
DEFAULT_MAX_FLOW_MM3_S = 8.0

MATERIAL_MAX_FLOW_MM3_S = {
    "PLA": 12.0, "PETG": 8.0, "ABS": 10.0, "ASA": 10.0,
    "TPU": 3.5, "PC": 8.0, "PA": 8.0, "PVA": 4.0,
}

#: What the stock INNER_FLUSH_FILAMENT macro (fluidd.cfg) purges when
#: nothing tells it otherwise - `ACE_SWAP_HEAD`/`get_purge_length()` omit
#: LENGTH= and the macro's own `params.LENGTH | default(80.0)` applies.
#: This is what a head-mode inline swap has always actually cost in
#: filament, with no per-pair stamp and swap_purge_length at its 0 default -
#: the number resolve_swap_purge_mm falls back to rather than silently
#: modelling zero purge for the single most common swap shape.
STOCK_FLUSH_DEFAULT_MM = 80.0


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

#: Header keys that explicitly switch the prime/wipe tower on or off.
#: These are the only keys trusted to say "no tower" - an on/off switch
#: that reads false is definitive.
_TOWER_ENABLE_KEYS = ("enable_prime_tower", "prime_tower_enable",
                      "wipe_tower")
#: Tower GEOMETRY keys - width, brim, position. Some slicer forks
#: (Snapmaker Orca among them) write these with their last non-zero value
#: even when the tower itself is disabled, so they are consulted only when
#: the header carries no explicit on/off switch at all (see
#: purge_destination) - never used to override one that says "off".
_TOWER_GEOMETRY_KEYS = (
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


def _any_key_present(text, keys):
    """True when one of `keys` appears in the header at all, whatever its
    value - distinct from `_any_key_truthy`, which also asks whether that
    value is "on". Used to tell "this fork wrote an explicit off-switch"
    from "this fork wrote no switch at all, only geometry"."""
    for key in keys:
        if re.search(r"^;\s*%s\s*[:=]\s*(.+)$" % re.escape(key), text,
                     re.I | re.M):
            return True
    return False


def purge_destination(header_text):
    """Where the SLICER'S OWN purge (if it modelled one at all) ends up:
    'tower' | 'flush' | 'mixed' | 'bin' | 'unknown'.

    Informational only - see build_estimate for why this no longer gates
    whether multiACE's own ACE-swap purge gets added. A prime tower or a
    flush-into-support target means the slicer printed an actual wipe
    tower or flushed into actual support/infill toolpaths, consuming real
    filament the post-processor never strips, so its seconds are in
    `base_s` and its grams are in `; filament used [g]` ALREADY - but that
    is the slicer's own mechanism, a plate feature multiACE neither needs
    nor controls. The ACE swap itself still retracts to the ACE, changes
    spool and purges through the hotend's own chute (INNER_FLUSH_FILAMENT)
    regardless of whether the slicer also built a tower - two separate
    physical events, both real, so both get reported.

    Read it from the file, never from a config default. `enable_prime_tower`
    (etc.) is trusted outright, even when false - a fork can write non-zero
    tower WIDTH/geometry lines whether or not the tower is enabled (seen on
    Snapmaker Orca with enable_prime_tower=0), so geometry is consulted only
    when the header carries no explicit on/off switch at all. When the
    header carries no recognisable slicer settings at all, say 'unknown'
    rather than guessing 'bin'.
    """
    text = header_text or ""
    if _any_key_present(text, _TOWER_ENABLE_KEYS):
        tower = _any_key_truthy(text, _TOWER_ENABLE_KEYS)
    else:
        tower = _any_key_truthy(text, _TOWER_GEOMETRY_KEYS)
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


def mm3_to_mm(volume_mm3, diameter_mm=DEFAULT_DIAMETER_MM):
    """Inverse of mm_to_mm3 - the slicer's flush matrix is in mm3, the cost
    model's purge lengths are in mm of filament."""
    r = (diameter_mm or DEFAULT_DIAMETER_MM) / 2.0
    area = 3.141592653589793 * r * r
    return (float(volume_mm3 or 0.0) / area) if area else 0.0


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

        # Hybrid per-head mode (§3/§4.1): a combo head's feeder-tap leg of a
        # swap uses the feeder's own load/retract config, not the ACE's -
        # the two are physically different runs (local drive gear vs. the
        # ACE's own bowden). No per-head override plumbing here (unlike the
        # ACE per-slot table above) - this model is an estimate, and a
        # global default is enough for that.
        self.feeder_load_length = float(p.get("feeder_load_length", 1100) or 0)
        self.feeder_swap_retract_length = float(
            p.get("feeder_swap_retract_length", 150) or 0)

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

        #: §1.6/Config-tab purge control. A global percentage every
        #: resolved purge length is scaled by (100 = unchanged, 70 = 30 %
        #: less purge everywhere) - the one knob that makes "fewer swaps"
        #: and "less purge per swap" both independently visible in the
        #: estimate and in the real print. `purge_pair_mm` overrides
        #: specific PHYSICAL transitions - keys are "ace:slot>ace:slot"
        #: (the slot a colour sits in, not its slicer T-index, since a
        #: user sets this before knowing which colour lands where) and
        #: win over every other mm source in resolve_swap_purge_mm, but
        #: the scale still applies on top: the slider is "purge, overall",
        #: a pair override is "this transition, specifically" - stacking
        #: them means turning the slider down always helps, even on a
        #: swap that also has a pair override set.
        self.purge_scale_pct = float(p.get("purge_scale_pct", 100) or 100)
        self.purge_pair_mm = dict(p.get("purge_pair_mm") or {})

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
        if kind == "feeder_swap":
            # A combo head's feeder tap: retract/load lengths come from the
            # feeder's own config, not the ACE's (§3.2/§4.1 of the hybrid
            # per-head mode plan) - no per-head override table for these
            # yet, so this is the global default only.
            pull = (self.feeder_swap_retract_length
                    + self._p("swap_anti_ooze_retract", ace, slot, 0.0))
            return pull / retract + self.feeder_load_length / feed \
                + (2.0 * seat) / feed
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
        if kind == "feeder_swap":
            # No ACE spool change involved either direction - the feeder
            # tap has exactly one spool, always ready.
            return (c["tip_form"] + c["seat_press"] + c["heat_settle"]
                    + c["tool_pickup"] + c["sensor_wait"])
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
        """Mechanical + constant seconds this swap adds to the print's
        critical path - NOT including purge time.

        `kind` is one of SWAP_KINDS - §3.1's whole point is that where a
        colour sits decides what a change to it costs, so the caller must
        classify the swap before asking.

        Purge time used to be baked in here via the plain `purge_mm`
        guess, which build_estimate then ALSO added on top from the
        timeline's own (flush-matrix-aware) purge_mm - double-counting
        purge seconds whenever both happened to be non-zero. Purge time
        is now exclusively `build_estimate`'s job, computed once from
        `resolve_swap_purge_mm` so every swap, inline or background,
        tower print or not, is billed its purge exactly once.
        """
        if kind not in SWAP_KINDS:
            raise ValueError("unknown swap kind: %r" % (kind,))
        measured = self.calibration.get(kind)
        if (isinstance(measured, dict)
                and int(measured.get("n", 0) or 0) >= self.min_calibration_samples
                and measured.get("median_s") is not None):
            return float(measured["median_s"])
        return self._mechanical_seconds(kind, ace, slot) \
            + self._constant_seconds(kind)

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

        This is a GUESS, used only when nothing better is available - see
        `purge_mm_for` for the slicer-supplied number that supersedes it.
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

    def purge_mm_for(self, from_t, to_t, flush_matrix=None, from_color=None,
                     to_color=None, material=None):
        """Millimetres to purge for one swap, preferring the SLICER'S OWN
        number over the colour-distance guess.

        `flush_matrix` is the slicer's `flush_volumes_matrix` (mm3,
        row-major NxN, row = from-tool) - parsed straight out of the gcode
        by `parse_flush_matrix`/`parse_flush_matrix_from_file`. The slicer
        already computed exactly how much this specific transition needs;
        guessing a volume from colour distance ON TOP of a real number the
        file already carries would be strictly worse, not additive. Falls
        back to `purge_mm` when there is no matrix, no prior tool (first
        load - nothing to flush), or the pair falls outside it.

        The matrix value still goes through `clamp_purge_mm` - the file may
        have been sliced against a different machine, and a purge-bin
        ceiling exists precisely to catch that regardless of source.
        """
        if (flush_matrix is not None and from_t is not None
                and to_t is not None
                and 0 <= from_t < len(flush_matrix)
                and 0 <= to_t < len(flush_matrix[from_t])):
            mm3 = flush_matrix[from_t][to_t]
            if mm3:
                mm = mm3_to_mm(mm3, self.filament_diameter)
                applied, _reason = self.clamp_purge_mm(mm)
                return max(applied, self._material_floor(material))
        return self.purge_mm(from_color, to_color, material)

    def resolve_swap_purge_mm(self, kind, from_key=None, to_key=None,
                              stamp_mm=None, from_color=None, to_color=None,
                              material=None):
        """mm to purge for ONE swap - the single source of truth shared by
        the rewrite (what the engine will actually extrude through the
        chute) and the estimate (what the preflight predicts), so the two
        never disagree. Resolution order, first match wins:

          1. `feeder_pin` -> 0 (no ACE work at all, nothing to flush)
          2. a Config-tab pair override for the PHYSICAL transition
             `from_key` -> `to_key` (each "ace:slot", e.g. "0:1") -
             `self.purge_pair_mm`
          3. `purge_color_aware`, when explicitly enabled (§3.4) - the
             colour-distance guess, kept exactly as before and orthogonal
             to everything below it
          4. `stamp_mm` - the slicer flush-matrix top-up the rewrite is
             about to stamp for this swap (see
             post_process_virtual_toolheads._matrix_purge_mm) - the
             slicer's own number for this pair, preferred over a guess
          5. `swap_purge_length`, when raised above its 0 default
          6. `STOCK_FLUSH_DEFAULT_MM` - what the engine's own
             INNER_FLUSH_FILAMENT macro purges when nothing above applies
             and no LENGTH= stamp is written at all. This is the one
             most swaps actually hit: the mm `purge_mm`/`purge_mm_for`
             silently round down to 0 for (swap_purge_length=0,
             purge_color_aware=off is the shipped default) actually never
             reach the nozzle - the macro falls back to 80 mm regardless.

        Whatever wins is then scaled by `purge_scale_pct` (100 = no
        change) and run through `clamp_purge_mm` - a pair override is a
        request like any other, not an escape from the purge-bin ceiling.
        """
        if kind == "feeder_pin":
            return 0.0
        floor = self._material_floor(material)
        mm = None
        if from_key is not None and to_key is not None and self.purge_pair_mm:
            mm = self.purge_pair_mm.get("%s>%s" % (from_key, to_key))
        if mm is None and self.purge_color_aware:
            severity = transition_severity(from_color, to_color)
            if severity is not None:
                lo, hi = float(self.swap_purge_min), float(self.swap_purge_max)
                if hi < lo:
                    lo, hi = hi, lo
                mm = lo + (hi - lo) * severity
        if mm is None and stamp_mm:
            mm = float(stamp_mm)
        if mm is None and self.swap_purge_length > 0:
            mm = float(self.swap_purge_length)
        if mm is None:
            mm = STOCK_FLUSH_DEFAULT_MM
        mm = max(float(mm), floor)
        scale = max(0.0, float(self.purge_scale_pct or 0.0)) / 100.0
        mm *= scale
        applied, _reason = self.clamp_purge_mm(mm)
        return applied

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
                   used_tools=None, extra_assumptions=None, prices=None):
    """Assemble the `estimate` block the report attaches to each plan.

    `timeline` is §3.2's per-event trace: a list of dicts with at least
    `kind`, and optionally `t`, `seconds`, `purge_mm`. Everything else -
    grams, per-colour breakdown, the purge total - is derived here so
    backend and browser produce identical numbers.
    """
    materials = materials or {}
    colors = colors or {}
    prices = prices or {}
    assumptions = list(extra_assumptions or [])

    counts = {"inline": 0, "bg": 0, "pin": 0, "unknown_window": 0,
              "first_load": 0}
    added_s = 0.0
    purge_mm_total = 0.0
    per_tool_purge_mm = {}
    purging_swaps = 0

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
            # No post-processor-supplied number (an older installed copy,
            # or a synthetic event built by a test) - resolve one the
            # same way the rewrite would, just without a slicer-matrix
            # stamp to prefer (see resolve_swap_purge_mm).
            purge = model.resolve_swap_purge_mm(
                kind if kind in SWAP_KINDS else "cross_ace_inline",
                from_color=colors.get(ev.get("from_t")),
                to_color=colors.get(ev.get("t")),
                material=materials.get(ev.get("t")))
        added_s += float(seconds or 0.0)
        purge_mm_total += float(purge or 0.0)
        if kind != "feeder_pin" and float(purge or 0.0) > 0:
            purging_swaps += 1
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

    # The ACE-chute purge (INNER_FLUSH_FILAMENT) is a physically SEPARATE
    # event from anything the slicer itself did with its own flush
    # volumes - a prime tower or a flush-into-support target is real
    # filament the slicer already extruded on the plate (never stripped
    # by the post-processor), but the ACE swap still retracts, changes
    # spool and purges through the hotend on top of that. So this is
    # added to every plan's total regardless of `dest` - see
    # purge_destination's docstring for why double-counting was never
    # actually the risk here.
    dest = header.purge_destination if header else "unknown"
    added_purge_s = sum(model.purge_seconds(mm, materials.get(t))
                        for t, mm in per_tool_purge_mm.items())
    if dest in ("tower", "flush", "mixed"):
        label = {"tower": "prime tower", "flush": "flush into infill/support",
                 "mixed": "prime tower + flush"}[dest]
        assumptions.append(
            "%s detected - that's the slicer's own purge, separate from "
            "the ACE-swap purge below" % label)
    elif dest == "unknown":
        assumptions.append(
            "no prime-tower or flush setting found in the header - the "
            "slicer's own purge destination is unknown (informational "
            "only; the ACE-swap purge below is unaffected)")
    if purging_swaps:
        scale = float(model.purge_scale_pct or 100)
        scale_note = "" if scale == 100 else " at %.0f%% of configured purge" % scale
        assumptions.append(
            "%d ACE swap(s) purge through the chute%s - %.1f g added to "
            "the total, on top of anything the slicer itself printed"
            % (purging_swaps, scale_note, sum(
                model.purge_grams(mm, materials.get(t))
                for t, mm in per_tool_purge_mm.items())))

    base_s = header.base_s if header else None
    total_s = None if base_s is None else base_s + added_s

    per_color = []
    totals_mm = 0.0
    totals_g = 0.0
    totals_price = 0.0
    defaulted_g = 0.0
    tools = sorted(used_tools) if used_tools else sorted(
        set(list(per_tool_purge_mm.keys())
            + list(range(len(header.per_tool_mm) if header else 0))))
    for t in tools:
        material = materials.get(t)
        print_mm = header.mm_for(t) if header else 0.0
        print_g = header.grams_for(t, material) if header else 0.0
        pmm = per_tool_purge_mm.get(t, 0.0)
        pg = model.purge_grams(pmm, material)
        # The ACE-chute purge is always extra: it is never the slicer's
        # own filament-used total, whatever purge_destination says (see
        # the note above).
        tool_total_g = print_g + pg
        totals_mm += print_mm + pmm
        totals_g += tool_total_g
        row = {
            "t": t,
            "print_mm": _round(print_mm), "print_g": _round(print_g, 2),
            "purge_mm": _round(pmm), "purge_g": _round(pg, 2),
            "total_m": _round((print_mm + pmm) / 1000.0, 2),
        }
        price_per_kg = prices.get(t)
        if price_per_kg is None:
            price_per_kg = DEFAULT_PRICE_PER_KG
            defaulted_g += tool_total_g
        row_price = tool_total_g / 1000.0 * price_per_kg
        row["price_eur"] = _round(row_price, 2)
        totals_price += row_price
        per_color.append(row)
    any_price_known = bool(tools)

    # Grams priced at the DEFAULT_PRICE_PER_KG guess rather than a spool the
    # user actually bound - the total is still shown (better than silently
    # going quiet for that tool), but flagged so it isn't mistaken for a
    # real number.
    if defaulted_g > 0:
        assumptions.append(
            "%.1f g has no spool bound and is priced at the default "
            "€%.0f/kg - bind a spool in Settings -> Spools to price it "
            "exactly" % (defaulted_g, DEFAULT_PRICE_PER_KG))

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
                  "destination": dest,
                  # Always True now: the ACE-chute purge is always added
                  # to `totals`. Kept (rather than removed) because the
                  # frontend and other consumers already key off it.
                  "counted_in_total": True},
        "per_color": per_color,
        "totals": {"mm": _round(totals_mm), "m": _round(totals_mm / 1000.0, 2),
                   "g": _round(totals_g, 2),
                   "price_eur": _round(totals_price, 2) if any_price_known else None},
        "layers": header.layers if header else None,
        "first_layer_s": _round(header.first_layer_s, 0) if header else None,
        "confidence": confidence,
        "assumptions": assumptions,
    }
    return out

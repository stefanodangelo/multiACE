"""The cost/purge model and the slicer-header parser (plan §1).

Two things are worth pinning down here and nothing else is:

  * the ARITHMETIC is derived from ace.cfg, not invented - a longer bowden
    really has to produce a longer estimate, and a background swap really
    has to be cheaper than an inline one, because the whole loadout
    planner (§3) optimises against these numbers;
  * the HEADER parse, because §1.6's double-counting guard hangs off it.
    A prime tower means the slicer already extruded the purge; adding a
    modelled purge on top would double-count the biggest single term in
    the estimate.
"""
from pathlib import Path

import pytest

from multiace.tools import swap_cost as sc

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

CFG = {
    "feed_speed": 80, "retract_speed": 80,
    "load_length": 2100, "retract_length": 1950,
    "swap_retract_length": 900, "seat_overshoot_length": 20,
    "swap_anti_ooze_retract": 10, "swap_purge_length": 0,
}


@pytest.fixture
def model():
    return sc.SwapCostModel.from_params(CFG)


class TestSwapArithmetic:
    def test_terms_come_from_the_config(self, model):
        """Term by term: retract 910 mm @ 80, load 2100 mm @ 80, seat
        2x20 mm @ 80, plus the named unmeasured constants."""
        mech = (910 / 80.0) + (2100 / 80.0) + (40 / 80.0)
        const = sum(sc.UNMEASURED_S[k] for k in (
            "tip_form", "ace_spool_change", "seat_press", "heat_settle",
            "tool_pickup", "sensor_wait"))
        assert model.swap_seconds("same_ace") == pytest.approx(mech + const)

    def test_day_one_matches_the_old_hardcoded_costs(self, model):
        """The constants were seeded so the shipped defaults reproduce
        BG_SWAP_COST_INLINE_S=210 / BG_SWAP_COST_BG_S=30. If this drifts,
        every existing plan silently changes shape."""
        assert model.swap_seconds("cross_ace_inline") == pytest.approx(210,
                                                                       abs=2)
        assert model.swap_seconds("cross_ace_bg") == pytest.approx(30, abs=1)

    def test_a_longer_bowden_costs_more(self, model):
        slow = sc.SwapCostModel.from_params(dict(CFG, load_length=4200))
        assert slow.swap_seconds("same_ace") > model.swap_seconds("same_ace")

    def test_a_faster_feed_costs_less(self, model):
        fast = sc.SwapCostModel.from_params(dict(CFG, feed_speed=160))
        assert fast.swap_seconds("same_ace") < model.swap_seconds("same_ace")

    def test_background_beats_inline(self, model):
        """§3.1's whole lever: the other head loads while this one prints."""
        assert model.swap_seconds("cross_ace_bg") \
            < model.swap_seconds("cross_ace_inline")

    def test_a_pinned_feeder_is_the_cheapest_of_all(self, model):
        assert model.swap_seconds("feeder_pin") \
            < model.swap_seconds("cross_ace_bg")

    def test_a_background_swap_ignores_the_bowden_length(self, model):
        """None of the mechanical time is on the critical path, so a longer
        load must not make a bg swap look worse - that would push the
        optimizer away from exactly the placement it should prefer."""
        long_bowden = sc.SwapCostModel.from_params(dict(CFG,
                                                        load_length=4200))
        assert long_bowden.swap_seconds("cross_ace_bg") \
            == pytest.approx(model.swap_seconds("cross_ace_bg"))

    def test_unknown_kind_is_a_programming_error(self, model):
        with pytest.raises(ValueError):
            model.swap_seconds("teleport")

    def test_per_ace_overrides_win_over_the_global(self):
        m = sc.SwapCostModel.from_params(
            CFG, {1: {"swap_retract_length": 1800}})
        assert m.swap_seconds("same_ace", ace=1) \
            > m.swap_seconds("same_ace", ace=0)

    def test_per_slot_overrides_win_over_per_ace(self):
        m = sc.SwapCostModel.from_params(
            CFG, {1: {"swap_retract_length": 1800,
                      "swap_retract_length_2": 300}})
        assert m.swap_seconds("same_ace", ace=1, slot=2) \
            < m.swap_seconds("same_ace", ace=1, slot=0)


class TestBgWindow:
    def test_the_window_is_derived_not_a_constant(self, model):
        """BG_UNLOAD_MIN_WINDOW_MIN=1 would call a window feasible that a
        2100 mm load cannot fit."""
        assert model.bg_window_minutes() > 1.0

    def test_a_longer_load_needs_a_longer_window(self, model):
        long_bowden = sc.SwapCostModel.from_params(dict(CFG,
                                                        load_length=4200))
        assert long_bowden.bg_window_minutes() > model.bg_window_minutes()


class TestCalibration:
    def test_modelled_until_history_says_otherwise(self, model):
        assert model.confidence() == "modelled"

    def test_measured_medians_win_once_there_are_enough_samples(self, model):
        cal = model.with_calibration(
            {"cross_ace_inline": {"median_s": 300.0, "n": 8}})
        assert cal.swap_seconds("cross_ace_inline") == pytest.approx(300.0)
        assert cal.confidence() == "calibrated"

    def test_three_swaps_of_anecdote_is_not_a_calibration(self, model):
        cal = model.with_calibration(
            {"cross_ace_inline": {"median_s": 300.0, "n": 3}})
        assert cal.swap_seconds("cross_ace_inline") \
            == pytest.approx(model.swap_seconds("cross_ace_inline"))
        assert cal.confidence() == "modelled"

    def test_calibrating_one_kind_leaves_the_others_modelled(self, model):
        cal = model.with_calibration({"same_ace": {"median_s": 400.0, "n": 9}})
        assert cal.swap_seconds("same_ace") == pytest.approx(400.0)
        assert cal.swap_seconds("cross_ace_bg") \
            == pytest.approx(model.swap_seconds("cross_ace_bg"))


class TestPurgeModel:
    def test_off_by_default_uses_the_configured_length(self, model):
        assert model.purge_mm("#000000", "#ffffff") == 0.0

    def test_color_aware_purges_more_for_a_bigger_jump(self):
        m = sc.SwapCostModel.from_params(
            dict(CFG, purge_color_aware=True, swap_purge_min=20,
                 swap_purge_max=150))
        near = m.purge_mm("#ff0000", "#ff2200")
        far = m.purge_mm("#000000", "#ffffff")
        assert 20 <= near < far <= 150

    def test_purge_is_monotone_in_colour_distance(self):
        m = sc.SwapCostModel.from_params(
            dict(CFG, purge_color_aware=True, swap_purge_min=20,
                 swap_purge_max=150))
        steps = ["#000000", "#404040", "#808080", "#c0c0c0", "#ffffff"]
        got = [m.purge_mm("#000000", c) for c in steps]
        assert got == sorted(got)

    def test_white_after_black_costs_more_than_black_after_white(self):
        """The asymmetry is the point of §3.4: a trace of black in white is
        glaring, a trace of white in black is invisible."""
        m = sc.SwapCostModel.from_params(
            dict(CFG, purge_color_aware=True, swap_purge_min=20,
                 swap_purge_max=150))
        assert m.purge_mm("#000000", "#ffffff") > m.purge_mm("#ffffff",
                                                             "#000000")

    def test_an_unknown_colour_falls_back_to_the_constant(self):
        """Never guess a volume for the hotend to extrude."""
        m = sc.SwapCostModel.from_params(
            dict(CFG, purge_color_aware=True, swap_purge_min=20,
                 swap_purge_max=150, swap_purge_length=35))
        assert m.purge_mm(None, "#ffffff") == 35

    def test_the_material_floor_holds(self):
        m = sc.SwapCostModel.from_params(
            dict(CFG, purge_color_aware=True, swap_purge_min=0,
                 swap_purge_max=150, purge_material_matrix={"TPU": 90}))
        assert m.purge_mm("#ff0000", "#ff1100", material="TPU") >= 90

    def test_purge_time_is_rate_limited_by_volumetric_flow(self, model):
        """Not by a raw feedrate - purging faster than the hotend can melt
        grinds the filament flat (§13.2)."""
        pla = model.purge_seconds(100, "PLA")
        tpu = model.purge_seconds(100, "TPU")
        assert tpu > pla > 0

    def test_purge_zero_is_free(self, model):
        assert model.purge_seconds(0) == 0.0


class TestPurgeFromFlushMatrix:
    """§1: the slicer already computes a real purge volume per transition
    (flush_volumes_matrix, mm3) - purge_mm_for must prefer that over the
    colour-distance guess, since guessing on top of a real number would be
    strictly worse, not additive."""

    def test_the_matrix_value_wins_over_the_guess(self, model):
        matrix = [[0, 120], [80, 0]]
        got = model.purge_mm_for(0, 1, matrix, "#000000", "#ffffff")
        expected = sc.mm3_to_mm(120, sc.DEFAULT_DIAMETER_MM)
        assert got == pytest.approx(expected)

    def test_no_matrix_falls_back_to_the_guess(self, model):
        assert model.purge_mm_for(0, 1, None, "#000000", "#ffffff") \
            == model.purge_mm("#000000", "#ffffff")

    def test_a_first_load_has_no_from_tool_and_falls_back(self, model):
        matrix = [[0, 120], [80, 0]]
        assert model.purge_mm_for(None, 1, matrix, None, "#ffffff") \
            == model.purge_mm(None, "#ffffff")

    def test_an_out_of_range_pair_falls_back(self, model):
        matrix = [[0, 120], [80, 0]]
        assert model.purge_mm_for(0, 5, matrix, "#000000", "#ffffff") \
            == model.purge_mm("#000000", "#ffffff")

    def test_a_zero_cell_falls_back_rather_than_reporting_zero(self, model):
        """A same-colour transition legitimately purges nothing per the
        slicer's own matrix, but 0 here is indistinguishable from 'this
        pair was never sliced' - fall back to the guess (0 by default)."""
        matrix = [[0, 0], [0, 0]]
        assert model.purge_mm_for(0, 1, matrix, "#000000", "#ffffff") \
            == model.purge_mm("#000000", "#ffffff")

    def test_the_matrix_value_is_still_clamped(self):
        m = sc.SwapCostModel.from_params(dict(CFG, swap_purge_max=50))
        matrix = [[0, 900], [80, 0]]
        assert m.purge_mm_for(0, 1, matrix) == 50

    def test_the_material_floor_still_applies(self):
        m = sc.SwapCostModel.from_params(
            dict(CFG, purge_material_matrix={"TPU": 90}))
        matrix = [[0, 10], [80, 0]]
        assert m.purge_mm_for(0, 1, matrix, material="TPU") >= 90


class TestPurgeClamp:
    """The number in the gcode is an upper REQUEST, never an instruction:
    the file may have been sliced against a different machine."""

    def test_a_request_above_the_ceiling_is_clamped(self):
        m = sc.SwapCostModel.from_params(dict(CFG, swap_purge_max=150))
        applied, reason = m.clamp_purge_mm(900)
        assert applied == 150
        assert "swap_purge_max" in reason

    def test_a_request_within_the_ceiling_passes_through(self):
        m = sc.SwapCostModel.from_params(dict(CFG, swap_purge_max=150))
        assert m.clamp_purge_mm(90) == (90, "")

    def test_bin_capacity_beats_the_per_swap_ceiling(self):
        """A per-swap ceiling alone does not prevent a blob: ten safe
        purges still overflow the bin."""
        m = sc.SwapCostModel.from_params(
            dict(CFG, swap_purge_max=150, purge_bin_capacity_mm=1000))
        applied, reason = m.clamp_purge_mm(150, already_used_mm=950)
        assert applied == 50
        assert "bin" in reason

    def test_a_full_bin_refuses_entirely(self):
        m = sc.SwapCostModel.from_params(
            dict(CFG, swap_purge_max=150, purge_bin_capacity_mm=1000))
        applied, reason = m.clamp_purge_mm(150, already_used_mm=1000)
        assert applied == 0
        assert "refused" in reason

    def test_garbage_clamps_to_zero_rather_than_raising(self):
        m = sc.SwapCostModel.from_params(CFG)
        assert m.clamp_purge_mm("PURGE")[0] == 0.0

    def test_a_negative_request_clamps_to_zero(self):
        m = sc.SwapCostModel.from_params(CFG)
        assert m.clamp_purge_mm(-50)[0] == 0.0


class TestDurationParsing:
    @pytest.mark.parametrize("text,seconds", [
        ("8m 30s", 510),
        ("19s", 19),
        ("5h 4m 0s", 18240),
        ("1d 2h 3m 4s", 93784),
        ("2h", 7200),
    ])
    def test_duration_strings(self, text, seconds):
        assert sc.parse_duration(text) == seconds

    def test_unparseable_is_none_not_zero(self):
        """Zero would silently read as 'this print takes no time'."""
        assert sc.parse_duration("who knows") is None
        assert sc.parse_duration(None) is None


class TestHeaderParsing:
    @pytest.fixture
    def header(self):
        return sc.parse_header(
            (FIXTURES / "sample_4color.gcode").read_text(encoding="utf-8"))

    def test_per_extruder_lists_keep_their_index(self, header):
        """One line per metric, comma-separated per extruder index - so
        [g][2] is T2. Trailing zeros must be kept, or tool indices shift."""
        assert header.per_tool_mm == [709.25, 298.43, 933.65, 941.90]
        assert header.per_tool_g[2] == 2.78

    def test_trailing_unused_extruders_stay_as_zeros(self):
        h = sc.parse_header("; filament used [mm] = 100.0, 0.00, 0.00, 5.00\n")
        assert h.per_tool_mm == [100.0, 0.0, 0.0, 5.0]
        assert h.mm_for(3) == 5.0

    def test_printing_time_is_a_duration_not_seconds(self, header):
        assert header.base_s == 6772.0
        assert header.first_layer_s == 19.0

    def test_layer_count_is_a_free_sanity_bound(self, header):
        assert header.layers == 205

    def test_the_slicer_total_is_an_independent_cross_check(self, header):
        assert header.confidence == "ok"
        assert header.total_g == pytest.approx(sum(header.per_tool_g), abs=0.1)

    def test_a_mismatched_total_degrades_rather_than_picking_one(self):
        h = sc.parse_header(
            "; filament used [g] = 10.0, 10.0\n"
            "; total filament used [g] = 90.0\n")
        assert h.confidence == "degraded"
        assert h.notes

    def test_total_line_is_not_mistaken_for_a_per_tool_line(self):
        h = sc.parse_header(
            "; filament used [g] = 1.0, 2.0\n"
            "; total filament used [g] = 3.0\n")
        assert h.per_tool_g == [1.0, 2.0]
        assert h.total_g == 3.0

    def test_missing_grams_falls_back_to_density(self):
        h = sc.parse_header(
            "; filament used [mm] = 1000.0\n"
            "; filament_type = PLA\n")
        expected = sc.mm_to_grams(1000.0, 1.75, sc.MATERIAL_DENSITY["PLA"])
        assert h.per_tool_g[0] == pytest.approx(expected)
        assert any("density" in n for n in h.notes)

    def test_an_empty_header_never_raises(self):
        h = sc.parse_header("")
        assert h.base_s is None and h.per_tool_mm == []

    def test_colon_separated_metrics_parse_too(self):
        """Same fork-inconsistency lesson as purge_destination: a metric we
        cannot read degrades the estimate silently, which is worse than a
        visibly missing one."""
        h = sc.parse_header(
            "; filament used [mm]: 100.0, 50.0\n"
            "; total layers count: 42\n"
            "; estimated printing time (normal mode): 1h 30m 0s\n")
        assert h.per_tool_mm == [100.0, 50.0]
        assert h.layers == 42
        assert h.base_s == 5400.0

    def test_grams_for_an_unknown_material_uses_the_default_density(self):
        h = sc.parse_header("; filament used [mm] = 1000.0\n")
        assert h.per_tool_g[0] == pytest.approx(
            sc.mm_to_grams(1000.0, 1.75, sc.DEFAULT_DENSITY))


class TestPurgeDestination:
    """§1.6: the one parse that changes the headline number."""

    def test_a_prime_tower_is_detected(self):
        assert sc.purge_destination(
            "; enable_prime_tower = 1\n; prime_tower_width = 60\n") == "tower"

    def test_flush_into_support_is_detected(self):
        assert sc.purge_destination(
            "; flush_into_support = 1\n; layer_height = 0.2\n") == "flush"

    def test_both_reads_as_mixed(self):
        assert sc.purge_destination(
            "; enable_prime_tower = 1\n; flush_into_support = 1\n") == "mixed"

    def test_a_disabled_tower_is_not_a_tower(self):
        assert sc.purge_destination(
            "; enable_prime_tower = 0\n; flush_into_infill = 0\n") == "bin"

    def test_no_slicer_settings_at_all_is_unknown_not_bin(self):
        """'cannot tell' and 'goes to the bin' are different answers."""
        assert sc.purge_destination("G1 X10 Y10\nM104 S200\n") == "unknown"

    def test_the_fixture_is_a_tower_plus_flush_print(self):
        text = (FIXTURES / "sample_4color.gcode").read_text(encoding="utf-8")
        assert sc.purge_destination(text) == "mixed"

    def test_a_colon_separated_tower_is_still_a_tower(self):
        """Slicer forks are not consistent about `=` vs `:` - the
        post-processor's colour parser already had to learn that. A
        separator we fail to accept reads as "no prime tower", and that
        adds a purge the slicer already paid for."""
        assert sc.purge_destination("; enable_prime_tower: 1\n") == "tower"

    def test_a_colon_separated_flush_is_still_a_flush(self):
        assert sc.purge_destination("; flush_into_support: 1\n") == "flush"

    def test_a_colon_only_header_is_not_mistaken_for_a_bin_print(self):
        """The dangerous asymmetry: guessing "bin" over-counts, guessing
        "unknown" only shows both totals."""
        assert sc.purge_destination(
            "; enable_prime_tower: 1\n; layer_height: 0.2\n") != "bin"


class TestEstimateBlock:
    def _header(self, extra=""):
        return sc.parse_header(
            "; filament used [mm] = 1000.0, 2000.0\n"
            "; filament used [g] = 3.0, 6.0\n"
            "; total filament used [g] = 9.0\n"
            "; total layers count = 100\n"
            "; estimated printing time (normal mode) = 1h 0m 0s\n"
            + extra)

    def _timeline(self, n_inline=2, n_bg=1, purge_mm=0.0):
        tl = []
        for i in range(n_inline):
            tl.append({"i": i, "t": 1, "kind": "cross_ace_inline",
                       "window_min": 5.0, "purge_mm": purge_mm})
        for i in range(n_bg):
            tl.append({"i": 100 + i, "t": 0, "kind": "cross_ace_bg",
                       "window_min": 9.0, "purge_mm": purge_mm})
        return tl

    def test_added_time_is_the_sum_of_the_modelled_swaps(self, model):
        est = sc.build_estimate(model, self._header("; enable_prime_tower = 1\n"),
                                self._timeline())
        expected = (2 * model.swap_seconds("cross_ace_inline")
                    + model.swap_seconds("cross_ace_bg"))
        assert est["added_s"] == pytest.approx(round(expected), abs=1)
        assert est["total_s"] == pytest.approx(3600 + est["added_s"], abs=1)

    def test_swaps_are_counted_by_kind(self, model):
        est = sc.build_estimate(model,
                                self._header("; enable_prime_tower = 1\n"),
                                self._timeline(n_inline=3, n_bg=2))
        assert est["inline_swaps"] == 3
        assert est["bg_swaps"] == 2

    def test_a_prime_tower_purge_is_reported_but_not_added(self, model):
        """Its seconds are already in base_s and its grams already in [g] -
        adding them would double-count the biggest term in the estimate."""
        header = self._header("; enable_prime_tower = 1\n")
        with_purge = sc.build_estimate(model, header,
                                       self._timeline(purge_mm=120))
        without = sc.build_estimate(model, header, self._timeline(purge_mm=0))
        assert with_purge["added_s"] == without["added_s"]
        assert with_purge["purge"]["mm"] == 360
        assert with_purge["purge"]["counted_in_total"] is False
        assert any("prime tower" in a for a in with_purge["assumptions"])

    def test_a_bin_purge_really_is_added(self, model):
        header = self._header("; enable_prime_tower = 0\n")
        est = sc.build_estimate(model, header, self._timeline(purge_mm=120))
        plain = sc.build_estimate(model, header, self._timeline(purge_mm=0))
        assert est["added_s"] > plain["added_s"]
        assert est["purge"]["counted_in_total"] is True

    def test_an_unknown_destination_shows_both_totals(self, model):
        est = sc.build_estimate(model, sc.parse_header(
            "; filament used [mm] = 1000.0\n"
            "; estimated printing time = 1h 0m 0s\n"),
            self._timeline(purge_mm=120))
        assert est["purge"]["destination"] == "unknown"
        assert est["total_s_without_purge"] < est["total_s"]

    def test_per_colour_breakdown_carries_print_and_purge(self, model):
        est = sc.build_estimate(
            model, self._header("; enable_prime_tower = 0\n"),
            self._timeline(purge_mm=100), used_tools=[0, 1])
        by_t = {row["t"]: row for row in est["per_color"]}
        assert by_t[0]["print_mm"] == 1000.0
        assert by_t[1]["print_mm"] == 2000.0
        assert by_t[1]["purge_mm"] == 200      # both inline swaps target T1
        assert by_t[0]["purge_mm"] == 100      # the bg swap targets T0

    def test_the_estimate_is_never_presented_as_measured(self, model):
        est = sc.build_estimate(model, self._header(), self._timeline())
        assert est["confidence"] == "modelled"
        assert est["assumptions"]

    def test_a_degraded_header_degrades_the_estimate(self, model):
        header = sc.parse_header(
            "; filament used [g] = 1.0, 1.0\n"
            "; total filament used [g] = 50.0\n"
            "; estimated printing time = 1h\n")
        est = sc.build_estimate(model, header, self._timeline())
        assert est["confidence"] == "degraded"

    def test_no_slicer_time_leaves_the_total_unknown_not_zero(self, model):
        header = sc.parse_header("; filament used [mm] = 100.0\n")
        est = sc.build_estimate(model, header, self._timeline())
        assert est["base_s"] is None
        assert est["total_s"] is None
        assert est["added_s"] > 0

    def test_an_empty_timeline_adds_nothing(self, model):
        est = sc.build_estimate(model, self._header(), [])
        assert est["added_s"] == 0
        assert est["total_s"] == 3600

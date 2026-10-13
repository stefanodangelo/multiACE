"""Cost-model injection, the per-event timeline, and colour-aware purge
(plan §3 / §3.1 / §3.2 / §3.4).

The point of injecting a cost model into the optimizers is that plans stop
being ranked by swap COUNT and start being ranked by modelled SECONDS - a
plan with more swaps, all of them backgrounded, can be the faster one. The
guarantee that has to survive that change is the compatibility one:
`cost_model=None` must reproduce today's output exactly, because a printer
running an older installed post-processor still has to work.
"""
import pytest

from multiace.tools import post_process_virtual_toolheads as pp
from multiace.tools import swap_cost as sc

CFG = {
    "feed_speed": 80, "retract_speed": 80,
    "load_length": 2100, "retract_length": 1950,
    "swap_retract_length": 900, "seat_overshoot_length": 20,
    "swap_anti_ooze_retract": 10, "swap_purge_length": 0,
}


@pytest.fixture
def model():
    return sc.SwapCostModel.from_params(CFG)


def ace(head, a, slot):
    return {"kind": "ace", "head": head, "ace": a, "slot": slot}


class TestBackwardsCompatibility:
    """cost_model=None has to reproduce the constants byte-for-byte, or an
    installed post-processor that predates this change silently starts
    choosing different plans."""

    def test_no_model_uses_the_historical_inline_constant(self):
        assert pp._model_cost(None, "cross_ace_inline") \
            == pp.BG_SWAP_COST_INLINE_S

    def test_no_model_uses_the_historical_bg_constant(self):
        assert pp._model_cost(None, "cross_ace_bg") == pp.BG_SWAP_COST_BG_S

    def test_no_model_uses_the_historical_window(self):
        assert pp._model_bg_window(None) == pp.BG_UNLOAD_MIN_WINDOW_MIN

    def test_no_model_means_no_purge(self):
        assert pp._model_purge_mm(
            None, "cross_ace_inline", "#000000", "#ffffff", "PLA") == 0.0

    def test_a_model_that_raises_falls_back_to_the_constants(self):
        class Broken:
            def swap_seconds(self, *a, **kw):
                raise RuntimeError("nope")

            def bg_window_minutes(self, *a, **kw):
                raise RuntimeError("nope")

        assert pp._model_cost(Broken(), "cross_ace_inline") \
            == pp.BG_SWAP_COST_INLINE_S
        assert pp._model_bg_window(Broken()) == pp.BG_UNLOAD_MIN_WINDOW_MIN

    def test_the_model_replaces_the_constant_when_supplied(self, model):
        got = pp._model_cost(model, "cross_ace_inline")
        assert got == pytest.approx(model.swap_seconds("cross_ace_inline"))

    def test_the_window_is_derived_when_a_model_is_supplied(self, model):
        assert pp._model_bg_window(model) > pp.BG_UNLOAD_MIN_WINDOW_MIN

    def test_an_older_model_without_purge_mm_for_still_purges(self):
        """A post-processor newer than the installed swap_cost.py (no
        resolve_swap_purge_mm/purge_mm_for yet) must fall back to
        purge_mm, not lose purge."""
        class OldModel:
            def purge_mm(self, *a, **kw):
                return 42.0
        got = pp._model_purge_mm(
            OldModel(), "cross_ace_inline", "#000000", "#ffffff", None,
            from_t=0, to_t=1, flush_matrix=[[0, 1]])
        assert got == 42.0


class TestTimeline:
    """§3.2's per-event trace: the thing the plan editor draws and the
    estimate sums."""

    def test_a_first_load_is_not_a_swap(self, model):
        tl = pp.build_swap_timeline([0, 1], {0: ace(0, 0, 0), 1: ace(1, 0, 1)},
                                    cost_model=model)
        assert [e["kind"] for e in tl] == ["first_load", "first_load"]

    def test_returning_to_a_head_that_already_holds_it_costs_nothing(
            self, model):
        """T0 -> T1 -> T0 on two different heads: the third event is a tool
        pickup, not a filament change, so it must not appear as a swap."""
        tl = pp.build_swap_timeline([0, 1, 0],
                                    {0: ace(0, 0, 0), 1: ace(1, 0, 1)},
                                    cost_model=model)
        assert len(tl) == 2

    def test_same_ace_is_classified_separately_from_cross_ace(self, model):
        tl = pp.build_swap_timeline(
            [0, 1], {0: ace(0, 0, 0), 1: ace(0, 0, 1)}, cost_model=model)
        assert tl[-1]["kind"] == "same_ace"

    def test_a_pinned_feeder_is_the_cheapest_event(self, model):
        tl = pp.build_swap_timeline(
            [0, 1], {0: {"kind": "pin", "head": 0}, 1: ace(1, 0, 1)},
            cost_model=model)
        assert tl[0]["kind"] == "feeder_pin"
        assert tl[0]["seconds"] < tl[1]["seconds"]

    def test_returning_to_a_pinned_feeder_that_already_holds_it_costs_nothing(
            self, model):
        """T0 -> T1 -> T0 with T0 pinned to its own feeder: the slicer just
        revisits the same tool around another one, so the third event is a
        tool pickup the slicer's own toolchange time already covers, not a
        second billable feeder_pin - same contract as the ACE case above."""
        tl = pp.build_swap_timeline(
            [0, 1, 0],
            {0: {"kind": "pin", "head": 0}, 1: ace(1, 0, 1)},
            cost_model=model)
        assert len(tl) == 2

    def test_a_wide_window_on_a_bg_head_becomes_a_background_swap(self, model):
        """The blue-from-head-2 / green-preloaded-in-head-1 case: head 0 is
        free for 30 minutes before it is needed again."""
        events = [0, 1, 2]
        times = [60.0, 50.0, 20.0]      # remaining minutes at each event
        tl = pp.build_swap_timeline(
            events, {0: ace(0, 0, 0), 1: ace(1, 1, 0), 2: ace(0, 0, 1)},
            event_times=times, bg_heads=[0, 1], cost_model=model)
        assert tl[-1]["kind"] == "cross_ace_bg"
        assert tl[-1]["window_min"] == pytest.approx(30.0)

    def test_a_narrow_window_degrades_to_inline(self, model):
        events = [0, 1, 2]
        times = [60.0, 59.9, 59.8]      # 6 seconds of idle time
        tl = pp.build_swap_timeline(
            events, {0: ace(0, 0, 0), 1: ace(1, 1, 0), 2: ace(0, 0, 1)},
            event_times=times, bg_heads=[0, 1], cost_model=model)
        assert tl[-1]["kind"] == "same_ace"
        assert "inline" in tl[-1]["note"] or "same ACE" in tl[-1]["note"]

    def test_a_head_that_is_not_bg_enabled_never_backgrounds(self, model):
        events = [0, 1, 2]
        times = [60.0, 50.0, 20.0]
        tl = pp.build_swap_timeline(
            events, {0: ace(0, 0, 0), 1: ace(1, 1, 0), 2: ace(0, 0, 1)},
            event_times=times, bg_heads=[], cost_model=model)
        assert tl[-1]["kind"] != "cross_ace_bg"

    def test_no_m73_times_leaves_the_window_unknown(self, model):
        tl = pp.build_swap_timeline(
            [0, 1, 2], {0: ace(0, 0, 0), 1: ace(1, 1, 0), 2: ace(0, 0, 1)},
            bg_heads=[0, 1], cost_model=model)
        assert tl[-1]["window_min"] is None

    def test_unassigned_colours_are_skipped_not_guessed(self, model):
        tl = pp.build_swap_timeline([0, 1], {0: ace(0, 0, 0)},
                                    cost_model=model)
        assert [e["t"] for e in tl] == [0]

    def test_an_empty_event_list_is_an_empty_timeline(self, model):
        assert pp.build_swap_timeline([], {}, cost_model=model) == []

    def test_the_timeline_carries_the_purge_the_rewrite_would_emit(self):
        m = sc.SwapCostModel.from_params(
            dict(CFG, purge_color_aware=True, swap_purge_min=20,
                 swap_purge_max=150))
        tl = pp.build_swap_timeline(
            [0, 1, 0], {0: ace(0, 0, 0), 1: ace(0, 0, 1)},
            cost_model=m, colors={0: "#000000", 1: "#ffffff"})
        swap = [e for e in tl if e["kind"] == "same_ace"][-1]
        assert swap["purge_mm"] > 20

    def test_a_flush_matrix_supplies_the_real_purge_number(self, model):
        """The timeline's purge_mm has to be exactly the stamp the rewrite
        will write for this pair (_matrix_purge_mm: 0.45x top-up, clamped
        40-150 mm) - not the slicer's raw, un-topped-up matrix cell, which
        is more than the engine actually purges for this transition."""
        matrix = [[0, 150], [90, 0]]
        tl = pp.build_swap_timeline(
            [0, 1], {0: ace(0, 0, 0), 1: ace(0, 0, 1)},
            cost_model=model, flush_matrix=matrix)
        swap = [e for e in tl if e["kind"] == "same_ace"][-1]
        assert swap["purge_mm"] == pytest.approx(
            pp._matrix_purge_mm(matrix, 0, 1), abs=0.1)

    def test_no_flush_matrix_falls_back_to_the_stock_default(self, model):
        """swap_purge_length=0 and purge_color_aware off (CFG's defaults)
        used to resolve to a reported 0 mm, but the engine's own
        INNER_FLUSH_FILAMENT macro purges STOCK_FLUSH_DEFAULT_MM (80 mm)
        whenever no LENGTH= stamp is written - the timeline has to match
        what actually comes out of the nozzle, not a config guess."""
        tl = pp.build_swap_timeline(
            [0, 1], {0: ace(0, 0, 0), 1: ace(0, 0, 1)}, cost_model=model)
        swap = [e for e in tl if e["kind"] == "same_ace"][-1]
        assert swap["purge_mm"] == sc.STOCK_FLUSH_DEFAULT_MM


class TestPlacementBeatsSwapCount:
    """§3.1's lever: where a colour sits decides what a change to it costs,
    so the cheap placement has to win even when it swaps just as often."""

    def test_backgroundable_placement_costs_less_than_same_ace(self, model):
        events = [0, 1, 2]
        times = [60.0, 50.0, 20.0]
        bg = pp.build_swap_timeline(
            events, {0: ace(0, 0, 0), 1: ace(1, 1, 0), 2: ace(0, 0, 1)},
            event_times=times, bg_heads=[0, 1], cost_model=model)
        same = pp.build_swap_timeline(
            events, {0: ace(0, 0, 0), 1: ace(0, 0, 1), 2: ace(0, 0, 2)},
            event_times=times, bg_heads=[0, 1], cost_model=model)
        assert sum(e["seconds"] for e in bg) < sum(e["seconds"] for e in same)

    def test_bg_stats_saving_follows_the_model_not_the_constant(self, model):
        events = [0, 1, 2]
        times = [60.0, 50.0, 20.0]
        asn = {0: ace(0, 0, 0), 1: ace(1, 1, 0), 2: ace(0, 0, 1)}
        with_model = pp.head_mode_bg_stats(events, asn, event_times=times,
                                           bg_heads=[0, 1], cost_model=model)
        without = pp.head_mode_bg_stats(events, asn, event_times=times,
                                        bg_heads=[0, 1])
        assert without["saved_s"] == without["bg_ok"] * \
            pp.BG_UNLOAD_INLINE_SAVING_S
        assert with_model["saved_s"] == pytest.approx(
            with_model["bg_ok"] * (model.swap_seconds("cross_ace_inline")
                                   - model.swap_seconds("cross_ace_bg")))


class TestPurgeEmission:
    """PURGE= round-trips through the rewrite and back through the parser."""

    def test_the_plan_line_regex_tolerates_the_extra_key(self):
        import re
        line = "ACE_SWAP_HEAD HEAD=1 ACE=0 SLOT=2 ANTI_OOZE=1.0 PURGE=140"
        m = re.match(
            r'^ACE_SWAP_HEAD HEAD=(\d+) ACE=(\d+) SLOT=(\d+)'
            r'(?:\s+\S+=\S+)*\s*$', line)
        assert m and m.groups() == ("1", "0", "2")

    def test_no_callback_emits_no_purge(self, tmp_path):
        out = _rewrite(tmp_path, purge_mm_for=None)
        assert "PURGE=" not in out

    def test_a_callback_emits_the_purge_it_returns(self, tmp_path):
        out = _rewrite(tmp_path, purge_mm_for=lambda *a: 140)
        assert "PURGE=140" in out

    def test_a_zero_purge_emits_nothing(self, tmp_path):
        """An explicit 0 must not add a no-op key to every swap line."""
        out = _rewrite(tmp_path, purge_mm_for=lambda *a: 0)
        assert "PURGE=" not in out

    def test_a_raising_callback_never_breaks_the_rewrite(self, tmp_path):
        def boom(*a):
            raise RuntimeError("no purge for you")
        out = _rewrite(tmp_path, purge_mm_for=boom)
        assert "ACE_SWAP_HEAD" in out and "PURGE=" not in out

    def test_the_callback_sees_the_outgoing_colour(self, tmp_path):
        seen = []

        def spy(head, a, slot, from_t, to_t):
            seen.append((from_t, to_t))
            return 0

        _rewrite(tmp_path, purge_mm_for=spy)
        # First swap on a head has no outgoing colour; later ones do.
        assert seen[0][0] is None
        assert any(f is not None for f, _t in seen)

    def test_the_callback_receives_the_matrix_stamp(self, tmp_path):
        """An inline swap used to get no per-pair stamp at all - the
        rewrite now computes the same _matrix_purge_mm top-up a
        background swap already gets and hands it to the callback, which
        can prefer it over its own guess (see swap_cost.
        resolve_swap_purge_mm's precedence)."""
        seen = []

        def spy(head, a, slot, from_t, to_t, stamp_mm=None):
            seen.append(stamp_mm)
            return stamp_mm

        _rewrite(tmp_path, purge_mm_for=spy,
                flush_matrix=[[0, 150], [150, 0]])
        # The first swap on a head has no outgoing colour, so no stamp;
        # the later one is T1->T0, inside the matrix, so it gets one.
        assert any(s is not None for s in seen)

    def test_an_older_callback_without_stamp_mm_still_works(self, tmp_path):
        """A purge_mm_for from an older preflight_core (no stamp_mm kwarg
        yet) must not lose its purge to a TypeError - version-skew
        tolerance, same contract as every other *_to_file kwarg here."""
        out = _rewrite(tmp_path, purge_mm_for=lambda *a: 77,
                       flush_matrix=[[0, 150], [150, 0]])
        assert "PURGE=77" in out

    def test_no_callback_still_stamps_from_the_matrix(self, tmp_path):
        """With purge_color_aware off (purge_mm_for is None in that case),
        an inline swap still gets the slicer's own per-pair number
        instead of silently falling through to the flat engine default."""
        out = _rewrite(tmp_path, purge_mm_for=None,
                       flush_matrix=[[0, 150], [150, 0]])
        assert "PURGE=" in out


def _rewrite(tmp_path, purge_mm_for, flush_matrix=None):
    """Run the head-mode rewrite over a tiny two-colour file.

    `flush_matrix`, when given, is written into the header as the
    slicer's own `flush_volumes_matrix` line - rewrite_head_mode_to_file
    parses that straight out of the file, same as the real pipeline."""
    header = ("; filament_colour = #000000;#ffffff\n"
             "; filament_type = PLA;PLA\n")
    if flush_matrix is not None:
        flat = ",".join(str(v) for row in flush_matrix for v in row)
        header += "; flush_volumes_matrix = %s\n" % flat
    src = tmp_path / "in.gcode"
    src.write_text(
        header +
        "; EXECUTABLE_BLOCK_START\n"
        "T0\n"
        "G1 X1 Y1 E1\n"
        "; Change Tool 0 -> Tool 1\n"
        "T1\n"
        "G1 X2 Y2 E1\n"
        "; Change Tool 1 -> Tool 0\n"
        "T0\n"
        "G1 X3 Y3 E1\n",
        encoding="utf-8")
    out = tmp_path / "out.gcode"
    pp.rewrite_head_mode_to_file(
        str(src), str(out),
        {0: ace(0, 0, 0), 1: ace(0, 0, 1)},
        None, None, purge_mm_for=purge_mm_for)
    return out.read_text(encoding="utf-8")


class TestBackgroundWindowPlanning:
    """A background swap is only emitted when the parked window can really
    hold it. The rewrite used to compare against a hard-coded 1 minute while
    the cost model (and so the estimate) wanted ~3.5, which sent swaps into
    the print's own toolchange - overnight 2026-10-12 that race ended in a
    hung print."""

    def _run(self, tmp_path, window_min, bg_window_for):
        # T1 prints on head 1, T2 on head 0 for `window_min` minutes (M73 R
        # counts down), then T3 needs head 1 again with a different slot -
        # head 1 is parked for exactly that window.
        src = tmp_path / "in.gcode"
        src.write_text(
            "; filament_colour = #000000;#ffffff;#ff0000;#00ff00\n"
            "; filament_type = PLA;PLA;PLA;PLA\n"
            "; EXECUTABLE_BLOCK_START\n"
            "M73 P1 R100\n"
            "T0\n"
            "G1 X1 Y1 E1\n"
            "; Change Tool 0 -> Tool 1\n"
            "M73 P2 R100\n"
            "T1\n"
            "G1 X2 Y2 E1\n"
            "; Change Tool 1 -> Tool 2\n"
            "M73 P3 R100\n"
            "T2\n"
            "G1 X3 Y3 E1\n"
            "; Change Tool 2 -> Tool 3\n"
            "M73 P4 R%d\n"
            "T3\n"
            "G1 X4 Y4 E1\n" % (100 - window_min),
            encoding="utf-8")
        out = tmp_path / "out.gcode"
        pp.rewrite_head_mode_to_file(
            str(src), str(out),
            {0: ace(0, 0, 0), 1: ace(1, 0, 1), 2: ace(0, 0, 2),
             3: ace(1, 0, 3)},
            None, None, bg_window_for=bg_window_for)
        return out.read_text(encoding="utf-8")

    def test_a_window_the_model_says_is_too_short_is_skipped(self, tmp_path):
        text = self._run(tmp_path, 2, lambda a, s: 3.5)
        assert "ACE_BG_SWAP" not in text
        assert "bg-swap HEAD=1 skipped" in text

    def test_the_whole_minute_rounding_is_judged_conservatively(
            self, tmp_path):
        """M73 R is whole minutes: a 4 min window may really be 3.01."""
        text = self._run(tmp_path, 4, lambda a, s: 3.5)
        assert "ACE_BG_SWAP" not in text

    def test_a_window_that_fits_is_still_backgrounded(self, tmp_path):
        text = self._run(tmp_path, 6, lambda a, s: 3.5)
        assert "ACE_BG_SWAP HEAD=1 ACE=0 SLOT=3" in text

    def test_without_a_model_the_historic_one_minute_rule_holds(
            self, tmp_path):
        assert "ACE_BG_SWAP" in self._run(tmp_path, 2, None)
        assert "ACE_BG_SWAP" not in self._run(tmp_path, 0, None)

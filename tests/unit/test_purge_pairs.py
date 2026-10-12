"""The purge-pair table and the recompute-everything path behind the
preflight's purge volume slider and override grid (§1.6).

The grid has two promises worth pinning: it only offers changes that can
really happen WITHIN the ACE (a stock-feeder colour never swaps, so it has
no row), and moving a purge setting re-runs every plan with the user's own
mapping, not the one the analysis started from.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "multiace" / "web" / "backend"))

import preflight_core as pc  # noqa: E402
from multiace.tools import post_process_virtual_toolheads as pp  # noqa: E402

COLORS = {0: "#0047ab", 1: "#d94040", 2: "#f2c200", 3: "#ffffff"}
TYPES = {t: "PLA" for t in COLORS}
CFG = {"main": {"feed_speed": 80, "retract_speed": 80, "load_length": 2100,
                "retract_length": 1950, "swap_retract_length": 900,
                "seat_overshoot_length": 20, "swap_anti_ooze_retract": 10,
                "swap_purge_length": 0}}
HEADER = ("; filament used [mm] = 1000.0, 1000.0, 1000.0, 1000.0\n"
          "; filament used [g] = 3.0, 3.0, 3.0, 3.0\n"
          "; estimated printing time (normal mode) = 1h 0m 0s\n"
          "; enable_prime_tower = 0\n")


def ace(head, a, slot):
    return {"kind": "ace", "head": head, "ace": a, "slot": slot}


#: T0-T2 share head 3 inside the ACE; T3 (white) lives on its own stock
#: feeder and never swaps.
ASSIGNMENT = {0: ace(3, 0, 0), 1: ace(3, 0, 1), 2: ace(3, 0, 2),
              3: {"kind": "pin", "head": 0}}
EVENTS = [0, 1, 2, 3, 0, 1]


def make_ctx(cost=CFG, matrix=None):
    return pc.make_estimate_ctx(
        pp, HEADER, cost_params=cost, slicer_colors=COLORS,
        slicer_types=TYPES, stamp_matrix=matrix)


def table(ctx):
    return pc.purge_pair_table(ctx, EVENTS, ASSIGNMENT)


class TestPurgePairTable:
    def test_only_changes_within_the_ace_are_offered(self):
        groups = table(make_ctx())["groups"]
        assert len(groups) == 1
        hexes = {c["hex"] for c in groups[0]["colors"]}
        assert hexes == {"0047ab", "d94040", "f2c200"}   # no white feeder

    def test_every_ordered_pair_appears_once(self):
        g = table(make_ctx())["groups"][0]
        assert len(g["pairs"]) == 6
        assert len({(p["from"], p["to"]) for p in g["pairs"]}) == 6

    def test_a_lone_colour_has_nothing_to_set(self):
        assign = {0: ace(3, 0, 0), 3: {"kind": "pin", "head": 0}}
        assert pc.purge_pair_table(make_ctx(), [0, 3, 0], assign)[
            "groups"] == []

    def test_defaults_come_from_the_stock_purge_and_the_scale(self):
        cfg = {"main": dict(CFG["main"], purge_scale_pct=50)}
        for p in table(make_ctx(cfg))["groups"][0]["pairs"]:
            assert p["default_mm"] == pytest.approx(40.0)
            assert p["mm"] == pytest.approx(40.0)
            assert p["override_mm"] is None

    def test_an_override_shows_exactly_and_keeps_the_default(self):
        cfg = {"main": dict(CFG["main"], purge_scale_pct=50,
                            purge_pair_mm={"0047ab>d94040": 60})}
        pair = next(p for p in table(make_ctx(cfg))["groups"][0]["pairs"]
                    if (p["from"], p["to"]) == ("0047ab", "d94040"))
        assert pair["mm"] == 60            # not scaled
        assert pair["default_mm"] == pytest.approx(40.0)
        assert pair["override_mm"] == 60

    def test_the_slicer_matrix_supplies_the_default(self):
        # 4x4 matrix, 300 mm3 for T0->T1 => 0.45 x 124.7 mm = 56 mm stamp
        m = [[0, 300, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]]
        pair = next(p for p in table(make_ctx(matrix=m))["groups"][0]["pairs"]
                    if (p["from_t"], p["to_t"]) == (0, 1))
        assert pair["default_mm"] == pytest.approx(
            pp._matrix_purge_mm(m, 0, 1), abs=0.1)

    def test_per_pair_purge_off_ignores_the_matrix_and_overrides(self):
        cfg = {"main": dict(CFG["main"], purge_matrix=False,
                            purge_pair_mm={"0047ab>d94040": 60})}
        m = [[0, 300, 0, 0]] + [[0] * 4] * 3
        t = table(make_ctx(cfg, m))
        assert t["matrix"] is False
        assert all(p["mm"] == 80.0 and p["override_mm"] is None
                   for p in t["groups"][0]["pairs"])


class TestRecomputeEverything:
    def _capture(self, ctx):
        plans = {
            "slicer": {"feasible": True, "mapping": []},
            "other": {"feasible": True, "mapping": []},
        }
        pc.attach_estimate(ctx, plans["slicer"], EVENTS, ASSIGNMENT)
        pc.attach_estimate(ctx, plans["other"], EVENTS[:3], ASSIGNMENT)
        return {"ctx": ctx, "events": EVENTS, "plans": plans}

    def test_a_lower_scale_lowers_every_plans_purge(self):
        cap = self._capture(make_ctx())
        before = {n: p["estimate"]["purge"]["g"]
                  for n, p in cap["plans"].items()}
        res = pc.recompute_all_estimates(cap, {"main": dict(
            CFG["main"], purge_scale_pct=50)})
        assert set(res) == {"slicer", "other"}
        for name, r in res.items():
            assert r["estimate"]["purge"]["g"] < before[name]
        # ...and the live plan dicts were updated in place too
        assert cap["plans"]["slicer"]["estimate"]["purge"]["g"] == \
            res["slicer"]["estimate"]["purge"]["g"]

    def test_a_pair_override_moves_the_plans_totals(self):
        """What the grid is for: pin one transition and the plan's grams
        and time follow, with no re-analysis."""
        cap = self._capture(make_ctx())
        base = cap["plans"]["slicer"]["estimate"]
        res = pc.recompute_all_estimates(cap, {"main": dict(
            CFG["main"], purge_pair_mm={"0047ab>d94040": 150,
                                        "d94040>f2c200": 150})})
        got = res["slicer"]["estimate"]
        assert got["purge"]["g"] > base["purge"]["g"]
        assert got["total_s"] > base["total_s"]
        assert got["totals"]["g"] > base["totals"]["g"]
        # ...and an override is not scaled by the volume slider
        res = pc.recompute_all_estimates(cap, {"main": dict(
            CFG["main"], purge_scale_pct=50,
            purge_pair_mm={"0047ab>d94040": 150})})
        pair = next(p for p in res["slicer"]["purge_pairs"]["groups"][0][
            "pairs"] if (p["from"], p["to"]) == ("0047ab", "d94040"))
        assert pair["mm"] == 150

    def test_the_users_edited_mapping_survives_a_purge_change(self):
        cap = self._capture(make_ctx())
        # The user re-seats T2 on another head, which removes it from the
        # swap group; a later slider move must keep THAT assignment.
        edited = {0: ace(3, 0, 0), 1: ace(3, 0, 1), 2: ace(2, 0, 2),
                  3: {"kind": "pin", "head": 0}}
        cap["targets"] = []
        orig = pc.assignment_from_target_ids
        pc.assignment_from_target_ids = lambda ids, targets: edited
        try:
            pc.recompute_estimate(cap, target_ids=["x"], plan="slicer")
        finally:
            pc.assignment_from_target_ids = orig
        res = pc.recompute_all_estimates(cap, {"main": dict(
            CFG["main"], purge_scale_pct=70)})
        hexes = {c["hex"] for g in res["slicer"]["purge_pairs"]["groups"]
                 for c in g["colors"]}
        assert hexes == {"0047ab", "d94040"}

    def test_later_edits_use_the_new_cost_params(self):
        cap = self._capture(make_ctx())
        pc.recompute_all_estimates(cap, {"main": dict(
            CFG["main"], purge_scale_pct=50)})
        res = pc.recompute_estimate(
            cap, mapping=[], plan=None)
        assert res["purge_pairs"]["scale_pct"] == 50

    def test_nothing_captured_is_none(self):
        assert pc.recompute_all_estimates({}, CFG) is None


class TestStampMatrixBasis:
    def test_a_sub_one_flush_multiplier_is_not_applied_downward(self):
        """The rewrite sizes stamps with the multiplier applied UPWARD only;
        the estimate has to read the same matrix or it understates purge."""
        text = ("; flush_volumes_matrix = 0,300,300,0\n"
                "; flush_multiplier = 0.5\n")
        raw = pp.parse_flush_matrix_raw(text)
        public = pp.parse_flush_matrix(text)
        assert raw[0][1] == 300
        assert public[0][1] == 150

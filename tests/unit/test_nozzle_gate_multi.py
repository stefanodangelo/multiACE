"""Mixed-nozzle gate for multi-mode (plan step 4, upstream v1.00.1b H4).

compute_swap_aware_layout gained an `allowed_heads` parameter so a filament
sliced for one nozzle diameter cannot be optimized onto a head carrying a
different one. Two things are worth pinning down:

  * BACKWARDS COMPATIBILITY - allowed_heads=None (or omitted) must search
    exactly as before, since every installed post-processor call site that
    predates this change never passes it.
  * The CONSTRAINT itself actually restricts the search, including the
    infeasible case (no assignment satisfies the allowed sets).

file_body_detectable's job is orthogonal: refuse a multi-colour file that
carries neither a tool-change marker nor a layer-change marker, so the
rewrite pipeline errors loudly instead of silently emitting a single-colour
print.
"""
import pytest

from multiace.tools import post_process_virtual_toolheads as pp


class TestAllowedHeadsBackwardsCompatibility:
    def test_none_reproduces_unconstrained_search(self):
        events = [0, 1, 0, 1, 2, 0]
        c2h_a, swaps_a = pp.compute_swap_aware_layout(events, num_aces=4)
        c2h_b, swaps_b = pp.compute_swap_aware_layout(
            events, num_aces=4, allowed_heads=None)
        assert swaps_a == swaps_b
        # Same swap count is the property that matters; ties in the search
        # order are allowed to land on any equally-good assignment.
        assert swaps_a is not None

    def test_empty_dict_is_also_unconstrained(self):
        events = [0, 1, 2, 1, 0]
        c2h, swaps = pp.compute_swap_aware_layout(
            events, num_aces=4, allowed_heads={})
        assert c2h is not None
        c2h_none, swaps_none = pp.compute_swap_aware_layout(
            events, num_aces=4)
        assert swaps == swaps_none


class TestAllowedHeadsConstraint:
    def test_pinned_colour_is_forced_onto_its_allowed_head(self):
        """Colour 0 sliced at a nozzle only head 2 carries - even though an
        unconstrained search would put it elsewhere, the result must honour
        the gate."""
        events = [0, 1, 0, 1]
        c2h, swaps = pp.compute_swap_aware_layout(
            events, num_aces=4, allowed_heads={0: {2}})
        assert c2h is not None
        assert c2h[0] == 2

    def test_disjoint_allowed_sets_still_feasible(self):
        events = [0, 1, 2]
        c2h, swaps = pp.compute_swap_aware_layout(
            events, num_aces=4,
            allowed_heads={0: {0}, 1: {1}, 2: {2, 3}})
        assert c2h is not None
        assert c2h[0] == 0
        assert c2h[1] == 1
        assert c2h[2] in (2, 3)

    def test_infeasible_constraint_returns_none(self):
        """A colour whose allowed set is empty (its sliced nozzle diameter
        matches no head) can never be placed - the search must report
        infeasible rather than violate the gate."""
        events = [0, 1]
        c2h, swaps = pp.compute_swap_aware_layout(
            events, num_aces=4, allowed_heads={0: set()})
        assert c2h is None
        assert swaps is None

    def test_missing_colour_in_allowed_heads_is_unconstrained(self):
        """allowed_heads only restricts the colours it names; a colour
        absent from the dict is free to land on any head."""
        events = [0, 1]
        c2h, swaps = pp.compute_swap_aware_layout(
            events, num_aces=4, allowed_heads={0: {1}})
        assert c2h is not None
        assert c2h[0] == 1


class TestFileBodyDetectable:
    def _write(self, tmp_path, lines):
        p = tmp_path / "job.gcode"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(p)

    def test_tool_change_marker_is_detectable(self, tmp_path):
        path = self._write(tmp_path, [
            "; header", "; Change Tool 0 -> Tool 1", "T1", "G1 X1"])
        assert pp.file_body_detectable(path) is True

    def test_layer_change_marker_is_detectable(self, tmp_path):
        path = self._write(tmp_path, [
            "; header", "; LAYER_CHANGE", "G1 X1"])
        assert pp.file_body_detectable(path) is True

    def test_neither_marker_is_not_detectable(self, tmp_path):
        path = self._write(tmp_path, [
            "; header", "G1 X1", "G1 Y1"])
        assert pp.file_body_detectable(path) is False

    def test_body_start_prefers_tool_change_when_present(self, tmp_path):
        path = self._write(tmp_path, [
            "; LAYER_CHANGE", "; Change Tool 0 -> Tool 1", "T1"])
        body_start = pp._body_start_re(path)
        assert body_start is pp._TC_MATCH_RE

    def test_body_start_falls_back_to_layer_change(self, tmp_path):
        path = self._write(tmp_path, ["; LAYER_CHANGE", "T1"])
        body_start = pp._body_start_re(path)
        assert body_start is pp._LAYER_BOUNDARY_RE

"""Hybrid per-head mode: a combo head is ACE-driven AND has its stock
feeder spliced onto the same path via a Y-splitter, so it can swap between
its ACE slots and the feeder spool mid-print (an extra slot='feeder' entry
in the head-mode assignment, distinct from a plain pinned feeder head).

Covers the planner/post-processor side (pure Python, no Klipper needed):
compute_head_mode_layout's combo_heads matching, head_mode_swap_count's
generic (ace, slot) keying, rewrite_head_mode_to_file's SOURCE=FEEDER
emission, and the bg-eligibility exclusion in head_mode_bg_stats /
build_swap_timeline. Also the swap_cost.py 'feeder_swap' kind.
"""
import os
import tempfile

from multiace.tools import post_process_virtual_toolheads as pp
from multiace.tools import swap_cost


def test_combo_head_feeder_tap_matches_its_own_fixed_colour():
    """A slicer colour matching a combo head's loaded feeder-tap identity
    is assigned kind='ace', ace=None, slot='feeder' on that head - not a
    plain 'pin' (it must remain swappable) and not consuming an ACE slot."""
    slicer_colors = {0: 'ff0000', 1: '00ff00'}
    slicer_types = {0: 'PLA', 1: 'PLA'}
    combo_heads = [{'head': 1, 'material': 'PLA', 'color': 'ff0000'}]
    ace_slots = [{'ace': 0, 'slot': s, 'material': 'PLA', 'color': c}
                 for s, c in enumerate(['00ff00', '0000ff', 'ffff00', 'ff00ff'])]
    ace_head_of_ace = {0: 0}

    result = pp.compute_head_mode_layout(
        slicer_colors, slicer_types, pinned_heads=[],
        ace_slots=ace_slots, ace_head_of_ace=ace_head_of_ace,
        combo_heads=combo_heads)

    assert result['feasible'], result
    e0 = result['assignment'][0]
    assert e0['kind'] == 'ace'
    assert e0['head'] == 1
    assert e0['ace'] is None
    assert e0['slot'] == 'feeder'

    e1 = result['assignment'][1]
    assert e1['kind'] == 'ace'
    assert e1['ace'] == 0
    assert e1['slot'] == 0


def test_combo_head_feeder_tap_never_offered_a_different_colour():
    """The feeder tap's identity is fixed input (whatever is physically on
    the stock holder) - a colour that does NOT match it must not be routed
    there even when the tap would otherwise be a free 'slot'."""
    slicer_colors = {0: 'aaaaaa'}
    slicer_types = {0: 'PLA'}
    combo_heads = [{'head': 1, 'material': 'PLA', 'color': 'ff0000'}]
    ace_slots = [{'ace': 0, 'slot': 0, 'material': 'PLA', 'color': 'aaaaaa'}]
    ace_head_of_ace = {0: 0}

    result = pp.compute_head_mode_layout(
        slicer_colors, slicer_types, pinned_heads=[],
        ace_slots=ace_slots, ace_head_of_ace=ace_head_of_ace,
        combo_heads=combo_heads)

    e0 = result['assignment'][0]
    assert e0['slot'] != 'feeder', (
        "an unrelated colour must never be routed onto the fixed-identity "
        "feeder tap: %r" % e0)
    assert e0 == {'kind': 'ace', 'head': 0, 'ace': 0, 'slot': 0,
                 'tier': 'exact_hex'}


def test_worked_example_one_ace_one_combo_head_eight_colours():
    """The plan's own worked example: 1 ACE (4 slots) + 1 combo head with
    a Y-splitter + 3 plain feeder heads = up to 8 addressable colours."""
    pinned_heads = [
        {'head': 1, 'material': 'PLA', 'color': '111111'},
        {'head': 2, 'material': 'PLA', 'color': '222222'},
        {'head': 3, 'material': 'PLA', 'color': '333333'},
    ]
    combo_heads = [{'head': 0, 'material': 'PLA', 'color': '444444'}]
    ace_slots = [{'ace': 0, 'slot': s, 'material': 'PLA', 'color': c}
                 for s, c in enumerate(
                     ['555555', '666666', '777777', '888888'])]
    ace_head_of_ace = {0: 0}
    slicer_colors = {t: c for t, c in enumerate(
        ['111111', '222222', '333333', '444444',
         '555555', '666666', '777777', '888888'])}
    slicer_types = {t: 'PLA' for t in slicer_colors}

    result = pp.compute_head_mode_layout(
        slicer_colors, slicer_types, pinned_heads, ace_slots,
        ace_head_of_ace, combo_heads=combo_heads)

    assert result['feasible'], result
    assert len(result['assignment']) == 8


def test_head_mode_swap_count_treats_feeder_tap_as_a_slot_change():
    """A transition into/out of the feeder tap on the SAME head counts as
    one swap, exactly like any other (ace, slot) change - no special-casing
    needed in head_mode_swap_count, since it already keys off the tuple."""
    assignment = {
        0: {'kind': 'ace', 'head': 0, 'ace': None, 'slot': 'feeder'},
        1: {'kind': 'ace', 'head': 0, 'ace': 0, 'slot': 2},
        2: {'kind': 'ace', 'head': 0, 'ace': None, 'slot': 'feeder'},
    }
    swaps = pp.head_mode_swap_count([0, 1, 2], assignment)
    assert swaps == 3, "first_load + 2 real transitions -> 3 swaps, got %d" % swaps

    # No-op re-print of the same T does not add a swap.
    swaps2 = pp.head_mode_swap_count([0, 0, 1, 2], assignment)
    assert swaps2 == 3


def test_rewrite_emits_source_feeder_for_the_feeder_slot():
    """rewrite_head_mode_to_file must address a feeder-tap entry with
    ACE_SWAP_HEAD HEAD=<h> SOURCE=FEEDER, never a bare ACE=/SLOT= pair
    (which would crash on the None/'feeder' sentinel) and never a plain
    T<h> (that would look like a permanently-pinned feeder head and skip
    unloading whatever the head was previously on)."""
    gcode = (
        "; multiACE auto-load: T0\n"
        "T0\n"
        "G1 X1\n"
        "; Change Tool 0 -> Tool 1\n"
        "T1\n"
        "G1 X2\n"
    )
    assignment = {
        0: {'kind': 'ace', 'head': 0, 'ace': 0, 'slot': 0},
        1: {'kind': 'ace', 'head': 0, 'ace': None, 'slot': 'feeder'},
    }
    with tempfile.TemporaryDirectory() as d:
        in_path = os.path.join(d, 'in.gcode')
        out_path = os.path.join(d, 'out.gcode')
        with open(in_path, 'w') as f:
            f.write(gcode)
        active, skipped = pp.rewrite_head_mode_to_file(in_path, out_path, assignment)
        out = open(out_path).read()

    assert 'SOURCE=FEEDER' in out, out
    assert 'ACE=None' not in out and 'SLOT=feeder' not in out, (
        "must never format the sentinel into ACE=/SLOT=: %r" % out)
    assert active >= 1


def test_bg_stats_excludes_any_transition_touching_the_feeder_tap():
    """head_mode_bg_stats must never classify a feeder-tap transition as
    background-eligible, regardless of bg_heads/window - it is always
    foreground (§2.3 of the hybrid per-head mode plan)."""
    # head 0: feeder tap (t=0) -> ACE slot (t=1) -> another head (t=2) uses
    # a DIFFERENT head so the unload-of-head-0 window logic engages.
    assignment = {
        0: {'kind': 'ace', 'head': 0, 'ace': None, 'slot': 'feeder'},
        1: {'kind': 'ace', 'head': 1, 'ace': 1, 'slot': 0},
        2: {'kind': 'ace', 'head': 0, 'ace': 0, 'slot': 1},
    }
    events = [0, 1, 2]
    event_times = [100.0, 90.0, 50.0]
    stats = pp.head_mode_bg_stats(
        events, assignment, event_times=event_times, bg_heads=[0, 1])
    assert stats['bg_ok'] == 0, (
        "a feeder-tap transition must never be counted bg-eligible: %r" % stats)
    assert stats['bg_disabled'] >= 1


def test_build_swap_timeline_labels_feeder_transitions_consistently():
    """build_swap_timeline (the UI/estimate side) must never show
    'cross_ace_bg' for a transition into/out of the feeder tap - it has to
    match what the rewrite actually emits (always foreground)."""
    assignment = {
        0: {'kind': 'ace', 'head': 0, 'ace': None, 'slot': 'feeder'},
        1: {'kind': 'ace', 'head': 0, 'ace': 0, 'slot': 0},
    }
    timeline = pp.build_swap_timeline(
        [0, 1], assignment, event_times=[100.0, 5.0], bg_heads=[0])
    assert len(timeline) == 2
    assert timeline[1]['kind'] == 'feeder_swap'
    assert timeline[1]['kind'] != 'cross_ace_bg'


def test_swap_cost_model_has_a_feeder_swap_kind():
    """swap_seconds must accept 'feeder_swap' (it is in SWAP_KINDS) rather
    than raising, and its mechanical term must come from the feeder's own
    load/retract config, not the ACE's - a much longer feeder_load_length
    must make it more expensive, independent of the ACE's own load_length."""
    model_short = swap_cost.SwapCostModel.from_params({
        "feed_speed": 80, "retract_speed": 80,
        "load_length": 2100, "retract_length": 1950,
        "swap_retract_length": 900,
        "feeder_load_length": 500, "feeder_swap_retract_length": 50,
    })
    model_long = swap_cost.SwapCostModel.from_params({
        "feed_speed": 80, "retract_speed": 80,
        "load_length": 2100, "retract_length": 1950,
        "swap_retract_length": 900,
        "feeder_load_length": 5000, "feeder_swap_retract_length": 500,
    })
    s_short = model_short.swap_seconds("feeder_swap")
    s_long = model_long.swap_seconds("feeder_swap")
    assert s_long > s_short, (s_short, s_long)

    # Changing the ACE's own load_length must NOT move the feeder_swap cost.
    model_ace_only = swap_cost.SwapCostModel.from_params({
        "feed_speed": 80, "retract_speed": 80,
        "load_length": 20000, "retract_length": 1950,
        "swap_retract_length": 900,
        "feeder_load_length": 500, "feeder_swap_retract_length": 50,
    })
    assert model_ace_only.swap_seconds("feeder_swap") == s_short


if __name__ == '__main__':
    import sys
    failures = 0
    for name, fn in sorted(list(globals().items())):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print('PASS: %s' % name)
            except AssertionError as e:
                failures += 1
                print('FAIL: %s: %s' % (name, e))
    sys.exit(1 if failures else 0)

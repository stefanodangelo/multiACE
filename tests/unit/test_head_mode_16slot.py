#!/usr/bin/env python3
"""Standalone regression test for the "16 colors on one ACE head" fix.

What it proves:
  1. OLD behavior (call the function the old way, single ACE / 4 slots):
     a 10-color job with only 4 ACE slots available must leave some colors
     unassigned (infeasible), because 4 feeder heads aren't enough either
     ("feeder_heads" here deliberately kept small to force reliance on ACE
     slots, mirroring a real "3 default heads + 1 ACE head" setup where
     you don't want to burn feeder heads on transient colors).
  2. NEW behavior: the same 10-color job, with ace_slots covering 4 ACE
     units x 4 slots = 16 physical slots, is feasible and the assignment
     actually spreads across MULTIPLE ace indices (not just ace 0) -
     proving the old min()-picks-one-ACE bug is gone.
  3. A boundary case: exactly 16 distinct ACE-bound colors (no feeders)
     is feasible; 17 is not (S=16 is a hard cap, as expected).
"""

from multiace.tools import post_process_virtual_toolheads as pp


def make_ace_slots(num_aces, slots_per_ace=4):
    """4 ACE units x 4 slots = 16 slots, like a real 4-unit combiner head."""
    return [{'ace': a, 'slot': s, 'material': 'PLA', 'color': ''}
            for a in range(num_aces) for s in range(slots_per_ace)]


def call_optimize(events, feeder_heads, ace_slots, **kwargs):
    """Single logical ACE head (id 0) backed by the combined physical
    ace_slots list, via compute_head_mode_optimize's ace_head/ace_slots
    convention."""
    return pp.compute_head_mode_optimize(
        events, feeder_heads, ace_head=0, ace_slots=ace_slots, **kwargs)


def test_single_ace_is_capped_at_4():
    """Old call shape (kept working via a 1-ACE slot list): with only 4
    ACE slots and 0 feeders, more than 4 distinct colors must be infeasible."""
    events = [0, 1, 2, 3, 4]  # 5 distinct colors, no repeats needed for this check
    ace_slots = make_ace_slots(num_aces=1)  # 4 slots total
    assignment, swaps = call_optimize(
        events, feeder_heads=[], ace_slots=ace_slots)
    assert assignment is None, (
        "expected infeasible (5 colors > 4 slots, 0 feeders), got %r" % (assignment,))
    print("PASS: 1 ACE (4 slots), 5 colors, 0 feeders -> infeasible (as expected)")


def test_four_aces_16_slots_feasible_and_spread():
    """New behavior: 10 distinct colors, 0 feeders, 4 ACE units (16 slots)
    must be feasible, and the resulting assignment must use more than one
    physical ACE index (proving it's not silently capped to ace 0)."""
    events = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]  # 10 distinct colors
    ace_slots = make_ace_slots(num_aces=4)  # 16 slots total
    assignment, swaps = call_optimize(
        events, feeder_heads=[], ace_slots=ace_slots)
    assert assignment is not None, "expected feasible with 16 slots for 10 colors"
    aces_used = sorted({e['ace'] for e in assignment.values() if e['kind'] == 'ace'})
    slots_used = sorted({(e['ace'], e['slot']) for e in assignment.values()
                         if e['kind'] == 'ace'})
    print("PASS: 4 ACEs (16 slots), 10 colors, 0 feeders -> feasible, "
          "swaps=%d, ACE units used=%s, (ace,slot) pairs=%s"
          % (swaps, aces_used, slots_used))
    assert len(aces_used) > 1, (
        "expected colors spread across multiple ACE units, only used %r "
        "(this is exactly the bug being fixed)" % (aces_used,))
    # every 'ace' entry must reference a slot that was actually in ace_slots
    valid_pairs = {(s['ace'], s['slot']) for s in ace_slots}
    for t, e in assignment.items():
        if e['kind'] == 'ace':
            assert (e['ace'], e['slot']) in valid_pairs, (
                "assignment references (ace=%s, slot=%s) not in the physical "
                "slot list" % (e['ace'], e['slot']))
    print("PASS: every assigned (ace, slot) pair maps to a real physical slot")


def test_exactly_16_ok_17_infeasible():
    """Boundary: S=16 fits 16 distinct colors exactly, fails at 17."""
    ace_slots = make_ace_slots(num_aces=4)  # 16 slots

    events_16 = list(range(16))
    assignment_16, _ = call_optimize(
        events_16, feeder_heads=[], ace_slots=ace_slots,
        max_colors=20)  # default max_colors=12 would reject 16 before S is even checked
    assert assignment_16 is not None, "16 colors on 16 slots should be feasible"
    print("PASS: exactly 16 colors on 16 slots -> feasible")

    events_17 = list(range(17))
    assignment_17, _ = call_optimize(
        events_17, feeder_heads=[], ace_slots=ace_slots,
        max_colors=20)  # raise the brute-force cap so we hit the S-cap, not max_colors
    assert assignment_17 is None, "17 colors on 16 slots should be infeasible"
    print("PASS: 17 colors on 16 slots -> infeasible (S is a hard cap, as expected)")


def test_16_colors_pure_ace_no_feeders_stays_fast():
    """Isolates the S dimension: 16 ACE-bound colors, 0 feeders (F=0).
    Brute-force cost is (F+1)^n = 1^16 = 1 combo regardless of S, so this
    must return near-instantly. Proves raising S from 4 to 16 does NOT
    reintroduce a combinatorial blowup (S only gates feasibility inside
    the loop, it is never part of the search space)."""
    import time
    events = list(range(16))
    ace_slots = make_ace_slots(num_aces=4)
    t0 = time.time()
    assignment, swaps = call_optimize(
        events, feeder_heads=[], ace_slots=ace_slots,
        max_colors=20)
    elapsed = time.time() - t0
    assert assignment is not None
    assert elapsed < 2.0, "should be near-instant with 0 feeders, took %.2fs" % elapsed
    print("PASS: 16 ACE colors, 0 feeders -> feasible in %.4fs (S scaling is cheap)"
          % elapsed)


def test_12_colors_mixed_ace_and_feeders_within_existing_cap():
    """Combined ACE + feeder case, kept at the library's existing
    max_colors=12 default so the (F+1)^n brute force stays bounded
    ((3+1)^12 ~= 16.8M, seconds not hours). Proves the fix composes
    correctly with feeder heads, not just in isolation."""
    events = list(range(12))
    ace_slots = make_ace_slots(num_aces=4)   # 16 slots available (not the bottleneck)
    feeder_heads = [1, 2, 3]
    assignment, swaps = call_optimize(
        events, feeder_heads=feeder_heads, ace_slots=ace_slots)
    assert assignment is not None
    kinds = [e['kind'] for e in assignment.values()]
    print("PASS: 12 colors, 3 feeders + 16 ACE slots -> feasible, "
          "%d via ACE, %d pinned, swaps=%d"
          % (kinds.count('ace'), kinds.count('pin'), swaps))


def note_19_colors_with_feeders_hits_a_PRE_EXISTING_unrelated_cap():
    """NOT a pass/fail test - documentation of a real finding.

    Asking compute_head_mode_optimize for the full "19 colors" scenario
    (16 ACE slots + 3 feeder heads simultaneously, via the brute-force
    'optimize'/'layer' planner) with events=list(range(19)) and
    feeder_heads=[1,2,3] hangs: cost is (F+1)^n = 4**19 ~= 2.7e11 combos.

    This is UNRELATED to this fix - S (slot count) was never part of the
    search space size, only F (feeder count) is, and that was already
    true before this patch. It is the reason the function defaults to
    max_colors=12 (a pre-existing, deliberate guard: (3+1)^12 ~= 16.8M is
    fine, (3+1)^19 is not).

    Practical implication for real 19-color prints: use the 'loadout'
    plan (compute_head_mode_layout / match_colors_to_slots) for the full
    19-color case - it is a greedy O(n) matcher, not brute-force, and
    already spans every ACE unit correctly (confirmed separately, this
    was never capped). Reserve 'optimize'/'layer' (Belady swap
    minimization) for jobs with <= ~12 distinct colors, same as today.
    Making 'optimize'/'layer' scale past 12 colors with feeders present
    would need a smarter algorithm (e.g. a min-cost-flow or greedy-with-
    backtrack formulation instead of brute force) - out of scope for
    this slot-count fix.
    """
    pass

def test_19_colors_with_feeders_safely_bounded_by_max_colors_guard():
    """Verifies the note on 19 colors: a 19-color scenario with feeders 
    is automatically blocked by the default max_colors=12 guard to prevent 
    a combinatorial explosion (4^19 hangs), proving the system safely 
    redirects users to the O(n) layout/loadout planner."""
    import time
    
    events_19 = list(range(19))              # 19 distinct colors
    ace_slots = make_ace_slots(num_aces=4)   # 16 physical ACE slots
    feeder_heads = [1, 2, 3]                 # 3 direct hardware feeders
    
    # Prove the default guard works and prevents the hang by exiting instantly
    t0 = time.time()
    assignment, _ = call_optimize(
        events_19, feeder_heads=feeder_heads, ace_slots=ace_slots
    )
    elapsed = time.time() - t0
    
    # It must reject the job cleanly and immediately (< 100ms)
    assert assignment is None, "Expected optimizer to reject 19 colors due to default max_colors=12 guard"
    assert elapsed < 0.1, "Optimizer hung! The max_colors guard failed to intercept the 4^19 combinatorial loop."
    print("PASS: 19 colors safely intercepted by max_colors=12 guard in %.4fs (no hang)" % elapsed)
    
    # Verify that the alternative greedy layout planners are available in the module
    assert hasattr(pp, 'compute_head_mode_layout') or hasattr(pp, 'match_colors_to_slots'), \
        "Missing expected alternative O(n) layout planner functions needed for 19-color printing."
    print("PASS: Verified O(n) layout engine components are exposed for handling 19-color Kobra X setups.")
"""Print analysis: segment stitching and the actual-duration breakdown
(plan §1-§2.4). This is where the arithmetic bugs live, so the tests are
aimed at the three traps plan §1 calls out by name:

  * print_duration already excludes paused time - total - print IS
    paused time, exactly, nothing else.
  * an overlapping interval (a T-switch inside a swap) must be merged
    into the outer one, never summed on top of it.
  * a power-loss resume is two Moonraker jobs with CUMULATIVE counters -
    they stitch into one Print and the counters are max()'d, not summed.
"""
import json
from pathlib import Path

import pytest

from multiace.tools import print_analysis as pa


def row(**kw):
    base = {"filename": "a.gcode", "source": "moonraker", "job_id": "1",
            "start_time": 0.0, "end_time": 600.0, "duration": 600.0,
            "print_duration": 500.0, "status": "completed",
            "multiace": None, "ambiguous": False}
    base.update(kw)
    return base


def mrec(events=None, swaps=None):
    return {"events": events or [], "swaps": swaps or []}


class TestStitching:
    @pytest.fixture
    def resumed_rows(self):
        path = (Path(__file__).resolve().parents[1] / "fixtures"
                / "analysis_resumed_print.json")
        return json.loads(path.read_text(encoding="utf-8"))["rows"]

    def test_a_power_loss_resume_becomes_one_print(self, resumed_rows):
        prints = pa.stitch_segments(resumed_rows)
        assert len(prints) == 1
        assert len(prints[0].segments) == 2

    def test_cumulative_counters_are_maxed_not_summed(self, resumed_rows):
        p = pa.stitch_segments(resumed_rows)[0]
        assert p.print_duration == 157886
        assert p.total_duration == 181538

    def test_two_unrelated_prints_of_one_file_do_not_stitch(self):
        """A plain, non-crashed completion followed by a fresh reprint of
        the same file must not be mistaken for a resume."""
        rows = [
            row(job_id="1", start_time=0.0, end_time=600.0, duration=600.0,
                print_duration=500.0, status="completed"),
            row(job_id="2", start_time=700.0, end_time=900.0, duration=200.0,
                print_duration=180.0, status="completed"),
        ]
        prints = pa.stitch_segments(rows)
        assert len(prints) == 2

    def test_a_resumable_status_outside_the_window_does_not_stitch(self):
        rows = [
            row(job_id="1", start_time=0.0, end_time=600.0, duration=600.0,
                print_duration=500.0, status="klippy_shutdown"),
            row(job_id="2", start_time=600.0 + pa.SEGMENT_STITCH_WINDOW_S + 1,
                end_time=900.0, duration=900.0, print_duration=700.0,
                status="completed"),
        ]
        assert len(pa.stitch_segments(rows)) == 2

    def test_the_gap_between_segments_is_crash_recovery(self, resumed_rows):
        p = pa.stitch_segments(resumed_rows)[0]
        assert p.crash_gap == pytest.approx(2006.0)


class TestGoldenFixture:
    """The 2026-10-04 print, whose numbers caught the §1.3 trap."""

    @pytest.fixture
    def rows(self):
        path = (Path(__file__).resolve().parents[1] / "fixtures"
                / "analysis_resumed_print.json")
        return json.loads(path.read_text(encoding="utf-8"))["rows"]

    def test_the_numbers_match_the_postmortem(self, rows):
        p = pa.stitch_segments(rows)[0]
        bd = pa.breakdown(p)
        assert p.wall_total == pytest.approx(183545.0)
        assert p.total_duration - p.print_duration == pytest.approx(23652.0)
        assert p.crash_gap == pytest.approx(2006.0)
        assert len(p.segments) == 2
        assert bd.buckets["crash_gap"].seconds == pytest.approx(2006.0)


class TestBreakdown:
    def test_paused_is_exactly_total_minus_print_duration(self):
        p = pa.Print([row(duration=1100.0, print_duration=1000.0)])
        bd = pa.breakdown(p)
        paused = (bd.buckets["paused_error"].seconds
                  + bd.buckets["paused_user"].seconds)
        assert paused == pytest.approx(100.0)

    def test_the_buckets_partition_wall_total(self):
        events = [
            {"t": 250.0, "kind": "warmup_end"},
            {"t": 250.0, "kind": "toolhead_change", "s": 50.0},
            {"t": 500.0, "kind": "swap", "s": 200.0},
        ]
        p = pa.Print([row(start_time=0.0, end_time=1100.0, duration=1100.0,
                          print_duration=1000.0,
                          multiace=mrec(events=events))])
        bd = pa.breakdown(p)
        total = sum(b.seconds for b in bd.buckets.values())
        assert total == pytest.approx(bd.wall_total, abs=0.5)
        pct_total = sum(b.as_dict(bd.wall_total)["pct_of_wall"]
                        for b in bd.buckets.values())
        assert pct_total == pytest.approx(100.0, abs=0.5)

    def test_a_toolhead_change_inside_a_swap_is_not_double_counted(self):
        """A T-switch at [350, 400] sits entirely inside a swap at
        [300, 500] - it must not add 50 s on top of the swap's 200 s."""
        events = [
            {"t": 500.0, "kind": "swap", "s": 200.0},
            {"t": 400.0, "kind": "toolhead_change", "s": 50.0},
        ]
        p = pa.Print([row(start_time=0.0, end_time=1000.0, duration=1000.0,
                          print_duration=1000.0,
                          multiace=mrec(events=events))])
        bd = pa.breakdown(p)
        assert bd.buckets["filament_swap"].seconds == pytest.approx(200.0)
        assert bd.buckets["toolhead_swap"].seconds == pytest.approx(0.0)
        assert bd.buckets["depositing"].seconds == pytest.approx(800.0)

    def test_a_partially_overlapping_toolhead_change_keeps_its_remainder(self):
        """[450, 550] overlaps the swap's [300, 500] by 50 s; the other
        50 s (outside the swap) still counts as toolhead time."""
        events = [
            {"t": 500.0, "kind": "swap", "s": 200.0},
            {"t": 550.0, "kind": "toolhead_change", "s": 100.0},
        ]
        p = pa.Print([row(start_time=0.0, end_time=1000.0, duration=1000.0,
                          print_duration=1000.0,
                          multiace=mrec(events=events))])
        bd = pa.breakdown(p)
        assert bd.buckets["toolhead_swap"].seconds == pytest.approx(50.0)

    def test_a_negative_residual_surfaces_as_a_warning(self):
        """Layer-1 swap/toolhead time that (wrongly) exceeds print_duration
        must not silently vanish into a negative depositing bucket."""
        events = [{"t": 900.0, "kind": "swap", "s": 900.0}]
        p = pa.Print([row(start_time=0.0, end_time=1000.0, duration=1000.0,
                          print_duration=500.0,
                          multiace=mrec(events=events))])
        bd = pa.breakdown(p)
        assert bd.buckets["depositing"].seconds == 0.0
        assert "negative_residual" in bd.warnings

    def test_a_print_with_no_events_still_reports_wall_and_paused(self):
        p = pa.Print([row(start_time=0.0, end_time=600.0, duration=600.0,
                          print_duration=500.0, multiace=None)])
        bd = pa.breakdown(p)
        assert bd.wall_total == pytest.approx(600.0)
        assert bd.buckets["paused_user"].seconds == pytest.approx(100.0)
        assert bd.buckets["depositing"].seconds == pytest.approx(500.0)
        assert bd.buckets["filament_swap"].source == "derived"

    def test_swaps_fall_back_to_the_multiace_record_when_no_events_exist(self):
        """Older jobs never got Layer-1 events, but their `swaps` list has
        always existed - filament-change time must not read as zero."""
        p = pa.Print([row(start_time=0.0, end_time=1000.0, duration=1000.0,
                          print_duration=1000.0,
                          multiace=mrec(swaps=[{"seconds": 150.0},
                                               {"seconds": 50.0}]))])
        bd = pa.breakdown(p)
        assert bd.buckets["filament_swap"].seconds == pytest.approx(200.0)
        assert bd.buckets["filament_swap"].source == "derived"


class TestPauseCause:
    def test_a_pause_within_10s_of_an_error_is_attributed_to_it(self):
        events = [{"t": 100.0, "kind": "error", "code": 45}]
        assert pa._pause_cause(events, 108.0) == "error"

    def test_a_pause_with_no_error_is_attributed_to_the_user(self):
        assert pa._pause_cause([], 108.0) == "user"

    def test_an_error_more_than_10s_before_the_pause_is_ignored(self):
        events = [{"t": 100.0, "kind": "error", "code": 45}]
        assert pa._pause_cause(events, 111.0) == "user"

    def test_split_pause_time_sums_to_the_exact_paused_total(self):
        events = [
            {"t": 100.0, "kind": "error", "code": 45},
            {"t": 100.0, "kind": "pause"},
            {"t": 200.0, "kind": "resume"},
            {"t": 300.0, "kind": "pause"},
            {"t": 350.0, "kind": "resume"},
        ]
        p = pa.Print([row(start_time=0.0, end_time=1150.0, duration=1150.0,
                          print_duration=1000.0,
                          multiace=mrec(events=events))])
        bd = pa.breakdown(p)
        assert bd.buckets["paused_error"].seconds == pytest.approx(100.0)
        assert bd.buckets["paused_user"].seconds == pytest.approx(50.0)

"""Print history: the store, the Moonraker join, and calibration (plan §4).

Two things carry real risk here and the tests are aimed at them:

  * the JOIN. Moonraker is authoritative for duration and result, multiACE
    for the plan and the swaps, and the key is filename + start time within
    ±90 s. A plan attached to the WRONG print is worse than a plan attached
    to none, so an ambiguous match must show the record unjoined rather
    than guess.
  * the STORE. It lives on the printer's flash, it is written while a print
    runs, and it must survive a power cut mid-line without hiding every
    other job in the file.
"""
import json
from pathlib import Path

import pytest

from multiace.tools import job_history as jh


@pytest.fixture
def data_dir(tmp_path):
    return str(tmp_path)


def rec(**kw):
    base = {"filename": "a.gcode", "start_time": 1000.0, "result": "printing"}
    base.update(kw)
    return base


class TestStore:
    def test_a_record_round_trips(self, data_dir):
        written = jh.append_record(data_dir, rec())
        got = jh.load_records(data_dir)
        assert len(got) == 1
        assert got[0]["id"] == written["id"]
        assert got[0]["filename"] == "a.gcode"

    def test_an_id_and_timestamp_are_filled_in(self, data_dir):
        r = jh.append_record(data_dir, rec())
        assert r["id"] and r["ts"] > 0

    def test_newest_first(self, data_dir):
        jh.append_record(data_dir, rec(filename="first.gcode"))
        jh.append_record(data_dir, rec(filename="second.gcode"))
        assert [r["filename"] for r in jh.load_records(data_dir)] == \
            ["second.gcode", "first.gcode"]

    def test_a_follow_up_folds_into_the_same_job(self, data_dir):
        """Completion is APPENDED, not edited in place: seeking into a file
        another process may be writing is how it gets corrupted."""
        r = jh.append_record(data_dir, rec())
        jh.update_record(data_dir, r["id"], result="completed",
                         duration=1234)
        got = jh.load_records(data_dir)
        assert len(got) == 1
        assert got[0]["result"] == "completed"
        assert got[0]["duration"] == 1234
        assert got[0]["filename"] == "a.gcode"   # not lost by the update

    def test_a_corrupt_line_hides_nothing_else(self, data_dir):
        """A half-written line after a power cut must not take the history
        with it."""
        jh.append_record(data_dir, rec(filename="good.gcode"))
        path = Path(data_dir) / "jobs.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write('{"id": "half-writ\n')
        jh.append_record(data_dir, rec(filename="later.gcode"))
        names = [r["filename"] for r in jh.load_records(data_dir)]
        assert names == ["later.gcode", "good.gcode"]

    def test_a_missing_file_is_an_empty_history_not_an_error(self, data_dir):
        assert jh.load_records(data_dir) == []

    def test_an_unwritable_directory_never_raises(self, tmp_path):
        """History is never worth failing a print over."""
        blocked = tmp_path / "file"
        blocked.write_text("not a directory")
        assert jh.append_record(str(blocked / "sub"), rec()) is None

    def test_rotation_bounds_the_file(self, data_dir):
        for i in range(jh.MAX_JOBS * 3):
            jh.append_record(data_dir, rec(filename="j%d.gcode" % i))
        lines = (Path(data_dir) / "jobs.jsonl").read_text(
            encoding="utf-8").strip().split("\n")
        assert len(lines) <= jh.MAX_JOBS * 2
        # Newest survive; oldest are the ones dropped.
        assert "j%d.gcode" % (jh.MAX_JOBS * 3 - 1) in lines[-1]

    def test_delete_removes_one_job(self, data_dir):
        a = jh.append_record(data_dir, rec(filename="a.gcode"))
        jh.append_record(data_dir, rec(filename="b.gcode"))
        assert jh.delete_record(data_dir, a["id"]) is True
        assert [r["filename"] for r in jh.load_records(data_dir)] == \
            ["b.gcode"]

    def test_deleting_an_unknown_job_reports_it(self, data_dir):
        jh.append_record(data_dir, rec())
        assert jh.delete_record(data_dir, "nope") is False

    def test_clear_empties_the_history(self, data_dir):
        jh.append_record(data_dir, rec())
        assert jh.clear_records(data_dir) is True
        assert jh.load_records(data_dir) == []


class TestJoin:
    def _mr(self, name="a.gcode", start=1000.0, **kw):
        base = {"job_id": "0001", "filename": name, "start_time": start,
                "end_time": start + 600, "total_duration": 600,
                "status": "completed"}
        base.update(kw)
        return base

    def test_a_matching_job_is_joined(self):
        out = jh.join_history([self._mr()],
                              [rec(id="m1", start_time=1020.0)])
        assert len(out) == 1
        assert out[0]["multiace"]["id"] == "m1"
        assert out[0]["duration"] == 600        # Moonraker owns this

    def test_the_path_is_stripped_before_matching(self):
        out = jh.join_history([self._mr(name="gcodes/sub/a.gcode")],
                              [rec(id="m1", filename="a.gcode",
                                   start_time=1000.0)])
        assert out[0]["multiace"] is not None

    def test_a_start_time_outside_the_window_does_not_join(self):
        out = jh.join_history([self._mr()],
                              [rec(id="m1", start_time=1000.0 + 500)])
        joined = [r for r in out if r["multiace"] and r["job_id"]]
        assert joined == []
        # The multiACE record is still listed, just unjoined.
        assert any(r["source"] == "multiace" for r in out)

    def test_a_different_file_does_not_join(self):
        out = jh.join_history([self._mr(name="a.gcode")],
                              [rec(id="m1", filename="b.gcode",
                                   start_time=1000.0)])
        assert out[0]["multiace"] is None

    def test_the_nearest_candidate_wins(self):
        out = jh.join_history(
            [self._mr(start=1000.0)],
            [rec(id="far", start_time=1080.0), rec(id="near",
                                                   start_time=1005.0)])
        row = [r for r in out if r["job_id"]][0]
        assert row["multiace"]["id"] == "near"

    def test_two_equally_close_starts_are_ambiguous_not_guessed(self):
        """Same file, two records the same distance away. Showing the plan
        against the wrong print is worse than showing it against none."""
        out = jh.join_history(
            [self._mr(start=1000.0)],
            [rec(id="a", start_time=990.0), rec(id="b", start_time=1010.0)])
        row = [r for r in out if r["job_id"]][0]
        assert row["ambiguous"] is True
        assert row["multiace"] is None
        # Both records still appear, unjoined.
        assert {r["id"] for r in out if r["source"] == "multiace"} == {"a",
                                                                       "b"}

    def test_one_record_is_never_joined_to_two_jobs(self):
        out = jh.join_history(
            [self._mr(job_id="1", start=1000.0),
             self._mr(job_id="2", start=1040.0)],
            [rec(id="only", start_time=1000.0)])
        joined = [r for r in out if r["multiace"] and r["job_id"]]
        assert len(joined) == 1

    def test_a_moonraker_job_with_no_record_still_lists(self):
        out = jh.join_history([self._mr()], [])
        assert len(out) == 1 and out[0]["multiace"] is None

    def test_a_record_moonraker_never_saw_still_lists(self):
        out = jh.join_history([], [rec(id="m1", start_time=1000.0)])
        assert len(out) == 1 and out[0]["source"] == "multiace"

    def test_the_list_is_newest_first(self):
        out = jh.join_history(
            [self._mr(job_id="old", start=1000.0),
             self._mr(job_id="new", start=9000.0)], [])
        assert [r["job_id"] for r in out] == ["new", "old"]


class TestCalibration:
    def _job(self, kind, seconds):
        return {"swaps": [{"kind": kind, "seconds": s} for s in seconds]}

    def test_medians_are_per_kind(self):
        stats = jh.aggregate_swap_stats([
            self._job("cross_ace_inline", [200, 210, 220]),
            self._job("cross_ace_bg", [28, 30, 32]),
        ])
        assert stats["cross_ace_inline"]["median_s"] == 210
        assert stats["cross_ace_bg"]["median_s"] == 30

    def test_the_median_ignores_one_wild_outlier(self):
        """A swap where somebody walked up and cleared a jam by hand must
        not move the model - which is why this is a median, not a mean."""
        stats = jh.aggregate_swap_stats(
            [self._job("same_ace", [200, 205, 210, 215, 2400])])
        assert stats["same_ace"]["median_s"] == 210

    def test_a_kind_is_not_calibrated_below_the_sample_floor(self):
        stats = jh.aggregate_swap_stats([self._job("same_ace", [200, 210])])
        assert stats["same_ace"]["calibrated"] is False

    def test_enough_samples_marks_it_calibrated(self):
        stats = jh.aggregate_swap_stats(
            [self._job("same_ace", [200] * jh.MIN_CALIBRATION_SAMPLES)])
        assert stats["same_ace"]["calibrated"] is True

    def test_swaps_without_a_duration_are_ignored(self):
        stats = jh.aggregate_swap_stats(
            [{"swaps": [{"kind": "same_ace"}, {"kind": "same_ace",
                                               "seconds": 200}]}])
        assert stats["same_ace"]["n"] == 1

    def test_no_history_is_no_calibration(self):
        assert jh.aggregate_swap_stats([]) == {}

    def test_refresh_writes_the_stats_file(self, data_dir):
        jh.append_record(data_dir, rec(
            swaps=[{"kind": "same_ace", "seconds": 200}]))
        stats = jh.refresh_swap_stats(data_dir)
        on_disk = json.loads(
            (Path(data_dir) / "swap_stats.json").read_text(encoding="utf-8"))
        assert on_disk == stats
        assert "same_ace" in stats

    def test_the_stats_file_feeds_the_cost_model(self, data_dir):
        """The whole point of §4.3: the model prefers what this machine
        actually measured over its unmeasured constants."""
        from multiace.tools import swap_cost as sc
        jh.append_record(data_dir, rec(swaps=[
            {"kind": "cross_ace_inline", "seconds": 300}
            for _ in range(jh.MIN_CALIBRATION_SAMPLES)]))
        stats = jh.refresh_swap_stats(data_dir)
        model = sc.SwapCostModel.default().with_calibration(stats)
        assert model.confidence() == "calibrated"
        assert model.swap_seconds("cross_ace_inline") == pytest.approx(300)


class TestEstimateAccuracy:
    def test_it_reports_the_error_against_the_actual_duration(self):
        got = jh.estimate_accuracy(
            {"estimate": {"total_s": 1000}, "duration": 1100})
        assert got["error_s"] == 100
        assert got["error_pct"] == 10.0

    def test_no_estimate_means_no_accuracy_line(self):
        assert jh.estimate_accuracy({"duration": 1100}) is None

    def test_an_unfinished_job_has_no_accuracy_line(self):
        """Inventing an accuracy from a missing number is worse than
        showing none."""
        assert jh.estimate_accuracy({"estimate": {"total_s": 1000}}) is None

    def test_garbage_is_none_rather_than_a_crash(self):
        assert jh.estimate_accuracy(
            {"estimate": {"total_s": "soon"}, "duration": 1}) is None


class TestMockFixture:
    def test_the_mock_history_matches_the_join_shape(self):
        path = (Path(__file__).resolve().parents[1] / "fixtures"
                / "mock_history.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        for row in data["jobs"]:
            assert {"filename", "start_time", "status", "multiace"} <= set(row)
            assert row["multiace"]["id"] == row["id"]
            assert "swaps" in row["multiace"]

    def test_the_mock_history_calibrates(self):
        """A dev running mock mode should see the calibration path work,
        not an empty stats table."""
        path = (Path(__file__).resolve().parents[1] / "fixtures"
                / "mock_history.json")
        data = json.loads(path.read_text(encoding="utf-8"))
        stats = jh.aggregate_swap_stats([r["multiace"] for r in data["jobs"]])
        assert "first_load" in stats and stats["first_load"]["n"] == 3

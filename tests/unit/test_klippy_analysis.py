"""klippy.log enrichment (plan §2.3, Layer 3) - the lowest-trust layer,
so the tests lean on exactly the traps the module's docstring calls out:
a line must be prefiltered before it is ever parsed, a missing/rotated
log must degrade rather than error, and a year-less timestamp must not
silently go backwards across New Year's.
"""
import os
import shutil
import time
from pathlib import Path

import pytest

from multiace.tools import klippy_analysis as ka

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "klippy_excerpt.log"


def _ts(year, month, day, hour, minute, second=0):
    return time.mktime((year, month, day, hour, minute, second, 0, 0, -1))


@pytest.fixture
def log_dir(tmp_path):
    """The excerpt, copied to a scratch dir with a CONTROLLED mtime - the
    year resolution in klippy_analysis.py reads the file's mtime, and a
    fixture file's checked-out mtime is whatever `git checkout` happened
    to leave it at, not whatever date the log excerpt's own lines imply."""
    dst = tmp_path / "klippy.log"
    shutil.copy(FIXTURE, dst)
    mtime = _ts(2026, 10, 3, 23, 59, 0)
    os.utime(dst, (mtime, mtime))
    return tmp_path


class TestTimerTooClose:
    def test_timer_too_close_yields_lateness_in_seconds(self, log_dir):
        result = ka.analyse_logs(
            str(log_dir), _ts(2026, 10, 3, 0, 0), _ts(2026, 10, 3, 23, 0))
        assert len(result["host_stalls"]) == 1
        assert result["host_stalls"][0]["lateness_s"] == pytest.approx(
            182023.016683 - 182023.008667)

    def test_the_mcu_shutdown_is_captured_alongside_it(self, log_dir):
        result = ka.analyse_logs(
            str(log_dir), _ts(2026, 10, 3, 0, 0), _ts(2026, 10, 3, 23, 0))
        assert len(result["mcu_shutdowns"]) == 1
        assert result["mcu_shutdowns"][0]["mcu"] == "mcu"


class TestPrefilterVsFlood:
    def test_the_repeated_permission_error_is_prefiltered_not_parsed(self, log_dir):
        result = ka.analyse_logs(
            str(log_dir), _ts(2026, 10, 3, 0, 0), _ts(2026, 10, 3, 23, 0))
        for key in ("host_stalls", "mcu_shutdowns", "pickup_failures",
                    "sd_positions", "ace_comms", "spool_ledger"):
            for item in result[key]:
                assert "_refresh_slot_overrides" not in item.get("line", "")

    def test_the_flood_is_still_detected_as_self_diagnosis(self, log_dir):
        result = ka.analyse_logs(
            str(log_dir), _ts(2026, 10, 3, 0, 0), _ts(2026, 10, 3, 23, 0))
        assert result["log_flood"] is not None
        assert result["log_flood"]["count"] >= 20


class TestOtherSignals:
    def test_pickup_failure_is_captured(self, log_dir):
        result = ka.analyse_logs(
            str(log_dir), _ts(2026, 10, 3, 0, 0), _ts(2026, 10, 3, 23, 0))
        assert any("pogopin not connected" in p["line"]
                    for p in result["pickup_failures"])

    def test_print_stall_increments_are_captured(self, log_dir):
        result = ka.analyse_logs(
            str(log_dir), _ts(2026, 10, 3, 0, 0), _ts(2026, 10, 3, 23, 0))
        assert any(s["count"] == 1 for s in result["print_stalls"])

    def test_health_samples_carry_buffer_time(self, log_dir):
        result = ka.analyse_logs(
            str(log_dir), _ts(2026, 10, 3, 0, 0), _ts(2026, 10, 3, 23, 0))
        assert any("buffer_time" in s for s in result["health_samples"])

    def test_ace_comms_flap_is_captured(self, log_dir):
        result = ka.analyse_logs(
            str(log_dir), _ts(2026, 10, 3, 0, 0), _ts(2026, 10, 3, 23, 0))
        assert len(result["ace_comms"]) >= 3

    def test_spool_ledger_lines_are_captured_verbatim(self, log_dir):
        result = ka.analyse_logs(
            str(log_dir), _ts(2026, 10, 3, 0, 0), _ts(2026, 10, 3, 23, 0))
        assert any("print total" in s["line"] for s in result["spool_ledger"])

    def test_sd_positions_track_start_and_finish(self, log_dir):
        result = ka.analyse_logs(
            str(log_dir), _ts(2026, 10, 3, 0, 0), _ts(2026, 10, 3, 23, 0))
        phases = {p["phase"] for p in result["sd_positions"]}
        assert phases == {"starting", "finished"}


class TestCoverage:
    def test_a_rotated_window_reports_partial_coverage(self, log_dir):
        """A window reaching back further than the retained log says so,
        rather than implying nothing happened before it (plan §2.3.5)."""
        result = ka.analyse_logs(
            str(log_dir), _ts(2026, 10, 2, 12, 0), _ts(2026, 10, 3, 7, 0))
        assert result["rotated_away"] is True

    def test_a_window_fully_inside_the_log_is_not_rotated_away(self, log_dir):
        result = ka.analyse_logs(
            str(log_dir), _ts(2026, 10, 3, 2, 0), _ts(2026, 10, 3, 7, 0))
        assert result["rotated_away"] is False

    def test_the_line_budget_truncates_rather_than_hangs(self, log_dir):
        result = ka.analyse_logs(
            str(log_dir), _ts(2026, 10, 3, 0, 0), _ts(2026, 10, 3, 23, 0),
            max_lines=5)
        assert result["truncated"] is True

    def test_a_missing_log_directory_is_empty_not_an_error(self, tmp_path):
        result = ka.analyse_logs(
            str(tmp_path / "does-not-exist"),
            _ts(2026, 10, 3, 0, 0), _ts(2026, 10, 3, 23, 0))
        assert result["files"] == []
        assert result["rotated_away"] is False
        assert result["truncated"] is False


class TestYearlessTimestamps:
    def test_a_year_less_timestamp_crossing_new_year_resolves(self, tmp_path):
        """Both lines sit in the SAME file - the rollover has to be
        tracked mid-scan, not just resolved once per file."""
        log = tmp_path / "klippy.log"
        log.write_text(
            "12-31 23:59:50.000 Timer too close: waketime=2.0, "
            "timer_read_time=1.0\n"
            "01-01 00:00:05.000 Timer too close: waketime=4.0, "
            "timer_read_time=1.0\n",
            encoding="utf-8")
        mtime = _ts(2027, 1, 1, 0, 10, 0)
        os.utime(log, (mtime, mtime))

        result = ka.analyse_logs(
            str(tmp_path), _ts(2026, 12, 31, 20, 0), _ts(2027, 1, 1, 1, 0))
        assert len(result["host_stalls"]) == 2
        a, b = result["host_stalls"]
        assert b["t"] - a["t"] == pytest.approx(15.0, abs=0.01)

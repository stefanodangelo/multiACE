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


def _stats(t_str, pos, bed=65, e0=0, e1=50):
    return ("%s:Stats 1000.0: gcodein=0  mcu: mcu_awake=0.010 sd_pos=%d "
            "heater_bed: target=%d temp=65.0 pwm=0.0 extruder: target=%d "
            "temp=29.0 pwm=0.0 extruder1: target=%d temp=60.0 pwm=0.0\n"
            % (t_str, pos, bed, e0, e1))


class TestPrintHangs:
    """A file position that stops moving while heaters stay on is a hung
    print (overnight 2026-10-12: an M109 whose target had been zeroed)."""

    def _log(self, tmp_path, lines):
        path = tmp_path / "klippy.log"
        path.write_text("".join(lines), encoding="utf-8")
        mtime = _ts(2026, 10, 13, 12, 0, 0)
        os.utime(path, (mtime, mtime))
        return str(tmp_path)

    def _scan(self, d, hour0=21, hour1=23):
        return ka.analyse_logs(d, _ts(2026, 10, 12, hour0, 0),
                               _ts(2026, 10, 13, hour1, 0))

    def test_a_frozen_position_with_heaters_on_is_a_hang(self, tmp_path):
        lines = [_stats("10-12 21:59:57.000", 100),
                 _stats("10-12 22:03:31.000", 500),
                 "10-12 22:03:36.726:[multiACE] [bg-unload] head 1: LOAD "
                 "FAILED (feed not confirmed by the device) - the arrival "
                 "swap loads inline\n"]
        for m in range(33, 60, 2):
            lines.append(_stats("10-12 22:%02d:00.000" % m, 500))
        for h in range(23, 24):
            lines.append(_stats("10-12 %02d:30:00.000" % h, 500))
        lines.append(_stats("10-13 06:30:00.000", 500))
        lines.append(_stats("10-13 06:53:11.000", 0, bed=0, e1=0))
        res = self._scan(self._log(tmp_path, lines))
        assert len(res["print_hangs"]) == 1
        h = res["print_hangs"][0]
        assert h["position"] == 500
        assert h["minutes"] > 400
        assert "LOAD FAILED" in h["last_op"]

    def test_an_idle_printer_is_not_a_hang(self, tmp_path):
        """Position constant but every heater off: the printer is idle."""
        lines = [_stats("10-12 22:%02d:00.000" % m, 500, bed=0, e1=0)
                 for m in range(0, 60, 2)]
        res = self._scan(self._log(tmp_path, lines))
        assert res["print_hangs"] == []

    def test_a_short_pause_in_progress_is_not_a_hang(self, tmp_path):
        lines = [_stats("10-12 22:00:00.000", 500),
                 _stats("10-12 22:05:00.000", 500),
                 _stats("10-12 22:05:02.000", 900)]
        assert self._scan(self._log(tmp_path, lines))["print_hangs"] == []

    def test_heartbeat_lines_are_not_a_log_flood(self, tmp_path):
        lines = [_stats("10-12 22:%02d:%02d.000" % (m, s), 500 + m)
                 for m in range(0, 30) for s in (0, 20, 40)]
        assert self._scan(self._log(tmp_path, lines))["log_flood"] is None

    def test_progress_and_the_last_progress_time_are_reported(self, tmp_path):
        lines = [_stats("10-12 22:00:00.000", 100),
                 _stats("10-12 22:01:00.000", 200)]
        res = self._scan(self._log(tmp_path, lines))
        assert res["last_progress_t"] == pytest.approx(
            _ts(2026, 10, 12, 22, 1, 0))

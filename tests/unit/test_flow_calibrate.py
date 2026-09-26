"""Flow-calibration relocation (plan step 7 / cluster D's non-PA half).

Stock's touchscreen start is the only place that turns FLOW_CALIBRATE on;
a web-started print never sees that dialog, so without this the slicer's
placeholder pressure-advance value printed regardless of what the user had
calibrated (issue #115: rough top surfaces, overextrusion). The fix moves
the calibration behind the auto-load block instead of leaving it forced
off - these tests pin the line-building and the file rewrite, not the
motion itself.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "multiace" / "web" / "backend"


@pytest.fixture
def core(monkeypatch):
    monkeypatch.syspath_prepend(str(BACKEND))
    sys.modules.pop("preflight_core", None)
    import preflight_core
    return preflight_core


class TestPrintPrefsLine:
    def test_flow_cal_off_by_default(self, core):
        line = core.print_prefs_line(False, False)
        assert "FLOW_CALIBRATE=0" in line

    def test_flow_cal_on_sets_the_flag(self, core):
        line = core.print_prefs_line(False, False, flow_cal=True)
        assert "FLOW_CALIBRATE=1" in line

    def test_bed_mesh_and_camera_are_independent_of_flow_cal(self, core):
        line = core.print_prefs_line(True, True, flow_cal=True)
        assert "BED_LEVEL=1" in line
        assert "TIME_LAPSE_CAMERA=1" in line
        assert "FLOW_CALIBRATE=1" in line
        assert line.endswith("FORCE=1")


class TestFlowCalBlock:
    def test_no_auto_load_marker_yields_no_block(self, core):
        lines = ["T0\n", "G1 X1\n"]
        idx, block = core._flow_cal_block(lines)
        assert idx is None
        assert block == []

    def test_no_bare_t_lines_yields_no_block(self, core):
        lines = ["; multiACE auto-load: end\n", "G1 X1\n"]
        idx, block = core._flow_cal_block(lines)
        assert idx is None
        assert block == []

    def test_anchors_right_after_the_auto_load_end_marker(self, core):
        lines = ["; header\n", "T2\n", "T0\n",
                 "; multiACE auto-load: end\n", "G1 X1\n"]
        idx, block = core._flow_cal_block(lines)
        assert idx == 4

    def test_initial_head_is_calibrated_last_and_reselected(self, core):
        """The LAST bare-T line before the auto-load-end marker is the tool
        the start gcode leaves active - the block must calibrate the OTHER
        heads first, then come back to it (the prime line right after
        assumes that tool is selected)."""
        lines = ["T2\n", "T0\n", "T1\n",
                 "; multiACE auto-load: end\n"]
        idx, block = core._flow_cal_block(lines)
        text = "".join(block)
        assert text.index("T2 A0") < text.index("T1 A0")
        assert text.rstrip().split("\n")[-2] == "T1"

    def test_one_pair_per_physical_head_only(self, core):
        lines = ["T0\n", "T1\n", "T0\n",  # head 0 appears twice
                 "; multiACE auto-load: end\n"]
        idx, block = core._flow_cal_block(lines)
        text = "".join(block)
        assert text.count("FLOW_CALIBRATE EXTRUDER=0") == 1
        assert text.count("FLOW_CALIBRATE EXTRUDER=1") == 1


class TestPrependPrintPrefs:
    def _write(self, tmp_path, lines):
        p = tmp_path / "in.gcode"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def test_flow_cal_off_leaves_stock_lines_untouched(self, core, tmp_path):
        src = self._write(tmp_path, [
            "SM_PRINT_FLOW_CALIBRATE", "T0",
            "; multiACE auto-load: end", "G1 X1"])
        out = tmp_path / "out.gcode"
        core.prepend_print_prefs(str(src), str(out), flow_cal=False)
        text = out.read_text(encoding="utf-8")
        assert "FLOW_CALIBRATE=0" in text.splitlines()[1]
        assert "SM_PRINT_FLOW_CALIBRATE" in text
        assert "; multiACE moved:" not in text

    def test_flow_cal_on_relocates_behind_auto_load(self, core, tmp_path):
        src = self._write(tmp_path, [
            "SM_PRINT_FLOW_CALIBRATE", "T0",
            "; multiACE auto-load: end", "G1 X1"])
        out = tmp_path / "out.gcode"
        core.prepend_print_prefs(str(src), str(out), flow_cal=True)
        text = out.read_text(encoding="utf-8")
        assert "FLOW_CALIBRATE=1" in text.splitlines()[1]
        assert "; multiACE moved: SM_PRINT_FLOW_CALIBRATE" in text
        end_pos = text.index("; multiACE auto-load: end")
        block_pos = text.index("; multiACE preflight: flow calibration")
        assert block_pos > end_pos

    def test_slicer_set_print_preferences_is_always_disabled(self, core, tmp_path):
        src = self._write(tmp_path, [
            "SET_PRINT_PREFERENCES BED_LEVEL=0", "T0",
            "; multiACE auto-load: end"])
        out = tmp_path / "out.gcode"
        core.prepend_print_prefs(str(src), str(out), flow_cal=True)
        text = out.read_text(encoding="utf-8")
        assert "; multiACE disabled: SET_PRINT_PREFERENCES" in text

    def test_no_anchor_falls_back_to_stock_placement(self, core, tmp_path):
        """A file with no auto-load block (e.g. all-pinned head mode) must
        not lose its SM_PRINT_FLOW_CALIBRATE line - it just stays where
        stock put it."""
        src = self._write(tmp_path, ["SM_PRINT_FLOW_CALIBRATE", "G1 X1"])
        out = tmp_path / "out.gcode"
        core.prepend_print_prefs(str(src), str(out), flow_cal=True)
        text = out.read_text(encoding="utf-8")
        assert "SM_PRINT_FLOW_CALIBRATE" in text
        assert "; multiACE moved:" not in text

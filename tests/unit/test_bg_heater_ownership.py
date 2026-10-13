"""The background swap must never switch off a heater the print took over.

Overnight 2026-10-12 a background load failed three seconds after the print
had reached the arrival `M109 S200 T0` for the same head. The failure
cleanup zeroed the heater, `M109` only returns when the temperature gets
within 1 degC of the target, and the print hung for 9 hours with a target of
0 and a nozzle at 30 degC. These tests pin the ownership rule: the swap
releases only what it set itself, and steps aside when the print sets a
printing temperature.
"""
import importlib.util
import sys
import types
from pathlib import Path

import pytest

EXTRAS = Path(__file__).resolve().parents[2] / "multiace" / "klipper" / "extras"


@pytest.fixture(scope="module")
def bg_module():
    sys.modules.setdefault("chelper", types.ModuleType("chelper"))
    pkg = sys.modules.get("_bg_extras")
    if pkg is None:
        pkg = types.ModuleType("_bg_extras")
        pkg.__path__ = [str(EXTRAS)]
        sys.modules["_bg_extras"] = pkg
        sys.modules["_bg_extras.force_move"] = types.ModuleType(
            "_bg_extras.force_move")
    spec = importlib.util.spec_from_file_location(
        "_bg_extras.ace_bg_swap", EXTRAS / "ace_bg_swap.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_bg_extras.ace_bg_swap"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeHeater:
    def __init__(self, target=0.0):
        self.target = target

    def get_temp(self, _t):
        return 29.7, self.target


class FakeHeaters:
    def set_temperature(self, heater, temp):
        heater.target = float(temp)


class FakeReactor:
    def monotonic(self):
        return 0.0


@pytest.fixture
def bg(bg_module):
    b = bg_module.AceBgSwap.__new__(bg_module.AceBgSwap)
    b.reactor = FakeReactor()
    b._bg_heat = {}
    b._bg_baseline = {}
    b._busy = set()
    b.state = {}
    b.said = []
    b._say = lambda msg: b.said.append(msg)
    b._dh = lambda h: h + 1
    return b


@pytest.fixture
def heater():
    return FakeHeater(50.0)        # parked head, slicer standby target


@pytest.fixture
def heaters():
    return FakeHeaters()


class TestOwnership:
    def test_starting_a_swap_takes_the_heater(self, bg, heater, heaters):
        bg._bg_swap_begin(0, heater)
        bg._bg_set_heat(0, heater, heaters, 215)
        assert heater.target == 215

    def test_release_switches_off_our_own_target(self, bg, heater, heaters):
        bg._bg_swap_begin(0, heater)
        bg._bg_set_heat(0, heater, heaters, 215)
        bg._bg_release_heat(0, heater, heaters)
        assert heater.target == 0

    def test_release_leaves_a_target_the_print_set(self, bg, heater, heaters):
        """The incident: the arrival M109 S200 landed while the swap held
        215; zeroing it left M109 waiting for ever."""
        bg._bg_swap_begin(0, heater)
        bg._bg_set_heat(0, heater, heaters, 215)
        heater.target = 200.0          # the print's M109 S200 T0
        bg._bg_release_heat(0, heater, heaters)
        assert heater.target == 200.0
        assert any("print owns" in m for m in bg.said)

    def test_release_after_the_swaps_own_zeroing_still_leaves_the_print(
            self, bg, heater, heaters):
        bg._bg_swap_begin(0, heater)
        bg._bg_set_heat(0, heater, heaters, 215)
        bg._bg_release_heat(0, heater, heaters)        # unload half done
        heater.target = 200.0                          # print takes over
        bg._bg_release_heat(0, heater, heaters)        # load half fails
        assert heater.target == 200.0

    def test_a_standby_target_is_not_a_takeover(self, bg, heater, heaters):
        bg._bg_swap_begin(0, heater)
        bg._bg_set_heat(0, heater, heaters, 215)
        heater.target = 90.0           # slicer standby cooldown
        bg._check_heater_yield(0, heater)              # must not raise
        bg._bg_set_heat(0, heater, heaters, 215)       # and we re-assert
        assert heater.target == 215

    def test_a_printing_target_makes_the_swap_step_aside(
            self, bg_module, bg, heater, heaters):
        bg._bg_swap_begin(0, heater)
        bg._bg_set_heat(0, heater, heaters, 215)
        heater.target = 200.0
        with pytest.raises(bg_module.ArrivalImminent):
            bg._check_heater_yield(0, heater)
        with pytest.raises(bg_module.ArrivalImminent):
            bg._bg_set_heat(0, heater, heaters, 215)
        assert heater.target == 200.0                  # never overridden

    def test_a_parked_head_that_started_hot_is_not_a_takeover(
            self, bg, heaters):
        """The head can keep a printing temperature of its own before the
        swap starts; that is the baseline, not the print arriving."""
        hot = FakeHeater(200.0)
        bg._bg_swap_begin(0, hot)
        bg._check_heater_yield(0, hot)                 # must not raise
        bg._bg_set_heat(0, hot, heaters, 215)
        assert hot.target == 215

    def test_a_hot_target_after_our_own_release_is_the_print(
            self, bg_module, bg, heater, heaters):
        bg._bg_swap_begin(0, heater)                   # baseline 50
        bg._bg_set_heat(0, heater, heaters, 215)
        bg._bg_release_heat(0, heater, heaters)        # unload done -> 0
        heater.target = 200.0                          # print arrives
        with pytest.raises(bg_module.ArrivalImminent):
            bg._bg_set_heat(0, heater, heaters, 215)   # the load half

    def test_swap_end_forgets_everything(self, bg, heater, heaters):
        bg._bg_swap_begin(0, heater)
        bg._bg_set_heat(0, heater, heaters, 215)
        bg._bg_swap_end(0)
        assert bg._bg_heat == {} and bg._bg_baseline == {}


class TestRunSwapFailure:
    """_run_swap end to end, with the two halves stubbed."""

    def _wire(self, bg, heater, heaters, load):
        ace = types.SimpleNamespace()
        ext = types.SimpleNamespace(get_heater=lambda: heater)
        objs = {"ace": ace, "toolhead": object(), "heaters": heaters}
        bg.printer = types.SimpleNamespace(
            lookup_object=lambda name, default=None: objs.get(name, default))
        bg._ext_name = lambda head: "extruder"
        bg.load_enabled = True
        bg._ace_quiesce = lambda *a, **k: None
        bg._dwell_fan = lambda *a, **k: None
        bg._unload_core = lambda *a, **k: None
        bg._load_core = load
        return ext, ace, objs

    def test_a_failed_load_does_not_zero_a_heater_the_print_owns(
            self, bg_module, bg, heater, heaters):
        def failing_load(head, ace, toolhead, ext, htr, pheaters, *a, **k):
            # the swap heats, then the print's M109 S200 lands, then the
            # feed is not confirmed
            bg._bg_set_heat(head, htr, pheaters, 215)
            htr.target = 200.0
            raise RuntimeError("feed not confirmed by the device")

        ext, ace, objs = self._wire(bg, heater, heaters, failing_load)
        bg.printer.lookup_object = lambda name, default=None: (
            ext if name == "extruder" else objs.get(name, default))
        bg._run_swap(0, None, (0, 3), 215, False, 10.0)
        assert heater.target == 200.0
        assert bg.state[0].startswith("FAILED")
        assert 0 not in bg._busy
        assert bg._bg_heat == {} and bg._bg_baseline == {}

    def test_a_failed_load_still_cools_its_own_heating(
            self, bg_module, bg, heater, heaters):
        def failing_load(head, ace, toolhead, ext, htr, pheaters, *a, **k):
            bg._bg_set_heat(head, htr, pheaters, 215)
            raise RuntimeError("feed not confirmed by the device")

        ext, ace, objs = self._wire(bg, heater, heaters, failing_load)
        bg.printer.lookup_object = lambda name, default=None: (
            ext if name == "extruder" else objs.get(name, default))
        bg._run_swap(0, None, (0, 3), 215, False, 10.0)
        assert heater.target == 0.0

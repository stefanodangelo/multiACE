"""The print-hang watchdog: alert, cool down, emergency stop.

A print whose file position stops moving while print_stats says "printing"
is the one failure the rest of the engine cannot see - the blocked command
holds the gcode mutex, so even a pause request queues behind it. This is the
safety net for whatever the next hang turns out to be (the first one was an
M109 waiting on a heater target that had been zeroed behind its back).
"""
import types

import pytest

from .test_load_retry import ace_module  # noqa: F401


class FakeHeater:
    def __init__(self):
        self.target = 200.0


class FakeHeaters:
    def __init__(self):
        self.heaters = {}

    def set_temperature(self, heater, temp):
        heater.target = temp


class FakeFan:
    def __init__(self):
        self.speed = 1.0

    def set_speed_from_command(self, v):
        self.speed = v


class FakePrinter:
    def __init__(self):
        self.objs = {}
        self.shutdown = None

    def lookup_object(self, name, default=None):
        return self.objs.get(name, default)

    def invoke_shutdown(self, msg):
        self.shutdown = msg


@pytest.fixture
def wd(ace_module):
    a = ace_module.MultiAce.__new__(ace_module.MultiAce)
    a.printer = FakePrinter()
    a.reactor = types.SimpleNamespace(monotonic=lambda: 0.0)
    a.print_stall_alert_min = 15.
    a.print_stall_cool_min = 30.
    a.print_stall_stop_min = 60.
    a._phw_pos = None
    a._phw_since = None
    a._phw_stage = 0
    a._swap_in_progress = False
    a.errors = []
    a.events = []
    a.log_error = lambda m: a.errors.append(m)
    a._ace_event = lambda name, **kw: a.events.append((name, kw))
    a._disp = lambda h: h + 1
    a.ps = types.SimpleNamespace(state='printing')
    a.sd = types.SimpleNamespace(
        get_status=lambda t: {'file_position': a.pos})
    a.pos = 1000
    a.heaters = FakeHeaters()
    a.hot = {n: FakeHeater() for n in
             ('extruder', 'extruder1', 'extruder2', 'extruder3',
              'heater_bed')}
    a.fans = {n: types.SimpleNamespace(fan=FakeFan())
              for n in ('fan', 'fan_generic e0_fan')}
    objs = a.printer.objs
    objs['print_stats'] = a.ps
    objs['virtual_sdcard'] = a.sd
    objs['heaters'] = a.heaters
    a.heaters.heaters.update(a.hot)
    objs.update(a.fans)
    return a


def minutes(m):
    return m * 60.0


class TestStages:
    def test_progress_keeps_the_clock_at_zero(self, wd):
        for i in range(10):
            wd.pos += 100
            assert wd._print_hang_check(minutes(i * 20)) == 0
        assert wd.errors == []

    def test_no_alert_before_the_threshold(self, wd):
        wd._print_hang_check(0)
        assert wd._print_hang_check(minutes(14)) == 0

    def test_alert_at_the_first_threshold_once(self, wd):
        wd._print_hang_check(0)
        assert wd._print_hang_check(minutes(15)) == 1
        assert wd._print_hang_check(minutes(16)) == 0
        assert len(wd.errors) == 1
        assert "stalled 15 min at file position 1000" in wd.errors[0]
        assert wd.events[0][0] == 'print_stall'
        assert wd.printer.shutdown is None

    def test_alert_names_what_the_engine_was_doing(self, wd):
        wd.printer.objs['ace_bg_swap'] = types.SimpleNamespace(
            state={0: 'FAILED:feed not confirmed by the device', 1: 'DONE'})
        wd._print_hang_check(0)
        wd._print_hang_check(minutes(15))
        assert "bg head 1: FAILED:feed not confirmed" in wd.errors[0]

    def test_cooldown_switches_off_heaters_and_fans(self, wd):
        wd._print_hang_check(0)
        wd._print_hang_check(minutes(15))
        assert wd._print_hang_check(minutes(30)) == 2
        assert all(h.target == 0 for h in wd.hot.values())
        assert all(f.fan.speed == 0 for f in wd.fans.values())
        assert wd.printer.shutdown is None

    def test_emergency_stop_at_the_last_threshold(self, wd):
        wd._print_hang_check(0)
        assert wd._print_hang_check(minutes(60)) == 3
        assert "emergency stop" in wd.printer.shutdown
        assert all(h.target == 0 for h in wd.hot.values())

    def test_progress_after_an_alert_rearms_everything(self, wd):
        wd._print_hang_check(0)
        wd._print_hang_check(minutes(20))
        wd.pos += 1
        assert wd._print_hang_check(minutes(21)) == 0
        assert wd._print_hang_check(minutes(21 + 15)) == 1
        assert len(wd.errors) == 2

    def test_a_paused_print_never_trips_it(self, wd):
        wd._print_hang_check(0)
        wd.ps.state = 'paused'
        assert wd._print_hang_check(minutes(120)) == 0
        wd.ps.state = 'printing'
        assert wd._print_hang_check(minutes(121)) == 0   # clock restarted
        assert wd.printer.shutdown is None

    def test_a_zero_stage_threshold_disables_that_stage(self, wd):
        wd.print_stall_stop_min = 0
        wd.print_stall_cool_min = 0
        wd._print_hang_check(0)
        assert wd._print_hang_check(minutes(600)) == 1
        assert wd.printer.shutdown is None
        assert all(h.target == 200.0 for h in wd.hot.values())

    def test_no_virtual_sdcard_means_nothing_to_watch(self, wd):
        wd.printer.objs.pop('virtual_sdcard')
        assert wd._print_hang_check(minutes(600)) == 0

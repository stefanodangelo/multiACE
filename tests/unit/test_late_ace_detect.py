"""An ACE powered on *after* the printer (plan §7).

Before this, a short startup scan set a flag, logged "FIRMWARE_RESTART
required" and returned - leaving the one code path that handles a
late-joining unit (`_refresh_ace_devices`) permanently disabled, because
it is guarded by `_ace_canonical is not None` and startup never got that
far. What the tests pin down:

  * a short count is survivable: no exception, a live degraded object,
    and a 'waiting' state the dashboard can render;
  * the rescan completes startup exactly once, through the *same*
    `_complete_startup()` the normal path uses;
  * indices are only locked at the full expected count, because index
    identity comes from the sorted USB path - locking early mismaps slots;
  * the rescan never runs while the USB bus is busy with a swap.
"""
import types

import pytest

from .test_load_retry import ace_module  # noqa: F401  (module fixture)


class FakeReactor:
    NEVER = float("inf")

    def __init__(self):
        self.now = 100.0
        self.timers = {}
        self._next = 1

    def monotonic(self):
        return self.now

    def pause(self, until):
        self.now = max(self.now, until)

    def register_timer(self, callback, waketime=None):
        handle = self._next
        self._next += 1
        self.timers[handle] = callback
        return handle

    def unregister_timer(self, handle):
        self.timers.pop(handle, None)


@pytest.fixture
def ace(ace_module):
    """A MultiAce with only the fields the startup helpers touch."""
    a = ace_module.MultiAce.__new__(ace_module.MultiAce)
    a.reactor = FakeReactor()
    a.ace_device_count = 1
    a.ace_rescan_interval = 5.0
    a._ace_rescan_timer = None
    a._ace_rescan_started = None
    a._startup_completed = False
    a._ace_startup_state = "init"
    a._ace_startup_found = 0
    a._ace_startup_expected = 0
    a._ace_startup_failed = False
    a._ace_canonical = None
    a._ace_present = set()
    a._ace_devices = []
    a._active_device_index = 0
    a.serial_id = ""
    a._auto_feed_enabled = False
    a._swap_in_progress = False
    a._homing_active = False
    a._usb_log = types.SimpleNamespace(info=lambda *args, **kw: None)

    a.present = []          # what the fake USB scan reports
    a.opened = []           # every _open_ace call
    a.active_set = []       # every _set_active_idx call
    a.messages = []

    a._scan_ace_devices = lambda ctx: list(a.present)
    a._open_ace = lambda idx: (a.opened.append(idx) or True)
    a._set_active_idx = lambda idx: a.active_set.append(idx)
    a._disp = lambda i: i
    a._t = lambda key, **kw: "%s %s" % (key, sorted(kw.items()))
    a.log_always = lambda msg: a.messages.append(msg)
    a.log_error = lambda msg: a.messages.append(msg)
    a.save_variables = types.SimpleNamespace(allVariables={})
    a.VARS_ACE_ACTIVE_DEVICE = "ace__active_device"
    return a


class FakeGcmd:
    def __init__(self, **params):
        self.params = params
        self.replies = []

    def get_int(self, name, default=None, minval=None, maxval=None):
        return self.params.get(name, default)

    def respond_info(self, msg):
        self.replies.append(msg)


class TestStartupWait:
    def test_short_count_is_degraded_not_fatal(self, ace):
        ace._enter_startup_wait(1)
        assert ace._ace_startup_state == "waiting"
        assert ace.ace_startup_status() == {
            "state": "waiting", "found": 0, "expected": 1}

    def test_waiting_never_locks_the_canonical_mapping(self, ace):
        """Locking with 1 of 2 units up makes the late arrival append at
        the end, so index != physical order and slots silently mismap."""
        ace._enter_startup_wait(1)
        assert ace._ace_canonical is None

    def test_a_rescan_timer_is_registered(self, ace):
        ace._enter_startup_wait(1)
        assert ace._ace_rescan_timer in ace.reactor.timers

    def test_interval_zero_registers_no_timer(self, ace):
        ace.ace_rescan_interval = 0.0
        ace._enter_startup_wait(1)
        assert ace._ace_rescan_timer is None
        assert ace.reactor.timers == {}

    def test_the_message_no_longer_demands_a_firmware_restart(self, ace_module):
        import json
        from pathlib import Path
        root = Path(__file__).resolve().parents[2] / "multiace" / "i18n"
        for lang in ("en", "de", "zh"):
            msgs = json.loads((root / ("%s.json" % lang)).read_text("utf-8"))
            text = msgs["msg"]["usb_unstable"]
            assert "FIRMWARE_RESTART required" not in text
            assert "ACE_RESCAN" in text
            assert "late_ace_connected" in msgs["msg"]


class TestRescanCompletesStartup:
    def test_a_later_scan_completes_startup_once(self, ace):
        ace._enter_startup_wait(1)
        tick = ace.reactor.timers[ace._ace_rescan_timer]

        # Nothing there yet: still waiting, timer stays armed.
        assert tick(ace.reactor.monotonic()) != ace.reactor.NEVER
        assert ace._startup_completed is False

        ace.present = ["/dev/serial/by-path/usb-0:1.1"]
        assert tick(ace.reactor.monotonic()) == ace.reactor.NEVER
        assert ace._startup_completed is True
        assert ace._ace_startup_state == "ready"
        assert ace.opened == [0]
        assert ace.active_set == [0]
        assert ace._ace_canonical == ace.present

    def test_completion_unregisters_the_timer(self, ace):
        ace._enter_startup_wait(1)
        handle = ace._ace_rescan_timer
        ace.present = ["/dev/a"]
        ace.reactor.timers[handle](ace.reactor.monotonic())
        assert handle not in ace.reactor.timers
        assert ace._ace_rescan_timer is None

    def test_startup_completes_only_once(self, ace):
        ace._enter_startup_wait(1)
        ace.present = ["/dev/a"]
        assert ace.try_complete_startup() is True
        assert ace.try_complete_startup() is False
        assert ace.opened == [0]

    def test_startup_failed_flag_is_cleared_on_recovery(self, ace):
        ace._ace_startup_failed = True
        ace._enter_startup_wait(1)
        ace.present = ["/dev/a"]
        ace.try_complete_startup()
        assert ace._ace_startup_failed is False

    def test_one_of_two_never_completes(self, ace):
        ace.ace_device_count = 2
        ace._enter_startup_wait(2)
        ace.present = ["/dev/a"]
        assert ace.try_complete_startup() is False
        assert ace._ace_canonical is None
        assert ace.opened == []
        assert ace.ace_startup_status() == {
            "state": "waiting", "found": 1, "expected": 2}

    def test_two_of_two_completes(self, ace):
        ace.ace_device_count = 2
        ace._enter_startup_wait(2)
        ace.present = ["/dev/a", "/dev/b"]
        assert ace.try_complete_startup() is True
        assert ace.opened == [0, 1]


class TestRescanCommand:
    def test_rescan_reports_still_waiting(self, ace):
        ace._enter_startup_wait(1)
        cmd = FakeGcmd()
        ace.cmd_ACE_RESCAN(cmd)
        assert "still waiting" in cmd.replies[0]
        assert ace._startup_completed is False

    def test_rescan_completes_when_the_unit_arrived(self, ace):
        ace._enter_startup_wait(1)
        ace.present = ["/dev/a"]
        cmd = FakeGcmd()
        ace.cmd_ACE_RESCAN(cmd)
        assert ace._startup_completed is True
        assert "no restart needed" in cmd.replies[0]

    def test_lock_1_completes_at_one_of_two(self, ace):
        """Deliberate, explicit, human-typed - never automatic."""
        ace.ace_device_count = 2
        ace._enter_startup_wait(2)
        ace.present = ["/dev/a"]
        ace.cmd_ACE_RESCAN(FakeGcmd(LOCK=1))
        assert ace._startup_completed is True
        assert ace._ace_canonical == ["/dev/a"]

    def test_lock_1_with_nothing_present_still_refuses(self, ace):
        ace._enter_startup_wait(1)
        ace.cmd_ACE_RESCAN(FakeGcmd(LOCK=1))
        assert ace._startup_completed is False

    def test_rescan_after_completion_is_a_no_op_report(self, ace):
        ace.present = ["/dev/a"]
        ace.try_complete_startup()
        cmd = FakeGcmd()
        ace.cmd_ACE_RESCAN(cmd)
        assert "already complete" in cmd.replies[0]


class TestRescanIsGatedOnABusyBus:
    """An _open_ace in the middle of a swap is the one way this feature
    could break a running print."""

    @pytest.mark.parametrize("flag", ["_swap_in_progress",
                                      "_auto_feed_enabled",
                                      "_homing_active"])
    def test_no_rescan_while_busy(self, ace, flag):
        ace._enter_startup_wait(1)
        ace.present = ["/dev/a"]
        setattr(ace, flag, True)
        tick = ace.reactor.timers[ace._ace_rescan_timer]
        assert tick(ace.reactor.monotonic()) != ace.reactor.NEVER
        assert ace._startup_completed is False
        assert ace.opened == []

        setattr(ace, flag, False)
        assert tick(ace.reactor.monotonic()) == ace.reactor.NEVER
        assert ace._startup_completed is True

    def test_a_scan_failure_does_not_kill_the_timer(self, ace):
        ace._enter_startup_wait(1)

        def boom(ctx):
            raise RuntimeError("udev is having a day")

        ace._scan_ace_devices = boom
        tick = ace.reactor.timers[ace._ace_rescan_timer]
        assert tick(ace.reactor.monotonic()) != ace.reactor.NEVER

    def test_the_interval_backs_off_after_a_day(self, ace):
        ace._enter_startup_wait(1)
        assert ace._rescan_interval_now() == pytest.approx(5.0)
        ace.reactor.now += 86401.0
        assert ace._rescan_interval_now() == pytest.approx(60.0)


class TestNormalPathSharesTheSameCode:
    """A pure extraction: if the normal full-count startup stopped routing
    through _complete_startup(), the deferred path would be a second
    implementation free to drift."""

    def test_full_count_startup_opens_and_activates(self, ace):
        ace.present = ["/dev/a"]
        ace._refresh_ace_devices("startup")
        ace._lock_canonical(1)
        ace._complete_startup()
        assert ace.opened == [0]
        assert ace.active_set == [0]
        assert ace._startup_completed is True

    def test_complete_startup_restores_the_saved_active_device(self, ace):
        ace.present = ["/dev/a", "/dev/b"]
        ace.save_variables.allVariables["ace__active_device"] = "/dev/b"
        ace._refresh_ace_devices("startup")
        ace._lock_canonical(2)
        ace._complete_startup()
        assert ace._active_device_index == 1
        assert ace.serial_id == "/dev/b"

    def test_no_devices_and_no_serial_does_not_complete(self, ace):
        assert not ace._complete_startup()
        assert ace._startup_completed is False

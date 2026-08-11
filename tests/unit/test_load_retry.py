"""Automatic retry of a failed toolhead load (ace.py side).

The retry runs inside Klipper's load command, so the UI cannot reach it
with G-code - it talks through two files instead. Those files ARE the
contract with the web backend, so this is what the tests pin down:

  * the state file says which attempt is in flight,
  * the control file lets the user skip the wait or give up,
  * the state file is removed when the sequence ends (a stale one would
    leave a "retrying…" banner on screen for ever).
"""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

EXTRAS = Path(__file__).resolve().parents[2] / "multiace" / "klipper" / "extras"


@pytest.fixture(scope="module")
def ace_module():
    """Import klippy/extras/ace.py outside Klipper.

    It is not an importable package in this repo (Klipper installs the
    files flat), so build a throwaway package around it and stub the one
    third-party import it does at module level.
    """
    sys.modules.setdefault("serial", types.ModuleType("serial"))
    sys.modules["serial"].SerialException = type("SerialException", (Exception,), {})
    sys.modules["serial"].Serial = object

    pkg = types.ModuleType("_ace_extras")
    pkg.__path__ = [str(EXTRAS)]
    sys.modules["_ace_extras"] = pkg
    spec = importlib.util.spec_from_file_location("_ace_extras.ace",
                                                  EXTRAS / "ace.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_ace_extras.ace"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeReactor:
    """Klipper's reactor, reduced to the two calls _retry_wait makes."""

    def __init__(self):
        self.now = 0.0
        self.pauses = []

    def monotonic(self):
        return self.now

    def pause(self, until):
        self.pauses.append(until - self.now)
        self.now = until


@pytest.fixture
def ace(ace_module, tmp_path):
    """A MultiAce with only the fields the retry helpers touch - the real
    __init__ needs a whole printer."""
    obj = ace_module.MultiAce.__new__(ace_module.MultiAce)
    obj.reactor = FakeReactor()
    obj._retry_state_path = str(tmp_path / "retry_state.json")
    obj._retry_control_path = str(tmp_path / "retry_control")
    obj.filament_load_max_auto_retries = 3
    obj.filament_load_retry_delay_ms = 1000
    obj.head_auto_retries = {0: 3, 1: 3, 2: 3, 3: 3}
    return obj


def read_state(ace):
    return json.loads(Path(ace._retry_state_path).read_text())


class TestRetryConfig:
    def test_defaults_come_from_the_global_setting(self, ace):
        assert ace._auto_retries_for(0) == 3

    def test_per_head_override_wins(self, ace):
        ace.head_auto_retries[1] = 7
        assert ace._auto_retries_for(1) == 7
        assert ace._auto_retries_for(0) == 3

    def test_missing_head_falls_back_to_the_global(self, ace):
        ace.head_auto_retries = {}
        assert ace._auto_retries_for(2) == 3


class TestRetryStateFile:
    def test_publish_writes_the_attempt_counter(self, ace):
        ace._retry_state_publish(0, 1, 2, attempt=2, max_attempts=3,
                                 reason="load_not_finished", next_retry_ms=800)
        st = read_state(ace)
        assert st["active"] is True
        assert (st["head"], st["ace"], st["slot"]) == (0, 1, 2)
        assert st["attempt"] == 2
        assert st["max_attempts"] == 3
        assert st["next_retry_ms"] == 800
        assert st["reason"] == "load_not_finished"
        assert st["ts"] > 0

    def test_clear_removes_the_file(self, ace):
        ace._retry_state_publish(0, 0, 0, 1, 3, "x", 100)
        ace._retry_state_clear()
        assert not Path(ace._retry_state_path).exists()

    def test_clear_is_safe_when_nothing_was_written(self, ace):
        ace._retry_state_clear()  # must not raise

    def test_write_failure_never_propagates(self, ace):
        """A read-only /tmp must not turn a recoverable jam into a crash
        inside the load command."""
        ace._retry_state_path = str(Path(ace._retry_state_path) / "nope" / "x.json")
        ace._retry_state_publish(0, 0, 0, 1, 3, "x", 100)


class TestRetryControlFile:
    def test_no_file_means_no_wish(self, ace):
        assert ace._retry_control_take() is None

    @pytest.mark.parametrize("word", ["now", "cancel", "NOW", " cancel\n"])
    def test_known_words_are_read_and_consumed(self, ace, word):
        Path(ace._retry_control_path).write_text(word)
        assert ace._retry_control_take() == word.strip().lower()
        # Consumed: a leftover would steer the NEXT retry sequence too.
        assert not Path(ace._retry_control_path).exists()

    def test_junk_is_ignored(self, ace):
        Path(ace._retry_control_path).write_text("reboot-the-printer")
        assert ace._retry_control_take() is None


class TestRetryWait:
    def test_waits_the_configured_delay(self, ace):
        assert ace._retry_wait(1000, 0, 0, 0, 1, 3, "jam") is None
        assert pytest.approx(sum(ace.reactor.pauses), abs=1e-6) == 1.0

    def test_publishes_a_countdown_while_waiting(self, ace):
        ace._retry_wait(300, 0, 0, 0, 1, 3, "jam")
        # Last publish before returning is the one that saw 0 remaining.
        assert read_state(ace)["next_retry_ms"] == 0

    def test_zero_delay_returns_immediately(self, ace):
        assert ace._retry_wait(0, 0, 0, 0, 1, 3, "jam") is None
        assert ace.reactor.pauses == []

    def test_retry_now_skips_the_remaining_delay(self, ace):
        Path(ace._retry_control_path).write_text("now")
        assert ace._retry_wait(5000, 0, 0, 0, 1, 3, "jam") is None
        assert ace.reactor.pauses == []

    def test_cancel_stops_the_sequence(self, ace):
        Path(ace._retry_control_path).write_text("cancel")
        assert ace._retry_wait(5000, 0, 0, 0, 1, 3, "jam") == "cancel"

    def test_cancel_arriving_mid_wait_is_honoured(self, ace):
        """The user hits "stop" while the countdown is running - the wait
        must break out, not run to completion."""
        ctl = Path(ace._retry_control_path)
        real_pause = ace.reactor.pause
        state = {"n": 0}

        def pause(until):
            state["n"] += 1
            if state["n"] == 3:
                ctl.write_text("cancel")
            real_pause(until)

        ace.reactor.pause = pause
        assert ace._retry_wait(5000, 0, 0, 0, 1, 3, "jam") == "cancel"
        assert sum(ace.reactor.pauses) < 5.0


class TestFeedChannelReset:
    """Every attempt has to start from a channel that FEED_AUTO will
    accept; without the reset a second attempt is refused because
    load_finish is still False from the failed one."""

    def _ff(self):
        return types.SimpleNamespace(
            channel_state=["loading", "idle"],
            config={"load_finish": [True, True], "auto_mode": [True, True]},
        )

    def test_resets_state_and_load_finish(self, ace):
        ff = self._ff()
        ace._reset_feed_channel(ff, "filament_feed left", 0)
        assert ff.channel_state[0] == "inited"
        assert ff.config["load_finish"][0] is False
        # Untouched neighbour: one head's retry must not disturb another.
        assert ff.channel_state[1] == "idle"
        assert ff.config["load_finish"][1] is True

    def test_missing_module_is_not_an_error(self, ace):
        ace._reset_feed_channel(None, "filament_feed left", 0)

    def test_out_of_range_channel_is_not_an_error(self, ace):
        ace._reset_feed_channel(self._ff(), "filament_feed left", 9)


class GcodeError(Exception):
    """Stand-in for gcmd.error - Klipper's is a CommandError subclass."""

    def __init__(self, message="", **kw):
        super().__init__(message)
        self.kw = kw


class FakeGcmd:
    def __init__(self, **params):
        self.params = params

    def get_int(self, name, default=None, minval=None, maxval=None):
        return self.params.get(name, default)

    def get(self, name, default=None):
        return self.params.get(name, default)

    def error(self, message="", **kw):
        return GcodeError(message, **kw)


@pytest.fixture
def loader(ace_module, ace):
    """A MultiAce wired up just far enough to run cmd_ACE_LOAD_HEAD.

    `feed_results` scripts what FEED_AUTO does on each attempt: "ok" or
    "raise". Everything else is the smallest stub that keeps the command
    on its normal path.
    """
    a = ace
    mod = ace_module
    a.feed_results = []
    a.feed_calls = []
    a._last_load_ok = True
    a._ace_mode = "multi"
    a._active_device_index = 0
    a._head_source = {}
    a._ghost_heads = set()
    a._bg_staged = {}
    a._bg_left_empty = set()
    a.gate_status = [mod.GATE_AVAILABLE] * 4
    a.head_manual = {i: False for i in range(4)}
    a._info = {"slots": [{"rfid": 1, "type": "PLA", "color": (1, 2, 3),
                          "brand": "Generic", "subtype": ""} for _ in range(4)]}
    a.audits = []
    a.events = []
    a.messages = []

    ff = types.SimpleNamespace(
        channel_state=["idle", "idle"],
        config={"load_finish": [False, False], "auto_mode": [True, True]},
    )
    a.ff = ff

    class FakeGcode:
        def run_script_from_command(self, script):
            if script.startswith("FEED_AUTO"):
                a.feed_calls.append(script)
                outcome = (a.feed_results.pop(0) if a.feed_results else "ok")
                if outcome == "raise":
                    raise RuntimeError("feed_auto exploded")
                ff.config["load_finish"][0] = (outcome == "ok")
                if outcome == "ok":
                    ff.channel_state[0] = "load_finish"

    a.gcode = FakeGcode()
    a.toolhead = types.SimpleNamespace(
        get_extruder=lambda: types.SimpleNamespace(get_name=lambda: "extruder"),
        wait_moves=lambda: None)
    a.printer = types.SimpleNamespace(
        lookup_object=lambda name, default="_raise":
            ff if name == "filament_feed left" else default)
    a.EXTRUDER_MAP = {0: ("left", 0), 1: ("left", 1),
                      2: ("right", 0), 3: ("right", 1)}

    a.head_is_manual = lambda h: False
    a.head_uses_ace = lambda h: True
    a._wait_bg_op = lambda h, g: None
    a._ensure_ace_available = lambda i: True
    a._save_head_source = lambda: None
    a._read_wheel_counts = lambda m, c: 0
    a._refresh_filament_exist_flags = lambda: None
    a._expect_ptc_push = lambda *args, **kw: None
    a._override_for = lambda ace_idx, slot: None
    a.rgb2hex = lambda *rgb: "000000"
    a._disp = lambda i: i
    a._t = lambda key, **kw: key
    a.log_always = lambda msg: a.messages.append(msg)
    a._audit_state = lambda name, data: a.audits.append((name, data))
    a._ace_event = lambda name, **kw: a.events.append((name, kw))
    a._is_actively_printing = lambda: False
    a.filament_load_retry_delay_ms = 0
    return a


def run_load(a, **params):
    return type(a).cmd_ACE_LOAD_HEAD(a, FakeGcmd(HEAD=0, **params))


class TestLoadRetryLoop:
    def test_a_clean_load_makes_one_attempt(self, loader):
        run_load(loader)
        assert len(loader.feed_calls) == 1
        assert loader._last_load_ok is True

    def test_retries_until_it_succeeds(self, loader):
        """The whole point: a jam that clears on the second try must not
        stop the print, and must not keep retrying afterwards."""
        loader.feed_results = ["fail", "ok"]
        run_load(loader)
        assert len(loader.feed_calls) == 2
        assert loader._last_load_ok is True
        assert not Path(loader._retry_state_path).exists()
        assert any(name == "LOAD_HEAD_RETRY_OK"
                   for name, _ in loader.audits)

    def test_stops_after_the_configured_number_of_retries(self, loader):
        """3 retries = 4 attempts total, then the failure is real."""
        loader.feed_results = ["fail"] * 9
        with pytest.raises(GcodeError):
            run_load(loader)
        assert len(loader.feed_calls) == 4
        assert loader._last_load_ok is False

    @pytest.mark.parametrize("retries,expected_attempts",
                             [(0, 1), (1, 2), (5, 6)])
    def test_attempt_count_follows_the_config(self, loader, retries,
                                              expected_attempts):
        loader.head_auto_retries[0] = retries
        loader.feed_results = ["fail"] * 12
        with pytest.raises(GcodeError):
            run_load(loader)
        assert len(loader.feed_calls) == expected_attempts

    def test_retries_survive_a_thrown_feed_auto(self, loader):
        loader.feed_results = ["raise", "ok"]
        run_load(loader)
        assert len(loader.feed_calls) == 2

    def test_auto_mode_refusal_is_not_retried(self, loader):
        """auto_mode off is a configuration fact, not a jam - retrying it
        would just burn four attempts to reach the same message."""
        loader.ff.config["auto_mode"][0] = False
        loader.feed_results = ["fail"] * 4
        with pytest.raises(GcodeError):
            run_load(loader)
        assert len(loader.feed_calls) == 1

    def test_user_cancel_stops_retrying(self, loader):
        loader.feed_results = ["fail"] * 4
        Path(loader._retry_control_path).write_text("cancel")
        with pytest.raises(GcodeError):
            run_load(loader)
        assert len(loader.feed_calls) == 1

    def test_state_file_is_cleared_on_final_failure(self, loader):
        """A leftover state file would leave "retrying…" on the dashboard
        long after the load gave up."""
        loader.feed_results = ["fail"] * 9
        with pytest.raises(GcodeError):
            run_load(loader)
        assert not Path(loader._retry_state_path).exists()

    def test_each_attempt_resets_the_feed_channel(self, loader):
        loader.feed_results = ["fail", "fail", "ok"]
        run_load(loader)
        assert len(loader.feed_calls) == 3

    def test_retry_events_are_emitted_for_external_listeners(self, loader):
        loader.feed_results = ["fail", "ok"]
        run_load(loader)
        names = [n for n, _ in loader.events]
        assert "load_retry" in names

    def test_exhausted_retries_pause_a_running_print(self, loader):
        """Mid-print the recoverable outcome is a pause, not an abort."""
        paused = []
        loader._is_actively_printing = lambda: True
        loader._pause_for_recovery = lambda gcmd, msg, steps, code=0: \
            paused.append((msg, steps))
        loader.feed_results = ["fail"] * 9
        with pytest.raises(GcodeError):
            run_load(loader)
        assert paused, "an exhausted retry during a print must pause"
        assert "4 attempts" in paused[0][0]

    def test_retries_disabled_behaves_like_before(self, loader):
        """filament_load_max_auto_retries: 0 must reproduce the old
        fail-immediately path, including no pause."""
        paused = []
        loader.head_auto_retries[0] = 0
        loader._is_actively_printing = lambda: True
        loader._pause_for_recovery = lambda *a, **k: paused.append(a)
        loader.feed_results = ["fail"]
        with pytest.raises(GcodeError):
            run_load(loader)
        assert len(loader.feed_calls) == 1
        assert paused == []


class TestFirmwareCompatMirror:
    def test_unknown_version_is_untested_not_a_crash(self, ace, caplog):
        ace.firmware_version = "9.9.9"
        ace.log_always = lambda msg: None
        ace._log_firmware_compat()

    def test_no_version_configured_is_fine(self, ace):
        ace.firmware_version = ""
        ace._log_firmware_compat()

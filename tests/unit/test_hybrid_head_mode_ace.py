"""Hybrid per-head mode: the ace.py side (config accessors, state helpers,
and the ACE_SET_HEAD_FEEDER_COMBO guard rails).

Uses the same throwaway-import fixture as test_load_retry.py so this runs
without a real Klipper environment: MultiAce.__new__ bypasses __init__ and
each test wires up only the attributes the method under test touches.
"""
import types

import pytest

from .test_load_retry import ace_module, FakeSensor  # noqa: F401  (module fixture)


@pytest.fixture
def ace(ace_module):
    obj = ace_module.MultiAce.__new__(ace_module.MultiAce)
    obj._ace_mode = 'head'
    obj.head_manual = {0: False, 1: False, 2: False, 3: False}
    obj.head_feeder = {0: False, 1: True, 2: True, 3: True}
    obj.head_feeder_combo = {0: False, 1: False, 2: False, 3: False}
    obj.head_ace = {0: 0, 1: 1, 2: 2, 3: 3}
    obj._head_source = {0: None, 1: None, 2: None, 3: None}
    obj.feeder_load_length = 1100.0
    obj.feeder_retract_length = None
    obj.retract_length = 1950.0
    obj.feeder_swap_retract_length = 150.0
    return obj


class TestHeadHasFeederTap:
    def test_false_outside_head_mode(self, ace):
        ace._ace_mode = 'multi'
        ace.head_feeder_combo[0] = True
        assert ace.head_has_feeder_tap(0) is False

    def test_false_when_not_ace_driven(self, ace):
        # head 1 is a plain feeder head (head_feeder[1] = True)
        ace.head_feeder_combo[1] = True
        assert ace.head_uses_ace(1) is False
        assert ace.head_has_feeder_tap(1) is False

    def test_true_once_enabled_on_an_ace_head(self, ace):
        assert ace.head_uses_ace(0) is True
        assert ace.head_has_feeder_tap(0) is False
        ace.head_feeder_combo[0] = True
        assert ace.head_has_feeder_tap(0) is True


class TestHeadSourceIsFeederTap:
    def test_none_source_is_not_the_feeder_tap(self, ace):
        assert ace.head_source_is_feeder_tap(0) is False

    def test_ace_slot_source_is_not_the_feeder_tap(self, ace):
        ace._head_source[0] = {'ace_index': 0, 'slot': 2}
        assert ace.head_source_is_feeder_tap(0) is False

    def test_sentinel_is_the_feeder_tap(self, ace):
        ace._head_source[0] = dict(ace_module_source_sentinel(ace))
        assert ace.head_source_is_feeder_tap(0) is True


def ace_module_source_sentinel(ace):
    # FEEDER_TAP_SOURCE is a module-level constant in ace.py.
    import sys
    mod = sys.modules[type(ace).__module__]
    return mod.FEEDER_TAP_SOURCE


class TestHeadAceActiveFor:
    """The DYNAMIC predicate filament_feed_ace.py uses instead of
    head_uses_ace for per-operation branching - must reflect the head's
    CURRENT source, not just its static wiring."""

    def test_true_for_a_plain_ace_head(self, ace):
        assert ace.head_ace_active_for(0) is True

    def test_false_for_a_plain_feeder_head(self, ace):
        assert ace.head_ace_active_for(1) is False

    def test_false_for_a_combo_head_currently_on_its_feeder_tap(self, ace):
        ace.head_feeder_combo[0] = True
        ace._head_source[0] = dict(ace_module_source_sentinel(ace))
        assert ace.head_uses_ace(0) is True, (
            "static wiring must stay True - the dashboard/UI still needs "
            "to show this head's ACE slots as belonging to it")
        assert ace.head_ace_active_for(0) is False

    def test_true_for_a_combo_head_currently_on_an_ace_slot(self, ace):
        ace.head_feeder_combo[0] = True
        ace._head_source[0] = {'ace_index': 0, 'slot': 1}
        assert ace.head_ace_active_for(0) is True


class TestHeadIsLoaded:
    """_head_is_loaded is the shared guard behind ACE_SET_HEAD_MANUAL,
    ACE_SET_HEAD_FEEDER, ACE_SET_HEAD_FEEDER_COMBO and ACE_SET_HEAD_ACE.

    A combo head currently sourced from its feeder tap carries the
    FEEDER_TAP_SOURCE sentinel in _head_source - a truthy dict with no ACE
    slot and no load_failed marker. Trusting that sentinel as "loaded"
    (instead of falling back to the toolhead sensor, the way a plain
    feeder/manual head already does) permanently refused
    ACE_SET_HEAD_FEEDER_COMBO ENABLE=0 once a combo head had ever loaded
    through its feeder tap - even after the filament was physically pulled
    out by hand, with no ACE_UNLOAD_HEAD to clear the sentinel."""

    def _wire_sensor(self, ace, head, detected):
        ace.printer = types.SimpleNamespace(
            lookup_object=lambda name, default=None:
                FakeSensor(detected)
                if name == 'filament_motion_sensor e%d_filament' % head
                else default)

    def test_feeder_tap_sentinel_with_no_filament_reads_as_unloaded(self, ace):
        ace.head_feeder_combo[0] = True
        ace._head_source[0] = dict(ace_module_source_sentinel(ace))
        self._wire_sensor(ace, 0, False)
        assert ace._head_is_loaded(0) is False

    def test_feeder_tap_sentinel_with_filament_present_stays_loaded(self, ace):
        ace.head_feeder_combo[0] = True
        ace._head_source[0] = dict(ace_module_source_sentinel(ace))
        self._wire_sensor(ace, 0, True)
        assert ace._head_is_loaded(0) is True

    def test_ace_slot_source_still_counts_as_loaded_without_a_sensor_read(self, ace):
        # An ordinary ACE-slot source (not the feeder-tap sentinel) must
        # keep counting as loaded on its own - this predicate must not
        # start trusting the sensor for every source, only the tap.
        ace._head_source[0] = {'ace_index': 0, 'slot': 1}
        self._wire_sensor(ace, 0, False)
        assert ace._head_is_loaded(0) is True


class _FakeSaveVariables:
    def __init__(self, all_variables):
        self.allVariables = all_variables


class TestRestoreHeadSource:
    """_restore_head_source() runs from _handle_ready() on every Klipper
    boot. A saved feeder-tap sentinel (ace_index=None, slot='feeder') must
    not crash the '%d' logging path - it did, taking Klipper to shutdown
    on every restart with a combo head parked on its feeder tap."""

    def test_ace_slot_source_restores_and_logs(self, ace):
        ace.save_variables = _FakeSaveVariables({
            'ace__head_source': {'0': {'ace_index': 1, 'slot': 2}},
        })
        ace._restore_head_source()
        assert ace._head_source[0] == {'ace_index': 1, 'slot': 2}

    def test_feeder_tap_sentinel_restores_without_crashing(self, ace):
        sentinel = dict(ace_module_source_sentinel(ace))
        ace.save_variables = _FakeSaveVariables({
            'ace__head_source': {'0': sentinel},
        })
        ace._restore_head_source()
        assert ace._head_source[0] == sentinel


class TestFeederLengthAccessors:
    """Global only - no per-head overrides."""

    def test_load_length_is_global(self, ace):
        assert ace.feeder_load_length_for(2) == 1100.0
        assert ace.feeder_load_length_for(0) == 1100.0

    def test_retract_length_falls_back_to_the_ace_retract_length(self, ace):
        # No feeder_retract_length configured at all: compat fallback to
        # retract_length, so an upgrade with no new config changes nothing.
        assert ace.feeder_retract_length_for(0) == 1950.0

    def test_retract_length_global_override_wins_over_compat_fallback(self, ace):
        ace.feeder_retract_length = 150.0
        assert ace.feeder_retract_length_for(0) == 150.0
        assert ace.feeder_retract_length_for(1) == 150.0

    def test_swap_retract_length_is_global(self, ace):
        assert ace.get_feeder_swap_retract_length(0) == 150.0
        assert ace.get_feeder_swap_retract_length(1) == 150.0


class TestSetHeadFeederComboGuardRails:
    """NOTE: combo mode is temporarily disabled - every ENABLE=1 call now
    raises immediately (see test_refuses_to_enable_while_combo_mode_is_disabled)
    before reaching any of the older, more specific guards below. The other
    ENABLE=1 tests here (no-ACE-wiring, head-loaded) still pass, but only
    because the blanket disable short-circuits first - their own guard
    logic is dormant until the kill switch is reverted. Left in place as
    regression coverage for that moment."""

    def _gcmd(self, **params):
        class G:
            def __init__(self, p):
                self._p = p

            def get_int(self, name, default=None, minval=None, maxval=None):
                return int(self._p.get(name, default))

            def error(self, msg):
                return AssertionError(msg)
        return G(params)

    def test_refuses_when_head_has_no_ace_wiring(self, ace, monkeypatch):
        # head 1 is a plain feeder head - not ACE-driven.
        monkeypatch.setattr(ace, '_head_is_loaded', lambda h: False)
        monkeypatch.setattr(ace, '_t', lambda k, **kw: k)
        gcmd = self._gcmd(HEAD=1, ENABLE=1)
        with pytest.raises(AssertionError):
            ace.cmd_ACE_SET_HEAD_FEEDER_COMBO(gcmd)
        assert ace.head_feeder_combo[1] is False

    def test_refuses_when_the_head_is_loaded(self, ace, monkeypatch):
        monkeypatch.setattr(ace, '_head_is_loaded', lambda h: True)
        monkeypatch.setattr(ace, '_t', lambda k, **kw: k)
        gcmd = self._gcmd(HEAD=0, ENABLE=1)
        with pytest.raises(AssertionError):
            ace.cmd_ACE_SET_HEAD_FEEDER_COMBO(gcmd)
        assert ace.head_feeder_combo[0] is False

    def test_refuses_to_enable_while_combo_mode_is_disabled(
            self, ace, monkeypatch):
        """Combo mode is temporarily disabled (not working correctly yet -
        see head_feeder_combo's init comment in ace.py): enabling must be
        refused even for an otherwise-eligible free ACE head. Once the
        underlying spool-tracking gap is fixed and the kill switch comes
        out, this reverts to asserting a successful enable."""
        monkeypatch.setattr(ace, '_head_is_loaded', lambda h: False)
        monkeypatch.setattr(ace, '_t', lambda k, **kw: k)
        ace.save_variables = None
        monkeypatch.setattr(ace, 'log_always', lambda *a, **k: None)
        gcmd = self._gcmd(HEAD=0, ENABLE=1)
        with pytest.raises(AssertionError):
            ace.cmd_ACE_SET_HEAD_FEEDER_COMBO(gcmd)
        assert ace.head_feeder_combo[0] is False

    def test_disables_while_the_head_is_loaded_from_its_own_feeder_tap(
            self, ace, monkeypatch):
        """The reported bug: filament genuinely loaded through the stock
        feeder on a combo head. head_source_is_feeder_tap is that head's
        OWN current source, not a foreign one being orphaned by this call -
        disabling combo must succeed even while _head_is_loaded is True,
        exactly because this is the call that retires that source."""
        ace.head_feeder_combo[0] = True
        ace._head_source[0] = dict(ace_module_source_sentinel(ace))
        monkeypatch.setattr(ace, '_head_is_loaded', lambda h: True)
        monkeypatch.setattr(ace, '_save_head_source', lambda: None)
        ace.save_variables = None
        monkeypatch.setattr(ace, 'log_always', lambda *a, **k: None)
        gcmd = self._gcmd(HEAD=0, ENABLE=0)
        ace.cmd_ACE_SET_HEAD_FEEDER_COMBO(gcmd)
        assert ace.head_feeder_combo[0] is False
        assert ace._head_source[0] is None

    def test_still_refuses_when_loaded_from_an_ace_slot_not_the_tap(
            self, ace, monkeypatch):
        """A genuine ACE-slot source is a different case from the tap
        sentinel: disabling combo while that is active would silently drop
        real routing information, so the tap bypass must not apply here."""
        ace.head_feeder_combo[0] = True
        ace._head_source[0] = {'ace_index': 0, 'slot': 1}
        monkeypatch.setattr(ace, '_head_is_loaded', lambda h: True)
        monkeypatch.setattr(ace, '_t', lambda k, **kw: k)
        gcmd = self._gcmd(HEAD=0, ENABLE=0)
        with pytest.raises(AssertionError):
            ace.cmd_ACE_SET_HEAD_FEEDER_COMBO(gcmd)
        assert ace.head_feeder_combo[0] is True

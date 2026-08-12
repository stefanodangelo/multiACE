"""The purge clamps on the printer side (plan §13.2).

Purge is the one thing multiACE commands the hotend to extrude by a
computed, config-derived volume, and there are exactly two ways that ends a
hotend: overflowing the purge bin (the blob that rips the silicone sock and
takes the thermistor and heater leads with it), and purging faster than the
hotend can melt (which grinds the filament flat and can pop the PTFE
coupler). Both guards live in ace.py rather than in the model, because the
number arriving in a `PURGE=` was computed somewhere else - possibly against
a machine with a different purge bin.
"""
import types

import pytest

from .test_load_retry import ace_module  # noqa: F401  (module fixture)


@pytest.fixture
def ace(ace_module):
    a = ace_module.MultiAce.__new__(ace_module.MultiAce)
    a.swap_purge_length = 0
    a.swap_purge_max = 150
    a.swap_purge_min = 0
    a.purge_bin_capacity_mm = 0
    a.purge_color_aware = False
    a._purge_length_override = None
    a._swap_purge_mm = None
    a.messages = []
    a.saved = {}
    a.scripts = []
    a.log_always = lambda m: a.messages.append(m)
    a._t = lambda key, **kw: key
    a.save_variables = types.SimpleNamespace(allVariables=a.saved)

    class FakeGcode:
        def run_script_from_command(self, script):
            a.scripts.append(script)
            if script.startswith("SAVE_VARIABLE"):
                parts = dict(p.split("=", 1) for p in script.split()[1:])
                a.saved[parts["VARIABLE"]] = int(parts["VALUE"])

    a.gcode = FakeGcode()
    return a


class TestClamp:
    def test_a_request_within_the_ceiling_passes_through(self, ace):
        assert ace.clamp_purge_mm(90) == (90, "")

    def test_the_per_swap_ceiling_holds(self, ace):
        applied, reason = ace.clamp_purge_mm(900)
        assert applied == 150
        assert "swap_purge_max" in reason

    def test_a_ceiling_of_zero_means_no_per_swap_limit(self, ace):
        ace.swap_purge_max = 0
        assert ace.clamp_purge_mm(400) == (400, "")

    def test_garbage_clamps_to_zero_rather_than_raising(self, ace):
        """A malformed PURGE= must never take the swap down with it."""
        assert ace.clamp_purge_mm("lots")[0] == 0

    def test_a_negative_request_clamps_to_zero(self, ace):
        assert ace.clamp_purge_mm(-90)[0] == 0


class TestBinAccounting:
    """The guard that actually prevents a blob. A per-swap ceiling alone
    does not: ten individually safe purges still overflow a bin."""

    def test_capacity_zero_disables_accounting(self, ace):
        ace.saved["ace__purge_bin_used_mm"] = 99999
        assert ace.clamp_purge_mm(100) == (100, "")

    def test_a_partly_full_bin_clamps_to_what_is_left(self, ace):
        ace.purge_bin_capacity_mm = 1000
        ace.saved["ace__purge_bin_used_mm"] = 950
        applied, reason = ace.clamp_purge_mm(150)
        assert applied == 50
        assert "purge bin full" in reason

    def test_a_full_bin_refuses_entirely(self, ace):
        ace.purge_bin_capacity_mm = 1000
        ace.saved["ace__purge_bin_used_mm"] = 1000
        applied, reason = ace.clamp_purge_mm(150)
        assert applied == 0
        assert "refused" in reason

    def test_accounting_accumulates_and_persists(self, ace):
        ace.purge_bin_capacity_mm = 1000
        ace.purge_bin_account(120)
        ace.purge_bin_account(80)
        assert ace.purge_bin_used_mm() == 200
        assert any(s.startswith("SAVE_VARIABLE") for s in ace.scripts)

    def test_it_warns_at_80_percent(self, ace):
        ace.purge_bin_capacity_mm = 1000
        ace.purge_bin_account(700)
        assert not ace.messages
        ace.purge_bin_account(150)
        assert any("purge_bin_nearly_full" in m for m in ace.messages)

    def test_reset_zeroes_the_accounting(self, ace):
        ace.purge_bin_capacity_mm = 1000
        ace.purge_bin_account(500)
        replies = []
        ace.cmd_ACE_PURGE_BIN_RESET(
            types.SimpleNamespace(respond_info=replies.append))
        assert ace.purge_bin_used_mm() == 0
        assert replies

    def test_unreadable_state_reads_as_zero_not_a_crash(self, ace):
        ace.saved["ace__purge_bin_used_mm"] = "corrupt"
        assert ace.purge_bin_used_mm() == 0


class TestGetPurgeLength:
    """One authority: every purge path reads get_purge_length(), so the
    clamp has to live there and not at the call sites."""

    def test_the_config_value_is_clamped_too(self, ace):
        ace.swap_purge_length = 900
        assert ace.get_purge_length() == 150

    def test_a_manual_override_is_clamped_too(self, ace):
        ace._purge_length_override = 900
        assert ace.get_purge_length() == 150

    def test_a_swap_scoped_purge_wins_and_is_not_re_clamped(self, ace):
        """cmd_ACE_SWAP_HEAD already clamped and accounted it; re-clamping
        here would charge the bin twice."""
        ace._swap_purge_mm = 40
        ace._purge_length_override = 900
        assert ace.get_purge_length() == 40

    def test_zero_means_use_the_stock_default(self, ace):
        assert ace.get_purge_length() == 0


class TestCanExtrudeGate:
    def test_a_cold_hotend_reports_it_cannot_extrude(self, ace):
        heater = types.SimpleNamespace(can_extrude=False)
        ace.toolhead = types.SimpleNamespace(
            get_extruder=lambda: types.SimpleNamespace(
                get_heater=lambda: heater))
        assert ace.purge_can_extrude() is False
        heater.can_extrude = True
        assert ace.purge_can_extrude() is True

    def test_an_unavailable_heater_reads_as_cannot_extrude(self, ace):
        """Fail closed: "cannot tell" and "safe to extrude" are not the
        same answer."""
        ace.toolhead = types.SimpleNamespace(
            get_extruder=lambda: (_ for _ in ()).throw(RuntimeError("no")))
        assert ace.purge_can_extrude() is False

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

from .test_load_retry import ace_module, FakeGcmd  # noqa: F401


@pytest.fixture
def ace(ace_module):
    a = ace_module.MultiAce.__new__(ace_module.MultiAce)
    a.swap_purge_length = 0
    a.swap_purge_max = 150
    a.swap_purge_min = 0
    a.purge_bin_capacity_mm = 0
    a.purge_color_aware = False
    a.purge_scale_pct = 100
    a.purge_pair_mm = {}
    a._purge_scale_cfg = 100
    a._purge_pair_mm_cfg = ''
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

    def test_zero_means_use_the_stock_default(self, ace, ace_module):
        """swap_purge_length=0 (the shipped default) used to mean "say
        nothing and let the macro's own 80 mm default apply" - that
        default is now made explicit so purge_scale_pct can still act on
        it (see effective_purge_mm)."""
        assert ace.get_purge_length() == ace_module.STOCK_FLUSH_DEFAULT_MM

    def test_the_stock_default_is_scaled_too(self, ace, ace_module):
        ace.purge_scale_pct = 70
        assert ace.get_purge_length() == pytest.approx(
            ace_module.STOCK_FLUSH_DEFAULT_MM * 0.7, abs=1)

    def test_an_explicit_zero_override_is_also_the_scaled_stock_default(
            self, ace, ace_module):
        """LENGTH=0 is documented as "use the stock default" - it has to
        resolve the SAME way swap_purge_length's own 0 does, or it would
        silently escape purge_scale_pct (a literal 0 here reads as "omit
        LENGTH=" to the caller, which then falls through to the macro's
        own UNSCALED 80 mm)."""
        ace._purge_length_override = 0
        ace.purge_scale_pct = 70
        assert ace.get_purge_length() == pytest.approx(
            ace_module.STOCK_FLUSH_DEFAULT_MM * 0.7, abs=1)

    def test_pair_overrides_are_the_preflights_job_not_the_engines(self, ace):
        """Overrides are keyed by colour, which the engine cannot see
        mid-print: the preflight resolves them into explicit PURGE= stamps,
        and get_purge_length's own fallback ignores them."""
        ace.swap_purge_length = 35
        ace.purge_pair_mm = {"0047ab>d94040": 60}
        assert ace.get_purge_length() == 35


class TestSetPurgeCommand:
    """ACE_SET_PURGE SCALE=/PAIRS= (§1.6/Config-tab) - the live,
    write-through setters the Config tab calls, same pattern as
    MATRIX=/PERSIST= already use."""

    def test_scale_applies_immediately(self, ace):
        ace.cmd_ACE_SET_PURGE(FakeGcmd(SCALE=70, PERSIST=0))
        assert ace.purge_scale_pct == 70
        assert ace.get_purge_length() == pytest.approx(56, abs=1)

    def test_pairs_applies_immediately(self, ace):
        ace.cmd_ACE_SET_PURGE(
            FakeGcmd(PAIRS="0047ab>d94040=60,d94040>0047ab=110", PERSIST=0))
        assert ace.purge_pair_mm == {"0047ab>d94040": 60.0,
                                     "d94040>0047ab": 110.0}

    def test_an_empty_pairs_clears_overrides(self, ace):
        ace.purge_pair_mm = {"0047ab>d94040": 60.0}
        ace.cmd_ACE_SET_PURGE(FakeGcmd(PAIRS="", PERSIST=0))
        assert ace.purge_pair_mm == {}

    def test_the_word_none_clears_overrides_too(self, ace):
        """Klipper may drop an empty PAIRS=, so the web sends a word."""
        ace.purge_pair_mm = {"0047ab>d94040": 60.0}
        ace.cmd_ACE_SET_PURGE(FakeGcmd(PAIRS="none", PERSIST=0))
        assert ace.purge_pair_mm == {}

    def test_neither_option_raises(self, ace):
        with pytest.raises(Exception):
            ace.cmd_ACE_SET_PURGE(FakeGcmd())


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


class _SwapGcmd:
    """Just enough of a GCodeCommand for cmd_ACE_SWAP_HEAD's argument
    parsing; HEAD=9 makes it stop right after the purge handling."""

    def __init__(self, **params):
        self.params = params

    def get(self, name, default=None):
        return self.params.get(name, default)

    get_int = get_float = lambda self, name, default=None, **kw:         self.params.get(name, default)

    def error(self, message="", **kw):
        return RuntimeError(message)


class TestPurgeStampsFollowThePerPairSwitch:
    """Per-pair purge off = fixed purge again, also on files that already
    carry PURGE= stamps (the Config-tab hint promises exactly that)."""

    def _swap(self, ace):
        ace.swap_anti_ooze_retract = 10
        ace.purge_can_extrude = lambda head=None: True
        ace.purge_bin_account = lambda mm: None
        with pytest.raises(RuntimeError):
            ace.cmd_ACE_SWAP_HEAD(_SwapGcmd(HEAD=9, PURGE=50))

    def test_a_stamp_is_honoured_while_the_switch_is_on(self, ace):
        ace.purge_matrix = True
        self._swap(ace)
        assert ace._swap_purge_mm == 50

    def test_a_stamp_is_ignored_while_the_switch_is_off(self, ace):
        ace.purge_matrix = False
        self._swap(ace)
        assert ace._swap_purge_mm is None

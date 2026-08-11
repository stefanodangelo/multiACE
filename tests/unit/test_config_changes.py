"""Config change taxonomy: what changed, and what has to restart.

This is what the "Changes applied" modal shows, so the interesting cases
are the boring ones: a comment-only edit must NOT claim a reboot, and a
change that really needs a reboot must never be under-served with a
Klipper restart.
"""
import pytest

from multiace import config_changes as cc

BASE = """\
# multiACE config
[ace]
ace_device_count: 1
load_length: 2000
retract_length: 1950
dryer_temp: 55
display_index_base: 0

[ace 1]
load_length: 2100

[ace_tipform]
mode: stock
"""


def summarize(new_text, old_text=BASE):
    return cc.summarize_changes(old_text, new_text)


class TestParseSections:
    def test_sections_and_keys(self):
        parsed = cc.parse_sections(BASE)
        assert parsed["ace"]["load_length"] == "2000"
        assert parsed["ace 1"]["load_length"] == "2100"
        assert parsed["ace_tipform"]["mode"] == "stock"

    def test_comments_and_blank_lines_are_ignored(self):
        parsed = cc.parse_sections("[ace]\n# load_length: 999\n\nfeed_speed: 80\n")
        assert parsed["ace"] == {"feed_speed": "80"}

    def test_section_header_whitespace_is_normalised(self):
        parsed = cc.parse_sections("[  ace   1 ]\nload_length: 5\n")
        assert "ace 1" in parsed


class TestNoChange:
    def test_identical_text_reports_nothing(self):
        s = summarize(BASE)
        assert s["changed"] is False
        assert s["changes"] == []
        assert s["restart_required"] == cc.RESTART_NONE

    def test_comment_and_whitespace_edits_are_not_changes(self):
        edited = BASE.replace("# multiACE config",
                              "# multiACE config (edited by hand)")
        edited = edited.replace("load_length: 2000", "load_length:   2000  ")
        s = summarize(edited)
        assert s["changed"] is False
        assert s["restart_required"] == cc.RESTART_NONE


class TestRestartLevels:
    def test_live_key_needs_no_restart(self):
        s = summarize(BASE.replace("display_index_base: 0",
                                   "display_index_base: 1"))
        assert s["restart_required"] == cc.RESTART_NONE
        assert s["changes"] == ["display_index_base: 0→1"]

    def test_ordinary_value_needs_a_klipper_restart(self):
        s = summarize(BASE.replace("load_length: 2000", "load_length: 2200"))
        assert s["restart_required"] == cc.RESTART_KLIPPER
        assert "load_length: 2000→2200" in s["changes"]

    def test_unit_count_needs_a_full_reboot(self):
        s = summarize(BASE.replace("ace_device_count: 1", "ace_device_count: 2"))
        assert s["restart_required"] == cc.RESTART_PRINTER

    def test_strongest_level_wins_in_a_batch(self):
        """A reboot-level change alongside live and Klipper-level ones must
        not be under-served: one restart has to satisfy all of them."""
        edited = (BASE.replace("ace_device_count: 1", "ace_device_count: 2")
                      .replace("load_length: 2000", "load_length: 2200")
                      .replace("display_index_base: 0", "display_index_base: 1"))
        s = summarize(edited)
        assert s["restart_required"] == cc.RESTART_PRINTER
        assert len(s["changes"]) == 3

    def test_live_and_klipper_batch_stops_at_klipper(self):
        edited = (BASE.replace("load_length: 2000", "load_length: 2200")
                      .replace("display_index_base: 0", "display_index_base: 1"))
        assert summarize(edited)["restart_required"] == cc.RESTART_KLIPPER

    def test_per_ace_section_value_is_klipper_level(self):
        s = summarize(BASE.replace("load_length: 2100", "load_length: 2400"))
        assert s["restart_required"] == cc.RESTART_KLIPPER
        assert s["changes"] == ["[ace 1] load_length: 2100→2400"]


class TestAddedRemoved:
    def test_added_key(self):
        s = summarize(BASE.replace("[ace]\n", "[ace]\nswap_purge_length: 20\n"))
        assert s["changes"] == ["swap_purge_length: (unset)→20"]
        assert s["details"][0]["kind"] == "added"

    def test_removed_key(self):
        s = summarize(BASE.replace("dryer_temp: 55\n", ""))
        assert s["changes"] == ["dryer_temp: 55→(unset)"]
        assert s["details"][0]["kind"] == "removed"

    def test_added_section_is_klipper_level(self):
        s = summarize(BASE + "\n[ace_bg_swap]\nenabled_heads: 0,1\n")
        assert s["restart_required"] == cc.RESTART_KLIPPER
        assert "[ace_bg_swap] added" in s["changes"]

    def test_losing_the_ace_section_needs_a_reboot(self):
        """Dropping [ace] is the stock/ACE flip - Klipper alone cannot
        undo an ACE that is no longer configured at boot."""
        stripped = BASE.split("[ace 1]")[1]
        s = summarize("[ace 1]" + stripped)
        assert s["restart_required"] == cc.RESTART_PRINTER
        assert "[ace] removed" in s["changes"]


class TestClassifyKey:
    @pytest.mark.parametrize("key", sorted(cc.REBOOT_KEYS))
    def test_reboot_keys_everywhere(self, key):
        assert cc.classify_key("ace", key) == cc.RESTART_PRINTER

    @pytest.mark.parametrize("key", sorted(cc.LIVE_KEYS))
    def test_live_keys_in_the_ace_section(self, key):
        assert cc.classify_key("ace", key) == cc.RESTART_NONE

    def test_unknown_key_defaults_to_klipper_restart(self):
        assert cc.classify_key("ace", "something_new") == cc.RESTART_KLIPPER

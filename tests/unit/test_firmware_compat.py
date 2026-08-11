"""Firmware version parsing and the compatibility table.

Covers the contract the web UI depends on: every lookup returns the same
shape, an unknown version is reported (not guessed at), and nothing here
ever raises on junk input - a printer that reports something weird must
still get a dashboard.
"""
import pytest

from multiace import firmware_compat as fc


class TestNormalizeVersion:
    @pytest.mark.parametrize("raw,expected", [
        ("1.5.2", "1.5.2"),
        ("V1.5.2", "1.5.2"),
        ("1.5.2-release", "1.5.2"),
        ("Snapmaker U1 1.5.1 (build 7)", "1.5.1"),
        (b"1.5.0", "1.5.0"),
        ("1.1.31", "1.1.31"),
    ])
    def test_extracts_dotted_version(self, raw, expected):
        assert fc.normalize_version(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "unknown", "no-digits-here"])
    def test_no_version_yields_empty(self, raw):
        assert fc.normalize_version(raw) == ""

    def test_version_tuple_is_comparable(self):
        assert fc.version_tuple("1.5.2") > fc.version_tuple("1.5.1")
        assert fc.version_tuple("1.5.10") > fc.version_tuple("1.5.2")
        assert fc.version_tuple("nonsense") == ()


class TestCompatFor:
    @pytest.mark.parametrize("version", ["1.5.0", "1.5.1", "1.5.2"])
    def test_supported_printer_firmware(self, version):
        rec = fc.compat_for(version)
        assert rec["status"] == fc.STATUS_SUPPORTED
        assert rec["supported"] is True
        assert rec["firmware_version"] == version

    def test_152_is_supported_with_no_known_issues(self):
        """The headline of this release: 1.5.2 is verified, not merely
        'prepared for'."""
        rec = fc.compat_for("1.5.2")
        assert rec["status"] == fc.STATUS_SUPPORTED
        assert rec["known_issues"] == []

    def test_ace_pro_2_firmware(self):
        rec = fc.compat_for("1.1.31")
        assert rec["status"] == fc.STATUS_SUPPORTED
        assert rec["device"] == "ACE Pro 2"

    def test_known_issues_are_reported(self):
        assert fc.compat_for("1.5.1")["known_issues"]

    def test_family_wildcard_matches_unsupported_line(self):
        rec = fc.compat_for("1.4.7")
        assert rec["status"] == fc.STATUS_UNSUPPORTED
        assert rec["supported"] is False
        assert rec["reason"]

    def test_unknown_but_parseable_version_is_untested_not_blocked(self):
        rec = fc.compat_for("9.9.9")
        assert rec["status"] == fc.STATUS_UNTESTED
        assert rec["supported"] is False
        assert rec["firmware_version"] == "9.9.9"

    @pytest.mark.parametrize("raw", [None, "", "???"])
    def test_missing_version_is_unknown(self, raw):
        rec = fc.compat_for(raw)
        assert rec["status"] == fc.STATUS_UNKNOWN
        assert rec["firmware_version"] == ""

    def test_shape_is_stable_across_every_branch(self):
        for raw in ("1.5.2", "1.4.0", "9.9.9", None):
            rec = fc.compat_for(raw)
            assert set(["firmware_version", "status", "known_issues",
                        "supported"]).issubset(rec)
            assert isinstance(rec["known_issues"], list)

    def test_table_is_not_mutated_by_callers(self):
        rec = fc.compat_for("1.5.1")
        rec["known_issues"].append("scribbled by a caller")
        assert "scribbled by a caller" not in \
            fc.FIRMWARE_COMPAT["1.5.1"]["known_issues"]


class TestAcePyMirror:
    """klippy/extras/ace.py carries an inlined copy of the table (it is
    installed on its own and cannot import this package). If the two
    disagree, the UI and klippy.log would say different things about the
    same printer."""

    def _ace_table(self):
        import ast
        from pathlib import Path
        src = Path(__file__).resolve().parents[2] / \
            "multiace" / "klipper" / "extras" / "ace.py"
        tree = ast.parse(src.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "FIRMWARE_COMPAT"
                    for t in node.targets):
                return ast.literal_eval(node.value)
        raise AssertionError("ace.py has no FIRMWARE_COMPAT table")

    def test_mirror_agrees_with_the_canonical_table(self):
        for version, status in self._ace_table().items():
            # ace.py stores family keys as '1.4', the table as '1.4.x'.
            probe = version if version.count(".") == 2 else version + ".0"
            assert fc.compat_for(probe)["status"] == status, version

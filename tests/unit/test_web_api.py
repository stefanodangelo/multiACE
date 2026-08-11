"""Web backend: config apply flow, firmware reporting, retry bridge.

Runs the real FastAPI app against a temp config file with Moonraker
stubbed out, so these cover the request/response contract the Vue app
codes against - including the one that matters most: a save must never
restart more (or less) than the change actually needs.
"""
import importlib
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "multiace" / "web" / "backend"

BASE_CFG = """\
[ace]
ace_device_count: 1
load_length: 2000
display_index_base: 0
"""


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    """Import main.py with printer paths pointed at tmp_path.

    The module reads its paths at import time, so it has to be imported
    fresh per test rather than configured afterwards.
    """
    cfg = tmp_path / "ace.cfg"
    cfg.write_text(BASE_CFG, encoding="utf-8")
    monkeypatch.setenv("MULTIACE_CFG_PATH", str(cfg))
    monkeypatch.setenv("MULTIACE_RETRY_STATE", str(tmp_path / "retry_state.json"))
    monkeypatch.setenv("MULTIACE_RETRY_CONTROL", str(tmp_path / "retry_control"))
    monkeypatch.setenv("MULTIACE_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setenv("MULTIACE_OVERRIDE_FILE", str(tmp_path / "overrides.json"))
    monkeypatch.setenv("MULTIACE_MOCK_DIR", str(ROOT / "tests" / "fixtures"))
    monkeypatch.setenv("MULTIACE_WEB_VERSION", "test")
    monkeypatch.syspath_prepend(str(BACKEND))
    sys.modules.pop("main", None)
    main = importlib.import_module("main")

    calls = []

    async def fake_post(path, body=None, timeout=30.0):
        calls.append(path)
        return {"ok": path}

    async def fake_get(path):
        if path == "/machine/system_info":
            return {"result": {"system_info": {"product_info": {
                "device_name": "Snapmaker U1", "machine_type": "U1",
                "firmware_version": "1.5.2"}}}}
        raise AssertionError(f"unexpected GET {path}")

    monkeypatch.setattr(main, "_mr_post", fake_post)
    monkeypatch.setattr(main, "_mr_get", fake_get)
    main._moonraker_calls = calls
    return main, cfg, calls


@pytest.fixture
def client(app_env):
    from fastapi.testclient import TestClient
    main, cfg, calls = app_env
    with TestClient(main.app) as c:
        yield c, main, cfg, calls


def put_config(client, content, **kw):
    c, main, cfg, calls = client
    body = {"content": content, "base_sha1": main._cfg_sha1(cfg.read_text()), **kw}
    return c.put("/api/config", json=body)


class TestVersionEndpoint:
    def test_reports_firmware_and_compatibility(self, client):
        c, main, cfg, calls = client
        j = c.get("/api/version").json()
        assert j["firmware_version"] == "1.5.2"
        assert j["compatibility"] == "supported"
        assert j["known_issues"] == []
        assert j["firmware"]["source"] == "moonraker"

    def test_config_override_wins_over_moonraker(self, client):
        c, main, cfg, calls = client
        cfg.write_text(BASE_CFG + "firmware_version: 1.4.7\n", encoding="utf-8")
        j = c.get("/api/version").json()
        assert j["firmware_version"] == "1.4.7"
        assert j["compatibility"] == "unsupported"
        assert j["firmware"]["source"] == "config"


class TestConfigApply:
    def test_diff_is_reported_with_the_save(self, client):
        r = put_config(client, BASE_CFG.replace("2000", "2200"))
        j = r.json()
        assert j["applied"] is True
        assert j["changed"] is True
        assert j["changes"] == ["load_length: 2000→2200"]
        assert j["restart_required"] == "klipper_restart"

    def test_a_no_op_save_needs_no_restart(self, client):
        j = put_config(client, BASE_CFG).json()
        assert j["changed"] is False
        assert j["restart_required"] == "none"

    def test_default_save_does_not_restart_anything(self, client):
        """The modal asks first; the save itself must not reboot a printer
        out from under the user."""
        c, main, cfg, calls = client
        put_config(client, BASE_CFG.replace("2000", "2200"))
        assert calls == []

    def test_auto_behavior_performs_the_needed_restart(self, client):
        c, main, cfg, calls = client
        put_config(client, BASE_CFG.replace("2000", "2200"),
                   restart_behavior="auto")
        assert calls == ["/printer/firmware_restart"]

    def test_auto_behavior_reboots_when_a_reboot_is_needed(self, client):
        c, main, cfg, calls = client
        put_config(client, BASE_CFG.replace("ace_device_count: 1",
                                            "ace_device_count: 2"),
                   restart_behavior="auto")
        assert calls == ["/machine/reboot"]

    def test_auto_behavior_restarts_nothing_for_a_live_key(self, client):
        c, main, cfg, calls = client
        put_config(client, BASE_CFG.replace("display_index_base: 0",
                                            "display_index_base: 1"),
                   restart_behavior="auto")
        assert calls == []

    def test_explicit_none_overrides_the_diff(self, client):
        c, main, cfg, calls = client
        put_config(client, BASE_CFG.replace("ace_device_count: 1",
                                            "ace_device_count: 2"),
                   restart_behavior="none")
        assert calls == []

    def test_legacy_restart_klipper_flag_still_works(self, client):
        c, main, cfg, calls = client
        put_config(client, BASE_CFG.replace("2000", "2200"),
                   restart_klipper=True)
        assert calls == ["/printer/firmware_restart"]

    def test_legacy_flag_is_upgraded_when_a_reboot_is_needed(self, client):
        """An old client asking for a Klipper restart on a change that
        needs a reboot would otherwise silently under-apply it."""
        c, main, cfg, calls = client
        put_config(client, BASE_CFG.replace("ace_device_count: 1",
                                            "ace_device_count: 2"),
                   restart_klipper=True)
        assert calls == ["/machine/reboot"]

    def test_backup_is_written_before_the_new_content(self, client):
        c, main, cfg, calls = client
        r = put_config(client, BASE_CFG.replace("2000", "2200"))
        backup = Path(r.json()["backup"])
        assert backup.read_text() == BASE_CFG
        assert "2200" in cfg.read_text()

    def test_stale_sha1_is_rejected(self, client):
        c, main, cfg, calls = client
        r = c.put("/api/config", json={"content": "[ace]\n",
                                       "base_sha1": "deadbeef"})
        assert r.status_code == 409
        detail = json.loads(r.json()["detail"])
        assert detail["content"] == BASE_CFG


class TestConfigPreview:
    def test_preview_reports_without_writing(self, client):
        c, main, cfg, calls = client
        r = c.post("/api/config/preview",
                   json={"content": BASE_CFG.replace("2000", "2200")})
        j = r.json()
        assert j["restart_required"] == "klipper_restart"
        assert cfg.read_text() == BASE_CFG


class TestRestartEndpoint:
    @pytest.mark.parametrize("behavior,expected", [
        ("klipper_restart", ["/printer/firmware_restart"]),
        ("printer_reboot", ["/machine/reboot"]),
        ("none", []),
    ])
    def test_behaviors(self, client, behavior, expected):
        c, main, cfg, calls = client
        r = c.post("/api/restart", json={"behavior": behavior})
        assert r.status_code == 200
        assert calls == expected

    def test_unknown_behavior_is_rejected(self, client):
        c, main, cfg, calls = client
        assert c.post("/api/restart", json={"behavior": "explode"}).status_code == 400


class TestRetryBridge:
    """ace.py writes the state file, the UI reads it through here; the UI
    writes the control file through here, ace.py reads it."""

    def test_no_state_file_means_no_retry(self, client):
        c, main, cfg, calls = client
        assert c.get("/api/retry-state").json()["retry_state"] is None

    def test_active_state_is_served(self, client):
        c, main, cfg, calls = client
        import time
        Path(main.RETRY_STATE_PATH).write_text(json.dumps({
            "active": True, "ts": time.time(), "head": 0, "ace": 0,
            "slot": 2, "attempt": 2, "max_attempts": 3,
            "next_retry_ms": 700, "reason": "load_not_finished"}))
        st = c.get("/api/retry-state").json()["retry_state"]
        assert st["attempt"] == 2 and st["slot"] == 2

    def test_stale_state_is_ignored(self, client):
        """Klipper died mid-retry: the file survives, the banner must not."""
        c, main, cfg, calls = client
        Path(main.RETRY_STATE_PATH).write_text(json.dumps({
            "active": True, "ts": 1.0, "attempt": 1, "max_attempts": 3}))
        assert c.get("/api/retry-state").json()["retry_state"] is None

    def test_inactive_state_is_ignored(self, client):
        c, main, cfg, calls = client
        import time
        Path(main.RETRY_STATE_PATH).write_text(json.dumps(
            {"active": False, "ts": time.time()}))
        assert c.get("/api/retry-state").json()["retry_state"] is None

    def test_corrupt_state_does_not_break_the_dashboard(self, client):
        c, main, cfg, calls = client
        Path(main.RETRY_STATE_PATH).write_text("{not json")
        assert c.get("/api/retry-state").json()["retry_state"] is None

    @pytest.mark.parametrize("action", ["now", "cancel"])
    def test_control_actions_reach_the_file(self, client, action):
        c, main, cfg, calls = client
        assert c.post(f"/api/retry/{action}").status_code == 200
        assert Path(main.RETRY_CONTROL_PATH).read_text() == action

    def test_unknown_control_action_is_rejected(self, client):
        c, main, cfg, calls = client
        assert c.post("/api/retry/explode").status_code == 400

    def test_state_rides_along_with_the_dashboard_state(self, client, monkeypatch):
        c, main, cfg, calls = client
        import time
        monkeypatch.setattr(main, "_query_state_gated",
                            lambda: _async_value({}))
        monkeypatch.setattr(main, "_parse_state", lambda s: {"aces": []})
        Path(main.RETRY_STATE_PATH).write_text(json.dumps({
            "active": True, "ts": time.time(), "attempt": 1,
            "max_attempts": 3}))
        assert c.get("/api/state").json()["retry_state"]["attempt"] == 1


async def _async_value(v):
    return v


class TestConsoleEndpoint:
    def test_buffer_starts_empty_and_records_lines(self, client):
        c, main, cfg, calls = client
        main._console_lines.clear()
        main._record_console_line("// [multiACE] hello")
        main._record_console_line("!! [multiACE] boom")
        lines = c.get("/api/console-logs").json()["lines"]
        assert [ln["kind"] for ln in lines] == ["response", "error"]

    def test_since_id_returns_only_newer_lines(self, client):
        c, main, cfg, calls = client
        main._console_lines.clear()
        main._record_console_line("first")
        mid = main._record_console_line("second")["id"]
        main._record_console_line("third")
        lines = c.get(f"/api/console-logs?since_id={mid}").json()["lines"]
        assert [ln["msg"] for ln in lines] == ["third"]

    def test_blank_lines_are_not_recorded(self, client):
        c, main, cfg, calls = client
        assert main._record_console_line("   ") is None

    def test_multi_line_scripts_are_rejected(self, client):
        c, main, cfg, calls = client
        assert c.post("/api/console", json={"script": "G28\nG1 X0"}).status_code == 400

    def test_empty_script_is_rejected(self, client):
        c, main, cfg, calls = client
        assert c.post("/api/console", json={"script": "  "}).status_code == 400


class TestWebcamResolution:
    def _cams(self, main, monkeypatch, cams):
        async def fake_get(path):
            assert path == "/server/webcams/list"
            return {"result": {"webcams": cams}}
        monkeypatch.setattr(main, "_mr_get", fake_get)

    def test_no_cameras_reports_unavailable(self, client, monkeypatch):
        c, main, cfg, calls = client
        self._cams(main, monkeypatch, [])
        j = c.get("/api/webcam/info").json()
        assert j["available"] is False and j["reason"]

    def test_our_own_panel_entry_is_skipped(self, client, monkeypatch):
        """The Fluidd panel camera is an iframe back to this UI - showing
        it inside this UI's own sidebar would be a mirror tunnel."""
        c, main, cfg, calls = client
        self._cams(main, monkeypatch, [
            {"name": main.FLUIDD_CAMERA_NAME, "service": "iframe",
             "stream_url": "/multiace/?panel=1"}])
        assert c.get("/api/webcam/info").json()["available"] is False

    def test_relative_stream_url_is_made_absolute(self, client, monkeypatch):
        c, main, cfg, calls = client
        self._cams(main, monkeypatch, [
            {"name": "cam", "service": "mjpegstreamer",
             "stream_url": "/webcam/?action=stream",
             "snapshot_url": "/webcam/?action=snapshot"}])
        j = c.get("/api/webcam/info").json()
        assert j["available"] is True
        assert j["stream_url"].startswith("http://127.0.0.1/webcam/")
        assert j["proxy_path"] == "/api/webcam/stream"

    def test_absolute_stream_url_is_left_alone(self, client, monkeypatch):
        c, main, cfg, calls = client
        self._cams(main, monkeypatch, [
            {"name": "cam", "service": "mjpegstreamer",
             "stream_url": "http://cam.local:8080/stream"}])
        assert c.get("/api/webcam/info").json()["stream_url"] == \
            "http://cam.local:8080/stream"

    def test_disabled_cameras_are_skipped(self, client, monkeypatch):
        c, main, cfg, calls = client
        self._cams(main, monkeypatch, [
            {"name": "off", "service": "mjpegstreamer", "enabled": False,
             "stream_url": "/webcam/?action=stream"}])
        assert c.get("/api/webcam/info").json()["available"] is False

    def test_moonraker_failure_is_not_fatal(self, client, monkeypatch):
        async def boom(path):
            raise RuntimeError("moonraker down")
        c, main, cfg, calls = client
        monkeypatch.setattr(main, "_mr_get", boom)
        assert c.get("/api/webcam/info").json()["available"] is False


class TestMockMode:
    def test_per_request_mock_serves_the_fixture(self, client):
        """?mock=1 lets a dev compare mocked and live data without
        restarting the server."""
        c, main, cfg, calls = client
        j = c.get("/api/state?mock=1").json()
        assert j["mock"] is True
        assert len(j["aces"]) == 2

    def test_mock_version_uses_the_fixture_firmware(self, client):
        c, main, cfg, calls = client
        j = c.get("/api/version?mock=1").json()
        assert j["firmware"]["firmware_version"] == "1.5.2"

    def test_simulation_is_refused_outside_mock_mode(self, client):
        """It must be impossible to fake a jam on a real printer."""
        c, main, cfg, calls = client
        r = c.post("/api/debug/simulate", json={"event": "load_failure"})
        assert r.status_code == 403

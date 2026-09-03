"""Web backend: config apply flow, firmware reporting, retry bridge.

Runs the real FastAPI app against a temp config file with Moonraker
stubbed out, so these cover the request/response contract the Vue app
codes against - including the one that matters most: a save must never
restart more (or less) than the change actually needs.
"""
import asyncio
import importlib
import json
import os
import sys
import time
from pathlib import Path

import httpx
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
    monkeypatch.setenv("MULTIACE_VIRTUAL_LOADOUT_FILE",
                        str(tmp_path / "virtual_loadout.json"))
    monkeypatch.setenv("MULTIACE_QUEUE_FILE", str(tmp_path / "print_queue.json"))
    monkeypatch.setenv("MULTIACE_MOCK_DIR", str(ROOT / "tests" / "fixtures"))
    monkeypatch.setenv("MULTIACE_WEB_VERSION", "test")
    # Every other MULTIACE_* path this fixture cares about is pinned above;
    # this one has to be pinned too, or a leftover MULTIACE_MOCK_MODE=1 from
    # a dev-ui session in the SAME shell (run-dev-ui.ps1/.sh sets it for the
    # life of that terminal, not just that one script run) silently forces
    # every test in this file into permanent mock mode. That is a global
    # MOCK_MODE at import time, not a per-request ?mock=1 - it broke the
    # endpoints that specifically assert non-mock behaviour, twice, before
    # this line existed. The suite has to be hermetic against shell state,
    # not rely on the caller remembering to clear it.
    monkeypatch.delenv("MULTIACE_MOCK_MODE", raising=False)
    # Same hazard as MULTIACE_MOCK_MODE above: run-dev-ui.ps1/.sh document
    # setting this in the shell to preview head mode locally
    # ($env:MULTIACE_MOCK_STATE_FILE = "mock_state_head.json"). Left set in
    # that same terminal, it silently swaps every mock-mode test here onto
    # the head fixture instead of the default one, which doesn't error -
    # it just returns a different (head-mode) plan shape, failing whatever
    # assertion assumed "slicer/optimize/layer" and zero swaps.
    monkeypatch.delenv("MULTIACE_MOCK_STATE_FILE", raising=False)
    # Same hazard: a leftover MULTIACE_PREFLIGHT_MAX_MB from a dev session
    # in the same shell would silently change the default-cap assertions
    # below out from under the test.
    monkeypatch.delenv("MULTIACE_PREFLIGHT_MAX_MB", raising=False)
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
        # These tests exercise the Moonraker webcams-list path; the
        # WebRTC override (a fixed camera URL) takes priority over it
        # in real deployments, so disable it here.
        monkeypatch.setattr(main, "WEBRTC_URL", "")

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
        monkeypatch.setattr(main, "WEBRTC_URL", "")
        monkeypatch.setattr(main, "_mr_get", boom)
        assert c.get("/api/webcam/info").json()["available"] is False

    def test_webrtc_override_short_circuits_moonraker(self, client, monkeypatch):
        c, main, cfg, calls = client
        monkeypatch.setattr(main, "WEBRTC_URL", "http://192.168.178.82/webcam/webrtc")
        j = c.get("/api/webcam/info").json()
        assert j["available"] is True
        assert j["service"] == "webrtc"
        assert j["webrtc_url"] == "http://192.168.178.82/webcam/webrtc"
        assert j["signal_path"] == "/api/webcam/webrtc-signal"
        assert "proxy_path" not in j


class TestWebrtcSignal:
    """The SDP handshake is relayed server-side rather than fetched by the
    browser directly: camera-streamer sends no CORS headers and 501s on
    OPTIONS, so a cross-origin browser POST can never reach it."""

    def test_rejected_when_no_webrtc_camera_configured(self, client, monkeypatch):
        c, main, cfg, calls = client
        monkeypatch.setattr(main, "WEBRTC_URL", "")
        self_cams = []

        async def fake_get(path):
            return {"result": {"webcams": self_cams}}
        monkeypatch.setattr(main, "_mr_get", fake_get)
        r = c.post("/api/webcam/webrtc-signal", json={"type": "request"})
        assert r.status_code == 503

    def test_rejected_for_a_plain_mjpeg_camera(self, client, monkeypatch):
        c, main, cfg, calls = client
        monkeypatch.setattr(main, "WEBRTC_URL", "")

        async def fake_get(path):
            return {"result": {"webcams": [
                {"name": "cam", "service": "mjpegstreamer",
                 "stream_url": "/webcam/?action=stream"}]}}
        monkeypatch.setattr(main, "_mr_get", fake_get)
        r = c.post("/api/webcam/webrtc-signal", json={"type": "request"})
        assert r.status_code == 503

    def test_unavailable_in_mock_mode(self, client, monkeypatch):
        c, main, cfg, calls = client
        monkeypatch.setattr(main, "WEBRTC_URL", "http://192.168.178.82/webcam/webrtc")
        r = c.post("/api/webcam/webrtc-signal?mock=1", json={"type": "request"})
        assert r.status_code == 503


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

    def test_sample_gcode_is_refused_outside_mock_mode(self, client):
        """A debug endpoint that leaks into production is the failure mode
        worth a test: on a real printer this must not hand out a 2.8 MB
        fixture from the install tree."""
        c, main, cfg, calls = client
        r = c.get("/api/debug/sample-gcode")
        assert r.status_code == 403

    def test_sample_gcode_streams_the_fixture_in_mock_mode(self, client,
                                                           monkeypatch):
        """The one-click dev affordance: the preview's specimen, without
        walking the upload flow by hand every time."""
        c, main, cfg, calls = client
        monkeypatch.setattr(main, "MOCK_MODE", True)
        r = c.get("/api/debug/sample-gcode")
        assert r.status_code == 200
        assert b"; Change Tool" in r.content or b"G1 " in r.content


class TestMockPreflight:
    """§2: upload a g-code on a laptop with no printer and get the full
    plan. The 409 "no slots are loaded" describes a printer, and in mock
    mode there isn't one."""

    def _upload(self, c):
        path = ROOT / "tests" / "fixtures" / "sample_4color.gcode"
        with path.open("rb") as f:
            return c.post("/api/preflight?mock=1",
                          files={"file": ("sample_4color.gcode", f,
                                          "text/plain")})

    def test_preflight_works_with_no_printer(self, client):
        c, main, cfg, calls = client
        r = self._upload(c)
        assert r.status_code == 200
        j = r.json()
        assert j["mock"] is True
        assert set(j["plans"]) == {"slicer", "optimize", "layer"}

    def test_every_plan_carries_an_estimate(self, client):
        """Plans are compared in minutes and grams, not swap counts."""
        c, main, cfg, calls = client
        j = self._upload(c).json()
        for name, plan in j["plans"].items():
            if not plan.get("feasible"):
                continue
            est = plan["estimate"]
            assert est["confidence"] == "modelled", name
            assert est["base_s"] == 6772.0, name
            assert est["total_s"] >= est["base_s"], name
            assert est["assumptions"], name

    def test_the_estimate_never_double_counts_a_prime_tower(self, client):
        """The fixture is a tower + flush print, so the purge is already
        inside the slicer's own numbers."""
        c, main, cfg, calls = client
        j = self._upload(c).json()
        est = j["plans"]["slicer"]["estimate"]
        assert est["purge"]["destination"] == "mixed"
        assert est["purge"]["counted_in_total"] is False

    def test_each_plan_carries_a_timeline(self, client):
        c, main, cfg, calls = client
        j = self._upload(c).json()
        tl = j["plans"]["optimize"]["timeline"]
        assert tl and all("kind" in e and "seconds" in e for e in tl)

    def test_livedata_serves_mock_slots(self, client):
        c, main, cfg, calls = client
        j = c.get("/api/preflight/livedata?mock=1").json()
        assert j["mock"] is True
        assert j["live_slots"], "the mock loadout has identified slots"
        assert "cost_params" in j

    def test_printing_is_refused_in_mock(self, client):
        """A mocked run must never look like it queued a real print."""
        c, main, cfg, calls = client
        r = c.post("/api/preflight/print?mock=1",
                   json={"token": "0" * 32, "mode": "slicer"})
        assert r.status_code == 503
        assert "no printer" in r.json()["detail"]

    def test_pysrc_serves_the_third_module(self, client):
        """The worker writes exactly the files this dict names - if
        swap_cost stops being served the browser silently loses the
        estimate."""
        c, main, cfg, calls = client
        j = c.get("/api/preflight/pysrc").json()
        assert "swap_cost" in j and "class SwapCostModel" in j["swap_cost"]
        assert "cost_params" in j and "calibration" in j

    def test_cost_params_come_from_the_config(self, client):
        c, main, cfg, calls = client
        j = c.get("/api/preflight/pysrc").json()
        assert j["cost_params"]["main"]["load_length"] == 2000


class TestPreflightLargeFiles:
    """Large-file support: uploads stream to disk instead of buffering the
    whole thing in RAM, analyze runs off the event loop, the async
    job/percent path lets the UI show real progress, and the size cap is
    served to the browser instead of being a second hardcoded copy there."""

    class _FakeUpload:
        """Minimal stand-in for FastAPI's UploadFile - just async read()."""
        def __init__(self, chunks):
            self._chunks = list(chunks)
            self.max_chunk_seen = 0

        async def read(self, size=-1):
            if not self._chunks:
                return b""
            chunk = self._chunks.pop(0)
            self.max_chunk_seen = max(self.max_chunk_seen, len(chunk))
            return chunk

    def test_stream_upload_writes_all_chunks_without_buffering_them_whole(
            self, app_env, tmp_path):
        main, cfg, calls = app_env
        dest = tmp_path / "out.gcode"
        fake = self._FakeUpload([b"a" * 10, b"b" * 10, b"c" * 10])
        size = asyncio.run(
            main._stream_upload_to_path(fake, dest, max_size=1024))
        assert size == 30
        assert dest.read_bytes() == b"a" * 10 + b"b" * 10 + b"c" * 10
        # Each read() call handed back one small chunk at a time - never
        # the whole file at once - which is the point of streaming.
        assert fake.max_chunk_seen == 10

    def test_oversized_upload_is_rejected_partway_and_cleans_up(
            self, app_env, tmp_path):
        main, cfg, calls = app_env
        dest = tmp_path / "out.gcode"
        fake = self._FakeUpload([b"a" * 10, b"b" * 10, b"c" * 10])
        with pytest.raises(main.HTTPException) as exc:
            asyncio.run(main._stream_upload_to_path(fake, dest, max_size=15))
        assert exc.value.status_code == 413
        assert not dest.exists(), "a rejected upload must not leave a partial file"

    def test_empty_upload_is_rejected(self, app_env, tmp_path):
        main, cfg, calls = app_env
        dest = tmp_path / "out.gcode"
        with pytest.raises(main.HTTPException) as exc:
            asyncio.run(
                main._stream_upload_to_path(self._FakeUpload([]), dest,
                                            max_size=1024))
        assert exc.value.status_code == 400
        assert not dest.exists()

    def test_sync_endpoint_runs_cpu_work_off_the_event_loop(
            self, client, monkeypatch):
        """A large-file analyze must not block the event loop, or every
        other client's status polling/WebSocket traffic stalls behind it -
        the rewrite/print path already gets this treatment via to_thread."""
        c, main, cfg, calls = client
        seen = []
        real_to_thread = asyncio.to_thread

        async def spy_to_thread(func, *a, **kw):
            seen.append(func)
            return await real_to_thread(func, *a, **kw)

        monkeypatch.setattr(main.asyncio, "to_thread", spy_to_thread)
        path = ROOT / "tests" / "fixtures" / "sample_4color.gcode"
        with path.open("rb") as f:
            r = c.post("/api/preflight?mock=1",
                       files={"file": ("sample_4color.gcode", f, "text/plain")})
        assert r.status_code == 200
        assert main.preflight_core.parse_meta in seen
        assert main.preflight_core.build_report in seen

    def test_analyze_job_reports_progress_and_matches_sync_report_shape(
            self, client):
        """The async job path is the same work as POST /api/preflight, just
        pollable - the UI needs a real percent instead of nothing to show
        during a large file's analyze."""
        c, main, cfg, calls = client
        path = ROOT / "tests" / "fixtures" / "sample_4color.gcode"
        with path.open("rb") as f:
            r = c.post("/api/preflight/analyze?mock=1",
                       files={"file": ("sample_4color.gcode", f, "text/plain")})
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        status = None
        for _ in range(200):
            status = c.get(
                f"/api/preflight/analyze/status?job_id={job_id}").json()
            if status["done"]:
                break
            time.sleep(0.02)
        else:
            pytest.fail("analyze job never completed")

        assert status["error"] is None
        report = status["report"]
        assert report["mock"] is True
        assert set(report["plans"]) == {"slicer", "optimize", "layer"}

    def test_analyze_status_404s_for_an_unknown_job(self, client):
        c, main, cfg, calls = client
        r = c.get("/api/preflight/analyze/status?job_id=" + "0" * 32)
        assert r.status_code == 404

    def test_max_upload_mb_is_served_and_matches_the_cap(self, client):
        """The browser's local/Pyodide size gate reads this instead of
        keeping its own hardcoded copy that can drift from the backend's."""
        c, main, cfg, calls = client
        j = c.get("/api/preflight/pysrc").json()
        assert j["max_upload_mb"] == main._PREFLIGHT_MAX_SIZE // (1024 * 1024)

    def test_default_cap_is_500mb(self, app_env):
        main, cfg, calls = app_env
        assert main._PREFLIGHT_MAX_SIZE == 500 * 1024 * 1024


class TestVirtualLoadout:
    """§2.2's what-if tool, and §13.6's structural rule for it.

    If a virtual loadout ever leaked into the real rewrite path, the printer
    would swap to slots holding a different material than planned and run it
    at the wrong temperature - PLA at PETG temps carbonises and clogs. The
    guard is not a flag (a flag gets inverted by a bug) but a separate field
    the rewrite path never reads.
    """

    def _upload(self, c, virtual=None):
        path = ROOT / "tests" / "fixtures" / "sample_4color.gcode"
        data = {}
        if virtual is not None:
            data["virtual_slots"] = json.dumps(virtual)
        with path.open("rb") as f:
            return c.post("/api/preflight?mock=1", data=data,
                          files={"file": ("sample_4color.gcode", f,
                                          "text/plain")})

    def test_a_virtual_loadout_plans_against_the_spools_you_asked_for(
            self, client):
        c, main, cfg, calls = client
        virtual = [{"ace": 0, "slot": i, "material": "PLA",
                    "color": h} for i, h in enumerate(
                        ["#1b1b1f", "#f2f2f2", "#c21b17", "#2e7d32"])]
        j = self._upload(c, virtual).json()
        assert j["virtual_loadout"] is True
        assert len(j["live_slots"]) == 4
        assert {s["color"] for s in j["live_slots"]} == {
            "#1b1b1f", "#f2f2f2", "#c21b17", "#2e7d32"}

    def test_it_is_flagged_loudly(self, client):
        """A plan computed against spools that are not in the machine must
        never look like one that was."""
        c, main, cfg, calls = client
        plain = self._upload(c).json()
        assert "virtual_loadout" not in plain
        assert "virtual_slots" not in plain

    def test_the_rewrite_path_never_receives_it(self, client):
        """The real path takes live_slots. There is no parameter through
        which a virtual loadout could arrive."""
        c, main, cfg, calls = client
        import inspect
        sig = inspect.signature(main.preflight_core.rewrite_pipeline)
        assert "virtual_slots" not in sig.parameters
        src = inspect.getsource(main.preflight_core.rewrite_pipeline)
        assert "virtual" not in src

    def test_material_availability_stays_unconditional_on_the_real_path(
            self, client):
        """The check that catches "that material is not loaded" must not be
        skippable - it is what stops a swap to the wrong filament."""
        c, main, cfg, calls = client
        import inspect
        src = inspect.getsource(main.preflight_core.rewrite_pipeline)
        assert "check_material_availability" in src

    def test_garbage_is_a_bad_request_not_a_silent_fallback(self, client):
        """Silently ignoring it would plan against the REAL loadout while
        the user believes they are looking at a what-if."""
        c, main, cfg, calls = client
        path = ROOT / "tests" / "fixtures" / "sample_4color.gcode"
        with path.open("rb") as f:
            r = c.post("/api/preflight?mock=1",
                       data={"virtual_slots": "not json"},
                       files={"file": ("sample_4color.gcode", f,
                                       "text/plain")})
        assert r.status_code == 400


class TestPlanRegression:
    """§12: a cost-model change must not silently alter which plan gets
    chosen. This pins the current answer for the fixture so a future edit
    to the constants has to be a deliberate one."""

    def _report(self, c):
        path = ROOT / "tests" / "fixtures" / "sample_4color.gcode"
        with path.open("rb") as f:
            return c.post("/api/preflight?mock=1",
                          files={"file": ("sample_4color.gcode", f,
                                          "text/plain")}).json()

    def test_the_fixture_plan_is_pinned(self, client):
        c, main, cfg, calls = client
        j = self._report(c)
        # Four colours, four heads: every colour gets its own head and
        # nothing swaps mid-print.
        assert j["plans"]["optimize"]["swaps"] == 0
        assert j["plans"]["layer"]["swaps"] == 0
        kinds = {e["kind"] for e in j["plans"]["optimize"]["timeline"]}
        assert kinds == {"first_load"}

    def test_the_slicer_base_time_is_read_not_invented(self, client):
        c, main, cfg, calls = client
        j = self._report(c)
        assert j["plans"]["slicer"]["estimate"]["base_s"] == 6772.0


class TestPrintControl:
    """§8/§13.5: the whitelist is the contract, and it has to hold against
    a stale page, a fat-fingered curl, or a slider that fires an
    out-of-range value on touch."""

    def call(self, c, verb, value=None):
        body = {"verb": verb}
        if value is not None:
            body["value"] = value
        return c.post("/api/print-control?mock=1", json=body)

    def test_speed_emits_m220(self, client):
        c, main, cfg, calls = client
        j = self.call(c, "speed", 120).json()
        assert j["script"] == "M220 S120" and j["applied"] == 120

    def test_flow_emits_m221(self, client):
        c, main, cfg, calls = client
        assert self.call(c, "flow", 110).json()["script"] == "M221 S110"

    def test_fan_percent_is_scaled_to_0_255(self, client):
        """The UI thinks in percent and M106 does not."""
        c, main, cfg, calls = client
        assert self.call(c, "fan", 100).json()["script"] == "M106 S255"
        assert self.call(c, "fan", 0).json()["script"] == "M106 S0"

    def test_out_of_range_is_clamped_not_silently_rejected(self, client):
        """Returning the applied value lets the UI snap back to the truth;
        a slider still showing 400 % is a slider that lies."""
        c, main, cfg, calls = client
        assert self.call(c, "speed", 4000).json()["applied"] == 300
        assert self.call(c, "speed", 1).json()["applied"] == 25
        assert self.call(c, "flow", 900).json()["applied"] == 125

    def test_a_missing_value_is_a_bad_request(self, client):
        c, main, cfg, calls = client
        assert self.call(c, "speed").status_code == 400

    def test_an_unknown_verb_is_refused(self, client):
        """The whitelist exists so a UI bug cannot emit arbitrary gcode."""
        c, main, cfg, calls = client
        assert self.call(c, "run_this_gcode", 1).status_code == 400

    def test_pause_resume_cancel_map_to_macros(self, client):
        c, main, cfg, calls = client
        assert self.call(c, "pause").json()["script"] == "PAUSE"
        assert self.call(c, "resume").json()["script"] == "RESUME"
        assert self.call(c, "cancel").json()["script"] == "CANCEL_PRINT"

    def test_a_babystep_is_capped_per_press(self, client):
        c, main, cfg, calls = client
        main._babystep_state.update({"job": None, "total": 0.0})
        j = self.call(c, "babystep", -5.0).json()
        assert j["applied"] == -0.05

    def test_the_cumulative_babystep_floor_holds_across_calls(self, client):
        """The situation where a user keeps pressing "down" is precisely
        the one where the next press gouges the plate."""
        c, main, cfg, calls = client
        main._babystep_state.update({"job": "j", "total": 0.0})
        for _ in range(10):
            self.call(c, "babystep", -0.05)
        assert main._babystep_state["total"] == pytest.approx(-0.5)
        j = self.call(c, "babystep", -0.05).json()
        assert j["ok"] is False and j["applied"] == 0.0
        assert "re-level" in j["detail"]

    def test_the_babystep_accumulator_resets_on_a_new_job(self, client):
        c, main, cfg, calls = client
        main._babystep_state.update({"job": "old-job", "total": -0.5})
        self.call(c, "babystep", -0.05)
        assert main._babystep_state["total"] == pytest.approx(-0.05)

    def test_mock_mode_never_contacts_moonraker(self, client):
        c, main, cfg, calls = client
        before = len(calls)
        self.call(c, "speed", 120)
        assert len(calls) == before

    def test_limits_are_advertised_to_the_ui(self, client):
        c, main, cfg, calls = client
        j = c.get("/api/print-control/limits").json()
        assert j["ranges"]["speed"] == {"min": 25.0, "max": 300.0}
        assert j["babystep"]["total"] == 0.5


class TestUploadAndPrint:
    """§9's g-code preview no longer uploads anywhere - it parses the file
    client-side and never touches Moonraker. This is what's left of the
    upload path: the ordinary "upload and print" action."""

    def test_it_prints(self, client, monkeypatch):
        c, main, cfg, calls = client
        seen = {}

        async def fake_upload(name, data, ctype, start_print):
            seen.update(start_print=start_print)
            return {"ok": True, "filename": name}

        monkeypatch.setattr(main, "_upload_to_moonraker", fake_upload)
        c.post("/api/upload-and-print",
               files={"file": ("a.gcode", b"G1 X1\n", "text/plain")})
        assert seen["start_print"] is True

    def test_a_non_gcode_upload_is_refused(self, client):
        c, main, cfg, calls = client
        r = c.post("/api/upload-and-print",
                   files={"file": ("a.txt", b"hi", "text/plain")})
        assert r.status_code == 400

    def test_a_traversal_filename_is_stripped_to_a_basename(self, client,
                                                            monkeypatch):
        """os.path.basename() strips the directory components; what matters
        is the upload never writes outside the returned filename."""
        c, main, cfg, calls = client

        async def fake_upload(name, data, ctype, start_print):
            return {"ok": True, "filename": name}

        monkeypatch.setattr(main, "_upload_to_moonraker", fake_upload)
        r = c.post("/api/upload-and-print",
                   files={"file": ("../../etc/passwd.gcode", b"hi",
                                   "text/plain")})
        assert r.status_code == 200
        assert r.json()["filename"] == "passwd.gcode"


class TestHistoryEndpoints:
    def test_mock_history_is_served(self, client):
        c, main, cfg, calls = client
        j = c.get("/api/history?mock=1").json()
        assert j["mock"] is True and len(j["jobs"]) == 3
        assert j["jobs"][0]["multiace"]["plan"] == "optimize"

    def test_a_mock_job_detail_is_served(self, client):
        c, main, cfg, calls = client
        j = c.get("/api/history/mock-job-1?mock=1").json()
        assert j["multiace"]["plan"] == "loadout"

    def test_an_unknown_job_is_a_404(self, client):
        c, main, cfg, calls = client
        assert c.get("/api/history/nope?mock=1").status_code == 404

    def test_editing_the_history_needs_debug_mode(self, client):
        """Destructive and irreversible - gated the same way persistent
        updates are."""
        c, main, cfg, calls = client
        assert c.request("DELETE", "/api/history/x").status_code == 403
        assert c.post("/api/history/clear").status_code == 403

    def test_reprint_starts_the_file_already_on_the_printer(self, client):
        """No preflight, no rewrite - just Moonraker's own print/start on
        whatever filename the history row carries."""
        c, main, cfg, calls = client
        r = c.post("/api/history/reprint", json={"filename": "vase.gcode"})
        assert r.status_code == 200
        assert r.json() == {"ok": True, "filename": "vase.gcode"}
        assert calls == ["/printer/print/start"]

    def test_reprint_needs_a_filename(self, client):
        c, main, cfg, calls = client
        r = c.post("/api/history/reprint", json={"filename": " "})
        assert r.status_code == 400
        assert calls == []

    def test_reprint_in_mock_mode_never_touches_moonraker(self, client):
        c, main, cfg, calls = client
        r = c.post("/api/history/reprint?mock=1", json={"filename": "vase.gcode"})
        assert r.status_code == 200
        assert r.json()["mock"] is True
        assert calls == []


class TestPrinterIdleGuard:
    """§13.4: applying an update mid-print aborts the job, cuts the heaters
    and leaves the nozzle set into cold plastic."""

    def _no_mock(self, main, monkeypatch):
        monkeypatch.setattr(main, "_mock_enabled", lambda req=None: False)

    def test_printing_blocks_an_update(self, client, monkeypatch):
        c, main, cfg, calls = client

        async def state():
            return "printing"

        self._no_mock(main, monkeypatch)
        monkeypatch.setattr(main, "printer_print_state", state)
        r = c.post("/api/update/apply")
        assert r.status_code == 409 and "printing" in r.json()["detail"]

    def test_paused_blocks_too(self, client, monkeypatch):
        c, main, cfg, calls = client

        async def state():
            return "paused"

        self._no_mock(main, monkeypatch)
        monkeypatch.setattr(main, "printer_print_state", state)
        assert c.post("/api/update/apply").status_code == 409

    def test_an_unrecognised_state_fails_closed(self, client, monkeypatch):
        """"Cannot tell" and "idle" are not the same answer."""
        c, main, cfg, calls = client

        async def weird():
            return "reticulating"

        self._no_mock(main, monkeypatch)
        monkeypatch.setattr(main, "printer_print_state", weird)
        r = c.post("/api/update/apply")
        assert r.status_code == 409
        assert "cannot determine" in r.json()["detail"]

    def test_idle_passes_the_guard_and_hits_the_debug_gate(self, client,
                                                           monkeypatch):
        c, main, cfg, calls = client

        async def state():
            return "standby"

        self._no_mock(main, monkeypatch)
        monkeypatch.setattr(main, "printer_print_state", state)
        r = c.post("/api/update/apply")
        # Past the print check, stopped by the persistent-updates gate.
        assert r.status_code == 409
        assert "Persistent updates" in r.json()["detail"]


class TestDebugModeFlag:
    """The web service runs AS ROOT on the only real deployment target
    (S98multiace-web's own comment: "Snapmaker U1 ships without sudo" -
    that is why it runs as root at all). /api/debug-mode/enable used to
    shell out to sudo unconditionally and hard-fail with "sudo not on
    PATH" on stock hardware, even though the process already had every
    permission it needed to touch the file directly. Direct access has to
    be tried first; sudo is the fallback for a hypothetical non-root
    deployment, not the primary path.
    """

    def test_enable_writes_the_flag_directly(self, client, tmp_path):
        c, main, cfg, calls = client
        flag = tmp_path / "debug-flag-direct"
        main._DEBUG_FLAG_PATH = flag
        r = c.post("/api/debug-mode/enable")
        assert r.status_code == 200
        assert r.json()["enabled"] is True
        assert flag.is_file()

    def test_enable_never_shells_out_when_direct_write_works(self, client,
                                                              tmp_path,
                                                              monkeypatch):
        """The common case (root, no sudo binary) must not even attempt the
        subprocess - that is exactly the call that fails on stock hardware."""
        c, main, cfg, calls = client
        main._DEBUG_FLAG_PATH = tmp_path / "debug-flag-no-sudo"

        async def boom(argv, timeout=5.0):
            raise AssertionError("sudo should not have been called")

        monkeypatch.setattr(main, "_sudo_run", boom)
        r = c.post("/api/debug-mode/enable")
        assert r.status_code == 200

    def test_disable_removes_the_flag_directly(self, client, tmp_path):
        c, main, cfg, calls = client
        flag = tmp_path / "debug-flag-disable"
        flag.touch()
        main._DEBUG_FLAG_PATH = flag
        r = c.post("/api/debug-mode/disable")
        assert r.status_code == 200
        assert r.json()["enabled"] is False
        assert not flag.exists()

    def test_disable_is_a_no_op_when_already_absent(self, client, tmp_path):
        c, main, cfg, calls = client
        main._DEBUG_FLAG_PATH = tmp_path / "debug-flag-absent"
        r = c.post("/api/debug-mode/disable")
        assert r.status_code == 200
        assert r.json()["stdout"] == "already disabled"

    def test_enable_falls_back_to_sudo_when_direct_write_is_denied(
            self, client, tmp_path, monkeypatch):
        """A hypothetical non-root deployment with a sudoers drop-in - the
        one case sudo genuinely exists for."""
        c, main, cfg, calls = client
        # A path INSIDE a file, not a directory: Path.touch() on it always
        # raises NotADirectoryError/OSError, on every OS, with no setup
        # step of its own to accidentally leave behind - unlike a
        # nonexistent parent directory, which a naive fallback can silently
        # create as a side effect and pollute a real path with.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        flag = blocker / "debug-flag"
        main._DEBUG_FLAG_PATH = flag

        async def fake_sudo(argv, timeout=5.0):
            return 0, "sudo touched it"

        monkeypatch.setattr(main, "_sudo_run", fake_sudo)
        r = c.post("/api/debug-mode/enable")
        assert r.status_code == 200
        assert r.json()["stdout"] == "sudo touched it"

    def test_enable_reports_both_failures_when_neither_path_works(
            self, client, tmp_path, monkeypatch):
        c, main, cfg, calls = client
        blocker = tmp_path / "blocker2"
        blocker.write_text("not a directory")
        main._DEBUG_FLAG_PATH = blocker / "debug-flag"

        async def fake_sudo(argv, timeout=5.0):
            return 127, "sudo not on PATH"

        monkeypatch.setattr(main, "_sudo_run", fake_sudo)
        r = c.post("/api/debug-mode/enable")
        assert r.status_code == 500
        detail = r.json()["detail"]
        assert "direct write failed" in detail
        assert "sudo touch also failed" in detail


class TestVirtualLoadoutStore:
    """Backend persistence for the frontend's what-if overlay (formerly
    localStorage-only, so it never survived a different browser/profile)."""

    def test_empty_by_default(self, client):
        c, main, cfg, calls = client
        assert c.get("/api/virtual-loadout").json() == {"enabled": False, "slots": []}

    def test_put_persists_to_disk(self, client):
        c, main, cfg, calls = client
        body = {"enabled": True, "slots": [
            {"ace": 0, "slot": 1, "material": "PLA", "color": "#ff0000"}]}
        r = c.put("/api/virtual-loadout", json=body)
        assert r.status_code == 200
        assert r.json()["enabled"] is True

        on_disk = json.loads(Path(main.VIRTUAL_LOADOUT_FILE).read_text())
        assert on_disk["enabled"] is True
        assert on_disk["slots"][0]["material"] == "PLA"

    def test_survives_a_fresh_read(self, client):
        c, main, cfg, calls = client
        body = {"enabled": True, "slots": [
            {"ace": 1, "slot": 2, "material": "PETG", "color": "#00ff00"}]}
        c.put("/api/virtual-loadout", json=body)
        assert c.get("/api/virtual-loadout").json() == body

    def test_delete_clears_it(self, client):
        c, main, cfg, calls = client
        c.put("/api/virtual-loadout", json={"enabled": True, "slots": [
            {"ace": 0, "slot": 0, "material": "PLA", "color": "#888888"}]})
        r = c.delete("/api/virtual-loadout")
        assert r.status_code == 200
        assert c.get("/api/virtual-loadout").json() == {"enabled": False, "slots": []}

    def test_corrupt_file_is_treated_as_empty(self, client):
        c, main, cfg, calls = client
        Path(main.VIRTUAL_LOADOUT_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(main.VIRTUAL_LOADOUT_FILE).write_text("{not json")
        assert c.get("/api/virtual-loadout").json() == {"enabled": False, "slots": []}


class TestPrintQueue:
    """Stage a preflight plan instead of printing it immediately: upload
    with print=false into multiace-queue/, keep a registry entry recording
    which physical ACE slots the plan depends on, and let the Queue tab
    launch or delete it later. See docs/plan for "Print Queue"."""

    def _seed_token(self, main, token, name="vase.gcode"):
        main._PREFLIGHT_DIR.mkdir(parents=True, exist_ok=True)
        src = ROOT / "tests" / "fixtures" / "sample_4color.gcode"
        (main._PREFLIGHT_DIR / f"{token}.gcode").write_text(
            src.read_text(encoding="utf-8"), encoding="utf-8")
        (main._PREFLIGHT_DIR / f"{token}.name").write_text(
            name, encoding="utf-8")

    def _run_stage_pipeline(self, main, monkeypatch, job_id, token,
                            resolved_slots, live_slots, name="vase.gcode"):
        """Drive _run_preflight_pipeline(stage=True) directly - staging a
        real file end-to-end through the HTTP layer would also need a live
        Moonraker for the state query, which unit tests don't have."""
        main._PREFLIGHT_JOBS[job_id] = {
            "stage": "queued", "percent": 0.0, "done": False, "error": None,
            "filename": name, "mode": "slicer", "ts": 0.0,
        }
        seen = {}

        def fake_rewrite(pp, *, src_path, **kw):
            return src_path, resolved_slots

        async def fake_upload(name_, data, ctype, start_print, dest_path=None):
            seen.update(name=name_, start_print=start_print, dest_path=dest_path)
            return {"ok": True, "filename": name_, "moonraker": {"item": {}}}

        monkeypatch.setattr(main.preflight_core, "rewrite_pipeline", fake_rewrite)
        monkeypatch.setattr(main, "_upload_to_moonraker", fake_upload)
        monkeypatch.setattr(main, "_live_slots_async",
                            lambda: _async_value(live_slots))
        monkeypatch.setattr(main, "_query_state_gated", lambda: _async_value({}))
        monkeypatch.setattr(main, "_parse_state", lambda s: {})
        asyncio.run(main._run_preflight_pipeline(
            job_id, token, "slicer", name, stage=True))
        return seen

    def _seed_entry(self, main, id_, **overrides):
        entry = {
            "id": id_, "filename": f"{id_}.gcode",
            "relpath": f"multiace-queue/{id_}-{id_}.gcode",
            "mode": "slicer", "head_plan": None, "staged_at": 1.0,
            "referenced_slots": [], "slot_snapshot": {},
        }
        entry.update(overrides)
        main._queue[id_] = entry
        return entry

    def test_stage_is_refused_in_mock_mode(self, client):
        c, main, cfg, calls = client
        r = c.post("/api/preflight/print?mock=1",
                   json={"token": "0" * 32, "mode": "slicer", "stage": True})
        assert r.status_code == 503
        assert "stage" in r.json()["detail"]

    def test_register_is_refused_in_mock_mode(self, client):
        c, main, cfg, calls = client
        r = c.post("/api/queue/register?mock=1", json={
            "id": "a" * 32, "filename": "x.gcode",
            "relpath": "multiace-queue/" + "a" * 32 + "-x.gcode",
            "mode": "slicer",
        })
        assert r.status_code == 503

    def test_staging_uploads_with_print_false_into_the_queue_folder(
            self, client, monkeypatch):
        c, main, cfg, calls = client
        token, job_id = "a" * 32, "b" * 32
        self._seed_token(main, token)
        seen = self._run_stage_pipeline(
            main, monkeypatch, job_id, token,
            [{"ace": 0, "slot": 1}],
            [{"ace": 0, "slot": 1, "material": "PLA", "color": "#c21b17"}])
        assert seen["start_print"] is False
        assert seen["dest_path"] == "multiace-queue"
        assert job_id in main._queue
        entry = main._queue[job_id]
        assert entry["referenced_slots"] == [{"ace": 0, "slot": 1}]
        assert entry["slot_snapshot"] == {
            "0:1": {"material": "PLA", "color": "#c21b17"}}
        assert main._PREFLIGHT_JOBS[job_id]["queued"] is True
        assert main._PREFLIGHT_JOBS[job_id]["queue_id"] == job_id

    def test_registry_persists_atomically_to_disk(self, client, monkeypatch):
        c, main, cfg, calls = client
        token, job_id = "c" * 32, "d" * 32
        self._seed_token(main, token)
        self._run_stage_pipeline(
            main, monkeypatch, job_id, token, [{"ace": 0, "slot": 0}],
            [{"ace": 0, "slot": 0, "material": "PETG", "color": "#111111"}])
        on_disk = json.loads(Path(main.QUEUE_FILE).read_text(encoding="utf-8"))
        assert on_disk[job_id]["filename"] == "vase.gcode"
        assert on_disk[job_id]["slot_snapshot"]["0:0"]["material"] == "PETG"

    def test_staging_is_refused_once_the_queue_is_full(self, client, monkeypatch):
        c, main, cfg, calls = client
        monkeypatch.setattr(main, "QUEUE_MAX", 1)
        self._seed_entry(main, "existing")
        r = c.post("/api/preflight/print",
                   json={"token": "e" * 32, "mode": "slicer", "stage": True})
        assert r.status_code == 409
        assert "existing" in main._queue  # untouched

    def test_list_flags_drift_when_a_slot_changed(self, client):
        c, main, cfg, calls = client
        # tests/fixtures/mock_state.json's ACE 0 / slot 0 is PLA #d94040 - a
        # snapshot recorded as PETG can never match it.
        self._seed_entry(main, "j1",
                         referenced_slots=[{"ace": 0, "slot": 0}],
                         slot_snapshot={"0:0": {"material": "PETG",
                                                "color": "#000000"}})
        j = c.get("/api/queue?mock=1").json()
        assert j["mock"] is True
        row = next(r for r in j["jobs"] if r["id"] == "j1")
        assert row["drift"]["changed"] is True
        assert row["drift"]["slots"][0]["was"] == {
            "material": "PETG", "color": "#000000"}

    def test_list_reports_no_drift_when_the_snapshot_still_matches(self, client):
        c, main, cfg, calls = client
        self._seed_entry(main, "j2",
                         referenced_slots=[{"ace": 0, "slot": 0}],
                         slot_snapshot={"0:0": {"material": "PLA",
                                                "color": "#d94040"}})
        j = c.get("/api/queue?mock=1").json()
        row = next(r for r in j["jobs"] if r["id"] == "j2")
        assert row["drift"]["changed"] is False

    def test_list_flags_a_file_missing_on_the_printer(self, client, monkeypatch):
        c, main, cfg, calls = client
        self._seed_entry(main, "j3")

        async def fake_get(path):
            if path == "/server/files/list?root=gcodes":
                return {"result": [{"path": "other.gcode"}]}
            raise AssertionError(f"unexpected GET {path}")

        monkeypatch.setattr(main, "_live_slots_async",
                            lambda: _async_value([]))
        monkeypatch.setattr(main, "_mr_get", fake_get)
        j = c.get("/api/queue").json()
        row = next(r for r in j["jobs"] if r["id"] == "j3")
        assert row["missing_on_printer"] is True

    def test_list_degrades_gracefully_when_moonraker_is_unreachable(
            self, client, monkeypatch):
        c, main, cfg, calls = client
        self._seed_entry(main, "j4")

        async def boom(path):
            raise httpx.ConnectError("no route")

        monkeypatch.setattr(main, "_live_slots_async",
                            lambda: _async_value([]))
        monkeypatch.setattr(main, "_mr_get", boom)
        r = c.get("/api/queue")
        assert r.status_code == 200
        row = next(j for j in r.json()["jobs"] if j["id"] == "j4")
        assert row["missing_on_printer"] is False

    def test_launch_starts_the_staged_file_and_drops_it_from_the_queue(
            self, client):
        c, main, cfg, calls = client
        self._seed_entry(main, "j5")
        r = c.post("/api/queue/j5/launch")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "filename": "j5.gcode"}
        assert calls == ["/printer/print/start"]
        assert "j5" not in main._queue

    def test_launch_in_mock_mode_never_touches_moonraker(self, client):
        c, main, cfg, calls = client
        self._seed_entry(main, "j6")
        r = c.post("/api/queue/j6/launch?mock=1")
        assert r.status_code == 200
        assert r.json()["mock"] is True
        assert calls == []
        assert "j6" in main._queue  # a pretend launch leaves it staged

    def test_launch_of_an_unknown_id_is_404(self, client):
        c, main, cfg, calls = client
        assert c.post("/api/queue/nope/launch").status_code == 404

    def test_launch_failure_leaves_the_job_staged_for_a_retry(
            self, client, monkeypatch):
        c, main, cfg, calls = client
        self._seed_entry(main, "j7")

        async def fail_post(path, body=None, timeout=30.0):
            req = httpx.Request("POST", "http://printer" + path)
            resp = httpx.Response(500, text="klippy busy", request=req)
            raise httpx.HTTPStatusError("500", request=req, response=resp)

        monkeypatch.setattr(main, "_mr_post", fail_post)
        r = c.post("/api/queue/j7/launch")
        assert r.status_code == 500
        assert "j7" in main._queue

    def test_delete_removes_the_moonraker_file_and_the_registry_row(
            self, client, monkeypatch):
        c, main, cfg, calls = client
        self._seed_entry(main, "j8")
        deleted = {}

        async def fake_delete(path):
            deleted["path"] = path

        monkeypatch.setattr(main, "_mr_delete", fake_delete)
        r = c.delete("/api/queue/j8")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "deleted": "j8"}
        assert deleted["path"] == "/server/files/gcodes/multiace-queue/j8-j8.gcode"
        assert "j8" not in main._queue

    def test_delete_is_idempotent_when_moonraker_already_lost_the_file(
            self, client, monkeypatch):
        c, main, cfg, calls = client
        self._seed_entry(main, "j9")

        async def fake_delete(path):
            req = httpx.Request("DELETE", "http://printer" + path)
            resp = httpx.Response(404, text="not found", request=req)
            raise httpx.HTTPStatusError("404", request=req, response=resp)

        monkeypatch.setattr(main, "_mr_delete", fake_delete)
        r = c.delete("/api/queue/j9")
        assert r.status_code == 200
        assert "j9" not in main._queue

    def test_delete_of_an_unknown_id_is_a_no_op(self, client):
        c, main, cfg, calls = client
        r = c.delete("/api/queue/nope")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "deleted": "nope"}


def _raw_ace_status(*, slot0_status="ready", slot0_gate=1, slot0_rfid=0,
                    slot0_color=None, remember_filament=None):
    """Minimal raw moonraker-shaped status dict for _parse_state(): one
    ACE, four slots, slot 0 is the one under test."""
    def _slot(idx, status="empty", rfid=0, color=None):
        return {"index": idx, "status": status, "rfid": rfid,
                "material": "PLA" if status != "empty" else "",
                "brand": "Generic" if status != "empty" else "",
                "sku": "", "subtype": "",
                "color": color if color is not None else [0, 0, 0]}

    gate_status = [slot0_gate, 0, 0, 0]
    ace_obj = {
        "device_count": 1, "active_device": 0,
        "head_source": {}, "head_manual": {}, "head_feeder": {},
        "head_feeder_combo": {}, "head_reader_spool": {},
        "aces": [{
            "idx": 0, "connected": True, "protocol": "v2",
            "status": "ready", "temp": 25,
            "gate_status": gate_status,
            "slots": [
                _slot(0, slot0_status, slot0_rfid,
                      slot0_color or [217, 64, 64]),
                _slot(1), _slot(2), _slot(3),
            ],
        }],
        "gate_status": gate_status,
        "pickup_cleaning": False, "confirm_commands": False,
        "airprint_detection": False, "quad_replenish": False,
        "purge_matrix": True, "quad_first": False,
    }
    if remember_filament is not None:
        ace_obj["remember_filament"] = remember_filament
    return {
        "ace": ace_obj,
        "filament_feed left": {}, "filament_feed right": {},
        "ace_bg_swap": {}, "ace_tipform": {},
        "print_task_config": {},
        "save_variables": {"variables": {}},
        "print_stats": {"state": "standby"},
        "idle_timeout": {"state": "Idle"},
    }


class TestRememberFilament:
    """Slot 0's manual override (color/material) should survive a
    physical eject when ace.remember_filament is on (the default), and
    keep being dropped when it's explicitly off - matching the toggle
    added for auto-reassigning a slot's identity on reinsertion."""

    def _seed_override(self, main):
        key = "0_0"
        main._slot_overrides[key] = {
            "ace": 0, "slot": 0, "material": "PLA",
            "brand": "Generic", "subtype": "", "color": "#3f7fd0",
        }
        main._save_overrides_to_disk()

    def test_defaults_to_true_when_firmware_omits_the_field(self, app_env):
        main, cfg, calls = app_env
        parsed = main._parse_state(_raw_ace_status())
        assert parsed["remember_filament"] is True

    def test_reports_false_when_firmware_says_so(self, app_env):
        main, cfg, calls = app_env
        parsed = main._parse_state(_raw_ace_status(remember_filament=False))
        assert parsed["remember_filament"] is False

    def test_override_survives_eject_when_remembering(self, app_env):
        main, cfg, calls = app_env
        self._seed_override(main)

        main._parse_state(_raw_ace_status(
            slot0_status="empty", slot0_gate=0, remember_filament=True))
        import time
        time.sleep(main.EJECT_DEBOUNCE_S + 0.1)
        main._parse_state(_raw_ace_status(
            slot0_status="empty", slot0_gate=0, remember_filament=True))

        assert "0_0" in main._slot_overrides

        parsed = main._parse_state(_raw_ace_status(
            slot0_status="ready", slot0_gate=1, remember_filament=True))
        slot0 = parsed["aces"][0]["slots"][0]
        assert slot0["material"] == "PLA"
        assert slot0["color"] == "#3f7fd0"

    def test_override_is_dropped_on_eject_when_not_remembering(self, app_env):
        main, cfg, calls = app_env
        self._seed_override(main)

        main._parse_state(_raw_ace_status(
            slot0_status="empty", slot0_gate=0, remember_filament=False))
        import time
        time.sleep(main.EJECT_DEBOUNCE_S + 0.1)
        main._parse_state(_raw_ace_status(
            slot0_status="empty", slot0_gate=0, remember_filament=False))

        assert "0_0" not in main._slot_overrides

    def test_fresh_rfid_read_clears_a_stale_override(self, app_env):
        main, cfg, calls = app_env
        self._seed_override(main)

        # First poll never saw a chip (rfid=0) - override still governs.
        parsed = main._parse_state(_raw_ace_status(
            slot0_status="ready", slot0_gate=1, slot0_rfid=0,
            remember_filament=True))
        assert parsed["aces"][0]["slots"][0]["color"] == "#3f7fd0"

        # A different, chipped spool gets read into the same slot.
        parsed = main._parse_state(_raw_ace_status(
            slot0_status="ready", slot0_gate=1, slot0_rfid=2,
            slot0_color=[0, 255, 0], remember_filament=True))
        assert "0_0" not in main._slot_overrides
        slot0 = parsed["aces"][0]["slots"][0]
        assert slot0["source"] == "rfid"
        assert slot0["color"] == "#00ff00"

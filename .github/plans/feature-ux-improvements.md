# multiACE UI & Control Flow Improvements Plan

**Status:** Draft  
**Date:** 2026-08-08  
**Scope:** Four UX improvements + filament mechanics review

---

## Overview

This plan addresses four high-impact UX friction points in multiACE:
1. Unified dashboard (webcam + console always visible)
2. Configuration apply flow with automated reboots where needed
3. Firmware 1.5.2 compatibility verification
4. Automatic retry for filament load failures

Plus: Review filament swap mechanics to reduce friction & binding.

---

## 1. Unified Dashboard: Stream Webcam & Console to `/multiace`

### Problem
Currently, the homepage (`/`) and the multiACE control panel (`/multiace`) are separate pages. Users must switch contexts to monitor webcam/console while managing loads/swaps, breaking focus during print pauses.

### Solution
Embed both webcam stream and console/log tail into the `/multiace` panel as collapsible sidebars or dockable regions, eliminating context switching.

### Implementation Details

#### 1.1 Webcam Integration
- **Current:** Webcam streaming is delegated to Moonraker/crowsnest via camera registration (`POST /api/fluidd-camera`). The multiACE panel uses iframe (`/multiace/?panel=1`) to embed itself in Fluidd.
- **Approach:** 
  - Add a new endpoint `GET /api/webcam/stream` or proxy Moonraker's stream URL (check Moonraker docs for camera MJPEG endpoint format, likely `/webcam/?action=stream`)
  - Embed as `<img src="/api/webcam/stream">` (MJPEG) or `<video>` element in the Vue panel
  - Alternatively, check if Moonraker exposes camera list API (`/server/webcams/list`) and fetch the stream URL from there
  - **Risk:** Crowsnest may not be running if printer uses USB camera only; handle gracefully with "Camera unavailable" state

#### 1.2 Console/Log Streaming
- **Current:** WebSocket endpoint (`@app.websocket("/ws")`, main.py:2608-2656) pushes `state` snapshots every ~1s but does not stream console/log output (only `gcode_error` notifications).
- **Approach:**
  - Extend WS `/ws` to include a `console` message type with buffered log lines (last 50-100 lines)
  - Tap into Klipper's log output via Moonraker's `/server/gcode_store` endpoint (fetches recent G-code history) or `/printer/gcode_store` (if available)
  - Alternatively, read from printer's log file if accessible (e.g., `/tmp/klipper.log`) via a new endpoint `GET /api/console-logs?lines=50&follow=true`
  - Send new log entries to WS clients as they arrive (or batch every 250ms)
  - **Risk:** Log file I/O on the Pi can be slow; keep follow-polling lightweight (e.g., 500ms interval)

#### 1.3 UI Layout
- Add a **two-column or three-pane layout** to `/multiace` Vue app:
  - **Left:** ACE dashboard (slots, status, queue) — existing content
  - **Right sidebar (collapsible):** Webcam stream + console log feed
  - **Alternative:** Tab bar: "Dashboard" | "Webcam" | "Console"
- Use CSS `grid` or flexbox for responsive layout (collapse sidebars on mobile)
- Add **"follow console" toggle** to auto-scroll latest logs

#### 1.4 Testing (Visual)
- **Dev environment:** Flask/FastAPI serves frontend dev bundle (or run Vue dev server separately)
- **Script:** `scripts/run-dev-ui.sh` (or `.ps1` for Windows)
  - Detects if printer/Moonraker is reachable; if not, serves mocked API responses
  - Mock data: sample state, fake MJPEG stream, sample console logs
  - Opens browser to `http://localhost:7126/?panel=1` (or dev server port)
  - **See:** Testing section below for more detail

---

## 2. Configuration Apply & Reboot Flow

### Problem
When users change settings (load length, mode, dryer temp, etc.) and save, they must manually reboot to activate changes. Some changes (mode switches) are forced-reboot; others (config values) silently take effect or require unclear restart behavior.

### Solution
Provide an explicit "Apply Changes" flow with:
- Detection of which changes require a reboot
- Automatic optional restart of Klipper/printer with a single button
- Clearer messaging on what happens after apply

### Implementation Details

#### 2.1 Change-Type Taxonomy
Audit all config parameters in the multiACE section and categorize:

| Change Type | Examples | Requires Reboot? | Current Behavior |
|-------------|----------|------------------|------------------|
| **Live** | dryer temp, slot speed limits | No | Applies immediately, saved to EEPROM |
| **Klipper restart needed** | load length, max velocity, slot mapping | Yes | Saved but ignored until restart |
| **Mode switch** | normal ↔ multi/head | Yes + sh script | Triggers ace_mode_switch.sh, then fails with message |
| **Firmware/hardware config** | ACE unit type, protocol version | Yes | May need printer reboot |

- **Source files to audit:**
  - `multiace/klipper/extras/ace.py` — config loading (look for `config.get_*` calls around init)
  - `multiace/web/backend/main.py:1655-1690` (`update_config` endpoint) — currently has optional `restart_klipper` flag
  - `multiace/config/extended/*.cfg` — config templates

#### 2.2 Backend Changes

**`main.py` Config Endpoints:**
- Update `PUT /api/config` to accept a `restart_behavior` enum:
  ```python
  class ConfigUpdateRequest:
      content: str
      base_sha1: str
      restart_behavior: Literal["none", "klipper_restart", "printer_reboot"]
  ```
- Keep `restart_klipper` for backward compatibility (default to "klipper_restart" if set)
- For mode switch detection: after writing config, check if `[ace_tipform]` or mode-related params changed → force `restart_behavior="printer_reboot"` with user prompt
- **Implement change detection:**
  - Parse old vs new config (extract ACE section)
  - Compare critical keys (mode, load_length, max_velocity, etc.)
  - Return a "summary of changes" response including what will restart
  ```python
  {
      "applied": True,
      "changes": ["load_length: 100→120", "mode unchanged"],
      "restart_required": "printer_reboot",  // or "klipper_restart" or "none"
      "restart": {... moonraker response if restart was triggered ...}
  }
  ```

#### 2.3 Frontend Changes

**`multiace/web/frontend/app.js` (Vue):**
- Add a **"Applied Changes" modal/overlay** that appears after config save:
  - Shows what was changed (diff summary)
  - States what restart is needed (or "none, applied immediately")
  - **Button options:**
    - ✅ "Restart Now" (triggers reboot/Klipper restart)
    - ⏰ "Restart Later" (user manually reboots when ready)
    - ❌ "Cancel" (reverts to backup, or just closes if already saved)
  - Auto-dismiss if no restart needed
- Update the config editor UI to show a **"Apply & Restart" mega-button** that:
  - Saves the config
  - Awaits the summary response
  - Shows the modal
  - Handles restart feedback (shows spinner during reboot, reconnects when printer comes back)
- **Reconnection logic:** After `restart_klipper` or `printer_reboot`, WebSocket will close; auto-reconnect every 2s until printer is back (max 5min timeout)

#### 2.4 Mode Switch Special Case
Currently `cmd_ACE_RUN_MODE_SWITCH` (ace.py:9080-9151) always forces a reboot. We can improve this:
- If switching between `multi` ↔ `head` (both ACE-enabled modes): **no reboot needed** (already live at line 9092-9112)
- If switching to/from `normal` mode: **reboot required** (keep current behavior, but now communicated clearly via API)
- Update UI to show "This will reboot your printer" before confirming mode switch

#### 2.5 Testing
- **Unit:** Mock a config change, verify the response includes correct `restart_required` value
- **Integration:** Apply a live config, verify it takes effect immediately and WS state reflects it
- **E2E:** Apply a restart-needed config (e.g., load_length), hit "Restart Now", verify printer comes back online
- **Visual:** Use dev UI to test the modal appearance and button flows

---

## 3. Firmware 1.5.2 Compatibility

### Problem
README mentions "Prepared for 1.5.1" (line 35), and the latest version is 1.5.2 (line 15). However, the code has no version parser or compatibility table. A hard version check was started at `ace.py:5252` ("Stock 1.5.2 only...") but never completed.

### Solution
Add firmware version detection and validation, with a clear compatibility matrix in both code and docs.

### Implementation Details

#### 3.1 Firmware Version Detection
- **Source:** Snapmaker U1 firmware version is sent by the printer over the serial port during Klipper boot (captured in logs as a handshake)
- **Implementation options:**
  1. **Via Klipper state:** Query `/printer` object for firmware version field (check Moonraker API docs; some printers expose this via `printer.system_info.firmware_version`)
  2. **Via multiACE state:** Add to `ace.py` a method to extract firmware from printer state (similar to how `_ptc_spool_id_for` checks version context)
  3. **Manual config:** Let user specify in `[ace]` section: `firmware_version: 1.5.2` (fallback if auto-detect fails)

#### 3.2 Compatibility Mapping
Define in code (or a JSON file in `multiace/config/`):
```python
FIRMWARE_COMPAT = {
    "1.4.x": {"status": "unsupported", "reason": "old protocol"},
    "1.5.0": {"status": "supported", "known_issues": ["RFID lag"]},
    "1.5.1": {"status": "supported", "known_issues": ["RFID lag"]},
    "1.5.2": {"status": "supported", "known_issues": []},
    "1.1.31": {"status": "supported", "device": "ACE Pro 2", "known_issues": []},
}
```

#### 3.3 Validation Points
- **On startup:** Log firmware version detected (info level if compatible, warning if untested)
- **On /api/version endpoint:** Return firmware version + compatibility status
  ```python
  {
      "firmware_version": "1.5.2",
      "compatibility": "supported",
      "known_issues": []
  }
  ```
- **On critical operations:** If firmware is unsupported, warn the user but allow override (e.g., "firmware untested with this version; continue anyway?")

#### 3.4 Testing
- **Verify 1.5.2 works:** Run existing test suite against 1.5.2 firmware (if available in test env)
- **Add version check test:** `tests/unit/test_firmware_compat.py` — mock various firmware versions and verify correct compat status
- **Docs:** Update README "Supported Firmware" section to explicitly list 1.5.2 (and any others) with known issues

---

## 4. Automatic Retry for Filament Load Failures

### Problem
When a filament load fails (jam, slot error), the UI pauses and waits for manual user intervention. If the user is away from the printer, they cannot retry. Filament load failures are recoverable ~80% of the time with a simple retry.

### Solution
Implement automatic retry mechanism (up to N attempts, configurable) before pausing for manual intervention, with UI visibility into attempts and ability to override.

### Implementation Details

#### 4.1 Retry Configuration
Add to `[ace]` config section (or per-slot):
```ini
[ace]
# Auto-retry filament load failures
filament_load_max_auto_retries: 3  # 0 = off, 1-10 = retries before pause
filament_load_retry_delay_ms: 1000  # delay between retries
```

#### 4.2 Backend Changes (ace.py)
- **Current state:** `ace.py:3418-3468` already has retry scaffolding for `_fa_start` (feed assist), but it caps at `max_retries = self._fa_start_retries` and logs attempts
- **Enhancement:** 
  - Extend retry logic to all filament load/unload operations (not just feed assist)
  - Add a **`filament_load_max_auto_retries`** config parameter (separate from existing feed assist retries)
  - When a load error occurs (lines 3438-3446 show `msg in ('forbidden', 'error_2')`), check if auto-retries are exhausted:
    - If retries remain: automatically re-invoke the load command after `filament_load_retry_delay_ms`
    - Log each retry attempt with timestamp
    - On final failure (or when retries exhausted): emit a **pausable error** (like jam handling does at line 8125, which pauses the print) instead of hard aborting

#### 4.3 Frontend Changes (Vue)
- **Display retry state in the UI:**
  - When a load fails, show a **"Retrying... (2/3 attempts)"** status in the ACE dashboard
  - Add a manual **"Retry Now"** button (skip delay, retry immediately)
  - Add a **"Stop Retries & Pause"** button (give up early, pause print for manual intervention)
- **Toasts/notifications:**
  - "Slot 0 load failed. Retrying (1/3)..." (info)
  - "Slot 0 load failed after 3 attempts. Printer paused." (warning)
- **WebSocket:** Extend `@app.websocket("/ws")` to include retry attempt counters in the state snapshot:
  ```python
  {
      "ace_status": [...],
      "retry_state": {
          "slot": 0,
          "ace_idx": 0,
          "attempt": 2,
          "max_attempts": 3,
          "next_retry_ms": 800,  # countdown to next attempt
      }
  }
  ```

#### 4.4 Config Recommendations
- **Default:** `filament_load_max_auto_retries: 3` (reasonable middle ground; most jams clear on 2nd-3rd try)
- **Tunable:** Let users configure per ACE or globally via config editor
- **Documentation:** Add a note in README/config templates explaining the feature and when to increase/decrease

#### 4.5 Testing
- **Unit:** Mock a load failure, verify retry scheduler is invoked N times
- **Integration:** Send a failed load command, monitor WebSocket for retry_state updates, verify it stops after max_attempts
- **Visual:** Use dev UI to simulate a failure and watch the retry counter tick down

---

## 5. Filament Swap Mechanics Review

### Problem
Users report occasional jams and binding during filament swaps, suggesting friction in the mechanical design. Physics optimizations could reduce failures and make the swap process more robust.

### Solution
Review filament path geometry, surface friction, and load sequencing to identify improvements.

### Investigation Areas

#### 5.1 Mechanical Path Review
- **Filament guide tubes:** Check for:
  - **Rough inner surfaces** (scratches, burrs) — smooth or replace
  - **Misalignment:** Ensure tubes are perfectly coaxial; any offset causes binding
  - **Diameter variance:** Measure ID of tubes; confirm they match filament diameter + clearance (typically 1.75mm filament needs ~1.9-2mm ID, 2.85mm needs ~3.0mm ID)
  - **Bends & stress points:** Sharp 90° bends cause friction; consider radius elbows or guide bushings
- **Pressure-sensitive components:**
  - Loadhead (gripper pressure): if too high, friction increases; if too low, slipping occurs
  - Measure gripper force with load cell if possible
  - Check for wear/flatness of gripper pads (should have even contact)

#### 5.2 Filament Path Dynamics
- **Load sequencing:** Does the firmware:
  - Pre-heat the nozzle before inserting filament? (helps reduce stiction)
  - Use a "pre-press" / "seat press" before full load? (already mentioned in README line 27-28 as 220°C + seat press improvement)
  - Gradually increase drive wheel speed, or ramp drive pressure?
- **Unload sequencing:** 
  - Retract at constant speed or ramped?
  - Any bleed-back time after stopping?
  - Cooling strategy (let nozzle cool before pulling, or pull hot)?

#### 5.3 Drive Wheel & Filament Contact
- **Hobbed bolt or drive gear wear:** Over time, grooves can become dull or misaligned
  - Inspect for wear patterns
  - Clean filament dust/residue from hobbed surfaces
- **Filament diameter variance:** Some brands are inconsistent (1.7-1.8mm nominal 1.75mm); check if firmware can auto-detect or compensate
- **Drive pressure (pinch force):** Tune for minimum necessary pressure (trade-off: too low = slip, too high = grinding)

#### 5.4 Temperature & Material Properties
- **Swap temperature:** README now says 220°C for PLA (up from 200°C), which helps. Consider per-material profiles:
  - PLA: 220-230°C (less likely to stick)
  - PETG: 240-250°C
  - TPU/Flex: 215-225°C (more prone to binding)
- **Cooling after unload:** Does firmware wait for nozzle cool to avoid filament sticking to walls?
- **Lubrication:** Some users apply dry silicone spray to filament before loading; document if this helps (or hurts)

#### 5.5 Recommended Improvements (Priority Order)
1. **Mechanical audit (high impact):** Inspect physical filament path, tubes, gripper pads for wear/damage
2. **Load sequencing optimization (medium impact):** Verify pre-heat → seat press → full load sequence is working as intended
3. **Per-material temperature profiles (medium impact):** Allow users to configure swap temps per filament type
4. **Pressure tuning (medium impact):** Document hobbed bolt inspection & replacement intervals
5. **Cooldown flow (low impact but quick):** Ensure nozzle cools sufficiently before long unload pulls

#### 5.6 Testing & Documentation
- **Create a filament swap troubleshooting guide:**
  - "If you get jams during load → Check tube alignment, run dry test, reduce drive speed if variable"
  - "If filament sticks after unload → Increase nozzle cooldown time, check grip pressure"
- **Test with multiple filament brands:** Document which brands/diameters are most problematic
- **Add telemetry:** Log swap duration, retry counts, error codes to identify patterns over time (already exists for feed assist; extend to load/unload)

---

## 6. Visual Dev Environment Setup

### Problem
Developing and testing UI changes requires either a running printer (expensive in time) or heavy mocking. A lightweight local dev environment with mocked API responses would let designers/devs iterate quickly.

### Solution
Provide a `run-dev-ui.sh` / `run-dev-ui.ps1` script that:
- Checks if Moonraker is reachable; if yes, uses real data; if no, loads mock data
- Serves the Vue frontend with hot-reload (if using dev server) or just static files
- Opens browser to the panel
- Provides a "toggle mock mode" button in the UI to switch between real/mock without restarting

### Implementation Details

#### 6.1 Dev Server Script
**File: `scripts/run-dev-ui.sh` (Bash) / `scripts/run-dev-ui.ps1` (PowerShell)**

```bash
#!/bin/bash
# run-dev-ui.sh — Start multiACE dev UI with mocked or real Moonraker

set -e

PORT=7126
FRONTEND_DIR="./multiace/web/frontend"
MOCK_DATA_DIR="./tests/fixtures"  # or create one

# Check if Moonraker is reachable
MOONRAKER_URL="${MOONRAKER_URL:-http://127.0.0.1:7125}"
if curl -s "$MOONRAKER_URL/printer/info" > /dev/null 2>&1; then
    echo "✓ Moonraker found at $MOONRAKER_URL — using real data"
    MOCK_MODE=false
else
    echo "✗ Moonraker not reachable — using mock data"
    MOCK_MODE=true
fi

# Launch FastAPI server with mock flag
export MULTIACE_MOCK_MODE=$MOCK_MODE
export MOONRAKER_URL=$MOONRAKER_URL
cd multiace/web/backend
python3 -m uvicorn main:app --host 127.0.0.1 --port $PORT --reload &
SERVER_PID=$!

# Wait for server to start
sleep 2

# Open browser
if command -v xdg-open &> /dev/null; then
    xdg-open "http://127.0.0.1:$PORT/?panel=1"  # Linux
elif command -v open &> /dev/null; then
    open "http://127.0.0.1:$PORT/?panel=1"  # macOS
elif command -v start &> /dev/null; then
    start "http://127.0.0.1:$PORT/?panel=1"  # Windows
fi

echo "Dev server running at http://127.0.0.1:$PORT/?panel=1"
echo "Press Ctrl+C to stop"
wait $SERVER_PID
```

**File: `scripts/run-dev-ui.ps1` (PowerShell for Windows)**
```powershell
# run-dev-ui.ps1

$Port = 7126
$MoonrakerUrl = $env:MOONRAKER_URL -or "http://127.0.0.1:7125"

# Check if Moonraker is reachable
try {
    $response = Invoke-WebRequest -Uri "$MoonrakerUrl/printer/info" -TimeoutSec 2
    Write-Host "✓ Moonraker found — using real data"
    $MockMode = $false
} catch {
    Write-Host "✗ Moonraker not reachable — using mock data"
    $MockMode = $true
}

# Set environment and launch server
$env:MULTIACE_MOCK_MODE = $MockMode
$env:MOONRAKER_URL = $MoonrakerUrl

Push-Location .\multiace\web\backend
Start-Process -NoNewWindow python3 -ArgumentList "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", $Port, "--reload"
Pop-Location

Start-Sleep -Seconds 2
Start-Process "http://127.0.0.1:$Port/?panel=1"

Write-Host "Dev server running at http://127.0.0.1:$Port/?panel=1"
Write-Host "Press Ctrl+C to stop"

Read-Host "Press Enter to stop"
```

#### 6.2 Mock Data
Create **`tests/fixtures/mock_state.json`** with representative API responses:
```json
{
  "ace_status": [
    {
      "idx": 0,
      "connected": true,
      "firmware": "1.5.2",
      "slots": [
        {"slot": 0, "color": "red", "material": "PLA", "status": "ready"},
        {"slot": 1, "color": "blue", "material": "PLA", "status": "empty"}
      ]
    }
  ],
  "print_status": {
    "state": "printing",
    "current_file": "benchy.gcode",
    "progress": 0.45
  }
}
```

#### 6.3 Backend Mock Support
Update **`main.py`** to handle mock mode:
```python
MOCK_MODE = os.getenv("MULTIACE_MOCK_MODE", "").lower() == "true"

@app.get("/api/state")
async def get_state():
    if MOCK_MODE:
        return json.load(open("../../tests/fixtures/mock_state.json"))
    # ... existing real code
```

#### 6.4 UI Mock Toggle Button
Add a small **"Debug Panel"** in the Vue UI (visible when `?debug=1` in URL):
- Toggle: "Use Mock Data" / "Use Real Data" (re-fetches from API)
- Debug info: Show WebSocket connection status, last message timestamp
- Simulated events: Buttons to inject mock "load failure", "print paused", etc. into WS for testing retry flows

#### 6.5 Testing
- **Manual:** Run `./scripts/run-dev-ui.sh` or `.ps1`, verify browser opens, mock data loads
- **Visual:** Iterate on UI changes, test responsive layout, verify new console sidebar appears
- **Accessibility:** Zoom to 150%, verify layout doesn't break; test with dark mode toggle

---

## 7. Implementation Roadmap

### Phase 1: Foundation (Week 1)
- [ ] Firmware version detection & compat table (3.1-3.3)
- [ ] Dev UI script & mock data (6.1-6.2)
- [ ] Config change taxonomy (2.1)
- [ ] **Testing:** Verify dev UI works locally; list supported firmware versions

### Phase 2: Config & Reboot Flow (Week 2)
- [ ] Backend: Change detection, restart_behavior enum (2.2)
- [ ] Frontend: Apply Changes modal & reconnection logic (2.3)
- [ ] Mode switch special-case handling (2.4)
- [ ] **Testing:** Test config changes with mock/real; verify restart flows

### Phase 3: Dashboard Unification (Week 3)
- [ ] Webcam integration endpoint & UI embed (1.1-1.3)
- [ ] Console log streaming via WS or API (1.2)
- [ ] Two-pane layout in Vue app (1.3)
- [ ] **Testing:** Visual testing with dev UI; verify streams update in real-time

### Phase 4: Filament Retry & Mechanics (Week 4)
- [ ] Config parameters for auto-retry (4.1)
- [ ] Retry logic in ace.py (4.2)
- [ ] Frontend retry UI & WebSocket state (4.3)
- [ ] Filament swap mechanics audit & recommendations (5.1-5.5)
- [ ] **Testing:** Simulate load failures; verify retry counter; document findings

### Phase 5: Polish & Release (Week 5)
- [ ] Documentation updates (README, config templates, troubleshooting guide)
- [ ] Merge all branches, test full integration
- [ ] Release notes summarizing features

---

## 8. Risk & Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| **Firmware version detection fails** | Can't validate compatibility | Fallback: Allow manual version entry in config; warn if unknown |
| **Webcam stream unavailable (no crowsnest)** | Dashboard looks incomplete | Graceful fallback: "Camera not configured" card; link to setup guide |
| **Auto-retry causes infinite retry loop** | Printer stuck indefinitely | Cap retries globally (max 5), always pause after exhaustion, not abort |
| **Config apply breaks something** | Bad config breaks printer | Keep backup, validate config before write, show diff summary first |
| **WebSocket drop during reboot** | UI loses state | Auto-reconnect every 2s; show "Reconnecting..." status; recover gracefully |
| **Mock mode stale data** | Dev UI misleads dev | Timestamp mock data; allow easy re-generation or swap to real via toggle |

---

## 9. Success Criteria

✅ **Feature 1 (Unified Dashboard):** Webcam + console visible in `/multiace` without switching pages; both update in real-time.

✅ **Feature 2 (Config Apply):** User changes a config, clicks "Apply", sees modal showing what changed & what restart is needed, clicks "Restart Now", printer reboots and UI reconnects.

✅ **Feature 3 (Firmware 1.5.2):** Code detects firmware version; `/api/version` reports it; README lists supported versions (1.5.0, 1.5.1, 1.5.2, ACE Pro 2 1.1.31).

✅ **Feature 4 (Auto Retry):** Load fails → UI shows "Retrying (1/3)" → auto-retries 2 more times → pauses for manual intervention if all fail.

✅ **Mechanics Review:** Document findings in a new file `docs/FILAMENT_SWAP_GUIDE.md` with audit results & recommendations.

✅ **Dev UI:** `run-dev-ui.sh/ps1` launches browser with mock or real data; dev can test UI changes without printer.

---

## 10. Files Touched (High-Level)

| File | Changes |
|------|---------|
| `multiace/klipper/extras/ace.py` | Firmware version detection, auto-retry logic for filament loads |
| `multiace/web/backend/main.py` | Change detection, restart_behavior enum, console log endpoint, webcam proxy (optional) |
| `multiace/web/frontend/app.js` | Two-pane layout, Apply Changes modal, retry UI, console sidebar, reconnection logic |
| `multiace/web/frontend/style.css` | Layout styles for sidebars, modal styling |
| `multiace/config/extended/multiace.cfg` | New config params: `filament_load_max_auto_retries`, firmware version field |
| `.github/plans/feature-ux-improvements.md` | This file |
| `scripts/run-dev-ui.sh` | New script |
| `scripts/run-dev-ui.ps1` | New script |
| `tests/fixtures/mock_state.json` | New mock data |
| `tests/unit/test_firmware_compat.py` | New test file |
| `tests/unit/test_config_changes.py` | New test file |
| `README.md` | Update firmware support section, document new features |
| `docs/FILAMENT_SWAP_GUIDE.md` | New troubleshooting guide & mechanics review |

---

## Appendix: Open Questions

1. **Firmware auto-detect:** Where does printer firmware version appear in Klipper/Moonraker state? Confirm via API docs or printer logs.
2. **Console log source:** Best way to tail logs—Moonraker `/server/gcode_store`? Printer log file? Real-time vs buffered?
3. **Webcam fallback:** If crowsnest not running, what's the fallback stream URL? USB camera via Moonraker?
4. **Retry delay tunability:** Should delay be configurable per ACE, or global? Should it backoff exponentially?
5. **Filament mechanics audit:** Is this a community review (users report jams) or hands-on inspection of reference unit? Budget/scope?

---


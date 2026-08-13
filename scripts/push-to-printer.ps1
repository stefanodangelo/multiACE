<#
.SYNOPSIS
Push the working tree to the printer without cutting a release.

.DESCRIPTION
A real port of push-to-printer.sh, not a stub - this is a Windows dev
machine, and the two scripts share one behaviour spec. Keep them in step.

TWO PUSH CLASSES, and -WebOnly is the DEFAULT (plan section 13.1):

  -WebOnly (default)  syncs multiace/web only, restarts the panel service,
                      never touches mcu.py, extruder.py or the Klipper
                      extras, never restarts Klipper. This is the inner
                      loop and it is mechanically inert - the worst case
                      is a broken web page.

  -Full               syncs everything and runs install_multiace.sh, which
                      REPLACES stock Klipper code and sed-patches
                      TRSYNC_TIMEOUT in mcu.py. TRSYNC_TIMEOUT governs
                      multi-MCU homing, and homing failures are how a
                      toolhead drives into the bed. So -Full is gated on
                      local tests passing, the printer being idle, and the
                      target's mcu.py being either stock or already
                      patched - never a third, hand-edited value.

  -Rollback           restores the installer's own backups
                      (extruder_pre_multiace.py, mcu.py.pre_multiace) and
                      restarts. Shipped alongside -Full, because a recovery
                      path that has never run is not a recovery path.

.EXAMPLE
  .\push-to-printer.ps1 -PrinterHost 192.168.1.50
  .\push-to-printer.ps1 -PrinterHost 192.168.1.50 -Full
  .\push-to-printer.ps1 -PrinterHost 192.168.1.50 -Rollback
#>
[CmdletBinding()]
param(
    [string]$PrinterHost = $(if ($env:MULTIACE_PRINTER_HOST) { $env:MULTIACE_PRINTER_HOST } else { "192.168.178.82" }),
    [string]$PrinterUser = $(if ($env:MULTIACE_PRINTER_USER) { $env:MULTIACE_PRINTER_USER } else { "root" }),
    [switch]$WebOnly,
    [switch]$Full,
    [switch]$Rollback,
    [switch]$DryRun,
    [switch]$NoRestart,
    [switch]$SkipTests,
    [switch]$ForceMidPrint
)

$ErrorActionPreference = "Stop"

$RepoRoot  = Split-Path -Parent $PSScriptRoot
$RemoteTar = "/tmp/multiace-dev.tar.gz"
$RemoteDir = "/tmp/multiace-dev"
# The value install_multiace.sh patches mcu.py to. Kept here so a third,
# unexpected value can be refused rather than re-patched.
$TrsyncValue = "0.350"

$Mode = if ($Full) { "full" } else { "web-only" }

function Say  ($m) { Write-Host "STATUS: $m" }
function Warn ($m) { Write-Warning $m }
function Die  ($m) { Write-Error $m; exit 1 }

# $ErrorActionPreference = "Stop" turns ANY stderr line written by a native
# .exe into a terminating exception - independent of its exit code, and
# empirically NOT reliably prevented by stream redirection alone (2>$null
# on `git diff --quiet` still threw here). Every native call in this
# script follows the same shape: run it, then decide success/failure from
# $LASTEXITCODE or its output MYSELF - a git line-ending warning or a
# `git diff --quiet` exit code of 1 ("there are differences", the expected
# common case) must not be able to abort the script on their own. The only
# reliable way to get that is to not be in "Stop" mode for the moment the
# native command runs; -ErrorAction isn't valid on a native command, so the
# preference variable itself has to move, temporarily, around the call.
function Invoke-NativeChecked {
    param([Parameter(Mandatory)][scriptblock]$Script)
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $Script
    } finally {
        $ErrorActionPreference = $prevEAP
    }
}

if (-not $PrinterHost) {
    Die "No printer host. Pass -PrinterHost or set MULTIACE_PRINTER_HOST."
}

function Invoke-Remote ($Command) {
    if ($DryRun) {
        Write-Host "DRY-RUN: ssh $PrinterUser@$PrinterHost $Command"
        return ""
    }
    $out = Invoke-NativeChecked { ssh -o BatchMode=yes "$PrinterUser@$PrinterHost" $Command 2>&1 }
    if ($LASTEXITCODE -ne 0) {
        Die "remote command failed ($LASTEXITCODE): $out"
    }
    return ($out -join "`n")
}

function Get-PrintState {
    try {
        $r = Invoke-RestMethod -TimeoutSec 5 `
            -Uri "http://$PrinterHost/printer/objects/query?print_stats"
        return [string]$r.result.status.print_stats.state
    } catch {
        return ""
    }
}

# Section 13.4: fails CLOSED. An unreachable or unrecognised state blocks -
# "cannot tell" and "idle" are not the same answer when the cost of being
# wrong is a ruined print and a nozzle set into cold plastic.
function Assert-PrinterIdle {
    $state = Get-PrintState
    if (-not $state) { $state = "unknown" }
    if ($state -in @("printing", "paused", "unknown")) {
        if ($ForceMidPrint) {
            Warn "printer reports '$state' - proceeding anyway (-ForceMidPrint)"
            return
        }
        Die ("printer is '$state'. Installing now would abort the print, cut " +
             "the heaters and leave the nozzle parked in setting plastic. " +
             "Wait for it to finish, or pass -ForceMidPrint if you are certain.")
    }
    Say "printer state: $state"
}

function Invoke-LocalTests {
    if ($SkipTests) {
        Warn "-SkipTests: NOT running pytest. You are about to install"
        Warn "  multiace/klipper/extras/*.py, multiace/klipper/kinematics/extruder_ace.py"
        Warn "  and let the installer patch TRSYNC_TIMEOUT in mcu.py,"
        Warn "  with no local check that any of it works."
        return
    }
    Say "running the local test suite (a tree that fails tests is never pushed)"
    if ($DryRun) { Write-Host "DRY-RUN: python -m pytest -q"; return }
    Push-Location $RepoRoot
    try {
        Invoke-NativeChecked { python -m pytest -q }
        if ($LASTEXITCODE -ne 0) {
            Die "tests failed - not pushing. Fix them, or -SkipTests if you truly mean to."
        }
    } finally { Pop-Location }
}

# The installer patches TRSYNC_TIMEOUT. A file that is neither stock nor
# already-patched has been hand-edited, and re-patching it is how a
# half-applied multi-MCU homing timeout gets shipped.
function Assert-McuPatchable {
    $out = Invoke-Remote "grep -E '^TRSYNC_TIMEOUT = ' /home/lava/klipper/klippy/mcu.py || true"
    if ($DryRun) { return }
    $value = ($out -split '\s+')[2]
    if (-not $value) {
        Die "No TRSYNC_TIMEOUT found in mcu.py - this is not a Klipper tree the installer knows how to patch."
    }
    if ($value -eq "0.050" -or $value -eq $TrsyncValue) {
        Say "mcu.py TRSYNC_TIMEOUT=$value (known)"
    } else {
        Die ("mcu.py has TRSYNC_TIMEOUT=$value, which is neither stock (0.050) " +
             "nor multiACE's ($TrsyncValue). It has been hand-edited. Refusing to " +
             "patch it again - multi-MCU homing depends on this value and a wrong " +
             "one drives the toolhead into the bed. Restore mcu.py.pre_multiace first.")
    }
}

function Get-DevVersion {
    Push-Location $RepoRoot
    try {
        $sha = Invoke-NativeChecked { git rev-parse --short HEAD 2>$null }
        if (-not $sha) { $sha = "nogit" }
        Invoke-NativeChecked { git diff --quiet 2>$null } | Out-Null
        $dirty = if ($LASTEXITCODE -ne 0) { "-dirty" } else { "" }
        return "dev.$sha$dirty"
    } finally { Pop-Location }
}

function Restart-Klipper {
    Say "restarting Klipper"
    if ($DryRun) { Write-Host "DRY-RUN: FIRMWARE_RESTART via Moonraker"; return }
    try {
        Invoke-RestMethod -Method Post -TimeoutSec 20 `
            -Uri "http://$PrinterHost/printer/firmware_restart" | Out-Null
    } catch {
        Warn "firmware_restart request failed - check the printer"
    }
}

function Restart-Web {
    Say "restarting the multiACE web service"
    Invoke-Remote '/etc/init.d/S98multiace-web restart >/dev/null 2>&1 || true' | Out-Null
}

# Verify recovery, do not assume it. A push that half-lands is exactly the
# "half-stock, half-multiACE" state the README warns about.
function Confirm-Recovery {
    if ($DryRun) { return }
    Say "waiting for the printer to come back"
    $up = $false
    foreach ($i in 1..60) {
        try {
            $h = Invoke-RestMethod -TimeoutSec 3 `
                -Uri "http://$PrinterHost/multiace/api/health"
            if ($h.status -eq "ok") { $up = $true; Say "multiACE web is up"; break }
        } catch { }
        Start-Sleep -Seconds 2
    }
    if (-not $up) { Warn "multiACE web did not answer within 2 minutes" }
    try {
        $st = Invoke-RestMethod -TimeoutSec 5 `
            -Uri "http://$PrinterHost/multiace/api/state"
        if ($st.ace_startup -and $st.ace_startup.state -eq "waiting") {
            Warn ("Klipper is up but an ACE did not reconnect " +
                  "($($st.ace_startup.found)/$($st.ace_startup.expected)). Power it on - " +
                  "multiACE picks it up within a few seconds now, or run ACE_RESCAN. " +
                  "If this push broke something: .\push-to-printer.ps1 -PrinterHost $PrinterHost -Rollback")
        } else {
            Say "Klipper reports ready and the ACEs reconnected"
        }
    } catch {
        Warn ("Could not read /multiace/api/state - the push may have half-landed. " +
              "If the printer misbehaves: .\push-to-printer.ps1 -PrinterHost $PrinterHost -Rollback")
    }
}

function Invoke-Rollback {
    Say "rolling back to the installer's pre-multiACE backups"
    $script = @'
set -e
K=/home/lava/klipper/klippy
did=0
if [ -f "$K/kinematics/extruder_pre_multiace.py" ]; then
    cp "$K/kinematics/extruder_pre_multiace.py" "$K/kinematics/extruder.py"
    echo "STATUS: restored extruder.py"
    did=1
fi
if [ -f "$K/mcu.py.pre_multiace" ]; then
    cp "$K/mcu.py.pre_multiace" "$K/mcu.py"
    echo "STATUS: restored mcu.py"
    did=1
fi
if [ "$did" = "0" ]; then
    echo "ERROR: no pre_multiace backups found - nothing to roll back" >&2
    exit 1
fi
'@
    Invoke-Remote $script
    if (-not $NoRestart) { Restart-Klipper }
    Say "rollback done"
}

# --- main ------------------------------------------------------------------

if ($Rollback) {
    Assert-PrinterIdle
    Invoke-Rollback
    Confirm-Recovery
    exit 0
}

Say "target: $PrinterUser@$PrinterHost  class: $Mode"
if ($Mode -eq "full") {
    Say "FULL push: this replaces stock Klipper files and may patch mcu.py."
} else {
    Say "WEB-ONLY push: multiace/web only. Klipper is not touched or restarted."
}

if ($Mode -eq "full") {
    Invoke-LocalTests
    Assert-PrinterIdle
    Assert-McuPatchable
} else {
    # A broken panel mid-print is bad UX, not damage - warn, do not refuse.
    $state = Get-PrintState
    if ($state -in @("printing", "paused")) {
        Warn "printer is $state - the panel will blink, but nothing mechanical is touched"
    }
}

$Stage = Join-Path ([System.IO.Path]::GetTempPath()) ("multiace-push-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $Stage -Force | Out-Null
try {
    Say "staging the working tree"
    $sources = if ($Mode -eq "full") {
        @("multiace")
    } else {
        @("multiace/web", "multiace/tools", "multiace/i18n")
    }
    foreach ($rel in $sources) {
        $src = Join-Path $RepoRoot ($rel -replace "/", "\")
        $dst = Join-Path $Stage ($rel -replace "/", "\")
        New-Item -ItemType Directory -Path (Split-Path -Parent $dst) -Force | Out-Null
        Copy-Item -Recurse -Force $src $dst
    }
    Get-ChildItem -Path $Stage -Recurse -Force -Directory `
        | Where-Object { $_.Name -eq "__pycache__" } `
        | Remove-Item -Recurse -Force
    Get-ChildItem -Path $Stage -Recurse -Force -Filter "*.pyc" `
        | Remove-Item -Force

    # Stamp the tarball copy only, never the working tree, so a push never
    # dirties your git status.
    $tag = Get-DevVersion
    $acePy = Join-Path $Stage "multiace\klipper\extras\ace.py"
    if (Test-Path $acePy) {
        if (-not (Select-String -Path $acePy -Pattern '^MULTIACE_BUILD_TAG' -Quiet)) {
            Say "stamping MULTIACE_BUILD_TAG = $tag"
            Add-Content -Path $acePy -Encoding utf8 -Value "`nMULTIACE_BUILD_TAG = `"$tag`""
        }
    }

    Say "packing"
    $tarball = Join-Path $Stage "multiace-dev.tar.gz"
    Invoke-NativeChecked { tar -C $Stage -czf $tarball multiace }
    if ($LASTEXITCODE -ne 0) { Die "tar failed - is bsdtar/tar on PATH?" }

    if ($DryRun) {
        $mb = [math]::Round((Get-Item $tarball).Length / 1MB, 2)
        Say "dry run: would copy $mb MB to $PrinterHost"
        exit 0
    }

    Say "copying to ${PrinterHost}:$RemoteTar"
    # NOT scp, on purpose. Both scp protocol modes (the default SFTP-based
    # transfer and the legacy -O one) took this printer's sshd down
    # entirely - the first during protocol negotiation, the second
    # mid-transfer - rather than just failing that one session (observed
    # HW-side 2026-08-13: Moonraker/nginx on the same box kept working the
    # whole time, only sshd crashed). This streams the file over a plain
    # SSH exec channel instead: base64 locally, decode remotely. Base64
    # rather than a raw byte pipe because piping binary data through
    # Windows PowerShell's own pipeline into a native process's stdin is
    # unreliable (encoding conversions can corrupt it, especially on
    # Windows PowerShell 5.1) - as plain ASCII text there is no such
    # ambiguity, at the cost of ~33% more bytes on the wire, irrelevant at
    # this file's size. The remote end only needs `base64`, a standard
    # BusyBox applet, not the scp/sftp subsystem that keeps crashing.
    $b64 = [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($tarball))
    # The pipe has to be INSIDE the scriptblock, not `$b64 | Invoke-
    # NativeChecked {...}` - that form tries to bind $b64 to one of the
    # wrapper's own parameters and throws a ParameterBindingException
    # before ssh ever runs. Inside the block, $b64 is just a normal closed-
    # over variable and pipes into the native command exactly as expected.
    Invoke-NativeChecked {
        $b64 | ssh -T -o BatchMode=yes "$PrinterUser@$PrinterHost" "base64 -d > $RemoteTar"
    }
    if ($LASTEXITCODE -ne 0) { Die "streaming copy to the printer failed" }

    Say "extracting"
    Invoke-Remote "rm -rf '$RemoteDir' && mkdir -p '$RemoteDir' && tar -C '$RemoteDir' -xzf '$RemoteTar'" | Out-Null

    if ($Mode -eq "full") {
        Say "running install_multiace.sh --install-web"
        Invoke-Remote "cd '$RemoteDir/multiace' && chmod +x install_multiace.sh && ./install_multiace.sh --install-web"
        if (-not $NoRestart) {
            Restart-Klipper
            Confirm-Recovery
        }
    } else {
        # Mirror what install_multiace.sh does for these three directories -
        # same destinations, same flattening (multiace/i18n ->
        # multiace_web/i18n, which is where the backend resolves it as
        # __file__/../../i18n), same __pycache__ drop and chown. Inventing a
        # layout here is how a push lands somewhere the service never reads.
        Say "syncing the web files in place (no installer, no Klipper restart)"
        $sync = @"
set -e
WEB_DEST=/home/lava/multiace_web
if [ ! -d "`$WEB_DEST/backend" ]; then
    echo 'ERROR: '"`$WEB_DEST"' not found - multiACE Web has never been installed on this printer. Run a -Full push first (it passes --install-web).' >&2
    exit 1
fi
mkdir -p "`$WEB_DEST/backend" "`$WEB_DEST/frontend" "`$WEB_DEST/i18n"
cp -a '$RemoteDir/multiace/web/backend/.'  "`$WEB_DEST/backend/"
cp -a '$RemoteDir/multiace/web/frontend/.' "`$WEB_DEST/frontend/"
cp -a '$RemoteDir/multiace/i18n/.'         "`$WEB_DEST/i18n/"
rm -rf "`$WEB_DEST/backend/__pycache__" 2>/dev/null || true
chown -R lava:lava "`$WEB_DEST" 2>/dev/null || true
mkdir -p /home/lava/printer_data/config/tools
cp -f '$RemoteDir/multiace/tools/'*.py /home/lava/printer_data/config/tools/
echo 'STATUS: synced backend, frontend, i18n and tools'
"@
        Invoke-Remote $sync | Out-Null
        if (-not $NoRestart) { Restart-Web }
    }

    Say "done"
} finally {
    Remove-Item -Recurse -Force $Stage -ErrorAction SilentlyContinue
}

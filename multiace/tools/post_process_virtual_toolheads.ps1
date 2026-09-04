<#
.SYNOPSIS
    Runs post_process_virtual_toolheads.py on a G-code file.

.DESCRIPTION
    Thin wrapper so the post-processor can be invoked from PowerShell (e.g. as an
    Orca/Snapmaker Orca post-processing script) without typing the full python
    command line. The Python script always rewrites its input file in place, so
    this wrapper first copies GcodeFile to "<name>_post<ext>" and runs the
    post-processor on that copy - the original file is left untouched.

.PARAMETER GcodeFile
    Path to the .gcode file to post-process. Passed through as the script's
    last argument (required by post_process_virtual_toolheads.py).

.PARAMETER Options
    Any extra options to forward to the Python script (e.g. -Options --layer,
    --optimize, "--live-lookup", "192.168.1.42", --aces, 2). These are passed
    before the file path.

.EXAMPLE
    .\post_process_virtual_toolheads.ps1 C:\prints\model.gcode
    # writes C:\prints\model_post.gcode, leaves model.gcode untouched

.EXAMPLE
    .\post_process_virtual_toolheads.ps1 -Options --layer C:\prints\model.gcode
#>

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$GcodeFile,

    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Options = @()
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $GcodeFile)) {
    Write-Error "File not found: $GcodeFile"
    exit 1
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pyScript = Join-Path $scriptDir 'post_process_virtual_toolheads.py'

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    $python = Get-Command python3 -ErrorAction SilentlyContinue
}
if (-not $python) {
    Write-Error "python not found on PATH. Install Python or adjust this script to point at your python.exe."
    exit 1
}

$gcodeFullPath = (Resolve-Path -LiteralPath $GcodeFile).Path

$dir = Split-Path -Parent $gcodeFullPath
$base = [System.IO.Path]::GetFileNameWithoutExtension($gcodeFullPath)
$ext = [System.IO.Path]::GetExtension($gcodeFullPath)
$postPath = Join-Path $dir "$base`_post$ext"

Copy-Item -LiteralPath $gcodeFullPath -Destination $postPath -Force

& $python.Source $pyScript @Options $postPath
$exitCode = $LASTEXITCODE

if ($exitCode -eq 0) {
    Write-Host "Post-processed copy written to: $postPath"
}

exit $exitCode

#Requires -Version 5.1
<#
  bampiao (image2-bampiao) installer for Windows.

  ASCII only, on purpose: Windows PowerShell 5.1 reads .ps1 as the system ANSI
  codepage unless the file has a UTF-8 BOM, so non-ASCII text here would turn
  into mojibake on some machines.

  Usage (from the repo root):
      powershell -ExecutionPolicy Bypass -File .\install.ps1
      powershell -ExecutionPolicy Bypass -File .\install.ps1 -Dev
      powershell -ExecutionPolicy Bypass -File .\install.ps1 -Python C:\Python312\python.exe

  What it does:
    1. finds a Python >= 3.12
    2. installs this package with pipx (preferred) or into a private venv
    3. runs `bampiao doctor` so you see immediately what is missing
    4. prints the next two commands

  It does NOT:
    - touch your daily Chrome/Edge profile
    - create scheduled tasks
    - download or install browsers
#>
param(
    [switch]$Dev,
    [string]$Python = "",
    [switch]$NoPipx
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

function Info($m) { Write-Host "  $m" }
function Ok($m)   { Write-Host "  [ok]   $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [warn] $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "  [fail] $m" -ForegroundColor Red }

Write-Host ""
Write-Host "bampiao installer" -ForegroundColor Cyan
Write-Host "-----------------" -ForegroundColor Cyan

if (-not $IsWindows -and $env:OS -ne "Windows_NT") {
    Fail "This installer is Windows only. v1 does not support macOS yet."
    exit 2
}

# ---------------------------------------------------------------- 1. python
if (-not (Test-Path (Join-Path $Root "pyproject.toml"))) {
    Fail "pyproject.toml not found. Run this script from the repo root."
    exit 2
}

$candidates = @()
if ($Python) { $candidates += $Python }
$candidates += @("py -3.13", "py -3.12", "python")

$pyExe = $null
$pyVer = $null
foreach ($c in $candidates) {
    $parts = $c.Split(" ")
    $exe = $parts[0]
    $pre = @()
    if ($parts.Length -gt 1) { $pre = $parts[1..($parts.Length - 1)] }
    try {
        $out = & $exe @pre -c "import sys;print('%d.%d.%d'%sys.version_info[:3])" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
        $ver = [version]($out.Trim())
        if ($ver -ge [version]"3.12") {
            $pyExe = $exe
            $pyPre = $pre
            $pyVer = $out.Trim()
            break
        }
    } catch { }
}

if (-not $pyExe) {
    Fail "Python 3.12+ not found."
    Info "Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'),"
    Info "then run this script again. Or pass -Python C:\path\to\python.exe"
    exit 3
}
Ok "Python $pyVer  ($pyExe $($pyPre -join ' '))"

# ---------------------------------------------------------------- 2. install
$pipx = Get-Command pipx -ErrorAction SilentlyContinue
$installed = $false

if ($pipx -and -not $NoPipx) {
    Info "using pipx"
    try {
        & pipx install --force $Root | Out-Host
        if ($LASTEXITCODE -eq 0) { $installed = $true }
    } catch { }
    if (-not $installed) { Warn "pipx failed, falling back to a private venv" }
}

if (-not $installed) {
    $venvDir = Join-Path $env:LOCALAPPDATA "bampiao\venv"
    if (-not (Test-Path $venvDir)) {
        Info "creating venv at $venvDir"
        & $pyExe @pyPre -m venv $venvDir
        if ($LASTEXITCODE -ne 0) { Fail "could not create venv"; exit 5 }
    }
    $venvPy = Join-Path $venvDir "Scripts\python.exe"
    Info "installing into the venv (this takes a minute)"
    & $venvPy -m pip install --upgrade pip --quiet
    if ($Dev) {
        & $venvPy -m pip install -e "${Root}[dev]" --quiet
    } else {
        & $venvPy -m pip install $Root --quiet
    }
    if ($LASTEXITCODE -ne 0) { Fail "pip install failed"; exit 5 }
    $binDir = Join-Path $venvDir "Scripts"
    Ok "installed into $venvDir"
} else {
    $binDir = Join-Path $env:USERPROFILE ".local\bin"
    Ok "installed with pipx"
}

$bampiao = Join-Path $binDir "bampiao.exe"
$desktop = Join-Path $binDir "bampiao-desktop.exe"

if (-not (Test-Path $bampiao)) {
    $found = Get-Command bampiao -ErrorAction SilentlyContinue
    if ($found) { $bampiao = $found.Source }
}

# ---------------------------------------------------------------- 3. check
Write-Host ""
Info "running doctor (summary only)"
# Why not print doctor's own output here:
# Windows PowerShell decodes a native command's stdout using the console
# codepage (cp936 on Chinese systems), so Chinese comes out as mojibake no
# matter which encoding we force on one side. `--json` is pure ASCII
# (non-ASCII is \uXXXX escaped), so it survives any codepage. We print an
# ASCII summary and let the user run `doctor` themselves for the details.
$okN = 0; $warnN = 0; $failN = 0
try {
    $raw = (& $bampiao doctor --json 2>$null) -join ""
    if ($raw) {
        $doc = $raw | ConvertFrom-Json
        foreach ($r in $doc.rows) {
            switch ($r.level) {
                "ok"   { $okN++ }
                "warn" { $warnN++ }
                "fail" { $failN++ }
            }
        }
    }
} catch {
    $failN = -1
}

if ($failN -lt 0) {
    Warn "doctor did not run (not fatal, you can run it yourself later)"
} elseif ($failN -gt 0) {
    Fail ("{0} check(s) failed - fix them before generating images" -f $failN)
    Write-Host "  run this to see the details:" -ForegroundColor Yellow
    Write-Host ('  & "' + $bampiao + '" doctor') -ForegroundColor White
} elseif ($warnN -gt 0) {
    Ok ("checks passed: {0} ok, {1} warning (normal on a fresh install)" -f $okN, $warnN)
} else {
    Ok ("checks passed: {0} ok" -f $okN)
}

# ---------------------------------------------------------------- 4. next
Write-Host ""
Write-Host "done." -ForegroundColor Green
Write-Host ""
Write-Host "COPY THE LINE BELOW, paste it into this window, press Enter." -ForegroundColor Cyan
Write-Host ""
Write-Host ('  $b = "' + $bampiao + '"') -ForegroundColor White
Write-Host ""
Write-Host "then:" -ForegroundColor Cyan
Write-Host '  & $b init'
Write-Host '  & $b accounts add --name me --browser chrome'
Write-Host '  & $b login acc1        <- log in inside the window it opens'
Write-Host '  & $b gen "1024x1024 icon: dark green circle on off-white, flat vector, no text" --out test.png'
Write-Host '  & $b ui                <- local panel'
Write-Host ''
Write-Host 'tray app (starts with the panel inside):' -ForegroundColor Cyan
Write-Host ('  "' + $desktop + '"')
Write-Host ""
Write-Host "if you open a NEW window later, paste the same `$b line again." -ForegroundColor Yellow
Write-Host ""

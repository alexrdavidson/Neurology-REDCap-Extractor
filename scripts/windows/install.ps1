# Neurology REDCap Extractor - one-time setup on Windows 10/11. Safe to run again: it skips what is already done.
# Started by "Install Neurology REDCap Extractor.bat" in the app folder. Needs internet. Installs for this Windows user only.
# Nothing here touches patient data. (ASCII only: Windows PowerShell 5.1 reads this file as ANSI.)

$ErrorActionPreference = 'Continue'   # native tools write progress to stderr; check exit codes instead
$ProgressPreference = 'SilentlyContinue'
$App = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location -LiteralPath $App
$Model = 'qwen3:8b'
$Total = 6
$script:Warnings = 0

function Say-Step([int]$n, [string]$text) { Write-Host ''; Write-Host "[$n/$Total] $text" -ForegroundColor Cyan }
function Say-Ok([string]$text) { Write-Host "  OK  $text" -ForegroundColor Green }
function Say-Warn([string]$text) { $script:Warnings++; Write-Host "  !   $text" -ForegroundColor Yellow }
function Stop-Setup([string]$text) {
    Write-Host ''
    Write-Host $text -ForegroundColor Red
    Write-Host 'Setup stopped. Fix the problem above, then run "Install Neurology REDCap Extractor" again.'
    exit 1
}
function Has-Winget { return [bool](Get-Command winget -ErrorAction SilentlyContinue) }

function Test-Python([string]$exe) {
    try {
        & $exe -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in @('3.12', '3.11', '3.13')) {
            try {
                $exe = & py "-$v" -c "import sys; print(sys.executable)" 2>$null
                if ($LASTEXITCODE -eq 0 -and $exe) { return ([string]$exe).Trim() }
            } catch {}
        }
    }
    foreach ($root in @("$env:LOCALAPPDATA\Programs\Python", "$env:ProgramFiles")) {
        if (-not (Test-Path $root)) { continue }
        $dirs = Get-ChildItem -Path $root -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue | Sort-Object Name -Descending
        foreach ($d in $dirs) {
            $exe = Join-Path $d.FullName 'python.exe'
            if ((Test-Path $exe) -and (Test-Python $exe)) { return $exe }
        }
    }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and ($cmd.Source -notlike '*WindowsApps*') -and (Test-Python $cmd.Source)) { return $cmd.Source }
    return $null
}

function Find-Ollama {
    $c = Get-Command ollama -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $p = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path $p) { return $p }
    return $null
}

function Test-OllamaUp {
    try {
        $wc = New-Object System.Net.WebClient
        $wc.Proxy = $null                       # 127.0.0.1 only, never through a proxy
        $null = $wc.DownloadString('http://127.0.0.1:11434/api/version')
        return $true
    } catch { return $false }
}

Write-Host 'Neurology REDCap Extractor setup' -ForegroundColor Cyan
Write-Host "App folder: $App"

# ------------------------------------------------------------------------------------------------ 1. Python
Say-Step 1 'Python 3.11 or newer'
$Python = Find-Python
if (-not $Python -and (Has-Winget)) {
    Write-Host '  Installing Python 3.12 for this user (winget)...'
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    $Python = Find-Python
}
if (-not $Python) {
    Start-Process 'https://www.python.org/downloads/windows/'
    Stop-Setup ("Python 3.11 or newer was not found. Install it from python.org (the page just opened): " +
                "'Windows installer (64-bit)', and tick 'Add python.exe to PATH' in the installer.")
}
Say-Ok "Python: $Python"

# ------------------------------------------------------------------------------------------------ 2. packages
Say-Step 2 'App packages (several minutes, about 1 GB)'
$VPy = Join-Path $App '.venv\Scripts\python.exe'
if (-not (Test-Path $VPy)) {
    & $Python -m venv (Join-Path $App '.venv')
    if ($LASTEXITCODE -ne 0) { Stop-Setup 'Could not create the Python environment (.venv) in the app folder.' }
}
& $VPy -m pip install --disable-pip-version-check --upgrade pip
& $VPy -m pip install --disable-pip-version-check -e ".[windows,ner,app]"
if ($LASTEXITCODE -ne 0) {
    Stop-Setup ('Installing the packages failed (see the messages above). Check the internet connection. On a ' +
                'hospital network, ask IT whether pypi.org is blocked, or try another network.')
}
Say-Ok 'Packages installed.'
# The app's own window (pywebview). Optional: without it the app opens in a Microsoft Edge window instead.
& $VPy -m pip install --disable-pip-version-check -e ".[window]"
if ($LASTEXITCODE -ne 0) { Say-Warn 'The app window package did not install: the app will open in a Microsoft Edge window instead.' }
else { Say-Ok 'App window ready.' }

# ------------------------------------------------------------------------------------------------ 3. NER models
Say-Step 3 'De-identification models (about 1 GB, one time)'
& $VPy -c "import torch, transformers" 2>$null
if ($LASTEXITCODE -ne 0) {
    Say-Warn ('The de-identification library (torch) does not load on this computer. Usually either the ' +
              "'Microsoft Visual C++ Redistributable (x64)' is missing, or security software blocks the .venv " +
              'folder: ask IT. De-identification cannot run until this works.')
} else {
    & $VPy -m nmosd download-models
    if ($LASTEXITCODE -ne 0) { Say-Warn 'Downloading the models failed. Run this again later, or use the button in the app (step 1).' }
    else { Say-Ok 'De-identification models ready.' }
}

# ------------------------------------------------------------------------------------------------ 4. Ollama
Say-Step 4 "Local AI: Ollama and the $Model model (about 5 GB, one time)"
$Ollama = Find-Ollama
if (-not $Ollama -and (Has-Winget)) {
    Write-Host '  Installing Ollama for this user (winget)...'
    winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements
    $Ollama = Find-Ollama
}
if (-not $Ollama) {
    Start-Process 'https://ollama.com/download/windows'
    Say-Warn 'Ollama is not installed. Install it from the page that just opened, then run this setup again.'
} else {
    if (-not (Test-OllamaUp)) {
        $appExe = Join-Path (Split-Path $Ollama) 'ollama app.exe'
        if (Test-Path $appExe) { Start-Process -FilePath $appExe }
        else { Start-Process -FilePath $Ollama -ArgumentList 'serve' -WindowStyle Hidden }
        for ($i = 0; ($i -lt 40) -and -not (Test-OllamaUp); $i++) { Start-Sleep -Seconds 1 }
    }
    if (-not (Test-OllamaUp)) {
        Say-Warn 'Ollama did not start. Open Ollama from the Start menu, then run this setup again.'
    } else {
        & $Ollama pull $Model
        if ($LASTEXITCODE -ne 0) { Say-Warn "Downloading $Model failed. Run this again later, or use the button in the app (step 1)." }
        else { Say-Ok "Ollama is running and $Model is ready." }
    }
}

# ------------------------------------------------------------------------------------------------ 5. Access
Say-Step 5 'Reading the RPDR export (Microsoft Access driver)'
& $VPy -c "import pyodbc, sys; sys.exit(0 if any('accdb' in d.lower() for d in pyodbc.drivers()) else 1)" 2>$null
if ($LASTEXITCODE -eq 0) { Say-Ok 'Microsoft Access driver found.' }
else {
    Say-Warn ("The 64-bit Microsoft Access driver is not installed. Install the 'Microsoft Access Database " +
              "Engine 2016 Redistributable' (accessdatabaseengine_X64.exe) from " +
              'https://www.microsoft.com/en-us/download/details.aspx?id=54920 - IT may need to do this.')
}

# ------------------------------------------------------------------------------------------------ 6. shortcut
Say-Step 6 'Desktop shortcut'
& $VPy (Join-Path $App 'scripts\make_desktop_shortcut.py')
if ($LASTEXITCODE -ne 0) { Say-Warn 'Could not make the desktop shortcut. Use "Start Neurology REDCap Extractor" in the app folder instead.' }

Write-Host ''
if ($script:Warnings -eq 0) {
    Write-Host 'Setup finished.' -ForegroundColor Green
} else {
    Write-Host "Setup finished with $($script:Warnings) thing(s) to sort out (in yellow above)." -ForegroundColor Yellow
}
Write-Host 'Start the app with the Neurology REDCap Extractor icon on the desktop (or "Start Neurology REDCap Extractor" in the app folder).'
Write-Host 'Step 1 in the app shows anything that still needs attention.'
exit 0

# PhoneVault installer for Windows 10/11. Started by Install-Windows.cmd (double-click).
# Installs per-user into %LOCALAPPDATA%\PhoneVault (no admin needed for the app itself).
# Also installs, if missing: Python 3.13, Apple Devices (Apple's iPhone USB driver), FFmpeg,
# Android platform-tools (adb).
# 'Continue': native tools (pip, winget) write notices to stderr; success is checked with $LASTEXITCODE
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
$Setup = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root  = Split-Path -Parent $Setup
$App   = Join-Path $Root 'app'
$Base  = Join-Path $env:LOCALAPPDATA 'PhoneVault'
$Venv  = Join-Path $Base 'venv'
$Py    = Join-Path $Venv 'Scripts\python.exe'
$PyW   = Join-Path $Venv 'Scripts\pythonw.exe'
New-Item -ItemType Directory -Force -Path $Base | Out-Null
Start-Transcript -Path (Join-Path $Base 'install.log') -Append | Out-Null

function Step($t) { Write-Host "`n== $t" -ForegroundColor Cyan }
function Warn($t) { Write-Host "!! $t" -ForegroundColor Yellow }
function Fail($t) {
    Write-Host "XX $t" -ForegroundColor Red
    Write-Host "Log: $(Join-Path $Base 'install.log')"
    Stop-Transcript | Out-Null
    exit 1
}
function Have-Winget { [bool](Get-Command winget -ErrorAction SilentlyContinue) }
function Winget-Install([string[]]$WingetArgs) {
    & winget install @WingetArgs --silent --accept-package-agreements --accept-source-agreements --disable-interactivity
    return ($LASTEXITCODE -eq 0)
}

function Find-Python {
    $tries = @(
        @{ Exe = 'py'; Pre = @('-3.13') }, @{ Exe = 'py'; Pre = @('-3.12') }, @{ Exe = 'py'; Pre = @('-3.11') },
        @{ Exe = (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'); Pre = @() },
        @{ Exe = 'python'; Pre = @() }
    )
    foreach ($t in $tries) {
        if (-not (Get-Command $t.Exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $out = & $t.Exe @($t.Pre) -c "import sys; print(int(sys.version_info >= (3, 11))); print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out -and $out[0] -eq '1') { return $out[1] }
        } catch { }
    }
    return $null
}

Step "PhoneVault installer ($(Get-Date))"
if (-not (Test-Path (Join-Path $App 'pyproject.toml'))) { Fail "Can't find the app folder. Unzip the whole PhoneVault folder first, then run Install-Windows.cmd from it." }
Get-ChildItem -Path $Root -Recurse -File | Unblock-File -ErrorAction SilentlyContinue

# ---- 1. Python ----
Step "Checking Python"
$Sys = Find-Python
if (-not $Sys) {
    if (-not (Have-Winget)) { Start-Process 'https://www.python.org/downloads/windows/'; Fail "Python 3.11+ is needed. Install it from the page that just opened (tick 'Add python.exe to PATH'), then run this again." }
    Step "Installing Python 3.13 (winget)"
    if (-not (Winget-Install @('-e', '--id', 'Python.Python.3.13', '--scope', 'user'))) { Warn "winget reported a problem installing Python" }
    $Sys = Find-Python
    if (-not $Sys) { Fail "Python didn't install. Install Python 3.13 from python.org, then run this again." }
}
Write-Host "Python: $Sys"

# ---- 2. Apple's iPhone driver (Apple Mobile Device Service) ----
Step "Checking Apple's iPhone connection service"
$Amds = 'HKLM:\SYSTEM\CurrentControlSet\Services\Apple Mobile Device Service'
if (-not (Test-Path $Amds)) {
    $ok = $false
    if (Have-Winget) {
        Step "Installing Apple Devices from the Microsoft Store (Apple's official iPhone app/driver)"
        $ok = Winget-Install @('--id', '9NM4T8B9JQZ1', '--source', 'msstore')
    }
    if (-not $ok -or -not (Test-Path $Amds)) {
        Warn "Apple Devices isn't installed yet. The Store page is opening - press Get/Install."
        Warn "PhoneVault installs anyway; the iPhone connects once Apple Devices (or iTunes) is installed."
        Start-Process 'ms-windows-store://pdp/?productid=9NM4T8B9JQZ1'
    }
} else { Write-Host "Found." }

# ---- 3. FFmpeg (plays voice notes and converts videos) ----
Step "Checking FFmpeg"
$ff = (Get-Command ffmpeg -ErrorAction SilentlyContinue) -or (Test-Path (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\ffmpeg.exe'))
if (-not $ff) {
    if ((Have-Winget) -and (Winget-Install @('-e', '--id', 'Gyan.FFmpeg'))) { Write-Host "FFmpeg installed." }
    else { Warn "FFmpeg didn't install. Voice notes and some videos may not play in the app until it's installed." }
} else { Write-Host "Found." }

# ---- 3b. adb (Android platform-tools: talks to Android phones over USB debugging) ----
Step "Checking Android platform-tools (adb)"
$ownAdb = Join-Path $Base 'platform-tools\adb.exe'
$adb = (Get-Command adb -ErrorAction SilentlyContinue) -or (Test-Path (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links\adb.exe')) -or (Test-Path $ownAdb)
if (-not $adb) {
    if ((Have-Winget) -and (Winget-Install @('-e', '--id', 'Google.PlatformTools'))) { Write-Host "Platform-tools installed." }
    else {
        Write-Host "Downloading platform-tools from Google ..."
        try {
            $zip = Join-Path $Base 'platform-tools.zip'
            Invoke-WebRequest -UseBasicParsing -Uri 'https://dl.google.com/android/repository/platform-tools-latest-windows.zip' -OutFile $zip
            Expand-Archive -Path $zip -DestinationPath $Base -Force
            Remove-Item $zip -Force
            Write-Host "Platform-tools saved to $(Join-Path $Base 'platform-tools')"
        } catch { Warn "Couldn't get Android platform-tools. iPhones work; Android phones need it (re-run this installer later)." }
    }
} else { Write-Host "Found." }
Write-Host "Android phones: if Windows doesn't see the phone after you allow USB debugging, install your brand's USB driver (Samsung Android USB Driver, Google USB Driver for Pixel)."

# ---- 4. stop a running copy ----
Step "Stopping PhoneVault if it's running"
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*-m phonevault run*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

# ---- 5. the app ----
Step "Installing PhoneVault into $Venv (downloads about 150 MB the first time)"
if (-not (Test-Path $Py)) { & $Sys -m venv $Venv; if ($LASTEXITCODE -ne 0) { Fail "Couldn't create $Venv" } }
& $Py -m pip install --upgrade --quiet pip
& $Py -m pip install --upgrade --force-reinstall --no-deps --quiet $App
if ($LASTEXITCODE -ne 0) { Fail "Installing the app failed." }
& $Py -m pip install --upgrade --quiet $App
if ($LASTEXITCODE -ne 0) { Fail "Installing the app's add-ons failed (internet needed)." }
& $Py -c "import phonevault, pymobiledevice3; print('PhoneVault', phonevault.__version__)"
if ($LASTEXITCODE -ne 0) { Fail "Install check failed." }

# ---- 6. shortcuts + open ----
Step "Adding PhoneVault to the Start menu and the Desktop"
& $Py -m phonevault install-desktop
Step "Opening PhoneVault"
Start-Process -FilePath $PyW -ArgumentList '-m', 'phonevault', 'gui'
Write-Host "`nDone. First time: the app asks where to store backups." -ForegroundColor Green
Write-Host "If Windows Firewall asks about Python later (phone view), allow it on Private networks."
Stop-Transcript | Out-Null

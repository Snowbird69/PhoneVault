# Removes the PhoneVault program, shortcuts and start-at-login.
# KEEPS your settings (%APPDATA%\PhoneVault) and your archive folder.
# Doesn't remove Python, Apple Devices or FFmpeg (other programs may use them).
$Base = Join-Path $env:LOCALAPPDATA 'PhoneVault'
$Py   = Join-Path $Base 'venv\Scripts\python.exe'
if (Test-Path $Py) { & $Py -m phonevault uninstall }
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*-m phonevault*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1
if (Test-Path (Join-Path $Base 'venv')) {
    Remove-Item -Recurse -Force (Join-Path $Base 'venv')
    Write-Host "Removed the program: $(Join-Path $Base 'venv')"
}
Write-Host "Kept: $env:APPDATA\PhoneVault (settings) and your archive folder."
Write-Host "To reinstall later: run Install-Windows.cmd"

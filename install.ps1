# Installs the LDAP Browser native helper for Firefox on Windows.
#   powershell -ExecutionPolicy Bypass -File install.ps1            install / update
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Uninstall
param([switch]$Uninstall)
$ErrorActionPreference = "Stop"

$Name = "ldap_browser_host"
$ExtId = "ldap-browser@ericwb.github.io"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$HostDir = Join-Path $Root "host"
$Venv = Join-Path $HostDir ".venv"
$Launcher = Join-Path $HostDir "$Name.bat"
$Manifest = Join-Path $HostDir "$Name.json"
$RegKey = "HKCU:\Software\Mozilla\NativeMessagingHosts\$Name"

if ($Uninstall) {
  Remove-Item -Force -ErrorAction SilentlyContinue $Launcher, $Manifest
  Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Venv
  Remove-Item -Force -ErrorAction SilentlyContinue $RegKey
  Write-Host "Uninstalled."
  exit 0
}

# Oldest Python still supported upstream (3.10 reached end-of-life 2026-10-01).
# Bump as releases age out: https://devguide.python.org/versions/
$MinMinor = 11

# The py launcher picks the newest installed Python 3.
if (Get-Command py -ErrorAction SilentlyContinue) { $Py = @("py", "-3") } else { $Py = @("python") }
$PyExe, $PyArgs = $Py
& $PyExe @PyArgs -c "import sys; sys.exit(not (sys.version_info[0] == 3 and sys.version_info[1] >= $MinMinor))"
if ($LASTEXITCODE -ne 0) {
  Write-Error "Python 3.$MinMinor or newer is required; older releases are end-of-life. Install a current Python from python.org."
  exit 1
}
if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
& $PyExe @PyArgs -m venv $Venv
& "$Venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r (Join-Path $HostDir "requirements.txt")

Set-Content -Encoding ASCII -Path $Launcher -Value "@echo off`r`n`"$Venv\Scripts\python.exe`" -I `"$HostDir\ldap_browser_host.py`" %*"

@{
  name = $Name
  description = "LDAP Browser native helper"
  path = $Launcher
  type = "stdio"
  allowed_extensions = @($ExtId)
} | ConvertTo-Json | Set-Content -Encoding UTF8 -Path $Manifest

New-Item -Force -Path $RegKey | Out-Null
Set-ItemProperty -Path $RegKey -Name "(Default)" -Value $Manifest
Write-Host "Registered $Manifest"
Write-Host "Next: about:debugging#/runtime/this-firefox -> Load Temporary Add-on -> $Root\extension\manifest.json"

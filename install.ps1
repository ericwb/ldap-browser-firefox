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

$Python = (Get-Command py -ErrorAction SilentlyContinue)
if ($Python) { & py -3 -m venv $Venv } else { & python -m venv $Venv }
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

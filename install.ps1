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

# Candidate interpreters, best first: the py launcher (newest installed
# Python 3), then whatever `python` is on PATH.
$Candidates = @()
if (Get-Command py -ErrorAction SilentlyContinue) { $Candidates += ,@("py", "-3") }
if (Get-Command python -ErrorAction SilentlyContinue) { $Candidates += ,@("python") }

$PyExe = $null
$PyArgs = @()
foreach ($c in $Candidates) {
  $exe = $c[0]
  $extra = @($c | Select-Object -Skip 1)
  & $exe @extra -c "import sys; sys.exit(not (sys.version_info[0] == 3 and sys.version_info[1] >= $MinMinor))" 2>$null
  if ($LASTEXITCODE -eq 0) { $PyExe = $exe; $PyArgs = $extra; break }
}
if (-not $PyExe) {
  Write-Error "Python 3.$MinMinor or newer is required; older releases are end-of-life. Install a current Python from python.org."
  exit 1
}
$Info = & $PyExe @PyArgs -c "import sys; print(sys.version.split()[0], 'at', sys.executable)"
Write-Host "Using Python $Info"

if (Test-Path $Venv) { Remove-Item -Recurse -Force $Venv }
& $PyExe @PyArgs -m venv $Venv
if ($LASTEXITCODE -ne 0) { Write-Error "Creating the virtualenv in $Venv failed."; exit 1 }
$VenvPython = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
  Get-ChildItem -Recurse $Venv | Select-Object -First 40 | ForEach-Object { Write-Host $_.FullName }
  Write-Error "The virtualenv has no $VenvPython."
  exit 1
}
& $VenvPython -m pip install --quiet --disable-pip-version-check -r (Join-Path $HostDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { Write-Error "Installing the helper's Python packages failed."; exit 1 }

Set-Content -Encoding ASCII -Path $Launcher -Value "@echo off`r`n`"$VenvPython`" -I `"$HostDir\ldap_browser_host.py`" %*"

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
if (Test-Path (Join-Path $Root "extension\manifest.json")) {
  Write-Host "Next: about:debugging#/runtime/this-firefox -> Load Temporary Add-on -> $Root\extension\manifest.json"
} else {
  Write-Host "Next: install the LDAP Browser add-on (.xpi) from the same release in Firefox."
}

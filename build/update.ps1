# Endora Playout updater, installed next to Playout.exe.
# Playout writes %TEMP%\endora-update-request.json, starts this script on its own (not as a child of
# Playout) and closes. The script waits for Playout to close, stops what still runs from its folder,
# runs the installer (Windows asks the admin Yes / No question), and starts Playout again.
$ErrorActionPreference = 'SilentlyContinue'
$req = Join-Path $env:TEMP 'endora-update-request.json'
$resultFile = Join-Path $env:TEMP 'endora-update-result.txt'
$log = Join-Path $env:TEMP 'endora-update-setup.log'
$appDir = $PSScriptRoot
try { $r = Get-Content -Raw -Path $req | ConvertFrom-Json } catch { exit 1 }
Remove-Item -Path $req -Force
$setup = [string]$r.setup
$version = [string]$r.version
# only an installer Playout downloaded itself
$name = Split-Path -Leaf $setup
if ((Split-Path -Parent $setup) -ne $env:TEMP.TrimEnd('\') -or $name -notmatch '^Playout-Setup-[0-9.]+\.exe$' -or -not (Test-Path $setup)) {
  Set-Content -Path $resultFile -Value "98 $version" -Encoding UTF8
  exit 1
}
# 1. wait for Playout to close (it closes itself), then make sure nothing from its folder still runs
for ($i = 0; $i -lt 40 -and (Get-Process -Id ([int]$r.pid) -ErrorAction SilentlyContinue); $i++) { Start-Sleep -Milliseconds 500 }
Start-Sleep -Seconds 1
$dir = $appDir.TrimEnd('\') + '\'
$mine = { Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith($dir, [StringComparison]::OrdinalIgnoreCase) -and $_.Id -ne $PID } }
& $mine | Stop-Process -Force
Start-Sleep -Seconds 1
# 2. install
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$sargs = @('/SILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/CLOSEAPPLICATIONS', '/FORCECLOSEAPPLICATIONS', "/LOG=`"$log`"")
$code = 1
try {
  if ($admin) { $p = Start-Process -FilePath $setup -ArgumentList $sargs -Wait -PassThru -ErrorAction Stop }
  else { $p = Start-Process -FilePath $setup -ArgumentList $sargs -Verb RunAs -Wait -PassThru -ErrorAction Stop }
  $code = if ($p) { $p.ExitCode } else { 97 }
} catch { $code = 97 }          # 97: the Windows admin question was answered No
Set-Content -Path $resultFile -Value "$code $version" -Encoding UTF8
# 3. start Playout again (the new one, or the old one if the install did not work), as the normal user
Start-Sleep -Seconds 2
if (-not (& $mine)) {
  $exe = Join-Path $appDir 'Playout.exe'
  if ($admin) {
    # the task runs with admin rights: start Playout through Explorer so it runs as the normal user
    Start-Process -FilePath "$env:WINDIR\explorer.exe" -ArgumentList "`"$exe`""
  } else {
    Start-Process -FilePath $exe -WorkingDirectory $appDir
  }
}

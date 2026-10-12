$ErrorActionPreference = "Stop"

$installDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$startMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Database & Offline Lineage"
$desktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Database & Offline Lineage.lnk"
$uninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\DatabaseOfflineLineage"

Set-Location $env:TEMP
Remove-Item (Join-Path $startMenuDir "Database & Offline Lineage.lnk") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $startMenuDir "Uninstall Database & Offline Lineage.lnk") -Force -ErrorAction SilentlyContinue
Remove-Item $startMenuDir -Force -Recurse -ErrorAction SilentlyContinue
Remove-Item $desktopShortcut -Force -ErrorAction SilentlyContinue
Remove-Item $uninstallKey -Force -Recurse -ErrorAction SilentlyContinue
Remove-Item $installDir -Force -Recurse
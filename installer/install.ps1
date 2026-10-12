$ErrorActionPreference = "Stop"

$installDir = Join-Path $env:LOCALAPPDATA "Programs\DatabaseOfflineLineage"
$startMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Database & Offline Lineage"
$desktopDir = [Environment]::GetFolderPath("Desktop")
$appPath = Join-Path $installDir "DatabaseOfflineLineage.exe"
$uninstallerPath = Join-Path $installDir "uninstall.ps1"
$uninstallKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\DatabaseOfflineLineage"

New-Item -ItemType Directory -Force -Path $installDir, $startMenuDir | Out-Null
Copy-Item (Join-Path $PSScriptRoot "DatabaseOfflineLineage.exe") $appPath -Force
Copy-Item (Join-Path $PSScriptRoot "uninstall.ps1") $uninstallerPath -Force

$shell = New-Object -ComObject WScript.Shell
$appShortcut = $shell.CreateShortcut((Join-Path $startMenuDir "Database & Offline Lineage.lnk"))
$appShortcut.TargetPath = $appPath
$appShortcut.WorkingDirectory = $installDir
$appShortcut.Description = "Analyze SQL offline or browse read-only database metadata."
$appShortcut.Save()

$desktopShortcut = $shell.CreateShortcut((Join-Path $desktopDir "Database & Offline Lineage.lnk"))
$desktopShortcut.TargetPath = $appPath
$desktopShortcut.WorkingDirectory = $installDir
$desktopShortcut.Description = "Analyze SQL offline or browse read-only database metadata."
$desktopShortcut.Save()

$uninstallShortcut = $shell.CreateShortcut((Join-Path $startMenuDir "Uninstall Database & Offline Lineage.lnk"))
$uninstallShortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$uninstallShortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$uninstallerPath`""
$uninstallShortcut.WorkingDirectory = $installDir
$uninstallShortcut.Save()

New-Item -Path $uninstallKey -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name DisplayName -Value "Database & Offline Lineage" -PropertyType String -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name DisplayVersion -Value "1.0.0" -PropertyType String -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name Publisher -Value "Local Data Tools" -PropertyType String -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name InstallLocation -Value $installDir -PropertyType String -Force | Out-Null
New-ItemProperty -Path $uninstallKey -Name UninstallString -Value "`"$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe`" -NoProfile -ExecutionPolicy Bypass -File `"$uninstallerPath`"" -PropertyType String -Force | Out-Null

Start-Process -FilePath $appPath
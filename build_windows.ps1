$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $root ".build-venv\Scripts\python.exe"
$appOutputDir = Join-Path $root "dist\app"
$workDir = Join-Path $root "build\pyinstaller"
$specDir = Join-Path $root "build\spec"
$packageWorkDir = Join-Path $env:TEMP ("DBConnect-" + [guid]::NewGuid().ToString("N"))
$installerDir = Join-Path $root "dist\installer"
$installerPath = Join-Path $installerDir "DatabaseOfflineLineage-Setup.exe"
$stagedInstallerPath = Join-Path $packageWorkDir "setup.exe"

Push-Location $root
try {
    $compilerPaths = @(
        $env:INNO_SETUP_COMPILER,
        (Get-Command ISCC.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 7\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 7\ISCC.exe"
    )
    $iscc = $compilerPaths | Where-Object { $_ -and (Test-Path $_ -PathType Leaf) } | Select-Object -First 1
    if (-not $iscc) { throw "Install Inno Setup 6.3 or later, or set INNO_SETUP_COMPILER to the full path of ISCC.exe." }

    if (-not (Test-Path $venvPython)) {
        py -3 -m venv (Join-Path $root ".build-venv")
    }

    & $venvPython -m pip install --disable-pip-version-check --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "Could not prepare the build environment." }

    & $venvPython -m pip install --disable-pip-version-check -r requirements.txt -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Could not install the packaging dependencies." }

    & $venvPython -m unittest discover -s . -p test_database_app.py
    if ($LASTEXITCODE -ne 0) { throw "Regression tests failed; the installer was not rebuilt." }

    New-Item -ItemType Directory -Force -Path $specDir | Out-Null
    $dataFile = "$(Join-Path $root 'index.html');."
    & $venvPython -m PyInstaller `
        --noconfirm `
        --clean `
        --windowed `
        --onefile `
        --name DatabaseOfflineLineage `
        --distpath $appOutputDir `
        --workpath $workDir `
        --specpath $specDir `
        --add-data $dataFile `
        --collect-submodules uvicorn `
        --collect-submodules sqlglot `
        --collect-submodules snowflake.connector `
        --collect-data certifi `
        --hidden-import pyodbc `
        --hidden-import oracledb `
        --hidden-import snowflake.connector `
        launcher.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed to create the application bundle." }

    New-Item -ItemType Directory -Force -Path $packageWorkDir, $installerDir | Out-Null
    & $iscc "/DAppSourceDir=$appOutputDir" "/O$packageWorkDir" "/Fsetup" (Join-Path $root "installer\DatabaseOfflineLineage.iss")
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $stagedInstallerPath)) {
        throw "Inno Setup failed to create the Windows installer."
    }
    Move-Item -Path $stagedInstallerPath -Destination $installerPath -Force

    Write-Host "Installer created: $installerPath"
}
finally {
    if (Test-Path $packageWorkDir) { Remove-Item -Path $packageWorkDir -Recurse -Force }
    Pop-Location
}
# Build the Windows release: dist\MotorHMI-<ver>-win64.zip (+ installer if Inno Setup is present)
# Run from the repository root in PowerShell:  .\packaging\build_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$ver = (Select-String -Path moteus_hmi\__init__.py -Pattern '__version__ = "(.+?)"').Matches[0].Groups[1].Value

py -3.12 -m venv .build-venv
.\.build-venv\Scripts\python -m pip install --upgrade pip
.\.build-venv\Scripts\python -m pip install -r requirements.txt pyinstaller pytest

# Never ship an untested build.
$env:QT_QPA_PLATFORM = "offscreen"; $env:PYTEST_DISABLE_PLUGIN_AUTOLOAD = "1"
.\.build-venv\Scripts\python -m pytest -q tests
if ($LASTEXITCODE -ne 0) { throw "tests failed" }
Remove-Item Env:QT_QPA_PLATFORM

Push-Location packaging
..\.build-venv\Scripts\pyinstaller --noconfirm --clean motor_hmi.spec
Pop-Location

$out = "dist\MotorHMI-$ver-win64"
if (Test-Path $out) { Remove-Item -Recurse -Force $out }
New-Item -ItemType Directory -Force $out | Out-Null
Copy-Item -Recurse packaging\dist\MotorHMI "$out\app"
Copy-Item README.md, docs\*.md, docs\*.docx $out -ErrorAction SilentlyContinue
Copy-Item -Recurse docs\images "$out\images" -ErrorAction SilentlyContinue
Get-ChildItem -Recurse -File "$out\app" | Get-FileHash -Algorithm SHA256 |
  ForEach-Object { "$($_.Hash.ToLower())  $($_.Path.Substring((Resolve-Path "$out\app").Path.Length + 1))" } |
  Set-Content "$out\SHA256SUMS"
Compress-Archive -Force -Path $out -DestinationPath "$out.zip"
(Get-FileHash -Algorithm SHA256 "$out.zip").Hash.ToLower() | Set-Content "$out.zip.sha256"

# Optional signed installer (Inno Setup 6 + your code-signing certificate).
$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (Test-Path $iscc) { & $iscc "/DAppVersion=$ver" packaging\windows\motor_hmi.iss }
Write-Host "Built $out.zip"
Write-Host "Sign before release:  signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /a <exe>"

# Builds dist\llm-switchboard\llm-switchboard.exe (onedir - keep the folder together).
# Usage:  .\build.ps1                  normal build
#         .\build.ps1 -OneFile         single-file exe instead (slower first launch)
#         .\build.ps1 -Dist dist-next  build elsewhere while the current exe is still running

param([switch]$OneFile, [string]$Dist = "dist")

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Qt ships far more than this app uses; excluding the heavy modules roughly halves the output.
$excludes = @(
    "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebEngineQuick",
    "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.QtQuickWidgets", "PySide6.QtQml",
    "PySide6.Qt3DCore", "PySide6.Qt3DRender", "PySide6.Qt3DInput", "PySide6.Qt3DLogic",
    "PySide6.Qt3DAnimation", "PySide6.Qt3DExtras",
    "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtSpatialAudio",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtGraphs",
    "PySide6.QtBluetooth", "PySide6.QtNfc", "PySide6.QtPositioning", "PySide6.QtLocation",
    "PySide6.QtSerialPort", "PySide6.QtSerialBus", "PySide6.QtSql", "PySide6.QtTest",
    "PySide6.QtHelp", "PySide6.QtDesigner", "PySide6.QtUiTools", "PySide6.QtWebSockets",
    "PySide6.QtWebChannel", "PySide6.QtWebView", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    "PySide6.QtSensors", "PySide6.QtTextToSpeech", "PySide6.QtNetworkAuth",
    "PySide6.QtRemoteObjects", "PySide6.QtScxml", "PySide6.QtStateMachine",
    "tkinter", "numpy", "PIL", "matplotlib", "scipy", "pandas"
)

# Not $args - that shadows PowerShell's automatic variable.
$piArgs = @("--noconfirm", "--clean", "--windowed", "--noupx",
            "--name", "llm-switchboard", "--paths", ".", "--distpath", $Dist,
            "--icon", "assets\icon.ico", "--add-data", "assets\icon.png;assets")
if ($OneFile) { $piArgs += "--onefile" } else { $piArgs += "--onedir" }
foreach ($e in $excludes) { $piArgs += @("--exclude-module", $e) }
$piArgs += "main.py"

Write-Host "Building…" -ForegroundColor Cyan
& python -m PyInstaller @piArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
Start-Sleep -Milliseconds 300  # let PyInstaller finish replacing dist\ before measuring it

$exe = if ($OneFile) { "$Dist\llm-switchboard.exe" } else { "$Dist\llm-switchboard\llm-switchboard.exe" }
if (-not (Test-Path $exe)) { throw "Expected output missing: $exe" }

$size = if ($OneFile) {
    (Get-Item $exe).Length
} else {
    (Get-ChildItem "$Dist\llm-switchboard" -Recurse -File | Measure-Object -Property Length -Sum).Sum
}
Write-Host ""
Write-Host "Built: $((Resolve-Path $exe).Path)" -ForegroundColor Green
Write-Host ("Total size: {0:N0} MB" -f ($size / 1MB)) -ForegroundColor Green

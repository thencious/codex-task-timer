$ErrorActionPreference = 'Stop'
$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $project '.build-venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Create .build-venv and install PyInstaller there first.'
}
Push-Location $project
try {
    $icon = Join-Path $project 'assets\codex-task-timer.ico'
    $assets = (Join-Path $project 'assets') + ';assets'
    $entry = Join-Path $project 'Launch.pyw'
    & $python -m PyInstaller --noconfirm --clean --onefile --windowed --name CodexTaskTimer --icon $icon --add-data $assets --distpath $project --workpath .build/work --specpath .build $entry
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed.' }
} finally {
    Pop-Location
}

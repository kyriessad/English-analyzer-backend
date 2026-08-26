# Read-only release checks. This script never migrates, restarts, or edits data.
$ErrorActionPreference = 'Stop'
$BackendRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $BackendRoot '.venv\Scripts\python.exe'
Set-Location $BackendRoot

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw "Python virtual environment not found: $Python" }
foreach ($path in @('alembic.ini', 'start-server.ps1', 'stop-server.ps1', 'scripts\backup-postgresql.ps1', 'scripts\check_database_target.py')) {
  if (-not (Test-Path -LiteralPath (Join-Path $BackendRoot $path) -PathType Leaf)) { throw "Required release file missing: $path" }
}

& $Python scripts\check_config.py
if ($LASTEXITCODE -ne 0) { throw 'Configuration preflight failed.' }
& $Python scripts\release_preflight.py
if ($LASTEXITCODE -ne 0) { throw 'Database/code release preflight failed.' }
Write-Host 'RELEASE PREFLIGHT PASS'

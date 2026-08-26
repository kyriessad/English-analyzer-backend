[CmdletBinding()]
param(
  [ValidateSet(
    'single_user',
    'multi_user_isolation',
    'load_5',
    'load_10',
    'load_30',
    'load_100',
    'burst_http',
    'burst_ai',
    'burst_tts',
    'fault_ollama',
    'fault_piper',
    'fault_db_pool',
    'all'
  )]
  [string]$Through = 'single_user'
)

$ErrorActionPreference = 'Stop'
$BackendDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $BackendDir '.venv\Scripts\python.exe'
$Runner = Join-Path $BackendDir 'e2e\run_level7_e2e.py'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
  throw "Project Python was not found: $Python"
}
if (-not (Test-Path -LiteralPath $Runner -PathType Leaf)) {
  throw "Level 7 runner was not found: $Runner"
}
if ([string]::IsNullOrWhiteSpace($env:DATABASE_URL)) {
  throw 'DATABASE_URL must be present in the process environment; .env is not modified.'
}

& $Python $Runner --through $Through
exit $LASTEXITCODE

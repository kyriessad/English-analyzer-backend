[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$BackendDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $BackendDir '.venv\Scripts\python.exe'
$Runner = Join-Path $BackendDir 'e2e\run_level7_wechat_crud_e2e.py'

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
  throw "Project Python was not found: $Python"
}
if (-not (Test-Path -LiteralPath $Runner -PathType Leaf)) {
  throw "WeChat CRUD E2E runner was not found: $Runner"
}
if ([string]::IsNullOrWhiteSpace($env:DATABASE_URL)) {
  throw 'DATABASE_URL must be present in the process environment; .env is not modified.'
}

& $Python $Runner
exit $LASTEXITCODE

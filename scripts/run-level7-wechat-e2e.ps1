[CmdletBinding()]
param(
  [switch]$AuthOnly
)

$ErrorActionPreference = 'Stop'
$BackendDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $BackendDir '.venv\Scripts\python.exe'
$RunnerName = if ($AuthOnly) { 'run_level7_wechat_auth_e2e.py' } else { 'run_level7_wechat_e2e.py' }
$Runner = Join-Path $BackendDir (Join-Path 'e2e' $RunnerName)

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
  throw "Project Python was not found: $Python"
}
if (-not (Test-Path -LiteralPath $Runner -PathType Leaf)) {
  throw "WeChat client E2E runner was not found: $Runner"
}
if ([string]::IsNullOrWhiteSpace($env:DATABASE_URL)) {
  throw 'DATABASE_URL must be present in the process environment; .env is not modified.'
}
& $Python $Runner
exit $LASTEXITCODE

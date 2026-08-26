#Requires -Version 5.1
[CmdletBinding()]
param(
  [string]$Target = "",
  [switch]$Force,
  [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
$BackendDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$PythonExe = Join-Path $BackendDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
  Write-Error "ECDICT SETUP FAILED: project virtual environment not found: $PythonExe"
  exit 1
}
$arguments = @((Join-Path $BackendDir 'scripts\build_ecdict.py'))
if ($Target) { $arguments += @('--target', $Target) }
if ($Force) { $arguments += '--force' }
if ($ValidateOnly) { $arguments += '--validate-only' }
& $PythonExe @arguments
exit $LASTEXITCODE

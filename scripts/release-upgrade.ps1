#Requires -Version 5.1
<#
.SYNOPSIS
  Explicit one-command release and PostgreSQL schema upgrade.

.DESCRIPTION
  Runs the existing release gates in this order:
  preflight, PostgreSQL-backed tests, validated backup, application stop,
  Alembic upgrade (only when needed), revision verification, the existing
  start-server.ps1 entrypoint, health, and a read-only smoke test.

  This script never restores, downgrades, stamps, or edits migration files.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:BackendRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$script:PythonExe = Join-Path $script:BackendRoot '.venv\Scripts\python.exe'
$script:AlembicExe = Join-Path $script:BackendRoot '.venv\Scripts\alembic.exe'
$script:PowerShellExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$script:PreflightScript = Join-Path $script:BackendRoot 'scripts\preflight-release.ps1'
$script:TestsScript = Join-Path $script:BackendRoot 'scripts\run-postgresql-tests.ps1'
$script:BackupScript = Join-Path $script:BackendRoot 'scripts\backup-postgresql.ps1'
$script:StopScript = Join-Path $script:BackendRoot 'stop-server.ps1'
$script:StartScript = Join-Path $script:BackendRoot 'start-server.ps1'
$script:DatabaseCheckScript = Join-Path $script:BackendRoot 'scripts\check_database_target.py'
$script:ReleasePreflightPython = Join-Path $script:BackendRoot 'scripts\release_preflight.py'
$script:DiscoverySeedScript = Join-Path $script:BackendRoot 'scripts\seed_discovery_content.py'
$script:PgDumpExe = 'C:\Program Files\PostgreSQL\16\bin\pg_dump.exe'
$script:PgRestoreExe = 'C:\Program Files\PostgreSQL\16\bin\pg_restore.exe'
$script:HealthUrl = 'http://127.0.0.1:8000/health'
$script:MetricsUrl = 'http://127.0.0.1:8000/metrics'
$script:StdoutLog = Join-Path $script:BackendRoot 'logs\server.out.log'
$script:StderrLog = Join-Path $script:BackendRoot 'logs\server.err.log'

function Write-ReleaseStep {
  param(
    [int]$Number,
    [string]$Name
  )

  Write-Host ''
  Write-Host ("[{0}/9] {1}" -f $Number, $Name) -ForegroundColor Cyan
}

function Write-CommandOutput {
  param([object[]]$Output)

  foreach ($line in $Output) {
    Write-Host ([string]$line)
  }
}

function Get-OutputValue {
  param(
    [object[]]$Output,
    [string]$Label
  )

  $line = $Output |
    ForEach-Object { [string]$_ } |
    Where-Object { $_ -like "$Label*" } |
    Select-Object -First 1
  if (-not $line) { return $null }
  return $line.Substring($Label.Length).Trim()
}

function Invoke-ChildPowerShell {
  param(
    [string]$ScriptPath,
    [string[]]$Arguments = @()
  )

  $commandArguments = @(
    '-NoProfile',
    '-ExecutionPolicy', 'Bypass',
    '-File', $ScriptPath
  ) + $Arguments
  $output = @(& $script:PowerShellExe @commandArguments 2>&1)
  $exitCode = $LASTEXITCODE
  Write-CommandOutput -Output $output
  return [pscustomobject]@{
    ExitCode = $exitCode
    Output = [object[]]$output
  }
}

function Invoke-NativeCaptured {
  param(
    [string]$FilePath,
    [string[]]$Arguments = @(),
    [switch]$SuppressOutput
  )

  $previousErrorActionPreference = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    # Alembic writes normal INFO logs to stderr. Judge native commands by their
    # exit code, while still retaining stderr for a useful failure report.
    $output = @(& $FilePath @Arguments 2>&1)
    $exitCode = $LASTEXITCODE
  }
  finally {
    $ErrorActionPreference = $previousErrorActionPreference
  }
  if (-not $SuppressOutput) { Write-CommandOutput -Output $output }
  return [pscustomobject]@{
    ExitCode = $exitCode
    Output = [object[]]$output
  }
}

function Assert-ReleaseDependencies {
  $requiredFiles = @(
    $script:PythonExe,
    $script:AlembicExe,
    $script:PowerShellExe,
    $script:PreflightScript,
    $script:TestsScript,
    $script:BackupScript,
    $script:StopScript,
    $script:StartScript,
    $script:DatabaseCheckScript,
    $script:ReleasePreflightPython,
    $script:DiscoverySeedScript,
    $script:PgDumpExe,
    $script:PgRestoreExe,
    (Join-Path $script:BackendRoot 'alembic.ini')
  )
  foreach ($path in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
      throw "Required release dependency is missing: $path"
    }
  }

  $alembicVersion = @(& $script:AlembicExe --version 2>&1)
  if ($LASTEXITCODE -ne 0) { throw 'Project Alembic executable is not usable.' }
  $pgDumpVersion = @(& $script:PgDumpExe --version 2>&1)
  if ($LASTEXITCODE -ne 0) { throw 'Approved pg_dump executable is not usable.' }
  $pgRestoreVersion = @(& $script:PgRestoreExe --version 2>&1)
  if ($LASTEXITCODE -ne 0) { throw 'Approved pg_restore executable is not usable.' }
}

function Invoke-ReleasePreflight {
  $result = Invoke-ChildPowerShell -ScriptPath $script:PreflightScript
  if ($result.ExitCode -ne 0) { throw 'Existing release preflight failed.' }

  $current = Get-OutputValue -Output $result.Output -Label 'Current Alembic revision:'
  $target = Get-OutputValue -Output $result.Output -Label 'Code Alembic head:'
  if ([string]::IsNullOrWhiteSpace($current) -or [string]::IsNullOrWhiteSpace($target)) {
    throw 'Existing release preflight did not report current and target revisions.'
  }
  return [pscustomobject]@{
    Current = $current
    Target = $target
  }
}

function Invoke-ReleaseTests {
  $result = Invoke-ChildPowerShell -ScriptPath $script:TestsScript
  if ($result.ExitCode -ne 0) { throw 'PostgreSQL-backed test suite failed.' }
}

function Invoke-ReleaseBackup {
  $result = Invoke-ChildPowerShell `
    -ScriptPath $script:BackupScript `
    -Arguments @('-AllowRevisionMismatch')
  if ($result.ExitCode -ne 0) { throw 'Validated PostgreSQL backup failed.' }

  $success = $result.Output |
    ForEach-Object { ([string]$_).Trim() } |
    Where-Object { $_ -eq 'Backup SUCCESS' } |
    Select-Object -First 1
  $path = Get-OutputValue -Output $result.Output -Label 'Backup path:'
  $revision = Get-OutputValue -Output $result.Output -Label 'Alembic revision:'
  $sha256 = Get-OutputValue -Output $result.Output -Label 'SHA-256:'
  if (-not $success -or [string]::IsNullOrWhiteSpace($path) -or
      [string]::IsNullOrWhiteSpace($revision) -or [string]::IsNullOrWhiteSpace($sha256)) {
    throw 'Backup script did not report a complete validated backup result.'
  }
  if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
    throw "Validated backup file is missing: $path"
  }
  $actualSha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
  if ($actualSha256 -ne $sha256) {
    throw 'Backup SHA-256 verification failed after backup completion.'
  }

  return [pscustomobject]@{
    Path = $path
    Revision = $revision
    Sha256 = $actualSha256
  }
}

function Test-FastApiPortListening {
  return $null -ne (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)
}

function Invoke-ReleaseStop {
  $result = Invoke-ChildPowerShell -ScriptPath $script:StopScript
  $reportedStopped = $result.Output |
    ForEach-Object { ([string]$_).Trim() } |
    Where-Object { $_ -eq 'SERVER STOPPED' } |
    Select-Object -First 1
  if ($result.ExitCode -ne 0 -or -not $reportedStopped) {
    throw 'Existing stop-server.ps1 did not report a complete stop.'
  }
  if (Test-FastApiPortListening) {
    throw 'FastAPI port 8000 is still listening after stop-server.ps1.'
  }
}

function Invoke-ReleaseMigration {
  $result = Invoke-NativeCaptured -FilePath $script:AlembicExe -Arguments @('upgrade', 'head')
  if ($result.ExitCode -ne 0) { throw 'alembic upgrade head failed.' }
}

function Invoke-ReleaseRevisionVerify {
  $result = Invoke-NativeCaptured -FilePath $script:PythonExe -Arguments @($script:DatabaseCheckScript)
  $actual = Get-OutputValue -Output $result.Output -Label 'Actual Alembic revision:'
  $expected = Get-OutputValue -Output $result.Output -Label 'Expected Alembic revision:'
  if ($result.ExitCode -ne 0 -or [string]::IsNullOrWhiteSpace($actual) -or
      [string]::IsNullOrWhiteSpace($expected) -or $actual -ne $expected) {
    throw 'Database revision does not match the code Alembic head.'
  }
  return [pscustomobject]@{
    Actual = $actual
    Expected = $expected
  }
}

function Invoke-ReleaseDiscoveryImport {
  $result = Invoke-NativeCaptured -FilePath $script:PythonExe -Arguments @($script:DiscoverySeedScript, '--word-limit', '500')
  if ($result.ExitCode -ne 0) { throw 'Discovery content import failed.' }
}

function Get-CurrentRevisionBestEffort {
  param([string]$Fallback)

  try {
    $result = Invoke-NativeCaptured `
      -FilePath $script:PythonExe `
      -Arguments @($script:ReleasePreflightPython) `
      -SuppressOutput
    if ($result.ExitCode -eq 0) {
      $current = Get-OutputValue -Output $result.Output -Label 'Current Alembic revision:'
      if (-not [string]::IsNullOrWhiteSpace($current)) { return $current }
    }
  }
  catch { }
  return $Fallback
}

function Invoke-ReleaseStart {
  # Do not capture this child through a pipe. start-server.ps1 launches ngrok,
  # which can inherit a redirected pipe handle and keep the release process
  # waiting for EOF after the startup script itself has already exited.
  & $script:PowerShellExe `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File $script:StartScript
  if ($LASTEXITCODE -ne 0) {
    throw 'Existing start-server.ps1 did not complete successfully.'
  }
}

function Invoke-ReleaseHealth {
  $response = Invoke-RestMethod -Uri $script:HealthUrl -Method Get -TimeoutSec 10
  if ($response.status -ne 'ok') {
    throw 'FastAPI health response was not status=ok.'
  }
}

function Invoke-ReleaseSmoke {
  $health = Invoke-RestMethod -Uri $script:HealthUrl -Method Get -TimeoutSec 10
  if ($health.status -ne 'ok') {
    throw 'Smoke health request was not status=ok.'
  }

  $metrics = Invoke-WebRequest -Uri $script:MetricsUrl -Method Get -TimeoutSec 10 -UseBasicParsing
  if ($metrics.StatusCode -ne 200 -or [string]::IsNullOrWhiteSpace([string]$metrics.Content)) {
    throw 'Read-only metrics smoke request failed.'
  }
}

function Write-ReleaseFailure {
  param(
    [string]$Step,
    [string]$Reason,
    [string]$BackupPath,
    [string]$CurrentRevision,
    [string]$TargetRevision,
    [bool]$MigrationAttempted,
    [bool]$StartAttempted
  )

  Write-Host ''
  Write-Host '========================================' -ForegroundColor Red
  if ($MigrationAttempted) {
    Write-Host 'RELEASE FAILED AFTER MIGRATION' -ForegroundColor Red
  }
  else {
    Write-Host 'RELEASE UPGRADE FAILED' -ForegroundColor Red
  }
  Write-Host '========================================' -ForegroundColor Red
  Write-Host "Step: $Step"
  Write-Host "Reason: $Reason"
  if ($BackupPath) { Write-Host "Backup: $BackupPath" }
  if ($CurrentRevision) { Write-Host "Database current revision: $CurrentRevision" }
  if ($TargetRevision) { Write-Host "Target revision: $TargetRevision" }
  if ($Step -in @('Start', 'Health', 'Smoke')) {
    Write-Host "Logs: $script:StdoutLog ; $script:StderrLog"
  }
  if ($Step -eq 'Tests') {
    Write-Host 'TESTS FAILED' -ForegroundColor Red
    Write-Host 'RELEASE ABORTED' -ForegroundColor Red
  }
  if ($Step -eq 'Migration') {
    Write-Host 'MIGRATION FAILED' -ForegroundColor Red
    Write-Host 'DATABASE MAY REQUIRE MANUAL INSPECTION' -ForegroundColor Red
    if ($BackupPath) { Write-Host "BACKUP: $BackupPath" }
  }
  if ($StartAttempted) {
    Write-Host 'Start was attempted; inspect the reported process state and logs.'
  }
  else {
    Write-Host 'Server was NOT started.'
  }
  throw 'RELEASE_UPGRADE_ABORTED'
}

function Invoke-ReleaseUpgrade {
  Set-Location $script:BackendRoot

  $beforeRevision = $null
  $targetRevision = $null
  $afterRevision = $null
  $backupPath = $null
  $backupSha256 = $null
  $migrationAttempted = $false
  $migrationStatus = 'PASS'
  $startAttempted = $false

  Write-ReleaseStep -Number 1 -Name 'Preflight'
  try {
    Assert-ReleaseDependencies
    $preflight = Invoke-ReleasePreflight
    $beforeRevision = $preflight.Current
    $targetRevision = $preflight.Target
  }
  catch {
    Write-ReleaseFailure -Step 'Preflight' -Reason $_.Exception.Message `
      -BackupPath $backupPath -CurrentRevision $beforeRevision -TargetRevision $targetRevision `
      -MigrationAttempted $migrationAttempted -StartAttempted $startAttempted
  }

  Write-ReleaseStep -Number 2 -Name 'Tests'
  try {
    Invoke-ReleaseTests
    Write-Host 'Tests: PASS' -ForegroundColor Green
  }
  catch {
    Write-ReleaseFailure -Step 'Tests' -Reason $_.Exception.Message `
      -BackupPath $backupPath -CurrentRevision $beforeRevision -TargetRevision $targetRevision `
      -MigrationAttempted $migrationAttempted -StartAttempted $startAttempted
  }

  Write-ReleaseStep -Number 3 -Name 'PostgreSQL backup'
  try {
    $backup = Invoke-ReleaseBackup
    $backupPath = $backup.Path
    $backupSha256 = $backup.Sha256
    if ($backup.Revision -ne $beforeRevision) {
      throw "Backup revision '$($backup.Revision)' does not match preflight revision '$beforeRevision'."
    }
  }
  catch {
    Write-ReleaseFailure -Step 'Backup' -Reason $_.Exception.Message `
      -BackupPath $backupPath -CurrentRevision $beforeRevision -TargetRevision $targetRevision `
      -MigrationAttempted $migrationAttempted -StartAttempted $startAttempted
  }

  Write-ReleaseStep -Number 4 -Name 'Stop application'
  try {
    Invoke-ReleaseStop
  }
  catch {
    Write-ReleaseFailure -Step 'Stop' -Reason $_.Exception.Message `
      -BackupPath $backupPath -CurrentRevision $beforeRevision -TargetRevision $targetRevision `
      -MigrationAttempted $migrationAttempted -StartAttempted $startAttempted
  }

  Write-ReleaseStep -Number 5 -Name 'Migration'
  Write-Host "Current: $beforeRevision"
  Write-Host "Target:  $targetRevision"
  if ($beforeRevision -eq $targetRevision) {
    $migrationStatus = 'SKIPPED (already at head)'
    Write-Host 'DATABASE ALREADY AT HEAD' -ForegroundColor Yellow
  }
  else {
    $migrationAttempted = $true
    try {
      Invoke-ReleaseMigration
      Write-Host 'Migration: PASS' -ForegroundColor Green
    }
    catch {
      $currentAfterFailure = Get-CurrentRevisionBestEffort -Fallback $beforeRevision
      Write-ReleaseFailure -Step 'Migration' -Reason $_.Exception.Message `
        -BackupPath $backupPath -CurrentRevision $currentAfterFailure -TargetRevision $targetRevision `
        -MigrationAttempted $migrationAttempted -StartAttempted $startAttempted
    }
  }

  Write-ReleaseStep -Number 6 -Name 'Revision verification'
  try {
    $verification = Invoke-ReleaseRevisionVerify
    $afterRevision = $verification.Actual
    if ($afterRevision -ne $targetRevision) {
      throw "Verified revision '$afterRevision' does not match preflight target '$targetRevision'."
    }
    Write-Host "$afterRevision == $targetRevision"
    Invoke-ReleaseDiscoveryImport
    Write-Host 'Discovery content: PASS' -ForegroundColor Green
  }
  catch {
    $currentAfterFailure = Get-CurrentRevisionBestEffort -Fallback $beforeRevision
    Write-ReleaseFailure -Step 'Revision verification' -Reason $_.Exception.Message `
      -BackupPath $backupPath -CurrentRevision $currentAfterFailure -TargetRevision $targetRevision `
      -MigrationAttempted $migrationAttempted -StartAttempted $startAttempted
  }

  Write-ReleaseStep -Number 7 -Name 'Start'
  $startAttempted = $true
  try {
    Invoke-ReleaseStart
    Write-Host 'Start: PASS' -ForegroundColor Green
  }
  catch {
    Write-ReleaseFailure -Step 'Start' -Reason $_.Exception.Message `
      -BackupPath $backupPath -CurrentRevision $afterRevision -TargetRevision $targetRevision `
      -MigrationAttempted $migrationAttempted -StartAttempted $startAttempted
  }

  Write-ReleaseStep -Number 8 -Name 'Health'
  try {
    Invoke-ReleaseHealth
    Write-Host 'Health: PASS' -ForegroundColor Green
  }
  catch {
    Write-ReleaseFailure -Step 'Health' -Reason $_.Exception.Message `
      -BackupPath $backupPath -CurrentRevision $afterRevision -TargetRevision $targetRevision `
      -MigrationAttempted $migrationAttempted -StartAttempted $startAttempted
  }

  Write-ReleaseStep -Number 9 -Name 'Smoke'
  try {
    Invoke-ReleaseSmoke
    Write-Host 'Smoke: PASS' -ForegroundColor Green
  }
  catch {
    Write-ReleaseFailure -Step 'Smoke' -Reason $_.Exception.Message `
      -BackupPath $backupPath -CurrentRevision $afterRevision -TargetRevision $targetRevision `
      -MigrationAttempted $migrationAttempted -StartAttempted $startAttempted
  }

  Write-Host ''
  Write-Host '========================================' -ForegroundColor Green
  Write-Host 'RELEASE UPGRADE PASS' -ForegroundColor Green
  Write-Host '========================================' -ForegroundColor Green
  Write-Host ''
  Write-Host 'Tests: PASS'
  Write-Host ''
  Write-Host 'Backup:'
  Write-Host $backupPath
  Write-Host "SHA-256: $backupSha256"
  Write-Host ''
  Write-Host 'Database:'
  Write-Host "Before: $beforeRevision"
  Write-Host "After:  $afterRevision"
  Write-Host "Code:   $targetRevision"
  Write-Host ''
  Write-Host "Migration: $migrationStatus"
  Write-Host 'Start: PASS'
  Write-Host 'Health: PASS'
  Write-Host 'Smoke: PASS'
  Write-Host ''
  Write-Host 'SERVER READY' -ForegroundColor Green
  Write-Host '========================================' -ForegroundColor Green
}

if ($MyInvocation.InvocationName -ne '.') {
  try {
    Invoke-ReleaseUpgrade
    exit 0
  }
  catch {
    exit 1
  }
}

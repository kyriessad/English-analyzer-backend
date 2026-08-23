$ErrorActionPreference = "Stop"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendRoot = Split-Path -Parent $ScriptRoot
$Python = Join-Path $BackendRoot ".venv\Scripts\python.exe"
$PgDump = "C:\Program Files\PostgreSQL\16\bin\pg_dump.exe"
$PgRestore = "C:\Program Files\PostgreSQL\16\bin\pg_restore.exe"
$BackupDirectory = "C:\Users\Administrator\PostgreSQLBackups\english_analyzer"
$ExpectedDatabase = "english_analyzer"
$RetentionDays = 14

function Remove-PartialBackup {
  param([string]$Path)

  if (Test-Path -LiteralPath $Path) {
    Remove-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
  }
}

if (-not (Test-Path -LiteralPath $Python)) {
  throw "Backend virtual environment not found."
}
if (-not (Test-Path -LiteralPath $PgDump)) {
  throw "pg_dump.exe not found at the approved PostgreSQL 16 path."
}
if (-not (Test-Path -LiteralPath $PgRestore)) {
  throw "pg_restore.exe not found at the approved PostgreSQL 16 path."
}

Set-Location $BackendRoot

# Read the existing application configuration without writing its URL or password to output.
$connectionCode = @'
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))

try:
    from sqlalchemy.engine import make_url
    from app.core.config import settings

    url = make_url(settings.database_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("DATABASE_URL is not PostgreSQL")
    if not url.host or not url.username or not url.database:
        raise ValueError("DATABASE_URL is missing required PostgreSQL connection fields")

    print(json.dumps({
        "host": url.host,
        "port": url.port or 5432,
        "username": url.username,
        "password": str(url.password) if url.password is not None else "",
        "database": url.database,
        "required_alembic_revision": settings.required_alembic_revision,
    }))
except Exception:
    print("Could not read the approved PostgreSQL connection configuration.", file=sys.stderr)
    sys.exit(1)
'@

$connectionJson = $connectionCode | & $Python -
if ($LASTEXITCODE -ne 0) {
  throw "Could not read the approved PostgreSQL connection configuration."
}
$connection = $connectionJson | ConvertFrom-Json

$ExpectedAlembicRevision = [string]$connection.required_alembic_revision
if ([string]::IsNullOrWhiteSpace($ExpectedAlembicRevision)) {
  throw "Required Alembic revision is not configured."
}

$preflightOutput = & $Python scripts\check_database_target.py
if ($LASTEXITCODE -ne 0) {
  throw "Database target preflight failed."
}

$revisionLine = $preflightOutput | Where-Object { $_ -like "Alembic revision:*" } | Select-Object -First 1
if ($revisionLine -ne "Alembic revision: $ExpectedAlembicRevision") {
  throw "Expected Alembic revision was not confirmed."
}

if ([string]$connection.database -ne $ExpectedDatabase) {
  throw "Configured database is not the approved production database."
}

New-Item -ItemType Directory -Path $BackupDirectory -Force | Out-Null

$createdAt = Get-Date
$timestamp = $createdAt.ToString("yyyyMMdd_HHmmss")
$backupName = "${ExpectedDatabase}_prod_${timestamp}_${ExpectedAlembicRevision}.dump"
$backupPath = Join-Path $BackupDirectory $backupName
$temporaryPath = "${backupPath}.partial"

if (Test-Path -LiteralPath $backupPath) {
  throw "Backup filename collision. Try again after the current second."
}
Remove-PartialBackup -Path $temporaryPath

$previousPgPassword = [Environment]::GetEnvironmentVariable("PGPASSWORD", "Process")
$hadPgPassword = $null -ne $previousPgPassword

try {
  # PGPASSWORD is process-scoped and is never echoed or included in command arguments.
  if ([string]::IsNullOrEmpty([string]$connection.password)) {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
  }
  else {
    $env:PGPASSWORD = [string]$connection.password
  }

  $pgDumpVersion = (& $PgDump --version).Trim()
  if ($LASTEXITCODE -ne 0) {
    throw "Could not determine pg_dump version."
  }

  & $PgDump `
    "--format=custom" `
    "--file=$temporaryPath" `
    "--host=$($connection.host)" `
    "--port=$($connection.port)" `
    "--username=$($connection.username)" `
    "--dbname=$($connection.database)" `
    "--no-password"
  if ($LASTEXITCODE -ne 0) {
    throw "pg_dump failed."
  }

  if (-not (Test-Path -LiteralPath $temporaryPath)) {
    throw "pg_dump did not create a backup file."
  }
  if ((Get-Item -LiteralPath $temporaryPath).Length -le 0) {
    throw "pg_dump created an empty backup file."
  }

  & $PgRestore --list $temporaryPath | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "pg_restore --list could not read the backup archive."
  }

  Move-Item -LiteralPath $temporaryPath -Destination $backupPath
  $backupFile = Get-Item -LiteralPath $backupPath
  $sha256 = (Get-FileHash -LiteralPath $backupPath -Algorithm SHA256).Hash

  # Retention runs only after this archive has passed all validation checks.
  $cutoff = (Get-Date).AddDays(-$RetentionDays)
  $expiredBackups = Get-ChildItem -LiteralPath $BackupDirectory -File -Filter "*.dump" |
    Where-Object { $_.LastWriteTime -lt $cutoff }
  foreach ($expiredBackup in $expiredBackups) {
    try {
      Remove-Item -LiteralPath $expiredBackup.FullName -Force -ErrorAction Stop
    }
    catch {
      Write-Warning "Could not remove expired backup: $($expiredBackup.Name)"
    }
  }

  Write-Host "Backup SUCCESS"
  Write-Host "Database: $ExpectedDatabase"
  Write-Host "PostgreSQL / pg_dump version: $pgDumpVersion"
  Write-Host "Alembic revision: $ExpectedAlembicRevision"
  Write-Host "Backup path: $backupPath"
  Write-Host "File size: $($backupFile.Length) bytes"
  Write-Host "SHA-256: $sha256"
  Write-Host "Created time: $($createdAt.ToString('yyyy-MM-dd HH:mm:ss K'))"
}
catch {
  Remove-PartialBackup -Path $temporaryPath
  Write-Host "Backup FAILED"
  Write-Host "Database: $ExpectedDatabase"
  Write-Host "Reason: $($_.Exception.Message)"
  exit 1
}
finally {
  if ($hadPgPassword) {
    $env:PGPASSWORD = $previousPgPassword
  }
  else {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
  }
}

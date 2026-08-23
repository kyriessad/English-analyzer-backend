param(
  [Parameter(Mandatory = $true)]
  [string]$BackupPath,

  [Parameter(Mandatory = $true)]
  [string]$TargetDatabase,

  [string]$ExpectedSha256,

  [string]$PostgresHost,

  [ValidateRange(1, 65535)]
  [int]$PostgresPort
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendRoot = Split-Path -Parent $ScriptRoot
$Python = Join-Path $BackendRoot ".venv\Scripts\python.exe"
$PgRestore = "C:\Program Files\PostgreSQL\16\bin\pg_restore.exe"
$ProductionDatabase = "english_analyzer"
$ProtectedRestoreDatabase = "english_analyzer_restore_test"

function Invoke-PythonCode {
  param(
    [string]$Code,
    [string[]]$ScriptArguments = @()
  )

  # Base64 keeps multiline Python code intact when this script runs in Windows PowerShell.
  $encodedCode = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Code))
  $runner = "import base64;exec(compile(base64.b64decode('$encodedCode'), '<restore-helper>', 'exec'))"
  & $Python -c $runner @ScriptArguments
}

function Write-BlockedTargetError {
  param([string]$DatabaseName)

  [Console]::Error.WriteLine(
    "ERROR: 目标数据库已存在：$DatabaseName。Restore 已中止；未执行 pg_restore。" +
    "请保留它并改用新的数据库名，或在确认无用后手动删除该数据库再 Restore。"
  )
  [Console]::Out.WriteLine("pg_restore executed: no")
  exit 3
}

if ($TargetDatabase -eq $ProductionDatabase) {
  throw "Refusing to restore to the production database."
}
if ($TargetDatabase -eq $ProtectedRestoreDatabase) {
  throw "Refusing to restore to the protected successful restore database."
}
if ($TargetDatabase -notmatch '^english_analyzer_restore_[a-z0-9_]+$') {
  throw "Target database name must use the english_analyzer_restore_* prefix."
}
if (-not (Test-Path -LiteralPath $Python)) {
  throw "Backend virtual environment not found."
}

Set-Location $BackendRoot

# Read application connection settings without echoing the URL or password.
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
    }))
except Exception:
    print("Could not read the approved PostgreSQL connection configuration.", file=sys.stderr)
    sys.exit(1)
'@

$connectionJson = Invoke-PythonCode -Code $connectionCode
if ($LASTEXITCODE -ne 0) {
  throw "Could not read the approved PostgreSQL connection configuration."
}
$connection = $connectionJson | ConvertFrom-Json
if ([string]$connection.database -ne $ProductionDatabase) {
  throw "Configured database is not the approved production database."
}
if ($PSBoundParameters.ContainsKey("PostgresHost")) {
  $connection.host = $PostgresHost
}
if ($PSBoundParameters.ContainsKey("PostgresPort")) {
  $connection.port = $PostgresPort
}
$connectionJson = $connection | ConvertTo-Json -Compress

$previousRestoreConnection = [Environment]::GetEnvironmentVariable("RESTORE_CONNECTION_JSON", "Process")
$hadRestoreConnection = $null -ne $previousRestoreConnection

try {
  [Environment]::SetEnvironmentVariable("RESTORE_CONNECTION_JSON", $connectionJson, "Process")

  # This is deliberately before database creation and before every pg_restore invocation.
  $databaseExistsCode = @'
import json
import os
import sys

import psycopg

connection = json.loads(os.environ["RESTORE_CONNECTION_JSON"])
target = sys.argv[1]
with psycopg.connect(
    host=connection["host"],
    port=connection["port"],
    user=connection["username"],
    password=connection["password"],
    dbname="postgres",
) as admin_connection:
    exists = admin_connection.execute(
        "SELECT 1 FROM pg_database WHERE datname = %s", (target,)
    ).fetchone() is not None
print("exists" if exists else "missing")
'@
  $databaseStateOutput = Invoke-PythonCode -Code $databaseExistsCode -ScriptArguments @($TargetDatabase)
  $databaseState = ""
  if ($null -ne $databaseStateOutput) {
    $databaseState = ([string]$databaseStateOutput).Trim()
  }
  if ($LASTEXITCODE -ne 0 -or $databaseState -notin @("exists", "missing")) {
    throw "PostgreSQL connection or target-database preflight failed. Restore was stopped before target database creation."
  }
  if ($databaseState -eq "exists") {
    Write-BlockedTargetError -DatabaseName $TargetDatabase
  }

  if (-not (Test-Path -LiteralPath $BackupPath)) {
    throw "Backup file not found."
  }
  if ((Get-Item -LiteralPath $BackupPath).Length -le 0) {
    throw "Backup file is empty."
  }
  if ($ExpectedSha256) {
    $actualSha256 = (Get-FileHash -LiteralPath $BackupPath -Algorithm SHA256).Hash
    if ($actualSha256 -ne $ExpectedSha256.ToUpperInvariant()) {
      throw "Backup SHA-256 does not match the expected value."
    }
  }
  if (-not (Test-Path -LiteralPath $PgRestore)) {
    throw "pg_restore.exe not found at the approved PostgreSQL 16 path."
  }

  # Validate the complete archive before creating a target database or restoring data.
  & $PgRestore --list $BackupPath | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "pg_restore --list could not read the backup archive. Restore was stopped before target database creation."
  }

  $createDatabaseCode = @'
import json
import os
import sys

import psycopg
from psycopg import sql

connection = json.loads(os.environ["RESTORE_CONNECTION_JSON"])
target = sys.argv[1]
with psycopg.connect(
    host=connection["host"],
    port=connection["port"],
    user=connection["username"],
    password=connection["password"],
    dbname="postgres",
    autocommit=True,
) as admin_connection:
    admin_connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
'@
  Invoke-PythonCode -Code $createDatabaseCode -ScriptArguments @($TargetDatabase)
  if ($LASTEXITCODE -ne 0) {
    throw "Could not create the empty target database."
  }

  $previousPgPassword = [Environment]::GetEnvironmentVariable("PGPASSWORD", "Process")
  $hadPgPassword = $null -ne $previousPgPassword
  try {
    if ([string]::IsNullOrEmpty([string]$connection.password)) {
      Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    }
    else {
      $env:PGPASSWORD = [string]$connection.password
    }

    # No --clean and no --create: only restore into the newly created empty target.
    & $PgRestore `
      "--host=$($connection.host)" `
      "--port=$($connection.port)" `
      "--username=$($connection.username)" `
      "--dbname=$TargetDatabase" `
      "--no-password" `
      "--no-owner" `
      "--no-privileges" `
      "--exit-on-error" `
      "--single-transaction" `
      "--verbose" `
      $BackupPath
    if ($LASTEXITCODE -ne 0) {
      throw "pg_restore failed. The target database was left in place for manual inspection."
    }
  }
  finally {
    if ($hadPgPassword) {
      $env:PGPASSWORD = $previousPgPassword
    }
    else {
      Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    }
  }

  Write-Host "Restore SUCCESS"
  Write-Host "Database: $TargetDatabase"
  Write-Host "pg_restore executed: yes"
}
catch {
  [Console]::Error.WriteLine("ERROR: $($_.Exception.Message)")
  exit 1
}
finally {
  if ($hadRestoreConnection) {
    [Environment]::SetEnvironmentVariable("RESTORE_CONNECTION_JSON", $previousRestoreConnection, "Process")
  }
  else {
    [Environment]::SetEnvironmentVariable("RESTORE_CONNECTION_JSON", $null, "Process")
  }
}

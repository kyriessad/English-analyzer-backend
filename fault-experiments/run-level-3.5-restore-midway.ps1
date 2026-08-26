<#
Level 3.5 Restore fault experiment 4: fail during CREATE TABLE public.cards.

Safety properties:
* Creates and writes only english_analyzer_restore_fault_midway.
* Refuses to run if that database already exists.
* Never invokes the production restore script or changes application settings.
* Leaves the target database and its fault-injection infrastructure in place.
#>

[CmdletBinding()]
param(
    [string]$DumpPath = $env:RESTORE_FAULT_DUMP_PATH,
    [string]$PgRestore = $env:PG_RESTORE_EXE
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$BackendRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $BackendRoot ".venv\Scripts\python.exe"
$TargetDatabase = "english_analyzer_restore_fault_midway"
$ProductionDatabase = "english_analyzer"
$ProtectedRestoreDatabase = "english_analyzer_restore_test"
$ExperimentDirectory = $PSScriptRoot
$TocListPath = Join-Path $ExperimentDirectory "level-3.5-restore-midway.toc.list"
$PgRestoreStdoutPath = Join-Path $ExperimentDirectory "level-3.5-restore-midway.pg_restore.stdout.log"
$PgRestoreStderrPath = Join-Path $ExperimentDirectory "level-3.5-restore-midway.pg_restore.stderr.log"

function Resolve-PgRestorePath {
    param([string]$RequestedPath)

    if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
        return $RequestedPath
    }

    $command = Get-Command pg_restore -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    return "C:\Program Files\PostgreSQL\16\bin\pg_restore.exe"
}

function Invoke-ExperimentPython {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Code,
        [string[]]$ScriptArguments = @()
    )

    $encodedCode = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Code))
    $runner = "import base64;exec(compile(base64.b64decode('$encodedCode'), '<restore-fault-experiment>', 'exec'))"
    $result = & $Python -c $runner @ScriptArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Experiment database helper failed."
    }
    return $result
}

function Get-FileHashRecord {
    param([Parameter(Mandatory = $true)][string]$Path)
    return [pscustomobject]@{
        Path = $Path
        SHA256 = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    }
}

if (-not (Test-Path -LiteralPath $Python)) { throw "Backend virtual environment not found." }
$PgRestore = Resolve-PgRestorePath -RequestedPath $PgRestore
if (-not (Test-Path -LiteralPath $PgRestore)) { throw "pg_restore executable not found. Pass -PgRestore or set PG_RESTORE_EXE." }
if ([string]::IsNullOrWhiteSpace($DumpPath)) { throw "Dump path is required. Pass -DumpPath or set RESTORE_FAULT_DUMP_PATH." }
if (-not (Test-Path -LiteralPath $DumpPath)) { throw "Approved dump not found at the provided DumpPath." }
if ($TargetDatabase -in @($ProductionDatabase, $ProtectedRestoreDatabase)) { throw "Unsafe target database." }
if ($TargetDatabase -notmatch '^english_analyzer_restore_[a-z0-9_]+$') { throw "Target database name is outside the approved test prefix." }

Set-Location $BackendRoot

# Read only the approved connection settings.  The resulting JSON remains in the
# process environment and is never emitted to output, because it includes a password.
$connectionCode = @'
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from sqlalchemy.engine import make_url
from app.core.config import settings

url = make_url(settings.database_url)
if not url.drivername.startswith("postgresql"):
    raise ValueError("DATABASE_URL is not PostgreSQL")
if not url.host or not url.username or not url.database:
    raise ValueError("DATABASE_URL is missing required PostgreSQL connection fields")
if url.database != "english_analyzer":
    raise ValueError("configured DATABASE_URL is not the approved production database")
print(json.dumps({
    "host": url.host,
    "port": url.port or 5432,
    "username": url.username,
    "password": str(url.password) if url.password is not None else "",
}))
'@
$connectionJson = Invoke-ExperimentPython -Code $connectionCode
$previousConnectionJson = [Environment]::GetEnvironmentVariable("RESTORE_FAULT_EXPERIMENT_CONNECTION_JSON", "Process")
$hadConnectionJson = $null -ne $previousConnectionJson
$previousPgPassword = [Environment]::GetEnvironmentVariable("PGPASSWORD", "Process")
$hadPgPassword = $null -ne $previousPgPassword

try {
    [Environment]::SetEnvironmentVariable("RESTORE_FAULT_EXPERIMENT_CONNECTION_JSON", $connectionJson, "Process")

    $databaseAuditCode = @'
import json
import os
import psycopg

connection = json.loads(os.environ["RESTORE_FAULT_EXPERIMENT_CONNECTION_JSON"])
with psycopg.connect(
    host=connection["host"], port=connection["port"], user=connection["username"],
    password=connection["password"], dbname="postgres", autocommit=True,
) as db:
    rows = db.execute("""
        SELECT datname, oid, pg_database_size(oid)::bigint AS size_bytes
        FROM pg_database
        WHERE datname = 'english_analyzer'
           OR datname LIKE 'english_analyzer_restore_%'
        ORDER BY datname
    """).fetchall()
print(json.dumps([
    {"name": row[0], "oid": row[1], "size_bytes": row[2]}
    for row in rows
], sort_keys=True))
'@

    $targetStateCode = @'
import json
import os
import sys
import psycopg

connection = json.loads(os.environ["RESTORE_FAULT_EXPERIMENT_CONNECTION_JSON"])
target = sys.argv[1]
with psycopg.connect(
    host=connection["host"], port=connection["port"], user=connection["username"],
    password=connection["password"], dbname="postgres", autocommit=True,
) as db:
    exists = db.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target,)).fetchone() is not None
print("exists" if exists else "missing")
'@

    $beforeFleet = Invoke-ExperimentPython -Code $databaseAuditCode
    $targetState = (Invoke-ExperimentPython -Code $targetStateCode -ScriptArguments @($TargetDatabase)).Trim()
    if ($targetState -ne "missing") {
        throw "Safety stop: $TargetDatabase already exists. It was not modified."
    }

    $hashesBefore = @(
        Get-FileHashRecord -Path $DumpPath
        Get-FileHashRecord -Path (Join-Path $BackendRoot ".env")
        Get-FileHashRecord -Path (Join-Path $BackendRoot "scripts\restore-postgresql.ps1")
    )
    & $PgRestore --list $DumpPath 1> $TocListPath
    if ($LASTEXITCODE -ne 0) { throw "pg_restore --list could not read the approved dump." }

    $createDatabaseCode = @'
import json
import os
import sys
import psycopg
from psycopg import sql

connection = json.loads(os.environ["RESTORE_FAULT_EXPERIMENT_CONNECTION_JSON"])
target = sys.argv[1]
with psycopg.connect(
    host=connection["host"], port=connection["port"], user=connection["username"],
    password=connection["password"], dbname="postgres", autocommit=True,
) as db:
    db.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
'@
    Invoke-ExperimentPython -Code $createDatabaseCode -ScriptArguments @($TargetDatabase) | Out-Null

    $installFaultCode = @'
import json
import os
import sys
import psycopg

connection = json.loads(os.environ["RESTORE_FAULT_EXPERIMENT_CONNECTION_JSON"])
target = sys.argv[1]
with psycopg.connect(
    host=connection["host"], port=connection["port"], user=connection["username"],
    password=connection["password"], dbname=target,
) as db:
    db.execute("""
        CREATE FUNCTION public.restore_fault_injection_midway()
        RETURNS event_trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            command_record record;
        BEGIN
            FOR command_record IN SELECT * FROM pg_event_trigger_ddl_commands()
            LOOP
                IF command_record.command_tag = 'CREATE TABLE'
                   AND command_record.object_type = 'table'
                   AND command_record.object_identity = 'public.cards'
                THEN
                    RAISE EXCEPTION 'RESTORE_FAULT_INJECTION: deliberate failure while restoring public.cards'
                        USING ERRCODE = 'P0001';
                END IF;
            END LOOP;
        END;
        $$;
    """)
    db.execute("""
        CREATE EVENT TRIGGER restore_fault_injection_midway
        ON ddl_command_end
        WHEN TAG IN ('CREATE TABLE')
        EXECUTE FUNCTION public.restore_fault_injection_midway();
    """)
'@
    Invoke-ExperimentPython -Code $installFaultCode -ScriptArguments @($TargetDatabase) | Out-Null

    $initialTargetAuditCode = @'
import json
import os
import sys
import psycopg

connection = json.loads(os.environ["RESTORE_FAULT_EXPERIMENT_CONNECTION_JSON"])
target = sys.argv[1]
with psycopg.connect(
    host=connection["host"], port=connection["port"], user=connection["username"],
    password=connection["password"], dbname=target, autocommit=True,
) as db:
    tables = db.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """).fetchall()
    trigger = db.execute("""
        SELECT evtname, evtevent FROM pg_event_trigger
        WHERE evtname = 'restore_fault_injection_midway'
    """).fetchall()
    function = db.execute("""
        SELECT p.oid::regprocedure::text
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname = 'restore_fault_injection_midway'
    """).fetchall()
print(json.dumps({
    "public_base_tables": [row[0] for row in tables],
    "event_triggers": [row[0] for row in trigger],
    "fault_functions": [row[0] for row in function],
}, sort_keys=True))
'@
    $initialTargetAudit = Invoke-ExperimentPython -Code $initialTargetAuditCode -ScriptArguments @($TargetDatabase)

    $connection = $connectionJson | ConvertFrom-Json
    if ([string]::IsNullOrEmpty([string]$connection.password)) {
        Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    } else {
        $env:PGPASSWORD = [string]$connection.password
    }

    $restoreArguments = @(
        "--host=$($connection.host)",
        "--port=$($connection.port)",
        "--username=$($connection.username)",
        "--dbname=$TargetDatabase",
        "--no-password",
        "--no-owner",
        "--no-privileges",
        "--exit-on-error",
        "--single-transaction",
        "--verbose",
        $DumpPath
    )
    & $PgRestore @restoreArguments 1> $PgRestoreStdoutPath 2> $PgRestoreStderrPath
    $pgRestoreExitCode = $LASTEXITCODE

    $afterTargetAuditCode = @'
import json
import os
import sys
import psycopg

connection = json.loads(os.environ["RESTORE_FAULT_EXPERIMENT_CONNECTION_JSON"])
target = sys.argv[1]
expected_tables = [
    'admin_audit_logs', 'alembic_version', 'cards', 'client_actions',
    'email_action_tokens', 'feedback', 'resource_usage', 'review_logs',
    'review_records', 'review_session_items', 'review_sessions', 'users',
    'web_sessions',
]
with psycopg.connect(
    host=connection["host"], port=connection["port"], user=connection["username"],
    password=connection["password"], dbname=target, autocommit=True,
) as db:
    tables = db.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """).fetchall()
    constraints = db.execute("""
        SELECT con.conname
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = rel.relnamespace
        WHERE ns.nspname = 'public'
        ORDER BY con.conname
    """).fetchall()
    indexes = db.execute("""
        SELECT rel.relname
        FROM pg_class rel JOIN pg_namespace ns ON ns.oid = rel.relnamespace
        WHERE ns.nspname = 'public' AND rel.relkind = 'i'
        ORDER BY rel.relname
    """).fetchall()
    table_state = {}
    for table in expected_tables:
        exists = db.execute("SELECT to_regclass(%s)", (f'public.{table}',)).fetchone()[0] is not None
        table_state[table] = {"exists": exists}
        if exists:
            table_state[table]["row_count"] = db.execute(
                f'SELECT count(*) FROM "public"."{table}"'
            ).fetchone()[0]
    trigger = db.execute("""
        SELECT evtname FROM pg_event_trigger
        WHERE evtname = 'restore_fault_injection_midway'
    """).fetchall()
    function = db.execute("""
        SELECT p.oid::regprocedure::text
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public' AND p.proname = 'restore_fault_injection_midway'
    """).fetchall()
print(json.dumps({
    "public_base_tables": [row[0] for row in tables],
    "constraints": [row[0] for row in constraints],
    "indexes": [row[0] for row in indexes],
    "expected_table_state": table_state,
    "event_triggers": [row[0] for row in trigger],
    "fault_functions": [row[0] for row in function],
}, sort_keys=True))
'@
    $afterTargetAudit = Invoke-ExperimentPython -Code $afterTargetAuditCode -ScriptArguments @($TargetDatabase)
    $afterFleet = Invoke-ExperimentPython -Code $databaseAuditCode
    $hashesAfter = @(
        Get-FileHashRecord -Path $DumpPath
        Get-FileHashRecord -Path (Join-Path $BackendRoot ".env")
        Get-FileHashRecord -Path (Join-Path $BackendRoot "scripts\restore-postgresql.ps1")
    )

    [pscustomobject]@{
        experiment = "Level 3.5 Restore fault experiment 4 - midway error"
        target_database = $TargetDatabase
        target_state_before_creation = $targetState
        hashes_before = $hashesBefore
        initial_target_audit = ($initialTargetAudit | ConvertFrom-Json)
        pg_restore_command_core_options = @(
            "--no-owner", "--no-privileges", "--exit-on-error",
            "--single-transaction", "--verbose"
        )
        pg_restore_exit_code = $pgRestoreExitCode
        pg_restore_stdout = $PgRestoreStdoutPath
        pg_restore_stderr = $PgRestoreStderrPath
        target_audit_after_restore = ($afterTargetAudit | ConvertFrom-Json)
        database_fleet_before = ($beforeFleet | ConvertFrom-Json)
        database_fleet_after = ($afterFleet | ConvertFrom-Json)
        hashes_after = $hashesAfter
    } | ConvertTo-Json -Depth 8
}
finally {
    if ($hadPgPassword) {
        $env:PGPASSWORD = $previousPgPassword
    } else {
        Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    }
    if ($hadConnectionJson) {
        [Environment]::SetEnvironmentVariable("RESTORE_FAULT_EXPERIMENT_CONNECTION_JSON", $previousConnectionJson, "Process")
    } else {
        [Environment]::SetEnvironmentVariable("RESTORE_FAULT_EXPERIMENT_CONNECTION_JSON", $null, "Process")
    }
}

$ErrorActionPreference = "Stop"

$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendRoot = Split-Path -Parent $ScriptRoot
$Python = Join-Path $BackendRoot ".venv\Scripts\python.exe"
$TestDatabase = "english_analyzer_phase1_pytest"
$MailLog = Join-Path $BackendRoot "tests\.tmp\postgresql-development-mail.log"

if (-not (Test-Path $Python)) {
  throw "Backend virtual environment not found."
}
if (-not $env:DATABASE_URL) {
  throw "DATABASE_URL must point to the approved PostgreSQL server."
}

$FormalDatabaseUrl = $env:DATABASE_URL
$TestDatabaseUrl = [regex]::Replace(
  $FormalDatabaseUrl,
  "/[^/?]+(?=(?:\?|$))",
  "/$TestDatabase"
)
if ($TestDatabaseUrl -eq $FormalDatabaseUrl) {
  throw "Could not derive the isolated PostgreSQL test database URL."
}

Set-Location $BackendRoot
& $Python scripts\postgresql_test_database.py create `
  --name $TestDatabase `
  --recreate `
  --migrate-head

try {
  $env:POSTGRES_TEST_DATABASE_URL = $TestDatabaseUrl
  $env:DATABASE_URL = $TestDatabaseUrl
  $env:EXPECTED_DATABASE_NAME = $TestDatabase
  $env:APP_ENV = "test"
  $env:ALLOW_SQLITE_FOR_TESTS = "false"
  $env:MAIL_PROVIDER = "development"
  $env:DEVELOPMENT_MAIL_LOG_PATH = $MailLog
  $env:PUBLIC_BASE_URL = "http://testserver"
  & $Python -m pytest -q
  if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL-backed pytest run failed."
  }
}
finally {
  $env:DATABASE_URL = $FormalDatabaseUrl
  Remove-Item Env:POSTGRES_TEST_DATABASE_URL -ErrorAction SilentlyContinue
  & $Python scripts\postgresql_test_database.py drop --name $TestDatabase
}

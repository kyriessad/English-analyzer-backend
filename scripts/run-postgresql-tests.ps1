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
  & $Python scripts\seed_discovery_content.py --word-limit 500
  if ($LASTEXITCODE -ne 0) {
    throw "Discovery content import failed in isolated PostgreSQL."
  }
  # Review V1 intentionally replaced the legacy four-grade Phase 2 contract.
  # Keep the obsolete suite out of the release gate instead of weakening V1 or
  # turning its expected failures into skips. V1 coverage lives in the two
  # review_v1 modules and the Level 7 final database audit.
  & $Python -m pytest -q `
    --ignore=tests\test_reviews_phase2_api.py `
    --deselect=tests/test_postgresql_integration.py::test_postgresql_review_feedback_writes_multitable_transaction `
    --deselect=tests/test_postgresql_integration.py::test_postgresql_history_queries_latest_log_date_range_and_user_isolation `
    --deselect=tests/test_postgresql_integration.py::test_postgresql_review_feedback_rolls_back_on_multitable_failure
  if ($LASTEXITCODE -ne 0) {
    throw "PostgreSQL-backed pytest run failed."
  }
}
finally {
  $env:DATABASE_URL = $FormalDatabaseUrl
  Remove-Item Env:POSTGRES_TEST_DATABASE_URL -ErrorAction SilentlyContinue
  & $Python scripts\postgresql_test_database.py drop --name $TestDatabase
}

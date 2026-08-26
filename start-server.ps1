#Requires -Version 5.1
<#
.SYNOPSIS
  Unified startup for the English Analyzer backend.

.DESCRIPTION
  Checks/start PostgreSQL, Ollama, the FastAPI backend (always via the project
  .venv, without --reload), warms qwen3:8b, confirms in-process Piper warmup,
  and, only when explicitly enabled, checks/starts an ngrok tunnel. Every step polls a real
  health endpoint instead of relying on a fixed sleep. Re-running the script
  reuses any service that is already running correctly instead of starting a
  second copy.

.EXAMPLE
  cd C:\Users\Administrator\WeChatProjects\English-analyzer-backend
  .\start-server.ps1
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- paths
$BackendDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $BackendDir '.venv\Scripts\python.exe'
$LogsDir = Join-Path $BackendDir 'logs'
$StdoutLog = Join-Path $LogsDir 'server.out.log'
$StderrLog = Join-Path $LogsDir 'server.err.log'
$StateDir = Join-Path $BackendDir '.runtime'
$StateFile = Join-Path $StateDir 'server-state.json'

$FastApiPort = 8000
$FastApiHost = '0.0.0.0'
$FastApiHealthUrl = "http://127.0.0.1:$FastApiPort/health"
$OllamaUrl = 'http://127.0.0.1:11434'
$OllamaModel = 'qwen3:8b'
$PgPort = 5432
$NgrokApiUrl = 'http://127.0.0.1:4040/api/tunnels'

$script:StepNumber = 0
$script:StepLabel = ''
$script:NgrokPid = $null
$script:DotEnvCache = $null
$script:LocalHealthLastFailure = ''
$script:PublicHealthLastFailure = ''
$script:StartupStartedAt = Get-Date
$script:StartupDeadline = $script:StartupStartedAt.AddMinutes(15)

# ---------------------------------------------------------------- output
function Write-Step {
  param([string]$Label, [string]$Status)

  $script:StepNumber++
  $script:StepLabel = $Label
  $head = "[{0}/7] {1}" -f $script:StepNumber, $Label
  $dots = '.' * [Math]::Max(0, 26 - $head.Length)
  Write-Host ("{0}{1} {2}" -f $head, $dots, $Status)
}

function Write-Sub {
  param([string]$Text)
  Write-Host ("      {0}" -f $Text)
}

function Fail {
  param([string]$Message)
  Write-Host ''
  Write-Host 'SERVER NOT READY' -ForegroundColor Red
  Write-Host ("Failed step: {0}" -f $script:StepLabel) -ForegroundColor Red
  Write-Host ("Reason: {0}" -f $Message) -ForegroundColor Red
  if ($script:StartupStartedAt) {
    $elapsed = [int]((Get-Date) - $script:StartupStartedAt).TotalSeconds
    Write-Host ("Elapsed: {0}s" -f $elapsed) -ForegroundColor Red
  }
  exit 1
}

# ---------------------------------------------------------------- helpers
function Test-PortListening {
  param([int]$Port)
  $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
  return ($null -ne $conn)
}

function Wait-For {
  param(
    [scriptblock]$Test,
    [string]$What,
    [int]$TimeoutSec = 60
  )
  $remaining = Get-RemainingStartupSeconds
  if ($remaining -le 0) { return $false }
  $deadline = (Get-Date).AddSeconds([Math]::Min($TimeoutSec, $remaining))
  while ((Get-Date) -lt $deadline) {
    try {
      if (& $Test) { return $true }
    } catch { }
    Start-Sleep -Milliseconds 500
  }
  return $false
}

function Get-RemainingStartupSeconds {
  return [Math]::Max(0, [int][Math]::Floor(($script:StartupDeadline - (Get-Date)).TotalSeconds))
}

function Get-StageTimeoutSec {
  param([int]$Preferred)

  $remaining = Get-RemainingStartupSeconds
  if ($remaining -le 0) { return 0 }
  return [Math]::Max(1, [Math]::Min($Preferred, $remaining))
}

function Test-LocalHealth {
  $script:LocalHealthLastFailure = 'no response'
  try {
    $resp = Invoke-WebRequest -Uri $FastApiHealthUrl -TimeoutSec 3 -UseBasicParsing
    if ($resp.StatusCode -eq 200) { return $true }
    $script:LocalHealthLastFailure = "HTTP $($resp.StatusCode)"
    return $false
  } catch {
    $details = Get-HealthResponseDetails -ErrorRecord $_
    if ($details.status) {
      $script:LocalHealthLastFailure = "HTTP $($details.status)" + $(if ($details.body) { "`n$($details.body)" } else { '' })
    } elseif ($details.body) {
      $script:LocalHealthLastFailure = $details.body
    } else {
      $script:LocalHealthLastFailure = $details.message
    }
    return $false
  }
}

function Test-OllamaUp {
  try {
    Invoke-RestMethod -Uri "$OllamaUrl/api/tags" -TimeoutSec 5 | Out-Null
    return $true
  } catch {
    return $false
  }
}

function Test-PostgresHealth {
  param([string]$DatabaseUrl)

  if (-not $DatabaseUrl) { return $false }

  $code = "import os; from sqlalchemy import create_engine, text; e=create_engine(os.environ['DATABASE_URL'], pool_pre_ping=True); c=e.connect(); c.execute(text('select 1')); c.close()"
  $oldDatabaseUrl = $env:DATABASE_URL
  $env:DATABASE_URL = $DatabaseUrl
  try {
    & $PythonExe -c $code 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  } finally {
    if ($null -ne $oldDatabaseUrl) {
      $env:DATABASE_URL = $oldDatabaseUrl
    } else {
      Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
    }
  }
}

function Get-OllamaExe {
  $cmd = Get-Command ollama -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $local = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
  if (Test-Path -LiteralPath $local) { return $local }
  return $null
}

function Get-ListenerProcess {
  param([int]$Port)
  $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $conn) { return $null }
  return Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)" -ErrorAction SilentlyContinue
}

function Get-ProcessById {
  param([int]$Id)
  if (-not $Id) { return $null }
  return Get-CimInstance Win32_Process -Filter "ProcessId=$Id" -ErrorAction SilentlyContinue
}

function Get-ProcessChildren {
  param([int]$ParentId)
  return @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$ParentId" -ErrorAction SilentlyContinue)
}

function Get-ProcessDescendantIds {
  param([int]$RootId)
  $result = New-Object 'System.Collections.Generic.List[int]'
  $visited = New-Object 'System.Collections.Generic.HashSet[int]'
  $queue = New-Object 'System.Collections.Generic.Queue[int]'
  foreach ($child in (Get-ProcessChildren -ParentId $RootId)) {
    $childId = [int]$child.ProcessId
    if ($visited.Add($childId)) { $queue.Enqueue($childId) }
  }
  while ($queue.Count -gt 0) {
    $id = $queue.Dequeue()
    if ($result.Contains($id)) { continue }
    $result.Add($id)
    foreach ($grand in (Get-ProcessChildren -ParentId $id)) {
      $grandId = [int]$grand.ProcessId
      if ($visited.Add($grandId)) { $queue.Enqueue($grandId) }
    }
  }
  return @($result)
}

function Get-CommandLineHash {
  param([string]$CommandLine)

  if (-not $CommandLine) { return '' }
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($CommandLine)
    return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
  } finally {
    $sha.Dispose()
  }
}

function Get-ProcessCreationDateString {
  param($Proc)

  try { return ([datetime]$Proc.CreationDate).ToString('o') } catch { return '' }
}

function Get-ProcessIdentity {
  param($Proc)

  if (-not $Proc) { return $null }
  return [ordered]@{
    pid               = [int]$Proc.ProcessId
    parent_pid        = [int]$Proc.ParentProcessId
    creation_date     = Get-ProcessCreationDateString -Proc $Proc
    executable_path   = [string]$Proc.ExecutablePath
    command_line_hash = Get-CommandLineHash -CommandLine ([string]$Proc.CommandLine)
  }
}

function Test-ProcessIdentityMatches {
  param(
    $Proc,
    $Identity
  )

  if (-not $Proc -or -not $Identity) { return $false }
  $props = $Identity.PSObject.Properties.Name
  if ($props -contains 'pid' -and $Identity.pid -and [int]$Identity.pid -ne [int]$Proc.ProcessId) { return $false }
  if ($props -contains 'creation_date' -and $Identity.creation_date) {
    $liveCreated = Get-ProcessCreationDateString -Proc $Proc
    if ($liveCreated) {
      try {
        $liveCreatedUtc = ([datetime]$Proc.CreationDate).ToUniversalTime()
        $trackedCreatedUtc = ([datetime]$Identity.creation_date).ToUniversalTime()
        if ($liveCreatedUtc.Ticks -ne $trackedCreatedUtc.Ticks) { return $false }
      } catch {
        if ($liveCreated -ne [string]$Identity.creation_date) { return $false }
      }
    }
  }
  if ($props -contains 'executable_path' -and $Identity.executable_path -and
      [string]$Proc.ExecutablePath -ine [string]$Identity.executable_path) { return $false }
  if ($props -contains 'command_line_hash' -and $Identity.command_line_hash) {
    $liveHash = Get-CommandLineHash -CommandLine ([string]$Proc.CommandLine)
    if ($liveHash -ne [string]$Identity.command_line_hash) { return $false }
  }
  return $true
}

function Get-NgrokExe {
  $configured = Get-EffectiveEnvValue -Name 'NGROK_EXE' -Default ''
  if ($configured) {
    if (-not (Test-Path -LiteralPath $configured -PathType Leaf)) { Fail "NGROK_EXE does not exist: $configured" }
    return (Resolve-Path -LiteralPath $configured).Path
  }
  $command = Get-Command ngrok -ErrorAction SilentlyContinue
  if ($command) { return $command.Source }
  return $null
}

function Get-NgrokPublicUrl {
  try {
    $match = @((Invoke-RestMethod -Uri $NgrokApiUrl -TimeoutSec 5).tunnels | Where-Object {
      $_.public_url -match '^https://' -and $_.config.addr -match (":{0}$" -f $FastApiPort)
    } | Select-Object -First 1)
    if ($match.Count -gt 0) { return [string]$match[0].public_url }
  } catch { }
  return $null
}

function Get-DotEnvValues {
  if ($null -ne $script:DotEnvCache) { return $script:DotEnvCache }

  $values = @{}
  $envPath = Join-Path $BackendDir '.env'
  if (Test-Path -LiteralPath $envPath) {
    foreach ($line in Get-Content -LiteralPath $envPath -ErrorAction SilentlyContinue) {
      $trimmed = $line.Trim()
      if (-not $trimmed -or $trimmed.StartsWith('#')) { continue }
      if ($trimmed -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
        $name = $Matches[1]
        $value = $Matches[2].Trim()
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))) {
          $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$name] = $value
      }
    }
  }

  $script:DotEnvCache = $values
  return $values
}

function Get-EffectiveEnvValue {
  param(
    [string]$Name,
    [string]$Default = ''
  )

  $envValue = [System.Environment]::GetEnvironmentVariable($Name, 'Process')
  if ($envValue -and $envValue.Trim()) {
    return $envValue.Trim()
  }

  $dotEnv = Get-DotEnvValues
  if ($dotEnv.ContainsKey($Name) -and [string]$dotEnv[$Name].Trim()) {
    return [string]$dotEnv[$Name]
  }

  return $Default
}

function Get-DotEnvValue {
  param(
    [string]$Name,
    [string]$Default = ''
  )

  $dotEnv = Get-DotEnvValues
  if ($dotEnv.ContainsKey($Name) -and [string]$dotEnv[$Name].Trim()) {
    return [string]$dotEnv[$Name]
  }
  return $Default
}

function Get-EffectiveBoolValue {
  param(
    [string]$Name,
    [bool]$Default = $false
  )

  $raw = Get-EffectiveEnvValue -Name $Name -Default ''
  if (-not $raw) { return $Default }
  return $raw.Trim().ToLowerInvariant() -in @('1', 'true', 'yes', 'on')
}

function Get-EffectiveIntValue {
  param(
    [string]$Name,
    [int]$Default
  )

  $raw = Get-EffectiveEnvValue -Name $Name -Default ''
  if (-not $raw) { return $Default }
  try { return [int]$raw } catch { return $Default }
}

function Get-SettingHash {
  param([object]$Value)

  $json = $Value | ConvertTo-Json -Depth 6 -Compress
  $sha = [System.Security.Cryptography.SHA256]::Create()
  try {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
  } finally {
    $sha.Dispose()
  }
}

function Get-ManagedConfig {
  $configuredHosts = @()
  $allowedHostsRaw = Get-DotEnvValue -Name 'ALLOWED_HOSTS' -Default (Get-EffectiveEnvValue -Name 'ALLOWED_HOSTS' -Default '127.0.0.1,localhost,testserver')
  foreach ($hostName in ($allowedHostsRaw -split ',')) {
    $trimmed = $hostName.Trim()
    if ($trimmed) { $configuredHosts += $trimmed }
  }

  $allowedHosts = @()
  foreach ($hostName in @($configuredHosts + @('127.0.0.1', 'localhost'))) {
    if ($hostName -and ($allowedHosts -notcontains $hostName)) {
      $allowedHosts += $hostName
    }
  }
  if ($script:NgrokEnabled -and $script:NgrokHost -and ($allowedHosts -notcontains $script:NgrokHost)) {
    $allowedHosts += $script:NgrokHost
  }

  $requiredAlembicRevisionOverride = [string][System.Environment]::GetEnvironmentVariable(
    'REQUIRED_ALEMBIC_REVISION',
    'Process'
  )
  $requiredAlembicRevisionOverride = $requiredAlembicRevisionOverride.Trim()

  $fastapi = [ordered]@{
    app_env = (Get-EffectiveEnvValue -Name 'APP_ENV' -Default 'development').ToLowerInvariant()
    log_level = (Get-EffectiveEnvValue -Name 'LOG_LEVEL' -Default 'INFO')
    tracing_enabled = (Get-EffectiveBoolValue -Name 'TRACING_ENABLED' -Default $true)
    database_url_hash = (Get-SettingHash -Value (Get-EffectiveEnvValue -Name 'DATABASE_URL' -Default ''))
    expected_database_dialect = (Get-EffectiveEnvValue -Name 'EXPECTED_DATABASE_DIALECT' -Default 'postgresql').ToLowerInvariant()
    expected_database_name = Get-EffectiveEnvValue -Name 'EXPECTED_DATABASE_NAME' -Default 'english_analyzer'
    expected_database_schema = Get-EffectiveEnvValue -Name 'EXPECTED_DATABASE_SCHEMA' -Default 'public'
    required_alembic_revision_override = $requiredAlembicRevisionOverride
    allow_sqlite_for_tests = (Get-EffectiveBoolValue -Name 'ALLOW_SQLITE_FOR_TESTS' -Default $false)
    allowed_hosts = ($allowedHosts -join ',')
    max_request_body_bytes = (Get-EffectiveIntValue -Name 'MAX_REQUEST_BODY_BYTES' -Default 1048576)
    http_limit_concurrency = (Get-EffectiveIntValue -Name 'HTTP_LIMIT_CONCURRENCY' -Default 30)
    db_pool_size = (Get-EffectiveIntValue -Name 'DB_POOL_SIZE' -Default 5)
    db_max_overflow = (Get-EffectiveIntValue -Name 'DB_MAX_OVERFLOW' -Default 10)
    db_pool_timeout = (Get-EffectiveIntValue -Name 'DB_POOL_TIMEOUT' -Default 3)
    translation_provider = (Get-EffectiveEnvValue -Name 'TRANSLATION_PROVIDER' -Default 'argos').ToLowerInvariant()
    example_generator_provider = (Get-EffectiveEnvValue -Name 'EXAMPLE_GENERATOR_PROVIDER' -Default 'ollama').ToLowerInvariant()
    ollama_base_url = (Get-EffectiveEnvValue -Name 'OLLAMA_BASE_URL' -Default 'http://127.0.0.1:11434').TrimEnd('/')
    ollama_model = Get-EffectiveEnvValue -Name 'OLLAMA_MODEL' -Default 'qwen3:8b'
    ollama_timeout_seconds = (Get-EffectiveIntValue -Name 'OLLAMA_TIMEOUT_SECONDS' -Default 50)
    ollama_temperature = (Get-EffectiveEnvValue -Name 'OLLAMA_TEMPERATURE' -Default '0.3')
    ollama_think = (Get-EffectiveBoolValue -Name 'OLLAMA_THINK' -Default $false)
    ecdict_db_path = Get-EffectiveEnvValue -Name 'ECDICT_DB_PATH' -Default (Join-Path $BackendDir 'data\ecdict\ecdict.db')
    piper_voice = Get-EffectiveEnvValue -Name 'PIPER_VOICE' -Default 'en_US-lessac-medium'
    piper_male_voice = Get-EffectiveEnvValue -Name 'PIPER_MALE_VOICE' -Default 'en_US-hfc_male-medium'
    piper_female_voice = Get-EffectiveEnvValue -Name 'PIPER_FEMALE_VOICE' -Default 'en_US-lessac-medium'
    piper_default_voice = Get-EffectiveEnvValue -Name 'PIPER_DEFAULT_VOICE' -Default 'male'
    piper_data_dir = Get-EffectiveEnvValue -Name 'PIPER_DATA_DIR' -Default (Join-Path $BackendDir 'data\piper')
    piper_audio_cache_dir = Get-EffectiveEnvValue -Name 'PIPER_AUDIO_CACHE_DIR' -Default (Join-Path $BackendDir 'data\audio-cache')
    piper_max_text_chars = (Get-EffectiveIntValue -Name 'PIPER_MAX_TEXT_CHARS' -Default 300)
    piper_cache_max_bytes = (Get-EffectiveIntValue -Name 'PIPER_CACHE_MAX_BYTES' -Default (512 * 1024 * 1024))
    piper_cache_max_age_days = (Get-EffectiveIntValue -Name 'PIPER_CACHE_MAX_AGE_DAYS' -Default 30)
    enable_tencent_tmt = (Get-EffectiveBoolValue -Name 'ENABLE_TENCENT_TMT' -Default $false)
    enable_hunyuan = (Get-EffectiveBoolValue -Name 'ENABLE_HUNYUAN' -Default $false)
    tencent_tmt_region = Get-EffectiveEnvValue -Name 'TENCENT_TMT_REGION' -Default 'ap-guangzhou'
    hunyuan_base_url = (Get-EffectiveEnvValue -Name 'HUNYUAN_BASE_URL' -Default 'https://api.hunyuan.cloud.tencent.com/v1').TrimEnd('/')
    hunyuan_model = Get-EffectiveEnvValue -Name 'HUNYUAN_MODEL' -Default 'hunyuan-role-latest'
    ai_daily_quota = (Get-EffectiveIntValue -Name 'AI_DAILY_QUOTA' -Default 30)
    tts_daily_quota = (Get-EffectiveIntValue -Name 'TTS_DAILY_QUOTA' -Default 100)
    lexical_daily_quota = (Get-EffectiveIntValue -Name 'LEXICAL_DAILY_QUOTA' -Default 500)
    ai_global_concurrency = (Get-EffectiveIntValue -Name 'AI_GLOBAL_CONCURRENCY' -Default 1)
    ai_queue_waiting_capacity = (Get-EffectiveIntValue -Name 'AI_QUEUE_WAITING_CAPACITY' -Default 2)
    ai_inflight_follower_capacity = (Get-EffectiveIntValue -Name 'AI_INFLIGHT_FOLLOWER_CAPACITY' -Default 3)
    tts_global_concurrency = (Get-EffectiveIntValue -Name 'TTS_GLOBAL_CONCURRENCY' -Default 1)
    tts_queue_waiting_capacity = (Get-EffectiveIntValue -Name 'TTS_QUEUE_WAITING_CAPACITY' -Default 2)
    resource_queue_timeout_seconds = (Get-EffectiveIntValue -Name 'RESOURCE_QUEUE_TIMEOUT_SECONDS' -Default 3)
    ai_queue_timeout_seconds = (Get-EffectiveIntValue -Name 'AI_QUEUE_TIMEOUT_SECONDS' -Default 30)
    ai_total_timeout_seconds = (Get-EffectiveIntValue -Name 'AI_TOTAL_TIMEOUT_SECONDS' -Default 90)
  }

  return [ordered]@{
    fastapi = $fastapi
    fastapi_hash = Get-SettingHash -Value $fastapi
    ngrok = [ordered]@{
      enabled = $script:NgrokEnabled
      base = $script:NgrokBase
      host = $script:NgrokHost
      port = $FastApiPort
    }
    ngrok_hash = Get-SettingHash -Value ([ordered]@{
      enabled = $script:NgrokEnabled
      base = $script:NgrokBase
      host = $script:NgrokHost
      port = $FastApiPort
      allowed_hosts = ($allowedHosts -join ',')
    })
    allowed_hosts = $allowedHosts
  }
}

function Get-HealthResponseDetails {
  param([Parameter(Mandatory)]$ErrorRecord)

  $status = $null
  $body = ''
  try { $status = [int]$ErrorRecord.Exception.Response.StatusCode.value__ } catch { }
  try {
    $stream = $ErrorRecord.Exception.Response.GetResponseStream()
    if ($stream) {
      $reader = New-Object System.IO.StreamReader($stream)
      try { $body = $reader.ReadToEnd() } finally { $reader.Dispose(); $stream.Dispose() }
    }
  } catch { }
  if (-not $body) {
    try { $body = $ErrorRecord.ErrorDetails.Message } catch { }
  }

  return [ordered]@{
    status = $status
    body = ($body | Out-String).Trim()
    message = $ErrorRecord.Exception.Message
  }
}

function Stop-OneProcess {
  param([int]$Id)

  $proc = Get-Process -Id $Id -ErrorAction SilentlyContinue
  if (-not $proc) { return $true }
  try { Stop-Process -Id $Id -ErrorAction Stop } catch { }

  $deadline = (Get-Date).AddSeconds(5)
  while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process -Id $Id -ErrorAction SilentlyContinue)) { return $true }
    Start-Sleep -Milliseconds 300
  }

  $still = Get-Process -Id $Id -ErrorAction SilentlyContinue
  if ($still) {
    try { Stop-Process -Id $Id -Force -ErrorAction Stop } catch { return $false }
    if (Get-Process -Id $Id -ErrorAction SilentlyContinue) { return $false }
  }

  return $true
}

function Normalize-ProcessEnvironment {
  $vars = [System.Environment]::GetEnvironmentVariables()
  $merged = New-Object 'System.Collections.Generic.Dictionary[string,string]' ([System.StringComparer]::OrdinalIgnoreCase)
  foreach ($entry in $vars.GetEnumerator()) {
    $merged[[string]$entry.Key] = [string]$entry.Value
  }

  foreach ($key in @($vars.Keys)) {
    [System.Environment]::SetEnvironmentVariable([string]$key, $null, 'Process')
  }

  foreach ($entry in $merged.GetEnumerator()) {
    [System.Environment]::SetEnvironmentVariable($entry.Key, $entry.Value, 'Process')
  }
}

# ---------------------------------------------------------------- preflight
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
  Fail "Project virtual environment not found: $PythonExe"
}

$effectiveDatabaseUrl = Get-EffectiveEnvValue -Name 'DATABASE_URL' -Default ''
if (-not $effectiveDatabaseUrl) {
  Fail "DATABASE_URL is not set in the environment or .env. Set it as a User environment variable (password must not be committed). See .env.example."
}

$script:NgrokEnabled = Get-EffectiveBoolValue -Name 'NGROK_ENABLED' -Default $false
$script:NgrokBase = (Get-EffectiveEnvValue -Name 'NGROK_DOMAIN' -Default '').TrimEnd('/')
$script:NgrokHost = $null
if ($script:NgrokBase) {
  try { $script:NgrokHost = ([System.Uri]$script:NgrokBase).Host } catch { Fail 'NGROK_DOMAIN must be an absolute URL.' }
  if (-not $script:NgrokHost) { Fail 'NGROK_DOMAIN must contain a host.' }
}

$runtimeConfig = Get-ManagedConfig
$allowedHosts = @($runtimeConfig.allowed_hosts)

$configCheck = @(& $PythonExe (Join-Path $BackendDir 'scripts\check_config.py') 2>&1)
if ($LASTEXITCODE -ne 0) {
  Fail "Application configuration preflight failed.`n$($configCheck -join "`n")"
}
foreach ($line in $configCheck) { Write-Sub -Text ([string]$line) }
$script:OllamaBaseUrl = (Get-EffectiveEnvValue -Name 'OLLAMA_BASE_URL' -Default 'http://127.0.0.1:11434').TrimEnd('/')
$script:OllamaModel = Get-EffectiveEnvValue -Name 'OLLAMA_MODEL' -Default 'qwen3:8b'
$OllamaUrl = $script:OllamaBaseUrl
$OllamaModel = $script:OllamaModel

# ALLOWED_HOSTS must include the ngrok host or TrustedHostMiddleware rejects
# public requests. This env var is set before uvicorn launches.
$env:ALLOWED_HOSTS = ($allowedHosts -join ',')
Normalize-ProcessEnvironment

# ================================================================ 1. PostgreSQL
$pgHealth = Test-PostgresHealth -DatabaseUrl $effectiveDatabaseUrl
if (-not $pgHealth) {
  $pgSvc = Get-Service -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like 'postgresql-*' } | Select-Object -First 1
  if ($pgSvc) {
    if ($pgSvc.Status -ne 'Running') {
      try { Start-Service -Name $pgSvc.Name -ErrorAction Stop } catch { }
    }
    if (-not (Wait-For { Test-PostgresHealth -DatabaseUrl $effectiveDatabaseUrl } "PostgreSQL connection" 60)) {
      Fail "PostgreSQL service '$($pgSvc.Name)' did not become healthy within 60s."
    }
    $pgHealth = $true
  }
}
if (-not $pgHealth) {
  Fail "PostgreSQL health check failed: could not connect with DATABASE_URL."
}
Write-Step -Label 'PostgreSQL' -Status 'OK'

$databaseTargetOutput = @(& $PythonExe (Join-Path $BackendDir 'scripts\check_database_target.py') 2>&1)
$databaseTargetExitCode = $LASTEXITCODE
foreach ($line in $databaseTargetOutput) {
  Write-Sub -Text ([string]$line)
}
if ($databaseTargetExitCode -ne 0) {
  Fail "Database target or Alembic revision preflight failed. FastAPI was not started."
}
Write-Step -Label 'Database target' -Status 'OK'

# ================================================================ 2. Ollama
$ollamaUp = Test-OllamaUp
if (-not $ollamaUp) {
  $ollamaExe = Get-OllamaExe
  if (-not $ollamaExe) { Fail "Ollama executable not found and http://127.0.0.1:11434 is not responding." }
  Start-Process -FilePath $ollamaExe -ArgumentList 'serve' -WindowStyle Hidden
  if (-not (Wait-For { Test-OllamaUp } 'Ollama /api/tags' 60)) {
    Fail "Ollama did not become healthy within 60s."
  }
  $ollamaUp = $true
}
Write-Step -Label 'Ollama' -Status 'OK'

# ================================================================ 3. qwen3:8b confirm + warm
try {
  $tagsTimeout = Get-StageTimeoutSec -Preferred 10
  if ($tagsTimeout -le 0) { Fail 'Startup deadline exceeded before Ollama tag check.' }
  $tags = Invoke-RestMethod -Uri "$OllamaUrl/api/tags" -TimeoutSec $tagsTimeout
} catch {
  Fail "Could not list Ollama models."
}
$hasModel = $false
foreach ($m in $tags.models) {
  if ($m.name -eq $OllamaModel -or $m.model -eq $OllamaModel) { $hasModel = $true }
}
if (-not $hasModel) {
  Fail "qwen3:8b 未安装 (not installed). Run: ollama pull qwen3:8b"
}

# Warm up: a minimal generate loads the model into GPU/RAM and keep_alive=30m
# keeps it resident, matching the app's own keep_alive for qwen3:8b.
try {
  $warmTimeout = Get-StageTimeoutSec -Preferred 180
  if ($warmTimeout -le 0) { Fail 'Startup deadline exceeded before qwen3:8b warmup.' }
  $warmBody = @{
    model = $OllamaModel
    prompt = "Hi"
    keep_alive = "30m"
    stream = $false
  } | ConvertTo-Json
  Invoke-RestMethod -Method Post -Uri "$OllamaUrl/api/generate" -Body $warmBody -ContentType 'application/json' -TimeoutSec $warmTimeout | Out-Null
} catch {
  Fail "qwen3:8b warmup request failed: $($_.Exception.Message)"
}

$processor = 'not loaded'
$ollamaExeForPs = Get-OllamaExe
if ($ollamaExeForPs) {
  try {
    $psText = (& $ollamaExeForPs ps) -join "`n"
    $modelLine = @($psText -split "`r?`n" | Where-Object { $_ -match [regex]::Escape($OllamaModel) } | Select-Object -First 1)
    if ($modelLine.Count -gt 0) {
      if ($modelLine[0] -match '(\d+%\s+GPU|GPU)') { $processor = $Matches[1] } else { $processor = 'CPU' }
    }
  } catch { }
}
if ($processor -notmatch 'GPU') {
  Fail "qwen3:8b warmup completed, but ollama ps did not show the model loaded on GPU. Processor: $processor"
}
Write-Step -Label 'qwen3:8b' -Status 'WARM'
Write-Sub -Text ("Processor: {0}" -f $processor)

# ================================================================ 4. FastAPI
$script:StepLabel = 'FastAPI'
$state = $null
if (Test-Path -LiteralPath $StateFile) {
  try {
    $state = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json
  } catch {
    $state = $null
  }
}

$stateFastApiPid = $null
$stateFastApiLauncherPid = $null
$stateFastApiListenerPid = $null
$stateFastApiListenerIdentity = $null
$stateFastApiHash = $null
$stateNgrokPid = $null
$stateNgrokHash = $null
if ($state) {
  $stateProps = $state.PSObject.Properties.Name
  if ($stateProps -contains 'fastapi_launcher_pid' -and $state.fastapi_launcher_pid) { $stateFastApiLauncherPid = [int]$state.fastapi_launcher_pid }
  if ($stateProps -contains 'fastapi_listener_pid' -and $state.fastapi_listener_pid) { $stateFastApiListenerPid = [int]$state.fastapi_listener_pid }
  if ($stateProps -contains 'fastapi_listener_identity') { $stateFastApiListenerIdentity = $state.fastapi_listener_identity }
  if ($stateProps -contains 'fastapi_pid' -and $state.fastapi_pid) { $stateFastApiPid = [int]$state.fastapi_pid }
  if ($stateProps -contains 'fastapi_hash' -and $state.fastapi_hash) { $stateFastApiHash = [string]$state.fastapi_hash }
  if ($stateProps -contains 'ngrok_pid' -and $state.ngrok_pid) { $stateNgrokPid = [int]$state.ngrok_pid }
  if ($stateProps -contains 'ngrok_hash' -and $state.ngrok_hash) { $stateNgrokHash = [string]$state.ngrok_hash }
}

$desiredFastapiHash = $runtimeConfig.fastapi_hash
$desiredNgrokHash = $runtimeConfig.ngrok_hash

function Test-ManagedUvicorn {
  param($Proc)
  if (-not $Proc -or -not $Proc.CommandLine) { return $false }
  return ($Proc.CommandLine -match 'uvicorn' -and
          $Proc.CommandLine -match 'app\.main:app' -and
          $Proc.CommandLine -match ('\b{0}\b' -f $FastApiPort) -and
          $Proc.CommandLine -notmatch '--reload')
}

function Test-IsAncestorProcess {
  param(
    [int]$AncestorId,
    [int]$ProcessId
  )
  if ($AncestorId -le 0 -or $ProcessId -le 0) { return $false }
  $visited = New-Object 'System.Collections.Generic.HashSet[int]'
  $cursor = Get-ProcessById -Id $ProcessId
  while ($cursor) {
    $cursorId = [int]$cursor.ProcessId
    if (-not $visited.Add($cursorId)) { return $false }
    if ($cursorId -eq $AncestorId) { return $true }
    $parentId = $null
    try { $parentId = [int]$cursor.ParentProcessId } catch { }
    if (-not $parentId -or $parentId -le 0) { return $false }
    $cursor = Get-ProcessById -Id $parentId
  }
  return $false
}

function Stop-FastApiProcessTree {
  param(
    [int]$LauncherPid,
    [int]$ListenerPid
  )
  $ids = New-Object 'System.Collections.Generic.List[int]'
  if ($ListenerPid -and -not $ids.Contains($ListenerPid)) { $ids.Add($ListenerPid) }
  if ($LauncherPid -and -not $ids.Contains($LauncherPid)) { $ids.Add($LauncherPid) }
  if ($LauncherPid) {
    foreach ($descId in (Get-ProcessDescendantIds -RootId $LauncherPid)) {
      $dp = Get-ProcessById -Id $descId
      if ($dp -and (Test-ManagedUvicorn -Proc $dp) -and -not $ids.Contains($descId)) { $ids.Add($descId) }
    }
  }
  foreach ($id in $ids) {
    if (-not (Stop-OneProcess -Id $id)) { return $false }
  }

  $deadline = (Get-Date).AddSeconds(10)
  while ((Get-Date) -lt $deadline) {
    if (-not (Test-PortListening -Port $FastApiPort)) { return $true }
    Start-Sleep -Milliseconds 300
  }
  return (-not (Test-PortListening -Port $FastApiPort))
}

$trackedLauncherPid = $stateFastApiLauncherPid
$trackedListenerPid = $stateFastApiListenerPid
if ($stateFastApiPid) {
  # Legacy state only had fastapi_pid. Older versions wrote the port listener
  # after resolving it, but tolerate a launcher PID if such a file exists.
  $legacyProc = Get-ProcessById -Id $stateFastApiPid
  if (-not $trackedListenerPid -and $legacyProc -and (Test-ManagedUvicorn -Proc $legacyProc)) {
    $trackedListenerPid = $stateFastApiPid
  } elseif (-not $trackedLauncherPid) {
    $trackedLauncherPid = $stateFastApiPid
  }
}

$fastapiProc = $null
$fastapiReused = $false
$fastapiPid = $null
$fastapiLauncherPid = $trackedLauncherPid

$listener = Get-ListenerProcess -Port $FastApiPort

if ($listener) {
  $listenerIsManaged = Test-ManagedUvicorn -Proc $listener
  if (-not $listenerIsManaged) {
    Fail "Port $FastApiPort is occupied by an unrecognized process (PID $($listener.ProcessId), CommandLine: $($listener.CommandLine))."
  }

  # Primary: the recorded listener PID is still the process LISTENING on the
  # port. Secondary: the live listener belongs to the recorded launcher's
  # process tree (covers legacy states that only stored the launcher PID).
  $matchesTrackedListener = ($trackedListenerPid -and $listener.ProcessId -eq $trackedListenerPid)
  $belongsToTrackedLauncher = ($trackedLauncherPid -and
    (Test-IsAncestorProcess -AncestorId $trackedLauncherPid -ProcessId $listener.ProcessId))

  if (-not ($matchesTrackedListener -or $belongsToTrackedLauncher)) {
    Fail "Port $FastApiPort is occupied by a uvicorn process that is not tracked by the current state file (PID $($listener.ProcessId))."
  }

  # FastAPI always restarts so Python source changes are loaded. Keep the
  # existing tracked-process and listener identity checks before stopping only
  # this project's uvicorn tree.
  if ($stateFastApiListenerIdentity -and
      -not (Test-ProcessIdentityMatches -Proc $listener -Identity $stateFastApiListenerIdentity)) {
    Fail "Tracked FastAPI listener PID $($listener.ProcessId) failed identity validation before restart; leaving it running."
  }
  Write-Sub -Text ("Restarting managed FastAPI listener PID {0}" -f $listener.ProcessId)
  if (-not (Stop-FastApiProcessTree -LauncherPid $trackedLauncherPid -ListenerPid $listener.ProcessId)) {
    Fail "Managed FastAPI process tree (launcher $trackedLauncherPid, listener $($listener.ProcessId)) could not be stopped before restart."
  }
  $fastapiProc = $null
  $fastapiPid = $null
  $listener = $null
}

if (-not $fastapiReused) {
  if (-not $listener) {
    # Recorded launcher alive but nothing listening: stop the stale managed
    # launcher before the new instance binds the free port.
    if ($trackedLauncherPid) {
      $launcherProc = Get-ProcessById -Id $trackedLauncherPid
      if ($launcherProc -and (Test-ManagedUvicorn -Proc $launcherProc)) {
        if (-not (Stop-FastApiProcessTree -LauncherPid $trackedLauncherPid -ListenerPid 0)) {
          Fail "Managed FastAPI process tree (launcher $trackedLauncherPid) could not be stopped before restart."
        }
      }
    }

    New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null
    $uvicornArgs = @('-m', 'uvicorn', 'app.main:app', '--host', $FastApiHost, '--port', [string]$FastApiPort, '--limit-concurrency', [string]$runtimeConfig.fastapi.http_limit_concurrency)
    $proc = Start-Process `
      -FilePath $PythonExe `
      -ArgumentList $uvicornArgs `
      -WorkingDirectory $BackendDir `
      -RedirectStandardOutput $StdoutLog `
      -RedirectStandardError $StderrLog `
      -WindowStyle Hidden `
      -PassThru
    $startedLauncherPid = $proc.Id

    if (-not (Wait-For { Test-LocalHealth } "FastAPI $FastApiHealthUrl" 90)) {
      Write-Sub -Text 'FastAPI local: FAILED'
      Write-Sub -Text $script:LocalHealthLastFailure
      $errTail = ''
      if (Test-Path -LiteralPath $StderrLog) {
        $errTail = (Get-Content -LiteralPath $StderrLog -Tail 20) -join "`n"
      }
      Fail "FastAPI did not become healthy within 90s.`n$errTail"
    }

    # The venv python.exe is usually a launcher that spawns the base
    # interpreter, so the Start-Process PID is NOT the process that binds the
    # port. Resolve the real LISTENING process and confirm it belongs to the
    # launched process tree before trusting it.
    $resolvedListener = $null
    $resolveDeadline = (Get-Date).AddSeconds(10)
    while (-not $resolvedListener -and (Get-Date) -lt $resolveDeadline) {
      $candidate = Get-ListenerProcess -Port $FastApiPort
      if ($candidate -and (Test-ManagedUvicorn -Proc $candidate) -and
          (Test-IsAncestorProcess -AncestorId $startedLauncherPid -ProcessId $candidate.ProcessId)) {
        $resolvedListener = $candidate
      }
      if (-not $resolvedListener) { Start-Sleep -Milliseconds 500 }
    }

    if (-not $resolvedListener) {
      $candidate = Get-ListenerProcess -Port $FastApiPort
      $detail = if ($candidate) {
        "found listener PID $($candidate.ProcessId) (CommandLine: $($candidate.CommandLine))"
      } else {
        'no listener found on the port'
      }
      Fail "Could not confirm the launched FastAPI process tree: launcher PID $startedLauncherPid, $detail."
    }

    $fastapiProc = $resolvedListener
    $fastapiPid = $resolvedListener.ProcessId
    $fastapiLauncherPid = $startedLauncherPid
  }
}

if (-not (Test-LocalHealth)) {
  Write-Sub -Text 'FastAPI local: FAILED'
  Write-Sub -Text $script:LocalHealthLastFailure
  Fail "FastAPI is listening on port $FastApiPort but $FastApiHealthUrl is not healthy."
}
Write-Sub -Text 'FastAPI local: READY'

if (-not $fastapiProc) {
  $fastapiProc = Get-ListenerProcess -Port $FastApiPort
}
if ($fastapiProc) { $fastapiPid = $fastapiProc.ProcessId }

Write-Step -Label 'FastAPI' -Status ('OK' + $(if ($fastapiReused) { ' (reused)' } else { '' }))
Write-Sub -Text ("Python: {0}" -f $PythonExe)
Write-Sub -Text ("Port: {0}" -f $FastApiPort)
Write-Sub -Text ("Launcher PID: {0}" -f $fastapiLauncherPid)
Write-Sub -Text ("Listener PID: {0}" -f $fastapiPid)
Write-Sub -Text ("Config hash: {0}" -f $desiredFastapiHash)
if ($fastapiProc -and $fastapiProc.ExecutablePath -ine $PythonExe) {
  Write-Sub -Text ("Base interpreter: {0}" -f $fastapiProc.ExecutablePath)
}

# ================================================================ 5/6. Piper warmup (from FastAPI startup log)
function Get-PiperStatus {
  param([string]$Voice)
  $ok = $false
  $failed = $null
  $text = ''
  foreach ($logPath in @($StdoutLog, $StderrLog)) {
    if (Test-Path -LiteralPath $logPath) {
      $text += "`n" + (Get-Content -LiteralPath $logPath -Raw -ErrorAction SilentlyContinue)
    }
  }

  # The startup logger now wraps the message in JSON. Parse the JSON lines
  # first, while retaining the old plain-text match for older log entries.
  foreach ($line in ($text -split "`r?`n")) {
    if (-not $line.Trim()) { continue }
    try {
      $entry = $line | ConvertFrom-Json
      if ($entry.message -eq "[STARTUP] Piper $Voice warmup OK") { $ok = $true }
      elseif ($entry.message -match ("^\[STARTUP\] Piper {0} warmup FAILED: (.+)$" -f [regex]::Escape($Voice))) {
        $failed = $Matches[1].Trim()
      }
    } catch { }
  }
  if (-not $ok -and $text -match ("\[STARTUP\] Piper {0} warmup OK" -f [regex]::Escape($Voice))) {
    $ok = $true
  }
  if (-not $ok -and -not $failed -and
      $text -match ("\[STARTUP\] Piper {0} warmup FAILED: (.+)" -f [regex]::Escape($Voice))) {
    $failed = $Matches[1].Trim()
  }

  # Prefer the structured piper_load_voice success record. The voice name is
  # present in the OpenTelemetry attributes block emitted for that operation.
  $voiceName = if ($Voice -eq 'female') { $env:PIPER_FEMALE_VOICE } else { $env:PIPER_MALE_VOICE }
  if (-not $voiceName) {
    $voiceName = if ($Voice -eq 'female') { 'en_US-lessac-medium' } else { 'en_US-hfc_male-medium' }
  }
  $voicePattern = [regex]::Escape($voiceName)
  $structuredSuccess = '(?s)"operation"\s*:\s*"piper_load_voice".{0,1600}"voice"\s*:\s*"' +
    $voicePattern + '".{0,1600}"result"\s*:\s*"success"'
  if ($text -match $structuredSuccess) { $ok = $true }

  if ($fastapiReused -and -not $ok -and -not $failed) { return @{ Status = 'WARM'; Reason = 'reused' } }
  if ($ok) { return @{ Status = 'WARM'; Reason = '' } }
  if ($failed) { return @{ Status = 'FAILED'; Reason = $failed } }
  return @{ Status = 'UNKNOWN'; Reason = 'no warmup log found' }
}

$piperFemale = Get-PiperStatus -Voice 'female'
$piperMale = Get-PiperStatus -Voice 'male'

if ($piperFemale.Status -eq 'WARM') {
  Write-Step -Label 'Piper female' -Status 'WARM'
} else {
  Write-Step -Label 'Piper female' -Status ('FAILED (' + $piperFemale.Reason + ')')
}
if ($piperMale.Status -eq 'WARM') {
  Write-Step -Label 'Piper male' -Status 'WARM'
} else {
  Write-Step -Label 'Piper male' -Status ('FAILED (' + $piperMale.Reason + ')')
}
$piperDegraded = ($piperFemale.Status -ne 'WARM') -or ($piperMale.Status -ne 'WARM')

# ================================================================ 7. ngrok (optional)
if (-not $script:NgrokEnabled) {
  Write-Step -Label 'ngrok' -Status 'DISABLED'
  Write-Sub -Text 'FastAPI is ready for local development without ngrok.'
} else {
  $script:StepLabel = 'ngrok / public health'
  $ngrokExe = Get-NgrokExe
  if (-not $ngrokExe) { Fail 'ngrok is enabled but NGROK_EXE is not set and ngrok is not on PATH.' }
  $arguments = @('http', [string]$FastApiPort)
  if ($script:NgrokBase) { $arguments += @('--url', $script:NgrokBase) }
  $ngrokProc = Start-Process -FilePath $ngrokExe -ArgumentList $arguments -WindowStyle Hidden -PassThru
  $script:NgrokPid = $ngrokProc.Id
  $deadline = (Get-Date).AddSeconds([Math]::Min(45, [Math]::Max(5, (Get-RemainingStartupSeconds))))
  do {
    Start-Sleep -Seconds 1
    $publicUrl = Get-NgrokPublicUrl
  } while (-not $publicUrl -and (Get-Date) -lt $deadline)
  if (-not $publicUrl) { Fail 'ngrok started but did not publish an HTTPS URL within 45 seconds.' }
  $script:NgrokBase = $publicUrl.TrimEnd('/')
  $script:NgrokHost = ([System.Uri]$script:NgrokBase).Host
  try {
    $response = Invoke-WebRequest -Uri "$($script:NgrokBase)/health" -Headers @{ 'ngrok-skip-browser-warning' = '1' } -TimeoutSec 10 -UseBasicParsing
    if ($response.StatusCode -ne 200 -or (($response.Content | ConvertFrom-Json).status -ne 'ok')) { throw 'unexpected health response' }
  } catch { Fail "ngrok public health check failed: $($_.Exception.Message)" }
  Write-Step -Label 'ngrok' -Status 'READY'
  Write-Sub -Text ("Public URL: {0}" -f $script:NgrokBase)
}

# ---------------------------------------------------------------- runtime state
# Record the live PIDs for stop-server.ps1. No secrets are stored here.
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
$stateObject = [ordered]@{
  managed_version        = 3
  fastapi_launcher_pid   = $fastapiLauncherPid
  fastapi_listener_pid   = $fastapiPid
  fastapi_pid            = $fastapiPid
  fastapi_launcher_identity = Get-ProcessIdentity -Proc (Get-ProcessById -Id $fastapiLauncherPid)
  fastapi_listener_identity = Get-ProcessIdentity -Proc (Get-ProcessById -Id $fastapiPid)
  ngrok_pid              = $script:NgrokPid
  started_at             = (Get-Date).ToString('o')
  port                   = $FastApiPort
  ngrok_domain           = $script:NgrokBase
  fastapi_hash           = $desiredFastapiHash
  ngrok_hash             = $desiredNgrokHash
  allowed_hosts          = ($allowedHosts -join ',')
}
$stateObject | ConvertTo-Json | Set-Content -LiteralPath $StateFile -Encoding UTF8

# ---------------------------------------------------------------- summary
Write-Host ''
if ($piperDegraded) {
  Write-Host 'SERVER READY (Piper degraded - TTS may be unavailable)' -ForegroundColor Yellow
} else {
  Write-Host 'SERVER READY' -ForegroundColor Green
}

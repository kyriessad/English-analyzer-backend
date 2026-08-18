#Requires -Version 5.1
<#
.SYNOPSIS
  Unified startup for the English Analyzer backend.

.DESCRIPTION
  Checks/start PostgreSQL, Ollama, the FastAPI backend (always via the project
  .venv, without --reload), warms qwen3:8b, confirms in-process Piper warmup,
  and checks/starts the fixed-domain ngrok tunnel. Every step polls a real
  health endpoint instead of relying on a fixed sleep. Re-running the script
  reuses any service that is already running correctly instead of starting a
  second copy.

.EXAMPLE
  cd C:\Users\Administrator\WeChatProjects\English-analyzer-backend
  .\start-server.ps1
#>
[CmdletBinding()]
param(
  # Fixed ngrok domain (scheme + host, no trailing slash). When empty, the
  # value is read from English-study-miniapp's localBackendConfig.js, then
  # falls back to the current fixed domain.
  [string]$NgrokDomain = ""
)

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
$MiniappConfigPath = Join-Path (Split-Path -Parent $BackendDir) 'English-study-miniapp\utils\localBackendConfig.js'

$FastApiPort = 8000
$FastApiHost = '0.0.0.0'
$FastApiHealthUrl = "http://127.0.0.1:$FastApiPort/health"
$OllamaUrl = 'http://127.0.0.1:11434'
$OllamaModel = 'qwen3:8b'
$PgPort = 5432
$FallbackNgrokDomain = 'https://detergent-starry-oboe.ngrok-free.dev'

$script:StepNumber = 0
$script:StepLabel = ''
$script:NgrokPid = $null

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
  $deadline = (Get-Date).AddSeconds($TimeoutSec)
  while ((Get-Date) -lt $deadline) {
    try {
      if (& $Test) { return $true }
    } catch { }
    Start-Sleep -Milliseconds 500
  }
  return $false
}

function Test-LocalHealth {
  try {
    $resp = Invoke-WebRequest -Uri $FastApiHealthUrl -TimeoutSec 3 -UseBasicParsing
    return ($resp.StatusCode -eq 200)
  } catch {
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

function Get-NgrokDomainFromConfig {
  if (-not (Test-Path -LiteralPath $MiniappConfigPath)) { return $null }
  $content = Get-Content -LiteralPath $MiniappConfigPath -Raw
  if ($content -match "NGROK_BACKEND_BASE_URL\s*=\s*['`"]([^'`"]+)['`"]") {
    return $Matches[1]
  }
  return $null
}

function Get-ProjectNgrokProcess {
  # Returns the ngrok process serving THIS project's fixed domain on THIS
  # port. Never used to identify other tunnels, so a future second ngrok is
  # left alone.
  $procs = Get-CimInstance Win32_Process -Filter "Name='ngrok.exe'" -ErrorAction SilentlyContinue
  foreach ($p in $procs) {
    if ($p.CommandLine -and
        $p.CommandLine -match [regex]::Escape($script:NgrokHost) -and
        $p.CommandLine -match ('\b{0}\b' -f $FastApiPort)) {
      return $p
    }
  }
  return $null
}

# ---------------------------------------------------------------- preflight
if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
  Fail "Project virtual environment not found: $PythonExe"
}

if (-not $env:DATABASE_URL) {
  Fail "DATABASE_URL is not set in the environment. Set it as a User environment variable (password must not be committed). See .env.example."
}

$script:NgrokBase = $NgrokDomain
if (-not $script:NgrokBase) { $script:NgrokBase = Get-NgrokDomainFromConfig }
if (-not $script:NgrokBase) { $script:NgrokBase = $FallbackNgrokDomain }
$script:NgrokBase = $script:NgrokBase.TrimEnd('/')
$script:NgrokHost = $null
try { $script:NgrokHost = ([System.Uri]$script:NgrokBase).Host } catch { }

# ALLOWED_HOSTS must include the ngrok host or TrustedHostMiddleware rejects
# public requests. This env var is set before uvicorn launches.
$allowedHosts = @('127.0.0.1', 'localhost')
if ($script:NgrokHost) { $allowedHosts += $script:NgrokHost }
$env:ALLOWED_HOSTS = ($allowedHosts -join ',')

# ================================================================ 1. PostgreSQL
$pgListening = Test-PortListening -Port $PgPort
if (-not $pgListening) {
  $pgSvc = Get-Service -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like 'postgresql-*' } | Select-Object -First 1
  if ($pgSvc) {
    if ($pgSvc.Status -ne 'Running') {
      try { Start-Service -Name $pgSvc.Name -ErrorAction Stop } catch { }
    }
    if (-not (Wait-For { Test-PortListening -Port $PgPort } "PostgreSQL port $PgPort" 30)) {
      Fail "PostgreSQL service '$($pgSvc.Name)' did not start listening on port $PgPort within 30s."
    }
    $pgListening = $true
  }
}
if (-not $pgListening) {
  Fail "PostgreSQL is not listening on port $PgPort and no postgresql-* Windows service was found."
}
Write-Step -Label 'PostgreSQL' -Status 'OK'

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
  $tags = Invoke-RestMethod -Uri "$OllamaUrl/api/tags" -TimeoutSec 10
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
  $warmBody = @{
    model = $OllamaModel
    prompt = "Hi"
    keep_alive = "30m"
    stream = $false
  } | ConvertTo-Json
  Invoke-RestMethod -Method Post -Uri "$OllamaUrl/api/generate" -Body $warmBody -ContentType 'application/json' -TimeoutSec 180 | Out-Null
} catch {
  Fail "qwen3:8b warmup request failed: $($_.Exception.Message)"
}

$processor = 'CPU'
$ollamaExeForPs = Get-OllamaExe
if ($ollamaExeForPs) {
  try {
    $psText = (& $ollamaExeForPs ps) -join "`n"
    if ($psText -match 'qwen3:8b') {
      if ($psText -match 'GPU') { $processor = 'GPU' } else { $processor = 'CPU' }
    }
  } catch { }
}
Write-Step -Label 'qwen3:8b' -Status 'WARM'
Write-Sub -Text ("Processor: {0}" -f $processor)

# ================================================================ 4. FastAPI
$existing = Get-ListenerProcess -Port $FastApiPort
$fastapiReused = $false
$fastapiPid = $null

if ($existing) {
  # The project .venv is built on Anaconda (pyvenv.cfg home = anaconda3), so the
  # port-8000 listener shows up as anaconda python even when launched via the
  # venv. Detect correctness by command line, not interpreter path.
  $isOurCommand = ($existing.CommandLine -match 'uvicorn' -and $existing.CommandLine -match 'app\.main:app')
  $usesReload = ($existing.CommandLine -match '--reload')

  if ($isOurCommand -and -not $usesReload) {
    $fastapiReused = $true
    $fastapiPid = $existing.ProcessId
  } else {
    $why = ''
    if (-not $isOurCommand) { $why = "unrecognized command: $($existing.CommandLine)" }
    else { $why = "running with --reload" }
    Fail "Port $FastApiPort is occupied by an unexpected uvicorn (PID $($existing.ProcessId), $why). Stop it first, then re-run."
  }
}

if (-not $fastapiReused) {
  New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null
  $uvicornArgs = @('-m', 'uvicorn', 'app.main:app', '--host', $FastApiHost, '--port', [string]$FastApiPort)
  $proc = Start-Process `
    -FilePath $PythonExe `
    -ArgumentList $uvicornArgs `
    -WorkingDirectory $BackendDir `
    -RedirectStandardOutput $StdoutLog `
    -RedirectStandardError $StderrLog `
    -WindowStyle Hidden `
    -PassThru
  $fastapiPid = $proc.Id

  if (-not (Wait-For { Test-LocalHealth } "FastAPI $FastApiHealthUrl" 90)) {
    $errTail = ''
    if (Test-Path -LiteralPath $StderrLog) {
      $errTail = (Get-Content -LiteralPath $StderrLog -Tail 20) -join "`n"
    }
    Fail "FastAPI did not become healthy within 90s.`n$errTail"
  }
}

$listener = Get-ListenerProcess -Port $FastApiPort
if ($listener) { $fastapiPid = $listener.ProcessId }

Write-Step -Label 'FastAPI' -Status ('OK' + $(if ($fastapiReused) { ' (reused)' } else { '' }))
Write-Sub -Text ("Python: {0}" -f $PythonExe)
Write-Sub -Text ("Port: {0}" -f $FastApiPort)
Write-Sub -Text ("PID: {0}" -f $fastapiPid)
if ($listener -and $listener.ExecutablePath -ine $PythonExe) {
  Write-Sub -Text ("Base interpreter: {0}" -f $listener.ExecutablePath)
}

# ================================================================ 5/6. Piper warmup (from FastAPI startup log)
function Get-PiperStatus {
  param([string]$Voice)
  $ok = $false
  $failed = $null
  $text = ''
  if (Test-Path -LiteralPath $StderrLog) { $text = Get-Content -LiteralPath $StderrLog -Raw -ErrorAction SilentlyContinue }
  if ($text -match ("\[STARTUP\] Piper {0} warmup OK" -f $Voice)) { $ok = $true }
  elseif ($text -match ("\[STARTUP\] Piper {0} warmup FAILED: (.+)" -f $Voice)) { $failed = $Matches[1].Trim() }
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

# ================================================================ 7. ngrok
$publicHealthUrl = "$($script:NgrokBase)/health"
function Test-PublicHealth {
  try {
    $resp = Invoke-WebRequest -Uri $publicHealthUrl -TimeoutSec 8 -UseBasicParsing
    return ($resp.StatusCode -eq 200)
  } catch {
    return $false
  }
}

$ngrokOk = Test-PublicHealth
if (-not $ngrokOk) {
  $ngrokProc = Get-Process ngrok -ErrorAction SilentlyContinue
  if ($ngrokProc) {
    Fail "An ngrok process is already running but $publicHealthUrl is not responding."
  }
  $ngrokExe = (Get-Command ngrok -ErrorAction SilentlyContinue).Source
  if (-not $ngrokExe) { Fail "ngrok executable not found on PATH." }
  Start-Process -FilePath $ngrokExe -ArgumentList @('http', [string]$FastApiPort, '--url', $script:NgrokBase) -WindowStyle Hidden
  if (-not (Wait-For { Test-PublicHealth } "ngrok public health $publicHealthUrl" 45)) {
    Fail "ngrok started but $publicHealthUrl did not become healthy within 45s."
  }
  $ngrokOk = $true
}

# Resolve the ngrok process that serves this project's fixed domain so that
# stop-server.ps1 can target it precisely (never a blanket "Stop-Process ngrok").
$ngrokProcObj = Get-ProjectNgrokProcess
if ($ngrokProcObj) { $script:NgrokPid = $ngrokProcObj.ProcessId }

Write-Step -Label 'ngrok' -Status ('OK' + $(if ($ngrokOk -and (Get-Process ngrok -ErrorAction SilentlyContinue)) { '' } else { '' }))
Write-Sub -Text ("Public URL: {0}" -f $script:NgrokBase)

# ---------------------------------------------------------------- runtime state
# Record the live PIDs for stop-server.ps1. No secrets are stored here.
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
$stateObject = [ordered]@{
  fastapi_pid  = $fastapiPid
  ngrok_pid    = $script:NgrokPid
  started_at   = (Get-Date).ToString('o')
  port         = $FastApiPort
  ngrok_domain = $script:NgrokBase
}
$stateObject | ConvertTo-Json | Set-Content -LiteralPath $StateFile -Encoding UTF8

# ---------------------------------------------------------------- summary
Write-Host ''
if ($piperDegraded) {
  Write-Host 'SERVER READY (Piper degraded - TTS may be unavailable)' -ForegroundColor Yellow
} else {
  Write-Host 'SERVER READY' -ForegroundColor Green
}

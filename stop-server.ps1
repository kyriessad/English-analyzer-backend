#Requires -Version 5.1
<#
.SYNOPSIS
  Safely stop the English Analyzer backend: the ngrok tunnel, the FastAPI
  process, and the resident qwen3:8b model — while leaving PostgreSQL and the
  Ollama service running for other projects.

.DESCRIPTION
  Identifies ONLY this project's processes before stopping anything:

    ngrok    — process command line references this project's fixed domain AND
               port 8000, cross-checked against the ngrok local API.
    FastAPI  — the port-8000 listener whose command line is our uvicorn
               (app.main:app), cross-checked against the PID recorded in
               .runtime/server-state.json. An unknown process holding port 8000
               is never killed.
    qwen3:8b — unloaded via "ollama stop qwen3:8b" (frees VRAM; the Ollama
               service itself is left running).

  Idempotent: running it twice is safe; anything already stopped reports
  NOT RUNNING / NOT LOADED.

.EXAMPLE
  cd C:\Users\Administrator\WeChatProjects\English-analyzer-backend
  .\stop-server.ps1
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------- paths
$BackendDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $BackendDir '.venv\Scripts\python.exe'
$FastApiPort = 8000
$OllamaUrl = 'http://127.0.0.1:11434'
$OllamaModel = 'qwen3:8b'
$PgPort = 5432
$NgrokApiUrl = 'http://127.0.0.1:4040/api/tunnels'
$FallbackNgrokDomain = 'https://detergent-starry-oboe.ngrok-free.dev'
$MiniappConfigPath = Join-Path (Split-Path -Parent $BackendDir) 'English-study-miniapp\utils\localBackendConfig.js'
$StateDir = Join-Path $BackendDir '.runtime'
$StateFile = Join-Path $StateDir 'server-state.json'

$script:StepNumber = 0
$script:StepLabel = ''
$script:Incomplete = $false

# ---------------------------------------------------------------- output
function Write-Step {
  param([string]$Label, [string]$Status)

  $script:StepNumber++
  $script:StepLabel = $Label
  $head = "[{0}/3] {1}" -f $script:StepNumber, $Label
  $dots = '.' * [Math]::Max(0, 26 - $head.Length)
  Write-Host ("{0}{1} {2}" -f $head, $dots, $Status)
}

function Write-Sub {
  param([string]$Text)
  Write-Host ("      {0}" -f $Text)
}

function Write-Warn {
  param([string]$Text)
  Write-Host ("      WARNING: {0}" -f $Text) -ForegroundColor Yellow
}

function Write-SummaryLine {
  param([string]$Label, [string]$Status)
  $dots = '.' * [Math]::Max(0, 30 - $Label.Length)
  Write-Host ("{0}{1} {2}" -f $Label, $dots, $Status)
}

# ---------------------------------------------------------------- helpers
function Get-ListenerProcess {
  param([int]$Port)
  $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
  if (-not $conn) { return $null }
  return Get-CimInstance Win32_Process -Filter "ProcessId=$($conn.OwningProcess)" -ErrorAction SilentlyContinue
}

function Test-PortListening {
  param([int]$Port)
  return ($null -ne (Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue))
}

function Test-IsProjectUvicorn {
  param($Proc)
  if (-not $Proc -or -not $Proc.CommandLine) { return $false }
  return ($Proc.CommandLine -match 'uvicorn' -and $Proc.CommandLine -match 'app\.main:app')
}

function Get-OllamaExe {
  $cmd = Get-Command ollama -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  $local = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
  if (Test-Path -LiteralPath $local) { return $local }
  return $null
}

function Test-OllamaUp {
  try {
    Invoke-RestMethod -Uri "$OllamaUrl/api/tags" -TimeoutSec 5 | Out-Null
    return $true
  } catch {
    return $false
  }
}

function Get-NgrokDomainFromConfig {
  if (-not (Test-Path -LiteralPath $MiniappConfigPath)) { return $null }
  $content = Get-Content -LiteralPath $MiniappConfigPath -Raw
  if ($content -match "NGROK_BACKEND_BASE_URL\s*=\s*['`"]([^'`"]+)['`"]") {
    return $Matches[1]
  }
  return $null
}

function Stop-OneProcess {
  param([int]$Id)
  # Returns 'STOPPED' | 'FORCE-STOPPED' | 'NOT RUNNING' | 'FAILED'
  $proc = Get-Process -Id $Id -ErrorAction SilentlyContinue
  if (-not $proc) { return 'NOT RUNNING' }

  try { Stop-Process -Id $Id -ErrorAction Stop } catch { }

  $deadline = (Get-Date).AddSeconds(5)
  while ((Get-Date) -lt $deadline) {
    if (-not (Get-Process -Id $Id -ErrorAction SilentlyContinue)) { return 'STOPPED' }
    Start-Sleep -Milliseconds 300
  }

  $still = Get-Process -Id $Id -ErrorAction SilentlyContinue
  if ($still) {
    try { Stop-Process -Id $Id -Force -ErrorAction Stop } catch { return 'FAILED' }
    if (-not (Get-Process -Id $Id -ErrorAction SilentlyContinue)) { return 'FORCE-STOPPED' }
    return 'FAILED'
  }
  return 'STOPPED'
}

# ---------------------------------------------------------------- load state
$state = $null
if (Test-Path -LiteralPath $StateFile) {
  try {
    $state = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json
  } catch {
    $state = $null
  }
}

$script:NgrokBase = $FallbackNgrokDomain
if ($state -and $state.ngrok_domain) { $script:NgrokBase = [string]$state.ngrok_domain }
else {
  $cfg = Get-NgrokDomainFromConfig
  if ($cfg) { $script:NgrokBase = $cfg }
}
$script:NgrokBase = $script:NgrokBase.TrimEnd('/')
$script:NgrokHost = $null
try { $script:NgrokHost = ([System.Uri]$script:NgrokBase).Host } catch { }

if ($state -and $state.port) { $FastApiPort = [int]$state.port }

$stateFastApiPid = $null
$stateNgrokPid = $null
if ($state) {
  if ($state.fastapi_pid) { $stateFastApiPid = [int]$state.fastapi_pid }
  if ($state.ngrok_pid) { $stateNgrokPid = [int]$state.ngrok_pid }
}

# ================================================================ 1. ngrok
$ngrokIds = @()
$tunnelUp = $false

$ngrokProcs = Get-CimInstance Win32_Process -Filter "Name='ngrok.exe'" -ErrorAction SilentlyContinue
foreach ($p in $ngrokProcs) {
  if ($p.CommandLine -and
      $p.CommandLine -match [regex]::Escape($script:NgrokHost) -and
      $p.CommandLine -match ('\b{0}\b' -f $FastApiPort)) {
    $ngrokIds += $p.ProcessId
  }
}

if ($stateNgrokPid) {
  $sp = Get-CimInstance Win32_Process -Filter "ProcessId=$stateNgrokPid" -ErrorAction SilentlyContinue
  if ($sp -and $sp.Name -eq 'ngrok.exe' -and $sp.CommandLine -and $sp.CommandLine -match [regex]::Escape($script:NgrokHost)) {
    $ngrokIds += $sp.ProcessId
  }
}

$ngrokIds = @($ngrokIds | Sort-Object -Unique)

try {
  $tunnelsResp = Invoke-RestMethod -Uri $NgrokApiUrl -TimeoutSec 5
  foreach ($t in $tunnelsResp.tunnels) {
    if ($t.public_url -and $t.public_url -match [regex]::Escape($script:NgrokHost)) { $tunnelUp = $true }
  }
} catch { }

if ($ngrokIds.Count -gt 0) {
  $allOk = $true
  foreach ($id in $ngrokIds) {
    $r = Stop-OneProcess -Id $id
    if ($r -eq 'FAILED') { $allOk = $false }
  }
  Write-Step -Label 'ngrok' -Status $(if ($allOk) { 'STOPPED' } else { 'STOP FAILED' })
  Write-Sub -Text ("PID: {0}" -f ($ngrokIds -join ', '))
  if (-not $allOk) { $script:Incomplete = $true }
}
elseif ($tunnelUp) {
  Write-Step -Label 'ngrok' -Status 'NOT STOPPED (unconfirmed)'
  Write-Warn ("A tunnel for {0} is still up, but no matching ngrok process could be confirmed; leaving it running." -f $script:NgrokBase)
  $allNgrok = Get-Process ngrok -ErrorAction SilentlyContinue
  foreach ($np in $allNgrok) {
    Write-Sub -Text ("ngrok process PID {0}: {1}" -f $np.Id, $np.Path)
  }
  $script:Incomplete = $true
}
else {
  Write-Step -Label 'ngrok' -Status 'NOT RUNNING'
}

# ================================================================ 2. FastAPI
$ourFastApiIds = @()
$unknownOccupant = $null

$listener = Get-ListenerProcess -Port $FastApiPort
if ($listener) {
  if (Test-IsProjectUvicorn -Proc $listener) {
    $ourFastApiIds += $listener.ProcessId
  } else {
    $unknownOccupant = $listener
  }
}

if ($stateFastApiPid) {
  $sp = Get-CimInstance Win32_Process -Filter "ProcessId=$stateFastApiPid" -ErrorAction SilentlyContinue
  if ($sp -and (Test-IsProjectUvicorn -Proc $sp)) {
    $ourFastApiIds += $sp.ProcessId
  }
}

$ourFastApiIds = @($ourFastApiIds | Sort-Object -Unique)

if ($ourFastApiIds.Count -gt 0) {
  $allOk = $true
  foreach ($id in $ourFastApiIds) {
    $r = Stop-OneProcess -Id $id
    if ($r -eq 'FAILED') { $allOk = $false }
  }
  Write-Step -Label 'FastAPI' -Status $(if ($allOk) { 'STOPPED' } else { 'STOP FAILED' })
  Write-Sub -Text ("PID: {0}" -f ($ourFastApiIds -join ', '))
  Write-Sub -Text 'Piper (in-process) released with FastAPI'
  if (-not $allOk) { $script:Incomplete = $true }

  if ($unknownOccupant) {
    Write-Warn ("Port {0} is also held by an unrecognized process — NOT touched." -f $FastApiPort)
    Write-Sub -Text ("UNKNOWN PID: {0}" -f $unknownOccupant.ProcessId)
    Write-Sub -Text ("UNKNOWN Path: {0}" -f $unknownOccupant.ExecutablePath)
    Write-Sub -Text ("UNKNOWN CommandLine: {0}" -f $unknownOccupant.CommandLine)
    $script:Incomplete = $true
  }
}
elseif ($unknownOccupant) {
  Write-Step -Label 'FastAPI' -Status 'SKIPPED / UNKNOWN PROCESS'
  Write-Sub -Text ("PID: {0}" -f $unknownOccupant.ProcessId)
  Write-Sub -Text ("Path: {0}" -f $unknownOccupant.ExecutablePath)
  Write-Sub -Text ("CommandLine: {0}" -f $unknownOccupant.CommandLine)
  $script:Incomplete = $true
}
else {
  Write-Step -Label 'FastAPI' -Status 'NOT RUNNING'
}

# ================================================================ 3. qwen3:8b unload
$ollamaExe = Get-OllamaExe
$ollamaUp = Test-OllamaUp
$loaded = $false

if ($ollamaUp -and $ollamaExe) {
  try {
    $psText = (& $ollamaExe ps) -join "`n"
    if ($psText -match 'qwen3:8b') { $loaded = $true }
  } catch { }
}

if (-not $ollamaUp) {
  Write-Step -Label 'qwen3:8b' -Status 'OLLAMA DOWN'
  Write-Sub -Text 'Ollama service is not reachable; nothing to unload.'
}
elseif (-not $loaded) {
  Write-Step -Label 'qwen3:8b' -Status 'NOT LOADED'
}
else {
  $unloaded = $false

  if ($ollamaExe) {
    try { & $ollamaExe stop $OllamaModel 2>$null | Out-Null } catch { }
    Start-Sleep -Milliseconds 500
    try {
      $psAfter = (& $ollamaExe ps) -join "`n"
      if ($psAfter -notmatch 'qwen3:8b') { $unloaded = $true }
    } catch { }
  }

  if (-not $unloaded) {
    # Fallback: keep_alive=0 unloads the model without touching the Ollama service.
    try {
      $body = @{ model = $OllamaModel; keep_alive = 0; prompt = '' } | ConvertTo-Json
      Invoke-RestMethod -Method Post -Uri "$OllamaUrl/api/generate" -Body $body -ContentType 'application/json' -TimeoutSec 30 | Out-Null
      Start-Sleep -Milliseconds 500
      if ($ollamaExe) {
        $psAfter2 = (& $ollamaExe ps) -join "`n"
        if ($psAfter2 -notmatch 'qwen3:8b') { $unloaded = $true }
      }
    } catch { }
  }

  if ($unloaded) {
    Write-Step -Label 'qwen3:8b' -Status 'UNLOADED'
  } else {
    Write-Step -Label 'qwen3:8b' -Status 'UNLOAD FAILED'
    Write-Warn 'qwen3:8b is still resident. The Ollama service was NOT stopped. Run "ollama stop qwen3:8b" manually to free VRAM.'
    $script:Incomplete = $true
  }
}

# ---------------------------------------------------------------- summary
Write-Host ''
if (Test-PortListening -Port $PgPort) {
  Write-SummaryLine -Label 'PostgreSQL' -Status 'KEPT RUNNING'
} else {
  Write-SummaryLine -Label 'PostgreSQL' -Status 'NOT RUNNING'
}
if ($ollamaUp) {
  Write-SummaryLine -Label 'Ollama service' -Status 'KEPT RUNNING'
} else {
  Write-SummaryLine -Label 'Ollama service' -Status 'NOT RUNNING'
}
Write-Host ''
if ($script:Incomplete) {
  Write-Host 'SERVER STOP INCOMPLETE' -ForegroundColor Red
} else {
  Write-Host 'SERVER STOPPED' -ForegroundColor Green
}

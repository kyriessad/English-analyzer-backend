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
  return ($Proc.CommandLine -match 'uvicorn' -and
          $Proc.CommandLine -match 'app\.main:app' -and
          $Proc.CommandLine -match ('\b{0}\b' -f $FastApiPort) -and
          $Proc.CommandLine -notmatch '--reload')
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

function Test-ProcessIdentityMatches {
  param(
    $Proc,
    $Identity
  )

  if (-not $Proc -or -not $Identity) { return $false }
  $props = $Identity.PSObject.Properties.Name
  if ($props -contains 'pid' -and $Identity.pid -and [int]$Identity.pid -ne [int]$Proc.ProcessId) { return $false }
  if ($props -contains 'creation_date' -and $Identity.creation_date) {
    $liveCreated = ''
    try { $liveCreated = ([datetime]$Proc.CreationDate).ToString('o') } catch { }
    if ($liveCreated -and $liveCreated -ne [string]$Identity.creation_date) { return $false }
  }
  if ($props -contains 'executable_path' -and $Identity.executable_path -and
      [string]$Proc.ExecutablePath -ine [string]$Identity.executable_path) { return $false }
  if ($props -contains 'command_line_hash' -and $Identity.command_line_hash) {
    $liveHash = Get-CommandLineHash -CommandLine ([string]$Proc.CommandLine)
    if ($liveHash -ne [string]$Identity.command_line_hash) { return $false }
  }
  return $true
}

function Test-ProcessCreatedNearState {
  param(
    $Proc,
    [string]$StartedAt
  )

  if (-not $Proc -or -not $StartedAt) { return $false }
  try {
    $created = [datetime]$Proc.CreationDate
    $stateTime = [datetime]$StartedAt
    return ($created -ge $stateTime.AddMinutes(-20) -and $created -le $stateTime.AddSeconds(5))
  } catch {
    return $false
  }
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
  if ($LauncherPid) {
    foreach ($descId in (Get-ProcessDescendantIds -RootId $LauncherPid)) {
      $dp = Get-ProcessById -Id $descId
      if ($dp -and (Test-IsProjectUvicorn -Proc $dp) -and -not $ids.Contains($descId)) { $ids.Add($descId) }
    }
  }
  if ($ListenerPid -and -not $ids.Contains($ListenerPid)) { $ids.Add($ListenerPid) }
  if ($LauncherPid -and -not $ids.Contains($LauncherPid)) { $ids.Add($LauncherPid) }

  $allOk = $true
  foreach ($id in $ids) {
    $r = Stop-OneProcess -Id $id
    if ($r -eq 'FAILED') { $allOk = $false }
  }

  return [ordered]@{
    Ok = $allOk
    Ids = @($ids)
  }
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

if ($state -and $state.port) { $FastApiPort = [int]$state.port }

$stateFastApiPid = $null
$stateFastApiLauncherPid = $null
$stateFastApiListenerPid = $null
$stateFastApiLauncherIdentity = $null
$stateFastApiListenerIdentity = $null
$stateStartedAt = $null
$stateNgrokPid = $null
if ($state) {
  $stateProps = $state.PSObject.Properties.Name
  if ($stateProps -contains 'fastapi_pid' -and $state.fastapi_pid) { $stateFastApiPid = [int]$state.fastapi_pid }
  if ($stateProps -contains 'fastapi_launcher_pid' -and $state.fastapi_launcher_pid) { $stateFastApiLauncherPid = [int]$state.fastapi_launcher_pid }
  if ($stateProps -contains 'fastapi_listener_pid' -and $state.fastapi_listener_pid) { $stateFastApiListenerPid = [int]$state.fastapi_listener_pid }
  if ($stateProps -contains 'fastapi_launcher_identity') { $stateFastApiLauncherIdentity = $state.fastapi_launcher_identity }
  if ($stateProps -contains 'fastapi_listener_identity') { $stateFastApiListenerIdentity = $state.fastapi_listener_identity }
  if ($stateProps -contains 'started_at' -and $state.started_at) { $stateStartedAt = [string]$state.started_at }
  if ($stateProps -contains 'ngrok_pid' -and $state.ngrok_pid) { $stateNgrokPid = [int]$state.ngrok_pid }
}

# ================================================================ 1. ngrok
$ngrokIds = @()
if ($stateNgrokPid) {
  $sp = Get-CimInstance Win32_Process -Filter "ProcessId=$stateNgrokPid" -ErrorAction SilentlyContinue
  if ($sp -and $sp.Name -eq 'ngrok.exe' -and $sp.CommandLine -match ('\b{0}\b' -f $FastApiPort)) {
    $ngrokIds += $sp.ProcessId
  }
}

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
else {
  Write-Step -Label 'ngrok' -Status 'NOT RUNNING'
}

# ================================================================ 2. FastAPI
$unknownOccupant = $null
$trackedLauncherPid = $stateFastApiLauncherPid
$trackedListenerPid = $stateFastApiListenerPid

if ($stateFastApiPid) {
  # Legacy state only had fastapi_pid. Older versions wrote the port listener
  # after resolving it, but tolerate a launcher PID if such a file exists.
  $legacyProc = Get-ProcessById -Id $stateFastApiPid
  if (-not $trackedListenerPid -and $legacyProc -and (Test-IsProjectUvicorn -Proc $legacyProc)) {
    $trackedListenerPid = $stateFastApiPid
  } elseif (-not $trackedLauncherPid) {
    $trackedLauncherPid = $stateFastApiPid
  }
}

$listener = Get-ListenerProcess -Port $FastApiPort
if ($listener) {
  if (Test-IsProjectUvicorn -Proc $listener) {
    $matchesTrackedListener = ($trackedListenerPid -and $listener.ProcessId -eq $trackedListenerPid)
    $matchesTrackedIdentity = ($matchesTrackedListener -and
      (-not $stateFastApiListenerIdentity -or
       (Test-ProcessIdentityMatches -Proc $listener -Identity $stateFastApiListenerIdentity)))
    $belongsToTrackedLauncher = ($trackedLauncherPid -and
      (Test-IsAncestorProcess -AncestorId $trackedLauncherPid -ProcessId $listener.ProcessId))

    # Compatibility for managed_version 2 state files that recorded the
    # launcher PID only. This is deliberately narrower than the new listener
    # identity path: it requires the expected command/port and a process start
    # time consistent with the state file.
    $legacyLauncherStateMatches = (-not $trackedListenerPid -and $trackedLauncherPid -and
      $stateFastApiPid -eq $trackedLauncherPid -and
      (Test-ProcessCreatedNearState -Proc $listener -StartedAt $stateStartedAt))

    if ($matchesTrackedListener -and -not $matchesTrackedIdentity) {
      Write-Warn ("Tracked FastAPI listener PID {0} failed identity validation; leaving it running." -f $listener.ProcessId)
      $unknownOccupant = $listener
    } elseif (-not ($matchesTrackedIdentity -or $belongsToTrackedLauncher -or $legacyLauncherStateMatches)) {
      $unknownOccupant = $listener
    } else {
      if (-not $trackedListenerPid) { $trackedListenerPid = [int]$listener.ProcessId }
    }
  } else {
    $unknownOccupant = $listener
  }
}

if (-not $unknownOccupant -and $trackedListenerPid) {
  $trackedListenerProc = Get-ProcessById -Id $trackedListenerPid
  if ($trackedListenerProc -and -not (Test-IsProjectUvicorn -Proc $trackedListenerProc)) {
    Write-Warn ("Tracked FastAPI listener PID {0} is no longer this project's uvicorn; leaving it running." -f $trackedListenerPid)
    $trackedListenerPid = $null
    $script:Incomplete = $true
  }
}

if (-not $unknownOccupant -and ($trackedLauncherPid -or $trackedListenerPid)) {
  if ($trackedLauncherPid) {
    $launcherProc = Get-ProcessById -Id $trackedLauncherPid
    if ($launcherProc -and $stateFastApiLauncherIdentity -and
        -not (Test-ProcessIdentityMatches -Proc $launcherProc -Identity $stateFastApiLauncherIdentity)) {
      Write-Warn ("Tracked FastAPI launcher PID {0} failed identity validation; it will not be stopped." -f $trackedLauncherPid)
      $trackedLauncherPid = $null
      $script:Incomplete = $true
    } elseif ($launcherProc -and -not $stateFastApiLauncherIdentity -and
              -not (Test-IsProjectUvicorn -Proc $launcherProc)) {
      Write-Warn ("Tracked FastAPI launcher PID {0} no longer matches this project's uvicorn command; it will not be stopped." -f $trackedLauncherPid)
      $trackedLauncherPid = $null
      $script:Incomplete = $true
    }
  }
  $stopResult = Stop-FastApiProcessTree -LauncherPid $trackedLauncherPid -ListenerPid $trackedListenerPid
  $stoppedIds = @($stopResult.Ids | Sort-Object -Unique)
  $portCleared = -not (Test-PortListening -Port $FastApiPort)
  Write-Step -Label 'FastAPI' -Status $(if ($stopResult.Ok -and $portCleared) { 'STOPPED' } else { 'STOP FAILED' })
  if ($trackedLauncherPid) { Write-Sub -Text ("Launcher PID: {0}" -f $trackedLauncherPid) }
  if ($trackedListenerPid) { Write-Sub -Text ("Listener PID: {0}" -f $trackedListenerPid) }
  if ($stoppedIds.Count -gt 0) { Write-Sub -Text ("Stopped PID: {0}" -f ($stoppedIds -join ', ')) }
  Write-Sub -Text 'Piper (in-process) released with FastAPI'
  if (-not $portCleared) { Write-Warn ("Port {0} is still LISTENING after stop attempt." -f $FastApiPort) }
  if (-not $stopResult.Ok -or -not $portCleared) { $script:Incomplete = $true }
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

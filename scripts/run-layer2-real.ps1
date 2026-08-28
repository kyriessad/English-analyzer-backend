[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendRoot = Split-Path -Parent $ScriptRoot
$Python = Join-Path $BackendRoot '.venv\Scripts\python.exe'
$HarperRoot = Join-Path $BackendRoot 'harper-sidecar'
$ArtifactRoot = Join-Path $BackendRoot '.e2e-artifacts'
$RunId = 'layer2-{0}-{1}' -f (Get-Date -Format 'yyyyMMddTHHmmss'), ([guid]::NewGuid().ToString('N').Substring(0, 6))
$ArtifactDir = Join-Path $ArtifactRoot $RunId
$HarperLog = Join-Path $ArtifactDir 'harper-sidecar.log'
$HarperErrorLog = Join-Path $ArtifactDir 'harper-sidecar.error.log'
$StartedHarper = $null
$PreviousRunLayer2 = $env:RUN_LAYER2
$PreviousHarperEnabled = $env:HARPER_ENABLED
$PreviousHarperBaseUrl = $env:HARPER_BASE_URL
$PreviousArtifactDir = $env:LAYER2_ARTIFACT_DIR

function Test-HarperReady {
  try {
    $response = Invoke-RestMethod -Uri 'http://127.0.0.1:8082/health' -TimeoutSec 2
    return ($response.status -eq 'ok' -and $response.service -eq 'harper-sidecar')
  } catch {
    return $false
  }
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
  throw "Project Python was not found: $Python"
}
if (-not (Test-Path -LiteralPath (Join-Path $HarperRoot 'server.mjs') -PathType Leaf)) {
  throw 'Harper sidecar source is missing.'
}

New-Item -ItemType Directory -Force -Path $ArtifactDir | Out-Null
Set-Location $BackendRoot

try {
  $listener = Get-NetTCPConnection -LocalPort 8082 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($listener -and -not (Test-HarperReady)) {
    throw "Port 8082 is occupied by a process that is not the Harper sidecar (PID $($listener.OwningProcess))."
  }
  if (-not $listener) {
    $node = (Get-Command node -ErrorAction Stop).Source
    $StartedHarper = Start-Process -FilePath $node -ArgumentList @('server.mjs') `
      -WorkingDirectory $HarperRoot -RedirectStandardOutput $HarperLog `
      -RedirectStandardError $HarperErrorLog -WindowStyle Hidden -PassThru
    $deadline = (Get-Date).AddSeconds(30)
    while (-not (Test-HarperReady) -and (Get-Date) -lt $deadline) {
      if ($StartedHarper.HasExited) { break }
      Start-Sleep -Milliseconds 100
    }
    if (-not (Test-HarperReady)) {
      throw 'Harper sidecar did not become ready within 30 seconds.'
    }
  }

  $env:RUN_LAYER2 = '1'
  $env:HARPER_ENABLED = 'true'
  $env:HARPER_BASE_URL = 'http://127.0.0.1:8082'
  $env:LAYER2_ARTIFACT_DIR = $ArtifactDir
  & $Python -m pytest -q tests\test_layer2_real_dependencies.py tests\test_layer2_services.py `
    -m layer2 --junitxml (Join-Path $ArtifactDir 'junit.xml')
  if ($LASTEXITCODE -ne 0) {
    throw 'Layer 2 real dependency tests failed.'
  }

  $summary = [ordered]@{
    run_id = $RunId
    finished_at = (Get-Date).ToString('o')
    status = 'PASS'
    git_commit = (& git rev-parse HEAD).Trim()
    git_dirty = [bool]((& git status --porcelain) -join '')
    dependencies = @('ECDICT', 'SymSpell', 'Harper', 'Piper')
    junit = (Join-Path $ArtifactDir 'junit.xml')
  }
  $summary | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $ArtifactDir 'result.json') -Encoding UTF8
  Write-Host "LAYER2_ARTIFACT_DIR $ArtifactDir"
}
finally {
  foreach ($entry in @(
    @{ Name = 'RUN_LAYER2'; Value = $PreviousRunLayer2 },
    @{ Name = 'HARPER_ENABLED'; Value = $PreviousHarperEnabled },
    @{ Name = 'HARPER_BASE_URL'; Value = $PreviousHarperBaseUrl },
    @{ Name = 'LAYER2_ARTIFACT_DIR'; Value = $PreviousArtifactDir }
  )) {
    if ($null -eq $entry.Value) {
      Remove-Item ("Env:{0}" -f $entry.Name) -ErrorAction SilentlyContinue
    } else {
      Set-Item ("Env:{0}" -f $entry.Name) $entry.Value
    }
  }
  if ($StartedHarper -and -not $StartedHarper.HasExited) {
    Stop-Process -Id $StartedHarper.Id -Force -ErrorAction SilentlyContinue
    $StartedHarper.WaitForExit(10000) | Out-Null
  }
}

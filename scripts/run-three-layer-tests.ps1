[CmdletBinding()]
param(
  [ValidateSet('Daily', 'PrePush', 'Layer1', 'Layer2', 'Layer3', 'Release', 'Manual')]
  [string]$Tier = 'Daily'
)

$ErrorActionPreference = 'Stop'
$ScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$BackendRoot = Split-Path -Parent $ScriptRoot
$WorkspaceRoot = Split-Path -Parent $BackendRoot
$MiniappRoot = Join-Path $WorkspaceRoot 'English-study-miniapp'
$Python = Join-Path $BackendRoot '.venv\Scripts\python.exe'

function Assert-LastExitCode([string]$Label) {
  if ($LASTEXITCODE -ne 0) { throw "$Label failed with exit code $LASTEXITCODE." }
}

function Invoke-Layer1 {
  Set-Location $BackendRoot
  & $Python -m pytest -q tests\test_frozen_contract_layer1.py `
    tests\test_frozen_api_card_layer1.py tests\test_frozen_lexical_tts_layer1.py `
    tests\test_discovery_api.py
  Assert-LastExitCode 'Frozen Layer 1'
  & $Python -m pytest -q tests\test_contract_traceability.py
  Assert-LastExitCode 'Contract traceability'
  Set-Location $MiniappRoot
  & node --test tests\frozen-corpus.test.js tests\english-validation.test.js `
    tests\validation-state-machine.test.js tests\stream-provisional.test.js
  Assert-LastExitCode 'Mini Program deterministic tests'
  & node --test tests\discovery-flow.test.js
  Assert-LastExitCode 'Mini Program discovery tests'
}

function Invoke-PrePush {
  Invoke-Layer1
  Set-Location $BackendRoot
  # Review V1 replaced the legacy result-based feedback contract. Its current
  # frozen/API coverage lives in test_review_v1_api.py; the phase2 module is
  # retained only as historical coverage for the retired endpoint shape.
  & $Python -m pytest -q --ignore=tests\test_layer2_real_dependencies.py `
    --ignore=tests\test_reviews_phase2_api.py
  Assert-LastExitCode 'Backend pre-push regression'
}

function Invoke-Layer2 {
  Set-Location $BackendRoot
  & (Join-Path $ScriptRoot 'run-postgresql-tests.ps1')
  Assert-LastExitCode 'PostgreSQL regression'
  & (Join-Path $ScriptRoot 'run-layer2-real.ps1')
  Assert-LastExitCode 'Real dependency regression'
  & (Join-Path $ScriptRoot 'run-level7-e2e.ps1') -Through all
  Assert-LastExitCode 'Level 7 integration/capacity/fault suite'
}

function Invoke-Layer3 {
  Set-Location $BackendRoot
  $before = @(Get-ChildItem .e2e-artifacts -Directory -ErrorAction SilentlyContinue | ForEach-Object FullName)
  & (Join-Path $ScriptRoot 'run-level7-wechat-e2e.ps1')
  Assert-LastExitCode 'Real WeChat Layer 3 runner'
  $latest = Get-ChildItem .e2e-artifacts -Directory | Where-Object { $before -notcontains $_.FullName } |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if (-not $latest) { throw 'Layer 3 runner did not create a new artifact directory.' }
  $artifact = Join-Path $latest.FullName 'layer3-journeys.json'
  if (-not (Test-Path -LiteralPath $artifact -PathType Leaf)) {
    throw "Layer 3 evidence artifact is missing: $artifact"
  }
  Set-Location $MiniappRoot
  $env:RUN_LAYER3 = '1'
  $env:LAYER3_ARTIFACT_PATH = $artifact
  try {
    & node --test tests\layer3-journeys.test.js
    Assert-LastExitCode 'Layer 3 evidence contract'
  } finally {
    Remove-Item Env:RUN_LAYER3,Env:LAYER3_ARTIFACT_PATH -ErrorAction SilentlyContinue
  }
}

switch ($Tier) {
  'Daily' { Invoke-Layer1 }
  'Layer1' { Invoke-Layer1 }
  'PrePush' { Invoke-PrePush }
  'Layer2' { Invoke-Layer2 }
  'Layer3' { Invoke-Layer3 }
  'Release' { Invoke-Layer1; Invoke-Layer2; Invoke-Layer3 }
  'Manual' {
    Write-Host 'Tier D remains explicit: real phones/speakers, DB exhaustion, process disaster, and backup/restore.'
    Write-Host 'Capacity automation: .\scripts\run-level7-e2e.ps1 -Through all'
    Write-Host 'Phone diagnostics: see docs\three-layer-testing.md'
  }
}

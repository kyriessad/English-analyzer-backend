$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
  $Python = "python"
}

function Get-OllamaPath {
  $command = Get-Command ollama -ErrorAction SilentlyContinue
  if ($command) {
    return $command.Source
  }

  $localPath = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
  if (Test-Path $localPath) {
    return $localPath
  }

  throw "ollama command not found. Install Ollama for Windows, then reopen PowerShell or use the official installer."
}

function Ensure-OllamaApi {
  param([string]$OllamaPath)

  try {
    Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 5 | Out-Null
    return
  } catch {
    Write-Host "Ollama API is not responding. Trying to start 'ollama serve'..."
    Start-Process -FilePath $OllamaPath -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 5
    Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 10 | Out-Null
  }
}

Set-Location $RepoRoot

$Ollama = Get-OllamaPath
& $Ollama --version
Ensure-OllamaApi -OllamaPath $Ollama

$models = (& $Ollama list) -join "`n"
if ($models -notmatch "qwen3:8b") {
  & $Ollama pull qwen3:8b
}

& $Python -m pip install argostranslate
& $Python "tools\install_argos_en_zh.py"

& $Python -c "from argostranslate import translate; langs=translate.get_installed_languages(); en=next((l for l in langs if l.code=='en'), None); zh=next((l for l in langs if l.code=='zh'), None); tr=en.get_translation(zh) if en and zh else None; assert tr is not None; print(tr.translate('I study English every day.'))"

$schema = @{
  type = "object"
  properties = @{
    exampleSentence = @{ type = "string" }
    exampleTranslation = @{ type = "string" }
  }
  required = @("exampleSentence", "exampleTranslation")
}
$body = @{
  model = "qwen3:8b"
  system = "Return only JSON."
  prompt = "Return JSON with exampleSentence and exampleTranslation. The English sentence must contain: break a leg"
  stream = $false
  think = $false
  format = $schema
  options = @{
    temperature = 0.3
    num_predict = 180
  }
  keep_alive = "5m"
} | ConvertTo-Json -Depth 8

try {
  Invoke-RestMethod "http://127.0.0.1:11434/api/generate" -Method Post -ContentType "application/json" -Body $body -TimeoutSec 60 | Out-Null
} catch {
  $fallbackBody = @{
    model = "qwen3:8b"
    system = "Return only JSON."
    prompt = "Return JSON with exampleSentence and exampleTranslation. The English sentence must contain: break a leg"
    stream = $false
    think = $false
    format = "json"
    options = @{
      temperature = 0.3
      num_predict = 180
    }
    keep_alive = "5m"
  } | ConvertTo-Json -Depth 8
  Invoke-RestMethod "http://127.0.0.1:11434/api/generate" -Method Post -ContentType "application/json" -Body $fallbackBody -TimeoutSec 60 | Out-Null
}

Write-Host "Local AI setup completed."

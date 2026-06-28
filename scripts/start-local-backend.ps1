$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
  throw "Python virtual environment not found at .venv\Scripts\python.exe"
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

  throw "ollama command not found. Install Ollama for Windows, then reopen PowerShell."
}

Set-Location $RepoRoot

& $Python --version

& $Python -c "from argostranslate import translate; langs=translate.get_installed_languages(); en=next((l for l in langs if l.code=='en'), None); zh=next((l for l in langs if l.code=='zh'), None); assert en and zh and en.get_translation(zh), 'Argos en->zh model missing. Run scripts\\setup-local-ai.ps1 first.'; print('Argos en->zh model OK')"

Invoke-RestMethod "http://127.0.0.1:11434/api/tags" -TimeoutSec 5 | Out-Null

$Ollama = Get-OllamaPath
$models = (& $Ollama list) -join "`n"
if ($models -notmatch "qwen3:8b") {
  throw "qwen3:8b is not installed. Run: ollama pull qwen3:8b"
}

& $Python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

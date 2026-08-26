# English Analyzer Backend

FastAPI backend for the English Study WeChat Mini Program. It stores learning
cards and review history in PostgreSQL, authenticates real Mini Program users
with wx.login, analyzes English with local AI, and creates Piper WAV audio.

## Features

- WeChat code-to-session login and JWT authentication
- Card CRUD and incremental synchronization
- English analysis and NDJSON streaming analysis
- Local Argos English-to-Chinese translation and Ollama qwen3:8b examples
- Lexical information and Piper pronunciation audio
- Daily, new-card, and free review sessions with feedback and history

## Architecture

    WeChat Mini Program
            |
            | HTTP in local DevTools, HTTPS for a phone
            v
    FastAPI ---- PostgreSQL
      |--- Argos Translate
      |--- Ollama (qwen3:8b)
      \--- Piper TTS

## Requirements

For a new Windows computer, use the complete
[Windows Setup Guide](docs/setup-windows.md). It covers Git, Python,
PostgreSQL, Ollama, Piper, WeChat DevTools, local testing, phone HTTPS access,
tests, and shutdown.

The runtime requires Python 3.12 (the version used by this project),
PostgreSQL, and real WeChat Mini Program credentials. AI additionally requires
Ollama and qwen3:8b; TTS additionally requires the Piper voice asset pairs
described in the setup guide.

## Quick Start

This is the 10-15 minute path for developers who already have the prerequisites.
Do not use example values as real secrets.

    git clone https://github.com/kyriessad/English-analyzer-backend.git
    cd English-analyzer-backend
    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    Copy-Item .env.example .env

Edit .env and set at least DATABASE_URL, WECHAT_APPID, WECHAT_SECRET, and
JWT_SECRET_KEY. Keep the default database name only if you created
english_analyzer.

    .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"
    .\.venv\Scripts\alembic.exe upgrade head
    .\.venv\Scripts\python.exe scripts\check_config.py
    .\.venv\Scripts\python.exe scripts\check_database_target.py
    .\scripts\setup-ecdict.ps1
    .\scripts\setup-local-ai.ps1
    .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

In another PowerShell window, verify:

    Invoke-RestMethod http://127.0.0.1:8000/health

Expected result: @{status=ok}. This is the recommended first-start path because
it has no ngrok dependency. `start-server.ps1` is also local by default;
enable ngrok only for phone HTTPS as described in the setup guide.

## Configuration

Copy .env.example to .env; .env is ignored by Git. Each developer must provide:

- DATABASE_URL, for example:
  postgresql+psycopg://english_app:choose_a_password@127.0.0.1:5432/english_analyzer
- WECHAT_APPID and WECHAT_SECRET for the same Mini Program
- JWT_SECRET_KEY, generated with the Python command above
- SESSION_SECRET_KEY when enabling the optional web-account flow

Usually keep the template defaults for pool sizes, concurrency, quotas, timeouts,
and AI/TTS resource limits. Do not change protection values merely to hide an
error. PIPER_* and ECDICT_DB_PATH are optional path overrides; see the setup
guide.

## Testing

Run the PostgreSQL-backed suite from this repository directory:

    .\scripts\run-postgresql-tests.ps1

It creates, migrates, and drops the isolated english_analyzer_phase1_pytest
database. DATABASE_URL must first point to the approved PostgreSQL server.

For an isolated real-dependency smoke test:

    .\scripts\run-level7-e2e.ps1

The release-only migration procedure is intentionally separate. Read
[release-runbook.md](docs/release-runbook.md) before running
scripts\release-upgrade.ps1.

## Documentation

- [Windows Setup Guide](docs/setup-windows.md): a new computer through Mini
  Program, AI, TTS, review, tests, and shutdown
- [Troubleshooting](docs/troubleshooting.md): first-run failures and checks
- [Release Runbook](docs/release-runbook.md): explicit release preflight and
  migration procedure
- [E2E README](e2e/README.md): isolated real-dependency smoke test boundary

## Repository Structure

    app/        FastAPI application, routes, services, and models
    alembic/    PostgreSQL schema migrations
    scripts/    startup, checks, tests, backup, restore, and release commands
    tools/      local language-asset helpers
    tests/      unit and PostgreSQL-backed tests
    e2e/        isolated real-dependency E2E runner
    docs/       setup, troubleshooting, release, and E2E reports

## Security and Status

Never commit .env, AppSecret, JWT/session secrets, database passwords, API keys,
tokens, cookies, private keys, dumps, Piper models, or Argos models. This
repository documents a Windows single-machine development baseline. A production
deployment needs its own domain, HTTPS, secret management, backup, and release
process; a temporary ngrok URL is not production infrastructure.

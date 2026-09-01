# Windows Setup Guide

This guide starts with a new Windows computer, the two public repositories, and
your own WeChat Mini Program AppID/AppSecret. It does not use an existing
developer's paths, database, tunnel, or credentials.

## 0. Goal and Tools

By the end you can run the backend, compile the Mini Program, perform real
wx.login, create a card, run AI and streaming analysis, play TTS, submit a
review, run tests, and stop the local processes.

Install these external tools first. They are not committed in either repository.

| Layer | Install | Success sign |
| --- | --- | --- |
| Source | [Git for Windows](https://git-scm.com/download/win) | git --version prints a version. |
| Backend | [Python 3.12](https://www.python.org/downloads/windows/) | python --version prints 3.12.x. Select Add Python to PATH during installation. |
| Database | [PostgreSQL for Windows](https://www.postgresql.org/download/windows/) | psql --version prints a version and the service is running. |
| AI | [Ollama for Windows](https://ollama.com/download/windows) | ollama --version prints a version. |
| Mini Program | [WeChat Developer Tools](https://developers.weixin.qq.com/miniprogram/dev/devtools/download.html) | You can sign in and open the project import screen. |
| Tests only | [Node.js LTS](https://nodejs.org/) | node --version prints a version. |
| Phone HTTPS only | [ngrok](https://ngrok.com/download) plus an ngrok account | ngrok version prints a version. |

Run commands in PowerShell. Close and reopen PowerShell after an installer
changes PATH. Ollama serves its local API at http://127.0.0.1:11434. The
qwen3:8b model currently uses about 5.2 GB of model storage, so leave adequate
disk and memory headroom.

## 1. Clone Both Repositories

Choose any local work directory, for example C:\Projects:

    New-Item -ItemType Directory -Force C:\Projects | Out-Null
    Set-Location C:\Projects
    git clone https://github.com/kyriessad/English-analyzer-backend.git
    git clone https://github.com/kyriessad/English-study-miniapp.git

Success: the two repository directories exist. Unless noted otherwise, run the
following backend commands in C:\Projects\English-analyzer-backend.

## 2. Create the Python Environment

    Set-Location C:\Projects\English-analyzer-backend
    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt

Success: (.venv) appears in the PowerShell prompt and pip exits with code 0.
If PowerShell blocks activation, run Set-ExecutionPolicy -Scope Process Bypass
once in that window, then activate again. Do not change machine-wide policy.

## 3. Install PostgreSQL and Create the Database

Use the official Windows installer. Keep the PostgreSQL administrator password;
it is not the application's database password. In a new PowerShell window:

    psql --version
    Get-Service -Name 'postgresql*'

Success: psql prints a version and a PostgreSQL service reports Running. If psql
is not on PATH, use the psql.exe in the installed PostgreSQL bin directory or
add that bin directory to your user PATH.

Create a least-privileged application role and UTF-8 database. Replace the
password placeholder before running the command. It will request the PostgreSQL
administrator password selected during installation.

    psql -U postgres -d postgres -c "CREATE ROLE english_app LOGIN PASSWORD 'replace_with_a_unique_database_password';"
    psql -U postgres -d postgres -c "CREATE DATABASE english_analyzer OWNER english_app ENCODING 'UTF8';"

If they already exist, do not rerun CREATE. Instead verify connectivity:

    psql -U english_app -d english_analyzer -h 127.0.0.1 -c "SELECT current_database(), current_user, current_schema();"

Success: the query returns english_analyzer, english_app, and public. This
project has no PostgreSQL extension prerequisite and uses the public schema.

## 4. Create .env and Initialize Alembic

    Copy-Item .env.example .env
    .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(48))"

Open .env and use the generated output as JWT_SECRET_KEY. Set these values with
your own credentials:

    DATABASE_URL=postgresql+psycopg://english_app:replace_with_a_unique_database_password@127.0.0.1:5432/english_analyzer
    EXPECTED_DATABASE_DIALECT=postgresql
    EXPECTED_DATABASE_NAME=english_analyzer
    EXPECTED_DATABASE_SCHEMA=public
    WECHAT_APPID=your_mini_program_appid
    WECHAT_SECRET=your_mini_program_appsecret
    JWT_SECRET_KEY=paste_the_generated_value_here

JWT_SECRET_KEY, AppSecret, and the database password are secret. .env is ignored
by Git; never put real values in commits, issues, screenshots, or this guide.
Keep JWT_EXPIRE_DAYS=3; configuration validates it is between 1 and 3.
SESSION_SECRET_KEY is needed only for the optional web-account flow, but replace
its example value before enabling that flow.

Usually keep DB_*, AI_*, TTS_*, HTTP_LIMIT_CONCURRENCY, quota, and timeout
defaults. They protect the single-process server. The normal local providers are
TRANSLATION_PROVIDER=argos and EXAMPLE_GENERATOR_PROVIDER=ollama.

Apply and verify the database schema:

    .\.venv\Scripts\alembic.exe upgrade head
    .\.venv\Scripts\python.exe scripts\check_config.py
    .\.venv\Scripts\python.exe scripts\check_database_target.py
    .\.venv\Scripts\alembic.exe current
    .\.venv\Scripts\alembic.exe heads

Success: both check scripts exit successfully, the target check reports
PostgreSQL, english_analyzer, and public, and current equals heads. upgrade head
is an intentional schema change. Normal startup does not migrate the database.

## 5. Prepare Local AI: Argos, Ollama, and Qwen

Argos is the local English-to-Chinese translation provider. Ollama is the local
model service used for examples and analysis.

    .\scripts\setup-local-ai.ps1
    ollama list
    ollama run qwen3:8b "Reply with exactly: local AI is ready"

setup-local-ai.ps1 checks or starts the Ollama API, pulls qwen3:8b if missing,
installs argostranslate into the project virtual environment, downloads and
installs the Argos English-to-Chinese package, tests a translation, and sends a
structured Ollama generation request. It does not install the Ollama Windows
application, Piper voice files, PostgreSQL, WeChat DevTools, or ngrok.

Success: the script exits with code 0, ollama list contains qwen3:8b, and the
last command returns the requested phrase. Do not change the configured model
identifier unless you deliberately change .env. See the official
[Ollama Windows guide](https://docs.ollama.com/windows) and
[Qwen3 model page](https://ollama.com/library/qwen3).

## 6. Prepare Piper TTS

requirements.txt installs piper-tts==1.4.2. This code does not use a separate
Piper executable; it loads Piper through Python. Voice models are intentionally
not in Git and are ignored under data\piper\. Each configured voice needs both
an ONNX model and its matching ONNX JSON configuration.

    New-Item -ItemType Directory -Force .\data\piper | Out-Null

Download the exact file pairs from the linked folders. Read each MODEL_CARD
before redistribution or changing voices.

| UI voice | Default model | Required files | Source |
| --- | --- | --- | --- |
| female | en_US-lessac-medium | en_US-lessac-medium.onnx and en_US-lessac-medium.onnx.json | [lessac medium](https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/lessac/medium) |
| male | en_US-hfc_male-medium | en_US-hfc_male-medium.onnx and en_US-hfc_male-medium.onnx.json | [hfc_male medium](https://huggingface.co/rhasspy/piper-voices/tree/main/en/en_US/hfc_male/medium) |

Put all four files directly under data\piper\. Then run:

    .\.venv\Scripts\python.exe -c "from app.services.piper_service import pronunciation_available; print(pronunciation_available('female')); print(pronunciation_available('male'))"

Success: it prints True twice. PIPER_DATA_DIR can move the directory;
PIPER_FEMALE_VOICE, PIPER_MALE_VOICE, PIPER_DEFAULT_VOICE, and
PIPER_AUDIO_CACHE_DIR are optional overrides.

## 7. Prepare ECDICT

ECDICT provides local phonetic and dictionary enrichment. It is downloaded from
the upstream [skywind3000/ECDICT](https://github.com/skywind3000/ECDICT)
repository at pinned commit `bc015ed2e24a7abef49fc6dbbb7fe32c1dadaf8b`
under its MIT license. The exact raw source URL is
`https://raw.githubusercontent.com/skywind3000/ECDICT/bc015ed2e24a7abef49fc6dbbb7fe32c1dadaf8b/ecdict.csv`.
The upstream source is CSV;
the project builds the compatible SQLite `stardict` table locally.

    .\scripts\setup-ecdict.ps1

The generated, Git-ignored target is `data\ecdict\ecdict.db`. The command keeps
an existing valid database unchanged. It verifies SQLite can open it, that the
required `stardict.word`, `phonetic`, and `translation` fields exist, and that a
known `hello` lookup has phonetic and translation data. Success prints
`ECDICT READY`; failure prints `ECDICT SETUP FAILED` and exits non-zero.

After ECDICT is ready, install the versioned public discovery corpus. This
command is transactional and idempotent, so it is safe to repeat after a
content-version update:

    .\.venv\Scripts\python.exe scripts\seed_discovery_content.py --word-limit 500

Success prints `DISCOVERY CONTENT READY` with five 500-word packs, six
18-entry expression packs, and 365 daily quotes. Normal backend startup does
not rerun this import.

## 8. Start and Verify the Backend

For the first local start, use Uvicorn directly. It works without ngrok and is
the correct path for a DevTools simulator using 127.0.0.1.

    .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

Leave that terminal open. In another terminal:

    Invoke-RestMethod http://127.0.0.1:8000/health

Success: it returns @{status=ok}. Startup can report a Piper warmup failure when
voice assets are absent; the server can still start but TTS will be unavailable.

### Managed Start Script

start-server.ps1 additionally checks PostgreSQL, configuration, Alembic,
Ollama, FastAPI, and Piper warmup. ngrok is disabled by default, so this is a
valid local managed-start command:

    .\start-server.ps1

Success: it ends with SERVER READY, or SERVER READY (Piper degraded - TTS may be
unavailable), and reports `ngrok DISABLED`. For release migrations, use
[release-runbook.md](release-runbook.md), not this script.

## 9. Configure and Run the Mini Program

The client reads BACKEND_BASE_URL from its ignored local configuration. Create it
from the tracked example, then use the local default for DevTools:

    Set-Location C:\Projects\English-study-miniapp
    Copy-Item .\utils\localBackendConfig.example.js .\utils\localBackendConfig.js

In WeChat Developer Tools, import C:\Projects\English-study-miniapp. Choose
your own Mini Program AppID during import, or set the appid in
project.config.json in your local worktree. It must match WECHAT_APPID in
backend .env. Never put AppSecret in the Mini Program repository.

For local development only, enable the DevTools option commonly labelled
Do not verify valid domains, web-view domains, TLS versions, and HTTPS
certificates. It permits HTTP 127.0.0.1 in the simulator and is not a release
setting. Compile the project.

Success: the project compiles without a blocking error. Trigger a backend action:
the app calls wx.login, posts the code to /api/auth/wechat-login, and stores the
returned access token. A successful login lets cards load from the backend.

## 10. Phone HTTPS with ngrok (optional)

DevTools and a phone are different clients. 127.0.0.1 on a phone means the
phone itself, not your Windows computer. Real Mini Program requests generally
need an HTTPS domain; ngrok is a development bridge, not a FastAPI dependency
or production solution.

Install and configure your own ngrok account only when phone HTTPS is needed.
The simplest temporary-tunnel path is to leave the backend running, then in
another PowerShell window run:

    ngrok http 8000

Copy the displayed HTTPS forwarding URL, update the ignored
`utils\localBackendConfig.js`, and add its host to backend `.env`:

    ALLOWED_HOSTS=127.0.0.1,localhost,your-temporary-domain.ngrok-free.app

Restart Uvicorn after changing .env, then use DevTools Preview and scan its QR
code in WeChat. Success: the phone can log in and load backend data. When ngrok
creates a new URL, repeat both updates. Production requires a stable domain,
HTTPS certificate, and WeChat legal-domain configuration.

To let the managed script start ngrok instead, set these optional `.env` values:

    NGROK_ENABLED=true
    NGROK_DOMAIN=
    NGROK_EXE=

`NGROK_EXE` is optional and otherwise uses `ngrok` on PATH. An empty
`NGROK_DOMAIN` asks ngrok for a temporary URL; set a URL only when your ngrok
account has one configured. For a temporary URL, ALLOWED_HOSTS must permit the
generated host before startup; the separate `ngrok http 8000` flow above is the
simplest first-phone path. Never commit a domain or credential. The script
prints the public URL after verifying `/health`.

## 11. Smoke Test Checklist

After DevTools login, and separately on a phone when required, verify:

- [ ] GET /health returns status: ok.
- [ ] wx.login succeeds and the backend receives /api/auth/wechat-login.
- [ ] Create a card and refresh; the card remains present.
- [ ] Direct AI analysis succeeds.
- [ ] Streaming analysis shows progress and a final result replaces it.
- [ ] Both Piper voices return playable WAV audio.
- [ ] A review session starts and accepts feedback.
- [ ] Review history opens.

If one check fails, see [troubleshooting.md](troubleshooting.md) before changing
resource limits or credentials.

## 12. Run Tests

The backend suite creates and drops the isolated
english_analyzer_phase1_pytest database; it does not use the application
database.

    Set-Location C:\Projects\English-analyzer-backend
    .\scripts\run-postgresql-tests.ps1

Success: pytest exits with code 0 and the script removes its test database in
its finally block. Run it only against a PostgreSQL server where your role can
create and drop that test database.

The Mini Program repository has a Node built-in test and no package dependency
install is required:

    Set-Location C:\Projects\English-study-miniapp
    node --test .\tests\stream-provisional.test.js

Success: Node reports all subtests passing.

## 13. Stop Safely

For direct Uvicorn, press Ctrl+C in its terminal. For a temporary ngrok tunnel,
press Ctrl+C in its terminal. PostgreSQL and Ollama are installed services and
remain available for later use.

If you used start-server.ps1, stop only project-owned FastAPI and ngrok
processes with:

    .\stop-server.ps1

It intentionally leaves PostgreSQL and Ollama running.

## Reproducibility Status

The setup path downloads ECDICT from a pinned public upstream, keeps ngrok
optional for local work, and creates the Mini Program backend URL from a tracked
safe template. No author-machine asset, tunnel domain, or local configuration is
required for the base Windows setup.

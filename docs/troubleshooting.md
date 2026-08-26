# Troubleshooting

Use the first matching section. Do not paste secrets, database passwords, or
AppSecrets into logs or issues.

## Python or virtual environment

**Symptom:** python is not recognized, activation is blocked, or pip installs
to the wrong Python.

**Check:** Run python --version and .\.venv\Scripts\python.exe --version.

**Fix:** Install Python 3.12 with PATH enabled. For a one-window PowerShell
activation restriction, run Set-ExecutionPolicy -Scope Process Bypass, then
.\.venv\Scripts\Activate.ps1. When unsure, install with:
.\.venv\Scripts\python.exe -m pip install -r requirements.txt.

## PostgreSQL will not connect

**Symptom:** DATABASE_URL is required, connection refused, database does not
exist, or startup reports PostgreSQL unhealthy.

**Check:** Get-Service -Name 'postgresql*' and:
psql -U english_app -d english_analyzer -h 127.0.0.1 -c "SELECT 1;"

**Fix:** Start the PostgreSQL service, create the role/database in the setup
guide, and compare .env DATABASE_URL, EXPECTED_DATABASE_NAME, and
EXPECTED_DATABASE_SCHEMA with the actual database. URL-encode reserved
characters in a password, or use a password without URL-reserved characters.

## Alembic revision mismatch

**Symptom:** check_database_target.py rejects the target or current differs from
alembic heads.

**Check:**

    .\.venv\Scripts\alembic.exe current
    .\.venv\Scripts\alembic.exe heads

**Fix:** Confirm DATABASE_URL targets the intended development database, then
run .\.venv\Scripts\alembic.exe upgrade head. Do not use a production database
as a shortcut. Read docs/release-runbook.md for release migrations.

## start-server.ps1 refuses to start

**Symptom:** configuration/database preflight fails, ngrok is missing, or public
health fails.

**Check:** Run scripts\check_config.py and scripts\check_database_target.py with
.\.venv\Scripts\python.exe. Check ngrok version if using the script.

**Fix:** Correct .env first. For initial local DevTools work, use direct Uvicorn
from the setup guide. start-server.ps1 additionally requires a reserved fixed
ngrok domain and is not the temporary-tunnel command.

## Port 8000 is already in use

**Symptom:** Uvicorn cannot bind to port 8000.

**Check:** Get-NetTCPConnection -LocalPort 8000 -State Listen.

**Fix:** Stop only a process you recognize as your prior backend, or choose
another port and update BACKEND_BASE_URL. Do not terminate unrelated listeners.

## Ollama or qwen3:8b is unavailable

**Symptom:** AI requests fail, time out, or report a missing model.

**Check:**

    ollama list
    Invoke-RestMethod http://127.0.0.1:11434/api/tags

**Fix:** Start the Ollama desktop application or run ollama serve, then run
ollama pull qwen3:8b. Run scripts\setup-local-ai.ps1 to validate Ollama and
Argos. The first generation can be slow while the model loads.

## Argos translation is unavailable

**Symptom:** logs mention an Argos package or en-to-zh model missing.

**Check and fix:** run scripts\setup-local-ai.ps1 with the virtual environment
available. It installs and tests the English-to-Chinese model.

## Piper TTS is unavailable or audio will not play

**Symptom:** the pronunciation endpoint returns an error, startup says Piper
warmup failed, or pronunciationAvailable is false.

**Check:** run the two-voice pronunciation_available command in the setup guide
and inspect data\piper.

**Fix:** Install Python dependencies, then place the exact .onnx and .onnx.json
pair for both configured voices in PIPER_DATA_DIR. Confirm names match
PIPER_MALE_VOICE and PIPER_FEMALE_VOICE. If the API succeeds but playback fails,
check the Mini Program download/network error first.

## WeChat DevTools cannot reach the backend

**Symptom:** request failure, timeout, or connection refused in the simulator.

**Check:** Open http://127.0.0.1:8000/health from Windows and inspect
utils\localBackendConfig.js.

**Fix:** set BACKEND_BASE_URL to http://127.0.0.1:8000, compile again, and
enable the local DevTools debug option that skips valid-domain/TLS/HTTPS checks.
That option is for development only.

## Phone cannot reach localhost or 127.0.0.1

**Symptom:** DevTools works but a phone preview cannot load backend data.

**Check:** confirm BACKEND_BASE_URL does not still contain 127.0.0.1.

**Fix:** start ngrok http 8000, put its HTTPS URL in localBackendConfig.js, add
its host to backend ALLOWED_HOSTS, and restart the backend. Configure the
appropriate legal domain in the Mini Program settings as required for phone
requests. A new temporary ngrok URL needs the same update.

## wx.login or code-to-session fails

**Symptom:** /api/auth/wechat-login returns an error or no backend token is
stored.

**Check:** verify the Mini Program AppID matches WECHAT_APPID and WECHAT_SECRET
belongs to that same AppID. Check backend logs without copying the secret.

**Fix:** replace placeholders with your real credentials, restart the backend,
and compile the Mini Program with that AppID. There is intentionally no
mock-login or test-JWT substitute for this flow.

## Tests fail before running

**Symptom:** backend tests refuse the database, or Node cannot run the frontend
test.

**Check:** run backend tests from English-analyzer-backend, not its parent
workspace. Ensure DATABASE_URL is set and the PostgreSQL role may create/drop
english_analyzer_phase1_pytest. Run node --version for the frontend test.

**Fix:** correct PostgreSQL role/connection, then run:
.\scripts\run-postgresql-tests.ps1. Install Node.js LTS and run:
node --test .\tests\stream-provisional.test.js from the Mini Program repository.

## ECDICT phonetic data is empty

**Symptom:** lexical information has no ECDICT-backed phonetic or dictionary
entry while other functions work.

**Check:** look for data\ecdict\ecdict.db or the configured ECDICT_DB_PATH.

**Fix:** this is a known reproducibility gap. The repository has no downloader
or canonical public ECDICT source. Keep this feature optional until a documented
source and setup command are added.

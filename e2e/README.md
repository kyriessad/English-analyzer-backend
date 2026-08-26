# Level 7 E2E

This directory contains the isolated real-dependency E2E runner. It does not
modify the formal `.env`, formal database, port 8000, formal TTS cache, or real
users.

Run the release-gate smoke test from PowerShell:

```powershell
.\scripts\run-level7-e2e.ps1
```

The default stops after the real single-user chain. Extended experiments remain
explicit and are not part of the release smoke command:

```powershell
  .\scripts\run-level7-e2e.ps1 -Through all
```

Every run creates `.e2e-artifacts/<run-id>/`. The runner always attempts, in a
`finally` block, to stop its exact PIDs, verify and close only matching E2E
listeners on 18000/18114, drop only `english_analyzer_phase1_e2e`, and remove
isolated model links/TTS cache. Artifacts and logs are retained on both PASS and
FAIL. The command prints the artifact, Uvicorn log, result, and report paths.

Authentication is intentionally reported as `PARTIAL`: users and signed JWTs
are bootstrapped only in the disposable database, while bearer verification and
logout revocation use the real backend. No WeChat API is mocked.

# Scenario material pipeline

Run from the backend repository:

```powershell
$env:PYTHONPATH='.'
.\.venv\Scripts\python.exe scripts\produce_scenario_materials.py --dry-run --refresh-source
.\.venv\Scripts\python.exe scripts\produce_scenario_materials.py --offline
```

The first command downloads the SHA-256-pinned English-Mandarin Tatoeba selection from ManyThings and
the official Tatoeba CC0 English export. It cleans and globally deduplicates both sources, assigns scene
categories, audits boundary items with local Ollama, translates only the selected CC0 gap-fill rows with
Tencent TMT, and writes the AI outputs plus `data/scenario-material-cache/production-report.json`.
The second command repeats the same production from the pinned sources and cached AI decisions, then imports it through
`import_public_materials(...)`.

Every imported bilingual row retains both Tatoeba sentence IDs and contributor names in `source_id`, uses
`source=tatoeba`, and records `license=CC BY 2.0 FR`. Translated gap-fill rows retain the original
Tatoeba sentence ID as `source_id=cc0:<id>`, use `source=tatoeba-cc0-ai-translation`, and record
`license=CC0 1.0` plus the translation provider in `review_note`. No AI-original row is included.

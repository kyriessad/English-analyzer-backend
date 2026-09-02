# Exam word-book production

The production entry point is:

```powershell
$env:PYTHONPATH='.'
.\.venv\Scripts\python.exe scripts\produce_exam_wordbooks.py --dry-run --refresh-sources
.\.venv\Scripts\python.exe scripts\produce_exam_wordbooks.py --offline
.\.venv\Scripts\python.exe scripts\produce_exam_wordbooks.py --book postgraduate --offline
```

The first command refreshes remote sources, builds all five books, validates them, and writes
`data/exam-wordbook-cache/production-report.json` without changing PostgreSQL. The second command
reuses the validated cache and imports through `import_public_materials(...)`. Repeating the import
is idempotent. `seed_discovery_content.py` preserves packs whose version starts with
`exam-wordbooks-`, so the earlier 500-word demo seed cannot replace production books.

Source definitions and scope limitations are recorded in
`app/data/exam_wordbook_sources.json`. Raw community papers and copyrighted official samples are
cached locally and are not redistributed by this repository. PostgreSQL stores the candidate source,
source identifier, candidate license, corpus frequency/rank, production batch, and a compact review
note. The full corpus provenance remains in the manifest and generated report.

Current matching is deterministic lowercase exact-token matching without lemmatization. The
postgraduate book uses the ECDICT `ky` tag and locally counts its pinned English I/English II corpus;
no third-party precomputed frequency table is used. Adding more legally usable corpus documents only
requires extending the manifest and rerunning the same command.

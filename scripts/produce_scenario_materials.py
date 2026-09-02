"""Build, audit, and optionally import the public scenario material packs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from app.services.public_material_importer import import_public_materials
from app.services.scenario_material_pipeline import DEFAULT_CACHE_DIR, produce_scenario_materials


DEFAULT_REPORT_PATH = DEFAULT_CACHE_DIR / "production-report.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="produce and validate without importing")
    parser.add_argument("--offline", action="store_true", help="require the source and AI review cache")
    parser.add_argument("--refresh-source", action="store_true", help="redownload the pinned source archive")
    parser.add_argument("--skip-ai-review", action="store_true", help="run deterministic stages only")
    parser.add_argument("--skip-ai-translation", action="store_true", help="do not add translated CC0 English rows")
    parser.add_argument("--skip-chinese-review", action="store_true", help="skip final Qwen Chinese quality review")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    args = parser.parse_args()
    if args.offline and args.refresh_source:
        parser.error("--offline and --refresh-source cannot be used together")

    produced = produce_scenario_materials(
        refresh_source=args.refresh_source,
        offline=args.offline,
        ai_review=not args.skip_ai_review,
        ai_translate=not args.skip_ai_translation,
        chinese_review=not args.skip_chinese_review,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(produced.report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    counts = {code: len(items) for code, items in produced.items_by_pack.items()}
    if not args.dry_run:
        with SessionLocal() as db:
            counts = import_public_materials(
                db,
                packs=produced.packs,
                items_by_pack=produced.items_by_pack,
            )
            db.commit()

    action = "VALIDATED" if args.dry_run else "IMPORTED"
    print(
        f"SCENARIO MATERIALS {action} {produced.production_batch} "
        + " ".join(f"{code}={count}" for code, count in counts.items())
    )
    print(f"REPORT {args.report.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

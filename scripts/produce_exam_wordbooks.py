"""Build and optionally import the five public exam word books."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from app.services.exam_wordbook_pipeline import DEFAULT_CACHE_DIR, produce_exam_wordbooks
from app.services.public_material_importer import import_public_materials


DEFAULT_REPORT_PATH = DEFAULT_CACHE_DIR / "production-report.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="build and validate without importing")
    parser.add_argument("--offline", action="store_true", help="require all remote sources to be cached")
    parser.add_argument("--refresh-sources", action="store_true", help="redownload remote sources")
    parser.add_argument(
        "--book",
        action="append",
        choices=("cet4", "cet6", "postgraduate", "ielts", "toefl"),
        help="produce only the selected book; may be repeated",
    )
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    args = parser.parse_args()
    if args.offline and args.refresh_sources:
        parser.error("--offline and --refresh-sources cannot be used together")

    produced = produce_exam_wordbooks(
        refresh_sources=args.refresh_sources,
        offline=args.offline,
        book_codes=set(args.book) if args.book else None,
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
        f"EXAM WORDBOOKS {action} {produced.production_batch} "
        + " ".join(f"{code}={count}" for code, count in counts.items())
    )
    print(f"REPORT {args.report.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

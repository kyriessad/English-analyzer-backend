from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal
from app.models.card import Card, CardLexicalMetadata
from app.services.lexical_metadata import MAX_REVIEW_CONTENT_LENGTH, METADATA_VERSION, upsert_card_lexical_metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill card_lexical_metadata in small idempotent batches.")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--after-card-id", type=UUID, default=None)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    processed = 0
    updated = 0
    with SessionLocal() as db:
        filters = [
            Card.deleted_at.is_(None),
            Card.status == "active",
            func.length(func.trim(Card.content_normalized)) > 0,
            func.length(func.trim(Card.content)) <= MAX_REVIEW_CONTENT_LENGTH,
        ]
        if args.after_card_id:
            filters.append(Card.id > args.after_card_id)

        cards = list(
            db.scalars(
                select(Card)
                .outerjoin(CardLexicalMetadata, CardLexicalMetadata.card_id == Card.id)
                .where(
                    *filters,
                    (
                        (CardLexicalMetadata.card_id.is_(None))
                        | (CardLexicalMetadata.content_normalized != Card.content_normalized)
                        | (CardLexicalMetadata.metadata_version != METADATA_VERSION)
                    ),
                )
                .order_by(Card.id)
                .limit(max(args.batch_size, 1))
            )
        )

        for card in cards:
            processed += 1
            if not args.dry_run:
                upsert_card_lexical_metadata(db, card)
                updated += 1

        if not args.dry_run:
            db.commit()

    print(f"card_lexical_metadata_backfill processed={processed} updated={updated} dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

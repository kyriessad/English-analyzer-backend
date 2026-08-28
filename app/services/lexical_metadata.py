from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.models.card import Card, CardLexicalMetadata
from app.models.user import utc_now
from app.services.ecdict_service import get_dictionary_entry


logger = logging.getLogger(__name__)
METADATA_VERSION = 1
MAX_REVIEW_CONTENT_LENGTH = 80


def should_have_lexical_metadata(card: Card) -> bool:
    return bool(card.content and len(card.content.strip()) <= MAX_REVIEW_CONTENT_LENGTH)


def upsert_card_lexical_metadata(db: Session, card: Card) -> CardLexicalMetadata | None:
    if not should_have_lexical_metadata(card):
        existing = db.get(CardLexicalMetadata, card.id)
        if existing is not None:
            db.delete(existing)
        return None

    entry = get_dictionary_entry(card.content_normalized or card.content)
    now = utc_now()
    metadata = db.get(CardLexicalMetadata, card.id)
    if metadata is None:
        metadata = CardLexicalMetadata(card_id=card.id, content_normalized=card.content_normalized)
        db.add(metadata)

    metadata.content_normalized = card.content_normalized
    metadata.edict_hit = entry is not None
    metadata.pos = entry.pos if entry else None
    metadata.frq = entry.frq if entry else None
    metadata.bnc = entry.bnc if entry else None
    metadata.metadata_version = METADATA_VERSION
    metadata.updated_at = now
    return metadata


def upsert_card_lexical_metadata_best_effort(db: Session, card: Card) -> None:
    try:
        upsert_card_lexical_metadata(db, card)
    except Exception:
        logger.exception("card_lexical_metadata_upsert_failed", extra={"card_id": str(card.id)})

"""Import versioned public discovery content without creating user Cards."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID, uuid5

from sqlalchemy import select, update

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data.discovery_content import CONTENT_VERSION, PACKS, STATIC_ENTRIES, WORD_BOOK_TAGS, daily_quotes
from app.database import SessionLocal
from app.models.discovery import PublicMaterialItem, PublicMaterialPack
from app.services.card_service import normalize_card_content
from app.services.ecdict_service import get_tagged_dictionary_entries
from app.services.validator import validate_english


CONTENT_NAMESPACE = UUID("c9ba9de8-3b40-4b36-a67d-6b3fa128988e")
BANNED_QUOTE_PHRASES = (
    "never give up",
    "everything happens for a reason",
    "believe in yourself",
    "dream big",
    "you can do anything",
    "stay positive",
)


def stable_id(kind: str, value: str) -> UUID:
    return uuid5(CONTENT_NAMESPACE, f"{kind}:{value}")


def validate_editorial_content(*, audit_runtime: bool) -> dict[str, tuple[tuple[str, str, str], ...]]:
    content = dict(STATIC_ENTRIES)
    content["daily-quote"] = daily_quotes()
    if len(content["daily-quote"]) != 365:
        raise RuntimeError("daily quote source must contain exactly 365 entries")

    for pack_code, entries in content.items():
        seen: set[str] = set()
        for english, chinese, card_type in entries:
            normalized = normalize_card_content(english)
            if not normalized or normalized in seen:
                raise RuntimeError(f"duplicate or empty content in {pack_code}: {english}")
            if not str(chinese).strip() or card_type not in {"phrase", "sentence"}:
                raise RuntimeError(f"invalid editorial entry in {pack_code}: {english}")
            if pack_code == "daily-quote" and any(phrase in normalized for phrase in BANNED_QUOTE_PHRASES):
                raise RuntimeError(f"daily quote contains banned cliche: {english}")
            if audit_runtime:
                validation = validate_english(english, requested_category=card_type)
                # Category mismatch is an existing non-blocking product warning.
                # Editorial content follows the pack's intended Card type while
                # hard-rule rejection remains forbidden.
                if validation["level"] == "error":
                    raise RuntimeError(f"English audit failed for {pack_code}: {english}: {validation}")
            seen.add(normalized)
    return content


def build_import_content(*, word_limit: int, audit_runtime: bool) -> dict[str, tuple[tuple[str, str, str], ...]]:
    content = validate_editorial_content(audit_runtime=audit_runtime)
    for pack_code, tag in WORD_BOOK_TAGS.items():
        entries = get_tagged_dictionary_entries(tag, limit=word_limit)
        if len(entries) < word_limit:
            raise RuntimeError(
                f"ECDICT tag {tag!r} returned {len(entries)} words; rebuild the local ECDICT "
                "with scripts/setup-ecdict.ps1 -Force so tag/frq/bnc/pos columns are available"
            )
        content[pack_code] = tuple((entry.word, entry.meanings[0], "word") for entry in entries)
    return content


def seed_discovery_content(*, word_limit: int = 500, audit_runtime: bool = False) -> dict[str, int]:
    content_by_pack = build_import_content(word_limit=word_limit, audit_runtime=audit_runtime)
    pack_definitions = {item[0]: item for item in PACKS}
    counts: dict[str, int] = {}

    with SessionLocal() as db:
        for code, title, description, kind, sort_order in PACKS:
            pack = db.scalar(select(PublicMaterialPack).where(PublicMaterialPack.code == code))
            if pack is None:
                pack = PublicMaterialPack(id=stable_id("pack", code), code=code)
                db.add(pack)
            pack.title = title
            pack.description = description
            pack.kind = kind
            pack.sort_order = sort_order
            pack.status = "active"
            pack.content_version = CONTENT_VERSION
        db.flush()

        for pack_code, entries in content_by_pack.items():
            pack = db.scalar(select(PublicMaterialPack).where(PublicMaterialPack.code == pack_code))
            if pack is None:
                raise RuntimeError(f"pack definition missing: {pack_code}")
            db.execute(update(PublicMaterialItem).where(PublicMaterialItem.pack_id == pack.id).values(
                position=PublicMaterialItem.position + 1_000_000,
                status="hidden",
            ))
            active_ids: set[UUID] = set()
            for position, (english, chinese, card_type) in enumerate(entries, start=1):
                normalized = normalize_card_content(english)
                item_id = stable_id("item", f"{pack_code}:{normalized}")
                item = db.get(PublicMaterialItem, item_id)
                if item is None:
                    item = PublicMaterialItem(id=item_id, pack_id=pack.id)
                    db.add(item)
                item.content = english.strip()
                item.content_normalized = normalized
                item.chinese = chinese.strip()
                item.card_type = card_type
                item.source_label = "今日一句" if pack_code == "daily-quote" else pack_definitions[pack_code][1]
                item.position = position
                item.status = "approved"
                item.review_note = (
                    f"ECDICT {WORD_BOOK_TAGS[pack_code]} {CONTENT_VERSION}"
                    if pack_code in WORD_BOOK_TAGS
                    else f"editorial review {CONTENT_VERSION}"
                )
                active_ids.add(item_id)
            counts[pack_code] = len(active_ids)
        db.commit()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--word-limit", type=int, default=500)
    parser.add_argument("--audit-content", action="store_true")
    args = parser.parse_args()
    if args.word_limit < 50 or args.word_limit > 2000:
        parser.error("--word-limit must be between 50 and 2000")
    counts = seed_discovery_content(word_limit=args.word_limit, audit_runtime=args.audit_content)
    print("DISCOVERY CONTENT READY", CONTENT_VERSION, " ".join(f"{key}={value}" for key, value in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

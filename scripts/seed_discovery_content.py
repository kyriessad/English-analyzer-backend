"""Import versioned public discovery content without creating user Cards."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.data.discovery_content import CONTENT_VERSION, PACKS, STATIC_ENTRIES, WORD_BOOK_TAGS, daily_quotes
from app.database import SessionLocal
from app.models.discovery import PublicMaterialPack
from app.services.card_service import normalize_card_content
from app.services.ecdict_service import get_tagged_dictionary_entries
from app.services.public_material_importer import (
    PublicMaterialItemImport,
    PublicMaterialPackImport,
    import_public_materials,
    stable_public_material_id,
)
from app.services.validator import validate_english


BANNED_QUOTE_PHRASES = (
    "never give up",
    "everything happens for a reason",
    "believe in yourself",
    "dream big",
    "you can do anything",
    "stay positive",
)


def stable_id(kind: str, value: str) -> UUID:
    return stable_public_material_id(kind, value)


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


def without_protected_word_books(
    pack_imports: list[PublicMaterialPackImport],
    items_by_pack: dict[str, list[PublicMaterialItemImport]],
    protected_codes: set[str],
) -> tuple[list[PublicMaterialPackImport], dict[str, list[PublicMaterialItemImport]]]:
    return (
        [pack for pack in pack_imports if pack.code not in protected_codes],
        {
            code: items for code, items in items_by_pack.items()
            if code not in protected_codes
        },
    )


def without_protected_scenario_packs(
    pack_imports: list[PublicMaterialPackImport],
    items_by_pack: dict[str, list[PublicMaterialItemImport]],
    protected_codes: set[str],
) -> tuple[list[PublicMaterialPackImport], dict[str, list[PublicMaterialItemImport]]]:
    return without_protected_word_books(pack_imports, items_by_pack, protected_codes)


def seed_discovery_content(*, word_limit: int = 500, audit_runtime: bool = False) -> dict[str, int]:
    content_by_pack = build_import_content(word_limit=word_limit, audit_runtime=audit_runtime)
    pack_definitions = {item[0]: item for item in PACKS}
    pack_imports = [
        PublicMaterialPackImport(
            code=code,
            title=title,
            description=description,
            kind=kind,
            sort_order=sort_order,
            content_version=CONTENT_VERSION,
        )
        for code, title, description, kind, sort_order in PACKS
    ]
    items_by_pack: dict[str, list[PublicMaterialItemImport]] = {}

    for pack_code, entries in content_by_pack.items():
        _, pack_title, _, _, _ = pack_definitions[pack_code]
        source_label = "今日一句" if pack_code == "daily-quote" else pack_title
        item_imports: list[PublicMaterialItemImport] = []
        for position, (english, chinese, card_type) in enumerate(entries, start=1):
            if pack_code in WORD_BOOK_TAGS:
                source = "ecdict"
                source_id = normalize_card_content(english)
                review_note = f"ECDICT {WORD_BOOK_TAGS[pack_code]} {CONTENT_VERSION}"
                corpus_rank = position
            else:
                source = "internal-editorial"
                source_id = f"{pack_code}:{position}"
                review_note = f"editorial review {CONTENT_VERSION}"
                corpus_rank = None
            item_imports.append(PublicMaterialItemImport(
                content=english,
                chinese=chinese,
                card_type=card_type,
                source_label=source_label,
                source=source,
                source_id=source_id,
                license=None,
                corpus_rank=corpus_rank,
                production_batch=CONTENT_VERSION,
                review_note=review_note,
            ))
        items_by_pack[pack_code] = item_imports

    with SessionLocal() as db:
        protected_word_books = set(db.scalars(select(PublicMaterialPack.code).where(
            PublicMaterialPack.kind == "word_book",
            PublicMaterialPack.content_version.like("exam-wordbooks-%"),
        )))
        pack_imports, items_by_pack = without_protected_word_books(
            pack_imports, items_by_pack, protected_word_books
        )
        protected_scenario_packs = set(db.scalars(select(PublicMaterialPack.code).where(
            PublicMaterialPack.kind == "expression",
            PublicMaterialPack.content_version.like("scenario-materials-%"),
        )))
        pack_imports, items_by_pack = without_protected_scenario_packs(
            pack_imports, items_by_pack, protected_scenario_packs
        )
        counts = import_public_materials(db, packs=pack_imports, items_by_pack=items_by_pack)
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

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid5

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.discovery import PublicMaterialItem, PublicMaterialPack
from app.services.card_service import normalize_card_content


CONTENT_NAMESPACE = UUID("c9ba9de8-3b40-4b36-a67d-6b3fa128988e")


def stable_public_material_id(kind: str, value: str) -> UUID:
    return uuid5(CONTENT_NAMESPACE, f"{kind}:{value}")


@dataclass(frozen=True)
class PublicMaterialPackImport:
    code: str
    title: str
    description: str
    kind: str
    sort_order: int
    content_version: str
    status: str = "active"


@dataclass(frozen=True)
class PublicMaterialItemImport:
    content: str
    chinese: str
    card_type: str
    source_label: str
    source: str | None = None
    source_id: str | None = None
    license: str | None = None
    corpus_rank: int | None = None
    corpus_frequency: float | None = None
    production_batch: str | None = None
    status: str = "approved"
    review_note: str | None = None


def _clean_optional(value: str | None) -> str | None:
    cleaned = str(value).strip() if value is not None else ""
    return cleaned or None


def _validate_pack(pack: PublicMaterialPackImport) -> None:
    if not pack.code.strip() or not pack.title.strip():
        raise ValueError("public material pack code and title are required")
    if pack.kind not in {"word_book", "expression", "daily_quote"}:
        raise ValueError(f"unsupported public material pack kind: {pack.kind}")
    if pack.status not in {"active", "hidden"}:
        raise ValueError(f"unsupported public material pack status: {pack.status}")


def _validate_item(item: PublicMaterialItemImport) -> tuple[str, str]:
    content = item.content.strip()
    chinese = item.chinese.strip()
    if not content or not chinese:
        raise ValueError("public material item content and chinese are required")
    if item.card_type not in {"word", "phrase", "sentence"}:
        raise ValueError(f"unsupported public material card_type: {item.card_type}")
    if item.status not in {"approved", "hidden"}:
        raise ValueError(f"unsupported public material item status: {item.status}")
    normalized = normalize_card_content(content)
    if not normalized:
        raise ValueError("public material item normalized content is empty")
    if item.corpus_rank is not None and item.corpus_rank < 1:
        raise ValueError("corpus_rank must be positive when provided")
    if item.corpus_frequency is not None and item.corpus_frequency < 0:
        raise ValueError("corpus_frequency cannot be negative")
    return content, normalized


def import_public_materials(
    db: Session,
    *,
    packs: list[PublicMaterialPackImport],
    items_by_pack: dict[str, list[PublicMaterialItemImport]],
) -> dict[str, int]:
    """Idempotently replace the active item set for each supplied pack."""
    pack_by_code: dict[str, PublicMaterialPack] = {}
    for pack_spec in packs:
        _validate_pack(pack_spec)
        code = pack_spec.code.strip()
        pack = db.scalar(select(PublicMaterialPack).where(PublicMaterialPack.code == code))
        if pack is None:
            pack = PublicMaterialPack(id=stable_public_material_id("pack", code), code=code)
            db.add(pack)
        pack.title = pack_spec.title.strip()
        pack.description = pack_spec.description.strip()
        pack.kind = pack_spec.kind
        pack.sort_order = pack_spec.sort_order
        pack.status = pack_spec.status
        pack.content_version = pack_spec.content_version.strip()
        pack_by_code[code] = pack
    db.flush()

    counts: dict[str, int] = {}
    for pack_code, item_specs in items_by_pack.items():
        pack = pack_by_code.get(pack_code) or db.scalar(
            select(PublicMaterialPack).where(PublicMaterialPack.code == pack_code)
        )
        if pack is None:
            raise ValueError(f"pack definition missing: {pack_code}")

        db.execute(update(PublicMaterialItem).where(PublicMaterialItem.pack_id == pack.id).values(
            position=PublicMaterialItem.position + 1_000_000,
            status="hidden",
        ))

        seen_normalized: set[str] = set()
        active_ids: set[UUID] = set()
        for position, item_spec in enumerate(item_specs, start=1):
            content, normalized = _validate_item(item_spec)
            if normalized in seen_normalized:
                raise ValueError(f"duplicate content in {pack_code}: {content}")
            seen_normalized.add(normalized)

            item_id = stable_public_material_id("item", f"{pack_code}:{normalized}")
            item = db.get(PublicMaterialItem, item_id)
            if item is None:
                item = PublicMaterialItem(id=item_id, pack_id=pack.id)
                db.add(item)
            item.pack_id = pack.id
            item.content = content
            item.content_normalized = normalized
            item.chinese = item_spec.chinese.strip()
            item.card_type = item_spec.card_type
            item.source_label = item_spec.source_label.strip()
            item.source = _clean_optional(item_spec.source)
            item.source_id = _clean_optional(item_spec.source_id)
            item.license = _clean_optional(item_spec.license)
            item.corpus_rank = item_spec.corpus_rank
            item.corpus_frequency = item_spec.corpus_frequency
            item.production_batch = _clean_optional(item_spec.production_batch)
            item.position = position
            item.status = item_spec.status
            item.review_note = _clean_optional(item_spec.review_note)
            active_ids.add(item_id)
        counts[pack_code] = len(active_ids)
    return counts

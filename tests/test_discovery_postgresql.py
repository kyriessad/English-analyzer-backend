import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.database import SessionLocal
from app.models.card import Card
from app.models.discovery import PublicMaterialItem, PublicMaterialPack, UserMaterialState
from app.models.user import User, utc_now
from app.schemas.card import CardCreate
from app.services.card_service import create_card_in_transaction
from app.services.discovery_service import get_today_quote, list_material_items, set_material_known
from app.services.public_material_importer import PublicMaterialItemImport, PublicMaterialPackImport, import_public_materials
from app.services.review_v1 import apply_fsrs_review, get_or_create_fsrs_state, is_v1_review_eligible


pytestmark = pytest.mark.skipif(
    not os.environ.get("POSTGRES_TEST_DATABASE_URL"),
    reason="requires the isolated PostgreSQL test database",
)


def test_postgresql_discovery_isolation_concurrency_card_and_fsrs_reuse():
    suffix = uuid4().hex[:10]
    user_id = uuid4()
    other_user_id = uuid4()
    pack_id = uuid4()
    item_id = uuid4()
    with SessionLocal() as db:
        db.add_all([
            User(id=user_id, wx_openid=f"discovery-pg-{suffix}"),
            User(id=other_user_id, wx_openid=f"discovery-pg-other-{suffix}"),
            PublicMaterialPack(
                id=pack_id, code=f"discovery-pg-{suffix}", title="测试表达", description="隔离测试",
                kind="expression", sort_order=1, status="active", content_version="test",
            ),
        ])
        db.flush()
        db.add_all([
            PublicMaterialItem(
                id=item_id, pack_id=pack_id, content="Take your time.",
                content_normalized="take your time.", chinese="慢慢来。", card_type="sentence",
                source_label="测试表达", position=1, status="approved",
            ),
        ])
        db.commit()

    def mark_known() -> bool:
        with SessionLocal() as db:
            return set_material_known(db, db.get(User, user_id), item_id, True)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            assert list(executor.map(lambda _: mark_known(), range(2))) == [True, True]

        with SessionLocal() as db:
            assert db.scalar(select(func.count()).select_from(UserMaterialState).where(
                UserMaterialState.user_id == user_id,
                UserMaterialState.material_item_id == item_id,
            )) == 1
            own_items, own_total = list_material_items(
                db, db.get(User, user_id), pack_code=f"discovery-pg-{suffix}",
                limit=20, offset=0, include_known=False, query=None,
            )
            other_items, other_total = list_material_items(
                db, db.get(User, other_user_id), pack_code=f"discovery-pg-{suffix}",
                limit=20, offset=0, include_known=False, query=None,
            )
            assert own_items == [] and own_total == 0
            assert len(other_items) == 1 and other_total == 1
            assert db.scalar(select(func.count()).select_from(Card).where(Card.user_id == user_id)) == 0

            card = create_card_in_transaction(db, CardCreate(
                user_id=user_id,
                content="Take your time.",
                card_type="sentence",
                understanding="慢慢来。",
                where_encountered="发现素材 · 测试表达",
                local_temp_id=f"discovery-pg-{suffix}",
            ))
            db.commit()
            db.refresh(card)
            assert is_v1_review_eligible(card)
            state = get_or_create_fsrs_state(db, user_id, card.id, utc_now())
            result = apply_fsrs_review(
                db,
                user_id=user_id,
                card_id=card.id,
                is_correct=True,
                reviewed_at=utc_now(),
            )
            db.commit()
            assert result.state_after_json
            assert state.card_id == card.id

            _, quote = get_today_quote(db, db.get(User, user_id), today=date(2026, 9, 1))
            assert quote.source_label == "今日一句"
            assert quote.content and quote.chinese
    finally:
        with SessionLocal() as db:
            db.query(UserMaterialState).filter(UserMaterialState.user_id.in_([user_id, other_user_id])).delete(synchronize_session=False)
            db.query(Card).filter(Card.user_id.in_([user_id, other_user_id])).delete(synchronize_session=False)
            db.query(PublicMaterialPack).filter(PublicMaterialPack.id == pack_id).delete(synchronize_session=False)
            db.query(User).filter(User.id.in_([user_id, other_user_id])).delete(synchronize_session=False)
            db.commit()


def test_postgresql_public_material_importer_is_idempotent_and_persists_trace_fields():
    suffix = uuid4().hex[:10]
    pack_code = f"discovery-import-{suffix}"
    pack = PublicMaterialPackImport(
        code=pack_code,
        title="测试导入词书",
        description="真实 PostgreSQL 导入验收",
        kind="word_book",
        sort_order=123,
        content_version="test-v1",
    )
    initial_items = [
        PublicMaterialItemImport(
            content="analyze",
            chinese="分析",
            card_type="word",
            source_label="测试导入词书",
            source="exam-corpus-test",
            source_id="exam-corpus-test:analyze",
            license="test-fixture-license",
            corpus_rank=1,
            corpus_frequency=42.5,
            production_batch="test-batch-1",
            review_note="postgres importer test",
        ),
        PublicMaterialItemImport(
            content="context",
            chinese="语境",
            card_type="word",
            source_label="测试导入词书",
            source="exam-corpus-test",
            source_id="exam-corpus-test:context",
            license="test-fixture-license",
            corpus_rank=2,
            corpus_frequency=21.0,
            production_batch="test-batch-1",
        ),
    ]
    updated_items = [
        PublicMaterialItemImport(
            content="analyze",
            chinese="分析；解析",
            card_type="word",
            source_label="测试导入词书",
            source="exam-corpus-test",
            source_id="exam-corpus-test:analyze-v2",
            license="test-fixture-license",
            corpus_rank=1,
            corpus_frequency=50.0,
            production_batch="test-batch-2",
            review_note="postgres importer test updated",
        ),
    ]

    try:
        with SessionLocal() as db:
            assert import_public_materials(db, packs=[pack], items_by_pack={pack_code: initial_items}) == {pack_code: 2}
            db.commit()
            assert import_public_materials(db, packs=[pack], items_by_pack={pack_code: updated_items}) == {pack_code: 1}
            assert import_public_materials(db, packs=[pack], items_by_pack={pack_code: updated_items}) == {pack_code: 1}
            db.commit()

            pack_id = db.scalar(select(PublicMaterialPack.id).where(PublicMaterialPack.code == pack_code))
            assert pack_id is not None
            approved = list(db.scalars(select(PublicMaterialItem).where(
                PublicMaterialItem.pack_id == pack_id,
                PublicMaterialItem.status == "approved",
            )))
            hidden = list(db.scalars(select(PublicMaterialItem).where(
                PublicMaterialItem.pack_id == pack_id,
                PublicMaterialItem.status == "hidden",
            )))
            assert len(approved) == 1
            assert len(hidden) == 1
            item = approved[0]
            assert item.content == "analyze"
            assert item.chinese == "分析；解析"
            assert item.source == "exam-corpus-test"
            assert item.source_id == "exam-corpus-test:analyze-v2"
            assert item.license == "test-fixture-license"
            assert item.corpus_rank == 1
            assert item.corpus_frequency == 50.0
            assert item.production_batch == "test-batch-2"
            assert item.position == 1
    finally:
        with SessionLocal() as db:
            db.query(PublicMaterialPack).filter(PublicMaterialPack.code == pack_code).delete(synchronize_session=False)
            db.commit()

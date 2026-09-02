from dataclasses import replace
from datetime import date
from uuid import UUID, uuid4
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.core.config import settings as app_settings
import app.models
from app.data.discovery_content import CONTENT_VERSION
from app.database import Base, get_db
from app.main import app
from app.models.card import Card
from app.models.discovery import PublicMaterialItem, PublicMaterialPack, UserMaterialState
from app.models.user import User
from app.services import auth_service
from app.services.card_service import normalize_card_content
from app.services.public_material_importer import (
    PublicMaterialItemImport,
    PublicMaterialPackImport,
    import_public_materials,
)
from scripts.seed_discovery_content import BANNED_QUOTE_PHRASES, stable_id, validate_editorial_content


engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


class DiscoveryApiTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)
        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)
        self.original_settings = auth_service.settings
        auth_service.settings = replace(
            app_settings,
            jwt_secret_key="discovery-api-secret-with-at-least-32-bytes",
            jwt_algorithm="HS256",
            jwt_expire_days=3,
        )
        self.user_id = uuid4()
        self.other_user_id = uuid4()
        with TestingSessionLocal() as db:
            db.add_all([
                User(id=self.user_id, wx_openid=f"discovery-{self.user_id}"),
                User(id=self.other_user_id, wx_openid=f"discovery-{self.other_user_id}"),
            ])
            expression_pack = PublicMaterialPack(
                id=stable_id("pack", "daily-life"), code="daily-life", title="日常表达",
                description="日常", kind="expression", sort_order=1, status="active", content_version=CONTENT_VERSION,
            )
            quote_pack = PublicMaterialPack(
                id=stable_id("pack", "daily-quote"), code="daily-quote", title="今日一句",
                description="每日", kind="daily_quote", sort_order=999, status="active", content_version=CONTENT_VERSION,
            )
            db.add_all([expression_pack, quote_pack])
            db.flush()
            self.material_id = stable_id("item", "daily-life:take your time.")
            db.add_all([
                PublicMaterialItem(
                    id=self.material_id, pack_id=expression_pack.id, content="Take your time.",
                    content_normalized="take your time.", chinese="慢慢来。", card_type="sentence",
                    source_label="日常表达", position=1, status="approved",
                ),
                PublicMaterialItem(
                    id=stable_id("item", "daily-life:fair enough."), pack_id=expression_pack.id,
                    content="Fair enough.", content_normalized="fair enough.", chinese="有道理。",
                    card_type="sentence", source_label="日常表达", position=2, status="approved",
                ),
                PublicMaterialItem(
                    id=stable_id("item", "daily-quote:one"), pack_id=quote_pack.id,
                    content="A quiet morning can leave enough room for a better question.",
                    content_normalized="a quiet morning can leave enough room for a better question.",
                    chinese="清晨的宁静能为一个更好的问题留出空间。", card_type="sentence",
                    source_label="今日一句", position=1, status="approved",
                ),
                PublicMaterialItem(
                    id=stable_id("item", "daily-quote:two"), pack_id=quote_pack.id,
                    content="A path beyond view asks us to notice what is already beginning.",
                    content_normalized="a path beyond view asks us to notice what is already beginning.",
                    chinese="视线外的小路让我们留意已经开始的事。", card_type="sentence",
                    source_label="今日一句", position=2, status="approved",
                ),
            ])
            db.commit()
        self.token = auth_service.create_access_token(self.user_id)
        self.other_token = auth_service.create_access_token(self.other_user_id)

    def tearDown(self):
        auth_service.settings = self.original_settings
        app.dependency_overrides.pop(get_db, None)

    def headers(self, other=False):
        return {"Authorization": f"Bearer {self.other_token if other else self.token}"}

    def test_materials_are_not_cards_and_auth_is_required(self):
        self.assertEqual(401, self.client.get("/api/discovery/packs").status_code)
        response = self.client.get("/api/discovery/packs", headers=self.headers())
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(["daily-life"], [item["code"] for item in response.json()["items"]])
        self.assertEqual(2, response.json()["items"][0]["remaining_count"])
        with TestingSessionLocal() as db:
            self.assertEqual(0, db.scalar(select(func.count()).select_from(Card)))

    def test_known_state_is_idempotent_and_isolated(self):
        for _ in range(2):
            response = self.client.put(
                f"/api/discovery/items/{self.material_id}/state",
                headers=self.headers(), json={"known": True},
            )
            self.assertEqual(200, response.status_code, response.text)
            self.assertTrue(response.json()["known"])
        own = self.client.get("/api/discovery/items?pack=daily-life", headers=self.headers()).json()
        other = self.client.get("/api/discovery/items?pack=daily-life", headers=self.headers(other=True)).json()
        self.assertEqual(1, own["total"])
        self.assertEqual(2, other["total"])
        with TestingSessionLocal() as db:
            self.assertEqual(1, db.scalar(select(func.count()).select_from(UserMaterialState)))

    def test_existing_active_card_sets_in_library_without_changing_material(self):
        create = self.client.post("/api/cards", headers=self.headers(), json={
            "content": "Take your time.", "card_type": "sentence", "understanding": "慢慢来。",
            "where_encountered": "日常表达", "local_temp_id": "discovery-prefill-1",
        })
        self.assertEqual(200, create.status_code, create.text)
        response = self.client.get(
            "/api/discovery/items?pack=daily-life&include_known=true",
            headers=self.headers(),
        )
        item = next(item for item in response.json()["items"] if item["id"] == str(self.material_id))
        self.assertTrue(item["in_library"])
        with TestingSessionLocal() as db:
            self.assertEqual(2, db.scalar(select(func.count()).select_from(PublicMaterialItem).where(
                PublicMaterialItem.pack_id == stable_id("pack", "daily-life")
            )))

    def test_today_quote_is_deterministic_for_fixed_shanghai_date(self):
        with patch("app.services.discovery_service.discovery_local_date", return_value=date(2026, 9, 1)):
            first = self.client.get("/api/discovery/today-quote", headers=self.headers())
            second = self.client.get("/api/discovery/today-quote", headers=self.headers())
        self.assertEqual(200, first.status_code, first.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual("2026-09-01", first.json()["display_date"])
        self.assertEqual("Asia/Shanghai", first.json()["timezone"])
        self.assertEqual("今日一句", first.json()["item"]["source_label"])


class DiscoveryEditorialContentTest(unittest.TestCase):
    def test_365_quotes_are_unique_reviewable_and_avoid_banned_cliches(self):
        content = validate_editorial_content(audit_runtime=True)
        quotes = content["daily-quote"]
        self.assertEqual(365, len(quotes))
        self.assertEqual(365, len({normalize_card_content(item[0]) for item in quotes}))
        self.assertTrue(all(item[1].strip() and item[2] == "sentence" for item in quotes))
        normalized = "\n".join(item[0].lower() for item in quotes)
        self.assertTrue(all(phrase not in normalized for phrase in BANNED_QUOTE_PHRASES))


class PublicMaterialImporterTest(unittest.TestCase):
    def setUp(self):
        Base.metadata.drop_all(bind=engine)
        Base.metadata.create_all(bind=engine)

    def test_importer_is_idempotent_and_preserves_trace_fields(self):
        pack = PublicMaterialPackImport(
            code="test-exam",
            title="测试词书",
            description="按语料排序",
            kind="word_book",
            sort_order=1,
            content_version="test-v1",
        )
        items = [
            PublicMaterialItemImport(
                content="analyze",
                chinese="分析",
                card_type="word",
                source_label="测试词书",
                source="exam-corpus-2026",
                source_id="exam:analyze",
                license=None,
                corpus_rank=1,
                corpus_frequency=42.5,
                production_batch="batch-1",
                review_note="verified fixture",
            ),
            PublicMaterialItemImport(
                content="context",
                chinese="语境",
                card_type="word",
                source_label="测试词书",
                source="exam-corpus-2026",
                source_id="exam:context",
                license=None,
                corpus_rank=2,
                corpus_frequency=21,
                production_batch="batch-1",
            ),
        ]

        with TestingSessionLocal() as db:
            first = import_public_materials(db, packs=[pack], items_by_pack={"test-exam": items})
            second = import_public_materials(db, packs=[pack], items_by_pack={"test-exam": items[:1]})
            third = import_public_materials(db, packs=[pack], items_by_pack={"test-exam": items[:1]})
            db.commit()

            self.assertEqual({"test-exam": 2}, first)
            self.assertEqual({"test-exam": 1}, second)
            self.assertEqual({"test-exam": 1}, third)
            approved = list(db.scalars(select(PublicMaterialItem).where(
                PublicMaterialItem.status == "approved",
            )))
            hidden = list(db.scalars(select(PublicMaterialItem).where(
                PublicMaterialItem.status == "hidden",
            )))
            self.assertEqual(1, len(approved))
            self.assertEqual(1, len(hidden))
            self.assertEqual("exam-corpus-2026", approved[0].source)
            self.assertEqual("exam:analyze", approved[0].source_id)
            self.assertIsNone(approved[0].license)
            self.assertEqual(1, approved[0].corpus_rank)
            self.assertEqual(42.5, approved[0].corpus_frequency)
            self.assertEqual("batch-1", approved[0].production_batch)


if __name__ == "__main__":
    unittest.main()

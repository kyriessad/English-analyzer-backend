import io
import bz2
import zipfile
from pathlib import Path

import app.models
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.discovery import PublicMaterialItem, PublicMaterialPack
from app.services.scenario_material_pipeline import (
    SCENE_CODES,
    ClassifiedScenario,
    ScenarioCandidate,
    _apply_ai_review,
    _apply_chinese_quality_review,
    _build_imports,
    _classify_candidates,
    _near_duplicate_fingerprint,
    _read_cc0_english,
    _read_tatoeba_pairs,
    load_source_manifest,
)
from scripts.seed_discovery_content import without_protected_scenario_packs
from app.services.public_material_importer import (
    PublicMaterialItemImport,
    PublicMaterialPackImport,
    import_public_materials,
)


def _archive(lines: list[str]) -> bytes:
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as zipped:
        zipped.writestr("cmn.txt", "\n".join(lines))
    return target.getvalue()


def test_manifest_covers_the_eleven_scene_packs_and_pins_commercially_usable_license():
    manifest = load_source_manifest()

    assert len(manifest["packs"]) == 11
    assert manifest["source"]["license"] == "CC BY 2.0 FR"
    assert manifest["source"]["sha256"]
    assert manifest["ai_review"]["model"] == "qwen3:8b"


def test_tatoeba_parser_preserves_ids_filters_names_and_deduplicates_english():
    lines = [
        "Could you help me?\t你能帮我吗？\tCC-BY 2.0 (France) Attribution: tatoeba.org #10 (CK) & #20 (Martha)",
        "Could you help me?\t可以帮我吗？\tCC-BY 2.0 (France) Attribution: tatoeba.org #10 (CK) & #21 (Martha)",
        "Tom can help me.\t汤姆能帮我。\tCC-BY 2.0 (France) Attribution: tatoeba.org #11 (CK) & #22 (Martha)",
    ]

    candidates, report = _read_tatoeba_pairs(_archive(lines), {"archive_member": "cmn.txt"})

    assert len(candidates) == 1
    assert candidates[0].source_id.startswith("en:10:CK;zh:")
    assert report["duplicate_english_removed"] == 1
    assert report["rejections"]["named_or_unsuitable_topic"] == 1


def test_classification_is_cross_pack_deduplicated_and_removes_politeness_variants():
    lines = [
        "Could you tell me the Wi-Fi password?\t请告诉我无线网密码。\tCC-BY 2.0 (France) Attribution: tatoeba.org #30 (CK) & #40 (Martha)",
        "Could you tell me the Wi-Fi password, please?\t请问无线网密码是什么？\tCC-BY 2.0 (France) Attribution: tatoeba.org #31 (CK) & #41 (Martha)",
        "Where is the train station?\t火车站在哪里？\tCC-BY 2.0 (France) Attribution: tatoeba.org #32 (CK) & #42 (Martha)",
    ]
    candidates, _ = _read_tatoeba_pairs(_archive(lines), {"archive_member": "cmn.txt"})

    classified, near_duplicates = _classify_candidates(candidates, target_per_pack=10, reserve_per_pack=0)
    normalized = [row.candidate.normalized for rows in classified.values() for row in rows]

    assert len(normalized) == len(set(normalized)) == 2
    assert near_duplicates >= 1
    assert len(classified["internet-social-media"]) == 1
    assert len(classified["travel"]) == 1


def test_demo_seed_filter_preserves_production_scenario_packs():
    packs = [
        PublicMaterialPackImport("daily-life", "Daily", "", "expression", 1, "demo"),
        PublicMaterialPackImport("daily-quote", "Quote", "", "daily_quote", 2, "demo"),
    ]
    item = PublicMaterialItemImport("Take your time.", "慢慢来。", "sentence", "Daily")

    filtered_packs, filtered_items = without_protected_scenario_packs(
        packs,
        {"daily-life": [item], "daily-quote": [item]},
        {"daily-life"},
    )

    assert [pack.code for pack in filtered_packs] == ["daily-quote"]
    assert set(filtered_items) == {"daily-quote"}


def test_importer_replaces_disjoint_short_pack_with_larger_pack():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    pack = PublicMaterialPackImport("scene-replacement", "Scene", "", "expression", 1, "v1")

    def item(number: int) -> PublicMaterialItemImport:
        return PublicMaterialItemImport(f"Example {number}.", f"例句 {number}", "sentence", "Scene")

    with testing_session() as db:
        import_public_materials(db, packs=[pack], items_by_pack={pack.code: [item(1), item(2)]})
        db.commit()
        replacement = [item(number) for number in range(10, 20)]
        import_public_materials(db, packs=[pack], items_by_pack={pack.code: replacement})
        db.commit()

        pack_id = db.scalar(select(PublicMaterialPack.id).where(PublicMaterialPack.code == pack.code))
        active = list(db.scalars(select(PublicMaterialItem).where(
            PublicMaterialItem.pack_id == pack_id,
            PublicMaterialItem.status == "approved",
        ).order_by(PublicMaterialItem.position)))
        assert [row.content for row in active] == [item(number).content for number in range(10, 20)]


def test_single_item_ai_review_binds_a_decision_even_if_model_rewrites_id():
    candidate = ScenarioCandidate(
        english="Could you help me?",
        chinese="你能帮我吗？",
        normalized="could you help me?",
        source_id="en:10:CK;zh:20:Martha",
        english_id="10",
        chinese_id="20",
        word_count=4,
    )
    classified = {code: [] for code in SCENE_CODES}
    classified["social-communication"] = [
        ClassifiedScenario(candidate, "social-communication", 5, 20)
    ]

    cache_path = Path(".runtime") / "test-scenario-ai-review.json"
    cache_path.unlink(missing_ok=True)
    try:
        reviewed, report = _apply_ai_review(
            classified,
            target_per_pack=1,
            items_per_pack=1,
            batch_size=1,
            model="test-model",
            prompt_version="test-prompt",
            cache_path=cache_path,
            reviewer=lambda rows, model: [{
                "id": "rewritten-by-model",
                "keep": True,
                "category": "social-communication",
                "reason": "useful request",
            }],
        )
    finally:
        cache_path.unlink(missing_ok=True)

    assert reviewed["social-communication"][0].ai_reviewed is True
    assert report["reviewed_count"] == 1
    assert report["accepted_count"] == 1


def test_cc0_english_parser_preserves_source_id_and_rejects_non_conversational_noise():
    archive = bz2.compress((
        "100\teng\tCould you send me the report?\t2026-01-01\n"
        "101\teng\tPhotovoltaic metamaterials radiate electromagnetic energy across multiple scientific dimensions without interruption.\t2026-01-01\n"
        "102\teng\tCould you send me the report?\t2026-01-01\n"
    ).encode("utf-8"))

    candidates, report = _read_cc0_english(archive, {"could you send me the report?"})

    assert candidates == []
    assert report == {"raw_english_count": 3, "clean_english_count": 0, "rejected_count": 3}


def test_chinese_review_preserves_good_translation_corrects_bad_translation_and_reuses_cache():
    good = ScenarioCandidate("Can you help me?", "你能帮我吗？", "can you help me?", "cc0:10", "10", "", 4)
    bad = ScenarioCandidate("The website is down.", "网站是向下的。", "the website is down.", "cc0:11", "11", "", 4)
    classified = {code: [] for code in SCENE_CODES}
    classified["useful-sentences"] = [
        ClassifiedScenario(good, "useful-sentences", 5, 20, True),
        ClassifiedScenario(bad, "useful-sentences", 5, 20, True),
    ]
    calls = []

    def reviewer(rows, model):
        calls.append(rows)
        return [
            {"id": "cc0:10", "changed": False, "chinese": "你能帮我吗？", "reason": "准确"},
            {"id": "cc0:11", "changed": True, "chinese": "网站宕机了。", "reason": "修正直译"},
        ]

    cache_path = Path(".runtime") / "test-chinese-review-success.json"
    cache_path.unlink(missing_ok=True)
    reviewed, report = _apply_chinese_quality_review(
        classified, translated_prefixes={"cc0"}, model="qwen3:8b", prompt_version="v1",
        batch_size=25, cache_path=cache_path, reviewer=reviewer,
    )
    rerun, rerun_report = _apply_chinese_quality_review(
        classified, translated_prefixes={"cc0"}, model="qwen3:8b", prompt_version="v1",
        batch_size=25, cache_path=cache_path, reviewer=reviewer,
    )

    rows = reviewed["useful-sentences"]
    assert [row.candidate.chinese for row in rows] == ["你能帮我吗？", "网站宕机了。"]
    assert [row.candidate.source_id for row in rows] == ["cc0:10", "cc0:11"]
    assert report["reviewed_count"] == 2
    assert report["modified_count"] == 1
    assert len(calls) == 1
    assert rerun["useful-sentences"][1].candidate.chinese == "网站宕机了。"
    assert rerun_report["cache_hit_count"] == 2

    manifest = load_source_manifest()
    _, items = _build_imports(
        reviewed, source=dict(manifest["source"]),
        english_cc0_source=dict(manifest["english_cc0_source"]),
        supplement_sources=[dict(row) for row in manifest["supplement_sources"]],
        production_batch="test",
    )
    assert items["useful-sentences"][1].source == "tatoeba-cc0-ai-translation"
    assert items["useful-sentences"][1].license == "CC0 1.0"
    cache_path.unlink(missing_ok=True)


def test_chinese_review_failure_keeps_tmt_translation_and_is_retried():
    candidate = ScenarioCandidate("The website is down.", "网站是向下的。", "the website is down.", "sgd:1:2", "12", "", 4)
    classified = {code: [] for code in SCENE_CODES}
    classified["internet-social-media"] = [ClassifiedScenario(candidate, "internet-social-media", 5, 20, True)]
    cache_path = Path(".runtime") / "test-chinese-review-failure.json"
    cache_path.unlink(missing_ok=True)

    reviewed, report = _apply_chinese_quality_review(
        classified, translated_prefixes={"sgd"}, model="qwen3:8b", prompt_version="v1",
        batch_size=25, cache_path=cache_path, reviewer=lambda rows, model: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    retried, retry_report = _apply_chinese_quality_review(
        classified, translated_prefixes={"sgd"}, model="qwen3:8b", prompt_version="v1",
        batch_size=25, cache_path=cache_path,
        reviewer=lambda rows, model: [{"id": "sgd:1:2", "changed": True, "chinese": "网站宕机了。", "reason": "修正直译"}],
    )

    assert reviewed["internet-social-media"][0].candidate.chinese == "网站是向下的。"
    assert report["failure_count"] == 1
    assert retried["internet-social-media"][0].candidate.chinese == "网站宕机了。"
    assert retry_report["fresh_reviewed_count"] == 1
    cache_path.unlink(missing_ok=True)

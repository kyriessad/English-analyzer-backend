import app.models
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.services.exam_wordbook_pipeline import (
    _clean_corpus_text,
    _token_counts,
    load_source_manifest,
)
from app.services.ecdict_service import _translation_candidates
from app.services.public_material_importer import PublicMaterialItemImport, PublicMaterialPackImport
from app.services.public_material_importer import import_public_materials
from scripts.seed_discovery_content import without_protected_word_books


def test_token_counts_are_case_insensitive_and_keep_lexical_hyphens():
    counts, total = _token_counts(["Analyze analysis ANALYZE. Well-known and well-known."])

    assert total == 6
    assert counts["analyze"] == 2
    assert counts["analysis"] == 1
    assert counts["well-known"] == 2


def test_corpus_cleaning_removes_exam_boilerplate_but_keeps_question_content():
    text = """# Part II / Reading
Directions: Mark the corresponding letter on Answer Sheet 2.
Questions 1 and 2 are based on the passage.
Climate change affects food production.
"""

    cleaned = _clean_corpus_text(text)

    assert cleaned == "Climate change affects food production."


def test_ecdict_literal_newlines_are_split_before_selecting_meanings():
    assert _translation_candidates("n. first\\n[medical] second") == ["first", "second"]


def test_source_manifest_covers_only_the_five_exam_books():
    manifest = load_source_manifest()

    assert set(manifest["books"]) == {"cet4", "cet6", "postgraduate", "ielts", "toefl"}
    assert manifest["candidate_sources"]["ecdict"]["license"] == "MIT"
    assert set(manifest["candidate_sources"]) == {"ecdict"}
    assert manifest["books"]["postgraduate"]["candidate_tag"] == "ky"
    assert manifest["books"]["postgraduate"]["corpus"]["kind"] == "github_text_archive"
    assert all(book["corpus"]["nature"] for book in manifest["books"].values())


def test_demo_seed_filter_preserves_production_word_books():
    packs = [
        PublicMaterialPackImport("cet4", "CET4", "", "word_book", 10, "demo"),
        PublicMaterialPackImport("daily-life", "Daily", "", "expression", 20, "demo"),
    ]
    item = PublicMaterialItemImport("study", "学习", "word", "CET4")

    filtered_packs, filtered_items = without_protected_word_books(
        packs,
        {"cet4": [item], "daily-life": [item]},
        {"cet4"},
    )

    assert [pack.code for pack in filtered_packs] == ["daily-life"]
    assert set(filtered_items) == {"daily-life"}


def test_importer_repeats_after_candidate_set_replacement():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    testing_session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)
    pack = PublicMaterialPackImport("replacement-test", "Test", "", "word_book", 1, "v1")

    def item(word: str) -> PublicMaterialItemImport:
        return PublicMaterialItemImport(word, word, "word", "Test")

    with testing_session() as db:
        import_public_materials(
            db, packs=[pack], items_by_pack={"replacement-test": [item("a"), item("b"), item("c")]}
        )
        db.commit()
        replacement = [item("b"), item("c"), item("d")]
        import_public_materials(db, packs=[pack], items_by_pack={"replacement-test": replacement})
        db.commit()
        assert import_public_materials(
            db, packs=[pack], items_by_pack={"replacement-test": replacement}
        ) == {"replacement-test": 3}
        db.commit()

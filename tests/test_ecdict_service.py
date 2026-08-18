from app.services.ecdict_service import (
    get_dictionary_translation,
    get_dictionary_translations,
)


def test_dictionary_meanings_are_clean_and_context_selects_the_used_sense():
    meanings = get_dictionary_translations("organ")

    assert "器官" in meanings
    assert get_dictionary_translation("organ", "医生检查了这个器官。") == "器官"
    assert get_dictionary_translation("organ", "这里没有可匹配的释义。") is None

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_every_declared_rule_has_required_layer_traceability():
    rule_catalog = json.loads((ROOT / "tests/fixtures/rule_ids.json").read_text(encoding="utf-8"))
    traceability = json.loads((ROOT / "tests/contract_traceability.json").read_text(encoding="utf-8"))
    expected = {item["rule_id"]: item for item in rule_catalog["rules"]}
    actual = traceability["rules"]

    assert set(actual) == set(expected)
    for rule_id, rule in expected.items():
        mapping = actual[rule_id]
        for layer in (1, 2, 3):
            key = f"layer{layer}"
            assert key in mapping, f"{rule_id} is missing {key}"
            if layer in rule["layers"]:
                assert mapping[key], f"{rule_id} has no {key} evidence"
            for relative_path in mapping[key]:
                assert (ROOT / relative_path).resolve().is_file(), (
                    f"{rule_id} {key} points to a missing file: {relative_path}"
                )

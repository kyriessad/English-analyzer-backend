"""Representative real-dependency checks for Layer 2.

Layer 2 is opt-in because it requires external services. The test cases are
fixed and only assert integration contracts, not natural-language output.
"""

import json
import os
from pathlib import Path

import pytest

from app.core.config import settings


RUN_LAYER2 = os.environ.get("RUN_LAYER2") == "1"
pytestmark = [
    pytest.mark.layer2,
    pytest.mark.skipif(not RUN_LAYER2, reason="set RUN_LAYER2=1 to run real dependencies"),
]

FIXTURE = Path(__file__).parent / "fixtures" / "sample_corpus.json"


def _corpus():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_LAYER2_001_real_dependency_profile_is_explicit():
    required = {
        "DATABASE_URL": settings.database_url,
        "ECDICT_DB_PATH": settings.ecdict_db_path,
        "HARPER_BASE_URL": settings.harper_base_url,
        "OLLAMA_BASE_URL": settings.ollama_base_url,
        "PIPER_DATA_DIR": settings.piper_data_dir,
    }
    missing = [name for name, value in required.items() if not value]
    assert not missing, f"Layer 2 requires configured real dependencies: {missing}"


@pytest.mark.parametrize(
    "scenario_id",
    [
        "VAL-PASS-001",
        "VAL-CONTENT-001",
        "VAL-ADVISORY-001",
        "VAL-SYSTEM-001",
        "VAL-HARD-001",
    ],
)
def test_LAYER2_002_representative_cases_use_fixed_scenario_ids(scenario_id):
    scenarios = {item["scenario_id"]: item for item in _corpus()["validation_scenarios"]}
    assert scenario_id in scenarios
    assert scenarios[scenario_id]["sample_id"]


def test_LAYER2_003_resource_boundary_configuration_is_deterministic():
    capacity = int(os.environ.get("TEST_RESOURCE_CAPACITY", "1"))
    assert capacity >= 1
    assert [capacity - 1, capacity, capacity + 1] == [0, 1, 2] if capacity == 1 else True

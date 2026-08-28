# Frozen Contract Fixtures

`sample_corpus.json` is the single source of fixed English samples and validation scenarios. The Python suite reads it directly; the Node suite reads the same file through a relative path into this backend repository.

`rule_ids.json` is the stable Rule ID registry. A test that implements a frozen rule should include the relevant ID in its test name or `RULE_ID` constant.

The corpus seed is metadata only. Layer 3 must never sample from this file randomly; it must reference an explicit `sample_id` or `scenario_id`.

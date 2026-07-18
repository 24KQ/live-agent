"""Phase 15 Task 2 Golden Dataset 与 Manifest 的 TDD 契约。

测试只读取脱敏本地文件或在临时目录生成数据，不访问模型、数据库、Kafka 或
GitHub。它固定当前 Release 的 48 例边界，同时保护 Phase 13/14 历史资产不被
原地覆盖。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from src.release_gates.dataset import (
    GoldenCase,
    GoldenSplit,
    generate_phase15_dataset,
    load_phase15_dataset,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
EVALUATION_ROOT = PROJECT_ROOT / "evaluation"


def test_phase15_dataset_has_frozen_48_case_shape_and_three_scenes() -> None:
    """Release 清单必须固定 48 例、12/24/12 split 和三场景来源分布。"""

    dataset = load_phase15_dataset(EVALUATION_ROOT)
    assert len(dataset.cases) == 48
    assert Counter(case.split for case in dataset.cases) == {
        GoldenSplit.DEVELOPMENT: 12,
        GoldenSplit.VALIDATION: 24,
        GoldenSplit.HOLDOUT: 12,
    }
    assert Counter(case.domain for case in dataset.cases) == {
        "RUNTIME_SKILL": 8,
        "RUNTIME_PLAN": 8,
        "RUNTIME_EVENT": 8,
        "LIVE": 16,
        "PREPARE": 4,
        "REVIEW": 4,
    }
    assert len({case.case_id for case in dataset.cases}) == 48
    assert all(case.case_id.startswith("phase15-") for case in dataset.cases)


def test_phase15_manifest_binds_case_digests_schema_and_history() -> None:
    """Manifest 必须绑定字节摘要、Schema/生成器身份和历史来源。"""

    dataset = load_phase15_dataset(EVALUATION_ROOT)
    manifest = dataset.manifest
    assert manifest.manifest_id == "phase15-runtime-v1"
    assert manifest.supersedes == ("phase13-v3", "phase14-human-support-v1")
    assert manifest.schema_digest
    assert manifest.generator_digest
    assert manifest.rules_digest
    assert manifest.source_code_digest
    assert len(manifest.case_digests) == 48
    assert set(manifest.case_digests) == {case.case_id for case in dataset.cases}
    assert manifest.manifest_digest
    assert set(manifest.source_manifest_digests) == {
        "phase13-v3",
        "phase14-human-support-v1",
    }

    schema = json.loads(
        (EVALUATION_ROOT / "schemas" / "phase15_golden_manifest.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema).validate(manifest.model_dump(mode="json"))

    historical = json.loads(
        (EVALUATION_ROOT / "manifests" / "phase13-v3.json").read_text(encoding="utf-8")
    )
    assert historical["manifest_id"] == "phase13-v3"
    assert sum(len(case_ids) for case_ids in historical["case_ids"].values()) == 240


def test_phase15_cases_keep_holdout_labels_out_of_model_input() -> None:
    """模型输入只含脱敏 case，评估标签必须落在独立 labels 资产。"""

    dataset = load_phase15_dataset(EVALUATION_ROOT)
    assert all("label" not in case.input and "expected" not in case.input for case in dataset.cases)
    for split in GoldenSplit:
        label_path = EVALUATION_ROOT / "labels" / "phase15-runtime-v1" / f"{split.value}.jsonl"
        assert label_path.is_file()
        assert len(label_path.read_text(encoding="utf-8").splitlines()) == {
            GoldenSplit.DEVELOPMENT: 12,
            GoldenSplit.VALIDATION: 24,
            GoldenSplit.HOLDOUT: 12,
        }[split]


def test_phase15_generator_is_byte_stable_and_does_not_mutate_history(tmp_path: Path) -> None:
    """固定 seed 连续生成必须逐字节一致，旧 Phase 13/14 文件不可被覆盖。"""

    first = tmp_path / "first"
    second = tmp_path / "second"
    generate_phase15_dataset(first, source_root=PROJECT_ROOT)
    generate_phase15_dataset(second, source_root=PROJECT_ROOT)

    first_files = sorted(path.relative_to(first) for path in first.rglob("*" ) if path.is_file())
    second_files = sorted(path.relative_to(second) for path in second.rglob("*" ) if path.is_file())
    assert first_files == second_files
    assert all(
        (first / relative).read_bytes() == (second / relative).read_bytes()
        for relative in first_files
    )
    assert (PROJECT_ROOT / "evaluation/manifests/phase13-v3.json").is_file()
    assert (PROJECT_ROOT / "evaluation/phase14_human_support/manifest.json").is_file()


def test_phase15_case_rejects_nested_sensitive_payload() -> None:
    """递归敏感字段必须在数据进入 Manifest 前被拒绝。"""

    with pytest.raises(ValueError, match="sensitive"):
        GoldenCase.model_validate(
            {
                "case_id": "phase15-runtime-skill-development-001",
                "split": "development",
                "domain": "RUNTIME_SKILL",
                "source": "synthetic",
                "source_case_id": None,
                "input": {"facts": {"nested": {"raw_text": "secret"}}},
            }
        )

"""Phase 17 holdout dataset manifest 机制测试（合成数据，不触碰真实 30 例）。

真实 30 例 manifest 在阶段③数据起草并经用户终审后冻结生成；本文件验证
manifest 模型、loader 与 case membership 校验机制的完整性。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from src.decision_support.phase17_holdout_dataset import (
    Phase17DatasetIdentityError,
    Phase17HoldoutDatasetManifest,
    load_phase17_holdout_dataset_manifest,
    validate_phase17_holdout_case,
)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _build_manifest_payload() -> dict:
    case_ids = [f"holdout-case-{i:03d}" for i in range(1, 31)]
    case_id_to_input_digest = {cid: _digest(f"input-{cid}") for cid in case_ids}
    payload = {
        "dataset_id": "phase17-holdout-cases-v1",
        "dataset_version": "1.0.0",
        "split": "HOLDOUT",
        "case_count": 30,
        "case_id_to_input_digest": case_id_to_input_digest,
        "batch_case_ids": {
            "1": case_ids[:10],
            "2": case_ids[10:],
        },
        "dev_excluded_case_ids": ["development-case-001", "development-case-002"],
        "inputs_root": "evaluation/phase17_holdout/inputs",
        "labels_root": "evaluation/phase17_holdout/labels",
        "labels_paths": [
            "evaluation/phase17_holdout/labels/labels-v1.json",
        ],
    }
    payload["manifest_digest"] = _manifest_digest(payload)
    return payload


def _manifest_digest(payload: dict) -> str:
    payload = dict(payload)
    payload.pop("manifest_digest", None)
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def test_phase17_dataset_manifest_valid_synthetic() -> None:
    manifest = Phase17HoldoutDatasetManifest(**_build_manifest_payload())
    assert manifest.case_count == 30
    assert manifest.split == "HOLDOUT"
    assert len(manifest.case_ids()) == 30
    assert len(manifest.batch_case_ids(1)) == 10
    assert len(manifest.batch_case_ids(2)) == 20
    assert manifest.manifest_digest == _manifest_digest(_build_manifest_payload())


def test_phase17_dataset_manifest_rejects_missing_case_mapping() -> None:
    payload = _build_manifest_payload()
    payload["case_id_to_input_digest"].pop("holdout-case-001")
    payload["manifest_digest"] = _manifest_digest(payload)
    with pytest.raises(Phase17DatasetIdentityError, match="exactly"):
        Phase17HoldoutDatasetManifest(**payload)


def test_phase17_dataset_manifest_rejects_wrong_batch_split() -> None:
    payload = _build_manifest_payload()
    case_ids = [f"holdout-case-{i:03d}" for i in range(1, 31)]
    payload["batch_case_ids"] = {"1": case_ids[:9], "2": case_ids[9:]}
    payload["manifest_digest"] = _manifest_digest(payload)
    with pytest.raises(Phase17DatasetIdentityError, match="10 \\+ 20"):
        Phase17HoldoutDatasetManifest(**payload)


def test_phase17_dataset_manifest_rejects_overlapping_batches() -> None:
    payload = _build_manifest_payload()
    case_ids = [f"holdout-case-{i:03d}" for i in range(1, 31)]
    payload["batch_case_ids"] = {"1": case_ids[:10], "2": case_ids[5:25]}
    payload["manifest_digest"] = _manifest_digest(payload)
    with pytest.raises(Phase17DatasetIdentityError, match="disjoint"):
        Phase17HoldoutDatasetManifest(**payload)


def test_phase17_dataset_manifest_rejects_dev_overlap() -> None:
    payload = _build_manifest_payload()
    payload["dev_excluded_case_ids"] = ("holdout-case-001",)
    payload["manifest_digest"] = _manifest_digest(payload)
    with pytest.raises(Phase17DatasetIdentityError, match="must not be in the dev dataset"):
        Phase17HoldoutDatasetManifest(**payload)


def test_phase17_dataset_manifest_rejects_labels_inside_inputs() -> None:
    payload = _build_manifest_payload()
    payload["labels_root"] = "evaluation/phase17_holdout/inputs/labels"
    payload["manifest_digest"] = _manifest_digest(payload)
    with pytest.raises(Phase17DatasetIdentityError, match="outside the inputs root"):
        Phase17HoldoutDatasetManifest(**payload)


def test_phase17_dataset_manifest_rejects_tampered_digest() -> None:
    payload = _build_manifest_payload()
    payload["case_id_to_input_digest"]["holdout-case-001"] = "0" * 64
    with pytest.raises(Phase17DatasetIdentityError, match="does not match payload"):
        Phase17HoldoutDatasetManifest(**payload)


def test_phase17_dataset_manifest_loader_roundtrip(
    tmp_path: Path,
) -> None:
    payload = _build_manifest_payload()
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    path = tmp_path / "phase17-holdout-dataset-manifest-v1.json"
    path.write_bytes(raw)
    manifest = load_phase17_holdout_dataset_manifest(repository_root=tmp_path, path=Path(path.name))
    assert manifest.manifest_digest == payload["manifest_digest"]


def test_phase17_dataset_manifest_loader_rejects_bom(tmp_path: Path) -> None:
    payload = _build_manifest_payload()
    raw = b"\xef\xbb\xbf" + json.dumps(payload).encode("utf-8")
    path = tmp_path / "phase17-holdout-dataset-manifest-v1.json"
    path.write_bytes(raw)
    with pytest.raises(Phase17DatasetIdentityError, match="UTF-8 LF without BOM"):
        load_phase17_holdout_dataset_manifest(repository_root=tmp_path, path=Path(path.name))


def test_phase17_holdout_case_membership_validation() -> None:
    manifest = Phase17HoldoutDatasetManifest(**_build_manifest_payload())
    case_id = "holdout-case-001"
    input_digest = manifest.input_digest(case_id)
    validate_phase17_holdout_case(
        case_id=case_id,
        input_digest=input_digest,
        batch_index=1,
        manifest=manifest,
    )
    with pytest.raises(Phase17DatasetIdentityError, match="not in the frozen manifest"):
        validate_phase17_holdout_case(
            case_id="unknown-case",
            input_digest=_digest("x"),
            batch_index=1,
            manifest=manifest,
        )
    with pytest.raises(Phase17DatasetIdentityError, match="not in batch 1"):
        validate_phase17_holdout_case(
            case_id="holdout-case-011",
            input_digest=manifest.input_digest("holdout-case-011"),
            batch_index=1,
            manifest=manifest,
        )
    with pytest.raises(Phase17DatasetIdentityError, match="does not match the frozen manifest"):
        validate_phase17_holdout_case(
            case_id=case_id,
            input_digest="0" * 64,
            batch_index=1,
            manifest=manifest,
        )

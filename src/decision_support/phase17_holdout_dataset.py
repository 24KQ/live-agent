"""Phase 17 holdout 数据集 manifest 模型与运行时 case membership 校验。

数据集身份（data identity）是 phase17 执行契约的 7+2 约束的承载层：
- manifest 固定完整 ``case_id -> input_digest`` 映射（``case_must_be_in_manifest``）；
- 两个 batch 只是同一 manifest 的固定子集 10 + 20（``batches_fixed_subsets``）；
- holdout case 不得出现在 dev 数据集（``case_must_not_be_in_dev``）；
- 标签与输入物理隔离（``labels_isolated_from_input``）：labels 目录不得是
  inputs 目录的子路径，runtime 代码只被允许读取 inputs。

真实 30 例 manifest 文件在阶段③数据起草并经用户终审后冻结生成；阶段②
交付的是本机制（模型 + 校验器 + loader），保证任何真实模型调用前
membership 校验已可用。manifest 变更即数据身份变更，必须生成新 digest 并
重新冻结 phase17 执行契约（``param_change_requires_new_digest``）。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from src.decision_support.phase16_qualification import (
    PHASE17_HOLDOUT_BATCHES,
    PHASE17_HOLDOUT_HIGH_CONFLICT_CASE_COUNT,
)

_SHA256_HEX = frozenset("0123456789abcdef")


def canonical_json_sha256(payload: object) -> str:
    """与 phase16_qualification 相同的 canonical 摘要口径（排序键、紧凑分隔符）。"""

    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class Phase17DatasetIdentityError(ValueError):
    """数据集身份校验失败；任何一次失败都必须在模型调用前 fail-closed。"""


class Phase17HoldoutDatasetManifest:
    """冻结的 holdout 数据集身份；不可动态追加，变更 = 新 digest = 新契约。"""

    def __init__(
        self,
        *,
        dataset_id: str,
        dataset_version: str,
        case_id_to_input_digest: dict[str, str],
        batch_case_ids: dict[int, tuple[str, ...]],
        dev_excluded_case_ids: tuple[str, ...] = (),
        inputs_root: str,
        labels_root: str,
        labels_paths: tuple[str, ...] = (),
        manifest_digest: str | None = None,
        split: str = "HOLDOUT",
        case_count: int = PHASE17_HOLDOUT_HIGH_CONFLICT_CASE_COUNT,
    ) -> None:
        if not dataset_id or not dataset_version:
            raise Phase17DatasetIdentityError("dataset id and version are required")
        if manifest_digest is None:
            raise Phase17DatasetIdentityError("dataset manifest requires a frozen manifest digest")
        if split != "HOLDOUT":
            raise Phase17DatasetIdentityError("holdout dataset split is frozen to HOLDOUT")
        if case_count != PHASE17_HOLDOUT_HIGH_CONFLICT_CASE_COUNT:
            raise Phase17DatasetIdentityError(
                "holdout dataset case count is frozen to "
                f"{PHASE17_HOLDOUT_HIGH_CONFLICT_CASE_COUNT}"
            )

        digest_map = dict(case_id_to_input_digest)
        if len(digest_map) != PHASE17_HOLDOUT_HIGH_CONFLICT_CASE_COUNT:
            raise Phase17DatasetIdentityError(
                "holdout dataset must contain exactly "
                f"{PHASE17_HOLDOUT_HIGH_CONFLICT_CASE_COUNT} cases"
            )
        for case_id, input_digest in digest_map.items():
            if not case_id or not isinstance(input_digest, str) or len(input_digest) != 64:
                raise Phase17DatasetIdentityError("holdout case input digest must be sha256 hex")
            if any(ch not in _SHA256_HEX for ch in input_digest):
                raise Phase17DatasetIdentityError("holdout case input digest must be sha256 hex")
        if len(digest_map) != len(set(digest_map)):
            raise Phase17DatasetIdentityError("holdout case ids must be unique")

        batch_spec = {
            int(index): tuple(case_ids) for index, case_ids in batch_case_ids.items()
        }
        if tuple(sorted(batch_spec)) != tuple(index for index, _ in PHASE17_HOLDOUT_BATCHES):
            raise Phase17DatasetIdentityError("holdout batches are frozen to batch 1 + batch 2")
        for (expected_index, expected_count), (index, case_ids) in zip(
            PHASE17_HOLDOUT_BATCHES, sorted(batch_spec.items())
        ):
            if index != expected_index or len(case_ids) != expected_count:
                raise Phase17DatasetIdentityError("holdout batch case counts are frozen to 10 + 20")
            unknown = [cid for cid in case_ids if cid not in digest_map]
            if unknown:
                raise Phase17DatasetIdentityError("holdout batch references unknown case ids")
        merged = [cid for case_ids in batch_spec.values() for cid in case_ids]
        if len(set(merged)) != len(merged):
            raise Phase17DatasetIdentityError("holdout batch subsets must be disjoint")
        if set(merged) != set(digest_map):
            raise Phase17DatasetIdentityError("holdout batches must exactly cover the frozen case set")

        if any(cid in digest_map for cid in dev_excluded_case_ids):
            raise Phase17DatasetIdentityError("holdout case must not be in the dev dataset")
        if not inputs_root or not labels_root:
            raise Phase17DatasetIdentityError("inputs and labels must be physically separated")
        if Path(labels_root).is_relative_to(Path(inputs_root)):
            raise Phase17DatasetIdentityError("holdout labels must live outside the inputs root")
        for labels_path in labels_paths:
            if Path(labels_path).is_relative_to(Path(inputs_root)):
                raise Phase17DatasetIdentityError(
                    "holdout labels must never be reachable from the inputs root"
                )

        expected = canonical_json_sha256(
            {
                "dataset_id": dataset_id,
                "dataset_version": dataset_version,
                "split": "HOLDOUT",
                "case_count": PHASE17_HOLDOUT_HIGH_CONFLICT_CASE_COUNT,
                "case_id_to_input_digest": digest_map,
                "batch_case_ids": batch_spec,
                "dev_excluded_case_ids": tuple(dev_excluded_case_ids),
                "inputs_root": inputs_root,
                "labels_root": labels_root,
                "labels_paths": tuple(labels_paths),
            }
        )
        if manifest_digest != expected:
            raise Phase17DatasetIdentityError("dataset manifest digest does not match payload")

        self.dataset_id = dataset_id
        self.dataset_version = dataset_version
        self.split = split
        self.case_count = PHASE17_HOLDOUT_HIGH_CONFLICT_CASE_COUNT
        self._case_id_to_input_digest = digest_map
        self._batch_case_ids = batch_spec
        self._dev_excluded_case_ids = tuple(dev_excluded_case_ids)
        self.inputs_root = inputs_root
        self.labels_root = labels_root
        self.labels_paths = tuple(labels_paths)
        self.manifest_digest = manifest_digest

    def case_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._case_id_to_input_digest))

    def batch_case_ids(self, batch_index: int) -> tuple[str, ...]:
        return self._batch_case_ids[batch_index]

    def input_digest(self, case_id: str) -> str:
        return self._case_id_to_input_digest[case_id]

    def as_json(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "dataset_version": self.dataset_version,
            "split": self.split,
            "case_count": self.case_count,
            "case_id_to_input_digest": dict(self._case_id_to_input_digest),
            "batch_case_ids": {
                str(index): list(case_ids) for index, case_ids in self._batch_case_ids.items()
            },
            "dev_excluded_case_ids": list(self._dev_excluded_case_ids),
            "inputs_root": self.inputs_root,
            "labels_root": self.labels_root,
            "labels_paths": list(self.labels_paths),
            "manifest_digest": self.manifest_digest,
        }


def load_phase17_holdout_dataset_manifest(
    *, repository_root: Path, path: Path
) -> Phase17HoldoutDatasetManifest:
    """加载冻结的 manifest 文件；UTF-8/LF 无 BOM，digest 自校验。"""

    full_path = repository_root / path
    raw = full_path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf") or b"\r" in raw:
        raise Phase17DatasetIdentityError("dataset manifest must be UTF-8 LF without BOM")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Phase17DatasetIdentityError("dataset manifest is invalid JSON") from exc
    if payload.get("manifest_digest") is None:
        raise Phase17DatasetIdentityError("dataset manifest requires a frozen manifest digest")
    try:
        return Phase17HoldoutDatasetManifest(
            dataset_id=payload["dataset_id"],
            dataset_version=payload["dataset_version"],
            case_id_to_input_digest=payload["case_id_to_input_digest"],
            batch_case_ids={int(k): tuple(v) for k, v in payload["batch_case_ids"].items()},
            dev_excluded_case_ids=tuple(payload.get("dev_excluded_case_ids", ())),
            inputs_root=payload["inputs_root"],
            labels_root=payload["labels_root"],
            labels_paths=tuple(payload.get("labels_paths", ())),
            manifest_digest=payload["manifest_digest"],
        )
    except KeyError as exc:
        raise Phase17DatasetIdentityError(f"dataset manifest is missing field: {exc.args[0]}") from exc


def validate_phase17_holdout_case(
    *,
    case_id: str,
    input_digest: str,
    batch_index: int,
    manifest: Phase17HoldoutDatasetManifest,
) -> None:
    """运行时 case membership 校验；任何失败都在模型调用前阻断。"""

    if case_id not in manifest._case_id_to_input_digest:
        raise Phase17DatasetIdentityError(f"holdout case {case_id} is not in the frozen manifest")
    if case_id in manifest._dev_excluded_case_ids:
        raise Phase17DatasetIdentityError(f"holdout case {case_id} must not be in the dev dataset")
    if batch_index not in manifest._batch_case_ids:
        raise Phase17DatasetIdentityError(f"holdout batch {batch_index} is not a frozen subset")
    if case_id not in manifest._batch_case_ids[batch_index]:
        raise Phase17DatasetIdentityError(
            f"holdout case {case_id} is not in batch {batch_index} subset"
        )
    if input_digest != manifest._case_id_to_input_digest[case_id]:
        raise Phase17DatasetIdentityError(
            f"holdout case {case_id} input digest does not match the frozen manifest"
        )

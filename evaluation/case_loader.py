"""候选运行时只读 case 输入 Loader，仅返回经冻结清单验证的模型输入。"""

from __future__ import annotations

import hashlib
import json
import secrets
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from src.specialist_runtime.models import _freeze_json


CANDIDATES = ("live_ops", "planner", "review_memory")
SPLITS = ("development", "validation", "holdout")
_MANIFEST_RELATIVE_PATH = Path("manifests/phase13-v2.json")
_SCHEMA_RELATIVE_PATH = Path("schemas/phase13_case.schema.json")
_CASE_KEYS = {"case_id", "candidate", "split", "input"}


def _strict_json(raw: bytes, *, description: str) -> Any:
    """严格解析 UTF-8 JSON，并拒绝重复键，避免后键覆盖前键改变验证语义。"""

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key in {description}: {key}")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON in {description}") from exc


def _canonical_digest(value: Any, *, trailing_lf: bool) -> str:
    """使用与生成器一致的规范 JSON 字节计算身份摘要。"""

    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw = (text + ("\n" if trailing_lf else "")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _load_manifest(root: Path, expected_manifest_digest: str) -> dict[str, Any]:
    """校验外部锚点及两层 Manifest 自摘要，拒绝内部摘要与资产同步替换。"""

    path = root / _MANIFEST_RELATIVE_PATH
    manifest = _strict_json(path.read_bytes(), description="phase13-v2 manifest")
    if not isinstance(manifest, dict) or manifest.get("manifest_id") != "phase13-v2":
        raise ValueError("invalid phase13-v2 manifest identity")
    if not all(
        isinstance(manifest.get(key), dict)
        for key in ("artifact_digests", "case_ids", "case_candidate_map")
    ):
        raise ValueError("invalid phase13-v2 manifest structure")
    embedded_digest = manifest.get("manifest_digest")
    calculated_digest = _canonical_digest(
        {key: value for key, value in manifest.items() if key != "manifest_digest"},
        trailing_lf=True,
    )
    if (
        not isinstance(expected_manifest_digest, str)
        or len(expected_manifest_digest) != 64
        or not isinstance(embedded_digest, str)
        or not secrets.compare_digest(embedded_digest, calculated_digest)
        or not secrets.compare_digest(embedded_digest, expected_manifest_digest)
    ):
        raise ValueError("phase13 manifest anchor mismatch")
    store_manifest = manifest.get("store_manifest")
    if not isinstance(store_manifest, dict):
        raise ValueError("invalid phase13 store manifest")
    store_digest = store_manifest.get("manifest_digest")
    calculated_store_digest = _canonical_digest(
        {key: value for key, value in store_manifest.items() if key != "manifest_digest"},
        trailing_lf=False,
    )
    if not isinstance(store_digest, str) or not secrets.compare_digest(
        store_digest, calculated_store_digest
    ):
        raise ValueError("phase13 store manifest digest mismatch")
    return manifest


def _verified_artifact_bytes(
    root: Path, manifest: dict[str, Any], relative: Path
) -> bytes:
    """先按清单校验文件原始字节摘要，防止解析或规范化掩盖任何字节变化。"""

    relative_key = relative.as_posix()
    expected_digest = manifest["artifact_digests"].get(relative_key)
    if not isinstance(expected_digest, str):
        raise ValueError(f"artifact digest is not frozen: {relative_key}")
    raw = (root / relative).read_bytes()
    actual_digest = hashlib.sha256(raw).hexdigest()
    if not secrets.compare_digest(actual_digest, expected_digest):
        raise ValueError(f"artifact digest mismatch: {relative_key}")
    return raw


def _load_case_validator(
    root: Path, manifest: dict[str, Any]
) -> Draft202012Validator:
    """只使用摘要绑定的严格 Schema，避免替换 Schema 后放宽输入约束。"""

    schema_raw = _verified_artifact_bytes(root, manifest, _SCHEMA_RELATIVE_PATH)
    schema = _strict_json(schema_raw, description="phase13 case schema")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError("invalid phase13 case schema") from exc
    return Draft202012Validator(schema)


def load_case_inputs(
    root: Path,
    candidate: str,
    split: str,
    *,
    expected_manifest_digest: str,
) -> tuple[Any, ...]:
    """加载并逐层验证冻结输入，任一摘要、结构或身份异常均拒绝返回。"""

    if candidate not in CANDIDATES or split not in SPLITS:
        raise ValueError("unknown Phase 13 candidate or split")
    root = Path(root)
    manifest = _load_manifest(root, expected_manifest_digest)
    relative = Path("cases") / "phase13" / f"{candidate}-{split}.jsonl"
    raw = _verified_artifact_bytes(root, manifest, relative)
    validator = _load_case_validator(root, manifest)

    # JSONL 不允许空文件或空行；每一行都必须是独立且严格的 JSON 对象。
    lines = raw.splitlines()
    if not lines or any(not line.strip() for line in lines):
        raise ValueError("invalid case JSONL framing")
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        record = _strict_json(line, description=f"case JSONL line {line_number}")
        if not isinstance(record, dict) or set(record) != _CASE_KEYS:
            raise ValueError(f"case schema validation failed at line {line_number}")
        try:
            validator.validate(record)
        except ValidationError as exc:
            raise ValueError(
                f"case schema validation failed at line {line_number}"
            ) from exc
        records.append(record)

    # 清单同时冻结 split 全集和 case 到候选的映射，集合与逐条身份必须完全一致。
    split_case_ids = manifest["case_ids"].get(split)
    candidate_map = manifest["case_candidate_map"]
    if not isinstance(split_case_ids, list):
        raise ValueError("case identity manifest is invalid")
    expected_ids = {
        case_id
        for case_id in split_case_ids
        if candidate_map.get(case_id) == candidate
    }
    actual_ids = [record["case_id"] for record in records]
    identity_valid = (
        len(actual_ids) == len(set(actual_ids))
        and set(actual_ids) == expected_ids
        and all(
            record["candidate"] == candidate
            and record["split"] == split
            and candidate_map.get(record["case_id"]) == candidate
            for record in records
        )
    )
    if not identity_valid:
        raise ValueError("case identity does not match phase13-v2 manifest")
    # baseline 与 Agent 会复用同一批 case；深冻结阻止调用方在校验后改写嵌套输入。
    return tuple(_freeze_json(record) for record in records)

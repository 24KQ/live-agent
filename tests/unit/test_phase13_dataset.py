"""Phase 13 Task 6：240 例脱敏配对数据集与冻结 Manifest 验收。"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import re
from decimal import Decimal
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from evaluation.generators.generate_phase13_cases import (
    CANDIDATES,
    SEED,
    SPLIT_COUNTS,
    generate_phase13_dataset,
)
from src.specialist_evaluation.models import EvaluationManifest
from src.specialist_evaluation.manifest_authorization import calculate_source_code_digest
from src.specialist_runtime.models import EvidenceRef, SpecialistTaskKind
from src.specialist_runtime.profiles import SpecialistProfile


ROOT = Path(__file__).parents[2]
EVALUATION_ROOT = ROOT / "evaluation"
MANIFEST_PATH = EVALUATION_ROOT / "manifests" / "phase13-v2.json"
SCHEMA_PATH = EVALUATION_ROOT / "schemas" / "phase13_case.schema.json"
PRICING_SNAPSHOT_PATH = (
    EVALUATION_ROOT / "pricing" / "deepseek-v4-flash-2026-07-16.json"
)
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _jsonl(path: Path) -> tuple[dict, ...]:
    """按 UTF-8 严格读取 JSONL，空行不能被静默忽略。"""

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and all(line for line in lines)
    return tuple(json.loads(line) for line in lines)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest_anchor(root: Path) -> str:
    """读取由调用方在进程外冻结的 Manifest 摘要。"""

    manifest = json.loads((root / "manifests/phase13-v2.json").read_text(encoding="utf-8"))
    return manifest["manifest_digest"]


def _refresh_case_digest(root: Path, candidate: str, split: str) -> None:
    """仅刷新测试副本中的目标摘要，以便分别触发 Schema 和身份校验层。"""

    relative = f"cases/phase13/{candidate}-{split}.jsonl"
    manifest_path = root / "manifests" / "phase13-v2.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["artifact_digests"][relative] = _sha256(root / relative)
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_digest"}
    manifest["manifest_digest"] = hashlib.sha256(
        (json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
    ).hexdigest()
    manifest_path.write_text(
        json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def test_phase13_dataset_has_exact_candidate_splits_and_unique_ids() -> None:
    """三个候选必须各自拥有 20/40/20，且 240 个 ID 全局唯一。"""

    all_ids: list[str] = []
    for candidate in CANDIDATES:
        for split, expected_count in SPLIT_COUNTS.items():
            path = EVALUATION_ROOT / "cases" / "phase13" / f"{candidate}-{split}.jsonl"
            records = _jsonl(path)
            assert len(records) == expected_count
            assert all(record["candidate"] == candidate for record in records)
            assert all(record["split"] == split for record in records)
            assert [record["case_id"] for record in records] == sorted(
                record["case_id"] for record in records
            )
            all_ids.extend(record["case_id"] for record in records)

    assert len(all_ids) == 240
    assert len(set(all_ids)) == 240


def test_phase13_v3_formal_baseline_uses_live_ops_v3_without_rewriting_v2() -> None:
    """D-110 的修正 LiveOps 资产必须进入新基线，旧 v2 仍保留为审计历史。"""

    v2 = json.loads((EVALUATION_ROOT / "manifests" / "phase13-v2.json").read_text(encoding="utf-8"))
    v3 = json.loads((EVALUATION_ROOT / "manifests" / "phase13-v3.json").read_text(encoding="utf-8"))
    live_v3 = json.loads(
        (EVALUATION_ROOT / "manifests" / "phase13-live-ops-v3.json").read_text(encoding="utf-8")
    )

    assert v2["manifest_id"] == "phase13-v2"
    assert v3["manifest_id"] == "phase13-v3"
    for split in SPLIT_COUNTS:
        live_case_ids = [
            case_id
            for case_id in v3["case_ids"][split]
            if v3["case_candidate_map"][case_id] == "live_ops"
        ]
        assert live_case_ids == live_v3["case_ids"][split]
        assert all("phase13-live_ops" not in case_id for case_id in live_case_ids)


def test_cases_and_labels_are_strict_valid_and_physically_separated() -> None:
    """Prompt 输入不含 gold label；标签只允许 evaluator 从独立目录读取。"""

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    case_validator = Draft202012Validator(schema)
    label_validator = Draft202012Validator(schema["$defs"]["label"])
    forbidden_keys = {"label", "labels", "gold", "expected", "expected_output"}
    sensitive_pattern = re.compile(
        r"(?i)(api[_-]?key|authorization|cookie|password|phone|mobile|id[_-]?card)"
    )

    for candidate in CANDIDATES:
        for split in SPLIT_COUNTS:
            cases = _jsonl(
                EVALUATION_ROOT / "cases" / "phase13" / f"{candidate}-{split}.jsonl"
            )
            labels = _jsonl(
                EVALUATION_ROOT / "labels" / "phase13" / f"{candidate}-{split}.jsonl"
            )
            assert {item["case_id"] for item in cases} == {
                item["case_id"] for item in labels
            }
            for record in cases:
                case_validator.validate(record)
                assert forbidden_keys.isdisjoint(record)
                assert not sensitive_pattern.search(
                    json.dumps(record, ensure_ascii=False, sort_keys=True)
                )
                for evidence in record["input"].get("evidence_refs", ()):
                    EvidenceRef.model_validate(evidence)
                for evidence in record["input"].get("decision_traces", ()):
                    EvidenceRef.model_validate(evidence)
            for label in labels:
                label_validator.validate(label)


def test_generator_is_byte_stable_and_matches_committed_assets(tmp_path: Path) -> None:
    """同一 seed 生成两次必须逐字节相等，并与仓库冻结资产一致。"""

    first = tmp_path / "first"
    second = tmp_path / "second"
    generate_phase13_dataset(first)
    generate_phase13_dataset(second)

    first_files = tuple(
        sorted(path.relative_to(first) for path in first.rglob("*") if path.is_file())
    )
    second_files = tuple(
        sorted(path.relative_to(second) for path in second.rglob("*") if path.is_file())
    )
    assert first_files == second_files
    for relative in first_files:
        assert (first / relative).read_bytes() == (second / relative).read_bytes()
        assert (first / relative).read_bytes() == (EVALUATION_ROOT / relative).read_bytes()

    changed_seed = tmp_path / "changed-seed"
    generate_phase13_dataset(changed_seed, seed=SEED + 1)
    assert (
        first / "cases" / "phase13" / "live_ops-holdout.jsonl"
    ).read_bytes() != (
        changed_seed / "cases" / "phase13" / "live_ops-holdout.jsonl"
    ).read_bytes()


def test_manifest_binds_every_artifact_and_official_price_snapshot() -> None:
    """Manifest 必须冻结模型、价格、Prompt、Schema、数据与逐文件摘要。"""

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert manifest["manifest_id"] == "phase13-v2"
    assert manifest["manifest_version"] == "2.0.0"
    assert manifest["seed"] == 20260716
    assert manifest["endpoint_host"] == "api.deepseek.com"
    assert manifest["model_id"] == "deepseek-v4-flash"
    assert manifest["temperature"] == 0
    assert manifest["development_real_smoke_limit_per_candidate"] == 5
    assert manifest["holdout_label_access"] == "EVALUATOR_ONLY"
    assert manifest["external_anchor_policy"] == "GIT_COMMIT_REQUIRED"

    pricing_snapshot = json.loads(PRICING_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert pricing_snapshot == {
        "citation_excerpt": (
            "1M INPUT TOKENS (CACHE MISS): $0.14; 1M OUTPUT TOKENS: $0.28"
        ),
        "conversion_policy": {
            "policy_version": "usd-cny-fixed-7.2-v1",
            "rounding": "ROUND_HALF_EVEN_TO_6_DECIMALS",
            "usd_to_cny_rate": "7.200000",
        },
        "observed_on": "2026-07-16",
        "official_prices_usd_per_million_tokens": {
            "cache_miss_input": "0.140000",
            "output": "0.280000",
        },
        "source_currency": "USD",
        "source_url": "https://api-docs.deepseek.com/quick_start/pricing",
    }
    assert manifest["pricing_source_digest"] == _sha256(PRICING_SNAPSHOT_PATH)

    pricing = manifest["pricing"]
    assert pricing == {
        "cache_miss_input_cny_per_million": "1.008000",
        "cache_miss_input_usd_per_million": "0.140000",
        "conversion_policy_version": "usd-cny-fixed-7.2-v1",
        "currency": "CNY",
        "observed_on": "2026-07-16",
        "output_cny_per_million": "2.016000",
        "output_usd_per_million": "0.280000",
        "source_currency": "USD",
        "source_url": "https://api-docs.deepseek.com/quick_start/pricing",
        "usd_to_cny_rate": "7.200000",
    }
    assert all(HASH_PATTERN.fullmatch(value) for key, value in manifest.items() if key.endswith("_digest"))

    listed = manifest["artifact_digests"]
    assert listed
    for relative, digest in listed.items():
        assert HASH_PATTERN.fullmatch(digest)
        assert _sha256(EVALUATION_ROOT / relative) == digest

    case_ids = manifest["case_ids"]
    assert len(case_ids["development"]) == 60
    assert len(case_ids["validation"]) == 120
    assert len(case_ids["holdout"]) == 60
    assert len(manifest["case_candidate_map"]) == 240

    store_manifest = EvaluationManifest.model_validate(manifest["store_manifest"])
    assert store_manifest.manifest_id == manifest["manifest_id"]
    assert store_manifest.manifest_digest == manifest["store_manifest"]["manifest_digest"]
    assert store_manifest.temperature == Decimal("0")
    expected_skill_versions = {
        "live_ops": {
            "aggregate_danmaku_questions": "1.0.0",
            "generate_danmaku_reply": "1.0.0",
            "generate_on_live_prompt": "1.0.0",
            "on_live_context_collect": "1.0.0",
            "recommend_backup_product": "1.0.0",
        },
        "planner": {},
        "review_memory": {
            "calculate_post_live_attribution": "1.0.0",
            "collect_post_live_evidence": "1.0.0",
            "stage_memory_candidates": "1.0.0",
        },
    }
    expected_catalog_availability = {
        "live_ops": {skill_id: True for skill_id in expected_skill_versions["live_ops"]},
        "planner": {},
        "review_memory": {
            skill_id: False for skill_id in expected_skill_versions["review_memory"]
        },
    }
    assert manifest["formal_execution_preflight"] == {
        "required": True,
        "task": "PHASE_13_TASK_11",
        "verifies": "FROZEN_SKILL_VERSIONS_AVAILABLE_IN_CURRENT_CATALOG",
    }
    for candidate in CANDIDATES:
        profile = manifest["profiles"][candidate]
        assert profile["profile_version"] == "1.0.0"
        assert profile["prompt_version"] == "1"
        assert profile["result_schema_version"] == "1"
        assert profile["max_model_calls"] >= 2
        assert profile["max_total_tokens"] in {4000, 8000}
        assert profile["deadline_seconds"] in {5, 15, 20}
        assert profile["max_case_cost_cny"]
        assert profile["skill_versions"] == expected_skill_versions[candidate]
        assert (
            profile["current_catalog_availability"]
            == expected_catalog_availability[candidate]
        )
        assert set(profile["skill_versions"]) == set(profile["allowed_skill_ids"])
        result_schema = json.loads(
            (EVALUATION_ROOT / profile["result_schema_path"]).read_text(encoding="utf-8")
        )
        # 使用 Manifest 绑定的 Prompt 原文构造当前 Runtime Profile，不绕过其必填契约。
        prompt_text = (EVALUATION_ROOT / profile["prompt_path"]).read_text(
            encoding="utf-8"
        )
        assert prompt_text.endswith("\n")
        assert profile["prompt_text"] == prompt_text
        assert "AgentAction" in prompt_text
        assert '"kind":"FINAL"' in prompt_text
        assert json.dumps(
            result_schema, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ) in prompt_text
        assert hashlib.sha256(profile["prompt_text"].encode("utf-8")).hexdigest() == profile[
            "prompt_digest"
        ]
        runtime_profile = SpecialistProfile(
            profile_id=profile["profile_id"],
            profile_version=profile["profile_version"],
            task_kind=SpecialistTaskKind(profile["task_kind"]),
            model_id=profile["model_id"],
            endpoint_host=profile["endpoint_host"],
            temperature=Decimal(profile["temperature"]),
            prompt_text=profile["prompt_text"],
            prompt_hash=profile["prompt_digest"],
            result_schema_hash=profile["result_schema_digest"],
            result_schema=result_schema,
            allowed_skill_ids=tuple(profile["allowed_skill_ids"]),
            skill_versions=profile["skill_versions"],
            max_model_calls=profile["max_model_calls"],
            max_skill_calls=profile["max_skill_calls"],
            max_total_tokens=profile["max_total_tokens"],
            deadline_seconds=profile["deadline_seconds"],
            max_case_cost_cny=Decimal(profile["max_case_cost_cny"]),
        )
        assert runtime_profile.profile_id == profile["profile_id"]
    # 源码闭包必须由目录发现得到精确全集；新增 Python 文件时不能依赖人工补名单。
    expected_source_paths = {
        path.relative_to(ROOT).as_posix()
        for source_root in (ROOT / "src", EVALUATION_ROOT)
        for path in source_root.rglob("*.py")
    }
    assert set(manifest["source_artifact_digests"]) == expected_source_paths
    assert calculate_source_code_digest(ROOT) == manifest["code_digest"]
    for relative, digest in manifest["source_artifact_digests"].items():
        assert HASH_PATTERN.fullmatch(digest)
        raw = (ROOT / relative).read_text(encoding="utf-8-sig")
        normalized = raw.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        assert hashlib.sha256(normalized).hexdigest() == digest


def test_holdout_case_loader_never_returns_evaluator_labels() -> None:
    """候选侧 Loader 对 holdout 也只能返回冻结输入，不能暴露 evaluator label。"""

    import evaluation.case_loader as case_loader

    # 候选侧源码连受审计答案目录的命名都不应出现，避免后续维护误接触该资产。
    assert "label" not in inspect.getsource(case_loader).lower()
    for candidate in CANDIDATES:
        records = case_loader.load_case_inputs(
            EVALUATION_ROOT,
            candidate,
            "holdout",
            expected_manifest_digest=_manifest_anchor(EVALUATION_ROOT),
        )
        assert len(records) == 20
        assert all(set(record) == {"case_id", "candidate", "split", "input"} for record in records)
        with pytest.raises(TypeError):
            records[0]["input"]["forged"] = True


def test_production_code_cannot_bypass_public_formal_manifest_preflight() -> None:
    """内部签发器只允许 Git 预检模块调用，其他生产代码不得直接导入。"""

    allowed = {
        ROOT / "src" / "specialist_evaluation" / "models.py",
        ROOT / "src" / "specialist_evaluation" / "manifest_authorization.py",
    }
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if path not in allowed
        and "_build_formal_manifest_authorization" in path.read_text(encoding="utf-8-sig")
    ]
    assert offenders == []


def test_case_loader_fails_closed_on_digest_schema_and_identity_tampering(
    tmp_path: Path,
) -> None:
    """Loader 必须自动消费 v2 Manifest，并分层拒绝字节、结构和身份篡改。"""

    from evaluation.case_loader import load_case_inputs

    digest_root = tmp_path / "digest"
    generate_phase13_dataset(digest_root)
    digest_case_path = digest_root / "cases/phase13/live_ops-holdout.jsonl"
    digest_case_path.write_bytes(digest_case_path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="digest"):
        load_case_inputs(
            digest_root,
            "live_ops",
            "holdout",
            expected_manifest_digest=_manifest_anchor(digest_root),
        )

    schema_root = tmp_path / "schema"
    generate_phase13_dataset(schema_root)
    schema_case_path = schema_root / "cases/phase13/live_ops-holdout.jsonl"
    schema_records = list(_jsonl(schema_case_path))
    schema_records[0]["input"]["inventory_alert"]["forged_control"] = True
    schema_case_path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for record in schema_records
        ),
        encoding="utf-8",
        newline="\n",
    )
    _refresh_case_digest(schema_root, "live_ops", "holdout")
    with pytest.raises(ValueError, match="schema"):
        load_case_inputs(
            schema_root,
            "live_ops",
            "holdout",
            expected_manifest_digest=_manifest_anchor(schema_root),
        )

    identity_root = tmp_path / "identity"
    generate_phase13_dataset(identity_root)
    identity_case_path = identity_root / "cases/phase13/live_ops-holdout.jsonl"
    identity_records = list(_jsonl(identity_case_path))
    identity_records[0]["case_id"] = "phase13-live_ops-holdout-999"
    identity_case_path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for record in identity_records
        ),
        encoding="utf-8",
        newline="\n",
    )
    _refresh_case_digest(identity_root, "live_ops", "holdout")
    with pytest.raises(ValueError, match="identity"):
        load_case_inputs(
            identity_root,
            "live_ops",
            "holdout",
            expected_manifest_digest=_manifest_anchor(identity_root),
        )

    anchor_root = tmp_path / "anchor"
    generate_phase13_dataset(anchor_root)
    with pytest.raises(ValueError, match="manifest anchor"):
        load_case_inputs(
            anchor_root,
            "live_ops",
            "holdout",
            expected_manifest_digest="0" * 64,
        )


def test_schema_rejects_nested_unknown_fields_and_candidate_label_mismatch() -> None:
    """严格 Schema 必须封闭嵌套输入，并把 label 形状绑定到候选身份。"""

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    case_validator = Draft202012Validator(schema)
    label_validator = Draft202012Validator(schema["$defs"]["label"])
    live_case = dict(
        _jsonl(EVALUATION_ROOT / "cases" / "phase13" / "live_ops-development.jsonl")[0]
    )
    live_case["input"] = dict(live_case["input"])
    live_case["input"]["inventory_alert"] = dict(
        live_case["input"]["inventory_alert"], forged_control=True
    )
    assert tuple(case_validator.iter_errors(live_case))

    live_label = dict(
        _jsonl(EVALUATION_ROOT / "labels" / "phase13" / "live_ops-development.jsonl")[0]
    )
    live_label["label"] = {
        "expected_node_keys": ["PREPARE_CARD_BATCH"],
        "executable": True,
        "constraint_recovery_required": False,
        "constraint_recovery": True,
    }
    assert tuple(label_validator.iter_errors(live_label))

    planner_case = _jsonl(
        EVALUATION_ROOT / "cases" / "phase13" / "planner-development.jsonl"
    )[0]
    mismatched = dict(planner_case, case_id="phase13-live_ops-development-001")
    assert tuple(case_validator.iter_errors(mismatched))


def test_splits_do_not_repeat_semantically_equivalent_inputs() -> None:
    """去除 case 身份后，各 split 仍不得复制相同业务输入。"""

    def scrub(value, case_id: str):
        if isinstance(value, dict):
            return {key: scrub(item, case_id) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item, case_id) for item in value]
        if isinstance(value, str):
            return value.replace(case_id, "<CASE>")
        return value

    for candidate in CANDIDATES:
        fingerprints: set[str] = set()
        for split in SPLIT_COUNTS:
            for record in _jsonl(
                EVALUATION_ROOT / "cases" / "phase13" / f"{candidate}-{split}.jsonl"
            ):
                normalized = scrub(record["input"], record["case_id"])
                digest = hashlib.sha256(
                    json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                assert digest not in fingerprints
                fingerprints.add(digest)


def test_result_schemas_reject_governance_field_injection() -> None:
    """冻结结果 Schema 必须拒绝授权字段、自由文本记忆和开放依赖对象。"""

    valid_results = {
        "live_ops": {
            "action": "NO_ACTION",
            "reason_code": "SAFE",
            "suggestion": "none",
            "evidence_refs": [
                {
                    "kind": "AUDIT",
                    "evidence_id": "e1",
                    "source_version": "1",
                    "digest": "a" * 64,
                }
            ],
        },
        "planner": {
            "nodes": [{"logical_key": "n1", "capability": "generate_product_card"}],
            "dependencies": [{"from": "n1", "to": "n1"}],
            "bindings": [],
        },
        "review_memory": {
            "attribution": {
                "category": "inventory",
                "reason_code": "STOCK_SIGNAL",
                "evidence_ids": ["e1", "e2"],
            },
            "memory_candidates": [
                {
                    "class": "APPLY",
                    "product_id": "sim-product-review-001",
                    "category": "inventory",
                    "tag": "inventory",
                    "evidence_ids": ["e1", "e2"],
                }
            ],
            "evidence_ids": ["e1", "e2"],
        },
    }
    injections = {
        "live_ops": ((), "authorization", "forged"),
        "planner": (("dependencies", 0), "forged_control", True),
        "review_memory": (("memory_candidates", 0), "raw_private_text", "secret"),
    }
    for candidate, valid_payload in valid_results.items():
        schema = json.loads(
            (EVALUATION_ROOT / "result_schemas" / "phase13" / f"{candidate}-v1.json").read_text(
                encoding="utf-8"
            )
        )
        validator = Draft202012Validator(schema)
        validator.validate(valid_payload)

        # 每次只注入一个治理字段，确保失败不是由缺字段或类型错误造成的假阳性。
        payload = copy.deepcopy(valid_payload)
        container_path, field_name, field_value = injections[candidate]
        container = payload
        for segment in container_path:
            container = container[segment]
        container[field_name] = field_value
        errors = tuple(validator.iter_errors(payload))
        assert len(errors) == 1
        assert errors[0].validator == "additionalProperties"
        assert tuple(errors[0].absolute_path) == container_path

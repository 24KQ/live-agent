"""Phase 11A Skill Catalog 测试。

测试覆盖：默认 Catalog 包含 14 个 Skill、ID 唯一、版本固定、
Schema 合法性、9 个未迁移工具严格一致、4 个核心工具的 Schema
差异受控且记录 compatibility_note、ToolRegistry 兼容投影。
"""

from __future__ import annotations

import hashlib
import json

import pytest
from jsonschema import Draft202012Validator, ValidationError as JsonSchemaError

from src.skill_runtime.catalog import get_default_skill_catalog, validate_manifests
from src.skill_runtime.models import SkillManifest

CORE_SKILLS = frozenset({
    "query_products",
    "generate_live_plan",
    "generate_product_card",
    "setup_live_session",
})

FROZEN_NON_CORE_METADATA_HASHES = {
    "aggregate_danmaku_questions": "8b4c4d02ff7895a555eaf82d3f09c9dbad25dd15fd41bf9b36eaecdda0b6cad7",
    "create_live_plan_draft": "ba0638ea351ea0dc58820da3e06214a67dfc12445438ab5d39cb2eabe9de0a75",
    "generate_danmaku_reply": "be8f8449906f2fb68910c2836c0c551ea24ed066b6a749e0f745a46e61abf1f6",
    "generate_on_live_prompt": "4adfc3356a0ef922a328c02279cf618418304a490c9923d81669bc16084cc971",
    "on_live_context_collect": "c57bd502ac5d2c4f9a8e105dabc390ed8c7899cdb521d9931b0d00e2a004f1c9",
    "recommend_backup_product": "d45b7f40be744b4d19d7332d247efd1bf3c115b1a1089ab1ba7a74129fa96e98",
    "suggest_price_change": "c64994732b2a184bdc104b0809d6d218b42eed29c7408210113b7caf90dee977",
}

# 改价与售罄写分别因显式 CAS、可信事件授权升级公开契约；它们不再属于 Phase 11A
# 冻结的未迁移元数据集合，其余七项仍必须逐字段保持不变。
VERSIONED_SKILLS = frozenset({"set_product_price", "handle_sold_out_event"})
PHASE13_SKILLS = frozenset({"retrieve_anchor_memory"})


def _manifest(skill_id: str) -> SkillManifest:
    """按 ID 读取默认 Manifest，测试失败时给出清晰上下文。"""
    return next(item for item in get_default_skill_catalog() if item.skill_id == skill_id)


def _product_snapshot() -> dict:
    """返回与 CatalogProduct.model_dump(mode='json') 一致的完整快照。"""
    return {
        "product_id": "p001",
        "name": "测试商品",
        "category": "日用",
        "price": "39.90",
        "inventory": 10,
        "conversion_rate": "0.1500",
        "commission_rate": "0.0500",
        "tags": ["引流"],
        "selling_points": ["测试卖点"],
        "is_active": True,
    }


def test_default_catalog_contains_14_skills() -> None:
    """默认 Catalog 必须包含原有 13 个能力和 Phase 13 记忆读取 Skill。"""
    catalog = get_default_skill_catalog()
    assert len(catalog) == 14


def test_all_skill_ids_are_unique() -> None:
    """所有 skill_id 必须唯一。"""
    catalog = get_default_skill_catalog()
    ids = [m.skill_id for m in catalog]
    assert len(ids) == len(set(ids))


def test_catalog_has_twelve_v1_skills_and_two_versioned_writes() -> None:
    """新增记忆读取保持 1.0.0，改价与售罄写继续使用升级后的单活版本。"""
    versions = {manifest.skill_id: manifest.version for manifest in get_default_skill_catalog()}

    assert list(versions.values()).count("1.0.0") == 12
    assert versions["set_product_price"] == "1.1.0"
    assert versions["handle_sold_out_event"] == "2.0.0"


def test_sold_out_v2_schema_and_authorization_exclude_control_fields() -> None:
    """售罄写只接收 CAS 业务参数，事件、房间、trace 和幂等身份必须留在 Context。"""
    from src.skill_runtime.models import AuthorizationRequirement

    manifest = _manifest("handle_sold_out_event")
    assert manifest.parameter_schema == {
        "type": "object",
        "required": ["product_id", "expected_version"],
        "properties": {
            "product_id": {"type": "string", "minLength": 1},
            "expected_version": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": False,
    }
    assert manifest.requires_idempotency_key is True
    assert manifest.authorization_requirement is (
        AuthorizationRequirement.TRUSTED_EVENT_OR_HUMAN
    )


def test_all_schemas_are_valid_draft202012() -> None:
    """所有 Manifest 的 parameter_schema 必须能够通过 Draft 2020-12 校验。"""
    catalog = get_default_skill_catalog()
    for manifest in catalog:
        schema = manifest.parameter_schema
        if not schema:
            # 空 schema 也被视为合法（无参数工具）
            continue
        try:
            Draft202012Validator.check_schema(schema)
        except JsonSchemaError as exc:
            pytest.fail(f"{manifest.skill_id} schema invalid: {exc}")


def test_all_skill_schemas_reject_undeclared_root_arguments() -> None:
    """13 个 Skill 的根参数对象都必须 fail-closed，不能静默放过额外业务字段。"""

    for manifest in get_default_skill_catalog():
        assert manifest.parameter_schema.get("additionalProperties") is False, manifest.skill_id


def test_non_core_skills_strict_match_frozen_metadata() -> None:
    """剩余 7 个未迁移工具的字段必须与冻结 ToolMetadata 完全一致。"""
    manifests = {item.skill_id: item for item in get_default_skill_catalog()}
    assert set(FROZEN_NON_CORE_METADATA_HASHES) == (
        set(manifests) - CORE_SKILLS - VERSIONED_SKILLS - PHASE13_SKILLS
    )

    for skill_id, expected_hash in FROZEN_NON_CORE_METADATA_HASHES.items():
        manifest = manifests[skill_id]
        payload = {
            "description": manifest.description,
            "lifecycle": sorted(item.value for item in manifest.lifecycle),
            "risk_level": manifest.risk_level.value,
            "parameter_schema": manifest.parameter_schema,
            "gate_decision": manifest.gate_decision.value,
            "requires_idempotency_key": manifest.requires_idempotency_key,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        assert hashlib.sha256(encoded).hexdigest() == expected_hash, skill_id
        assert manifest.compatibility_note is None


def test_only_schema_migration_skills_have_compatibility_notes() -> None:
    """兼容说明只允许用于四个旧核心能力和 Phase 13 新增的脱敏读取契约。"""
    catalog = get_default_skill_catalog()
    for manifest in catalog:
        if manifest.skill_id in CORE_SKILLS | PHASE13_SKILLS:
            assert manifest.compatibility_note is not None, (
                f"{manifest.skill_id} should have compatibility_note describing schema delta"
            )
        else:
            assert manifest.compatibility_note is None, (
                f"{manifest.skill_id} should not have compatibility_note"
            )


def test_tool_registry_contains_same_13_names() -> None:
    """ToolRegistry 兼容投影必须返回相同的有序 13 个名称。"""
    from src.config.tool_registry import get_default_tool_registry

    registry = get_default_tool_registry()
    catalog = get_default_skill_catalog()
    registry_names = registry.tool_names()
    catalog_ids = sorted(m.skill_id for m in catalog)
    assert registry_names == catalog_ids


def test_core_skill_arguments_exclude_trusted_context_fields() -> None:
    """room_id 等可信上下文字段不得再次出现在四个核心 Skill arguments 中。"""
    for skill_id in CORE_SKILLS:
        properties = _manifest(skill_id).parameter_schema.get("properties", {})
        assert "room_id" not in properties, f"{skill_id} 泄漏了可信 room_id 到业务参数"
        assert "trace_id" not in properties, f"{skill_id} 泄漏了可信 trace_id 到业务参数"
        assert "idempotency_key" not in properties, f"{skill_id} 泄漏了幂等键到业务参数"


def test_price_schema_requires_explicit_resource_version() -> None:
    """改价 Schema 必须显式约束 CAS 版本，且不接受执行控制字段。"""
    schema = _manifest("set_product_price").parameter_schema

    assert schema == {
        "type": "object",
        "required": ["product_id", "price", "expected_version"],
        "properties": {
            "product_id": {"type": "string"},
            "price": {"type": "string", "pattern": r"^[0-9]+(?:\.[0-9]+)?$"},
            "expected_version": {"type": "integer", "minimum": 1},
        },
        "additionalProperties": False,
    }


@pytest.mark.parametrize("invalid_price", ["Infinity", "-0.01", "NaN", "1e2", ""])
def test_price_schema_rejects_non_decimal_or_negative_values(invalid_price: str) -> None:
    """高风险改价必须在 Runtime Schema 层拒绝非有限值、负数和指数写法。"""
    validator = Draft202012Validator(_manifest("set_product_price").parameter_schema)

    with pytest.raises(JsonSchemaError):
        validator.validate(
            {"product_id": "p001", "price": invalid_price, "expected_version": 1}
        )


def test_product_snapshot_schema_accepts_full_catalog_product_and_rejects_unknown_fields() -> None:
    """商品快照必须容纳完整 CatalogProduct，同时拒绝未声明字段。"""
    product = _product_snapshot()
    plan_validator = Draft202012Validator(_manifest("generate_live_plan").parameter_schema)
    card_validator = Draft202012Validator(_manifest("generate_product_card").parameter_schema)

    plan_validator.validate({"products": [product]})
    card_validator.validate({"product": product})

    with pytest.raises(JsonSchemaError):
        card_validator.validate({"product": {**product, "unexpected": "value"}})


def test_setup_schema_accepts_real_live_plan_snapshot() -> None:
    """建播输入必须匹配项目真实 LivePlanDraft 快照，而不是另一套虚构计划结构。"""
    snapshot = {
        "room_id": "room-001",
        "trace_id": "trace-001",
        "items": [
            {
                "rank": 1,
                "product_id": "p001",
                "product_name": "测试商品",
                "role": "引流款",
                "reason": "测试原因",
            }
        ],
    }
    Draft202012Validator(_manifest("setup_live_session").parameter_schema).validate(
        {"plan": snapshot}
    )


def test_catalog_validation_rejects_duplicate_ids() -> None:
    """Catalog 构造时发现重复 Skill ID 必须 fail-fast。"""
    manifest = _manifest("query_products")
    with pytest.raises(ValueError, match="重复 skill_id"):
        validate_manifests([manifest, manifest])


def test_catalog_validation_rejects_invalid_schema() -> None:
    """Catalog 构造时发现非法 Draft 2020-12 Schema 必须 fail-fast。"""
    invalid = _manifest("query_products").model_copy(
        update={"parameter_schema": {"type": "not-a-json-schema-type"}}
    )
    with pytest.raises(ValueError, match="schema"):
        validate_manifests([invalid])


def test_catalog_validation_rejects_empty_version() -> None:
    """即使输入绕过 Pydantic 字段校验，Catalog 也必须拒绝空版本。"""
    invalid = SkillManifest.model_construct(
        skill_id="invalid-version",
        version="",
        description="测试",
        lifecycle=set(),
        parameter_schema={},
    )
    with pytest.raises(ValueError, match="version"):
        validate_manifests([invalid])

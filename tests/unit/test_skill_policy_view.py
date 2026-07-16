"""Phase 12B SkillPolicyView 只读治理投影契约测试。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import importlib
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from src.skill_runtime.catalog import get_default_skill_catalog


def _policy_module() -> Any:
    """延迟导入待实现模块，让 RED 以明确断言而不是收集错误呈现。"""
    try:
        return importlib.import_module("src.skill_runtime.policy_view")
    except ModuleNotFoundError:
        pytest.fail("尚未实现 Phase 12B SkillPolicyView", pytrace=False)


def _view() -> Any:
    """从默认 Catalog 创建启动冻结的策略视图。"""
    return _policy_module().SkillPolicyView(get_default_skill_catalog())


def test_policy_view_projects_all_catalog_governance_fields() -> None:
    """策略视图必须完整投影执行治理字段，不能形成第二份手写元数据。"""
    manifests = {item.skill_id: item for item in get_default_skill_catalog()}
    view = _view()

    assert view.skill_ids() == tuple(sorted(manifests))
    for skill_id, manifest in manifests.items():
        policy = view.get(skill_id)
        assert policy.skill_id == manifest.skill_id
        assert policy.version == manifest.version
        assert policy.lifecycle == manifest.lifecycle
        assert policy.risk_level == manifest.risk_level
        assert policy.parameter_schema == manifest.parameter_schema
        assert policy.gate_decision == manifest.gate_decision
        assert policy.requires_idempotency_key == manifest.requires_idempotency_key
        assert policy.authorization_requirement == manifest.authorization_requirement


def test_policy_view_is_a_frozen_snapshot_not_a_live_sequence_alias() -> None:
    """调用方改写来源列表后，已装配视图仍必须保持原 Catalog 快照。"""
    source = list(get_default_skill_catalog())
    view = _policy_module().SkillPolicyView(source)
    expected_ids = view.skill_ids()

    source.clear()

    assert view.skill_ids() == expected_ids
    assert len(expected_ids) == 14
    with pytest.raises((AttributeError, FrozenInstanceError)):
        view._policies = {}


def test_policy_and_nested_schema_are_read_only() -> None:
    """策略对象及嵌套 Schema 都不能在进程运行中被原地修改。"""
    policy = _view().get("generate_product_card")

    with pytest.raises((AttributeError, FrozenInstanceError, TypeError, ValidationError)):
        policy.version = "9.9.9"
    with pytest.raises(TypeError):
        policy.parameter_schema["additionalProperties"] = True
    with pytest.raises(TypeError):
        policy.parameter_schema["properties"]["product"] = {"type": "string"}


def test_unknown_skill_is_rejected_by_a_stable_policy_error() -> None:
    """未知能力必须 fail-closed，不能返回空策略或隐式 AUTO。"""
    module = _policy_module()

    with pytest.raises(module.SkillPolicyNotFoundError):
        _view().get("missing-skill")


def test_default_policy_view_factory_uses_current_catalog_versions() -> None:
    """默认工厂必须在装配时钉住当前 Catalog 的精确单活版本。"""
    module = _policy_module()
    expected = {
        item.skill_id: item.version for item in get_default_skill_catalog()
    }
    actual = {
        skill_id: module.get_default_skill_policy_view().get(skill_id).version
        for skill_id in module.get_default_skill_policy_view().skill_ids()
    }

    assert actual == expected


def test_authorization_requirements_expose_versioned_sold_out_runtime() -> None:
    """Task 6 原子切换后，PolicyView 必须暴露售罄 2.0.0 与可信授权要求。"""
    from src.skill_runtime.models import AuthorizationRequirement

    view = _view()
    assert view.get("setup_live_session").authorization_requirement is (
        AuthorizationRequirement.HUMAN_APPROVAL
    )
    assert view.get("set_product_price").authorization_requirement is (
        AuthorizationRequirement.HUMAN_APPROVAL
    )
    assert view.get("handle_sold_out_event").version == "2.0.0"
    assert view.get("handle_sold_out_event").authorization_requirement is (
        AuthorizationRequirement.TRUSTED_EVENT_OR_HUMAN
    )


def test_policy_view_lifecycle_query_uses_frozen_manifest_rules() -> None:
    """生命周期查询必须复用 Manifest 集合，并对未知能力保持 fail-closed。"""
    from src.state.models import LifecycleStage

    view = _view()
    assert view.is_available("generate_product_card", LifecycleStage.PRE_LIVE) is True
    assert view.is_available("generate_product_card", LifecycleStage.ON_LIVE) is False
    with pytest.raises(_policy_module().SkillPolicyNotFoundError):
        view.is_available("missing-skill", LifecycleStage.PRE_LIVE)


def test_production_code_no_longer_imports_tool_registry_facade() -> None:
    """Phase 14 删除 Facade 前，生产消费者必须先全部迁往策略视图。"""

    source_root = Path(__file__).resolve().parents[2] / "src"
    facade = source_root / "config" / "tool_registry.py"
    forbidden_imports: list[str] = []
    for path in source_root.rglob("*.py"):
        if path == facade:
            continue
        content = path.read_text(encoding="utf-8")
        if (
            "from src.config.tool_registry" in content
            or "import src.config.tool_registry" in content
        ):
            forbidden_imports.append(str(path.relative_to(source_root)))

    assert forbidden_imports == []

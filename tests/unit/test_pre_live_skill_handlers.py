"""Phase 11A 播前核心 Handler 测试。

测试覆盖 handler 注册、四个 Handler 的业务逻辑、
显式幂等键覆盖（而不是自动替换为 trace 默认值）。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.skill_runtime.executor import register_handler
from src.skill_runtime.models import (
    ApprovalContext,
    SkillCall,
    SkillExecutionContext,
    SkillExecutionRoute,
    SkillExecutionStatus,
    _build_human_interrupt_approval,
)


# ── 辅助：清空注册表并重新注册 ──────────────────────────────────────


@pytest.fixture(autouse=True)
def _register_handlers() -> None:
    """每次测试前重新注册 Handler，确保测试隔离。"""
    # 注意：注册表是全局的，这里通过导入来确保 Handler 已注册
    # 不重置已经注册的 Handler，因为它们是幂等的
    import src.skill_runtime.pre_live_handlers  # noqa: F401


def _build_call(
    skill_id: str,
    args: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    approval: ApprovalContext | None = None,
) -> SkillCall:
    """构建测试用 SkillCall。"""
    ctx = SkillExecutionContext(
        room_id="room_1",
        trace_id="trace_1",
        lifecycle="PRE_LIVE",
        execution_route=SkillExecutionRoute.SKILL_RUNTIME,
        idempotency_key=idempotency_key,
        approval=approval,
    )
    return SkillCall(skill_id=skill_id, version="1.0.0", context=ctx, arguments=args or {})


# ── 辅助：从 Catalog 获取第一个测试用商品快照 ──────────────────────


def _sample_product(override: dict | None = None) -> dict:
    """返回模拟商品快照（匹配 CatalogProduct 必需字段）。"""
    base = {
        "product_id": "p1",
        "name": "测试商品",
        "category": "美妆",
        "price": "99.00",
        "inventory": 100,
        "conversion_rate": "0.1200",
        "commission_rate": "0.0800",
        "tags": ["测试"],
        "selling_points": ["测试卖点"],
        "is_active": True,
    }
    if override:
        base.update(override)
    return base


def _sample_products() -> list[dict]:
    """返回模拟商品货盘。"""
    return [
        _sample_product({"product_id": "p002", "name": "商品A", "price": "39.90"}),
        _sample_product({"product_id": "p005", "name": "商品B", "price": "29.90"}),
        _sample_product({"product_id": "p008", "name": "商品C", "price": "19.90"}),
    ]

# ── query_products ──────────────────────────────────────────────────


def test_query_products_returns_products() -> None:
    """query_products 返回商品列表。"""
    from src.skill_runtime.executor import SyncSkillExecutorAdapter

    executor = SyncSkillExecutorAdapter()
    call = _build_call("query_products")
    result = executor.execute(call)
    assert result.status == SkillExecutionStatus.SUCCESS
    assert result.output is not None
    assert "products" in result.output
    assert isinstance(result.output["products"], list)


# ── generate_live_plan ──────────────────────────────────────────────


def test_generate_live_plan_requires_products() -> None:
    """generate_live_plan 需要 products 参数。"""
    from src.skill_runtime.executor import SyncSkillExecutorAdapter

    executor = SyncSkillExecutorAdapter()
    # 缺少 products 参数
    call = _build_call("generate_live_plan")
    result = executor.execute(call)
    assert result.status == SkillExecutionStatus.ERROR
    assert result.error_code.name == "INVALID_ARGUMENTS"


def test_generate_live_plan_succeeds() -> None:
    """generate_live_plan 成功返回排品计划。"""
    from src.skill_runtime.executor import SyncSkillExecutorAdapter

    executor = SyncSkillExecutorAdapter()
    call = _build_call(
        "generate_live_plan",
        args={"products": _sample_products()},
    )
    result = executor.execute(call)
    assert result.status == SkillExecutionStatus.SUCCESS
    assert result.output is not None
    assert "plan" in result.output
    assert "items" in result.output["plan"]


# ── generate_product_card ────────────────────────────────────────────


def test_generate_product_card_requires_product() -> None:
    """generate_product_card 需要 product 参数。"""
    from src.skill_runtime.executor import SyncSkillExecutorAdapter

    executor = SyncSkillExecutorAdapter()
    # 缺少 product 参数
    call = _build_call("generate_product_card")
    result = executor.execute(call)
    assert result.status == SkillExecutionStatus.ERROR
    assert result.error_code.name == "INVALID_ARGUMENTS"


def test_generate_product_card_succeeds() -> None:
    """generate_product_card 成功返回单商品手卡。"""
    from src.skill_runtime.executor import SyncSkillExecutorAdapter

    executor = SyncSkillExecutorAdapter()
    call = _build_call(
        "generate_product_card",
        args={"product": _sample_product()},
    )
    result = executor.execute(call)
    assert result.status == SkillExecutionStatus.SUCCESS
    assert result.output is not None
    assert "card" in result.output


# ── setup_live_session ────────────────────────────────────────────────


def test_setup_live_session_without_approval_is_pending() -> None:
    """setup_live_session 缺审批时返回 pending。"""
    from src.skill_runtime.executor import SyncSkillExecutorAdapter

    executor = SyncSkillExecutorAdapter()
    call = _build_call(
        "setup_live_session",
        args={
            "plan": {
                "room_id": "room_1",
                "trace_id": "trace_1",
                "items": [{"rank": 1, "product_id": "p1", "product_name": "测试商品", "role": "引流款", "reason": "测试"}],
            },
        },
        # 本用例只验证缺审批语义；固定执行顺序要求先提供幂等键，才能到达审批检查。
        idempotency_key="key_setup_without_approval",
    )
    result = executor.execute(call)
    assert result.status == SkillExecutionStatus.PENDING
    assert result.error_code.name == "APPROVAL_REQUIRED"


def test_setup_live_session_with_approval_succeeds() -> None:
    """setup_live_session 批准后成功建播。"""
    from src.skill_runtime.executor import SyncSkillExecutorAdapter

    executor = SyncSkillExecutorAdapter()
    approval = _build_human_interrupt_approval(
        decision="APPROVED",
        operator_id="test_operator",
        approval_audit_id="aud_setup_001",
    )
    call = _build_call(
        "setup_live_session",
        args={
            "plan": {
                "room_id": "room_1",
                "trace_id": "trace_1",
                "items": [{"rank": 1, "product_id": "p1", "product_name": "测试商品", "role": "引流款", "reason": "测试"}],
            },
        },
        idempotency_key="key_setup_2",
        approval=approval,
    )
    result = executor.execute(call)
    assert result.status == SkillExecutionStatus.SUCCESS
    assert result.output is not None
    assert result.output.get("setup_status") == "prepared"
    assert result.audit_id is not None
    assert "audit_id" not in result.output


def test_setup_live_session_rejected() -> None:
    """setup_live_session 拒绝后返回 error。"""
    from src.skill_runtime.executor import SyncSkillExecutorAdapter

    executor = SyncSkillExecutorAdapter()
    rejection = _build_human_interrupt_approval(
        decision="REJECTED",
        operator_id="test_operator",
        approval_audit_id="aud_rej_002",
    )
    call = _build_call(
        "setup_live_session",
        args={
            "plan": {
                "room_id": "room_1",
                "trace_id": "trace_1",
                "items": [{"rank": 1, "product_id": "p1", "product_name": "测试商品", "role": "引流款", "reason": "测试"}],
            },
        },
        idempotency_key="key_setup_3",
        approval=rejection,
    )
    result = executor.execute(call)
    assert result.status == SkillExecutionStatus.ERROR
    assert result.error_code.name == "APPROVAL_REJECTED"


def test_build_pre_live_handlers_returns_instance_local_mappings() -> None:
    """不同 Facade 的 Handler 映射不得共享实例或覆盖彼此 service。"""
    from src.skill_runtime.pre_live_handlers import build_pre_live_handlers

    first_service = object()
    second_service = object()
    first = build_pre_live_handlers(first_service)  # type: ignore[arg-type]
    second = build_pre_live_handlers(second_service)  # type: ignore[arg-type]

    assert first is not second
    assert set(first) == set(second) == {
        "query_products",
        "generate_live_plan",
        "generate_product_card",
        "setup_live_session",
    }
    assert all(first[skill_id] is not second[skill_id] for skill_id in first)

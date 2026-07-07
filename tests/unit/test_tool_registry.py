"""工具注册表测试。

工具注册表是 Agent 可执行能力的白名单。未注册工具、生命周期不匹配、
风险等级不明确，都应该在执行前被挡住。
"""

import pytest

from src.config.tool_registry import ToolNotFoundError, get_default_tool_registry
from src.core.security_hooks import GateDecision
from src.state.models import LifecycleStage, RiskLevel


def test_default_registry_contains_phase1_pre_live_tools() -> None:
    """Phase 1 必须注册四个播前工具。"""

    registry = get_default_tool_registry()

    assert set(registry.tool_names()) == {
        "query_products",
        "suggest_price_change",
        "set_product_price",
        "create_live_plan_draft",
    }


def test_set_product_price_is_hard_gate_pre_live_tool() -> None:
    """执行改价属于高风险播前写操作，必须 hard-gate。"""

    registry = get_default_tool_registry()
    metadata = registry.get("set_product_price")

    assert metadata.lifecycle == {LifecycleStage.PRE_LIVE}
    assert metadata.risk_level == RiskLevel.HIGH
    assert metadata.gate_decision == GateDecision.HARD_GATE
    assert metadata.requires_idempotency_key is True


def test_registry_rejects_unknown_tool() -> None:
    """未知工具不能落入默认执行路径。"""

    registry = get_default_tool_registry()

    with pytest.raises(ToolNotFoundError):
        registry.get("unknown_tool")


def test_query_products_is_only_available_in_pre_live() -> None:
    """播前查询货盘工具不能在播中或播后误用。"""

    registry = get_default_tool_registry()

    assert registry.is_available("query_products", LifecycleStage.PRE_LIVE) is True
    assert registry.is_available("query_products", LifecycleStage.ON_LIVE) is False

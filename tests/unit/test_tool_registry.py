"""工具注册表测试。

工具注册表是 Agent 可执行能力的白名单。未注册工具、生命周期不匹配、
风险等级不明确，都应该在执行前被挡住。
"""

import pytest

from src.config.tool_registry import ToolNotFoundError, get_default_tool_registry
from src.core.security_hooks import GateDecision
from src.state.models import LifecycleStage, RiskLevel


def test_default_registry_contains_pre_live_tools() -> None:
    """工具注册表必须同时包含 Phase 1 地基层和 Phase 2A 播前业务工具。"""

    registry = get_default_tool_registry()

    assert set(registry.tool_names()) == {
        "query_products",
        "suggest_price_change",
        "set_product_price",
        "create_live_plan_draft",
        "generate_live_plan",
        "generate_product_card",
        "setup_live_session",
        "handle_sold_out_event",
        "recommend_backup_product",
        "generate_on_live_prompt",
        "aggregate_danmaku_questions",
        "generate_danmaku_reply",
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


def test_setup_live_session_requires_confirmation_and_idempotency() -> None:
    """模拟建播属于播前写入动作，必须带幂等键并经过 hard-gate 确认。"""

    registry = get_default_tool_registry()
    metadata = registry.get("setup_live_session")

    assert metadata.lifecycle == {LifecycleStage.PRE_LIVE}
    assert metadata.risk_level == RiskLevel.HIGH
    assert metadata.gate_decision == GateDecision.HARD_GATE
    assert metadata.requires_idempotency_key is True


def test_on_live_tools_are_only_available_during_on_live() -> None:
    """Phase 2B 播中工具只能在 ON_LIVE 阶段可用。"""

    registry = get_default_tool_registry()
    metadata = registry.get("handle_sold_out_event")

    assert registry.is_available("handle_sold_out_event", LifecycleStage.ON_LIVE) is True
    assert registry.is_available("handle_sold_out_event", LifecycleStage.PRE_LIVE) is False
    assert metadata.risk_level == RiskLevel.HIGH
    assert metadata.gate_decision == GateDecision.AUTO
    assert metadata.requires_idempotency_key is True


def test_on_live_prompt_tool_is_low_risk_readonly_tool() -> None:
    """播中提示生成不直接改状态，应保持低风险自动执行。"""

    registry = get_default_tool_registry()
    metadata = registry.get("generate_on_live_prompt")

    assert metadata.lifecycle == {LifecycleStage.ON_LIVE}
    assert metadata.risk_level == RiskLevel.LOW
    assert metadata.gate_decision == GateDecision.AUTO
    assert metadata.requires_idempotency_key is False


def test_danmaku_tools_are_only_available_during_on_live() -> None:
    """Phase 2C 弹幕工具只能在 ON_LIVE 阶段使用。"""

    registry = get_default_tool_registry()
    aggregate_metadata = registry.get("aggregate_danmaku_questions")
    reply_metadata = registry.get("generate_danmaku_reply")

    assert registry.is_available("aggregate_danmaku_questions", LifecycleStage.ON_LIVE) is True
    assert registry.is_available("aggregate_danmaku_questions", LifecycleStage.PRE_LIVE) is False
    assert aggregate_metadata.risk_level == RiskLevel.LOW
    assert aggregate_metadata.gate_decision == GateDecision.AUTO

    assert registry.is_available("generate_danmaku_reply", LifecycleStage.ON_LIVE) is True
    assert reply_metadata.risk_level == RiskLevel.MEDIUM
    assert reply_metadata.gate_decision == GateDecision.SOFT_GATE
    assert reply_metadata.requires_idempotency_key is False

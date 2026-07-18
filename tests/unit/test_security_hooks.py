"""安全 Hook 测试。

安全 Hook 是工具调用前的硬边界。测试覆盖 auto、soft-gate、hard-gate、
block 四类策略，确保高风险动作不会绕过主播确认。
"""

from dataclasses import dataclass
from typing import Any

from src.core.security_hooks import GateDecision, evaluate_tool_gate
from src.state.models import LifecycleStage, RiskLevel


@dataclass(frozen=True)
class HookPolicyFixture:
    """只为 Security Hook 提供最小属性协议，不重新引入生产注册表类型。"""

    name: str
    description: str
    lifecycle: set[LifecycleStage]
    risk_level: RiskLevel
    parameter_schema: dict[str, Any]
    gate_decision: GateDecision
    requires_idempotency_key: bool


def make_tool(name: str, gate: GateDecision, risk: RiskLevel = RiskLevel.LOW) -> HookPolicyFixture:
    """构造最小治理策略对象，验证 Hook 不依赖 Facade 实现。"""

    return HookPolicyFixture(
        name=name,
        description="测试工具",
        lifecycle={LifecycleStage.PRE_LIVE},
        risk_level=risk,
        parameter_schema={"type": "object"},
        gate_decision=gate,
        requires_idempotency_key=False,
    )


def test_auto_gate_allows_read_only_tool() -> None:
    """auto 工具应直接允许执行。"""

    result = evaluate_tool_gate(make_tool("query_products", GateDecision.AUTO), confirmed=False)

    assert result.allowed is True
    assert result.decision == GateDecision.AUTO


def test_soft_gate_allows_with_notice() -> None:
    """soft-gate 工具允许执行，但必须带提示信息。"""

    result = evaluate_tool_gate(make_tool("suggest_price_change", GateDecision.SOFT_GATE), confirmed=False)

    assert result.allowed is True
    assert result.requires_confirmation is False
    assert "提示" in result.reason


def test_hard_gate_blocks_without_confirmation() -> None:
    """hard-gate 未确认时必须拦截执行。"""

    result = evaluate_tool_gate(make_tool("set_product_price", GateDecision.HARD_GATE, RiskLevel.HIGH), confirmed=False)

    assert result.allowed is False
    assert result.requires_confirmation is True
    assert result.decision == GateDecision.HARD_GATE


def test_hard_gate_allows_after_confirmation() -> None:
    """hard-gate 确认后才允许继续执行。"""

    result = evaluate_tool_gate(make_tool("set_product_price", GateDecision.HARD_GATE, RiskLevel.HIGH), confirmed=True)

    assert result.allowed is True
    assert result.requires_confirmation is False


def test_block_gate_always_rejects() -> None:
    """block 工具即使确认也不能执行。"""

    result = evaluate_tool_gate(make_tool("dangerous_tool", GateDecision.BLOCK, RiskLevel.CRITICAL), confirmed=True)

    assert result.allowed is False
    assert result.decision == GateDecision.BLOCK

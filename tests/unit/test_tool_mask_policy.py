"""Phase 3A 工具可见性策略测试。"""

from decimal import Decimal

from src.core.security_hooks import GateDecision
from src.memory.tool_mask_policy import ToolMaskPolicy
from src.skill_runtime.policy_view import get_default_skill_policy_view
from src.state.models import LifecycleStage


def test_high_trust_anchor_can_see_all_non_block_tools() -> None:
    """trust_score >= 0.7 时，主播可看到当前生命周期内所有非 block 工具。"""

    policy = ToolMaskPolicy(get_default_skill_policy_view())

    visible = policy.visible_tools(Decimal("0.70"), LifecycleStage.PRE_LIVE)

    assert "query_products" in visible
    assert "generate_live_plan" in visible
    assert "setup_live_session" in visible


def test_medium_trust_anchor_can_only_see_auto_and_soft_gate_tools() -> None:
    """0.4 <= trust_score < 0.7 时，高风险 hard-gate 工具不可见。"""

    policy = ToolMaskPolicy(get_default_skill_policy_view())

    visible = policy.visible_tools(Decimal("0.55"), LifecycleStage.PRE_LIVE)

    assert "query_products" in visible
    assert "generate_live_plan" in visible
    assert "setup_live_session" not in visible
    assert all(
        policy.policy_view.get(tool_name).gate_decision
        in {GateDecision.AUTO, GateDecision.SOFT_GATE}
        for tool_name in visible
    )


def test_exact_medium_boundary_keeps_soft_gate_tools_visible() -> None:
    """trust_score 等于 0.40 时仍属于中信任区间，应保留 soft-gate 工具。"""

    policy = ToolMaskPolicy(get_default_skill_policy_view())

    visible = policy.visible_tools(Decimal("0.40"), LifecycleStage.PRE_LIVE)

    assert "generate_live_plan" in visible
    assert "setup_live_session" not in visible


def test_low_trust_anchor_can_only_see_auto_tools() -> None:
    """trust_score < 0.4 时，只保留自动低风险路径，降低误操作半径。"""

    policy = ToolMaskPolicy(get_default_skill_policy_view())

    visible = policy.visible_tools(Decimal("0.39"), LifecycleStage.PRE_LIVE)

    # 记忆读取是 LOW/AUTO 的播前只读能力，与货盘查询一样可在低信任阶段暴露。
    assert visible == ["query_products", "retrieve_anchor_memory"]

"""Phase 3A trust_score 更新规则测试。"""

from decimal import Decimal

import pytest

from src.memory.models import AnchorAction, BusinessResult, TrustState
from src.memory.trust_manager import TrustManager


@pytest.mark.parametrize(
    ("anchor_action", "business_result", "expected_delta"),
    [
        (AnchorAction.ACCEPTED, BusinessResult.GOOD, Decimal("0.05")),
        (AnchorAction.ACCEPTED, BusinessResult.BAD, Decimal("-0.10")),
        (AnchorAction.REJECTED, BusinessResult.AGENT_RIGHT, Decimal("0.03")),
        (AnchorAction.REJECTED, BusinessResult.ANCHOR_RIGHT, Decimal("-0.05")),
    ],
)
def test_trust_manager_calculates_deterministic_delta(
    anchor_action: AnchorAction,
    business_result: BusinessResult,
    expected_delta: Decimal,
) -> None:
    """四种主播反馈组合必须映射到固定 trust_delta，保证测试和审计可复现。"""

    assert TrustManager.calculate_delta(anchor_action, business_result) == expected_delta


def test_trust_manager_updates_and_clamps_score() -> None:
    """trust_score 更新后必须钳制到 0.0-1.0，避免长期累计后越界。"""

    manager = TrustManager()

    high_update = manager.apply_feedback(
        TrustState(anchor_id="anchor-001", trust_score=Decimal("0.98")),
        AnchorAction.ACCEPTED,
        BusinessResult.GOOD,
    )
    low_update = manager.apply_feedback(
        TrustState(anchor_id="anchor-001", trust_score=Decimal("0.03")),
        AnchorAction.ACCEPTED,
        BusinessResult.BAD,
    )

    assert high_update.new_state.trust_score == Decimal("1.00")
    assert high_update.trust_delta == Decimal("0.05")
    assert low_update.new_state.trust_score == Decimal("0.00")
    assert low_update.trust_delta == Decimal("-0.10")


def test_trust_manager_rejects_invalid_feedback_combo() -> None:
    """采纳建议却传入 agent_right 这类拒绝场景结果时，应明确拒绝。"""

    with pytest.raises(ValueError, match="feedback"):
        TrustManager.calculate_delta(AnchorAction.ACCEPTED, BusinessResult.AGENT_RIGHT)

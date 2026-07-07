"""Phase 3A trust_score 确定性更新器。

本阶段不使用模型预测，也不根据模糊文本判断反馈，只采用项目计划里明确的四条规则。
这样每一次 trust_score 变化都可以由 Decision Trace 回放解释，便于后续合规评审。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from src.memory.models import AnchorAction, BusinessResult, TrustState


@dataclass(frozen=True)
class TrustUpdate:
    """trust_score 更新结果。

    trust_delta 保留“本次规则给出的原始变化量”，new_state 保存钳制后的最终状态。
    """

    old_state: TrustState
    new_state: TrustState
    trust_delta: Decimal


class TrustManager:
    """按固定规则更新主播维度 trust_score。"""

    _RULES: dict[tuple[AnchorAction, BusinessResult], Decimal] = {
        (AnchorAction.ACCEPTED, BusinessResult.GOOD): Decimal("0.05"),
        (AnchorAction.ACCEPTED, BusinessResult.BAD): Decimal("-0.10"),
        (AnchorAction.REJECTED, BusinessResult.AGENT_RIGHT): Decimal("0.03"),
        (AnchorAction.REJECTED, BusinessResult.ANCHOR_RIGHT): Decimal("-0.05"),
    }

    @classmethod
    def calculate_delta(cls, anchor_action: AnchorAction, business_result: BusinessResult) -> Decimal:
        """根据反馈组合返回固定 trust_delta。

        未列入白名单的组合会被拒绝，例如“主播采纳建议”不应搭配“agent_right”，因为
        agent_right 表示主播拒绝后事后证明 Agent 更准。
        """

        key = (anchor_action, business_result)
        if key not in cls._RULES:
            raise ValueError(f"unsupported feedback combination: {anchor_action}/{business_result}")
        return cls._RULES[key]

    def apply_feedback(
        self,
        state: TrustState,
        anchor_action: AnchorAction,
        business_result: BusinessResult,
    ) -> TrustUpdate:
        """应用一次主播反馈，并把最终 trust_score 钳制到 0.00-1.00。"""

        delta = self.calculate_delta(anchor_action, business_result)
        raw_score = state.trust_score + delta
        clamped_score = min(Decimal("1.00"), max(Decimal("0.00"), raw_score)).quantize(Decimal("0.01"))
        new_state = state.model_copy(
            update={
                "trust_score": clamped_score,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        return TrustUpdate(old_state=state, new_state=new_state, trust_delta=delta)

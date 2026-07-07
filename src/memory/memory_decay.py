"""Phase 3B 记忆衰减策略。

本模块只做确定性权重计算，不访问数据库、不调用 LLM。这样检索、排品和冲突修正
都可以复用同一套衰减规则，并通过单元测试稳定验证。
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from src.memory.models import AnchorMemoryEntry, MemoryLayer, MemoryStatus


class MemoryDecayPolicy:
    """按层级、新鲜度和状态计算记忆有效权重。"""

    _HALF_LIFE_DAYS = {
        MemoryLayer.L1: Decimal("365"),
        MemoryLayer.L2: Decimal("180"),
        MemoryLayer.L3: Decimal("90"),
    }
    _SUPPRESSED_FACTOR = Decimal("0.10")

    def effective_weight(
        self,
        memory: AnchorMemoryEntry,
        *,
        reference_time: datetime | None = None,
    ) -> Decimal:
        """计算单条记忆在当前时刻的有效权重。

        基础权重来自 `confidence * evidence_weight`；新鲜度使用简单半衰期曲线，
        让旧记忆逐步变弱但不突然归零；suppressed 记忆保留 10% 影响力用于审计回放，
        防止旧偏好继续主导排品。
        """

        current_time = reference_time or datetime.now(timezone.utc)
        created_at = memory.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)

        age_days = Decimal(max((current_time - created_at).total_seconds(), 0)) / Decimal("86400")
        half_life = self._HALF_LIFE_DAYS[memory.layer]
        freshness_factor = half_life / (half_life + age_days)
        status_factor = self._SUPPRESSED_FACTOR if memory.status == MemoryStatus.SUPPRESSED else Decimal("1.00")
        weight = memory.confidence * memory.evidence_weight * freshness_factor * status_factor
        return weight.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

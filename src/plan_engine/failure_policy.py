"""Phase 12A 集中失败恢复策略。

Skill、Handler 和 Adapter 只报告已经发生的 ``FailureFact``；本模块结合节点能力、
尝试次数和绝对 deadline 决定恢复动作。它不执行 Store 写入、Skill 调用或 Graph
跳转，避免失败事实生产者越权决定重试和人工处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from hashlib import sha256

from src.plan_engine.capabilities import ResolvedPlanCapability
from src.plan_engine.store import PlanStoreInvariantError
from src.skill_runtime.models import FailureCategory, FailureFact


class FailureAction(StrEnum):
    """Phase 12A Worker 能执行的受控恢复动作。"""

    RETRY = "RETRY"
    WAIT_HUMAN = "WAIT_HUMAN"
    SKIP = "SKIP"
    FAIL_PLAN = "FAIL_PLAN"


@dataclass(frozen=True)
class FailureDecision:
    """一次确定性策略判定；只有 RETRY 可以携带下一次尝试时间。"""

    action: FailureAction
    retry_at: datetime | None = None


class FailurePolicy:
    """只读手卡节点的确定性重试与人工处理矩阵。

    ``attempt_number`` 表示刚刚失败的总尝试序号，因此值为 3 时已经耗尽“最多三次
    尝试”的预算。Phase 12A 不实现 Replan，不能恢复的失败统一收敛为 FAIL_PLAN。
    """

    MAX_READ_ATTEMPTS = 3

    def decide(
        self,
        *,
        failure: FailureFact,
        capability: ResolvedPlanCapability,
        attempt_number: int,
        deadline_at: datetime,
        now: datetime,
    ) -> FailureDecision:
        """根据结构化失败事实返回唯一恢复动作，不产生任何副作用。"""
        normalized_now = self._aware_utc(now, "策略判定时间")
        normalized_deadline = self._aware_utc(deadline_at, "节点 deadline")
        if type(attempt_number) is not int or attempt_number < 1:
            raise PlanStoreInvariantError("attempt_number 必须是大于等于 1 的精确 int")

        if failure.category is FailureCategory.SIDE_EFFECT_UNKNOWN:
            return FailureDecision(FailureAction.WAIT_HUMAN)

        retryable_read = (
            capability.node_type == "SKILL"
            and capability.skill_id == "generate_product_card"
            and failure.category
            in {FailureCategory.TRANSIENT_INFRA, FailureCategory.RATE_LIMITED}
        )
        if not retryable_read or attempt_number >= self.MAX_READ_ATTEMPTS:
            return FailureDecision(FailureAction.FAIL_PLAN)

        delay = self._retry_delay(failure, attempt_number)
        retry_at = normalized_now + delay
        required_attempt_seconds = capability.max_attempt_seconds or 0
        if retry_at + timedelta(seconds=required_attempt_seconds) >= normalized_deadline:
            return FailureDecision(FailureAction.FAIL_PLAN)
        return FailureDecision(FailureAction.RETRY, retry_at=retry_at)

    @staticmethod
    def _retry_delay(failure: FailureFact, attempt_number: int) -> timedelta:
        """限流优先服从 Retry-After，其余使用指数退避与确定性毫秒抖动。"""
        if (
            failure.category is FailureCategory.RATE_LIMITED
            and failure.retry_after_seconds is not None
        ):
            return timedelta(seconds=failure.retry_after_seconds)
        base_seconds = 2 ** (attempt_number - 1)
        seed = "\x1f".join(
            (failure.external_code, failure.attempt_id, str(attempt_number))
        ).encode("utf-8")
        jitter_milliseconds = int(sha256(seed).hexdigest()[:8], 16) % 1000
        return timedelta(
            seconds=base_seconds,
            milliseconds=jitter_milliseconds,
        )

    @staticmethod
    def _aware_utc(value: datetime, field_name: str) -> datetime:
        """拒绝无时区时间，避免 deadline 和退避在不同时区下产生分歧。"""
        if value.tzinfo is None or value.utcoffset() is None:
            raise PlanStoreInvariantError(f"{field_name}必须包含时区")
        return value.astimezone(timezone.utc)

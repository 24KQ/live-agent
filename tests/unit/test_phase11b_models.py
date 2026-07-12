"""Phase 11B 统一执行模型测试。

本文件只固定 FailureFact、可信 deadline 和 Manifest 尝试上限的公共契约，
不涉及 Attempt Store、Adapter 或执行器行为，避免第一批测试跨越后续任务边界。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from src.skill_runtime.catalog import get_default_skill_catalog
from src.skill_runtime.models import (
    FailureCategory,
    FailureFact,
    SideEffectState,
    SkillExecutionContext,
    SkillExecutionRoute,
)
from src.state.models import LifecycleStage


def test_failure_fact_is_frozen_and_has_no_recovery_action() -> None:
    """失败事实只能说明已经发生的结果，不能在模型中夹带恢复决策。"""
    fact = FailureFact(
        category=FailureCategory.RATE_LIMITED,
        external_code="fake.rate_limited",
        side_effect_state=SideEffectState.NOT_SENT,
        attempt_id="attempt-001",
        retry_after_seconds=3,
    )

    assert fact.category == FailureCategory.RATE_LIMITED
    assert fact.retry_after_seconds == 3
    with pytest.raises(ValidationError):
        fact.category = FailureCategory.TRANSIENT_INFRA  # type: ignore[misc]


def test_context_rejects_naive_deadline() -> None:
    """deadline 必须带时区，防止不同机器把本地时间解释为不同的执行预算。"""
    with pytest.raises(ValidationError, match="timezone"):
        SkillExecutionContext(
            room_id="room-001",
            trace_id="trace-001",
            lifecycle=LifecycleStage.PRE_LIVE,
            execution_route=SkillExecutionRoute.SKILL_RUNTIME,
            deadline_at=datetime(2026, 7, 12, 10, 0, 0),
        )


def test_all_manifests_have_phase11b_attempt_cap() -> None:
    """所有首版 Skill 必须显式拥有一致的单次尝试上限，避免运行时隐式无限等待。"""
    manifests = get_default_skill_catalog()

    assert len(manifests) == 13
    assert {manifest.max_attempt_seconds for manifest in manifests} == {15}

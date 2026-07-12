"""Phase 11A Skill Runtime 模型测试。

测试覆盖：枚举合法性、ApprovalContext 的信任边界、
SkillCall 不可变性、SkillExecutionResult 状态控制。
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_human_approval_requires_operator_and_audit_evidence() -> None:
    """人工批准缺少操作员或审批审计时必须 fail-closed。"""
    from src.skill_runtime.models import ApprovalContext, ApprovalSource

    with pytest.raises(ValidationError):
        ApprovalContext(source=ApprovalSource.HUMAN_INTERRUPT, decision="APPROVED")


def test_skill_call_is_immutable() -> None:
    """调用开始后不得替换路由或版本。"""
    from src.skill_runtime.models import SkillCall, SkillExecutionContext, SkillExecutionRoute, ApprovalContext, ApprovalSource

    ctx = SkillExecutionContext(
        room_id="room_1",
        trace_id="trace_1",
        lifecycle="PRE_LIVE",
        execution_route=SkillExecutionRoute.LEGACY,
        approval=ApprovalContext(
            source=ApprovalSource.TRUSTED_COMPAT,
            decision="APPROVED",
            operator_id="system",
            approval_audit_id="compat_001",
        ),
    )
    call = SkillCall(skill_id="test", version="1.0.0", context=ctx, arguments={})
    with pytest.raises(ValidationError):
        call.context.execution_route = SkillExecutionRoute.SKILL_RUNTIME


def test_skill_execution_result_status_controlled() -> None:
    """结果状态必须属于受控枚举，非法状态被拒绝。"""
    from src.skill_runtime.models import SkillExecutionResult, SkillExecutionStatus

    result = SkillExecutionResult(
        skill_id="test",
        version="1.0.0",
        status=SkillExecutionStatus.SUCCESS,
        output={"key": "value"},
        summary="ok",
        audit_id="audit_001",
    )
    assert result.status == SkillExecutionStatus.SUCCESS

    with pytest.raises(ValidationError):
        SkillExecutionResult(
            skill_id="test",
            version="1.0.0",
            status="unknown",
            output={},
            summary="bad status",
            audit_id=None,
        )


def test_route_enum_rejects_unknown() -> None:
    """路由枚举拒绝未知值。"""
    from src.skill_runtime.models import SkillExecutionRoute

    assert SkillExecutionRoute.LEGACY.value == "LEGACY"
    assert SkillExecutionRoute.SKILL_RUNTIME.value == "SKILL_RUNTIME"
    with pytest.raises(ValueError):
        SkillExecutionRoute("SHADOW_COMPARE")


def test_approval_context_trusted_compat_accepts_missing_operator() -> None:
    """TRUSTED_COMPAT 来源允许缺少 operator_id（兼容场景），但 HUMAN_INTERRUPT 不行。"""
    from src.skill_runtime.models import ApprovalContext, ApprovalSource

    # TRUSTED_COMPAT 可以缺 operator_id（由兼容适配器提供回调上下文）
    ctx = ApprovalContext(
        source=ApprovalSource.TRUSTED_COMPAT,
        decision="APPROVED",
        operator_id="compat_migration",
        approval_audit_id="aud_001",
    )
    assert ctx.source == ApprovalSource.TRUSTED_COMPAT
    assert ctx.operator_id == "compat_migration"

    # HUMAN_INTERRUPT 缺 operator_id 时必 fail
    with pytest.raises(ValidationError):
        ApprovalContext(
            source=ApprovalSource.HUMAN_INTERRUPT,
            decision="APPROVED",
            approval_audit_id="aud_001",
        )

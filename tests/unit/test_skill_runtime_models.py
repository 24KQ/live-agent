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
            source=ApprovalSource.HUMAN_INTERRUPT,
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


def test_human_interrupt_requires_operator() -> None:
    """HUMAN_INTERRUPT 缺少 operator_id 时必须 fail-closed。"""
    from src.skill_runtime.models import ApprovalContext, ApprovalSource

    with pytest.raises(ValidationError):
        ApprovalContext(
            source=ApprovalSource.HUMAN_INTERRUPT,
            decision="APPROVED",
            approval_audit_id="aud_001",
        )


def test_trusted_compat_only_accepts_approved_decision() -> None:
    """直接构造拒绝的兼容来源必须失败。"""
    from src.skill_runtime.models import ApprovalContext, ApprovalSource

    with pytest.raises(ValidationError):
        ApprovalContext(
            source=ApprovalSource.TRUSTED_COMPAT,
            decision="REJECTED",
            operator_id="compat_migration",
            approval_audit_id="audit-rejected",
        )


def test_approval_decision_rejects_uncontrolled_text() -> None:
    """审批决定必须是结构化枚举值，不能接收任意自然语言。"""
    from src.skill_runtime.models import ApprovalContext, ApprovalSource

    with pytest.raises(ValidationError):
        ApprovalContext(
            source=ApprovalSource.HUMAN_INTERRUPT,
            decision="looks good",
            operator_id="operator-001",
            approval_audit_id="audit-001",
        )


def test_trusted_compat_cannot_use_public_constructor() -> None:
    """普通调用方不能直接伪造 TRUSTED_COMPAT 审批来源。"""
    from src.skill_runtime.models import ApprovalContext, ApprovalSource

    with pytest.raises(ValidationError):
        ApprovalContext(
            source=ApprovalSource.TRUSTED_COMPAT,
            decision="APPROVED",
            operator_id="caller",
            approval_audit_id="forged-audit",
        )


def test_skill_call_arguments_and_manifest_schema_are_deeply_immutable() -> None:
    """调用开始后不能原地修改业务参数，启动后也不能修改 Manifest Schema。"""
    from src.skill_runtime.catalog import get_default_skill_catalog
    from src.skill_runtime.models import SkillCall, SkillExecutionContext, SkillExecutionRoute

    call = SkillCall(
        skill_id="generate_live_plan",
        version="1.0.0",
        context=SkillExecutionContext(
            room_id="room-001",
            trace_id="trace-001",
            lifecycle="PRE_LIVE",
            execution_route=SkillExecutionRoute.SKILL_RUNTIME,
        ),
        arguments={"products": [{"product_id": "p001"}]},
    )
    manifest = next(
        item for item in get_default_skill_catalog() if item.skill_id == "query_products"
    )

    with pytest.raises(TypeError):
        call.arguments["products"] = []
    with pytest.raises(TypeError):
        call.arguments["products"][0]["product_id"] = "p002"
    with pytest.raises(TypeError):
        manifest.parameter_schema["additionalProperties"] = True


def test_skill_call_and_result_reject_non_json_values() -> None:
    """Runtime 边界拒绝不可持久化或可变的非 JSON 值。"""
    from src.skill_runtime.models import (
        SkillCall,
        SkillExecutionContext,
        SkillExecutionResult,
        SkillExecutionRoute,
        SkillExecutionStatus,
    )

    context = SkillExecutionContext(
        room_id="room-001",
        trace_id="trace-001",
        lifecycle="PRE_LIVE",
        execution_route=SkillExecutionRoute.SKILL_RUNTIME,
    )
    with pytest.raises((TypeError, ValidationError, ValueError)):
        SkillCall(
            skill_id="query_products",
            version="1.0.0",
            context=context,
            arguments={"payload": bytearray(b"mutable")},
        )
    with pytest.raises((TypeError, ValidationError, ValueError)):
        SkillExecutionResult(
            skill_id="query_products",
            version="1.0.0",
            status=SkillExecutionStatus.SUCCESS,
            output={"value": float("nan")},
        )

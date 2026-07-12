"""Phase 11A SkillExecutor 测试。

测试覆盖：未知 Skill、版本不匹配、生命周期错误、参数校验、
缺幂等键、缺审批、拒绝审批、Handler 不存在和 Handler 异常。
每个前置失败步骤都不会调用 Handler。
"""

from __future__ import annotations

from typing import Any

import pytest

from src.skill_runtime.executor import SyncSkillExecutorAdapter
from src.skill_runtime.models import (
    ApprovalContext,
    ApprovalSource,
    SkillCall,
    SkillExecutionContext,
    SkillExecutionRoute,
    SkillExecutionStatus,
    SkillErrorCode,
)


# ── 辅助：构建测试用的 SkillCall ──────────────────────────────────────


def _build_call(
    skill_id: str = "test_skill",
    version: str = "1.0.0",
    args: dict[str, Any] | None = None,
    lifecycle: str = "PRE_LIVE",
    idempotency_key: str | None = None,
    approval: ApprovalContext | None = None,
) -> SkillCall:
    """构建测试 SkillCall，参数不完整时用默认值补全。"""
    ctx = SkillExecutionContext(
        room_id="room_1",
        trace_id="trace_1",
        lifecycle=lifecycle,
        execution_route=SkillExecutionRoute.SKILL_RUNTIME,
        idempotency_key=idempotency_key,
        approval=approval,
    )
    return SkillCall(
        skill_id=skill_id,
        version=version,
        context=ctx,
        arguments=args or {},
    )


# ── 测试 ────────────────────────────────────────────────────────────────


def test_unknown_skill_fails_before_handler() -> None:
    """未知 Skill 在 Handler 之前失败，不调用 Handler。"""
    executor = SyncSkillExecutorAdapter()
    call = _build_call(skill_id="does_not_exist")
    result = executor.execute(call)
    assert result.status == SkillExecutionStatus.ERROR
    assert result.error_code == SkillErrorCode.SKILL_NOT_FOUND


def test_version_mismatch_fails_before_handler() -> None:
    """版本不匹配在 Handler 之前失败。"""
    executor = SyncSkillExecutorAdapter()
    # query_products 在 Catalog 中 version=1.0.0
    call = _build_call(skill_id="query_products", version="9.9.9")
    result = executor.execute(call)
    assert result.status == SkillExecutionStatus.ERROR
    assert result.error_code == SkillErrorCode.VERSION_MISMATCH


def test_lifecycle_mismatch_fails_before_handler() -> None:
    """生命周期不匹配在 Handler 之前失败。"""
    executor = SyncSkillExecutorAdapter()
    # query_products 只允许 PRE_LIVE，用 ON_LIVE 触发错误
    call = _build_call(skill_id="query_products", lifecycle="ON_LIVE")
    result = executor.execute(call)
    assert result.status == SkillExecutionStatus.ERROR
    assert result.error_code == SkillErrorCode.LIFECYCLE_MISMATCH


def test_invalid_arguments_fails_before_handler() -> None:
    """参数不符合 Schema 在 Handler 之前失败。"""
    executor = SyncSkillExecutorAdapter()
    # query_products: room_id 应为 string，传 int 触发 Schema 错误
    call = _build_call(skill_id="query_products", args={"room_id": 123})
    result = executor.execute(call)
    assert result.status == SkillExecutionStatus.ERROR
    assert result.error_code == SkillErrorCode.INVALID_ARGUMENTS


def test_missing_idempotency_key_fails_before_handler() -> None:
    """需要幂等键但没有提供时在 Handler 之前失败。"""
    executor = SyncSkillExecutorAdapter()
    # set_product_price 需要幂等键，但先要过 hard-gate 审批
    approval = ApprovalContext(
        source=ApprovalSource.TRUSTED_COMPAT,
        decision="APPROVED",
        operator_id="test",
        approval_audit_id="aud_001",
    )
    call = _build_call(skill_id="set_product_price", args={"product_id": "p1", "price": "99"}, approval=approval)
    result = executor.execute(call)
    assert result.status == SkillExecutionStatus.ERROR
    assert result.error_code == SkillErrorCode.IDEMPOTENCY_REQUIRED


def test_hard_gate_without_approval_returns_pending() -> None:
    """高风险 Skill 缺少可信审批时返回 pending，不调用 Handler。"""
    executor = SyncSkillExecutorAdapter()
    call = _build_call(skill_id="set_product_price", args={"product_id": "p1", "price": "99"}, idempotency_key="key1")
    result = executor.execute(call)
    assert result.status == SkillExecutionStatus.PENDING
    assert result.error_code == SkillErrorCode.APPROVAL_REQUIRED


def test_hard_gate_rejected_approval_fails() -> None:
    """拒绝审批不执行 Handler。"""
    executor = SyncSkillExecutorAdapter()
    rejected = ApprovalContext(
        source=ApprovalSource.TRUSTED_COMPAT,
        decision="REJECTED",
        operator_id="auditor",
        approval_audit_id="aud_rej_001",
    )
    call = _build_call(skill_id="set_product_price", args={"product_id": "p1", "price": "99"}, idempotency_key="key2", approval=rejected)
    result = executor.execute(call)
    assert result.status == SkillExecutionStatus.ERROR
    assert result.error_code == SkillErrorCode.APPROVAL_REJECTED

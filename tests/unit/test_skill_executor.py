"""Phase 11A SkillExecutor 测试。

测试覆盖：未知 Skill、版本不匹配、生命周期错误、参数校验、
缺幂等键、缺审批、拒绝审批、Handler 不存在和 Handler 异常。
每个前置失败步骤都不会调用 Handler。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.skill_runtime.executor import (
    SkillExecutor,
    SyncSkillExecutorAdapter,
    _SkillHandler,
    get_handler,
    register_handler,
)
from src.skill_runtime.models import (
    ApprovalContext,
    EventAuthorizationContext,
    SkillCall,
    SkillExecutionContext,
    SkillExecutionRoute,
    SkillExecutionStatus,
    SkillErrorCode,
    _build_human_interrupt_approval,
    _build_verified_event_authorization,
)

# 确保四个核心 Handler 已注册，测试替换后可以恢复原实例。
import src.skill_runtime.pre_live_handlers  # noqa: F401, E402


class FakeHandler(_SkillHandler):
    """记录调用次数，并可配置异常或非法输出以验证单次受控执行边界。"""

    def __init__(
        self,
        *,
        should_raise: bool = False,
        output: dict[str, Any] | None = None,
    ) -> None:
        self.calls = 0
        self.should_raise = should_raise
        self.output = {"ok": True} if output is None else output

    async def execute(self, skill_id, arguments, context):
        """模拟原生 async Handler，禁止 Executor 将其送入同步线程池。"""
        self.calls += 1
        if self.should_raise:
            raise RuntimeError("测试异常不得泄漏到结果摘要")
        return self.output


# ── 辅助：构建测试用的 SkillCall ──────────────────────────────────────


def _build_call(
    skill_id: str = "test_skill",
    version: str = "1.0.0",
    args: dict[str, Any] | None = None,
    lifecycle: str = "PRE_LIVE",
    idempotency_key: str | None = None,
    approval: ApprovalContext | None = None,
    event_authorization: EventAuthorizationContext | None = None,
) -> SkillCall:
    """构建测试 SkillCall，参数不完整时用默认值补全。"""
    ctx = SkillExecutionContext(
        room_id="room_1",
        trace_id="trace_1",
        lifecycle=lifecycle,
        execution_route=SkillExecutionRoute.SKILL_RUNTIME,
        idempotency_key=idempotency_key,
        approval=approval,
        event_authorization=event_authorization,
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
    approval = _build_human_interrupt_approval(
        decision="APPROVED",
        operator_id="test",
        approval_audit_id="aud_001",
    )
    call = _build_call(
        skill_id="set_product_price",
        version="1.1.0",
        args={"product_id": "p1", "price": "99", "expected_version": 1},
        approval=approval,
    )
    result = executor.execute(call)
    assert result.status == SkillExecutionStatus.ERROR
    assert result.error_code == SkillErrorCode.IDEMPOTENCY_REQUIRED


def test_missing_idempotency_and_approval_reports_idempotency_first() -> None:
    """冻结顺序要求幂等检查先于风险审批，二者同时缺失时 Handler 不得调用。"""
    fake = FakeHandler()
    executor = SyncSkillExecutorAdapter(
        SkillExecutor(handlers={"set_product_price": fake})
    )

    result = executor.execute(
        _build_call(
            skill_id="set_product_price",
            version="1.1.0",
            args={"product_id": "p1", "price": "99", "expected_version": 1},
        )
    )

    assert result.status == SkillExecutionStatus.ERROR
    assert result.error_code == SkillErrorCode.IDEMPOTENCY_REQUIRED
    assert fake.calls == 0


def test_hard_gate_without_approval_returns_pending() -> None:
    """高风险 Skill 缺少可信审批时返回 pending，不调用 Handler。"""
    executor = SyncSkillExecutorAdapter()
    call = _build_call(
        skill_id="set_product_price",
        version="1.1.0",
        args={"product_id": "p1", "price": "99", "expected_version": 1},
        idempotency_key="key1",
    )
    result = executor.execute(call)
    assert result.status == SkillExecutionStatus.PENDING
    assert result.error_code == SkillErrorCode.APPROVAL_REQUIRED


def test_hard_gate_rejected_approval_fails() -> None:
    """拒绝审批不执行 Handler。"""
    executor = SyncSkillExecutorAdapter()
    rejected = _build_human_interrupt_approval(
        decision="REJECTED",
        operator_id="auditor",
        approval_audit_id="aud_rej_001",
    )
    call = _build_call(
        skill_id="set_product_price",
        version="1.1.0",
        args={"product_id": "p1", "price": "99", "expected_version": 1},
        idempotency_key="key2",
        approval=rejected,
    )
    result = executor.execute(call)
    assert result.status == SkillExecutionStatus.ERROR
    assert result.error_code == SkillErrorCode.APPROVAL_REJECTED


def _event_authorization(observed_version: int = 3) -> EventAuthorizationContext:
    """构造已由事件边界核验的最小授权证据。"""
    return _build_verified_event_authorization(
        event_id="event-sold-out-executor",
        provenance_id="provenance-sold-out-executor",
        payload_digest="a" * 64,
        observed_version=observed_version,
    )


def _sold_out_call(
    *,
    approval: ApprovalContext | None = None,
    event_authorization: EventAuthorizationContext | None = None,
    expected_version: int = 3,
) -> SkillCall:
    """构造售罄 2.0.0 调用，控制字段只进入执行上下文。"""
    return _build_call(
        skill_id="handle_sold_out_event",
        version="2.0.0",
        args={"product_id": "p001", "expected_version": expected_version},
        lifecycle="ON_LIVE",
        idempotency_key="event-sold-out-executor:root-001:handle_sold_out_event",
        approval=approval,
        event_authorization=event_authorization,
    )


def test_sold_out_v2_missing_authorization_is_pending_before_attempt() -> None:
    """缺少事件或人工授权时必须 pending，不能调用 Handler。"""
    handler = FakeHandler()
    runtime = SyncSkillExecutorAdapter(
        SkillExecutor(handlers={"handle_sold_out_event": handler})
    )

    result = runtime.execute(_sold_out_call())

    assert result.status is SkillExecutionStatus.PENDING
    assert result.error_code is SkillErrorCode.APPROVAL_REQUIRED
    assert handler.calls == 0


def test_sold_out_v2_accepts_verified_event_or_approved_human() -> None:
    """可信事件和真实人工批准是两条独立可用路径，任一路径都只执行一次 Handler。"""
    event_handler = FakeHandler()
    human_handler = FakeHandler()
    approved = _build_human_interrupt_approval(
        decision="APPROVED",
        operator_id="operator-sold-out",
        approval_audit_id="approval-sold-out",
    )

    event_result = SyncSkillExecutorAdapter(
        SkillExecutor(handlers={"handle_sold_out_event": event_handler})
    ).execute(_sold_out_call(event_authorization=_event_authorization()))
    human_result = SyncSkillExecutorAdapter(
        SkillExecutor(handlers={"handle_sold_out_event": human_handler})
    ).execute(_sold_out_call(approval=approved))

    assert event_result.status is SkillExecutionStatus.SUCCESS
    assert human_result.status is SkillExecutionStatus.SUCCESS
    assert event_handler.calls == 1
    assert human_handler.calls == 1


def test_sold_out_v2_rejects_event_version_mismatch_and_ambiguous_sources() -> None:
    """事件观察版本必须绑定 CAS 输入，model_copy 伪造的双授权也不能进入 Handler。"""
    handler = FakeHandler()
    runtime = SyncSkillExecutorAdapter(
        SkillExecutor(handlers={"handle_sold_out_event": handler})
    )
    mismatched = runtime.execute(
        _sold_out_call(
            event_authorization=_event_authorization(observed_version=2),
            expected_version=3,
        )
    )
    approved = _build_human_interrupt_approval(
        decision="APPROVED",
        operator_id="operator-ambiguous",
        approval_audit_id="approval-ambiguous",
    )
    valid = _sold_out_call(event_authorization=_event_authorization())
    ambiguous_context = valid.context.model_copy(update={"approval": approved})
    ambiguous = runtime.execute(valid.model_copy(update={"context": ambiguous_context}))

    assert mismatched.error_code is SkillErrorCode.APPROVAL_REJECTED
    assert ambiguous.error_code is SkillErrorCode.APPROVAL_REJECTED
    assert handler.calls == 0


def test_sold_out_v1_is_rejected_before_handler_and_attempt() -> None:
    """单活切换后旧 1.0.0 必须精确拒绝，不能 fallback 或隐式升级。"""
    handler = FakeHandler()
    runtime = SyncSkillExecutorAdapter(
        SkillExecutor(handlers={"handle_sold_out_event": handler})
    )
    call = _sold_out_call(event_authorization=_event_authorization()).model_copy(
        update={"version": "1.0.0"}
    )

    result = runtime.execute(call)

    assert result.error_code is SkillErrorCode.VERSION_MISMATCH
    assert handler.calls == 0


def test_invalid_arguments_never_call_registered_handler() -> None:
    """Schema 失败必须发生在 Handler 之前。"""
    original = get_handler("query_products")
    fake = FakeHandler()
    register_handler("query_products", fake)
    try:
        result = SyncSkillExecutorAdapter().execute(
            _build_call(skill_id="query_products", args={"unexpected": True})
        )
    finally:
        if original is not None:
            register_handler("query_products", original)

    assert result.error_code == SkillErrorCode.INVALID_ARGUMENTS
    assert fake.calls == 0


def test_missing_handler_returns_controlled_error() -> None:
    """已注册 Manifest 但当前 Executor 未装配 Handler 时返回稳定错误码。"""
    result = SyncSkillExecutorAdapter(SkillExecutor(handlers={})).execute(
        _build_call(
            skill_id="suggest_price_change",
            args={"product_id": "p001", "suggested_price": "29.90"},
        )
    )
    assert result.error_code == SkillErrorCode.HANDLER_NOT_FOUND


def test_handler_exception_is_sanitized() -> None:
    """Handler 异常返回固定失败摘要，不回显异常类型、消息或业务参数。"""
    original = get_handler("query_products")
    fake = FakeHandler(should_raise=True)
    register_handler("query_products", fake)
    try:
        result = SyncSkillExecutorAdapter().execute(
            _build_call(skill_id="query_products", args={})
        )
    finally:
        if original is not None:
            register_handler("query_products", original)

    assert result.error_code == SkillErrorCode.HANDLER_FAILED
    assert fake.calls == 1
    assert result.summary == "Handler execution failed"
    assert "测试异常" not in result.summary


@pytest.mark.parametrize("use_async", [False, True], ids=["sync", "async"])
def test_non_json_handler_output_returns_controlled_failure(use_async: bool) -> None:
    """Handler 返回非 JSON 对象时，两个入口都应单次执行并返回结构化失败。"""
    fake = FakeHandler(output={"unsafe": object()})
    executor = SkillExecutor(handlers={"query_products": fake})
    call = _build_call(skill_id="query_products", args={})

    result = (
        asyncio.run(executor.execute(call))
        if use_async
        else SyncSkillExecutorAdapter(executor).execute(call)
    )

    assert result.status == SkillExecutionStatus.ERROR
    assert result.error_code == SkillErrorCode.HANDLER_FAILED
    assert result.output is None
    assert result.summary == "Handler execution failed"
    assert "object at" not in result.summary
    assert fake.calls == 1


def test_async_execute_uses_same_single_attempt_core() -> None:
    """异步入口应复用相同校验与 Handler，不需要额外 pytest 插件。"""
    original = get_handler("query_products")
    fake = FakeHandler()
    register_handler("query_products", fake)
    try:
        result = asyncio.run(
            SkillExecutor().execute(_build_call(skill_id="query_products", args={}))
        )
    finally:
        if original is not None:
            register_handler("query_products", original)

    assert result.status == SkillExecutionStatus.SUCCESS
    assert fake.calls == 1


def test_executor_pins_handler_snapshot_at_construction() -> None:
    """后续 Facade 重注册 Handler 时，不得改变已经装配完成的 Executor。"""
    original = get_handler("query_products")
    first = FakeHandler()
    second = FakeHandler()
    register_handler("query_products", first)
    first_executor = SyncSkillExecutorAdapter()
    register_handler("query_products", second)
    second_executor = SyncSkillExecutorAdapter()
    try:
        first_executor.execute(_build_call(skill_id="query_products", args={}))
        second_executor.execute(_build_call(skill_id="query_products", args={}))
    finally:
        if original is not None:
            register_handler("query_products", original)

    assert first.calls == 1
    assert second.calls == 1

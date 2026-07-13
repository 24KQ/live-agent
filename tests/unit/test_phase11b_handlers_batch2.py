"""Phase 11B 批次二 Handler 与播中 Runtime 执行器测试。

批次二包含建播和售罄两个会产生状态变化或审批依赖的能力。本文件验证：
缺少可信审批时 setup 不写 Attempt；批准后 setup 经 LiveSessionPort；售罄同一
幂等键只调用一次 Port；播中 Runtime 执行器保持旧 execute(...) -> dict 外观。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from src.skill_runtime.attempt_store import InMemoryAttemptStore, OperationRequest
from src.skill_runtime.executor import SkillExecutor, SyncSkillExecutorAdapter
from src.skill_runtime.fake_platform import (
    FakeLiveCommercePlatform,
    FakePlatformFixture,
    FakePlatformProduct,
)
from src.skill_runtime.handlers import SkillRuntimeDependencies, build_skill_handlers
from src.skill_runtime.models import (
    ApprovalContext,
    FailureCategory,
    SkillCall,
    SkillExecutionContext,
    SkillExecutionRoute,
    SkillExecutionStatus,
    _build_human_interrupt_approval,
)


class CountingAttemptStore(InMemoryAttemptStore):
    """记录 claim 次数，用于证明 pending 审批不会提前写外部执行意图。"""

    def __init__(self) -> None:
        super().__init__()
        self.claims = 0

    def claim_or_replay(self, request: OperationRequest):
        """只在 Executor 真正准备调用 Handler 前递增。"""
        self.claims += 1
        return super().claim_or_replay(request)


class CountingPlatform(FakeLiveCommercePlatform):
    """在真实 Fake 行为外记录 Port 调用次数，避免用 Mock 替代平台状态。"""

    def __init__(self, fixture: FakePlatformFixture) -> None:
        super().__init__(fixture)
        self.prepare_session_calls = 0
        self.mark_sold_out_calls = 0

    async def prepare_session(self, request):
        """记录建播 Port 调用次数。"""
        self.prepare_session_calls += 1
        return await super().prepare_session(request)

    async def mark_sold_out(self, request):
        """记录售罄 Port 调用次数。"""
        self.mark_sold_out_calls += 1
        return await super().mark_sold_out(request)


def _fixture() -> FakePlatformFixture:
    """构造含售罄商品和备选商品的独立 Fake 平台状态。"""
    return FakePlatformFixture(
        room_id="room-11b-batch2",
        products=(
            FakePlatformProduct(
                product_id="p001",
                name="售罄商品",
                price=Decimal("39.90"),
                inventory=5,
                version=1,
            ),
            FakePlatformProduct(
                product_id="p002",
                name="备选商品",
                price=Decimal("59.90"),
                inventory=8,
                version=1,
            ),
        ),
    )


def _executor(
    platform: CountingPlatform,
    store: CountingAttemptStore | None = None,
) -> tuple[SyncSkillExecutorAdapter, CountingAttemptStore]:
    """装配带统一 Handler 和可观察 Attempt Store 的同步 Runtime。"""
    attempt_store = store or CountingAttemptStore()
    executor = SkillExecutor(
        handlers=build_skill_handlers(SkillRuntimeDependencies(platform=platform)),
        attempt_store=attempt_store,
    )
    return SyncSkillExecutorAdapter(executor), attempt_store


def _context(
    *,
    lifecycle: str = "PRE_LIVE",
    idempotency_key: str = "idem-setup-001",
    approval: ApprovalContext | None = None,
) -> SkillExecutionContext:
    """构造带绝对 deadline 的可信执行上下文。"""
    return SkillExecutionContext(
        room_id="room-11b-batch2",
        trace_id="trace-11b-batch2",
        lifecycle=lifecycle,
        execution_route=SkillExecutionRoute.SKILL_RUNTIME,
        idempotency_key=idempotency_key,
        approval=approval,
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=15),
    )


def _plan_snapshot() -> dict[str, Any]:
    """返回 setup_live_session 所需的不可变计划快照。"""
    return {
        "room_id": "room-11b-batch2",
        "trace_id": "trace-11b-batch2",
        "items": [
            {
                "rank": 1,
                "product_id": "p001",
                "product_name": "售罄商品",
                "role": "主推款",
                "reason": "测试建播",
            }
        ],
    }


def _approval() -> ApprovalContext:
    """构造已写入审计的可信人工审批证据。"""
    return _build_human_interrupt_approval(
        decision="APPROVED",
        operator_id="operator-11b",
        approval_audit_id="approval-audit-11b",
    )


def _rejection() -> ApprovalContext:
    """构造已写入审计的可信人工拒绝证据。"""
    return _build_human_interrupt_approval(
        decision="REJECTED",
        operator_id="operator-11b",
        approval_audit_id="approval-audit-rejected-11b",
    )


def test_setup_without_trusted_approval_is_pending_without_attempt() -> None:
    """缺少审批时应在 Executor 门禁层 pending，不能写 Attempt 或调用 Port。"""
    platform = CountingPlatform(_fixture())
    executor, store = _executor(platform)

    result = executor.execute(
        SkillCall(
            skill_id="setup_live_session",
            version="1.0.0",
            context=_context(approval=None),
            arguments={"plan": _plan_snapshot()},
        )
    )

    assert result.status == SkillExecutionStatus.PENDING
    assert store.claims == 0
    assert platform.prepare_session_calls == 0


def test_setup_with_rejected_approval_errors_without_attempt() -> None:
    """显式拒绝必须在门禁层终止，不能写 Attempt 或调用会话 Port。"""
    platform = CountingPlatform(_fixture())
    executor, store = _executor(platform)

    result = executor.execute(
        SkillCall(
            skill_id="setup_live_session",
            version="1.0.0",
            context=_context(approval=_rejection()),
            arguments={"plan": _plan_snapshot()},
        )
    )

    assert result.status == SkillExecutionStatus.ERROR
    assert store.claims == 0
    assert platform.prepare_session_calls == 0


def test_setup_with_approval_calls_live_session_port_once() -> None:
    """批准后的 setup 通过 LiveSessionPort，并把 session 事实映射为旧输出字段。"""
    platform = CountingPlatform(_fixture())
    executor, store = _executor(platform)

    result = executor.execute(
        SkillCall(
            skill_id="setup_live_session",
            version="1.0.0",
            context=_context(approval=_approval()),
            arguments={"plan": _plan_snapshot()},
        )
    )

    assert result.status == SkillExecutionStatus.SUCCESS
    assert result.output is not None
    assert result.output["allowed"] is True
    assert result.output["setup_status"] == "prepared"
    assert result.output["session"]["session_id"] == "session-1"
    assert result.attempt_id is not None
    assert store.claims == 1
    assert platform.prepare_session_calls == 1


def test_sold_out_replay_invokes_port_once() -> None:
    """同一售罄幂等键第二次调用只能重放 Attempt，不能再次写平台状态。"""
    platform = CountingPlatform(_fixture())
    executor, store = _executor(platform)
    call = SkillCall(
        skill_id="handle_sold_out_event",
        version="1.0.0",
        context=_context(
            lifecycle="ON_LIVE",
            idempotency_key="idem-sold-out-001",
        ),
        arguments={
            "room_id": "room-11b-batch2",
            "trace_id": "trace-11b-batch2",
            "product_id": "p001",
            "idempotency_key": "idem-sold-out-001",
        },
    )

    first = executor.execute(call)
    second = executor.execute(call)

    assert first.status == SkillExecutionStatus.SUCCESS
    assert second.status == SkillExecutionStatus.SUCCESS
    assert second.attempt_id == first.attempt_id
    assert store.claims == 2
    assert platform.mark_sold_out_calls == 1
    assert platform.product("p001").inventory == 0
    assert first.output is not None
    assert first.output["backup_product"]["product_id"] == "p002"
    assert "prompt" in first.output


def test_sold_out_failure_fact_maps_without_second_call() -> None:
    """缺失商品返回 FailureFact 并被同一幂等键稳定重放。"""
    platform = CountingPlatform(_fixture())
    executor, store = _executor(platform)
    call = SkillCall(
        skill_id="handle_sold_out_event",
        version="1.0.0",
        context=_context(
            lifecycle="ON_LIVE",
            idempotency_key="idem-sold-out-missing",
        ),
        arguments={
            "room_id": "room-11b-batch2",
            "trace_id": "trace-11b-batch2",
            "product_id": "missing",
            "idempotency_key": "idem-sold-out-missing",
        },
    )

    first = executor.execute(call)
    second = executor.execute(call)

    assert first.status == SkillExecutionStatus.ERROR
    assert first.attempt_id is not None
    assert first.failure is not None
    assert first.failure.category == FailureCategory.INVALID_INPUT
    # Adapter 只知道本次请求携带的 attempt_id；Executor 必须把它约束为
    # Attempt Store 实际分配的 ID，避免失败证据被挂到其他 Operation。
    assert first.failure.attempt_id == first.attempt_id
    assert second.failure is not None
    assert second.failure.category == FailureCategory.INVALID_INPUT
    assert second.attempt_id == first.attempt_id
    assert second.failure.attempt_id == first.attempt_id
    assert store.claims == 2
    assert platform.mark_sold_out_calls == 1

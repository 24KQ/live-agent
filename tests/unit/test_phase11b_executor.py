"""Phase 11B 原生 async SkillExecutor 的单次尝试测试。"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.skill_runtime.attempt_store import InMemoryAttemptStore, OperationRequest
from src.skill_runtime.executor import SkillExecutor, SyncSkillExecutorAdapter, _SkillHandler
from src.skill_runtime.models import (
    FailureCategory,
    SkillCall,
    SkillExecutionContext,
    SkillExecutionRoute,
    SkillExecutionStatus,
    _build_human_interrupt_approval,
)


class _RecordingAttemptStore(InMemoryAttemptStore):
    """以真实内存 Store 为基础记录顺序，不用 Mock 替代状态迁移。"""

    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self._events = events

    def claim_or_replay(self, request: OperationRequest):
        """记录意图写入点，证明外部 Handler 之前已存在重放证据。"""
        self._events.append("claim")
        return super().claim_or_replay(request)

    def complete_success(self, attempt_id: str, payload: dict[str, Any]):
        """记录成功闭合点，证明 Attempt 不会遗留在意图状态。"""
        self._events.append("success")
        return super().complete_success(attempt_id, payload)


class _RecordingAsyncHandler(_SkillHandler):
    """只记录真实 async Handler 的调用点，验证 Executor 不走线程池。"""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> dict[str, Any]:
        """在 await 边界后返回最小 JSON 结果，模拟 Adapter 编排完成。"""
        self._events.append("handler")
        await asyncio.sleep(0)
        return {"updated_product_id": arguments["product_id"]}


def _price_call(*, deadline_at: datetime | None = None) -> SkillCall:
    """构造已具备可信审批和幂等键的高风险改价调用。"""
    return SkillCall(
        skill_id="set_product_price",
        # 改价公开契约已升级为 1.1.0；这些测试仍只验证 Executor 的 Attempt/deadline
        # 顺序，因此显式提供有效 CAS 参数，避免在版本或 Schema 门禁提前终止。
        version="1.1.0",
        arguments={"product_id": "p001", "price": "19.90", "expected_version": 1},
        context=SkillExecutionContext(
            room_id="room-001",
            trace_id="trace-001",
            lifecycle="PRE_LIVE",
            execution_route=SkillExecutionRoute.SKILL_RUNTIME,
            idempotency_key="price-idempotency-001",
            deadline_at=deadline_at or datetime.now(timezone.utc) + timedelta(seconds=10),
            approval=_build_human_interrupt_approval(
                decision="APPROVED",
                operator_id="auditor-001",
                approval_audit_id="approval-audit-001",
            ),
        ),
    )


def test_executor_writes_intent_before_async_handler_and_closes_success() -> None:
    """意图必须先于 Handler，成功后结果必须关联唯一 Attempt。"""
    events: list[str] = []
    store = _RecordingAttemptStore(events)
    executor = SkillExecutor(
        handlers={"set_product_price": _RecordingAsyncHandler(events)},
        attempt_store=store,
    )

    result = asyncio.run(executor.execute(_price_call()))

    assert result.status == SkillExecutionStatus.SUCCESS
    assert result.attempt_id is not None
    assert events == ["claim", "handler", "success"]


class _SlowAsyncHandler(_SkillHandler):
    """显式跨越 deadline 的 Handler，用于验证发送后未知的保守语义。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: SkillExecutionContext,
    ) -> dict[str, Any]:
        """先记录已开始，再等待超过测试调用的绝对 deadline。"""
        self.calls += 1
        await asyncio.sleep(0.1)
        return {"updated_product_id": arguments["product_id"]}


def test_timeout_after_handler_started_is_unknown_and_cannot_replay() -> None:
    """Handler 已开始后超时必须关闭为副作用未知，重放不得再次调用。"""
    store = InMemoryAttemptStore()
    handler = _SlowAsyncHandler()
    executor = SkillExecutor(handlers={"set_product_price": handler}, attempt_store=store)
    call = _price_call(deadline_at=datetime.now(timezone.utc) + timedelta(milliseconds=20))

    first = asyncio.run(executor.execute(call))
    second = asyncio.run(executor.execute(call))

    assert first.status == SkillExecutionStatus.ERROR
    assert first.failure is not None
    assert first.failure.category == FailureCategory.SIDE_EFFECT_UNKNOWN
    assert second.failure is not None
    assert second.failure.category == FailureCategory.SIDE_EFFECT_UNKNOWN
    assert second.attempt_id == first.attempt_id
    assert handler.calls == 1


def test_deadline_before_handler_returns_not_sent_failure_without_calling_handler() -> None:
    """发送前 deadline 到期时只写可重放意图终态，绝不调用 Handler。"""
    events: list[str] = []
    handler = _RecordingAsyncHandler(events)
    executor = SkillExecutor(
        handlers={"set_product_price": handler},
        attempt_store=_RecordingAttemptStore(events),
    )

    result = asyncio.run(
        executor.execute(_price_call(deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    )

    assert result.status == SkillExecutionStatus.ERROR
    assert result.failure is not None
    assert result.failure.category == FailureCategory.TRANSIENT_INFRA
    assert events == ["claim"]


def test_sync_adapter_rejects_an_active_event_loop() -> None:
    """同步桥接器不得在线程已有事件循环时创建嵌套 loop 或偷偷转线程。"""
    adapter = SyncSkillExecutorAdapter()

    async def _invoke_inside_loop() -> None:
        """用真实活动 loop 调用同步入口，验证其明确要求调用方改用 await。"""
        with pytest.raises(RuntimeError, match="active event loop"):
            adapter.execute(_price_call())

    asyncio.run(_invoke_inside_loop())

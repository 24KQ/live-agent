"""Phase 11B 批次三高风险改价 Handler 测试。

测试通过真实 SkillExecutor、内存 Attempt Store 和有状态 Fake 平台锁定门禁顺序、
CAS 价格写入和失败事实重放。记录型子类只存在于测试中，生产 Store/Fake 不为
断言添加观测 API。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.skill_runtime.attempt_store import InMemoryAttemptStore, OperationRequest
from src.skill_runtime.executor import SkillExecutor, SyncSkillExecutorAdapter
from src.skill_runtime.fake_platform import (
    FakeFaultKind,
    FakeFaultRule,
    FakeLiveCommercePlatform,
    FakePlatformFixture,
    FakePlatformProduct,
)
from src.skill_runtime.handlers import SkillRuntimeDependencies, build_skill_handlers
from src.skill_runtime.models import (
    ApprovalContext,
    FailureCategory,
    SkillCall,
    SkillErrorCode,
    SkillExecutionContext,
    SkillExecutionRoute,
    SkillExecutionStatus,
    _build_human_interrupt_approval,
)


class CountingAttemptStore(InMemoryAttemptStore):
    """记录 claim 次数，证明 Executor 在外部调用前完成全部门禁。"""

    def __init__(self) -> None:
        super().__init__()
        self.claims = 0

    def claim_or_replay(self, request: OperationRequest):
        """仅在 Executor 需要创建或重放 Operation 时递增计数。"""
        self.claims += 1
        return super().claim_or_replay(request)


class CountingPlatform(FakeLiveCommercePlatform):
    """保留真实 CAS/Fault 行为，同时记录改价 Port 是否被调用。"""

    def __init__(self, fixture: FakePlatformFixture) -> None:
        super().__init__(fixture)
        self.set_price_calls = 0

    async def set_price(self, request):
        """记录一次真实 Port 调用，不以 Mock 替代写入语义。"""
        self.set_price_calls += 1
        return await super().set_price(request)


def _fixture(*, faults: tuple[FakeFaultRule, ...] = ()) -> FakePlatformFixture:
    """为每个测试创建独立的价格版本与故障脚本状态。"""
    return FakePlatformFixture(
        room_id="room-11b-batch3",
        products=(
            FakePlatformProduct(
                product_id="p001",
                name="改价测试商品",
                price=Decimal("39.90"),
                inventory=10,
                version=1,
            ),
        ),
        faults=faults,
    )


def _approved() -> ApprovalContext:
    """构造受控工厂产生的可信人工批准证据。"""
    return _build_human_interrupt_approval(
        decision="APPROVED",
        operator_id="operator-11b-batch3",
        approval_audit_id="approval-audit-11b-batch3",
    )


def _rejected() -> ApprovalContext:
    """构造受控工厂产生的可信人工拒绝证据。"""
    return _build_human_interrupt_approval(
        decision="REJECTED",
        operator_id="operator-11b-batch3",
        approval_audit_id="approval-audit-rejected-11b-batch3",
    )


def _runtime(
    *,
    platform: CountingPlatform | None = None,
) -> tuple[SyncSkillExecutorAdapter, CountingPlatform, CountingAttemptStore]:
    """装配批次三真实 Handler、计数 Port 与计数 Attempt Store。"""
    active_platform = platform or CountingPlatform(_fixture())
    store = CountingAttemptStore()
    executor = SkillExecutor(
        handlers=build_skill_handlers(SkillRuntimeDependencies(platform=active_platform)),
        attempt_store=store,
    )
    return SyncSkillExecutorAdapter(executor), active_platform, store


def _price_call(
    *,
    version: str = "1.1.0",
    arguments: dict[str, object] | None = None,
    idempotency_key: str | None = "idem-price-001",
    approval: ApprovalContext | None = None,
) -> SkillCall:
    """构造带绝对 deadline 的改价调用，默认保留有效业务输入。"""
    return SkillCall(
        skill_id="set_product_price",
        version=version,
        context=SkillExecutionContext(
            room_id="room-11b-batch3",
            trace_id="trace-11b-batch3",
            lifecycle="PRE_LIVE",
            execution_route=SkillExecutionRoute.SKILL_RUNTIME,
            idempotency_key=idempotency_key,
            approval=approval,
            deadline_at=datetime.now(timezone.utc) + timedelta(seconds=15),
        ),
        arguments=arguments
        or {"product_id": "p001", "price": "35.90", "expected_version": 1},
    )


def test_price_v1_is_rejected_before_handler_and_attempt() -> None:
    """Catalog 已退出的 1.0.0 必须在任何外部执行证据前拒绝。"""
    runtime, platform, store = _runtime()

    result = runtime.execute(_price_call(version="1.0.0", approval=_approved()))

    assert result.error_code == SkillErrorCode.VERSION_MISMATCH
    assert platform.set_price_calls == 0
    assert store.claims == 0


@pytest.mark.parametrize(
    ("call_factory", "status", "error_code"),
    [
        (
            lambda: _price_call(
                arguments={"product_id": "p001", "price": "35.90"},
                approval=_approved(),
            ),
            SkillExecutionStatus.ERROR,
            SkillErrorCode.INVALID_ARGUMENTS,
        ),
        (
            lambda: _price_call(idempotency_key=None, approval=_approved()),
            SkillExecutionStatus.ERROR,
            SkillErrorCode.IDEMPOTENCY_REQUIRED,
        ),
        (
            lambda: _price_call(approval=None),
            SkillExecutionStatus.PENDING,
            SkillErrorCode.APPROVAL_REQUIRED,
        ),
        (
            lambda: _price_call(approval=_rejected()),
            SkillExecutionStatus.ERROR,
            SkillErrorCode.APPROVAL_REJECTED,
        ),
    ],
    ids=["missing-version", "missing-idempotency", "missing-approval", "rejected-approval"],
)
def test_price_preconditions_never_create_attempt_or_call_port(
    call_factory,
    status: SkillExecutionStatus,
    error_code: SkillErrorCode,
) -> None:
    """所有前置失败均须在 Attempt intent 和 Port 调用前 fail-closed。"""
    runtime, platform, store = _runtime()

    result = runtime.execute(call_factory())

    assert (result.status, result.error_code) == (status, error_code)
    assert platform.set_price_calls == 0
    assert store.claims == 0


@pytest.mark.parametrize("invalid_price", ["Infinity", "-0.01", "NaN", "1e2", ""])
def test_invalid_price_is_rejected_before_attempt_and_port(invalid_price: str) -> None:
    """非法价格必须在 Schema 门禁终止，不能占用幂等键或形成未知副作用。"""
    runtime, platform, store = _runtime()

    result = runtime.execute(
        _price_call(
            arguments={
                "product_id": "p001",
                "price": invalid_price,
                "expected_version": 1,
            },
            approval=_approved(),
        )
    )

    assert result.status == SkillExecutionStatus.ERROR
    assert result.error_code == SkillErrorCode.INVALID_ARGUMENTS
    assert result.failure is None
    assert result.attempt_id is None
    assert platform.set_price_calls == 0
    assert store.claims == 0


def test_approved_price_call_updates_price_once_and_replays_result() -> None:
    """受控批准后的首次 CAS 只写一次，同一 Operation 重放同一 Attempt。"""
    runtime, platform, store = _runtime()
    call = _price_call(approval=_approved())

    first = runtime.execute(call)
    second = runtime.execute(call)

    assert first.status == SkillExecutionStatus.SUCCESS
    assert first.output is not None
    assert first.output["product"]["price"] == "35.90"
    assert first.output["product"]["version"] == 2
    assert second.attempt_id == first.attempt_id
    assert platform.set_price_calls == 1
    assert store.claims == 2


def test_price_version_conflict_is_failure_fact_without_state_change() -> None:
    """资源 CAS 冲突必须保留结构化分类，且不能发生最后写入获胜。"""
    runtime, platform, store = _runtime()
    before = platform.product("p001")

    result = runtime.execute(
        _price_call(
            arguments={"product_id": "p001", "price": "35.90", "expected_version": 2},
            approval=_approved(),
        )
    )

    assert result.status == SkillExecutionStatus.ERROR
    assert result.failure is not None
    assert result.failure.category == FailureCategory.VERSION_CONFLICT
    assert platform.product("p001") == before
    assert platform.set_price_calls == 1
    assert store.claims == 1


def test_price_rate_limit_preserves_retry_after_seconds() -> None:
    """Port 限流事实必须原样保留 retry-after，不由 Handler 自行重试。"""
    platform = CountingPlatform(
        _fixture(
            faults=(
                FakeFaultRule(
                    operation_name="set_price",
                    resource_key="p001",
                    call_index=1,
                    kind=FakeFaultKind.RATE_LIMITED,
                    retry_after_seconds=7,
                ),
            )
        )
    )
    runtime, _, _ = _runtime(platform=platform)

    result = runtime.execute(_price_call(approval=_approved()))

    assert result.failure is not None
    assert result.failure.category == FailureCategory.RATE_LIMITED
    assert result.failure.retry_after_seconds == 7
    assert platform.set_price_calls == 1


def test_price_unknown_after_send_replays_without_second_port_call() -> None:
    """发送后未知必须保留潜在改价证据，重放禁止再次发送。"""
    platform = CountingPlatform(
        _fixture(
            faults=(
                FakeFaultRule(
                    operation_name="set_price",
                    resource_key="p001",
                    call_index=1,
                    kind=FakeFaultKind.UNKNOWN_AFTER_SEND,
                ),
            )
        )
    )
    runtime, _, _ = _runtime(platform=platform)
    call = _price_call(approval=_approved())

    first = runtime.execute(call)
    second = runtime.execute(call)

    assert first.failure is not None
    assert first.failure.category == FailureCategory.SIDE_EFFECT_UNKNOWN
    assert second.attempt_id == first.attempt_id
    assert platform.set_price_calls == 1
    assert platform.product("p001").price == Decimal("35.90")

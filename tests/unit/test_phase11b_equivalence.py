"""Phase 11B 真实 Legacy/Runtime 建播等价性与 Runtime 失败契约。

成功建播比较必须分别执行两条真实生产调用链：Legacy 侧调用
``PreLiveBusinessFlowService.setup_live_session``，Runtime 侧调用
``SkillExecutor`` 与统一 Handler。两侧只比较共同公开的建播结果及可观察业务
事实，不比较随机 audit_id、attempt_id 或 Runtime 专属 session_id。

迁移前的 Legacy 建播只有安全门禁和业务审计，本来没有 Fake Platform 与 Attempt
Store。测试必须保留这项架构差异，不能为了让数据形状一致而在 Legacy 侧手工补造
Runtime 状态机。改价限流、版本冲突和发送后未知也只属于新平台契约，因此在本文件
中作为 Runtime-only 测试验证，不再声称与旧生产路径等价。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from src.audit.tool_call_audit import AuditEvent
from src.core.pre_live_business_flow import PreLiveBusinessFlowService
from src.core.security_hooks import GateResult
from src.skill_runtime.attempt_store import (
    AttemptRecord,
    ClaimResult,
    InMemoryAttemptStore,
    OperationRequest,
)
from src.skill_runtime.executor import SkillExecutor
from src.skill_runtime.fake_platform import (
    FakeFaultKind,
    FakeFaultRule,
    FakeLiveCommercePlatform,
    FakePlatformFixture,
    FakePlatformProduct,
)
from src.skill_runtime.handlers import SkillRuntimeDependencies, build_skill_handlers
from src.skill_runtime.models import (
    AdapterRequest,
    AdapterSuccess,
    FailureCategory,
    FailureFact,
    SideEffectState,
    SkillCall,
    SkillExecutionContext,
    SkillExecutionResult,
    SkillExecutionRoute,
    _build_human_interrupt_approval,
)
from src.skill_runtime.platform_ports import AdapterResult
from src.skills.live_plan_generator import LivePlanDraft


@dataclass(frozen=True)
class _SkillCase:
    """保存一次 Runtime 调用及 Legacy 成功建播所需的冻结业务输入。"""

    skill_id: str
    version: str
    lifecycle: str
    idempotency_key: str
    arguments: dict[str, Any]
    deadline_at: datetime
    fixture: FakePlatformFixture


class _RecordingAttemptStore(InMemoryAttemptStore):
    """记录 Runtime 最近一次 Attempt 终态，同时复用生产内存状态机。

    该测试子类只增加可观察快照，不放宽 claim、成功闭合、失败闭合或发送后未知的
    状态迁移约束。Legacy 侧不会创建此 Store，这一点是迁移架构差异的一部分。
    """

    def __init__(self) -> None:
        super().__init__()
        self.latest_record: AttemptRecord | None = None

    def claim_or_replay(self, request: OperationRequest) -> ClaimResult:
        """委托生产内存 Store 执行 claim，并保存返回记录。"""
        result = super().claim_or_replay(request)
        self.latest_record = result.record
        return result

    def complete_success(self, attempt_id: str, payload: dict[str, Any]) -> AttemptRecord:
        """委托生产内存 Store 完成成功迁移，并保存终态。"""
        self.latest_record = super().complete_success(attempt_id, payload)
        return self.latest_record

    def complete_failure(self, attempt_id: str, failure: FailureFact) -> AttemptRecord:
        """委托生产内存 Store 完成失败迁移，并保存确定或未知终态。"""
        self.latest_record = super().complete_failure(attempt_id, failure)
        return self.latest_record


class _LegacyAuditStore:
    """为真实 Legacy 服务提供独立的单进程内存审计实现。

    Store 只实现本测试所执行的生产入口需要的 ``record_event`` 协议。随机审计
    ID 会返回给真实服务，但规范化比较只读取 AuditEvent 业务字段，避免把标识生成
    策略误当成迁移契约。
    """

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def record_event(self, event: AuditEvent) -> str:
        """保存生产 Legacy 生成的原始 AuditEvent，并返回随机审计 ID。"""
        self._events.append(event)
        return f"audit-legacy-{uuid4()}"

    @property
    def only_event(self) -> AuditEvent:
        """返回唯一审计事件，额外写入或漏写都立即使契约测试失败。"""
        assert len(self._events) == 1
        return self._events[0]


@dataclass(frozen=True)
class _RuntimePlatformCall:
    """保存一次 Runtime 平台调用的原始请求与返回事实。"""

    operation: str
    request: AdapterRequest
    result: AdapterResult


class _RuntimePlatformAuditStore:
    """记录 Runtime 通过 Platform Port 发送和接收的独立边界事实。

    Store 不重新实现 Adapter 序列化，而是保留生产模型对象；比较器只读取双方共同
    拥有的字段，因此 operation_id、attempt_id 和 deadline 不会进入跨架构比较。
    """

    def __init__(self) -> None:
        self._calls: list[_RuntimePlatformCall] = []

    def record(
        self,
        operation: str,
        request: AdapterRequest,
        result: AdapterResult,
    ) -> None:
        """按发生顺序保存真实 Port 模型，避免测试复制另一套 Adapter 映射。"""
        self._calls.append(
            _RuntimePlatformCall(
                operation=operation,
                request=request,
                result=result,
            )
        )

    @property
    def only_call(self) -> _RuntimePlatformCall:
        """返回唯一平台调用事实，确保测试场景没有隐式重试或双写。"""
        assert len(self._calls) == 1
        return self._calls[0]


class _AuditedPlatform:
    """测试专用 Platform Port 装饰器，委托真实 Fake 后记录边界事实。"""

    def __init__(
        self,
        platform: FakeLiveCommercePlatform,
        audit_store: _RuntimePlatformAuditStore,
    ) -> None:
        self._platform = platform
        self._audit_store = audit_store

    async def prepare_session(self, request: AdapterRequest) -> AdapterResult:
        """执行真实 Fake 建播，并记录平台确认或失败事实。"""
        result = await self._platform.prepare_session(request)
        self._audit_store.record("prepare_session", request, result)
        return result

    async def set_price(self, request: AdapterRequest) -> AdapterResult:
        """执行真实 Fake CAS 改价，并记录发送前或发送后结果。"""
        result = await self._platform.set_price(request)
        self._audit_store.record("set_price", request, result)
        return result


@dataclass(frozen=True)
class _LegacySetupOutcome:
    """真实 Legacy 建播结果。

    字段刻意只有生产 Legacy 返回的 GateResult 与业务审计；没有 platform 或
    attempt_store，防止测试再次伪造迁移前并不存在的 Runtime 基础设施。
    """

    audit_store: _LegacyAuditStore
    gate: GateResult


@dataclass(frozen=True)
class _RuntimeOutcome:
    """真实 Runtime 执行结果及其专属 Fake、Attempt 和平台审计快照。"""

    platform: FakeLiveCommercePlatform
    attempt_store: _RecordingAttemptStore
    audit_store: _RuntimePlatformAuditStore
    result: SkillExecutionResult


def _fixture(*, faults: tuple[FakeFaultRule, ...] = ()) -> FakePlatformFixture:
    """构造固定双商品平台夹具，并按场景注入声明式故障。"""
    return FakePlatformFixture(
        room_id="room-phase11b-equivalence",
        products=(
            FakePlatformProduct(
                product_id="p001",
                name="主推商品",
                price=Decimal("39.90"),
                inventory=10,
                version=1,
            ),
        ),
        faults=faults,
    )


def _approved_setup_case() -> _SkillCase:
    """创建真实 Legacy 与 Runtime 都能消费的成功建播输入。"""
    return _SkillCase(
        skill_id="setup_live_session",
        version="1.0.0",
        lifecycle="PRE_LIVE",
        idempotency_key="idem-equivalence-setup",
        arguments={
            "plan": {
                "room_id": "room-phase11b-equivalence",
                "trace_id": "trace-phase11b-equivalence",
                "items": [
                    {
                        "rank": 1,
                        "product_id": "p001",
                        "product_name": "主推商品",
                        "role": "主推款",
                        "reason": "真实生产入口隔离比较",
                    }
                ],
            }
        },
        # Legacy 会先于 Runtime 执行，较宽的调用方 deadline 用于吸收慢 CI 调度；
        # Executor 仍按 Skill Manifest 的 max_attempt_seconds 限制单次真实尝试。
        deadline_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        fixture=_fixture(),
    )


def _runtime_price_case(fault_kind: FakeFaultKind) -> _SkillCase:
    """创建只供 Runtime 平台失败语义验证的受控改价场景。"""
    retry_after_seconds = 7 if fault_kind == FakeFaultKind.RATE_LIMITED else None
    fault = FakeFaultRule(
        operation_name="set_price",
        resource_key="p001",
        call_index=1,
        kind=fault_kind,
        retry_after_seconds=retry_after_seconds,
    )
    return _SkillCase(
        skill_id="set_product_price",
        version="1.1.0",
        lifecycle="PRE_LIVE",
        idempotency_key=f"idem-runtime-price-{fault_kind.value.lower()}",
        arguments={"product_id": "p001", "price": "35.90", "expected_version": 1},
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=15),
        fixture=_fixture(faults=(fault,)),
    )


def _runtime_call(case: _SkillCase) -> SkillCall:
    """构造明确标记为 SKILL_RUNTIME 的真实 Executor 调用。"""
    return SkillCall(
        skill_id=case.skill_id,
        version=case.version,
        context=SkillExecutionContext(
            room_id=case.fixture.room_id,
            trace_id="trace-phase11b-equivalence",
            lifecycle=case.lifecycle,
            execution_route=SkillExecutionRoute.SKILL_RUNTIME,
            idempotency_key=case.idempotency_key,
            approval=_build_human_interrupt_approval(
                decision="APPROVED",
                operator_id="operator-phase11b-equivalence",
                approval_audit_id="approval-audit-phase11b-equivalence",
            ),
            deadline_at=case.deadline_at,
        ),
        arguments=dict(case.arguments),
    )


def _runtime_observable_state(
    platform: FakeLiveCommercePlatform,
    store: _RecordingAttemptStore,
    fixture: FakePlatformFixture,
) -> dict[str, Any]:
    """读取 Runtime Attempt 终态和 Fake 商品状态，且不暴露随机 attempt_id。"""
    assert store.latest_record is not None
    return {
        "attempt_state": store.latest_record.state.value,
        "products": {
            product.product_id: platform.product(product.product_id).model_dump(mode="json")
            for product in fixture.products
        },
    }


def _run_production_legacy_setup(case: _SkillCase) -> _LegacySetupOutcome:
    """调用真实生产 Legacy 建播入口，不创建 Fake 或 Attempt Store。"""
    audit_store = _LegacyAuditStore()
    service = PreLiveBusinessFlowService(
        catalog_repository=object(),  # setup_live_session 不读取货盘 Repository。
        audit_store=audit_store,  # type: ignore[arg-type]
    )
    plan = LivePlanDraft.model_validate(case.arguments["plan"])
    gate, audit_id = service.setup_live_session(
        room_id=case.fixture.room_id,
        plan=plan,
        trace_id=plan.trace_id,
        confirmed_setup=True,
        idempotency_key=case.idempotency_key,
    )

    # 只校验真实入口确实写出一条审计，不保存或跨栈比较随机 audit_id。
    assert audit_id is not None
    _ = audit_store.only_event
    return _LegacySetupOutcome(
        audit_store=audit_store,
        gate=gate,
    )


async def _run_runtime(case: _SkillCase) -> _RuntimeOutcome:
    """通过真实 SkillExecutor、统一 Handler 和场景专属 Runtime 依赖执行。"""
    platform = FakeLiveCommercePlatform.from_fixture(case.fixture.model_copy(deep=True))
    attempt_store = _RecordingAttemptStore()
    audit_store = _RuntimePlatformAuditStore()
    audited_platform = _AuditedPlatform(platform, audit_store)
    executor = SkillExecutor(
        handlers=build_skill_handlers(
            SkillRuntimeDependencies(platform=audited_platform)  # type: ignore[arg-type]
        ),
        attempt_store=attempt_store,
    )

    result = await executor.execute(_runtime_call(case))
    return _RuntimeOutcome(
        platform=platform,
        attempt_store=attempt_store,
        audit_store=audit_store,
        result=result,
    )


def run_setup_comparison(
    case: _SkillCase,
) -> tuple[_LegacySetupOutcome, _RuntimeOutcome]:
    """顺序执行真实 Legacy 建播与独立 Runtime 建播，避免任何共享可变依赖。"""
    legacy = _run_production_legacy_setup(case)
    runtime = asyncio.run(_run_runtime(case))
    return legacy, runtime


def _normalize_legacy_setup(outcome: _LegacySetupOutcome) -> dict[str, Any]:
    """从 Legacy 公开门禁和生产审计中提取双方共同拥有的业务事实。"""
    event = outcome.audit_store.only_event
    return {
        "public_result": {
            "allowed": outcome.gate.allowed,
            "setup_status": event.result_payload["status"],
        },
        "observable_fact": {
            "room_id": event.room_id,
            "trace_id": event.trace_id,
            "idempotency_key": event.idempotency_key,
            "plan_item_ids": list(event.result_payload["plan_item_ids"]),
            "status": event.result_payload["status"],
        },
    }


def _normalize_runtime_setup(outcome: _RuntimeOutcome) -> dict[str, Any]:
    """从 Runtime 公开输出和平台审计中提取同一组共同业务事实。"""
    assert outcome.result.failure is None
    assert outcome.result.output is not None
    platform_call = outcome.audit_store.only_call
    assert isinstance(platform_call.result, AdapterSuccess)
    request_payload = platform_call.request.payload
    session = platform_call.result.output["session"]
    return {
        "public_result": {
            "allowed": outcome.result.output["allowed"],
            "setup_status": outcome.result.output["setup_status"],
        },
        "observable_fact": {
            "room_id": platform_call.request.room_id,
            "trace_id": request_payload["__trace_id"],
            "idempotency_key": platform_call.request.idempotency_key,
            "plan_item_ids": [
                item["product_id"] for item in request_payload["plan"]["items"]
            ],
            "status": session["status"],
        },
    }


def test_success_comparison_calls_production_legacy_setup(monkeypatch: Any) -> None:
    """成功比较必须进入生产 Legacy 建播入口，禁止测试自行复制旧路径。"""
    original_setup = PreLiveBusinessFlowService.setup_live_session
    legacy_calls: list[dict[str, Any]] = []

    def recording_setup(
        self: PreLiveBusinessFlowService,
        *args: Any,
        **kwargs: Any,
    ) -> tuple[GateResult, str | None]:
        """旁路记录真实方法输入后继续执行生产实现，不替换门禁或审计语义。"""
        legacy_calls.append({"service": self, "args": args, "kwargs": kwargs})
        return original_setup(self, *args, **kwargs)

    monkeypatch.setattr(
        PreLiveBusinessFlowService,
        "setup_live_session",
        recording_setup,
    )

    run_setup_comparison(_approved_setup_case())

    # 入口身份必须单独受测试约束；只断言最终 JSON 无法发现比较器复制了 Runtime。
    assert len(legacy_calls) == 1
    assert legacy_calls[0]["kwargs"]["confirmed_setup"] is True


def test_production_legacy_and_runtime_setup_share_common_contract() -> None:
    """成功建播只比较两种架构共同公开的结果和可观察业务事实。"""
    case = _approved_setup_case()
    legacy, runtime = run_setup_comparison(case)

    # Legacy 本来没有 Fake 或 Attempt，结果类型不提供这些字段；Runtime 则必须
    # 持有自己的真实内存状态机和平台。两侧唯一同类依赖是各自独立的审计 Store。
    assert not hasattr(legacy, "platform")
    assert not hasattr(legacy, "attempt_store")
    assert legacy.audit_store is not runtime.audit_store
    runtime_state = _runtime_observable_state(
        runtime.platform,
        runtime.attempt_store,
        case.fixture,
    )
    assert runtime_state["attempt_state"] == "SUCCEEDED"
    assert runtime_state["products"]["p001"]["price"] == "39.90"

    normalized_legacy = _normalize_legacy_setup(legacy)
    normalized_runtime = _normalize_runtime_setup(runtime)
    assert normalized_legacy == {
        "public_result": {
            "allowed": True,
            "setup_status": "prepared",
        },
        "observable_fact": {
            "room_id": "room-phase11b-equivalence",
            "trace_id": "trace-phase11b-equivalence",
            "idempotency_key": "idem-equivalence-setup",
            "plan_item_ids": ["p001"],
            "status": "prepared",
        },
    }
    assert normalized_runtime == normalized_legacy


@pytest.mark.parametrize(
    (
        "fault_kind",
        "expected_category",
        "expected_side_effect",
        "expected_price",
        "expected_attempt_state",
        "expected_retry_after",
    ),
    [
        (
            FakeFaultKind.RATE_LIMITED,
            FailureCategory.RATE_LIMITED,
            SideEffectState.NOT_SENT,
            "39.90",
            "FAILED",
            7,
        ),
        (
            FakeFaultKind.VERSION_CONFLICT,
            FailureCategory.VERSION_CONFLICT,
            SideEffectState.NOT_SENT,
            "39.90",
            "FAILED",
            None,
        ),
        (
            FakeFaultKind.UNKNOWN_AFTER_SEND,
            FailureCategory.SIDE_EFFECT_UNKNOWN,
            SideEffectState.UNKNOWN,
            "35.90",
            "SIDE_EFFECT_UNKNOWN",
            None,
        ),
    ],
)
def test_runtime_price_failure_contract_preserves_platform_fact(
    fault_kind: FakeFaultKind,
    expected_category: FailureCategory,
    expected_side_effect: SideEffectState,
    expected_price: str,
    expected_attempt_state: str,
    expected_retry_after: int | None,
) -> None:
    """Runtime-only 改价失败必须保留分类、副作用、重试和平台终态。"""
    case = _runtime_price_case(fault_kind)
    runtime = asyncio.run(_run_runtime(case))
    runtime_state = _runtime_observable_state(
        runtime.platform,
        runtime.attempt_store,
        case.fixture,
    )

    assert runtime.result.output is None
    assert runtime.result.failure is not None
    assert runtime.result.failure.category == expected_category
    assert runtime.result.failure.side_effect_state == expected_side_effect
    assert runtime.result.failure.retry_after_seconds == expected_retry_after
    assert runtime_state["attempt_state"] == expected_attempt_state
    assert runtime_state["products"]["p001"]["price"] == expected_price

    # 平台审计只验证 Runtime 自己的边界语义；这里没有构造或调用任何 Legacy 路径。
    platform_call = runtime.audit_store.only_call
    assert isinstance(platform_call.result, FailureFact)
    assert platform_call.result.category == expected_category
    assert platform_call.result.side_effect_state == expected_side_effect

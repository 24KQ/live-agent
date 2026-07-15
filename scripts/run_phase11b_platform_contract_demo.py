"""Phase 11B 统一执行平台契约的六场景内存演示。

本脚本只装配 ``FakeLiveCommercePlatform``、``InMemoryAttemptStore`` 和统一
``SkillExecutor``。它不读取 Settings，不初始化数据库 Schema，也不导入 Kafka、
LLM 或真实平台客户端。每个场景使用独立状态，输出中省略随机 audit/attempt ID，
便于本地运行和自动化测试稳定比较。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import sys
from typing import Any, Callable


# 直接以文件路径执行时，Python 默认只把 scripts/ 放入模块搜索路径。显式加入项目
# 根目录后，脚本与 ``python -m``、pytest 导入两种方式使用同一套 src 包。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.skill_runtime.attempt_store import (  # noqa: E402
    AttemptRecord,
    ClaimResult,
    InMemoryAttemptStore,
    OperationRequest,
)
from src.skill_runtime.executor import SkillExecutor  # noqa: E402
from src.skill_runtime.fake_platform import (  # noqa: E402
    FakeFaultKind,
    FakeFaultRule,
    FakeLiveCommercePlatform,
    FakePlatformFixture,
    FakePlatformProduct,
)
from src.skill_runtime.handlers import (  # noqa: E402
    SkillRuntimeDependencies,
    build_skill_handlers,
)
from src.skill_runtime.models import (  # noqa: E402
    ApprovalContext,
    EventAuthorizationContext,
    FailureFact,
    SideEffectState,
    SkillCall,
    SkillExecutionContext,
    SkillExecutionResult,
    SkillExecutionRoute,
    _build_human_interrupt_approval,
    _build_verified_event_authorization,
)


SCENARIO_ORDER: tuple[str, ...] = (
    "setup_success",
    "sold_out",
    "rate_limited",
    "version_conflict",
    "deadline",
    "side_effect_unknown",
)


class _InspectableAttemptStore(InMemoryAttemptStore):
    """在真实内存 Store 状态机外保留最近终态，供 Demo 生成摘要。

    该子类不改变 claim 或终态迁移语义，也不提供生产查询接口。随机 attempt_id
    只保留在 Store 内部；演示输出仅展示 ``AttemptState``，避免每次运行产生噪声。
    """

    def __init__(self) -> None:
        super().__init__()
        self.latest_record: AttemptRecord | None = None

    def claim_or_replay(self, request: OperationRequest) -> ClaimResult:
        """委托真实原子 claim，并保存当前记录快照。"""
        result = super().claim_or_replay(request)
        self.latest_record = result.record
        return result

    def complete_success(self, attempt_id: str, payload: dict[str, Any]) -> AttemptRecord:
        """委托真实成功闭合，并保存成功终态。"""
        self.latest_record = super().complete_success(attempt_id, payload)
        return self.latest_record

    def complete_failure(self, attempt_id: str, failure: FailureFact) -> AttemptRecord:
        """委托真实失败闭合，并保存失败或副作用未知终态。"""
        self.latest_record = super().complete_failure(attempt_id, failure)
        return self.latest_record


def _fixture(*, faults: tuple[FakeFaultRule, ...] = ()) -> FakePlatformFixture:
    """为单个场景创建固定双商品平台状态和可选声明式故障。"""
    return FakePlatformFixture(
        room_id="room-phase11b-demo",
        products=(
            FakePlatformProduct(
                product_id="p001",
                name="主推商品",
                price=Decimal("39.90"),
                inventory=10,
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
        faults=faults,
    )


def _runtime(
    *,
    faults: tuple[FakeFaultRule, ...] = (),
) -> tuple[SkillExecutor, FakeLiveCommercePlatform, _InspectableAttemptStore]:
    """装配一个场景专属 Runtime，禁止 Fake 或 Attempt 事实跨场景泄漏。"""
    platform = FakeLiveCommercePlatform.from_fixture(_fixture(faults=faults))
    attempt_store = _InspectableAttemptStore()
    executor = SkillExecutor(
        handlers=build_skill_handlers(SkillRuntimeDependencies(platform=platform)),
        attempt_store=attempt_store,
    )
    return executor, platform, attempt_store


def _approval(scenario: str) -> ApprovalContext:
    """为高风险内部演示调用创建受控人工批准证据。

    approval_audit_id 是固定输入证据，不是 Demo 新写的随机审计记录；它只用于通过
    Runtime 的来源校验，且不会进入输出摘要。
    """
    return _build_human_interrupt_approval(
        decision="APPROVED",
        operator_id="operator-phase11b-demo",
        approval_audit_id=f"approval-audit-{scenario}",
    )


def _context(
    scenario: str,
    *,
    lifecycle: str,
    deadline_at: datetime | None = None,
    approval: ApprovalContext | None = None,
    event_authorization: EventAuthorizationContext | None = None,
) -> SkillExecutionContext:
    """构造场景专属可信上下文，使每个 Operation 的幂等身份互不冲突。"""
    return SkillExecutionContext(
        room_id="room-phase11b-demo",
        trace_id=f"trace-{scenario}",
        lifecycle=lifecycle,
        execution_route=SkillExecutionRoute.SKILL_RUNTIME,
        idempotency_key=f"idem-{scenario}",
        approval=approval,
        event_authorization=event_authorization,
        deadline_at=deadline_at or datetime.now(timezone.utc) + timedelta(seconds=15),
    )


def _setup_call() -> SkillCall:
    """构造成功建播所需的完整计划快照和可信批准。"""
    scenario = "setup_success"
    return SkillCall(
        skill_id="setup_live_session",
        version="1.0.0",
        context=_context(
            scenario,
            lifecycle="PRE_LIVE",
            approval=_approval(scenario),
        ),
        arguments={
            "plan": {
                "room_id": "room-phase11b-demo",
                "trace_id": f"trace-{scenario}",
                "items": [
                    {
                        "rank": 1,
                        "product_id": "p001",
                        "product_name": "主推商品",
                        "role": "主推款",
                        "reason": "Phase 11B 契约演示",
                    }
                ],
            }
        },
    )


def _sold_out_call() -> SkillCall:
    """构造带可信事件证据的 2.0.0 售罄 CAS 写操作。

    Demo 只调用内部工厂构造事件授权，展示正常 Event Inbox 验证完成后进入 Runtime
    的形状；不会通过普通参数或 Graph state 伪造可信来源。
    """
    scenario = "sold_out"
    return SkillCall(
        skill_id="handle_sold_out_event",
        version="2.0.0",
        context=_context(
            scenario,
            lifecycle="ON_LIVE",
            event_authorization=_build_verified_event_authorization(
                event_id="event-phase11b-demo-sold-out",
                provenance_id="provenance-phase11b-demo-sold-out",
                payload_digest="a" * 64,
                observed_version=1,
            ),
        ),
        arguments={
            "product_id": "p001",
            "expected_version": 1,
        },
    )


def _price_call(
    scenario: str,
    *,
    expected_version: int = 1,
    deadline_at: datetime | None = None,
) -> SkillCall:
    """构造通过内部可信批准的 1.1.0 CAS 改价调用。"""
    return SkillCall(
        skill_id="set_product_price",
        version="1.1.0",
        context=_context(
            scenario,
            lifecycle="PRE_LIVE",
            deadline_at=deadline_at,
            approval=_approval(scenario),
        ),
        arguments={
            "product_id": "p001",
            "price": "35.90",
            "expected_version": expected_version,
        },
    )


def _platform_state(platform: FakeLiveCommercePlatform) -> dict[str, Any]:
    """返回稳定排序的可观察商品状态，用于展示副作用是否发生。"""
    return {
        "products": {
            product_id: platform.product(product_id).model_dump(mode="json")
            for product_id in ("p001", "p002")
        }
    }


def _summary(
    scenario: str,
    result: SkillExecutionResult,
    platform: FakeLiveCommercePlatform,
    attempt_store: _InspectableAttemptStore,
) -> dict[str, Any]:
    """把 Runtime 结果规范化为不含随机 ID 和时间戳的确定性摘要。"""
    assert attempt_store.latest_record is not None
    failure = result.failure
    return {
        "scenario": scenario,
        "status": result.status.value,
        "failure_category": None if failure is None else failure.category.value,
        "side_effect_state": (
            SideEffectState.CONFIRMED.value
            if failure is None
            else failure.side_effect_state.value
        ),
        "retry_after_seconds": None if failure is None else failure.retry_after_seconds,
        "attempt_state": attempt_store.latest_record.state.value,
        "output": result.model_dump(mode="json")["output"],
        "platform_state": _platform_state(platform),
    }


def _execute(
    scenario: str,
    call_factory: Callable[[], SkillCall],
    *,
    faults: tuple[FakeFaultRule, ...] = (),
) -> dict[str, Any]:
    """在场景专属 Runtime 中执行一次调用并生成稳定摘要。"""
    executor, platform, attempt_store = _runtime(faults=faults)
    result = asyncio.run(executor.execute(call_factory()))
    return _summary(scenario, result, platform, attempt_store)


def _setup_success() -> dict[str, Any]:
    """演示批准后建播成功并闭合为 SUCCEEDED。"""
    return _execute("setup_success", _setup_call)


def _sold_out() -> dict[str, Any]:
    """演示售罄写入、商品下架和确定性备选商品返回。"""
    return _execute("sold_out", _sold_out_call)


def _rate_limited() -> dict[str, Any]:
    """演示平台发送前限流事实及 retry-after 保留。"""
    scenario = "rate_limited"
    fault = FakeFaultRule(
        operation_name="set_price",
        resource_key="p001",
        call_index=1,
        kind=FakeFaultKind.RATE_LIMITED,
        retry_after_seconds=7,
    )
    return _execute(
        scenario,
        lambda: _price_call(scenario),
        faults=(fault,),
    )


def _version_conflict() -> dict[str, Any]:
    """演示商品资源版本冲突，而不是 Skill 版本不匹配。"""
    scenario = "version_conflict"
    return _execute(
        scenario,
        lambda: _price_call(scenario, expected_version=2),
    )


def _deadline() -> dict[str, Any]:
    """演示 Handler 开始前 deadline 已过期，平台状态保持未发送。"""
    scenario = "deadline"
    expired = datetime.now(timezone.utc) - timedelta(seconds=1)
    return _execute(
        scenario,
        lambda: _price_call(scenario, deadline_at=expired),
    )


def _side_effect_unknown() -> dict[str, Any]:
    """演示改价已发生但响应未知，Attempt 进入不可自动重放终态。"""
    scenario = "side_effect_unknown"
    fault = FakeFaultRule(
        operation_name="set_price",
        resource_key="p001",
        call_index=1,
        kind=FakeFaultKind.UNKNOWN_AFTER_SEND,
    )
    return _execute(
        scenario,
        lambda: _price_call(scenario),
        faults=(fault,),
    )


def run_demo_scenarios(*, emit: bool = True) -> list[dict[str, Any]]:
    """按固定顺序执行六个独立场景，并可选择打印 JSON 行。"""
    scenario_functions: tuple[Callable[[], dict[str, Any]], ...] = (
        _setup_success,
        _sold_out,
        _rate_limited,
        _version_conflict,
        _deadline,
        _side_effect_unknown,
    )
    rows = [scenario_function() for scenario_function in scenario_functions]
    assert tuple(row["scenario"] for row in rows) == SCENARIO_ORDER
    if emit:
        for row in rows:
            # 一场景一行便于人读、shell 保存和测试逐行解析；sort_keys 只固定字段
            # 顺序，不改变上方 ``SCENARIO_ORDER`` 定义的场景顺序。
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    return rows


def _configure_stdout_utf8() -> None:
    """在 Windows 直接入口把 JSON 输出固定为 UTF-8。

    Windows 子进程管道默认可能继承 GBK 等系统代码页；仅在 ``main`` 调用本函数，
    避免模块导入时改写 pytest 或其他宿主进程提供的标准输出替身。
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")


def main() -> int:
    """运行完整内存演示；任一断言或契约错误都由 Python 以非零码退出。"""
    _configure_stdout_utf8()
    run_demo_scenarios(emit=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Phase 12A DAG PlanEngine 的五场景无外部依赖演示。

脚本只装配 ``InMemoryPlanStore``、固定 Proposal、真实 ``PlanWorker``、脚本化
SkillExecutor、``InMemorySaver`` 和 Command Ledger。每个场景重新创建全部状态，不
读取 Settings，不连接 PostgreSQL/Kafka/LLM/淘宝 API。直接执行时只输出五行 JSON，
供自动化测试和人工复现使用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
from typing import Any


# 直接以文件路径执行时 Python 只把 scripts/ 放入搜索路径；显式加入项目根目录，
# 让直接脚本、run_all 子进程和 pytest 导入使用同一套 src 包。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402

from src.plan_engine.commands import CommandService, PlanCommand  # noqa: E402
from src.plan_engine.models import (  # noqa: E402
    CardBatchPlanningInput,
    PlanCommandType,
    PlanNodeState,
    PlanRunState,
)
from src.plan_engine.reconciliation import (  # noqa: E402
    PlanReconciliationService,
)
from src.plan_engine.service import DefaultCardBatchPlanService  # noqa: E402
from src.plan_engine.store import InMemoryPlanStore  # noqa: E402
from src.plan_engine.worker import (  # noqa: E402
    PlanWorker,
    SyncPlanWorkerAdapter,
)
from src.skill_runtime.models import (  # noqa: E402
    FailureCategory,
    FailureFact,
    SideEffectState,
    SkillCall,
    SkillExecutionResult,
    SkillExecutionStatus,
)
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem  # noqa: E402
from src.skills.product_card_generator import generate_product_card  # noqa: E402
from src.skills.product_catalog import CatalogProduct  # noqa: E402


SCENARIO_ORDER: tuple[str, ...] = (
    "three_cards_parallel",
    "rate_limited_retry",
    "unrecoverable_failure",
    "planstore_ahead_recovery",
    "duplicate_command",
)


class _MutableClock:
    """只由单个 Demo 场景持有的 UTC 时钟，用于确定性推进 retry_at。"""

    def __init__(self, value: datetime) -> None:
        self._value = value

    def now(self) -> datetime:
        """返回当前 aware UTC 时间，不依赖真实 sleep。"""
        return self._value

    def advance(self, seconds: int) -> None:
        """显式推进场景时间，让持久化 RETRY_WAIT 到期。"""
        self._value += timedelta(seconds=seconds)


class _ScriptedCardExecutor:
    """按商品返回预设单次结果的 async Executor 替身。

    它不实现重试；每次调用只消费一个脚本动作。重试是否发生、何时发生仍由真实
    FailurePolicy、PlanStore 的 RETRY_WAIT 和 Worker 下一次 claim 决定。
    """

    def __init__(self, scripts: dict[str, list[str]] | None = None) -> None:
        self._scripts = {
            product_id: list(actions)
            for product_id, actions in (scripts or {}).items()
        }
        self.calls: list[SkillCall] = []

    async def execute(self, call: SkillCall) -> SkillExecutionResult:
        """执行一个脚本动作，并返回完整 Runtime 结果契约。"""
        self.calls.append(call)
        product = CatalogProduct.model_validate(call.arguments["product"])
        actions = self._scripts.get(product.product_id, [])
        action = actions.pop(0) if actions else "success"
        if action == "success":
            card = generate_product_card(product)
            return SkillExecutionResult(
                skill_id=call.skill_id,
                version=call.version,
                status=SkillExecutionStatus.SUCCESS,
                output={"card": card.model_dump(mode="json")},
                summary="demo card generated",
            )
        if action == "pending":
            return SkillExecutionResult(
                skill_id=call.skill_id,
                version=call.version,
                status=SkillExecutionStatus.PENDING,
                summary="demo approval required",
            )

        category = (
            FailureCategory.RATE_LIMITED
            if action == "rate_limited"
            else FailureCategory.INVALID_INPUT
        )
        return SkillExecutionResult(
            skill_id=call.skill_id,
            version=call.version,
            status=SkillExecutionStatus.ERROR,
            summary=f"demo {action}",
            failure=FailureFact(
                category=category,
                external_code=f"demo.{action}",
                side_effect_state=SideEffectState.NOT_SENT,
                attempt_id=f"demo-{product.product_id}-{len(self.calls)}",
                retry_after_seconds=7 if category is FailureCategory.RATE_LIMITED else None,
            ),
        )


@dataclass(frozen=True)
class _ScenarioStack:
    """一个场景专属的 Runtime 组件集合，不在场景之间复用可变对象。"""

    store: InMemoryPlanStore
    executor: _ScriptedCardExecutor
    worker: SyncPlanWorkerAdapter
    service: DefaultCardBatchPlanService
    clock: _MutableClock
    request: CardBatchPlanningInput


def _product(product_id: str, rank: int) -> CatalogProduct:
    """构造固定货盘商品，字段足够生成完整 ProductCard。"""
    return CatalogProduct(
        product_id=product_id,
        name=f"演示商品 {rank}",
        category="家居",
        price=Decimal("29.90") + Decimal(rank),
        inventory=20 + rank,
        conversion_rate=Decimal("0.20"),
        commission_rate=Decimal("0.10"),
        tags=["引流款"],
        selling_points=["卖点一", "卖点二", "卖点三"],
    )


def _planning_input(
    scenario: str,
    product_ids: tuple[str, ...] = ("p001", "p002", "p003"),
) -> CardBatchPlanningInput:
    """为场景创建完整、冻结且 run_key 稳定的规划输入。"""
    products = {
        product_id: _product(product_id, index)
        for index, product_id in enumerate(product_ids, start=1)
    }
    trace_id = f"trace-phase12a-demo-{scenario}"
    return CardBatchPlanningInput(
        room_id=f"room-phase12a-demo-{scenario}",
        trace_id=trace_id,
        live_plan=LivePlanDraft(
            room_id=f"room-phase12a-demo-{scenario}",
            trace_id=trace_id,
            items=[
                LivePlanItem(
                    rank=index,
                    product_id=product_id,
                    product_name=products[product_id].name,
                    role="引流款",
                    reason=f"Phase 12A {scenario}",
                )
                for index, product_id in enumerate(product_ids, start=1)
            ],
        ),
        products_by_id=products,
    )


def _stack(
    scenario: str,
    *,
    scripts: dict[str, list[str]] | None = None,
    product_ids: tuple[str, ...] = ("p001", "p002", "p003"),
    store: InMemoryPlanStore | None = None,
    request: CardBatchPlanningInput | None = None,
    clock: _MutableClock | None = None,
) -> _ScenarioStack:
    """装配一套隔离 Store/Executor/Worker/Service，不触碰全局配置。"""
    resolved_store = store or InMemoryPlanStore()
    resolved_request = request or _planning_input(scenario, product_ids)
    resolved_clock = clock or _MutableClock(
        datetime(2026, 7, 15, 8, 0, tzinfo=timezone.utc)
    )
    executor = _ScriptedCardExecutor(scripts)
    worker = SyncPlanWorkerAdapter(
        PlanWorker(
            store=resolved_store,
            skill_executor=executor,
            worker_id=f"worker-{scenario}",
            clock=resolved_clock.now,
        )
    )
    service = DefaultCardBatchPlanService(
        store=resolved_store,
        worker=worker,
    )
    return _ScenarioStack(
        store=resolved_store,
        executor=executor,
        worker=worker,
        service=service,
        clock=resolved_clock,
        request=resolved_request,
    )


def _base_row(scenario: str, plan_run_id: str, plan_status: PlanRunState) -> dict[str, Any]:
    """返回所有场景共享的无外部依赖审计字段。"""
    return {
        "scenario": scenario,
        "plan_run_id": plan_run_id,
        "plan_status": plan_status.value,
        "external_dependencies": [],
    }


def _three_cards_parallel() -> dict[str, Any]:
    """执行 PREPARE、三个并行手卡和 COLLECT，并展示最大 claim 批次。"""
    scenario = "three_cards_parallel"
    stack = _stack(scenario)
    reference = stack.service.create_or_resume(stack.request)
    prepare = stack.worker.run_once(reference.plan_run_id)
    cards = stack.worker.run_once(reference.plan_run_id)
    collect = stack.worker.run_once(reference.plan_run_id)
    result = stack.service.drive_to_terminal(reference.plan_run_id)
    assert (prepare.claimed, cards.claimed, collect.claimed) == (1, 3, 1)
    return {
        **_base_row(scenario, reference.plan_run_id, result.status),
        "card_count": len(result.cards_snapshot),
        "skill_calls": len(stack.executor.calls),
        "parallel_claim_count": cards.claimed,
    }


def _rate_limited_retry() -> dict[str, Any]:
    """展示一次限流只写 RETRY_WAIT，时钟到期后才创建第二次 NodeRun。"""
    scenario = "rate_limited_retry"
    stack = _stack(
        scenario,
        scripts={"p001": ["rate_limited", "success"]},
        product_ids=("p001",),
    )
    reference = stack.service.create_or_resume(stack.request)
    stack.worker.run_once(reference.plan_run_id)
    first_attempt = stack.worker.run_once(reference.plan_run_id)
    card_node = next(
        node
        for node in stack.store.list_nodes(reference.plan_run_id)
        if node.logical_key == "card:p001"
    )
    retry_wait_observed = card_node.state is PlanNodeState.RETRY_WAIT
    stack.clock.advance(7)
    second_attempt = stack.worker.run_once(reference.plan_run_id)
    stack.worker.run_once(reference.plan_run_id)
    result = stack.service.drive_to_terminal(reference.plan_run_id)
    attempts = stack.store.list_node_runs(reference.plan_run_id, card_node.node_id)
    assert first_attempt.retried == 1 and second_attempt.succeeded == 1
    return {
        **_base_row(scenario, reference.plan_run_id, result.status),
        "card_count": len(result.cards_snapshot),
        "retry_wait_observed": retry_wait_observed,
        "retry_after_seconds": 7,
        "skill_calls": len(stack.executor.calls),
        "attempt_count": len(attempts),
    }


def _unrecoverable_failure() -> dict[str, Any]:
    """展示同批在途节点收敛，但一个非法输入使整批失败且 COLLECT 不运行。"""
    scenario = "unrecoverable_failure"
    stack = _stack(scenario, scripts={"p002": ["invalid_input"]})
    reference = stack.service.create_or_resume(stack.request)
    stack.worker.run_once(reference.plan_run_id)
    stack.worker.run_once(reference.plan_run_id)
    plan_run = stack.store.get_plan_run(reference.plan_run_id)
    nodes = stack.store.list_nodes(reference.plan_run_id)
    card_nodes = [node for node in nodes if node.logical_key.startswith("card:")]
    collect_node = next(node for node in nodes if node.logical_key == "collect-card-results")
    return {
        **_base_row(scenario, reference.plan_run_id, plan_run.state),
        "skill_calls": len(stack.executor.calls),
        "succeeded_card_nodes": sum(
            node.state is PlanNodeState.SUCCEEDED for node in card_nodes
        ),
        "failed_card_nodes": sum(
            node.state is PlanNodeState.FAILED for node in card_nodes
        ),
        "collect_runs": len(
            stack.store.list_node_runs(reference.plan_run_id, collect_node.node_id)
        ),
    }


def _planstore_ahead_recovery() -> dict[str, Any]:
    """模拟 Store 成功后 checkpoint 尚未写入的崩溃，重启只复用结果。"""
    scenario = "planstore_ahead_recovery"
    first = _stack(scenario)
    reference = first.service.create_or_resume(first.request)
    first.worker.run_once(reference.plan_run_id)
    first.worker.run_once(reference.plan_run_id)
    first.worker.run_once(reference.plan_run_id)
    before_restart = len(first.executor.calls)

    reconciliation = PlanReconciliationService(
        store=first.store,
        checkpointer=InMemorySaver(),
    ).reconcile(reference.plan_run_id)
    restarted = _stack(
        scenario,
        store=first.store,
        request=first.request,
        clock=first.clock,
    )
    resumed_ref = restarted.service.create_or_resume(restarted.request)
    result = restarted.service.drive_to_terminal(resumed_ref.plan_run_id)
    return {
        **_base_row(scenario, resumed_ref.plan_run_id, result.status),
        "reconciliation": reconciliation.category.value,
        "skill_calls_before_restart": before_restart,
        "skill_calls_after_restart": len(restarted.executor.calls),
        "card_count": len(result.cards_snapshot),
    }


def _duplicate_command() -> dict[str, Any]:
    """重复提交同一 APPROVE command_id，返回首次结果且只推进节点一次。"""
    scenario = "duplicate_command"
    stack = _stack(
        scenario,
        scripts={"p001": ["pending"]},
        product_ids=("p001",),
    )
    reference = stack.service.create_or_resume(stack.request)
    stack.worker.run_once(reference.plan_run_id)
    stack.worker.run_once(reference.plan_run_id)
    waiting_node = next(
        node
        for node in stack.store.list_nodes(reference.plan_run_id)
        if node.state is PlanNodeState.WAITING_APPROVAL
    )
    command_id = "phase12a-demo-approve"
    command = PlanCommand(
        command_id=command_id,
        command_type=PlanCommandType.APPROVE,
        plan_run_id=reference.plan_run_id,
        expected_plan_version=reference.plan_version,
        node_id=waiting_node.node_id,
        expected_node_status=PlanNodeState.WAITING_APPROVAL,
        payload={"operator_id": "phase12a-demo-operator"},
        issued_at=stack.clock.now(),
    )
    service = CommandService(stack.store)
    first = service.submit(command, now=stack.clock.now() + timedelta(seconds=1))
    second = service.submit(command, now=stack.clock.now() + timedelta(seconds=2))
    ledger = stack.store.get_command(command_id)
    plan_run = stack.store.get_plan_run(reference.plan_run_id)
    return {
        **_base_row(scenario, reference.plan_run_id, plan_run.state),
        "command_id": command_id,
        "first_accepted": first.accepted,
        "second_accepted": second.accepted,
        "replayed": first == second,
        "ledger_reason": ledger.reason,
        "resulting_node_status": (
            None
            if first.resulting_node_status is None
            else first.resulting_node_status.value
        ),
    }


def run_demo_scenarios(*, emit: bool = True) -> list[dict[str, Any]]:
    """按冻结顺序运行五个隔离场景，并可输出机器可读 JSON。"""
    rows = [
        _three_cards_parallel(),
        _rate_limited_retry(),
        _unrecoverable_failure(),
        _planstore_ahead_recovery(),
        _duplicate_command(),
    ]
    assert tuple(row["scenario"] for row in rows) == SCENARIO_ORDER
    if emit:
        for row in rows:
            print(json.dumps(row, ensure_ascii=False, sort_keys=True))
    return rows


def main() -> int:
    """运行全部场景；任何断言或 Runtime 失败都让脚本非零退出。"""
    run_demo_scenarios(emit=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

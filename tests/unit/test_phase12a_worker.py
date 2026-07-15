"""Phase 12A Worker、FailurePolicy 与输入审计的 TDD 契约测试。

Worker 只能执行一次已 claim 的节点批次；重试由集中策略决定并持久化为
``RETRY_WAIT``，不能在 Executor 内隐藏循环。派发 Skill 前必须先把解析后的冻结
参数和输入指纹写入 PlanStore，确保进程崩溃时仍能判断是否可以安全重放。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from src.plan_engine.bindings import MaterializedNodeInput
from src.plan_engine.capabilities import PlanCapabilityProfile
from src.plan_engine.failure_policy import FailureAction, FailurePolicy
from src.plan_engine.models import CardBatchPlanningInput, PlanNodeState, PlanRunState
from src.plan_engine.proposal import CanonicalCardBatchProposalProvider
from src.plan_engine.store import InMemoryPlanStore, MaterializedPlan
from src.plan_engine.worker import PlanWorker, SyncPlanWorkerAdapter
from src.skill_runtime.catalog import get_default_skill_catalog
from src.skill_runtime.executor import SkillExecutor
from src.skill_runtime.models import (
    FailureCategory,
    FailureFact,
    SideEffectState,
    SkillCall,
    SkillExecutionResult,
    SkillExecutionStatus,
)
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_catalog import CatalogProduct


def _product(product_id: str) -> CatalogProduct:
    """构造 Worker 测试使用的完整、确定性商品快照。"""
    return CatalogProduct(
        product_id=product_id,
        name=f"商品 {product_id}",
        category="家居",
        price=Decimal("19.90"),
        inventory=20,
        conversion_rate=Decimal("0.20"),
        commission_rate=Decimal("0.10"),
        tags=["引流款"],
        selling_points=["可审计卖点"],
    )


def _materialized_plan(
    product_ids: tuple[str, ...] = ("p001", "p002", "p003"),
) -> MaterializedPlan:
    """使用真实 Catalog/Profile 物化三张手卡固定 DAG。"""
    planning_input = CardBatchPlanningInput(
        room_id="room-001",
        trace_id="trace-001",
        live_plan=LivePlanDraft(
            room_id="room-001",
            trace_id="trace-001",
            items=[
                LivePlanItem(
                    rank=index,
                    product_id=product_id,
                    product_name=f"商品 {product_id}",
                    role="引流款",
                    reason="Worker 契约测试",
                )
                for index, product_id in enumerate(product_ids, start=1)
            ],
        ),
        products_by_id={product_id: _product(product_id) for product_id in product_ids},
    )
    proposal = CanonicalCardBatchProposalProvider().propose_sync(planning_input)
    profile = PlanCapabilityProfile.default(catalog=get_default_skill_catalog())
    capabilities = {}
    for node in proposal.nodes:
        if node.logical_key == "prepare-card-batch":
            capability = profile.resolve_control_node(
                control_type=PlanCapabilityProfile.PREPARE_CARD_BATCH
            )
        elif node.logical_key == "collect-card-results":
            capability = profile.resolve_control_node(
                control_type=PlanCapabilityProfile.COLLECT_CARD_RESULTS
            )
        else:
            capability = profile.resolve_skill_node(
                skill_id="generate_product_card",
                product_id=node.logical_key.removeprefix("card:"),
                room_id=planning_input.room_id,
            )
        capabilities[node.logical_key] = capability
    return MaterializedPlan(
        planning_input=planning_input,
        proposal=proposal,
        capabilities_by_logical_key=capabilities,
    )


def _failure(
    category: FailureCategory,
    *,
    retry_after_seconds: int | None = None,
) -> FailureFact:
    """构造不携带恢复动作的失败事实，交由 FailurePolicy 集中判定。"""
    return FailureFact(
        category=category,
        external_code=f"test.{category.value.lower()}",
        side_effect_state=(
            SideEffectState.UNKNOWN
            if category is FailureCategory.SIDE_EFFECT_UNKNOWN
            else SideEffectState.NOT_SENT
        ),
        attempt_id="attempt-001",
        retry_after_seconds=retry_after_seconds,
    )


def test_failure_policy_prefers_retry_after_for_rate_limit() -> None:
    """限流在预算内优先采用 Retry-After，并保持单次尝试次数上限。"""
    now = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    capability = PlanCapabilityProfile.default(
        catalog=get_default_skill_catalog()
    ).resolve_skill_node(
        skill_id="generate_product_card",
        product_id="p001",
        room_id="room-001",
    )

    decision = FailurePolicy().decide(
        failure=_failure(FailureCategory.RATE_LIMITED, retry_after_seconds=7),
        capability=capability,
        attempt_number=1,
        deadline_at=now + timedelta(minutes=1),
        now=now,
    )

    assert decision.action is FailureAction.RETRY
    assert decision.retry_at == now + timedelta(seconds=7)


@pytest.mark.parametrize("attempt_number", [3, 4])
def test_failure_policy_stops_retrying_after_three_total_attempts(
    attempt_number: int,
) -> None:
    """第三次及以后的失败不能再生成第四次自动尝试。"""
    now = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    capability = PlanCapabilityProfile.default(
        catalog=get_default_skill_catalog()
    ).resolve_skill_node(
        skill_id="generate_product_card",
        product_id="p001",
        room_id="room-001",
    )

    decision = FailurePolicy().decide(
        failure=_failure(FailureCategory.TRANSIENT_INFRA),
        capability=capability,
        attempt_number=attempt_number,
        deadline_at=now + timedelta(minutes=1),
        now=now,
    )

    assert decision.action is FailureAction.FAIL_PLAN
    assert decision.retry_at is None


def test_failure_policy_waits_for_human_on_unknown_side_effect() -> None:
    """发送后结果未知必须等待对账，不能自动重试或伪造失败。"""
    now = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    capability = PlanCapabilityProfile.default(
        catalog=get_default_skill_catalog()
    ).resolve_skill_node(
        skill_id="generate_product_card",
        product_id="p001",
        room_id="room-001",
    )

    decision = FailurePolicy().decide(
        failure=_failure(FailureCategory.SIDE_EFFECT_UNKNOWN),
        capability=capability,
        attempt_number=1,
        deadline_at=now + timedelta(minutes=1),
        now=now,
    )

    assert decision.action is FailureAction.WAIT_HUMAN


def test_failure_policy_rejects_retry_that_would_finish_at_deadline() -> None:
    """预计完成时刻等于 deadline 也没有可用预算，必须直接失败收敛。"""
    now = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    capability = PlanCapabilityProfile.default(
        catalog=get_default_skill_catalog()
    ).resolve_skill_node(
        skill_id="generate_product_card",
        product_id="p001",
        room_id="room-001",
    )

    decision = FailurePolicy().decide(
        failure=_failure(FailureCategory.RATE_LIMITED, retry_after_seconds=7),
        capability=capability,
        attempt_number=1,
        deadline_at=now + timedelta(
            seconds=7 + (capability.max_attempt_seconds or 0)
        ),
        now=now,
    )

    assert decision.action is FailureAction.FAIL_PLAN
    assert decision.retry_at is None


def test_store_records_materialized_input_before_execution() -> None:
    """当前 fencing 只能写入一次冻结输入快照，供 Worker 调用 Skill 前审计。"""
    store = InMemoryPlanStore()
    plan_run = store.create_or_resume(_materialized_plan())
    now = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    claim = store.claim_ready_nodes(
        plan_run_id=plan_run.plan_run_id,
        worker_id="worker-001",
        now=now,
        lease_seconds=30,
    )[0]
    materialized = MaterializedNodeInput(
        parameters={},
        input_fingerprint="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
    )

    recorded = store.record_node_input(
        node_run_id=claim.node_run_id,
        worker_id=claim.worker_id,
        claim_version=claim.claim_version,
        materialized_input=materialized,
        now=now + timedelta(seconds=1),
    )

    assert recorded.input_snapshot == {}
    assert recorded.input_fingerprint == materialized.input_fingerprint
    with pytest.raises(TypeError):
        recorded.input_snapshot["tampered"] = True


class _RecordingExecutor:
    """按商品返回脚本化结果，并断言每次调用前 Store 已保存输入指纹。"""

    def __init__(
        self,
        store: InMemoryPlanStore,
        plan_run_id: str,
        outcomes: dict[str, SkillExecutionResult | Exception],
    ) -> None:
        self._store = store
        self._plan_run_id = plan_run_id
        self._outcomes = outcomes
        self.calls: list[SkillCall] = []

    async def execute(self, call: SkillCall) -> SkillExecutionResult:
        """模拟统一 async Executor，不实现任何内部重试。"""
        product_id = call.arguments["product"]["product_id"]
        node = next(
            item
            for item in self._store.list_nodes(self._plan_run_id)
            if item.logical_key == f"card:{product_id}"
        )
        current_run = self._store.list_node_runs(self._plan_run_id, node.node_id)[-1]
        assert current_run.state is PlanNodeState.RUNNING
        assert current_run.input_fingerprint is not None
        self.calls.append(call)
        outcome = self._outcomes[product_id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _CorruptPlanningInputStore(InMemoryPlanStore):
    """仅用于验证 Worker 面对权威输入损坏时会失败闭合，而非遗留 RUNNING。"""

    corrupt_reads = False

    def get_plan_run(self, plan_run_id: str):  # type: ignore[override]
        """创建阶段返回真实视图，执行阶段返回缺字段的可审计损坏快照。"""
        view = super().get_plan_run(plan_run_id)
        if not self.corrupt_reads:
            return view
        return view.model_copy(update={"planning_input": {"room_id": view.room_id}})

    def get_plan_version(self, plan_run_id: str, version_number: int):  # type: ignore[override]
        """D-098 后版本输入才是 Worker 权威源，因此在同一开关下损坏该快照。"""
        view = super().get_plan_version(plan_run_id, version_number)
        if not self.corrupt_reads:
            return view
        return view.model_copy(update={"planning_input": {"room_id": plan_run_id}})


class _DeterministicCardHandler:
    """真实 SkillExecutor 集成测试使用的最小单次 Handler。"""

    def __init__(self) -> None:
        self.calls = 0

    async def execute(
        self,
        skill_id: str,
        arguments: dict[str, Any],
        context: Any,
    ) -> dict[str, Any]:
        """返回 JSON-safe 手卡，不执行重试或外部副作用。"""
        self.calls += 1
        return {
            "card": {
                "product_id": arguments["product"]["product_id"],
                "room_id": context.room_id,
            }
        }


def _skill_success(product_id: str) -> SkillExecutionResult:
    """构造卡片生成成功结果。"""
    return SkillExecutionResult(
        skill_id="generate_product_card",
        version="1.0.0",
        status=SkillExecutionStatus.SUCCESS,
        output={"card": {"product_id": product_id, "script": f"讲解 {product_id}"}},
        summary="执行成功",
    )


def _skill_failure(
    product_id: str,
    category: FailureCategory,
    *,
    retry_after_seconds: int | None = None,
) -> SkillExecutionResult:
    """构造带结构化失败事实的 Runtime 错误结果。"""
    failure = FailureFact(
        category=category,
        external_code=f"test.{product_id}.{category.value.lower()}",
        side_effect_state=(
            SideEffectState.UNKNOWN
            if category is FailureCategory.SIDE_EFFECT_UNKNOWN
            else SideEffectState.NOT_SENT
        ),
        attempt_id=f"attempt-{product_id}",
        retry_after_seconds=retry_after_seconds,
    )
    return SkillExecutionResult(
        skill_id="generate_product_card",
        version="1.0.0",
        status=SkillExecutionStatus.ERROR,
        summary="执行失败",
        failure=failure,
    )


def _build_worker(
    product_ids: tuple[str, ...],
    outcomes: dict[str, SkillExecutionResult | Exception],
    *,
    now: datetime,
) -> tuple[PlanWorker, InMemoryPlanStore, str, _RecordingExecutor]:
    """装配隔离 Store、脚本 Executor 和固定时钟 Worker。"""
    store = InMemoryPlanStore()
    plan_run = store.create_or_resume(_materialized_plan(product_ids))
    executor = _RecordingExecutor(store, plan_run.plan_run_id, outcomes)
    worker = PlanWorker(
        store=store,
        skill_executor=executor,
        worker_id="worker-001",
        clock=lambda: now,
    )
    return worker, store, plan_run.plan_run_id, executor


def test_worker_executes_control_then_skill_with_exact_version_and_frozen_input() -> None:
    """Worker 分批执行 PREPARE 与 Skill，Skill 调用必须钉住版本并使用冻结商品参数。"""
    now = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    worker, store, plan_run_id, executor = _build_worker(
        ("p001",),
        {"p001": _skill_success("p001")},
        now=now,
    )

    prepare_result = SyncPlanWorkerAdapter(worker).run_once(plan_run_id)
    skill_result = SyncPlanWorkerAdapter(worker).run_once(plan_run_id)

    assert prepare_result.claimed == 1
    assert prepare_result.succeeded == 1
    assert skill_result.claimed == 1
    assert skill_result.succeeded == 1
    assert len(executor.calls) == 1
    assert executor.calls[0].version == "1.0.0"
    assert executor.calls[0].arguments["product"]["product_id"] == "p001"
    card_run = next(
        item
        for item in store.list_node_runs(plan_run_id)
        if item.skill_id == "generate_product_card"
    )
    assert card_run.input_fingerprint is not None
    assert card_run.state is PlanNodeState.SUCCEEDED


def test_worker_integrates_with_real_phase11b_skill_executor() -> None:
    """真实 Runtime 核心应复用同一 SkillCall 契约完成手卡节点。"""
    store = InMemoryPlanStore()
    plan_run = store.create_or_resume(_materialized_plan(("p001",)))
    handler = _DeterministicCardHandler()
    worker = PlanWorker(
        store=store,
        skill_executor=SkillExecutor(
            handlers={"generate_product_card": handler},  # type: ignore[dict-item]
        ),
        worker_id="worker-real-runtime",
    )

    SyncPlanWorkerAdapter(worker).run_once(plan_run.plan_run_id)
    result = SyncPlanWorkerAdapter(worker).run_once(plan_run.plan_run_id)

    assert result.succeeded == 1
    assert handler.calls == 1
    card_run = next(
        item
        for item in store.list_node_runs(plan_run.plan_run_id)
        if item.skill_id == "generate_product_card"
    )
    assert card_run.output["card"] == {
        "product_id": "p001",
        "room_id": "room-001",
    }


def test_worker_completes_canonical_dag_and_collects_cards_in_plan_order() -> None:
    """三次单批运行应完成 PREPARE、并行手卡和 COLLECT，并先持久化完整结果。"""
    now = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    product_ids = ("p001", "p002", "p003")
    worker, store, plan_run_id, _ = _build_worker(
        product_ids,
        {product_id: _skill_success(product_id) for product_id in product_ids},
        now=now,
    )

    first = SyncPlanWorkerAdapter(worker).run_once(plan_run_id)
    second = SyncPlanWorkerAdapter(worker).run_once(plan_run_id)
    third = SyncPlanWorkerAdapter(worker).run_once(plan_run_id)

    assert (first.claimed, second.claimed, third.claimed) == (1, 3, 1)
    assert store.get_plan_run(plan_run_id).state is PlanRunState.SUCCEEDED
    collect_node = next(
        item
        for item in store.list_nodes(plan_run_id)
        if item.logical_key == "collect-card-results"
    )
    collect_run = store.list_node_runs(plan_run_id, collect_node.node_id)[0]
    assert [
        item["card"]["product_id"] for item in collect_run.output["cards"]
    ] == list(product_ids)


def test_worker_persists_rate_limit_as_retry_wait_without_hidden_retry() -> None:
    """限流只调用一次 Executor，并把下一次尝试时间持久化到 Store。"""
    now = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    worker, store, plan_run_id, executor = _build_worker(
        ("p001",),
        {
            "p001": _skill_failure(
                "p001",
                FailureCategory.RATE_LIMITED,
                retry_after_seconds=7,
            )
        },
        now=now,
    )
    SyncPlanWorkerAdapter(worker).run_once(plan_run_id)

    result = SyncPlanWorkerAdapter(worker).run_once(
        plan_run_id,
        deadline_at=now + timedelta(minutes=1),
    )

    assert result.claimed == 1
    assert result.retried == 1
    assert len(executor.calls) == 1
    card = next(
        item for item in store.list_nodes(plan_run_id) if item.logical_key == "card:p001"
    )
    assert card.state is PlanNodeState.RETRY_WAIT
    assert store.claim_ready_nodes(
        plan_run_id=plan_run_id,
        worker_id="probe-before",
        now=now + timedelta(seconds=6),
        lease_seconds=30,
    ) == ()
    assert len(
        store.claim_ready_nodes(
            plan_run_id=plan_run_id,
            worker_id="probe-at",
            now=now + timedelta(seconds=7),
            lease_seconds=30,
        )
    ) == 1


def test_worker_does_not_dispatch_skill_after_persisted_deadline() -> None:
    """到达节点 deadline 后必须在 Executor 前失败，不能依赖下游自觉拒绝。"""
    now = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    worker, store, plan_run_id, executor = _build_worker(
        ("p001",),
        {"p001": _skill_success("p001")},
        now=now,
    )
    SyncPlanWorkerAdapter(worker).run_once(plan_run_id)

    result = SyncPlanWorkerAdapter(worker).run_once(
        plan_run_id,
        deadline_at=now,
    )

    assert result.failed == 1
    assert executor.calls == []
    card_run = next(
        item
        for item in store.list_node_runs(plan_run_id)
        if item.skill_id == "generate_product_card"
    )
    assert card_run.state is PlanNodeState.FAILED
    assert card_run.output == {"error": "NODE_DEADLINE_EXPIRED"}


def test_retry_claim_reuses_original_persisted_node_deadline() -> None:
    """重试不能通过重新装配 Worker 获得更晚 deadline，必须复用首次节点预算。"""
    now = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    original_deadline = now + timedelta(seconds=30)
    worker, store, plan_run_id, _ = _build_worker(
        ("p001",),
        {
            "p001": _skill_failure(
                "p001",
                FailureCategory.RATE_LIMITED,
                retry_after_seconds=7,
            )
        },
        now=now,
    )
    SyncPlanWorkerAdapter(worker).run_once(plan_run_id, deadline_at=original_deadline)
    SyncPlanWorkerAdapter(worker).run_once(plan_run_id, deadline_at=original_deadline)

    retried = store.claim_ready_nodes(
        plan_run_id=plan_run_id,
        worker_id="worker-retry",
        now=now + timedelta(seconds=7),
        lease_seconds=30,
        deadline_at=now + timedelta(minutes=5),
    )[0]

    assert retried.deadline_at == original_deadline


def test_worker_waits_for_reconciliation_on_unknown_side_effect() -> None:
    """未知副作用必须停在 WAITING_RECONCILIATION，禁止自动重试。"""
    now = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    worker, store, plan_run_id, executor = _build_worker(
        ("p001",),
        {"p001": _skill_failure("p001", FailureCategory.SIDE_EFFECT_UNKNOWN)},
        now=now,
    )
    SyncPlanWorkerAdapter(worker).run_once(plan_run_id)

    result = SyncPlanWorkerAdapter(worker).run_once(plan_run_id)

    assert result.waiting_human == 1
    assert result.retried == 0
    assert len(executor.calls) == 1
    card = next(
        item for item in store.list_nodes(plan_run_id) if item.logical_key == "card:p001"
    )
    assert card.state is PlanNodeState.WAITING_RECONCILIATION


def test_worker_batch_failure_preserves_successes_and_stops_future_dispatch() -> None:
    """同批一个不可恢复失败不取消在途节点，但计划失败后不能再派发 COLLECT。"""
    now = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    worker, store, plan_run_id, executor = _build_worker(
        ("p001", "p002", "p003"),
        {
            "p001": _skill_success("p001"),
            "p002": _skill_failure("p002", FailureCategory.INVALID_INPUT),
            "p003": _skill_success("p003"),
        },
        now=now,
    )
    SyncPlanWorkerAdapter(worker).run_once(plan_run_id)

    batch = SyncPlanWorkerAdapter(worker).run_once(plan_run_id)
    after_failure = SyncPlanWorkerAdapter(worker).run_once(plan_run_id)

    assert batch.claimed == 3
    assert batch.succeeded == 2
    assert batch.failed == 1
    assert len(executor.calls) == 3
    assert store.get_plan_run(plan_run_id).state is PlanRunState.FAILED
    assert after_failure.claimed == 0
    card_runs = [
        item
        for item in store.list_node_runs(plan_run_id)
        if item.skill_id == "generate_product_card"
    ]
    assert sum(item.state is PlanNodeState.SUCCEEDED for item in card_runs) == 2
    assert sum(item.state is PlanNodeState.FAILED for item in card_runs) == 1


def test_worker_fail_closed_when_executor_raises_unexpected_exception() -> None:
    """Executor 边界异常必须闭合 NodeRun 和 PlanRun，不能遗留无限 RUNNING。"""
    now = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    worker, store, plan_run_id, _ = _build_worker(
        ("p001",),
        {"p001": RuntimeError("sensitive provider error")},
        now=now,
    )
    SyncPlanWorkerAdapter(worker).run_once(plan_run_id)

    result = SyncPlanWorkerAdapter(worker).run_once(plan_run_id)

    assert result.failed == 1
    assert store.get_plan_run(plan_run_id).state is PlanRunState.FAILED
    card_run = next(
        item
        for item in store.list_node_runs(plan_run_id)
        if item.skill_id == "generate_product_card"
    )
    assert card_run.state is PlanNodeState.FAILED
    assert card_run.output == {"error": "INTERNAL_INVARIANT"}


def test_worker_rejects_result_for_different_skill_version() -> None:
    """Executor 返回身份必须与钉住的 SkillCall 一致，避免串用其他调用结果。"""
    now = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    mismatched = SkillExecutionResult(
        skill_id="generate_product_card",
        version="9.9.9",
        status=SkillExecutionStatus.SUCCESS,
        output={"card": {"product_id": "p001"}},
    )
    worker, store, plan_run_id, _ = _build_worker(
        ("p001",),
        {"p001": mismatched},
        now=now,
    )
    SyncPlanWorkerAdapter(worker).run_once(plan_run_id)

    result = SyncPlanWorkerAdapter(worker).run_once(plan_run_id)

    assert result.failed == 1
    card_run = next(
        item
        for item in store.list_node_runs(plan_run_id)
        if item.skill_id == "generate_product_card"
    )
    assert card_run.state is PlanNodeState.FAILED
    assert card_run.output == {"error": "RESULT_IDENTITY_MISMATCH"}


def test_worker_fail_closed_when_materialized_plan_input_is_corrupt() -> None:
    """权威输入无法重建时必须记录 INTERNAL_INVARIANT，不能让 claim 永久悬挂。"""
    now = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)
    store = _CorruptPlanningInputStore()
    plan_run = store.create_or_resume(_materialized_plan(("p001",)))
    executor = _RecordingExecutor(
        store,
        plan_run.plan_run_id,
        {"p001": _skill_success("p001")},
    )
    worker = PlanWorker(
        store=store,
        skill_executor=executor,
        worker_id="worker-corrupt-input",
        clock=lambda: now,
    )
    store.corrupt_reads = True

    result = SyncPlanWorkerAdapter(worker).run_once(plan_run.plan_run_id)

    assert result.failed == 1
    node_run = store.list_node_runs(plan_run.plan_run_id)[0]
    assert node_run.state is PlanNodeState.FAILED
    assert node_run.output == {"error": "INTERNAL_INVARIANT"}

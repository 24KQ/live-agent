"""Phase 12B 不可变增量 Replan、结果复用与循环预算契约测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.plan_engine.event_state_machine import EventApplicationState
from src.plan_engine.event_store import EventDelivery, InMemoryEventStore
from src.plan_engine.events import InventoryFactEvent, VerifiedIngressProvenance
from src.plan_engine.models import CardBatchPlanningInput, PlanNodeState, PlanRunState
from src.plan_engine.store import PlanStoreInvariantError
from src.plan_engine.worker import SyncPlanWorkerAdapter
from tests.unit.test_phase12a_worker import (
    _RecordingExecutor,
    _build_worker,
    _skill_success,
)


NOW = datetime(2026, 7, 15, 3, tzinfo=timezone.utc)


def _completed_root() -> tuple[object, str, CardBatchPlanningInput]:
    """通过真实 Worker 执行首版三张手卡，保留可比较的 NodeRun 指纹与输出。"""
    outcomes = {
        product_id: _skill_success(product_id)
        for product_id in ("p001", "p002", "p003")
    }
    worker, store, plan_run_id, _ = _build_worker(
        ("p001", "p002", "p003"),
        outcomes,
        now=NOW,
    )
    adapter = SyncPlanWorkerAdapter(worker)
    for _ in range(3):
        adapter.run_once(plan_run_id)
    original = CardBatchPlanningInput.model_validate(
        store.get_plan_run(plan_run_id).planning_input
    )
    return store, plan_run_id, original


def _changed_input(original: CardBatchPlanningInput, product_id: str) -> CardBatchPlanningInput:
    """只改变一个商品快照，使其卡片输入指纹变化而其他商品保持稳定。"""
    payload = original.model_dump(mode="json")
    payload["products_by_id"][product_id]["inventory"] = 0
    payload.pop("run_key", None)
    return CardBatchPlanningInput.model_validate(payload)


def _ready_application(
    event_store: InMemoryEventStore,
    *,
    root_plan_run_id: str,
    event_id: str,
    product_id: str,
    source_version: int,
) -> None:
    """构造已经完成紧急 child、等待合并进 root 新版本的事件应用。"""
    event = InventoryFactEvent.create_sold_out(
        event_id=event_id,
        room_id="room-001",
        product_id=product_id,
        observed_version=3,
        occurred_at=NOW,
        source="inventory-service",
    )
    provenance = VerifiedIngressProvenance(
        provenance_id=f"provenance-{event_id}",
        profile_id="inventory-profile-v1",
        transport="kafka",
        topic="inventory.sold-out",
        source=event.source,
        received_at=NOW,
        payload_digest=event.payload_digest,
    )
    event_store.register_event(
        event,
        provenance,
        EventDelivery(
            occurrence_id=f"occurrence-{event_id}",
            transport="kafka",
            topic="inventory.sold-out",
            partition=0,
            offset=abs(hash(event_id)) % 100000,
            received_at=NOW,
        ),
    )
    event_store.create_application(
        event_id,
        root_plan_run_id=root_plan_run_id,
        source_plan_version=source_version,
        now=NOW,
    )
    impact = {
        "scope": "PRODUCT",
        "affected_logical_keys": [f"card:{product_id}", "collect-card-results"],
    }
    event_store.transition_application(
        event_id,
        root_plan_run_id,
        expected_state=EventApplicationState.PENDING,
        target_state=EventApplicationState.FREEZING,
        now=NOW + timedelta(seconds=1),
        impact_analysis=impact,
    )
    event_store.transition_application(
        event_id,
        root_plan_run_id,
        expected_state=EventApplicationState.FREEZING,
        target_state=EventApplicationState.EMERGENCY_RUNNING,
        now=NOW + timedelta(seconds=2),
        emergency_plan_run_id=f"emergency-{event_id}",
    )
    event_store.transition_application(
        event_id,
        root_plan_run_id,
        expected_state=EventApplicationState.EMERGENCY_RUNNING,
        target_state=EventApplicationState.REPLAN_READY,
        now=NOW + timedelta(seconds=3),
    )


def test_replan_creates_new_nodes_and_reuses_only_unchanged_successful_cards() -> None:
    """新版本必须引用复用旧结果，不复制 NodeRun，并重算受影响商品。"""
    from src.plan_engine.replan import ReplanCoordinator, ReplanStatus

    store, root_id, original = _completed_root()
    event_store = InMemoryEventStore()
    _ready_application(
        event_store,
        root_plan_run_id=root_id,
        event_id="event-replan-p001",
        product_id="p001",
        source_version=1,
    )
    coordinator = ReplanCoordinator(plan_store=store, event_store=event_store)

    result = coordinator.replan(
        root_plan_run_id=root_id,
        planning_input=_changed_input(original, "p001"),
        failure_signature="a" * 64,
        now=NOW + timedelta(seconds=4),
    )

    assert result.status is ReplanStatus.CREATED
    assert result.plan_version == 2
    old_nodes = {node.logical_key: node for node in store.list_nodes(root_id, 1)}
    new_nodes = {node.logical_key: node for node in store.list_nodes(root_id, 2)}
    assert {node.node_id for node in old_nodes.values()}.isdisjoint(
        {node.node_id for node in new_nodes.values()}
    )
    assert new_nodes["card:p001"].state is PlanNodeState.PENDING
    for product_id in ("p002", "p003"):
        node = new_nodes[f"card:{product_id}"]
        assert node.state is PlanNodeState.SUCCEEDED
        assert node.reused_from_node_id == old_nodes[f"card:{product_id}"].node_id
        assert store.list_node_runs(root_id, node.node_id) == ()
    version = store.get_plan_version(root_id, 2)
    assert version.planning_input["products_by_id"]["p001"]["inventory"] == 0
    with pytest.raises(TypeError):
        version.planning_input["products_by_id"]["p001"]["inventory"] = 99
    assert (
        store.get_plan_version(root_id, 2).planning_input["products_by_id"]["p001"][
            "inventory"
        ]
        == 0
    )
    assert version.failure_signature == "a" * 64
    application = event_store.get_application("event-replan-p001", root_id)
    assert application.state is EventApplicationState.APPLIED
    assert application.applied_plan_version == 2


def test_replan_replays_latest_version_without_creating_another_version() -> None:
    """PlanVersion 已提交但 Application 更新重试时必须复用原版本。"""
    from src.plan_engine.replan import ReplanCoordinator, ReplanStatus

    store, root_id, original = _completed_root()
    event_store = InMemoryEventStore()
    _ready_application(
        event_store,
        root_plan_run_id=root_id,
        event_id="event-replan-replay",
        product_id="p001",
        source_version=1,
    )
    coordinator = ReplanCoordinator(plan_store=store, event_store=event_store)
    changed = _changed_input(original, "p001")
    first = coordinator.replan(
        root_plan_run_id=root_id,
        planning_input=changed,
        failure_signature="b" * 64,
        now=NOW + timedelta(seconds=4),
    )
    replay = coordinator.replan(
        root_plan_run_id=root_id,
        planning_input=changed,
        failure_signature="b" * 64,
        now=NOW + timedelta(seconds=5),
    )

    assert first.plan_version == 2
    assert replay.status is ReplanStatus.REPLAYED
    assert replay.plan_version == 2
    assert store.get_plan_run(root_id).current_version == 2


def test_replan_blocks_equivalent_new_event_and_freezes_root() -> None:
    """新事件重复同一失败签名与输入指纹时必须阻断循环，不能冒充重放。"""
    from src.plan_engine.replan import ReplanCoordinator

    store, root_id, original = _completed_root()
    event_store = InMemoryEventStore()
    changed = _changed_input(original, "p001")
    _ready_application(
        event_store,
        root_plan_run_id=root_id,
        event_id="event-loop-first",
        product_id="p001",
        source_version=1,
    )
    coordinator = ReplanCoordinator(plan_store=store, event_store=event_store)
    coordinator.replan(
        root_plan_run_id=root_id,
        planning_input=changed,
        failure_signature="c" * 64,
        now=NOW + timedelta(seconds=4),
    )
    _ready_application(
        event_store,
        root_plan_run_id=root_id,
        event_id="event-loop-second",
        product_id="p001",
        source_version=2,
    )

    with pytest.raises(PlanStoreInvariantError, match="等价循环"):
        coordinator.replan(
            root_plan_run_id=root_id,
            planning_input=changed,
            failure_signature="c" * 64,
            now=NOW + timedelta(seconds=5),
        )
    assert store.get_plan_run(root_id).state is PlanRunState.FROZEN


def test_replan_freezes_after_version_three_budget_is_exhausted() -> None:
    """root 最多拥有版本 1、2、3，第四次创建请求必须转人工冻结。"""
    from src.plan_engine.replan import ReplanCoordinator

    store, root_id, original = _completed_root()
    event_store = InMemoryEventStore()
    coordinator = ReplanCoordinator(plan_store=store, event_store=event_store)
    current_input = original
    for index, product_id in enumerate(("p001", "p002"), start=1):
        _ready_application(
            event_store,
            root_plan_run_id=root_id,
            event_id=f"event-budget-{index}",
            product_id=product_id,
            source_version=index,
        )
        current_input = _changed_input(current_input, product_id)
        coordinator.replan(
            root_plan_run_id=root_id,
            planning_input=current_input,
            failure_signature=str(index) * 64,
            now=NOW + timedelta(seconds=4 + index),
        )
    assert store.get_plan_run(root_id).current_version == 3
    _ready_application(
        event_store,
        root_plan_run_id=root_id,
        event_id="event-budget-3",
        product_id="p003",
        source_version=3,
    )

    with pytest.raises(PlanStoreInvariantError, match="版本预算"):
        coordinator.replan(
            root_plan_run_id=root_id,
            planning_input=_changed_input(current_input, "p003"),
            failure_signature="3" * 64,
            now=NOW + timedelta(seconds=7),
        )
    assert store.get_plan_run(root_id).state is PlanRunState.FROZEN


def test_replanned_worker_collects_reused_outputs_without_copying_node_runs() -> None:
    """版本 2 汇总必须沿复用引用读取旧输出，并只执行变化商品。"""
    from src.plan_engine.replan import ReplanCoordinator
    from src.plan_engine.worker import PlanWorker

    store, root_id, original = _completed_root()
    event_store = InMemoryEventStore()
    _ready_application(
        event_store,
        root_plan_run_id=root_id,
        event_id="event-replan-execute",
        product_id="p001",
        source_version=1,
    )
    ReplanCoordinator(plan_store=store, event_store=event_store).replan(
        root_plan_run_id=root_id,
        planning_input=_changed_input(original, "p001"),
        failure_signature="e" * 64,
        now=NOW + timedelta(seconds=4),
    )
    executor = _RecordingExecutor(
        store,
        root_id,
        {"p001": _skill_success("p001")},
    )
    worker = SyncPlanWorkerAdapter(
        PlanWorker(
            store=store,
            skill_executor=executor,
            worker_id="worker-replan-v2",
            clock=lambda: NOW + timedelta(seconds=5),
        )
    )

    for _ in range(3):
        worker.run_once(root_id)

    assert [call.arguments["product"]["product_id"] for call in executor.calls] == [
        "p001"
    ]
    assert store.get_plan_run(root_id).state is PlanRunState.SUCCEEDED
    collect = next(
        node
        for node in store.list_nodes(root_id, 2)
        if node.logical_key == "collect-card-results"
    )
    output = store.list_node_runs(root_id, collect.node_id)[-1].output
    assert [card["card"]["product_id"] for card in output["cards"]] == [
        "p001",
        "p002",
        "p003",
    ]


class _FailAfterOneAppliedStore(InMemoryEventStore):
    """模拟 PlanVersion 已提交、仅一个 Application 更新成功后进程崩溃。"""

    fail_enabled = True
    applied_calls = 0

    def transition_application(self, *args, **kwargs):  # type: ignore[override]
        if kwargs.get("target_state") is EventApplicationState.APPLIED:
            self.applied_calls += 1
            if self.fail_enabled and self.applied_calls == 2:
                raise RuntimeError("simulated crash after one application")
        return super().transition_application(*args, **kwargs)


def test_replan_recovers_when_only_part_of_applications_were_marked_applied() -> None:
    """重启后剩余 source_event 子集必须复用已提交版本，不能误判为循环。"""
    from src.plan_engine.replan import ReplanCoordinator, ReplanStatus

    store, root_id, original = _completed_root()
    event_store = _FailAfterOneAppliedStore()
    for product_id in ("p001", "p002"):
        _ready_application(
            event_store,
            root_plan_run_id=root_id,
            event_id=f"event-partial-{product_id}",
            product_id=product_id,
            source_version=1,
        )
    coordinator = ReplanCoordinator(plan_store=store, event_store=event_store)
    changed = _changed_input(_changed_input(original, "p001"), "p002")
    with pytest.raises(RuntimeError, match="simulated crash"):
        coordinator.replan(
            root_plan_run_id=root_id,
            planning_input=changed,
            failure_signature="f" * 64,
            now=NOW + timedelta(seconds=4),
        )
    event_store.fail_enabled = False

    replay = coordinator.replan(
        root_plan_run_id=root_id,
        planning_input=changed,
        failure_signature="f" * 64,
        now=NOW + timedelta(seconds=5),
    )

    assert replay.status is ReplanStatus.REPLAYED
    assert replay.plan_version == 2
    assert all(
        application.state is EventApplicationState.APPLIED
        for application in event_store.list_applications(root_plan_run_id=root_id)
    )


def test_version_three_can_reuse_a_version_two_reference_chain() -> None:
    """连续 Replan 必须沿 reused_from 链找到原 NodeRun，而不是无谓重算。"""
    from src.plan_engine.replan import ReplanCoordinator

    store, root_id, original = _completed_root()
    event_store = InMemoryEventStore()
    coordinator = ReplanCoordinator(plan_store=store, event_store=event_store)
    changed = _changed_input(original, "p001")
    _ready_application(
        event_store,
        root_plan_run_id=root_id,
        event_id="event-chain-v2",
        product_id="p001",
        source_version=1,
    )
    coordinator.replan(
        root_plan_run_id=root_id,
        planning_input=changed,
        failure_signature="1" * 64,
        now=NOW + timedelta(seconds=4),
    )
    _ready_application(
        event_store,
        root_plan_run_id=root_id,
        event_id="event-chain-v3",
        product_id="p001",
        source_version=2,
    )
    coordinator.replan(
        root_plan_run_id=root_id,
        planning_input=changed,
        failure_signature="2" * 64,
        now=NOW + timedelta(seconds=5),
    )

    nodes = {node.logical_key: node for node in store.list_nodes(root_id, 3)}
    assert nodes["card:p002"].state is PlanNodeState.SUCCEEDED
    assert nodes["card:p003"].state is PlanNodeState.SUCCEEDED
    assert nodes["card:p002"].reused_from_node_id is not None


def test_replan_does_not_apply_an_unrelated_stale_source_version() -> None:
    """旧版本迟到事件若不属于最新版本来源，不能参与当前 Replan。"""
    from src.plan_engine.replan import ReplanCoordinator

    store, root_id, original = _completed_root()
    event_store = InMemoryEventStore()
    coordinator = ReplanCoordinator(plan_store=store, event_store=event_store)
    changed = _changed_input(original, "p001")
    _ready_application(
        event_store,
        root_plan_run_id=root_id,
        event_id="event-current-v2",
        product_id="p001",
        source_version=1,
    )
    coordinator.replan(
        root_plan_run_id=root_id,
        planning_input=changed,
        failure_signature="7" * 64,
        now=NOW + timedelta(seconds=4),
    )
    _ready_application(
        event_store,
        root_plan_run_id=root_id,
        event_id="event-unrelated-stale",
        product_id="p002",
        source_version=1,
    )

    with pytest.raises(PlanStoreInvariantError, match="没有可合并"):
        coordinator.replan(
            root_plan_run_id=root_id,
            planning_input=_changed_input(changed, "p002"),
            failure_signature="8" * 64,
            now=NOW + timedelta(seconds=5),
        )

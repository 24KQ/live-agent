"""Phase 12B Event Inbox 内存 Store、lease/fencing 与状态机契约测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import importlib
from typing import Any

import pytest
from pydantic import ValidationError

from src.plan_engine.events import InventoryFactEvent, VerifiedIngressProvenance
from src.skill_runtime.models import FailureCategory, FailureFact, SideEffectState


BASE_TIME = datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc)


def _store_module() -> Any:
    """延迟导入待实现 Store，使 RED 以明确失败呈现。"""
    try:
        return importlib.import_module("src.plan_engine.event_store")
    except ModuleNotFoundError:
        pytest.fail("尚未实现 Phase 12B Event Store", pytrace=False)


def _state_module() -> Any:
    """延迟导入待实现状态机，使缺失模块不阻断测试收集。"""
    try:
        return importlib.import_module("src.plan_engine.event_state_machine")
    except ModuleNotFoundError:
        pytest.fail("尚未实现 Phase 12B 事件状态机", pytrace=False)


def _event(
    event_id: str = "event-001",
    *,
    product_id: str = "product-001",
    observed_version: int = 3,
    occurred_at: datetime = BASE_TIME,
) -> InventoryFactEvent:
    """创建确定性售罄事实，摘要由公共模型计算。"""
    return InventoryFactEvent.create_sold_out(
        event_id=event_id,
        room_id="room-001",
        product_id=product_id,
        observed_version=observed_version,
        occurred_at=occurred_at,
        source="inventory-service",
    )


def _provenance(
    event: InventoryFactEvent,
    *,
    provenance_id: str | None = None,
    received_at: datetime = BASE_TIME + timedelta(seconds=1),
) -> VerifiedIngressProvenance:
    """创建与事件摘要闭合的 Kafka 来源证据。"""
    return VerifiedIngressProvenance(
        provenance_id=provenance_id or f"provenance-{event.event_id}",
        profile_id="inventory-kafka-v1",
        transport="KAFKA",
        topic="live-inventory",
        source=event.source,
        received_at=received_at,
        payload_digest=event.payload_digest,
    )


def _delivery(
    occurrence_id: str = "occurrence-001",
    *,
    offset: int = 1,
    received_at: datetime = BASE_TIME + timedelta(seconds=1),
) -> Any:
    """创建不含原始消息体的传输投递元数据。"""
    return _store_module().EventDelivery(
        occurrence_id=occurrence_id,
        transport="KAFKA",
        topic="live-inventory",
        partition=0,
        offset=offset,
        received_at=received_at,
    )


def _register(
    store: Any,
    event: InventoryFactEvent | None = None,
    *,
    occurrence_id: str = "occurrence-001",
    offset: int = 1,
    received_at: datetime = BASE_TIME + timedelta(seconds=1),
) -> Any:
    """向 Store 登记一个摘要闭合的投递。"""
    fact = event or _event()
    return store.register_event(
        fact,
        _provenance(fact, received_at=received_at),
        _delivery(
            occurrence_id,
            offset=offset,
            received_at=received_at,
        ),
    )


def test_event_and_application_state_machines_use_explicit_whitelists() -> None:
    """正常状态链可通过，终态回退和跨阶段跳转必须拒绝。"""
    state = _state_module()

    state.assert_inbox_transition(
        state.EventInboxState.RECEIVED,
        state.EventInboxState.VERIFIED,
    )
    state.assert_inbox_transition(
        state.EventInboxState.VERIFIED,
        state.EventInboxState.PROCESSING,
    )
    state.assert_inbox_transition(
        state.EventInboxState.PROCESSING,
        state.EventInboxState.APPLIED,
    )
    with pytest.raises(state.EventStateTransitionError):
        state.assert_inbox_transition(
            state.EventInboxState.APPLIED,
            state.EventInboxState.PROCESSING,
        )

    application_path = [
        state.EventApplicationState.PENDING,
        state.EventApplicationState.FREEZING,
        state.EventApplicationState.EMERGENCY_RUNNING,
        state.EventApplicationState.REPLAN_READY,
        state.EventApplicationState.APPLIED,
    ]
    for current, target in zip(application_path, application_path[1:]):
        state.assert_application_transition(current, target)
    with pytest.raises(state.EventStateTransitionError):
        state.assert_application_transition(
            state.EventApplicationState.APPLIED,
            state.EventApplicationState.PENDING,
        )


def test_event_store_protocol_declares_the_complete_inmemory_contract() -> None:
    """Task 3 PostgreSQL Store 不能因 Protocol 过窄而漏实现恢复与查询能力。"""
    protocol = _store_module().EventStore
    required_methods = {
        "register_event",
        "get_inbox",
        "list_inbox",
        "list_occurrences",
        "claim_next",
        "heartbeat",
        "transition_inbox",
        "create_application",
        "get_application",
        "list_applications",
        "transition_application",
    }

    assert required_methods <= set(protocol.__dict__)


def test_first_registration_persists_verified_fact_and_accepted_occurrence() -> None:
    """首次登记保存唯一事实与 ACCEPTED occurrence，不保留原始 Kafka 消息。"""
    module = _store_module()
    state = _state_module()
    store = module.InMemoryEventStore()
    event = _event()

    result = _register(store, event)

    assert result.created is True
    assert result.inbox.event == event
    assert result.inbox.state is state.EventInboxState.VERIFIED
    assert result.occurrence.classification is state.EventOccurrenceKind.ACCEPTED
    assert result.occurrence.payload_digest == event.payload_digest
    assert not hasattr(result.occurrence, "raw_message")
    assert store.get_inbox(event.event_id) == result.inbox
    assert store.list_occurrences(event.event_id) == (result.occurrence,)


def test_same_event_and_digest_append_duplicate_without_overwriting_fact() -> None:
    """同摘要重投只追加 DUPLICATE occurrence，并返回首次 Inbox 事实。"""
    module = _store_module()
    state = _state_module()
    store = module.InMemoryEventStore()
    event = _event()
    first = _register(store, event)

    replay = _register(
        store,
        event,
        occurrence_id="occurrence-002",
        offset=2,
        received_at=BASE_TIME + timedelta(seconds=2),
    )

    assert replay.created is False
    assert replay.occurrence.classification is state.EventOccurrenceKind.DUPLICATE
    assert replay.inbox.event is first.inbox.event
    assert replay.inbox.state is state.EventInboxState.VERIFIED
    assert [item.classification for item in store.list_occurrences(event.event_id)] == [
        state.EventOccurrenceKind.ACCEPTED,
        state.EventOccurrenceKind.DUPLICATE,
    ]


def test_exact_delivery_replay_returns_original_occurrence_without_appending() -> None:
    """数据库已提交但 offset 未提交时，同一 delivery 重放不得制造第二条记录。"""
    module = _store_module()
    store = module.InMemoryEventStore()
    event = _event()
    provenance = _provenance(event)
    delivery = _delivery()
    first = store.register_event(event, provenance, delivery)

    replay = store.register_event(event, provenance, delivery)

    assert replay.created is False
    assert replay.occurrence == first.occurrence
    assert store.list_occurrences(event.event_id) == (first.occurrence,)


def test_same_event_id_with_different_digest_preserves_first_and_marks_conflict() -> None:
    """摘要冲突必须保留首次 payload、追加 CONFLICT，并使 Inbox fail-closed。"""
    module = _store_module()
    state = _state_module()
    store = module.InMemoryEventStore()
    first_event = _event()
    first = _register(store, first_event)
    conflicting_event = _event(observed_version=4)

    conflict = _register(
        store,
        conflicting_event,
        occurrence_id="occurrence-conflict",
        offset=2,
        received_at=BASE_TIME + timedelta(seconds=2),
    )

    assert conflict.created is False
    assert conflict.occurrence.classification is state.EventOccurrenceKind.CONFLICT
    assert conflict.inbox.state is state.EventInboxState.CONFLICT
    assert conflict.inbox.event == first.inbox.event
    assert conflict.inbox.event.payload_digest == first_event.payload_digest
    assert conflict.occurrence.payload_digest == conflicting_event.payload_digest


def test_backdated_conflict_keeps_monotonic_inbox_time_and_still_records_fact() -> None:
    """接收时钟回拨不能破坏登记事务，Inbox 更新时间必须保持单调。"""
    module = _store_module()
    state = _state_module()
    store = module.InMemoryEventStore()
    first_event = _event()
    first = _register(
        store,
        first_event,
        received_at=BASE_TIME + timedelta(seconds=10),
    )
    conflicting_event = _event(observed_version=4)

    conflict = _register(
        store,
        conflicting_event,
        occurrence_id="occurrence-backdated-conflict",
        offset=2,
        received_at=BASE_TIME + timedelta(seconds=5),
    )

    assert conflict.inbox.state is state.EventInboxState.CONFLICT
    assert conflict.inbox.updated_at == first.inbox.updated_at
    assert conflict.occurrence.received_at == BASE_TIME + timedelta(seconds=5)
    assert len(store.list_occurrences(first_event.event_id)) == 2


def test_registration_rejects_mismatched_provenance_and_duplicate_occurrence_id() -> None:
    """来源摘要/传输不闭合或 occurrence 主键重用时不能静默覆盖证据。"""
    module = _store_module()
    store = module.InMemoryEventStore()
    event = _event()
    provenance = _provenance(event).model_copy(update={"payload_digest": "f" * 64})

    with pytest.raises(module.EventStoreInvariantError, match="摘要"):
        store.register_event(event, provenance, _delivery())

    _register(store, event)
    with pytest.raises(module.EventStoreInvariantError, match="occurrence"):
        _register(store, event, occurrence_id="occurrence-001", offset=2)
    with pytest.raises(module.EventStoreInvariantError, match="传输坐标"):
        _register(store, event, occurrence_id="occurrence-other-id", offset=1)


def test_concurrent_duplicate_registration_creates_one_fact_and_all_occurrences() -> None:
    """并发重复投递只能有一个 ACCEPTED，其他投递都作为 DUPLICATE 留证。"""
    module = _store_module()
    state = _state_module()
    store = module.InMemoryEventStore()
    event = _event()

    def register(index: int) -> Any:
        """使用唯一 delivery 身份并发登记同一个业务事件。"""
        return _register(
            store,
            event,
            occurrence_id=f"occurrence-{index}",
            offset=index,
            received_at=BASE_TIME + timedelta(seconds=index),
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(register, range(1, 9)))

    classifications = [item.occurrence.classification for item in results]
    assert classifications.count(state.EventOccurrenceKind.ACCEPTED) == 1
    assert classifications.count(state.EventOccurrenceKind.DUPLICATE) == 7
    assert len(store.list_occurrences(event.event_id)) == 8


def test_claim_uses_received_order_and_excludes_unexpired_processing_event() -> None:
    """claim 按接收时间稳定选择，未过期 PROCESSING 事件不能被第二 Worker 抢走。"""
    module = _store_module()
    state = _state_module()
    store = module.InMemoryEventStore()
    early = _event("event-early", occurred_at=BASE_TIME)
    late = _event("event-late", occurred_at=BASE_TIME + timedelta(seconds=1))
    _register(store, late, occurrence_id="occ-late", offset=2, received_at=BASE_TIME + timedelta(seconds=2))
    _register(store, early, occurrence_id="occ-early", offset=1, received_at=BASE_TIME + timedelta(seconds=1))

    first = store.claim_next("worker-a", now=BASE_TIME + timedelta(seconds=3), lease_seconds=30)
    second = store.claim_next("worker-b", now=BASE_TIME + timedelta(seconds=3), lease_seconds=30)

    assert first is not None and first.record.event.event_id == "event-early"
    assert first.record.state is state.EventInboxState.PROCESSING
    assert first.fencing_token == 1
    assert second is not None and second.record.event.event_id == "event-late"
    assert store.claim_next("worker-c", now=BASE_TIME + timedelta(seconds=3), lease_seconds=30) is None


def test_heartbeat_extends_current_lease_and_rejects_wrong_or_expired_owner() -> None:
    """只有当前未过期 claim 的 Worker 和 fencing token 可以续租。"""
    module = _store_module()
    store = module.InMemoryEventStore()
    _register(store)
    claimed_at = BASE_TIME + timedelta(seconds=2)
    claim = store.claim_next("worker-a", now=claimed_at, lease_seconds=10)
    assert claim is not None

    extended = store.heartbeat(
        claim.record.event.event_id,
        worker_id="worker-a",
        fencing_token=claim.fencing_token,
        now=claimed_at + timedelta(seconds=5),
        lease_seconds=20,
    )
    assert extended.lease_expires_at == claimed_at + timedelta(seconds=25)

    # 系统墙钟短暂回拨时，lease 不缩短，审计更新时间也不能倒退。
    backdated = store.heartbeat(
        claim.record.event.event_id,
        worker_id="worker-a",
        fencing_token=claim.fencing_token,
        now=claimed_at + timedelta(seconds=4),
        lease_seconds=5,
    )
    assert backdated.updated_at == extended.updated_at
    assert backdated.lease_expires_at == extended.lease_expires_at

    with pytest.raises(module.EventLeaseError):
        store.heartbeat(
            claim.record.event.event_id,
            worker_id="worker-b",
            fencing_token=claim.fencing_token,
            now=claimed_at + timedelta(seconds=6),
            lease_seconds=20,
        )
    with pytest.raises(module.EventLeaseError):
        store.heartbeat(
            claim.record.event.event_id,
            worker_id="worker-a",
            fencing_token=claim.fencing_token,
            now=claimed_at + timedelta(seconds=26),
            lease_seconds=20,
        )


def test_expired_worker_cannot_commit_and_reclaim_increments_fencing() -> None:
    """租约到期即拒绝晚到结果；新 Worker 重领后旧 token 仍不能提交。"""
    module = _store_module()
    state = _state_module()
    store = module.InMemoryEventStore()
    _register(store)
    claimed_at = BASE_TIME + timedelta(seconds=2)
    first = store.claim_next("worker-a", now=claimed_at, lease_seconds=10)
    assert first is not None

    with pytest.raises(module.EventLeaseError, match="过期"):
        store.transition_inbox(
            first.record.event.event_id,
            expected_state=state.EventInboxState.PROCESSING,
            target_state=state.EventInboxState.APPLIED,
            now=claimed_at + timedelta(seconds=11),
            worker_id="worker-a",
            fencing_token=first.fencing_token,
        )

    second = store.claim_next(
        "worker-b",
        now=claimed_at + timedelta(seconds=11),
        lease_seconds=10,
    )
    assert second is not None
    assert second.fencing_token == first.fencing_token + 1
    with pytest.raises(module.EventLeaseError):
        store.transition_inbox(
            first.record.event.event_id,
            expected_state=state.EventInboxState.PROCESSING,
            target_state=state.EventInboxState.APPLIED,
            now=claimed_at + timedelta(seconds=12),
            worker_id="worker-a",
            fencing_token=first.fencing_token,
        )

    applied = store.transition_inbox(
        second.record.event.event_id,
        expected_state=state.EventInboxState.PROCESSING,
        target_state=state.EventInboxState.APPLIED,
        now=claimed_at + timedelta(seconds=12),
        worker_id="worker-b",
        fencing_token=second.fencing_token,
    )
    assert applied.state is state.EventInboxState.APPLIED
    assert applied.lease_owner is None
    assert applied.lease_expires_at is None


def test_conflict_arriving_during_processing_fences_current_worker() -> None:
    """在途处理时出现摘要冲突必须立即撤销逻辑 claim，旧 Worker 不得落终态。"""
    module = _store_module()
    state = _state_module()
    store = module.InMemoryEventStore()
    event = _event()
    _register(store, event)
    claim = store.claim_next("worker-a", now=BASE_TIME + timedelta(seconds=2), lease_seconds=30)
    assert claim is not None

    conflicting = _event(observed_version=event.observed_version + 1)
    _register(
        store,
        conflicting,
        occurrence_id="occurrence-conflict",
        offset=2,
        received_at=BASE_TIME + timedelta(seconds=3),
    )

    with pytest.raises((module.EventLeaseError, module.EventStoreInvariantError)):
        store.transition_inbox(
            event.event_id,
            expected_state=state.EventInboxState.PROCESSING,
            target_state=state.EventInboxState.APPLIED,
            now=BASE_TIME + timedelta(seconds=4),
            worker_id="worker-a",
            fencing_token=claim.fencing_token,
        )
    assert store.get_inbox(event.event_id).state is state.EventInboxState.CONFLICT


def test_application_is_unique_per_event_and_root_and_idempotently_replayed() -> None:
    """同一 event/root 只能有一个 Application，相同意图重放返回首次记录。"""
    module = _store_module()
    store = module.InMemoryEventStore()
    event = _event()
    _register(store, event)

    first = store.create_application(
        event.event_id,
        root_plan_run_id="root-plan-001",
        source_plan_version=1,
        now=BASE_TIME + timedelta(seconds=2),
    )
    replay = store.create_application(
        event.event_id,
        root_plan_run_id="root-plan-001",
        source_plan_version=1,
        now=BASE_TIME + timedelta(seconds=3),
    )

    assert first.created is True
    assert replay.created is False
    assert replay.application == first.application
    with pytest.raises(module.EventStoreInvariantError, match="source_plan_version"):
        store.create_application(
            event.event_id,
            root_plan_run_id="root-plan-001",
            source_plan_version=2,
            now=BASE_TIME + timedelta(seconds=4),
        )


def test_application_transition_persists_frozen_impact_and_terminal_failure() -> None:
    """Application 更新遵循状态机，Impact/Failure 证据进入冻结视图。"""
    module = _store_module()
    state = _state_module()
    store = module.InMemoryEventStore()
    event = _event()
    _register(store, event)
    created = store.create_application(
        event.event_id,
        root_plan_run_id="root-plan-001",
        source_plan_version=1,
        now=BASE_TIME + timedelta(seconds=2),
    )
    key = (event.event_id, "root-plan-001")

    freezing = store.transition_application(
        *key,
        expected_state=state.EventApplicationState.PENDING,
        target_state=state.EventApplicationState.FREEZING,
        now=BASE_TIME + timedelta(seconds=3),
        impact_analysis={"scope": "PRODUCT", "nodes": ["card:product-001"]},
    )
    with pytest.raises(TypeError):
        freezing.impact_analysis["scope"] = "ROOM"
    with pytest.raises(TypeError):
        freezing.impact_analysis["nodes"].append("collect-card-results")

    running = store.transition_application(
        *key,
        expected_state=state.EventApplicationState.FREEZING,
        target_state=state.EventApplicationState.EMERGENCY_RUNNING,
        now=BASE_TIME + timedelta(seconds=4),
        emergency_plan_run_id="emergency-plan-001",
    )
    with pytest.raises(module.EventStoreInvariantError, match="impact_analysis"):
        store.transition_application(
            *key,
            expected_state=state.EventApplicationState.EMERGENCY_RUNNING,
            target_state=state.EventApplicationState.REPLAN_READY,
            now=BASE_TIME + timedelta(seconds=5),
            impact_analysis={"scope": "ROOM", "nodes": []},
        )
    with pytest.raises(module.EventStoreInvariantError, match="emergency_plan_run_id"):
        store.transition_application(
            *key,
            expected_state=state.EventApplicationState.EMERGENCY_RUNNING,
            target_state=state.EventApplicationState.REPLAN_READY,
            now=BASE_TIME + timedelta(seconds=5),
            emergency_plan_run_id="emergency-plan-other",
        )
    failure = FailureFact(
        category=FailureCategory.INTERNAL_INVARIANT,
        external_code="event.application.failed",
        side_effect_state=SideEffectState.NOT_SENT,
        attempt_id="event-application-attempt",
    )
    failed = store.transition_application(
        *key,
        expected_state=state.EventApplicationState.EMERGENCY_RUNNING,
        target_state=state.EventApplicationState.FAILED,
        now=BASE_TIME + timedelta(seconds=6),
        failure=failure,
    )

    assert running.emergency_plan_run_id == "emergency-plan-001"
    assert failed.failure == failure
    assert store.get_application(*key) == failed
    with pytest.raises(state.EventStateTransitionError):
        store.transition_application(
            *key,
            expected_state=state.EventApplicationState.FAILED,
            target_state=state.EventApplicationState.PENDING,
            now=BASE_TIME + timedelta(seconds=7),
        )
    with pytest.raises(ValidationError):
        created.application.source_plan_version = 2


def test_store_views_do_not_expose_mutable_collections() -> None:
    """读取结果必须是冻结快照，外部不能改写 Store 内部 occurrence 或 application。"""
    module = _store_module()
    store = module.InMemoryEventStore()
    event = _event()
    registered = _register(store, event)

    with pytest.raises(ValidationError):
        registered.inbox.state = _state_module().EventInboxState.APPLIED
    occurrences = store.list_occurrences(event.event_id)
    assert isinstance(occurrences, tuple)
    with pytest.raises(AttributeError):
        occurrences.append(registered.occurrence)
    assert store.list_inbox() == (registered.inbox,)

    application = store.create_application(
        event.event_id,
        root_plan_run_id="root-plan-views",
        source_plan_version=1,
        now=BASE_TIME + timedelta(seconds=2),
    ).application
    assert store.list_applications() == (application,)
    assert store.list_applications(root_plan_run_id="root-plan-views") == (application,)

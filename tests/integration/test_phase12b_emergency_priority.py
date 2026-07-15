"""Phase 12B 紧急 child PlanRun 的 PostgreSQL 优先级与 lineage 契约测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import psycopg

from src.config.settings import get_settings
from src.plan_engine.capabilities import PlanCapabilityProfile
from src.plan_engine.emergency import SoldOutEmergencyProposalProvider
from src.plan_engine.event_store import (
    EventDelivery,
    PostgresEventStore,
    initialize_event_store_schema,
)
from src.plan_engine.events import InventoryFactEvent, VerifiedIngressProvenance
from src.plan_engine.models import (
    CardBatchPlanningInput,
    EmergencySoldOutPlanningInput,
    PlanNodeState,
)
from src.plan_engine.proposal import CanonicalCardBatchProposalProvider
from src.plan_engine.store import MaterializedPlan, PostgresPlanStore, initialize_plan_engine_schema
from src.skill_runtime.catalog import get_default_skill_catalog
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_catalog import CatalogProduct


NOW = datetime(2026, 7, 15, 2, tzinfo=timezone.utc)


class _SchemaSettings:
    """为兼容测试把生产连接参数限制到一个临时 PostgreSQL schema。"""

    def __init__(self, base_settings: object, schema_name: str) -> None:
        self._kwargs = dict(base_settings.postgres_connection_kwargs)
        self._kwargs["options"] = f"-c search_path={schema_name}"

    @property
    def postgres_connection_kwargs(self) -> dict[str, object]:
        """返回隔离副本，防止 Store 修改测试持有的连接配置。"""
        return dict(self._kwargs)


def _clear_previous_task7_facts(settings: object) -> None:
    """只清理本测试专用前缀，避免全局 claim 被上次失败留下的 READY 行污染。"""
    room_pattern = "room-emergency-priority-%"
    event_pattern = "event-emergency-priority-%"
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM plan_event_applications WHERE root_plan_run_id IN "
                "(SELECT plan_run_id FROM plan_runs WHERE room_id LIKE %s);",
                (room_pattern,),
            )
            for table in (
                "node_runs",
                "plan_node_dependencies",
                "plan_commands",
                "plan_nodes",
                "plan_versions",
            ):
                cursor.execute(
                    f"DELETE FROM {table} WHERE plan_run_id IN "
                    "(SELECT plan_run_id FROM plan_runs WHERE room_id LIKE %s);",
                    (room_pattern,),
                )
            cursor.execute(
                "DELETE FROM plan_runs WHERE room_id LIKE %s "
                "AND parent_plan_run_id IS NOT NULL;",
                (room_pattern,),
            )
            cursor.execute(
                "DELETE FROM plan_runs WHERE room_id LIKE %s;",
                (room_pattern,),
            )
            cursor.execute(
                "DELETE FROM plan_event_occurrences WHERE event_id LIKE %s;",
                (event_pattern,),
            )
            cursor.execute(
                "DELETE FROM plan_event_inbox WHERE event_id LIKE %s;",
                (event_pattern,),
            )
        connection.commit()


def _root_plan(room_id: str, trace_id: str) -> MaterializedPlan:
    """构造一个 READY 的普通手卡计划，用来与紧急 child 竞争全局 claim。"""
    product = CatalogProduct(
        product_id="p001",
        name="优先级测试商品",
        category="家居",
        price=Decimal("19.90"),
        inventory=10,
        conversion_rate=Decimal("0.20"),
        commission_rate=Decimal("0.10"),
        tags=["测试"],
        selling_points=["可回放"],
    )
    planning_input = CardBatchPlanningInput(
        room_id=room_id,
        trace_id=trace_id,
        live_plan=LivePlanDraft(
            room_id=room_id,
            trace_id=trace_id,
            items=[
                LivePlanItem(
                    rank=1,
                    product_id=product.product_id,
                    product_name=product.name,
                    role="引流款",
                    reason="验证紧急优先级",
                )
            ],
        ),
        products_by_id={product.product_id: product},
    )
    proposal = CanonicalCardBatchProposalProvider().propose_sync(planning_input)
    profile = PlanCapabilityProfile.default(catalog=get_default_skill_catalog())
    capabilities = {}
    for node in proposal.nodes:
        if node.logical_key == "prepare-card-batch":
            capability = profile.resolve_control_node(
                control_type=profile.PREPARE_CARD_BATCH
            )
        elif node.logical_key == "collect-card-results":
            capability = profile.resolve_control_node(
                control_type=profile.COLLECT_CARD_RESULTS
            )
        else:
            capability = profile.resolve_skill_node(
                skill_id="generate_product_card",
                room_id=room_id,
                product_id="p001",
            )
        capabilities[node.logical_key] = capability
    return MaterializedPlan(
        planning_input=planning_input,
        proposal=proposal,
        capabilities_by_logical_key=capabilities,
    )


def _emergency_plan(
    *,
    root_plan_run_id: str,
    event: InventoryFactEvent,
    provenance: VerifiedIngressProvenance,
) -> MaterializedPlan:
    """用真实 root 与 Inbox 事件构造满足关系外键的紧急计划。"""
    request = EmergencySoldOutPlanningInput(
        room_id=event.room_id,
        trace_id=f"trace-emergency-{uuid4().hex}",
        root_plan_run_id=root_plan_run_id,
        parent_plan_run_id=root_plan_run_id,
        trigger_event_id=event.event_id,
        event=event,
        provenance=provenance,
        expected_version=event.observed_version,
    )
    proposal = SoldOutEmergencyProposalProvider().propose_sync(request)
    profile = PlanCapabilityProfile.default(catalog=get_default_skill_catalog())
    capabilities = {
        node.logical_key: (
            profile.resolve_emergency_control_node(logical_key=node.logical_key)
            if node.skill_id is None
            else profile.resolve_emergency_skill_node(
                skill_id=node.skill_id,
                room_id=request.room_id,
                product_id=request.product_id,
            )
        )
        for node in proposal.nodes
    }
    return MaterializedPlan(
        planning_input=request,
        proposal=proposal,
        capabilities_by_logical_key=capabilities,
    )


def test_postgres_global_claim_prefers_emergency_child_and_persists_lineage() -> None:
    """真实 PostgreSQL 必须在普通 READY 节点之前 claim priority 100 child。"""
    settings = get_settings()
    initialize_plan_engine_schema(settings)
    initialize_event_store_schema(settings)
    _clear_previous_task7_facts(settings)
    suffix = uuid4().hex
    room_id = f"room-emergency-priority-{suffix}"
    plan_store = PostgresPlanStore(settings)
    root = plan_store.create_or_resume(_root_plan(room_id, f"trace-root-{suffix}"))
    event = InventoryFactEvent.create_sold_out(
        event_id=f"event-emergency-priority-{suffix}",
        room_id=room_id,
        product_id="p001",
        observed_version=3,
        occurred_at=NOW,
        source="inventory-service",
    )
    provenance = VerifiedIngressProvenance(
        provenance_id=f"provenance-{suffix}",
        profile_id="inventory-profile-v1",
        transport="kafka",
        topic="inventory.sold-out",
        source=event.source,
        received_at=NOW,
        payload_digest=event.payload_digest,
    )
    PostgresEventStore(settings).register_event(
        event,
        provenance,
        EventDelivery(
            occurrence_id=f"occurrence-{suffix}",
            transport="kafka",
            topic="inventory.sold-out",
            partition=0,
            offset=int(suffix[:8], 16),
            received_at=NOW,
        ),
    )
    emergency = plan_store.create_or_resume(
        _emergency_plan(
            root_plan_run_id=root.plan_run_id,
            event=event,
            provenance=provenance,
        )
    )

    def claim(worker_suffix: str) -> tuple[object, ...]:
        """每个并发 Worker 使用独立 Store/连接，真实触发数据库锁竞争。"""
        return PostgresPlanStore(settings).claim_next_ready_nodes(
            worker_id=f"worker-{worker_suffix}-{suffix}",
            now=NOW,
            lease_seconds=60,
            limit=1,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        batches = tuple(pool.map(claim, ("first", "second")))
    claims = tuple(item for batch in batches for item in batch)
    assert len(claims) == 2
    assert len({claim.node_run_id for claim in claims}) == 2
    emergency_claim = next(
        claim for claim in claims if claim.plan_run_id == emergency.plan_run_id
    )
    persisted = plan_store.get_plan_run(emergency.plan_run_id)
    assert persisted.priority == 100
    assert persisted.root_plan_run_id == root.plan_run_id
    assert persisted.trigger_event_id == event.event_id

    plan_store.record_node_result(
        node_run_id=emergency_claim.node_run_id,
        worker_id=emergency_claim.worker_id,
        claim_version=emergency_claim.claim_version,
        state=PlanNodeState.SUCCEEDED,
        output={"validated": True},
        now=NOW + timedelta(seconds=1),
    )
    next_claim = plan_store.claim_next_ready_nodes(
        worker_id=f"worker-next-{suffix}",
        now=NOW + timedelta(seconds=2),
        lease_seconds=60,
        limit=1,
    )
    assert next_claim[0].plan_run_id == emergency.plan_run_id
    assert next_claim[0].skill_id == "handle_sold_out_event"


def test_card_batch_remains_usable_before_phase12b_migration() -> None:
    """滚动发布时仅有 Phase 12A 表也必须继续支持普通手卡计划。"""
    settings = get_settings()
    schema_name = f"phase12b_compat_{uuid4().hex}"
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f'CREATE SCHEMA "{schema_name}";')
        connection.commit()
    isolated = _SchemaSettings(settings, schema_name)
    try:
        initialize_plan_engine_schema(isolated)
        store = PostgresPlanStore(isolated)
        plan_run = store.create_or_resume(
            _root_plan(
                room_id=f"room-compat-{schema_name}",
                trace_id=f"trace-compat-{schema_name}",
            )
        )
        claim = store.claim_ready_nodes(
            plan_run_id=plan_run.plan_run_id,
            worker_id="worker-phase12a-compat",
            now=NOW,
            lease_seconds=60,
            limit=1,
        )
        assert claim[0].node_type == "PREPARE_CARD_BATCH"
        store.record_node_result(
            node_run_id=claim[0].node_run_id,
            worker_id=claim[0].worker_id,
            claim_version=claim[0].claim_version,
            state=PlanNodeState.SUCCEEDED,
            output={"prepared": True},
            now=NOW + timedelta(seconds=1),
        )
    finally:
        with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
            with connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA "{schema_name}" CASCADE;')
            connection.commit()

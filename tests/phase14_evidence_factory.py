"""Phase 14 Store 测试共用的六角色 EvidenceBundle 构造器。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.decision_support.evidence import (
    AnchorRhythmPayload,
    AssembledEvidenceBundle,
    DanmakuAggregatePayload,
    DanmakuNoiseLevel,
    DanmakuTopicEvidence,
    EvidenceAssemblyRequest,
    EvidenceBundleAssembler,
    EvidenceFreshnessPolicy,
    EvidenceRole,
    EvidenceScope,
    GovernedEvidenceComponent,
    GovernedEvidenceContextResolver,
    GovernedReadOnlyEvidenceResolver,
    LiveEvidenceResolverRegistry,
    PlanEvidencePayload,
    ProductInventoryPayload,
    ProductSnapshotEvidence,
    RoleEvidenceReference,
    RhythmSignalKind,
    VerifiedEventPayload,
    governed_evidence_digest,
)
from src.decision_support.models import Incident, LiveSessionWorkspace, WorkspaceView
from src.plan_engine.event_state_machine import EventApplicationState, EventInboxState
from src.plan_engine.events import InventoryFactEvent, VerifiedIngressProvenance
from src.plan_engine.models import PlanRunKind, PlanRunState
from src.skill_runtime.models import SideEffectState
from src.specialist_runtime.models import EvidenceKind, EvidenceRef


NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)


def build_evidence_bundle(
    *,
    live_session_id: str,
    incident_id: str,
    suffix: str,
    idempotency_key: str,
    evidence_bundle_id: str | None = None,
    room_id: str | None = None,
    trace_id: str | None = None,
    anchor_id: str = "anchor-phase14",
    root_plan_run_id: str | None = None,
    created_at: datetime = NOW,
    reconciliation_required: bool = False,
    side_effect_unknown: bool = False,
    evidence_time: datetime | None = None,
    include_availability_noise: bool = True,
    pause_required: bool = True,
    valid_backup_count: int = 1,
) -> AssembledEvidenceBundle:
    """通过真实受治理 Assembler 生成可写入 Store 的最小证据链。"""

    if type(valid_backup_count) is not int or not 1 <= valid_backup_count <= 3:
        raise ValueError("valid_backup_count must be an integer from 1 through 3")
    # 默认固定时间维持历史测试字节稳定；需要验证数据库实时 freshness 的调用方显式
    # 注入 UTC 时钟，避免通过修改全局 NOW 或手工伪造 receipt 改变被测安全边界。
    reference_time = evidence_time or NOW
    resolved_room_id = room_id or f"room-{live_session_id}"
    resolved_root_plan_run_id = root_plan_run_id or f"plan-root-{live_session_id}"
    scope = EvidenceScope(
        live_session_id=live_session_id,
        incident_id=incident_id,
        room_id=resolved_room_id,
        trace_id=trace_id or f"trace-{live_session_id}",
        anchor_id=anchor_id,
        root_plan_run_id=resolved_root_plan_run_id,
    )
    occurred_at = reference_time - timedelta(seconds=8)
    event_id = f"event-{suffix}"
    event = InventoryFactEvent.create_sold_out(
        event_id=event_id,
        room_id=resolved_room_id,
        product_id="p001",
        observed_version=2,
        occurred_at=occurred_at,
        source="taobao.inventory",
    )
    provenance = VerifiedIngressProvenance(
        provenance_id=f"provenance-{suffix}",
        profile_id="taobao-inventory-v1",
        transport="KAFKA",
        topic="inventory-events",
        source=event.source,
        received_at=reference_time - timedelta(seconds=7),
        payload_digest=event.payload_digest,
    )
    components = (
        _component(
            role=EvidenceRole.VERIFIED_EVENT,
            scope=scope,
            evidence_id=event_id,
            kind=EvidenceKind.EVENT,
            source_version="2.0.0",
            observed_version=2,
            observed_at=event.occurred_at,
            received_at=provenance.received_at,
            payload=VerifiedEventPayload(
                event=event,
                provenance=provenance,
                inbox_state=EventInboxState.APPLIED,
                application_state=EventApplicationState.APPLIED,
                emergency_plan_run_id=f"plan-emergency-{suffix}",
                applied_plan_version=2,
                side_effect_state=SideEffectState.CONFIRMED,
            ),
        ),
        _component(
            role=EvidenceRole.PRODUCT_INVENTORY_SNAPSHOT,
            scope=scope,
            evidence_id=f"inventory-{suffix}",
            kind=EvidenceKind.SKILL_ATTEMPT,
            source_version="2.0.0",
            observed_version=2,
            observed_at=reference_time - timedelta(seconds=5),
            received_at=reference_time - timedelta(seconds=4),
            payload=ProductInventoryPayload(
                captured_at=reference_time - timedelta(seconds=5),
                sold_out_product_id="p001",
                expected_version=2,
                planned_product=_product("p001", "39.90", 1, 10, True),
                current_product=_product("p001", "39.90", 2, 0, False),
                # Task 5 需要覆盖三选二的每个组合；仍通过正式产品快照类型构造
                # 多备品信号，避免测试直接覆盖 snapshot 而跳过证据摘要校验。
                backup_products=tuple(
                    _product(
                        f"p{index + 2:03d}",
                        "35.90",
                        4,
                        18,
                        True,
                    )
                    for index in range(valid_backup_count)
                ),
            ),
        ),
        _component(
            role=EvidenceRole.ROOT_PLAN_SNAPSHOT,
            scope=scope,
            evidence_id=resolved_root_plan_run_id,
            kind=EvidenceKind.PLAN,
            source_version="2.0.0",
            observed_version=2,
            observed_at=reference_time - timedelta(seconds=5),
            received_at=reference_time - timedelta(seconds=4),
            payload=PlanEvidencePayload(
                captured_at=reference_time - timedelta(seconds=5),
                plan_run_id=resolved_root_plan_run_id,
                root_plan_run_id=resolved_root_plan_run_id,
                plan_kind=PlanRunKind.CARD_BATCH,
                plan_state=PlanRunState.FROZEN,
                plan_version=2,
                reconciliation_required=reconciliation_required,
                side_effect_unknown=side_effect_unknown,
            ),
        ),
        _component(
            role=EvidenceRole.EMERGENCY_PLAN_SNAPSHOT,
            scope=scope,
            evidence_id=f"plan-emergency-{suffix}",
            kind=EvidenceKind.PLAN,
            source_version="1.0.0",
            observed_version=1,
            observed_at=reference_time - timedelta(seconds=5),
            received_at=reference_time - timedelta(seconds=4),
            payload=PlanEvidencePayload(
                captured_at=reference_time - timedelta(seconds=5),
                plan_run_id=f"plan-emergency-{suffix}",
                root_plan_run_id=resolved_root_plan_run_id,
                parent_plan_run_id=resolved_root_plan_run_id,
                trigger_event_id=event_id,
                plan_kind=PlanRunKind.EMERGENCY_SOLD_OUT,
                plan_state=PlanRunState.SUCCEEDED,
                plan_version=1,
                reconciliation_required=reconciliation_required,
                side_effect_unknown=side_effect_unknown,
            ),
        ),
        _component(
            role=EvidenceRole.DANMAKU_AGGREGATE,
            scope=scope,
            evidence_id=f"danmaku-{suffix}",
            kind=EvidenceKind.AUDIT,
            source_version="3.0.0",
            observed_version=3,
            observed_at=reference_time - timedelta(seconds=2),
            received_at=reference_time - timedelta(seconds=1),
            payload=DanmakuAggregatePayload(
                aggregate_id=f"danmaku-{suffix}",
                window_start=reference_time - timedelta(seconds=10),
                window_end=reference_time - timedelta(seconds=2),
                # 默认继续构造高冲突售罄样本；Task 5 可显式关闭任一信号，复用
                # 同一受治理装配链生成“正常但仍完整”的六角色 Bundle，不能用
                # 手工篡改快照来测试选择器的未选中分支。
                noise_level=(
                    DanmakuNoiseLevel.HIGH
                    if include_availability_noise
                    else DanmakuNoiseLevel.LOW
                ),
                topics=(
                    DanmakuTopicEvidence(
                        category="PRODUCT_AVAILABILITY",
                        summary="用户集中询问主商品是否还有库存",
                        count=1,
                    ),
                ),
            ),
        ),
        _component(
            role=EvidenceRole.RHYTHM_SIGNAL,
            scope=scope,
            evidence_id=f"rhythm-{suffix}",
            kind=EvidenceKind.AUDIT,
            source_version="5.0.0",
            observed_version=5,
            observed_at=reference_time - timedelta(seconds=1),
            received_at=reference_time,
            payload=AnchorRhythmPayload(
                signal_id=f"rhythm-{suffix}",
                window_start=reference_time - timedelta(seconds=9),
                window_end=reference_time - timedelta(seconds=1),
                signal_kind=(
                    RhythmSignalKind.PAUSE_REQUIRED
                    if pause_required
                    else RhythmSignalKind.STEADY
                ),
                pace_score=82,
            ),
        ),
    )
    # Fixture 自身也必须沿用正式 Context Resolver 和六角色 Registry，不能用
    # 公开 SHA-256 手工拼装写入能力，否则会把被测安全边界变成测试旁路。
    workspace = LiveSessionWorkspace(
        live_session_id=live_session_id,
        run_key=f"phase14-evidence-fixture-{suffix}",
        room_id=scope.room_id,
        trace_id=scope.trace_id,
        anchor_id=scope.anchor_id,
        root_plan_run_id=scope.root_plan_run_id,
        event_inbox_scope_id=f"fixture-event-inbox-{suffix}",
        decision_trace_scope_id=f"fixture-trace-{suffix}",
        replay_scope_id=f"fixture-replay-{suffix}",
        evaluation_scope_id=f"fixture-evaluation-{suffix}",
        view=WorkspaceView.LIVE,
    )
    incident = Incident(
        incident_id=incident_id,
        live_session_id=live_session_id,
        idempotency_key=f"fixture-incident-{suffix}",
        incident_type="SOLD_OUT_COMPOSITE",
        source_ref_ids=(event_id,),
        snapshot={"product_id": "p001", "expected_version": 2},
        created_at=created_at,
    )
    registry = LiveEvidenceResolverRegistry(
        {
            component.role: GovernedReadOnlyEvidenceResolver(
                resolver_id=f"fixture-{component.role.value.lower()}",
                resolver_version="1.0.0",
                role=component.role,
                loader=lambda _evidence_id, item=component: item,
            )
            for component in components
        }
    )
    request = EvidenceAssemblyRequest(
        evidence_bundle_id=evidence_bundle_id or f"evidence-{suffix}",
        idempotency_key=idempotency_key,
        live_session_id=live_session_id,
        incident_id=incident_id,
        references=tuple(
            RoleEvidenceReference(role=item.role, reference=item.reference)
            for item in components
        ),
    )
    return EvidenceBundleAssembler(
        context_resolver=GovernedEvidenceContextResolver(
            workspace_loader=lambda _live_session_id: workspace,
            incident_loader=lambda _incident_id: incident,
        ),
        registry=registry,
        freshness_policy=EvidenceFreshnessPolicy.default(),
        clock=lambda: reference_time,
    ).assemble(request)


def _product(
    product_id: str, price: str, version: int, inventory: int, is_active: bool
) -> ProductSnapshotEvidence:
    return ProductSnapshotEvidence(
        product_id=product_id,
        name=product_id,
        price=price,
        inventory=inventory,
        version=version,
        is_active=is_active,
    )


def _component(
    *,
    role: EvidenceRole,
    scope: EvidenceScope,
    evidence_id: str,
    kind: EvidenceKind,
    source_version: str,
    observed_version: int,
    observed_at: datetime,
    received_at: datetime,
    payload: object,
) -> GovernedEvidenceComponent:
    digest = governed_evidence_digest(
        role=role,
        scope=scope,
        evidence_id=evidence_id,
        source_version=source_version,
        observed_version=observed_version,
        observed_at=observed_at,
        received_at=received_at,
        payload=payload,
    )
    return GovernedEvidenceComponent(
        role=role,
        reference=EvidenceRef(
            kind=kind,
            evidence_id=evidence_id,
            source_version=source_version,
            digest=digest,
            room_id=scope.room_id,
            anchor_id=scope.anchor_id,
        ),
        scope=scope,
        observed_version=observed_version,
        observed_at=observed_at,
        received_at=received_at,
        payload=payload,
    )

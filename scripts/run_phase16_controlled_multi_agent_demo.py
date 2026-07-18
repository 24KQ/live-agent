"""Phase 16 受控双 Agent 升级的确定性本地 Demo 与 Acceptance 生成器。

本脚本只在内存中回放 ``live-session-p001-sold-out-v2``。它通过真实的
EvidenceBundle、HighConflictEscalationCoordinator、Store 和 CommandCompiler 证明：
确定性售罄保护先行，高冲突时才顺序运行 Analyst 与 Planner，经营恢复仍必须经过
人工决定。这里的 ScriptedModel 只用于可重复技术验收，绝不连接真实模型或提交业务命令。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path
import sys
from threading import RLock
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    # 直接执行脚本时 Python 只会把 scripts 目录放入模块搜索路径；此处补充仓库根目录，
    # 不改变运行时服务的导入语义，也不会读取任何外部配置或密钥。
    sys.path.insert(0, str(PROJECT_ROOT))

from src.decision_support.commands import (
    DecisionExecutionContext,
    DecisionSupportCommandCompiler,
    OperatorDecisionDraft,
    OperatorModification,
)
from src.decision_support.evidence import (
    AnchorRhythmPayload,
    DanmakuAggregatePayload,
    DanmakuNoiseLevel,
    DanmakuTopicEvidence,
    EvidenceAssemblyRequest,
    EvidenceBundleAssembler,
    EvidenceBundleSnapshot,
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
    RhythmSignalKind,
    RoleEvidenceReference,
    VerifiedEventPayload,
    governed_evidence_digest,
)
from src.decision_support.models import (
    DecisionKind,
    Incident,
    LiveSessionWorkspace,
    MultiAgentOutcome,
    MultiAgentOutcomeStatus,
    OperatorLease,
    WorkspaceView,
)
from src.decision_support.multi_agent import (
    HighConflictEscalationCoordinator,
    build_decision_planner_profile,
    build_evidence_analyst_profile,
)
from src.decision_support.multi_agent_evaluation import (
    Phase16CaseKind,
    generate_phase16_controlled_multi_agent_dataset,
    load_phase16_controlled_multi_agent_dataset,
    run_phase16_scripted_evaluation,
)
from src.decision_support.multi_agent_smoke import (
    FORMAL_ENDPOINT_HOST,
    FORMAL_MODEL_ID,
    INPUT_PRICE_CNY_PER_MILLION,
    OUTPUT_PRICE_CNY_PER_MILLION,
    PHASE16_MULTI_AGENT_SMOKE,
    Phase16OfficialPriceEvidence,
    Phase16SmokeBudgetStore,
    Phase16SmokeConfig,
    Phase16SmokeRunner,
    phase16_smoke_runtime_digest,
    preflight_phase16_multi_agent_smoke,
)
from src.decision_support.proposal import DecisionTiming
from src.decision_support.routing import DecisionSupportRoute, DecisionSupportRoutePolicy
from src.decision_support.store import InMemoryDecisionSupportStore
from src.config.settings import Settings
from src.plan_engine.capabilities import PlanCapabilityProfile
from src.plan_engine.event_store import EventDelivery, InMemoryEventStore
from src.plan_engine.event_state_machine import EventApplicationState, EventInboxState
from src.plan_engine.events import InventoryFactEvent, VerifiedIngressProvenance
from src.plan_engine.models import CardBatchPlanningInput, PlanNodeState, PlanRunKind, PlanRunState
from src.plan_engine.preemption import PreemptionCoordinator, PreemptionStatus
from src.plan_engine.proposal import CanonicalCardBatchProposalProvider
from src.plan_engine.replan import ReplanCoordinator
from src.plan_engine.side_effect_reconciliation import (
    SoldOutReconciliationResult,
    SoldOutReconciliationStatus,
)
from src.plan_engine.store import InMemoryPlanStore, MaterializedPlan, PlanStoreInvariantError
from src.plan_engine.worker import PlanWorker
from src.skill_runtime.catalog import get_default_skill_catalog
from src.skill_runtime.models import (
    FailureCategory,
    FailureFact,
    SideEffectState,
    SkillExecutionResult,
    SkillExecutionStatus,
)
from src.specialist_runtime.models import (
    AgentResult,
    AgentResultStatus,
    AgentTask,
    EvidenceKind,
    EvidenceRef,
    canonical_json_sha256,
)
from src.specialist_runtime.profiles import SpecialistProfile
from src.skills.live_plan_generator import LivePlanDraft, LivePlanItem
from src.skills.product_card_generator import generate_product_card
from src.skills.product_catalog import CatalogProduct


DEMO_LIVE_SESSION_ID = "live-session-p001-sold-out-v2"
DEMO_ROOM_ID = "room-phase16-demo"
DEMO_TRACE_ID = "trace-phase16-demo"
DEMO_ROOT_PLAN_ID = "plan-root-phase16-demo"
DEMO_EVENT_ID = "event-phase16-demo-sold-out"
DEMO_INCIDENT_ID = f"incident:{DEMO_EVENT_ID}:{DEMO_ROOT_PLAN_ID}"
# 内存 Demo 的 append-only 事实必须可逐字节复现，而 InMemory Store 的升级新鲜度检查
# 仍使用真实事务时钟。选择远期固定时钟可同时满足两者：证据时间与摘要稳定，Store 也会
# 像生产环境一样自行判定 Bundle 尚未超过 TTL，绝不为 Demo 放宽 freshness 规则。
DEMO_NOW = datetime(2099, 1, 1, 16, 0, tzinfo=timezone.utc)
DEMO_EVALUATION_DIRECTORY = "phase16_controlled_multi_agent"
# InMemoryPlanStore 在历史实现中以模块级 uuid4 生成临时身份。Demo 为获得跨进程可比的
# 审计摘要必须短暂替换它；锁将替换范围串行化，避免并行 Demo/测试互相覆盖恢复函数。
_DEMO_PLAN_IDENTIFIER_LOCK = RLock()


class Phase16AcceptanceStatus(StrEnum):
    """Phase 16 的最终结论；缺少真实 smoke 证据时不能被本地演练提升为 PASS。"""

    PASS = "PASS"
    INCONCLUSIVE = "INCONCLUSIVE"
    FAIL = "FAIL"


class Phase16DemoResult(BaseModel):
    """本地受控双 Agent Demo 的稳定、无自由文本的验收投影。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Phase16AcceptanceStatus
    phase_state: str = Field(default="AWAITING_PHASE_17_GATE", min_length=1)
    live_session_id: str = Field(..., min_length=1)
    incident_id: str = Field(..., min_length=1)
    evidence_bundle_id: str = Field(..., min_length=1)
    evidence_bundle_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    automatic_protection_status: str
    automatic_protection_authoritative: bool
    automatic_protection_event_application_state: str
    automatic_protection_external_write_count: int = Field(..., ge=0, strict=True)
    automatic_protection_root_plan_run_id: str = Field(..., min_length=1)
    automatic_protection_evidence_bound: bool
    execution_order: tuple[str, ...]
    dual_agent_call_sequence: tuple[str, ...]
    dual_agent_call_counts: dict[str, int]
    escalation_id: str = Field(..., min_length=1)
    escalation_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    analysis_id: str = Field(..., min_length=1)
    analysis_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    ready_proposal_id: str = Field(..., min_length=1)
    ready_proposal_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    ready_proposal_origin: str
    ready_outcome_id: str = Field(..., min_length=1)
    ready_outcome_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    ready_outcome_status: str
    ready_lineage_complete: bool
    operator_decision_kinds: tuple[str, ...]
    selected_operator_decision_kind: str
    compiled_command_id: str = Field(..., min_length=1)
    compiled_command_context_bound: bool
    execution_command_persisted: bool
    execution_command_submitted: bool
    execution_submission_count: int = Field(..., ge=0, strict=True)
    replay_stable: bool
    restart_store_reconstructed: bool
    replay_agent_call_sequence: tuple[str, ...]
    audit_projection_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    replay_audit_projection_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    production_default_route: str
    task9_dataset_id: str = Field(..., min_length=1)
    task9_manifest_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    task9_source_code_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    task9_profile_digests: dict[str, str]
    task9_total_cases: int = Field(..., ge=48, strict=True)
    task9_route_correct_cases: int = Field(..., ge=48, strict=True)
    task9_pairwise_identity_correct_cases: int = Field(..., ge=24, strict=True)
    task9_analyst_calls: int = Field(..., ge=30, strict=True)
    task9_planner_calls: int = Field(..., ge=26, strict=True)
    task9_ready_outcomes: int = Field(..., ge=24, strict=True)
    task9_degraded_outcomes: int = Field(..., ge=6, strict=True)
    task9_no_send_cases: int = Field(..., ge=18, strict=True)
    task9_scripted_reserved_cost_cny: str = Field(..., pattern=r"^\d+\.\d{2}$")
    real_smoke_scope_id: str
    real_smoke_status: str
    real_smoke_reason_codes: tuple[str, ...]
    real_model_call_count: int = Field(..., ge=0, strict=True)
    real_model_cost_cny: str = Field(..., pattern=r"^\d+\.\d{6}$")


@dataclass(frozen=True)
class _AuthoritativeProtectionTrace:
    """正式 Phase 12B Coordinator 产生的最小可审计保护投影，不携带随机内部 ID。"""

    status: PreemptionStatus
    event_id: str
    event: InventoryFactEvent
    provenance: VerifiedIngressProvenance
    inbox_state: EventInboxState
    event_application_state: EventApplicationState
    external_write_count: int
    root_plan_run_id: str
    root_plan_version: int
    root_plan_state: PlanRunState
    emergency_plan_run_id: str
    emergency_plan_version: int
    emergency_plan_state: PlanRunState
    recovery_node_id: str
    recovery_plan_version: int
    plan_store: InMemoryPlanStore


@contextmanager
def _deterministic_plan_identifier_scope() -> Any:
    """只在单进程 Demo 内替换 InMemoryPlanStore 的 UUID 生成器，确保跨次审计可比较。

    正式 Store 的 UUID 行为不变。该受限作用域包围完整的内存 Phase 12B Fixture，并在 finally
    中无条件还原模块函数，因此它不能泄漏到服务进程、测试并发或真实 PostgreSQL 路径。
    """

    import src.plan_engine.store as plan_store_module

    with _DEMO_PLAN_IDENTIFIER_LOCK:
        original_uuid4 = plan_store_module.uuid4
        sequence = 0

        def next_uuid() -> Any:
            nonlocal sequence
            sequence += 1
            return uuid5(NAMESPACE_URL, f"phase16-demo-plan-id:{sequence}")

        plan_store_module.uuid4 = next_uuid
        try:
            yield
        finally:
            plan_store_module.uuid4 = original_uuid4


class _AuthoritativeProtectionExecutor:
    """只为本地 Demo 提供确定性 Skill 输出，实际抢占/对账/重排仍由正式 Coordinator 执行。"""

    def __init__(self) -> None:
        self.external_write_count = 0
        self._hold_next_product_card_for_operator = False

    def require_next_product_card_approval(self) -> None:
        """让 Replan 后下一张手卡经正式 Worker 进入 WAITING_APPROVAL，供人工命令上下文读取。"""

        self._hold_next_product_card_for_operator = True

    async def execute(self, call: Any) -> SkillExecutionResult:
        """让售罄写先进入未知副作用，再由正式只读对账闭合，证明不会重复发送。"""

        if call.skill_id == "generate_product_card":
            product = CatalogProduct.model_validate(call.arguments["product"])
            if self._hold_next_product_card_for_operator:
                self._hold_next_product_card_for_operator = False
                return SkillExecutionResult(
                    skill_id=call.skill_id,
                    version=call.version,
                    status=SkillExecutionStatus.PENDING,
                    summary="operator approval is required before recovery card execution",
                )
            return SkillExecutionResult(
                skill_id=call.skill_id,
                version=call.version,
                status=SkillExecutionStatus.SUCCESS,
                output={"card": generate_product_card(product).model_dump(mode="json")},
                summary="Phase 16 demo card generated",
            )
        if call.skill_id == "handle_sold_out_event":
            self.external_write_count += 1
            failure = FailureFact(
                category=FailureCategory.SIDE_EFFECT_UNKNOWN,
                external_code="phase16.demo.sold_out_unknown_after_send",
                side_effect_state=SideEffectState.UNKNOWN,
                attempt_id="attempt-phase16-demo-sold-out",
            )
            return SkillExecutionResult(
                skill_id=call.skill_id,
                version=call.version,
                status=SkillExecutionStatus.ERROR,
                summary="sold-out write sent with unknown outcome",
                failure=failure,
                attempt_id=failure.attempt_id,
            )
        outputs = {
            "recommend_backup_product": {"backup_product": {"product_id": "p002"}},
            "generate_on_live_prompt": {"prompt": {"message": "p001 sold out; await operator decision"}},
        }
        return SkillExecutionResult(
            skill_id=call.skill_id,
            version=call.version,
            status=SkillExecutionStatus.SUCCESS,
            output=outputs[call.skill_id],
            summary="Phase 16 demo emergency fact generated",
        )


class _AuthoritativeProtectionReconciler:
    """严格只读对账替身：只确认原 Attempt，不能创建第二次售罄写。"""

    async def reconcile(self, request: Any) -> SoldOutReconciliationResult:
        """返回绑定原 failure attempt 的确认事实，供正式 Coordinator 继续恢复。"""

        return SoldOutReconciliationResult(
            status=SoldOutReconciliationStatus.CONFIRMED_SUCCESS,
            original_attempt_id=request.original_failure.attempt_id,
            evidence={
                "event_id": request.event_authorization.event_id,
                "product_id": request.product_id,
                "confirmed_version": request.expected_version + 1,
            },
            reason_code="SOLD_OUT_FACT_CONFIRMED",
        )


class _NoSendModelPort:
    """真实 smoke 预检必须阻断时的保护替身；任何调用都说明门禁被绕过。"""

    async def complete(self, request: Any) -> Any:
        """阻断意外模型发送，避免 Demo 因未来回归访问外部 endpoint。"""

        del request
        raise AssertionError("Phase 16 Demo 的真实模型 smoke 必须在预检阶段被阻断")


class _DeterministicDemoRunner:
    """只实现受控双 Agent 的固定成功回放，不暴露 Tool、Store 或执行命令能力。"""

    def __init__(self) -> None:
        self.calls: list[AgentTask] = []

    def resolve_profile(self, task: AgentTask) -> SpecialistProfile:
        """Coordinator 发送前会复核完整 Profile；未知任务类型不能借 Demo 取得路由。"""

        if task.task_kind.value == "CONFLICT_ANALYSIS":
            return build_evidence_analyst_profile()
        if task.task_kind.value == "LIVE_DECISION_PLANNING":
            return build_decision_planner_profile()
        raise ValueError("Phase 16 Demo only supports controlled dual-agent tasks")

    async def run(self, task: AgentTask) -> AgentResult:
        """根据 Coordinator 已绑定的冻结任务生成结构化结果，禁止从 Store 补取事实。"""

        profile = self.resolve_profile(task)
        references = tuple(task.initial_evidence_refs)
        self.calls.append(task)
        if task.task_kind.value == "CONFLICT_ANALYSIS":
            input_snapshot = task.model_dump(mode="json")["input_snapshot"]
            output = {
                "finding_codes": input_snapshot["trigger_codes"],
                "constraint_codes": ["OPERATOR_CONFIRMATION_REQUIRED"],
                "risk_codes": ["INVENTORY_CONFLICT_REQUIRES_REVIEW"],
                "explanation": "Governed sold-out facts require an operator decision.",
                "evidence_refs": [
                    reference.model_dump(mode="json") for reference in references
                ],
            }
        elif task.task_kind.value == "LIVE_DECISION_PLANNING":
            output = {
                "options": [
                    {
                        "option_id": "switch-backup",
                        "product_strategy": "SWITCH_TO_BACKUP",
                        "backup_product_id": "p002",
                        "host_prompt": "请运营确认后切换备品 p002，并提示观众库存已更新。",
                        "timing": "AFTER_OPERATOR_CONFIRMATION",
                        "risk_flags": [
                            "BACKUP_PRODUCT_REQUIRES_CONFIRMATION",
                            "HUMAN_CONFIRMATION_REQUIRED",
                            "INVENTORY_CONFLICT_REQUIRES_REVIEW",
                        ],
                        "evidence_refs": [
                            reference.model_dump(mode="json")
                            for reference in references
                        ],
                    }
                ]
            }
        else:
            raise ValueError("Phase 16 Demo received an unsupported task")
        return AgentResult(
            task_id=task.task_id,
            profile_id=task.profile_id,
            profile_version=task.profile_version,
            status=AgentResultStatus.SUCCEEDED,
            output=output,
            evidence_refs=references,
            summary="SCRIPTED_PHASE16_DEMO_SUCCEEDED",
            model_calls=1,
            cost_cny=profile.max_case_cost_cny,
        )


def _product(
    product_id: str,
    price: str,
    version: int,
    inventory: int,
    is_active: bool,
) -> ProductSnapshotEvidence:
    """构造严格产品快照，避免 Demo 把自由 JSON 伪装成可信库存事实。"""

    return ProductSnapshotEvidence(
        product_id=product_id,
        name=product_id,
        price=price,
        inventory=inventory,
        version=version,
        is_active=is_active,
    )


def _plan_product(product_id: str, rank: int) -> CatalogProduct:
    """构造正式 PlanEngine 所需的冻结货盘快照，与后续 Evidence 中的售罄商品保持一致。"""

    return CatalogProduct(
        product_id=product_id,
        name=f"Phase 16 demo product {rank}",
        category="demo",
        price=Decimal("29.90") + Decimal(rank),
        inventory=20 + rank,
        conversion_rate=Decimal("0.20"),
        commission_rate=Decimal("0.10"),
        tags=["demo"],
        selling_points=["stable fixture", "controlled recovery"],
    )


def _authoritative_planning_input() -> CardBatchPlanningInput:
    """用当前演示房间和 Trace 创建 root PlanRun，禁止借用旧 v1 场景身份。"""

    products = {
        product_id: _plan_product(product_id, index)
        for index, product_id in enumerate(("p001", "p002", "p003"), 1)
    }
    return CardBatchPlanningInput(
        room_id=DEMO_ROOM_ID,
        trace_id=DEMO_TRACE_ID,
        live_plan=LivePlanDraft(
            room_id=DEMO_ROOM_ID,
            trace_id=DEMO_TRACE_ID,
            items=[
                LivePlanItem(
                    rank=index,
                    product_id=product_id,
                    product_name=products[product_id].name,
                    role="demo",
                    reason="Phase 16 authoritative protection fixture",
                )
                for index, product_id in enumerate(products, 1)
            ],
        ),
        products_by_id=products,
    )


def _authoritative_root_plan(
    request: CardBatchPlanningInput,
) -> MaterializedPlan:
    """通过正式 ProposalProvider 和 CapabilityProfile 物化 root DAG，不手写节点权限。"""

    proposal = CanonicalCardBatchProposalProvider().propose_sync(request)
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
                skill_id=node.skill_id,
                product_id=node.logical_key.removeprefix("card:"),
                room_id=request.room_id,
            )
        capabilities[node.logical_key] = capability
    return MaterializedPlan(
        planning_input=request,
        proposal=proposal,
        capabilities_by_logical_key=capabilities,
    )


def _register_authoritative_sold_out_event(
    event_store: InMemoryEventStore,
) -> InventoryFactEvent:
    """登记当前 v2 Demo 的可信售罄事实；Coordinator 只能从 Inbox 读取它。"""

    event = InventoryFactEvent.create_sold_out(
        event_id=DEMO_EVENT_ID,
        room_id=DEMO_ROOM_ID,
        product_id="p001",
        observed_version=2,
        occurred_at=DEMO_NOW - timedelta(seconds=2),
        source="inventory-service",
    )
    provenance = VerifiedIngressProvenance(
        provenance_id="provenance-phase16-authoritative-protection",
        profile_id="inventory-profile-v1",
        transport="KAFKA",
        topic="inventory.sold-out",
        source=event.source,
        received_at=DEMO_NOW - timedelta(seconds=1),
        payload_digest=event.payload_digest,
    )
    event_store.register_event(
        event,
        provenance,
        EventDelivery(
            occurrence_id="occurrence-phase16-authoritative-protection",
            transport="KAFKA",
            topic="inventory.sold-out",
            partition=0,
            offset=16,
            received_at=DEMO_NOW - timedelta(seconds=1),
        ),
    )
    return event


def _run_authoritative_protection() -> _AuthoritativeProtectionTrace:
    """运行完整 Phase 12B Coordinator 链，证明保护不是由 Phase 16 Demo 自行伪造。"""

    # InMemoryPlanStore 默认 UUID 用于业务运行时唯一性；Demo 需要独立执行两次并比较
    # 审计投影，因此仅在这个不连接外部服务的短作用域内注入可复现身份序列。
    with _deterministic_plan_identifier_scope():
        request = _authoritative_planning_input()
        plan_store = InMemoryPlanStore()
        root = plan_store.create_or_resume(_authoritative_root_plan(request))
        executor = _AuthoritativeProtectionExecutor()
        event_store = InMemoryEventStore()
        worker = PlanWorker(
            store=plan_store,
            event_store=event_store,
            skill_executor=executor,
            worker_id="worker-phase16-authoritative-protection",
            clock=lambda: DEMO_NOW + timedelta(seconds=1),
            max_claims=3,
        )
        event = _register_authoritative_sold_out_event(event_store)
        # 先运行 root 的手卡节点，保留汇总节点未完成，从而让正式 Coordinator 对受影响分支
        # 执行 freeze、紧急 child、只读对账与 Replan，而不是直接伪造 APPLIED 结果。
        asyncio.run(worker.run_once(root.plan_run_id))
        asyncio.run(worker.run_once(root.plan_run_id))
        coordinator = PreemptionCoordinator(
            plan_store=plan_store,
            event_store=event_store,
            emergency_worker=worker,
            replan_coordinator=ReplanCoordinator(
                plan_store=plan_store,
                event_store=event_store,
            ),
            reconciliation_service=_AuthoritativeProtectionReconciler(),
            worker_id="coordinator-phase16-authoritative-protection",
            clock=lambda: DEMO_NOW + timedelta(seconds=2),
        )
        waiting = asyncio.run(
            coordinator.run_next(root_plan_run_id=root.plan_run_id, now=DEMO_NOW)
        )
        reconciled = asyncio.run(
            coordinator.reconcile_waiting(
                event_id=event.event_id,
                root_plan_run_id=root.plan_run_id,
                now=DEMO_NOW + timedelta(seconds=1),
            )
        )
        applied = asyncio.run(
            coordinator.run_next(
                root_plan_run_id=root.plan_run_id,
                now=DEMO_NOW + timedelta(seconds=2),
            )
        )
        if (
            waiting.status is not PreemptionStatus.WAITING_RECONCILIATION
            or reconciled.status is not PreemptionStatus.RETRY_PENDING
            or applied.status is not PreemptionStatus.APPLIED
            or applied.evidence_ref is None
            or executor.external_write_count != 1
        ):
            raise AssertionError("authoritative Phase 12B protection fixture did not close")
        application = event_store.get_application(event.event_id, root.plan_run_id)
        inbox = event_store.get_inbox(event.event_id)
        # 真实 Replan 当前版本中的恢复手卡必须再次经 Worker 进入 WAITING_APPROVAL。这里
        # 不伪造 node_id 或状态：只读取 PlanStore 物化出来的节点，后续 Compiler 才能得到
        # 可被 PlanStore 接受的 CAS 上下文，而 Demo 仍然不会提交该命令。
        executor.require_next_product_card_approval()
        recovery_node = None
        for _ in range(4):
            asyncio.run(worker.run_once(root.plan_run_id))
            current = plan_store.get_plan_run(root.plan_run_id)
            recovery_node = next(
                (
                    node
                    for node in plan_store.list_nodes(
                        root.plan_run_id, current.current_version
                    )
                    if node.state is PlanNodeState.WAITING_APPROVAL
                ),
                None,
            )
            if recovery_node is not None:
                break
        if recovery_node is None:
            raise AssertionError("authoritative recovery node did not reach WAITING_APPROVAL")
        # Phase 12B 完成增量 Replan 后 root 可重新处于 ACTIVE；进入高冲突人工决策窗口前
        # 必须再次走 PlanStore 的公开 fail-safe freeze，确保 EvidenceAssembler 和后续
        # Compiler 都面对“恢复仍被阻断”的当前事实，而不是拼接旧冻结快照。
        root_after = plan_store.freeze_plan(plan_run_id=root.plan_run_id)
        emergency_after = plan_store.get_plan_run(
            applied.evidence_ref.emergency_plan_run_id
        )
        if (
            application.state is not EventApplicationState.APPLIED
            or inbox.state is not EventInboxState.APPLIED
        ):
            raise AssertionError("authoritative protection did not persist applied inbox facts")
        return _AuthoritativeProtectionTrace(
            status=applied.status,
            event_id=event.event_id,
            event=event,
            provenance=inbox.provenance,
            inbox_state=inbox.state,
            event_application_state=application.state,
            external_write_count=executor.external_write_count,
            root_plan_run_id=root_after.plan_run_id,
            root_plan_version=root_after.current_version,
            root_plan_state=root_after.state,
            emergency_plan_run_id=applied.evidence_ref.emergency_plan_run_id,
            emergency_plan_version=applied.evidence_ref.applied_plan_version,
            emergency_plan_state=emergency_after.state,
            recovery_node_id=recovery_node.node_id,
            recovery_plan_version=root_after.current_version,
            plan_store=plan_store,
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
    """按正式受治理摘要规则构造单个组件，杜绝绕过六角色 Bundle 校验。"""

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


def _assemble_demo_bundle(
    *,
    workspace: LiveSessionWorkspace,
    incident: Incident,
    protection: _AuthoritativeProtectionTrace,
) -> Any:
    """用正式 Assembler 创建固定六角色 EvidenceBundle，而不是直接构造 Store 快照。"""

    scope = EvidenceScope(
        live_session_id=workspace.live_session_id,
        incident_id=incident.incident_id,
        room_id=workspace.room_id,
        trace_id=workspace.trace_id,
        anchor_id=workspace.anchor_id,
        root_plan_run_id=workspace.root_plan_run_id,
    )
    # 直接使用权威 Coordinator 已消费并成功应用的同一事件/provenance；调用方不能在
    # Demo 层重新创建一个“看起来相同”的售罄事实而断开保护与多 Agent 的因果链。
    event = protection.event
    provenance = protection.provenance
    components = (
        _component(
            role=EvidenceRole.VERIFIED_EVENT,
            scope=scope,
            evidence_id=DEMO_EVENT_ID,
            kind=EvidenceKind.EVENT,
            source_version="2.0.0",
            observed_version=2,
            observed_at=event.occurred_at,
            received_at=provenance.received_at,
            payload=VerifiedEventPayload(
                event=event,
                provenance=provenance,
                inbox_state=protection.inbox_state,
                application_state=protection.event_application_state,
                emergency_plan_run_id=protection.emergency_plan_run_id,
                applied_plan_version=protection.root_plan_version,
                side_effect_state=SideEffectState.CONFIRMED,
            ),
        ),
        _component(
            role=EvidenceRole.PRODUCT_INVENTORY_SNAPSHOT,
            scope=scope,
            evidence_id="inventory-phase16-demo",
            kind=EvidenceKind.SKILL_ATTEMPT,
            source_version="2.0.0",
            observed_version=2,
            observed_at=DEMO_NOW - timedelta(seconds=5),
            received_at=DEMO_NOW - timedelta(seconds=4),
            payload=ProductInventoryPayload(
                captured_at=DEMO_NOW - timedelta(seconds=5),
                sold_out_product_id="p001",
                expected_version=event.observed_version,
                planned_product=_product("p001", "39.90", 1, 10, True),
                current_product=_product(
                    "p001", "39.90", event.observed_version, 0, False
                ),
                backup_products=(
                    _product("p002", "35.90", 4, 8, True),
                    _product("p003", "32.90", 3, 6, True),
                ),
            ),
        ),
        _component(
            role=EvidenceRole.ROOT_PLAN_SNAPSHOT,
            scope=scope,
            evidence_id=workspace.root_plan_run_id,
            kind=EvidenceKind.PLAN,
            source_version="2.0.0",
            observed_version=protection.root_plan_version,
            observed_at=DEMO_NOW - timedelta(seconds=5),
            received_at=DEMO_NOW - timedelta(seconds=4),
            payload=PlanEvidencePayload(
                captured_at=DEMO_NOW - timedelta(seconds=5),
                plan_run_id=workspace.root_plan_run_id,
                root_plan_run_id=workspace.root_plan_run_id,
                plan_kind=PlanRunKind.CARD_BATCH,
                plan_version=protection.root_plan_version,
                plan_state=protection.root_plan_state,
                reconciliation_required=False,
                side_effect_unknown=False,
            ),
        ),
        _component(
            role=EvidenceRole.EMERGENCY_PLAN_SNAPSHOT,
            scope=scope,
            evidence_id=protection.emergency_plan_run_id,
            kind=EvidenceKind.PLAN,
            source_version="1.0.0",
            observed_version=protection.emergency_plan_version,
            observed_at=DEMO_NOW - timedelta(seconds=5),
            received_at=DEMO_NOW - timedelta(seconds=4),
            payload=PlanEvidencePayload(
                captured_at=DEMO_NOW - timedelta(seconds=5),
                plan_run_id=protection.emergency_plan_run_id,
                root_plan_run_id=workspace.root_plan_run_id,
                parent_plan_run_id=workspace.root_plan_run_id,
                trigger_event_id=DEMO_EVENT_ID,
                plan_kind=PlanRunKind.EMERGENCY_SOLD_OUT,
                plan_state=protection.emergency_plan_state,
                plan_version=protection.emergency_plan_version,
                reconciliation_required=False,
                side_effect_unknown=False,
            ),
        ),
        _component(
            role=EvidenceRole.DANMAKU_AGGREGATE,
            scope=scope,
            evidence_id="danmaku-phase16-demo",
            kind=EvidenceKind.AUDIT,
            source_version="3.0.0",
            observed_version=3,
            observed_at=DEMO_NOW - timedelta(seconds=2),
            received_at=DEMO_NOW - timedelta(seconds=1),
            payload=DanmakuAggregatePayload(
                aggregate_id="danmaku-phase16-demo",
                window_start=DEMO_NOW - timedelta(seconds=10),
                window_end=DEMO_NOW - timedelta(seconds=2),
                noise_level=DanmakuNoiseLevel.HIGH,
                topics=(
                    DanmakuTopicEvidence(
                        category="PRODUCT_AVAILABILITY",
                        summary="用户集中询问主商品是否还有库存",
                        count=12,
                    ),
                ),
            ),
        ),
        _component(
            role=EvidenceRole.RHYTHM_SIGNAL,
            scope=scope,
            evidence_id="rhythm-phase16-demo",
            kind=EvidenceKind.AUDIT,
            source_version="5.0.0",
            observed_version=5,
            observed_at=DEMO_NOW - timedelta(seconds=1),
            received_at=DEMO_NOW,
            payload=AnchorRhythmPayload(
                signal_id="rhythm-phase16-demo",
                window_start=DEMO_NOW - timedelta(seconds=9),
                window_end=DEMO_NOW - timedelta(seconds=1),
                signal_kind=RhythmSignalKind.PAUSE_REQUIRED,
                pace_score=65,
            ),
        ),
    )
    registry = LiveEvidenceResolverRegistry(
        {
            component.role: GovernedReadOnlyEvidenceResolver(
                resolver_id=f"phase16-demo-{component.role.value.lower()}",
                resolver_version="1.0.0",
                role=component.role,
                loader=lambda _evidence_id, item=component: item,
            )
            for component in components
        }
    )
    request = EvidenceAssemblyRequest(
        evidence_bundle_id="bundle-phase16-demo",
        idempotency_key="bundle-phase16-demo",
        live_session_id=workspace.live_session_id,
        incident_id=incident.incident_id,
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
        clock=lambda: DEMO_NOW,
    ).assemble(request)


def _load_or_generate_dataset(evaluation_root: Path) -> Any:
    """优先读取提交的冻结资产；单元测试临时目录缺失时只生成同一字节稳定数据集。"""

    root = (
        evaluation_root
        if evaluation_root.name == DEMO_EVALUATION_DIRECTORY
        else evaluation_root / DEMO_EVALUATION_DIRECTORY
    )
    if not (root / "manifest.json").exists():
        generate_phase16_controlled_multi_agent_dataset(root)
    return load_phase16_controlled_multi_agent_dataset(root)


def _audit_projection(
    store: InMemoryDecisionSupportStore,
    live_session_id: str,
) -> dict[str, Any]:
    """只投影 append-only 审计事实，保证重放比较不依赖对象地址或进程内引用。"""

    escalations = store.list_escalations(live_session_id)
    # lease 与两个 dispatch claim 都会影响重启后是否允许再发一次模型。它们必须进入
    # Demo 的稳定审计投影，不能只比较终态 Proposal/Outcome 而遗漏线性化事实。
    operator_lease = store.acquire_operator_lock(
        live_session_id,
        "operator-phase16-demo",
        60,
        now=DEMO_NOW,
    )
    return {
        "workspace": store.get_workspace(live_session_id).model_dump(mode="json"),
        "operator_lease": operator_lease.model_dump(mode="json"),
        "incidents": [
            item.model_dump(mode="json") for item in store.list_incidents(live_session_id)
        ],
        "evidence_bundles": [
            item.model_dump(mode="json")
            for item in store.list_evidence_bundles(live_session_id)
        ],
        "escalations": [item.model_dump(mode="json") for item in escalations],
        "analyst_dispatch_claims": [
            claim.model_dump(mode="json")
            for item in escalations
            if (claim := store.get_analyst_dispatch_claim(item.escalation_id)) is not None
        ],
        "planner_dispatch_claims": [
            claim.model_dump(mode="json")
            for item in escalations
            if (claim := store.get_planner_dispatch_claim(item.escalation_id)) is not None
        ],
        "analyses": [
            item.model_dump(mode="json")
            for item in store.list_conflict_analyses(live_session_id)
        ],
        "outcomes": [
            item.model_dump(mode="json")
            for item in store.list_multi_agent_outcomes(live_session_id)
        ],
        "proposals": [
            item.model_dump(mode="json")
            for item in store.list_proposals(live_session_id)
        ],
        "operator_decisions": [
            item.model_dump(mode="json")
            for item in store.list_operator_decisions(live_session_id)
        ],
        "execution_commands": [
            item.model_dump(mode="json")
            for item in store.list_execution_commands(live_session_id)
        ],
    }


def _reconstruct_store_and_replay(
    *,
    source_store: InMemoryDecisionSupportStore,
    assembled: Any,
) -> tuple[InMemoryDecisionSupportStore, Any, tuple[str, ...]]:
    """从 append-only 事实重建新 Store，再由新 Coordinator 验证无二次 Agent 发送的恢复语义。

    InMemory Store 没有磁盘连接层，因此此函数严格使用其公开 append API 按原始事实顺序
    重放到一个全新实例，模拟重启后的装配过程。它不复制私有字典，也不把旧进程对象当作
    恢复缓存；任何父链、CAS、lease 或多 Agent Proposal 验证错误都会在重建时 fail-closed。
    """

    live_session_id = DEMO_LIVE_SESSION_ID
    original_workspace = source_store.get_workspace(live_session_id)
    restart_store = InMemoryDecisionSupportStore(clock=lambda: DEMO_NOW)
    initial_workspace = LiveSessionWorkspace(
        **{
            **original_workspace.model_dump(mode="python"),
            "view": WorkspaceView.PREPARE,
            "version": 1,
        }
    )
    restart_workspace = restart_store.create_workspace(initial_workspace)
    lease = restart_store.acquire_operator_lock(
        live_session_id,
        "operator-phase16-demo",
        60,
        now=DEMO_NOW,
    )
    restart_workspace = restart_store.advance_view(
        live_session_id,
        target_view=WorkspaceView.LIVE,
        expected_version=restart_workspace.version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
        now=DEMO_NOW,
    )
    incident = source_store.list_incidents(live_session_id)[0]
    restart_workspace = restart_store.append_incident(
        incident,
        expected_workspace_version=restart_workspace.version,
    )
    restart_workspace = restart_store.append_evidence_bundle(
        assembled,
        expected_workspace_version=restart_workspace.version,
    )
    escalation = source_store.list_escalations(live_session_id)[0]
    restart_workspace = restart_store.append_escalation(
        escalation,
        expected_workspace_version=restart_workspace.version,
        now=DEMO_NOW,
    )
    source_analyst_claim = source_store.get_analyst_dispatch_claim(escalation.escalation_id)
    if source_analyst_claim is None:
        raise AssertionError("source Demo is missing the persisted Analyst dispatch claim")
    analyst_claim, analyst_created, _ = restart_store.claim_analyst_dispatch(
        escalation_id=escalation.escalation_id,
        task_digest=source_analyst_claim.task_digest,
        now=DEMO_NOW,
    )
    if not analyst_created or analyst_claim != source_analyst_claim:
        raise AssertionError("restart did not reconstruct the exact Analyst dispatch claim")
    analysis = source_store.list_conflict_analyses(live_session_id)[0]
    restart_workspace = restart_store.append_conflict_analysis(
        analysis,
        expected_workspace_version=restart_workspace.version,
    )
    source_planner_claim = source_store.get_planner_dispatch_claim(escalation.escalation_id)
    if source_planner_claim is None:
        raise AssertionError("source Demo is missing the persisted Planner dispatch claim")
    planner_claim, planner_created, _ = restart_store.claim_planner_dispatch(
        escalation_id=escalation.escalation_id,
        analysis_id=analysis.analysis_id,
        analysis_digest=analysis.analysis_digest,
        task_digest=source_planner_claim.task_digest,
        now=DEMO_NOW,
    )
    if not planner_created or planner_claim != source_planner_claim:
        raise AssertionError("restart did not reconstruct the exact Planner dispatch claim")
    proposal = source_store.list_proposals(live_session_id)[0]
    restart_workspace = restart_store.append_multi_agent_proposal(
        proposal,
        expected_workspace_version=restart_workspace.version,
    )
    outcome = source_store.list_multi_agent_outcomes(live_session_id)[0]
    restart_workspace = restart_store.append_multi_agent_outcome(
        outcome,
        expected_workspace_version=restart_workspace.version,
    )
    decision = source_store.list_operator_decisions(live_session_id)[0]
    restart_workspace = restart_store.append_operator_decision(
        decision,
        expected_workspace_version=restart_workspace.version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
        now=DEMO_NOW,
    )
    command = source_store.list_execution_commands(live_session_id)[0]
    restart_store.append_execution_command(
        command,
        expected_workspace_version=restart_workspace.version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
        now=DEMO_NOW,
    )
    replay_runner = _DeterministicDemoRunner()
    replay = asyncio.run(
        HighConflictEscalationCoordinator(
            store=restart_store,
            analyst_runner=replay_runner,
            planner_runner=replay_runner,
            clock=lambda: DEMO_NOW,
            monotonic_clock=lambda: 0.0,
        ).run_automatic(
            assembled.bundle,
            expected_workspace_version=restart_store.get_workspace(live_session_id).version,
        )
    )
    return (
        restart_store,
        replay,
        tuple(task.task_kind.value for task in replay_runner.calls),
    )


def _ready_lineage_complete(result: Any) -> bool:
    """检查 Demo 输出精确闭合 escalation、analysis、proposal、outcome 和 Bundle lineage。"""

    if (
        result.escalation is None
        or result.analysis is None
        or result.proposal is None
        or result.outcome is None
        or result.outcome.status is not MultiAgentOutcomeStatus.READY
        or result.proposal.multi_agent_lineage is None
    ):
        return False
    escalation = result.escalation
    analysis = result.analysis
    proposal = result.proposal
    outcome = result.outcome
    lineage = result.proposal.multi_agent_lineage
    return (
        # Escalation、Analysis、Proposal、Outcome 和 lineage 必须闭合到同一个精确 Bundle，
        # 不能只比较名称相同的 ID；摘要失配意味着 Store/恢复链可能被跨事实拼接。
        escalation.evidence_bundle_id
        == analysis.evidence_bundle_id
        == proposal.evidence_bundle_id
        == outcome.evidence_bundle_id
        == lineage.evidence_bundle_id
        and escalation.evidence_bundle_digest
        == analysis.evidence_bundle_digest
        == proposal.evidence_bundle_digest
        == outcome.evidence_bundle_digest
        == lineage.evidence_bundle_digest
        # Analyst 返回的完整 evidence 集既是 Planner 输入，也是 Proposal lineage 的父证据；
        # 任一遗漏、顺序变化或替换都必须阻断 READY 的完整链结论。
        and analysis.evidence_refs == proposal.evidence_refs == lineage.evidence_refs
        # Analysis 是 Escalation 的直接子事实。即便两个事实恰好复用了同一 Bundle、摘要和
        # EvidenceRef，也不能把来自另一轮升级的 Analysis 拼接进本次 READY Proposal。
        and analysis.escalation_id == escalation.escalation_id
        and lineage.escalation_id == escalation.escalation_id
        and lineage.escalation_digest == escalation.escalation_digest
        and lineage.analysis_id == analysis.analysis_id
        and lineage.analysis_digest == analysis.analysis_digest
        and outcome.escalation_id == escalation.escalation_id
        and outcome.escalation_digest == escalation.escalation_digest
        and outcome.analysis_id == analysis.analysis_id
        and outcome.analysis_digest == analysis.analysis_digest
        and outcome.proposal_id == proposal.proposal_id
        and outcome.proposal_digest == canonical_json_sha256(proposal.model_dump(mode="json"))
        # Outcome 也是 append-only 终态，其自身摘要必须与全部字段闭合；仅信任其父链
        # 摘要会遗漏“同一父链但被篡改的终态展示/失败字段”这一类审计伪造。
        and outcome.outcome_digest
        == canonical_json_sha256(
            outcome.model_dump(mode="json", exclude={"outcome_digest"})
        )
    )


def _protection_evidence_bound(
    *,
    workspace: LiveSessionWorkspace,
    bundle: Any,
    protection: _AuthoritativeProtectionTrace,
) -> bool:
    """验证多 Agent Bundle 是否逐字段继承权威保护链，而非仅复述 APPLIED 状态文本。"""

    try:
        snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
        components = {component.role: component for component in snapshot.components}
        event_component = components[EvidenceRole.VERIFIED_EVENT].payload
        root_component = components[EvidenceRole.ROOT_PLAN_SNAPSHOT].payload
        emergency_component = components[EvidenceRole.EMERGENCY_PLAN_SNAPSHOT].payload
        return bool(
            workspace.root_plan_run_id == protection.root_plan_run_id
            and snapshot.scope.room_id == protection.event.room_id
            and snapshot.scope.trace_id == DEMO_TRACE_ID
            and snapshot.scope.root_plan_run_id == protection.root_plan_run_id
            and event_component.event == protection.event
            and event_component.provenance == protection.provenance
            and event_component.inbox_state == protection.inbox_state
            and event_component.application_state
            == protection.event_application_state
            and event_component.emergency_plan_run_id
            == protection.emergency_plan_run_id
            and event_component.applied_plan_version
            == protection.root_plan_version
            and root_component.plan_run_id == protection.root_plan_run_id
            and root_component.plan_version == protection.root_plan_version
            and root_component.plan_state == protection.root_plan_state
            and emergency_component.plan_run_id == protection.emergency_plan_run_id
            and emergency_component.root_plan_run_id == protection.root_plan_run_id
            and emergency_component.plan_version == protection.emergency_plan_version
            and emergency_component.plan_state == protection.emergency_plan_state
        )
    except (KeyError, ValueError, TypeError):
        return False


def _compile_operator_decisions(
    *,
    store: InMemoryDecisionSupportStore,
    lease: OperatorLease,
    outcome: MultiAgentOutcome,
    protection: _AuthoritativeProtectionTrace,
) -> tuple[tuple[str, ...], str, str, bool, bool, int]:
    """验证三种人工决定形状，只持久化运营实际选择的 MODIFY 分支且不执行命令。"""

    proposal = store.list_proposals(DEMO_LIVE_SESSION_ID)[0]
    context = DecisionExecutionContext(
        plan_run_id=protection.root_plan_run_id,
        expected_plan_version=protection.recovery_plan_version,
        node_id=protection.recovery_node_id,
        expected_node_status=PlanNodeState.WAITING_APPROVAL,
    )
    compiler = DecisionSupportCommandCompiler()
    drafts = (
        OperatorDecisionDraft(
            decision_id="decision-phase16-demo-approve",
            proposal_id=proposal.proposal_id,
            expected_proposal_version=proposal.proposal_version,
            operator_id=lease.operator_id,
            decision_kind=DecisionKind.APPROVE,
            reason_code="OPERATOR_APPROVED",
            idempotency_key="decision-phase16-demo-approve",
            option_id="switch-backup",
        ),
        OperatorDecisionDraft(
            decision_id="decision-phase16-demo-modify",
            proposal_id=proposal.proposal_id,
            expected_proposal_version=proposal.proposal_version,
            operator_id=lease.operator_id,
            decision_kind=DecisionKind.MODIFY,
            reason_code="OPERATOR_MODIFIED",
            idempotency_key="decision-phase16-demo-modify",
            option_id="switch-backup",
            modification=OperatorModification(
                backup_product_id="p003",
                host_prompt="请运营确认后切换备品 p003，并提示观众库存已更新。",
                priority=70,
                timing=DecisionTiming.AFTER_OPERATOR_CONFIRMATION,
            ),
        ),
        OperatorDecisionDraft(
            decision_id="decision-phase16-demo-reject",
            proposal_id=proposal.proposal_id,
            expected_proposal_version=proposal.proposal_version,
            operator_id=lease.operator_id,
            decision_kind=DecisionKind.REJECT,
            reason_code="OPERATOR_REJECTED",
            idempotency_key="decision-phase16-demo-reject",
        ),
    )
    compiled = tuple(
        compiler.compile(
            proposal=proposal,
            draft=draft,
            lease=lease,
            execution_context=context,
            now=DEMO_NOW,
            multi_agent_ready_outcome=outcome,
        )
        for draft in drafts
    )
    if compiled[0].execution_command is None or compiled[1].execution_command is None:
        raise AssertionError("APPROVE and MODIFY must compile controlled commands")
    if compiled[2].execution_command is not None:
        raise AssertionError("REJECT must not compile a recovery command")

    # 模拟运营明确选择 MODIFY：Decision 与编译后的 ExecutionCommand 可以成为审计事实，
    # 但本脚本绝不把 plan_command 提交到 PlanEngine，因此不存在自动经营恢复。
    selected = compiled[1]
    after_decision = store.append_operator_decision(
        selected.operator_decision,
        expected_workspace_version=store.get_workspace(DEMO_LIVE_SESSION_ID).version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
        now=DEMO_NOW,
    )
    assert selected.execution_command is not None
    store.append_execution_command(
        selected.execution_command,
        expected_workspace_version=after_decision.version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
        now=DEMO_NOW,
    )
    if (
        selected.plan_command is None
        or selected.plan_command.plan_run_id != protection.root_plan_run_id
        or selected.plan_command.expected_plan_version
        != protection.recovery_plan_version
        or selected.plan_command.node_id != protection.recovery_node_id
        or selected.plan_command.expected_node_status
        is not PlanNodeState.WAITING_APPROVAL
    ):
        raise AssertionError("compiled command is not bound to authoritative PlanStore context")
    # 编译边界与提交边界必须通过正式 PlanStore 命令账本区分。查询不到该 command 才能
    # 证明本地 Demo 没有把人工恢复意图提交给 PlanEngine，而不是依赖展示层的常量。
    try:
        protection.plan_store.get_command(selected.plan_command.command_id)
    except PlanStoreInvariantError:
        submitted = False
        submission_count = 0
    else:
        raise AssertionError("Phase 16 Demo must never submit the compiled recovery command")
    return (
        tuple(draft.decision_kind.value for draft in drafts),
        selected.operator_decision.decision_kind.value,
        selected.execution_command.command_id,
        True,
        submitted,
        submission_count,
    )


def _blocked_real_smoke(dataset: Any) -> Any:
    """使用完整冻结身份运行无网络预检；端点和 usage 证据缺失时永远不会发送模型请求。"""

    manifest = dataset.manifest
    analyst = build_evidence_analyst_profile()
    planner = build_decision_planner_profile()
    # 该摘要只是本地预检的占位身份，不声称来自真实官方价格页；端点和 usage 合同显式
    # 缺失会使后续 Runner 在任何 ModelPort 调用前阻断，不能把占位摘要伪造为官方证据。
    local_price_digest = sha256(b"phase16-demo-local-price-placeholder").hexdigest()
    config = Phase16SmokeConfig(
        manifest_id=manifest.dataset_id,
        manifest_digest=manifest.manifest_digest,
        dataset_digest=manifest.dataset_digest,
        source_code_digest=manifest.source_code_digest,
        evidence_analyst_profile_digest=analyst.profile_digest,
        decision_planner_profile_digest=planner.profile_digest,
        official_price_digest=local_price_digest,
        smoke_runtime_digest=phase16_smoke_runtime_digest(),
        model_id=FORMAL_MODEL_ID,
        endpoint_host=FORMAL_ENDPOINT_HOST,
    )
    price = Phase16OfficialPriceEvidence(
        model_id=FORMAL_MODEL_ID,
        endpoint_host=FORMAL_ENDPOINT_HOST,
        input_cny_per_million=INPUT_PRICE_CNY_PER_MILLION,
        output_cny_per_million=OUTPUT_PRICE_CNY_PER_MILLION,
        official_price_digest=local_price_digest,
    )
    preflight = preflight_phase16_multi_agent_smoke(
        config,
        dataset=dataset,
        official_price=price,
        endpoint_available=False,
        usage_contract_available=False,
    )
    runner = Phase16SmokeRunner(
        config=config,
        preflight=preflight,
        budget_store=Phase16SmokeBudgetStore(),
        model_port=_NoSendModelPort(),
    )
    return asyncio.run(runner.run(()))


def run_demo(evaluation_root: Path) -> Phase16DemoResult:
    """回放固定高冲突售罄事故、人工决定边界和真实 smoke 阻断事实。"""

    dataset = _load_or_generate_dataset(Path(evaluation_root))
    # Phase 16 的多 Agent 只能建立在已经由 Phase 12B 权威 Coordinator 执行的保护事实之上。
    # 此调用包含可信 Inbox、freeze、紧急 child、单次售罄写、只读对账与 Replan，不使用
    # 任何手写 APPLIED 替身；返回投影不保留 InMemoryPlanStore 的随机内部 UUID。
    protection_trace = _run_authoritative_protection()
    if (
        protection_trace.status is not PreemptionStatus.APPLIED
        or protection_trace.event_id != DEMO_EVENT_ID
        or protection_trace.event_application_state is not EventApplicationState.APPLIED
    ):
        raise AssertionError("Phase 16 escalation requires authoritative sold-out protection")
    store = InMemoryDecisionSupportStore(clock=lambda: DEMO_NOW)
    workspace = store.create_workspace(
        LiveSessionWorkspace(
            live_session_id=DEMO_LIVE_SESSION_ID,
            run_key="run-phase16-demo",
            room_id=DEMO_ROOM_ID,
            trace_id=DEMO_TRACE_ID,
            anchor_id="anchor-phase16-demo",
            root_plan_run_id=protection_trace.root_plan_run_id,
            event_inbox_scope_id="inbox-phase16-demo",
            decision_trace_scope_id="trace-scope-phase16-demo",
            replay_scope_id="replay-scope-phase16-demo",
            evaluation_scope_id="evaluation-scope-phase16-demo",
        )
    )
    # 显式把固定演练时钟传给 lease 与视图切换，避免这两个公开 API 的兼容默认参数
    # 回退到机器墙钟，进而让后续按同一审计时钟编译的人工决定被误判为过期。
    lease = store.acquire_operator_lock(
        DEMO_LIVE_SESSION_ID,
        "operator-phase16-demo",
        60,
        now=DEMO_NOW,
    )
    workspace = store.advance_view(
        DEMO_LIVE_SESSION_ID,
        target_view=WorkspaceView.LIVE,
        expected_version=workspace.version,
        operator_id=lease.operator_id,
        fencing_token=lease.fencing_token,
        now=DEMO_NOW,
    )

    incident = Incident(
        incident_id=DEMO_INCIDENT_ID,
        live_session_id=DEMO_LIVE_SESSION_ID,
        idempotency_key=DEMO_INCIDENT_ID,
        incident_type="SOLD_OUT_COMPOSITE",
        source_ref_ids=(DEMO_EVENT_ID,),
        snapshot={
            "product_id": "p001",
            "expected_version": protection_trace.event.observed_version,
            "root_plan_run_id": protection_trace.root_plan_run_id,
        },
        created_at=DEMO_NOW,
    )
    workspace = store.append_incident(incident, expected_workspace_version=workspace.version)
    assembled = _assemble_demo_bundle(
        workspace=workspace,
        incident=incident,
        protection=protection_trace,
    )
    # Store 只接受 Assembler 签发的 receipt；传入其内层 Bundle 会失去“经六角色 Resolver
    # 校验”的能力证明，因此必须保留完整 assembled 对象直到持久化边界。
    workspace = store.append_evidence_bundle(
        assembled, expected_workspace_version=workspace.version
    )

    runner = _DeterministicDemoRunner()
    coordinator = HighConflictEscalationCoordinator(
        store=store,
        analyst_runner=runner,
        planner_runner=runner,
        clock=lambda: DEMO_NOW,
        # 固定单调时钟使五秒总预算不依赖本机负载；只用于离线演练，不改变生产超时实现。
        monotonic_clock=lambda: 0.0,
    )
    coordinated = asyncio.run(
        coordinator.run_automatic(
            assembled.bundle, expected_workspace_version=workspace.version
        )
    )
    if not _ready_lineage_complete(coordinated):
        raise AssertionError("Demo controlled multi-agent route must produce a complete READY lineage")
    assert coordinated.outcome is not None
    (
        decision_kinds,
        selected_kind,
        command_id,
        command_context_bound,
        command_submitted,
        command_submission_count,
    ) = _compile_operator_decisions(
        store=store,
        lease=lease,
        outcome=coordinated.outcome,
        protection=protection_trace,
    )
    first_projection = _audit_projection(store, DEMO_LIVE_SESSION_ID)

    # 重启验收必须使用全新的 Store 实例。函数会按公开 append-only API 重建 Workspace、
    # Evidence、升级链、人工决定和命令，再由新 Coordinator 只恢复 Outcome 而零发送。
    restart_store, replay, replay_agent_call_sequence = _reconstruct_store_and_replay(
        source_store=store,
        assembled=assembled,
    )
    replay_projection = _audit_projection(restart_store, DEMO_LIVE_SESSION_ID)
    replay_stable = (
        first_projection == replay_projection
        and replay_agent_call_sequence == ()
        and coordinated.escalation == replay.escalation
        and coordinated.analysis == replay.analysis
        and coordinated.proposal == replay.proposal
        and coordinated.outcome == replay.outcome
    )
    # 全量 Task 9 配对评估是阶段验收证据，不属于这一场直播事故的在线决策。把它放在
    # 保护、双 Agent 和人工边界之后，保证 Demo 实际执行顺序始终由确定性保护开场。
    evaluation = run_phase16_scripted_evaluation(dataset)
    if evaluation.total_cases != 48 or evaluation.real_model_calls != 0:
        raise AssertionError("Phase 16 Task 9 scripted evidence is not in the frozen state")
    smoke = _blocked_real_smoke(dataset)
    real_smoke_reasons = tuple(
        sorted({"REAL_MODEL_SMOKE_NOT_RUN", *smoke.reason_codes})
    )
    status = (
        Phase16AcceptanceStatus.FAIL
        if not replay_stable or smoke.model_request_count != 0
        else Phase16AcceptanceStatus.INCONCLUSIVE
    )
    snapshot = EvidenceBundleSnapshot.model_validate(assembled.bundle.snapshot)
    protection_evidence_bound = _protection_evidence_bound(
        workspace=workspace,
        bundle=assembled.bundle,
        protection=protection_trace,
    )
    if not protection_evidence_bound:
        raise AssertionError("multi-agent evidence is not bound to authoritative protection")
    route_policy = DecisionSupportRoutePolicy.from_settings(Settings())
    if route_policy.route is not DecisionSupportRoute.DETERMINISTIC_ONLY:
        raise AssertionError("Phase 16 Acceptance requires the frozen default route to stay deterministic")
    return Phase16DemoResult(
        status=status,
        live_session_id=DEMO_LIVE_SESSION_ID,
        incident_id=DEMO_INCIDENT_ID,
        evidence_bundle_id=assembled.bundle.evidence_bundle_id,
        evidence_bundle_digest=snapshot.bundle_digest,
        automatic_protection_status=protection_trace.status.value,
        automatic_protection_authoritative=True,
        automatic_protection_event_application_state=(
            protection_trace.event_application_state.value
        ),
        automatic_protection_external_write_count=(
            protection_trace.external_write_count
        ),
        automatic_protection_root_plan_run_id=protection_trace.root_plan_run_id,
        automatic_protection_evidence_bound=protection_evidence_bound,
        execution_order=(
            "AUTOMATIC_PROTECTION",
            *(task.task_kind.value for task in runner.calls),
            "OPERATOR_DECISION_COMPILED",
        ),
        dual_agent_call_sequence=tuple(task.task_kind.value for task in runner.calls),
        dual_agent_call_counts={
            "analyst": sum(
                task.task_kind.value == "CONFLICT_ANALYSIS" for task in runner.calls
            ),
            "planner": sum(
                task.task_kind.value == "LIVE_DECISION_PLANNING"
                for task in runner.calls
            ),
        },
        escalation_id=coordinated.escalation.escalation_id,
        escalation_digest=coordinated.escalation.escalation_digest,
        analysis_id=coordinated.analysis.analysis_id,
        analysis_digest=coordinated.analysis.analysis_digest,
        ready_proposal_id=coordinated.proposal.proposal_id,
        # Proposal 的摘要由 Outcome 已持久化引用；投影保留该值，使 Acceptance 可在不读取
        # 内存对象的情况下逐字段复核完整谱系。
        ready_proposal_digest=coordinated.outcome.proposal_digest,
        ready_proposal_origin=coordinated.proposal.proposal_origin.value,
        ready_outcome_id=coordinated.outcome.outcome_id,
        ready_outcome_digest=coordinated.outcome.outcome_digest,
        ready_outcome_status=coordinated.outcome.status.value,
        ready_lineage_complete=True,
        operator_decision_kinds=decision_kinds,
        selected_operator_decision_kind=selected_kind,
        compiled_command_id=command_id,
        compiled_command_context_bound=command_context_bound,
        execution_command_persisted=len(store.list_execution_commands(DEMO_LIVE_SESSION_ID))
        == 1,
        execution_command_submitted=command_submitted,
        execution_submission_count=command_submission_count,
        replay_stable=replay_stable,
        restart_store_reconstructed=(
            restart_store is not store and first_projection == replay_projection
        ),
        replay_agent_call_sequence=replay_agent_call_sequence,
        audit_projection_digest=canonical_json_sha256(first_projection),
        replay_audit_projection_digest=canonical_json_sha256(replay_projection),
        production_default_route=route_policy.route.value,
        task9_dataset_id=dataset.manifest.dataset_id,
        task9_manifest_digest=dataset.manifest.manifest_digest,
        task9_source_code_digest=dataset.manifest.source_code_digest,
        task9_profile_digests=dict(sorted(dataset.manifest.profile_digests.items())),
        task9_total_cases=evaluation.total_cases,
        task9_route_correct_cases=evaluation.route_correct_cases,
        task9_pairwise_identity_correct_cases=evaluation.paired_identity_correct_cases,
        task9_analyst_calls=evaluation.analyst_calls,
        task9_planner_calls=evaluation.planner_calls,
        task9_ready_outcomes=evaluation.ready_outcomes,
        task9_degraded_outcomes=evaluation.degraded_outcomes,
        task9_no_send_cases=evaluation.no_send_cases,
        task9_scripted_reserved_cost_cny=f"{evaluation.scripted_reserved_cost_cny:.2f}",
        real_smoke_scope_id=PHASE16_MULTI_AGENT_SMOKE,
        real_smoke_status=smoke.status.value,
        real_smoke_reason_codes=real_smoke_reasons,
        real_model_call_count=smoke.model_request_count,
        real_model_cost_cny=f"{smoke.settled_cost_cny:.6f}",
    )


def render_acceptance_report(result: Phase16DemoResult) -> str:
    """以固定顺序渲染阶段事实，避免模型文本、时间戳或本地环境进入验收报告。"""

    lines = [
        "# Phase 16 Controlled Multi-Agent Escalation Acceptance",
        "",
        "本报告只记录本地确定性保护、受控双 Agent 演练、人工命令边界和真实 smoke 外部证据状态。它不把 ScriptedModel 或本地预检冒充为真实模型调用。",
        "",
        f"- Acceptance status: `{result.status.value}`",
        f"- Phase state: `{result.phase_state}`",
        f"- Production default route: `{result.production_default_route}`",
        f"- Live session: `{result.live_session_id}`",
        f"- Incident: `{result.incident_id}`",
        "",
        "## Protection And Controlled Route",
        "",
        f"- Automatic protection: `{result.automatic_protection_status}`",
        f"- Authoritative Phase 12B Coordinator evidence: `{str(result.automatic_protection_authoritative).lower()}`",
        f"- Protected EventApplication state: `{result.automatic_protection_event_application_state}`",
        f"- Protected sold-out write count: `{result.automatic_protection_external_write_count}`",
        f"- Protected root PlanRun: `{result.automatic_protection_root_plan_run_id}`",
        f"- Protection facts bound into EvidenceBundle: `{str(result.automatic_protection_evidence_bound).lower()}`",
        f"- Execution order: `{', '.join(result.execution_order)}`",
        f"- Evidence bundle: `{result.evidence_bundle_id}` / `{result.evidence_bundle_digest}`",
        f"- Dual-Agent calls: `{', '.join(result.dual_agent_call_sequence)}`",
        f"- Analyst / Planner calls: `{result.dual_agent_call_counts['analyst']} / {result.dual_agent_call_counts['planner']}`",
        f"- Escalation: `{result.escalation_id}` / `{result.escalation_digest}`",
        f"- Analysis: `{result.analysis_id}` / `{result.analysis_digest}`",
        f"- Proposal: `{result.ready_proposal_id}` / `{result.ready_proposal_digest}`",
        f"- Outcome: `{result.ready_outcome_id}` / `{result.ready_outcome_digest}`",
        f"- READY proposal origin: `{result.ready_proposal_origin}`",
        f"- READY outcome: `{result.ready_outcome_status}`",
        f"- Exact lineage complete: `{str(result.ready_lineage_complete).lower()}`",
        "",
        "## Human Recovery Boundary",
        "",
        f"- Valid operator decision kinds: `{', '.join(result.operator_decision_kinds)}`",
        f"- Selected operator decision: `{result.selected_operator_decision_kind}`",
        f"- Compiled command: `{result.compiled_command_id}`",
        f"- Compiled command bound to PlanStore context: `{str(result.compiled_command_context_bound).lower()}`",
        f"- Execution command persisted: `{str(result.execution_command_persisted).lower()}`",
        f"- Execution command submitted: `{str(result.execution_command_submitted).lower()}`",
        f"- Execution submissions: `{result.execution_submission_count}`",
        "",
        "## Restart Audit",
        "",
        f"- Replay stable: `{str(result.replay_stable).lower()}`",
        f"- Store reconstructed from append-only facts: `{str(result.restart_store_reconstructed).lower()}`",
        f"- Replay Agent calls: `{', '.join(result.replay_agent_call_sequence) or 'none'}`",
        f"- Initial audit digest: `{result.audit_projection_digest}`",
        f"- Replay audit digest: `{result.replay_audit_projection_digest}`",
        "",
        "## Frozen Scripted Evaluation",
        "",
        f"- Dataset / Manifest: `{result.task9_dataset_id}` / `{result.task9_manifest_digest}`",
        f"- Source closure digest: `{result.task9_source_code_digest}`",
        f"- Profile digests: `{json.dumps(result.task9_profile_digests, ensure_ascii=False, sort_keys=True)}`",
        f"- Cases / route-correct / paired identity: `{result.task9_total_cases} / {result.task9_route_correct_cases} / {result.task9_pairwise_identity_correct_cases}`",
        f"- Analyst / Planner / READY / DEGRADED / no-send: `{result.task9_analyst_calls} / {result.task9_planner_calls} / {result.task9_ready_outcomes} / {result.task9_degraded_outcomes} / {result.task9_no_send_cases}`",
        f"- Scripted reserved cost: `{result.task9_scripted_reserved_cost_cny} CNY`",
        "",
        "## Real Smoke Evidence",
        "",
        f"- Scope: `{result.real_smoke_scope_id}` (10 cases / 1.00 CNY hard cap)",
        f"- Smoke status: `{result.real_smoke_status}`",
        f"- Real model calls / cost: `{result.real_model_call_count} / {result.real_model_cost_cny} CNY`",
        "- Blockers:",
        *(f"  - `{code}`" for code in result.real_smoke_reason_codes),
        "",
        "真实 endpoint、usage 合同和真实模型回执未提供，因此 Phase 16 结论保持 INCONCLUSIVE；默认路由继续为 DETERMINISTIC_ONLY。Phase 16 完成后不自动实施 Phase 17，当前状态固定为 AWAITING_PHASE_17_GATE。",
        "",
    ]
    return "\n".join(lines)


def write_acceptance_report(root: Path, result: Phase16DemoResult) -> Path:
    """把同一稳定投影写入唯一的 Phase 16 Acceptance 文档，统一使用 UTF-8 LF。"""

    output_root = Path(root)
    output_root.mkdir(parents=True, exist_ok=True)
    output = output_root / "phase-16-controlled-multi-agent-acceptance.md"
    output.write_text(render_acceptance_report(result), encoding="utf-8", newline="\n")
    return output


def main() -> int:
    """执行本地 Demo 并写入仓库 Acceptance；INCONCLUSIVE 不是脚本执行失败。"""

    result = run_demo(PROJECT_ROOT / "evaluation")
    write_acceptance_report(PROJECT_ROOT / "docs" / "superpowers" / "reports", result)
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 1 if result.status is Phase16AcceptanceStatus.FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Phase 12B 售罄抢占协调与播中证据边界。

本模块是 Event Inbox、ImpactAnalyzer、PlanStore、紧急 child Worker 和 ReplanCoordinator
之间的唯一确定性协调入口。它不接收 Legacy fallback，也不允许 Harness 直接执行售罄
写 Skill；Harness 只能消费这里产生的 ``PreemptionEvidenceRef``。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Callable, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.config.settings import Settings
from src.plan_engine.emergency import SoldOutEmergencyProposalProvider
from src.plan_engine.event_state_machine import (
    EventApplicationState,
    EventInboxState,
)
from src.plan_engine.event_store import EventClaim, EventStore
from src.plan_engine.events import canonical_json_sha256
from src.plan_engine.impact import ImpactAnalysis, ImpactAnalyzer
from src.plan_engine.models import (
    EmergencySoldOutPlanningInput,
    PlanNodeState,
    PlanRunKind,
    PlanRunState,
)
from src.plan_engine.replan import ReplanCoordinator
from src.plan_engine.side_effect_reconciliation import (
    SoldOutReconciliationRequest,
    SoldOutReconciliationResult,
    SoldOutReconciliationStatus,
)
from src.plan_engine.store import MaterializedPlan, PlanStore, PlanStoreInvariantError
from src.plan_engine.worker import PlanWorker
from src.skill_runtime.catalog import get_default_skill_catalog
from src.skill_runtime.models import FailureCategory, FailureFact, SideEffectState
from src.skill_runtime.models import _build_verified_event_authorization
from src.plan_engine.capabilities import PlanCapabilityProfile


class SoldOutExecutionRoute(StrEnum):
    """启动级售罄执行路由；重启前不得在运行时修改。"""

    LEGACY = "LEGACY"
    PLAN_ENGINE = "PLAN_ENGINE"


class SoldOutRoutePolicy(BaseModel):
    """已冻结的售罄路由策略。"""

    model_config = ConfigDict(frozen=True)

    route: SoldOutExecutionRoute = SoldOutExecutionRoute.LEGACY

    @classmethod
    def from_settings(cls, settings: Settings) -> "SoldOutRoutePolicy":
        """在进程装配时读取一次 Settings，拒绝未知路由值。"""

        return cls(route=SoldOutExecutionRoute(settings.sold_out_execution_route))


class PreemptionStatus(StrEnum):
    """一次协调推进的持久化结果。"""

    APPLIED = "APPLIED"
    WAITING_RECONCILIATION = "WAITING_RECONCILIATION"
    RETRY_PENDING = "RETRY_PENDING"
    FAILED = "FAILED"
    IDLE = "IDLE"


class PreemptionEvidenceRef(BaseModel):
    """供 Harness 和 Replay 引用的售罄最终事实，不携带可执行权限。"""

    model_config = ConfigDict(frozen=True)

    event_id: str = Field(..., min_length=1)
    root_plan_run_id: str = Field(..., min_length=1)
    application_state: EventApplicationState
    emergency_plan_run_id: str = Field(..., min_length=1)
    applied_plan_version: int = Field(..., ge=1)
    final_suggestion_fact: str = Field(..., min_length=1)
    evidence_digest: str = Field(..., pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _digest_matches_facts(self) -> "PreemptionEvidenceRef":
        """拒绝调用方修改建议事实后沿用旧 EvidenceRef 摘要。"""

        payload = {
            "event_id": self.event_id,
            "root_plan_run_id": self.root_plan_run_id,
            "application_state": self.application_state.value,
            "emergency_plan_run_id": self.emergency_plan_run_id,
            "applied_plan_version": self.applied_plan_version,
            "final_suggestion_fact": self.final_suggestion_fact,
        }
        if self.evidence_digest != canonical_json_sha256(payload):
            raise ValueError("evidence_digest 与售罄事实不一致")
        if self.application_state is not EventApplicationState.APPLIED:
            raise ValueError("EvidenceRef 只允许引用 APPLIED Application")
        return self

    @classmethod
    def create(
        cls,
        *,
        event_id: str,
        root_plan_run_id: str,
        application_state: EventApplicationState | str,
        emergency_plan_run_id: str,
        applied_plan_version: int,
        final_suggestion_fact: str,
    ) -> "PreemptionEvidenceRef":
        """按全部公开事实计算不可伪造的证据摘要。"""

        normalized_state = EventApplicationState(application_state)
        payload = {
            "event_id": event_id,
            "root_plan_run_id": root_plan_run_id,
            "application_state": normalized_state.value,
            "emergency_plan_run_id": emergency_plan_run_id,
            "applied_plan_version": applied_plan_version,
            "final_suggestion_fact": final_suggestion_fact,
        }
        return cls(**payload, evidence_digest=canonical_json_sha256(payload))


class PreemptionResult(BaseModel):
    """Coordinator 一次推进返回的只读摘要。"""

    model_config = ConfigDict(frozen=True)

    status: PreemptionStatus
    event_id: str | None = None
    root_plan_run_id: str
    evidence_ref: PreemptionEvidenceRef | None = None
    failure: FailureFact | None = None


class SideEffectReconciliationService(Protocol):
    """Coordinator 依赖的 Task 6 严格只读对账最小接口。"""

    async def reconcile(
        self,
        request: SoldOutReconciliationRequest,
    ) -> SoldOutReconciliationResult:
        """只读确认原 Attempt，不得发送第二次售罄写。"""


class PreemptionCoordinator:
    """串联一个可信售罄事件的确定性抢占流程。

    每个阶段都先写权威 Store，再进入下一阶段。进程在任一步骤崩溃后，下一次
    ``run_next`` 会依据 Inbox/Application 当前状态继续，而不是重新创建第二个 child
    或第二个外部写 Operation。
    """

    def __init__(
        self,
        *,
        plan_store: PlanStore,
        event_store: EventStore,
        emergency_worker: PlanWorker,
        replan_coordinator: ReplanCoordinator,
        reconciliation_service: SideEffectReconciliationService | None = None,
        worker_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not worker_id:
            raise ValueError("worker_id 不能为空")
        self._plan_store = plan_store
        self._event_store = event_store
        self._emergency_worker = emergency_worker
        self._replan = replan_coordinator
        self._reconciliation_service = reconciliation_service
        self._worker_id = worker_id
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._impact = ImpactAnalyzer()
        self._provider = SoldOutEmergencyProposalProvider()
        self._profile = PlanCapabilityProfile.default(catalog=get_default_skill_catalog())

    async def reconcile_waiting(
        self,
        *,
        event_id: str,
        root_plan_run_id: str,
        now: datetime,
    ) -> PreemptionResult:
        """只读闭合未知售罄 Attempt，并把 child 恢复到可继续调度状态。

        本方法不 claim 新事件、不创建 Operation，也不直接执行后续节点。确认成功后
        只把原 WAITING_RECONCILIATION NodeRun 引用闭合，并将 Inbox 恢复为 VERIFIED；
        下一次 ``run_next`` 再通过正常 lease/fencing 继续 child 与 Replan。
        """

        if self._reconciliation_service is None:
            raise PlanStoreInvariantError("Coordinator 未装配严格只读对账服务")
        current_time = self._aware_utc(now)
        application = self._event_store.get_application(event_id, root_plan_run_id)
        inbox = self._event_store.get_inbox(event_id)
        if (
            application.state is EventApplicationState.EMERGENCY_RUNNING
            and inbox.state is EventInboxState.WAITING_HUMAN
        ):
            # Application 已恢复但 Inbox 写入前崩溃，只补偿 Inbox，不再次读平台或
            # 改写 NodeRun。下一次 run_next 会重新 claim 并继续 child。
            self._event_store.transition_inbox(
                event_id,
                expected_state=EventInboxState.WAITING_HUMAN,
                target_state=EventInboxState.VERIFIED,
                now=current_time,
            )
            return PreemptionResult(
                status=PreemptionStatus.RETRY_PENDING,
                event_id=event_id,
                root_plan_run_id=root_plan_run_id,
            )
        if application.state is not EventApplicationState.WAITING_RECONCILIATION:
            raise PlanStoreInvariantError("Application 当前不在 WAITING_RECONCILIATION")
        child_id = application.emergency_plan_run_id
        if child_id is None:
            raise PlanStoreInvariantError("等待对账的 Application 缺少 child PlanRun")
        mark_nodes = tuple(
            node
            for node in self._plan_store.list_nodes(child_id)
            if node.logical_key == "mark-sold-out"
        )
        if len(mark_nodes) != 1:
            raise PlanStoreInvariantError("无法定位唯一售罄对账节点")
        node = mark_nodes[0]
        if node.state is PlanNodeState.WAITING_RECONCILIATION:
            runs = self._plan_store.list_node_runs(child_id, node.node_id)
            if not runs or not isinstance(runs[-1].output, Mapping):
                raise PlanStoreInvariantError("等待对账节点缺少原失败事实")
            failure_payload = runs[-1].output.get("failure")
            failure = FailureFact.model_validate(failure_payload)
            authorization = _build_verified_event_authorization(
                event_id=inbox.event.event_id,
                provenance_id=inbox.provenance.provenance_id,
                payload_digest=inbox.event.payload_digest,
                observed_version=inbox.event.observed_version,
            )
            result = await self._reconciliation_service.reconcile(
                SoldOutReconciliationRequest(
                    room_id=inbox.event.room_id,
                    trace_id=self._plan_store.get_plan_run(child_id).trace_id,
                    product_id=inbox.event.product_id,
                    expected_version=inbox.event.observed_version,
                    event_authorization=authorization,
                    original_failure=failure,
                    deadline_at=current_time + timedelta(seconds=30),
                )
            )
            if result.status is SoldOutReconciliationStatus.WAITING_RECONCILIATION:
                return self._waiting_result(event_id, root_plan_run_id, application)
            self._plan_store.reconcile_plan_reference(
                plan_run_id=child_id,
                node_id=node.node_id,
                outcome=PlanNodeState.SUCCEEDED,
                reference={"side_effect_reconciliation": result.model_dump(mode="json")},
            )
        elif node.state is not PlanNodeState.SUCCEEDED:
            raise PlanStoreInvariantError("售罄对账节点不处于可恢复状态")
        application = self._event_store.transition_application(
            event_id,
            root_plan_run_id,
            expected_state=EventApplicationState.WAITING_RECONCILIATION,
            target_state=EventApplicationState.EMERGENCY_RUNNING,
            now=current_time,
        )
        self._event_store.transition_inbox(
            event_id,
            expected_state=EventInboxState.WAITING_HUMAN,
            target_state=EventInboxState.VERIFIED,
            now=current_time,
        )
        return PreemptionResult(
            status=PreemptionStatus.RETRY_PENDING,
            event_id=event_id,
            root_plan_run_id=root_plan_run_id,
        )

    async def run_next(
        self,
        *,
        root_plan_run_id: str,
        now: datetime,
    ) -> PreemptionResult:
        """领取并推进一个 root 下最早可信事件；没有事件时返回 IDLE。"""

        current_time = self._aware_utc(now)
        selected_root = self._plan_store.get_plan_run(root_plan_run_id)
        candidate_roots = self._active_card_batch_roots(selected_root.room_id)
        if candidate_roots != (root_plan_run_id,):
            # 事件只带 room，不带 root 身份。同房间存在零个或多个活动 root 时，
            # Coordinator 没有权限猜测绑定关系，必须在 claim 前停止。
            raise PlanStoreInvariantError("售罄事件无法解析唯一活动 root PlanRun")
        claim = self._event_store.claim_next_for_room(
            self._worker_id,
            room_id=selected_root.room_id,
            now=current_time,
            lease_seconds=60,
        )
        if claim is None:
            return PreemptionResult(status=PreemptionStatus.IDLE, root_plan_run_id=root_plan_run_id)
        if self._active_card_batch_roots(selected_root.room_id) != (
            root_plan_run_id,
        ):
            # PlanStore 与 EventStore 不能伪装成跨库事务。claim 后重新读取 root 集合，
            # 若窗口内出现歧义则使用当前 fencing 安全释放事件，不创建 Application。
            self._event_store.transition_inbox(
                claim.record.event.event_id,
                expected_state=EventInboxState.PROCESSING,
                target_state=EventInboxState.VERIFIED,
                now=current_time,
                worker_id=claim.record.lease_owner or self._worker_id,
                fencing_token=claim.fencing_token,
            )
            return PreemptionResult(
                status=PreemptionStatus.RETRY_PENDING,
                event_id=claim.record.event.event_id,
                root_plan_run_id=root_plan_run_id,
            )
        if selected_root.room_id != claim.record.event.room_id:
            failure = self._failure(claim.record.event.event_id)
            self._event_store.transition_inbox(
                claim.record.event.event_id,
                expected_state=EventInboxState.PROCESSING,
                target_state=EventInboxState.FAILED,
                now=current_time,
                worker_id=claim.record.lease_owner,
                fencing_token=claim.fencing_token,
                failure=failure,
            )
            return PreemptionResult(
                status=PreemptionStatus.FAILED,
                event_id=claim.record.event.event_id,
                root_plan_run_id=root_plan_run_id,
                failure=failure,
            )

        application_result = self._event_store.create_application(
            claim.record.event.event_id,
            root_plan_run_id=root_plan_run_id,
            source_plan_version=self._plan_store.get_plan_run(root_plan_run_id).current_version,
            now=current_time,
        )
        return await self._resume_application(
            claim=claim,
            root_plan_run_id=root_plan_run_id,
            now=current_time,
            application=application_result.application,
        )

    async def _resume_application(
        self,
        *,
        claim: EventClaim,
        root_plan_run_id: str,
        now: datetime,
        application: Any,
    ) -> PreemptionResult:
        """按 Application 状态恢复，所有跨 Store 步骤均保持幂等。"""

        event_id = claim.record.event.event_id
        root = self._plan_store.get_plan_run(root_plan_run_id)
        if application.state is EventApplicationState.APPLIED:
            # ReplanCoordinator 可能已提交 APPLIED，随后进程才在 Inbox 闭合前崩溃；
            # 重放必须只补齐 Inbox，不得再次创建版本或外部 Operation。
            self._event_store.heartbeat(
                event_id,
                worker_id=claim.record.lease_owner or self._worker_id,
                fencing_token=claim.fencing_token,
                now=now,
                lease_seconds=60,
            )
            self._event_store.transition_inbox(
                event_id,
                expected_state=EventInboxState.PROCESSING,
                target_state=EventInboxState.APPLIED,
                now=now,
                worker_id=claim.record.lease_owner or self._worker_id,
                fencing_token=claim.fencing_token,
            )
            return PreemptionResult(
                status=PreemptionStatus.APPLIED,
                event_id=event_id,
                root_plan_run_id=root_plan_run_id,
                evidence_ref=self._evidence(application),
            )
        if application.state is EventApplicationState.WAITING_RECONCILIATION:
            # WAITING_HUMAN Inbox 不会被自动 claim；若受控人工命令先恢复到 VERIFIED，
            # 此分支仍保持 fail-closed，必须由显式对账服务闭合 NodeRun 后再继续。
            return self._waiting_result(event_id, root_plan_run_id, application)
        if application.state is EventApplicationState.FAILED:
            failure = application.failure or self._failure(event_id)
            if not isinstance(failure, FailureFact):
                failure = FailureFact.model_validate(failure)
            if claim.record.state is EventInboxState.PROCESSING:
                self._event_store.transition_inbox(
                    event_id,
                    expected_state=EventInboxState.PROCESSING,
                    target_state=EventInboxState.FAILED,
                    now=now,
                    worker_id=claim.record.lease_owner or self._worker_id,
                    fencing_token=claim.fencing_token,
                    failure=failure,
                )
            return PreemptionResult(
                status=PreemptionStatus.FAILED,
                event_id=event_id,
                root_plan_run_id=root_plan_run_id,
                failure=failure,
            )
        if application.state is EventApplicationState.PENDING:
            analysis = self._impact.analyze(
                inbox=claim.record,
                plan_run=root,
                nodes=self._plan_store.list_nodes(root_plan_run_id, root.current_version),
            )
            application = self._event_store.transition_application(
                event_id,
                root_plan_run_id,
                expected_state=EventApplicationState.PENDING,
                target_state=EventApplicationState.FREEZING,
                now=now,
                impact_analysis=analysis.model_dump(mode="json"),
            )

        if application.state is EventApplicationState.FREEZING:
            analysis = ImpactAnalysis.model_validate(application.impact_analysis)
            self._plan_store.apply_impact_freeze(
                plan_run_id=root_plan_run_id,
                expected_plan_version=root.current_version,
                event_id=event_id,
                analysis=analysis,
                now=now,
            )
            child = self._plan_store.create_or_resume(self._emergency_plan(claim, root_plan_run_id))
            application = self._event_store.transition_application(
                event_id,
                root_plan_run_id,
                expected_state=EventApplicationState.FREEZING,
                target_state=EventApplicationState.EMERGENCY_RUNNING,
                now=now,
                emergency_plan_run_id=child.plan_run_id,
            )

        if application.state is EventApplicationState.EMERGENCY_RUNNING:
            child_id = application.emergency_plan_run_id
            if child_id is None:
                raise PlanStoreInvariantError("紧急 Application 缺少 child PlanRun")
            child = await self._run_child(child_id, claim)
            if child.state is PlanRunState.FAILED:
                failure = self._failure(event_id)
                application = self._event_store.transition_application(
                    event_id,
                    root_plan_run_id,
                    expected_state=EventApplicationState.EMERGENCY_RUNNING,
                    target_state=EventApplicationState.FAILED,
                    now=now,
                    failure=failure,
                )
                self._event_store.transition_inbox(
                    event_id,
                    expected_state=EventInboxState.PROCESSING,
                    target_state=EventInboxState.FAILED,
                    now=now,
                    worker_id=claim.record.lease_owner,
                    fencing_token=claim.fencing_token,
                    failure=failure,
                )
                return PreemptionResult(
                    status=PreemptionStatus.FAILED,
                    event_id=event_id,
                    root_plan_run_id=root_plan_run_id,
                    failure=failure,
                )
            if child.state is not PlanRunState.SUCCEEDED:
                waiting_node = any(
                    node.state is PlanNodeState.WAITING_RECONCILIATION
                    for node in self._plan_store.list_nodes(child_id)
                )
                if waiting_node:
                    application = self._event_store.transition_application(
                        event_id,
                        root_plan_run_id,
                        expected_state=EventApplicationState.EMERGENCY_RUNNING,
                        target_state=EventApplicationState.WAITING_RECONCILIATION,
                        now=now,
                    )
                    self._event_store.transition_inbox(
                        event_id,
                        expected_state=EventInboxState.PROCESSING,
                        target_state=EventInboxState.WAITING_HUMAN,
                        now=now,
                        worker_id=claim.record.lease_owner or self._worker_id,
                        fencing_token=claim.fencing_token,
                    )
                    return self._waiting_result(event_id, root_plan_run_id, application)
                self._event_store.transition_inbox(
                    event_id,
                    expected_state=EventInboxState.PROCESSING,
                    target_state=EventInboxState.VERIFIED,
                    now=now,
                    worker_id=claim.record.lease_owner or self._worker_id,
                    fencing_token=claim.fencing_token,
                )
                return PreemptionResult(
                    status=PreemptionStatus.RETRY_PENDING,
                    event_id=event_id,
                    root_plan_run_id=root_plan_run_id,
                )
            application = self._event_store.transition_application(
                event_id,
                root_plan_run_id,
                expected_state=EventApplicationState.EMERGENCY_RUNNING,
                target_state=EventApplicationState.REPLAN_READY,
                now=now,
            )

        if application.state is EventApplicationState.REPLAN_READY:
            version = self._plan_store.get_plan_version(root_plan_run_id, root.current_version)
            from src.plan_engine.models import CardBatchPlanningInput

            planning_input = CardBatchPlanningInput.model_validate(version.planning_input)
            analysis = ImpactAnalysis.model_validate(
                self._event_store.get_application(event_id, root_plan_run_id).impact_analysis
            )
            replan_result = self._replan.replan(
                root_plan_run_id=root_plan_run_id,
                planning_input=planning_input,
                failure_signature=canonical_json_sha256(
                    {"event_id": event_id, "analysis_digest": analysis.analysis_digest}
                ),
                now=now,
            )
            # ReplanCoordinator 已在 PlanStore CAS 成功后负责把 Application 标为
            # APPLIED；这里重新读取事实，避免第二次状态写入破坏幂等恢复。
            application = self._event_store.get_application(event_id, root_plan_run_id)
            if application.state is not EventApplicationState.APPLIED:
                raise PlanStoreInvariantError("Replan 成功后 Application 未闭合为 APPLIED")
            # child/DB 操作可能接近事件 lease 上限；最终闭合 Inbox 前续租并取得
            # 同一 fencing token，旧协调 Worker 不能在续租后继续晚到写入。
            completion_now = self._aware_utc(self._clock())
            self._event_store.heartbeat(
                event_id,
                worker_id=claim.record.lease_owner or self._worker_id,
                fencing_token=claim.fencing_token,
                now=completion_now,
                lease_seconds=60,
            )
            self._event_store.transition_inbox(
                event_id,
                expected_state=EventInboxState.PROCESSING,
                target_state=EventInboxState.APPLIED,
                now=completion_now,
                worker_id=claim.record.lease_owner,
                fencing_token=claim.fencing_token,
            )

        evidence = self._evidence(application)
        return PreemptionResult(
            status=PreemptionStatus.APPLIED,
            event_id=event_id,
            root_plan_run_id=root_plan_run_id,
            evidence_ref=evidence,
        )

    async def _run_child(self, child_plan_run_id: str, claim: EventClaim) -> Any:
        """只运行当前 child，避免全局 Worker 抢走其他 root 的节点。"""

        for _ in range(16):
            heartbeat_now = self._aware_utc(self._clock())
            self._event_store.heartbeat(
                claim.record.event.event_id,
                worker_id=claim.record.lease_owner or self._worker_id,
                fencing_token=claim.fencing_token,
                now=heartbeat_now,
                lease_seconds=60,
            )
            child = self._plan_store.get_plan_run(child_plan_run_id)
            if child.state in {PlanRunState.SUCCEEDED, PlanRunState.FAILED}:
                return child
            result = await self._emergency_worker.run_once(child_plan_run_id)
            child = self._plan_store.get_plan_run(child_plan_run_id)
            if result.waiting_human or result.waiting_approval:
                return child
            if result.retried:
                return child
            if result.claimed == 0:
                return child
        raise PlanStoreInvariantError("紧急 child 在单次协调中超过固定推进上限")

    def _emergency_plan(self, claim: EventClaim, root_plan_run_id: str) -> MaterializedPlan:
        """从当前权威 Inbox 构造固定 child DAG，版本/资源由 Capability 注入。"""

        event = claim.record.event
        request = EmergencySoldOutPlanningInput(
            room_id=event.room_id,
            trace_id=f"{event.event_id}:{root_plan_run_id}",
            root_plan_run_id=root_plan_run_id,
            parent_plan_run_id=root_plan_run_id,
            trigger_event_id=event.event_id,
            event=event,
            provenance=claim.record.provenance,
            expected_version=event.observed_version,
        )
        proposal = self._provider.propose_sync(request)
        capabilities = {
            node.logical_key: (
                self._profile.resolve_emergency_control_node(logical_key=node.logical_key)
                if node.skill_id is None
                else self._profile.resolve_emergency_skill_node(
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

    def _evidence(self, application: Any) -> PreemptionEvidenceRef:
        """从 child 最终提示节点只读提取建议事实，不生成新的业务动作。"""

        if application.state is not EventApplicationState.APPLIED:
            raise PlanStoreInvariantError("只有 APPLIED Application 可以生成 EvidenceRef")
        child_id = application.emergency_plan_run_id
        if child_id is None:
            raise PlanStoreInvariantError("已完成 Application 缺少 child PlanRun")
        final_message = "售罄事件已完成确定性处理"
        for node in self._plan_store.list_nodes(child_id):
            if node.logical_key != "generate-sold-out-prompt":
                continue
            runs = self._plan_store.list_node_runs(child_id, node.node_id)
            if not runs:
                continue
            output = runs[-1].output
            prompt = output.get("prompt") if isinstance(output, dict) else None
            if isinstance(prompt, dict) and isinstance(prompt.get("message"), str):
                final_message = prompt["message"]
        return PreemptionEvidenceRef.create(
            event_id=application.event_id,
            root_plan_run_id=application.root_plan_run_id,
            application_state=application.state.value,
            emergency_plan_run_id=child_id,
            applied_plan_version=application.applied_plan_version,
            final_suggestion_fact=final_message,
        )

    def _waiting_result(self, event_id: str, root_plan_run_id: str, application: Any) -> PreemptionResult:
        """等待对账或下一次 lease 的结果不伪造成功证据。"""

        return PreemptionResult(
            status=PreemptionStatus.WAITING_RECONCILIATION,
            event_id=event_id,
            root_plan_run_id=root_plan_run_id,
        )

    @staticmethod
    def _failure(event_id: str) -> FailureFact:
        """构造脱敏、不可自动回退的协调失败事实。"""

        return FailureFact(
            category=FailureCategory.INTERNAL_INVARIANT,
            external_code="preemption.coordinator_failed",
            side_effect_state=SideEffectState.NOT_SENT,
            attempt_id=f"preemption:{event_id}",
        )

    def _active_card_batch_roots(self, room_id: str) -> tuple[str, ...]:
        """返回指定 room 的稳定活动 CARD_BATCH root 集合。"""

        return tuple(
            sorted(
                plan_run.plan_run_id
                for plan_run in self._plan_store.list_plan_runs()
                if plan_run.room_id == room_id
                and plan_run.plan_kind is PlanRunKind.CARD_BATCH
            )
        )

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        """协调器所有持久化时间统一为带时区 UTC。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("协调时间必须包含时区")
        return value.astimezone(timezone.utc)

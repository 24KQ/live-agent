"""Phase 14 Task 6 可信售罄的人机协同控制面。

本模块只编排三个已经存在的权威边界：Phase 12B 的售罄抢占/对账协调器、Phase 14
Workspace Store 和 Phase 12A CommandService。它不复制事件、计划或审批状态，也不
接受原始 PlanCommand 作为经营恢复入口。自动保护可以在没有人工决定时运行；真正的
经营恢复必须先由 Task 5 Compiler 生成 ``CompiledOperatorDecision``，再按 Store 的
CAS、operator lease 与 fencing 规则入账并提交到 PlanEngine。
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from src.decision_support.commands import (
    CompiledOperatorDecision,
)
from src.decision_support.models import (
    DecisionKind,
    Incident,
    LiveSessionWorkspace,
    OperatorDecision,
    WorkspaceView,
)
from src.decision_support.store import (
    InMemoryDecisionSupportStore,
    WorkspaceConflictError,
)
from src.plan_engine.commands import PlanCommand, PlanCommandResult
from src.plan_engine.event_store import EventStore
from src.plan_engine.event_state_machine import EventInboxState
from src.plan_engine.preemption import PreemptionResult, PreemptionStatus
from src.skill_runtime.models import FailureFact


class SoldOutFlowBoundaryError(RuntimeError):
    """售罄恢复请求越过人类决定或 Workspace 事实边界时抛出的稳定错误。"""


class SoldOutFlowStatus(StrEnum):
    """工作台可展示的售罄控制结果，不把未知副作用伪装成成功。"""

    PROTECTED = "PROTECTED"
    PROTECTION_PENDING = "PROTECTION_PENDING"
    WAITING_RECONCILIATION = "WAITING_RECONCILIATION"
    RECOVERY_ACCEPTED = "RECOVERY_ACCEPTED"
    RECOVERY_REJECTED = "RECOVERY_REJECTED"
    FAILED = "FAILED"


class SoldOutFlowResult(BaseModel):
    """一次保护、对账或人工恢复调用的只读摘要。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: SoldOutFlowStatus
    event_id: str = Field(..., min_length=1)
    root_plan_run_id: str = Field(..., min_length=1)
    incident_id: str = Field(..., min_length=1)
    protection_status: PreemptionStatus | None = None
    command_result: PlanCommandResult | None = None
    failure: FailureFact | None = None


class _ProtectionCoordinator(Protocol):
    """Phase 12B PreemptionCoordinator 的最小受控调用面。"""

    async def run_next(
        self,
        *,
        root_plan_run_id: str,
        now: datetime,
    ) -> PreemptionResult:
        """推进一个已验证售罄事件的确定性保护链。"""

    async def reconcile_waiting(
        self,
        *,
        event_id: str,
        root_plan_run_id: str,
        now: datetime,
    ) -> PreemptionResult:
        """只读闭合原 Attempt，不重复发送售罄写。"""


class _CommandService(Protocol):
    """Phase 12A 命令账本的最小执行端口。"""

    def submit(self, command: PlanCommand, *, now: datetime) -> PlanCommandResult:
        """把已经由人工决定编译的命令提交到权威 PlanStore。"""


class HumanGuidedSoldOutFlow:
    """把可信售罄事实接入三场景 Workspace，并保留运营最终权限。

    ``handle_verified_event`` 只登记不可变 Incident 并调用 Phase 12B 确定性保护。
    ``submit_compiled_recovery`` 是唯一经营恢复入口，调用方必须先经过 Task 5
    Compiler；方法本身仍把决定、命令和 PlanCommand 分别交给各自权威账本。
    """

    def __init__(
        self,
        *,
        workspace_store: InMemoryDecisionSupportStore,
        event_store: EventStore,
        protection_coordinator: _ProtectionCoordinator,
        command_service: _CommandService,
    ) -> None:
        self._workspace_store = workspace_store
        self._event_store = event_store
        self._protection = protection_coordinator
        self._command_service = command_service

    @property
    def workspace_store(self) -> InMemoryDecisionSupportStore:
        """公开只读 Store 引用，供工作台查询和测试验证事实；不暴露内部容器。"""

        return self._workspace_store

    async def handle_verified_event(
        self,
        *,
        event_id: str,
        root_plan_run_id: str,
        now: datetime,
    ) -> SoldOutFlowResult:
        """在 LIVE Workspace 中推进一次已验证售罄事件的自动保护。"""

        instant = self._aware_utc(now)
        workspace, inbox = self._validate_event_scope(event_id, root_plan_run_id)
        incident_id = self._incident_id(event_id, root_plan_run_id)
        self._append_incident(workspace, inbox.event, incident_id, instant)

        # Event Store 负责事件 lease/fencing；已经 APPLIED 的事件只能读取重放，
        # 不再次调用协调器，避免响应丢失后的第二次售罄写。
        if inbox.state is EventInboxState.APPLIED:
            return SoldOutFlowResult(
                status=SoldOutFlowStatus.PROTECTED,
                event_id=event_id,
                root_plan_run_id=root_plan_run_id,
                incident_id=incident_id,
                protection_status=PreemptionStatus.APPLIED,
            )
        if inbox.state is not EventInboxState.VERIFIED:
            raise WorkspaceConflictError(
                "售罄事件必须处于 VERIFIED，等待对账事件只能走 reconcile_waiting"
            )

        protection = await self._protection.run_next(
            root_plan_run_id=root_plan_run_id,
            now=instant,
        )
        return self._protection_result(
            event_id=event_id,
            root_plan_run_id=root_plan_run_id,
            incident_id=incident_id,
            protection=protection,
        )

    async def reconcile_waiting(
        self,
        *,
        event_id: str,
        root_plan_run_id: str,
        now: datetime,
    ) -> SoldOutFlowResult:
        """只读闭合未知副作用，并把后续运行留给下一次确定性协调。"""

        instant = self._aware_utc(now)
        workspace, inbox = self._validate_event_scope(event_id, root_plan_run_id)
        incident_id = self._incident_id(event_id, root_plan_run_id)
        self._append_incident(workspace, inbox.event, incident_id, instant)
        protection = await self._protection.reconcile_waiting(
            event_id=event_id,
            root_plan_run_id=root_plan_run_id,
            now=instant,
        )
        return self._protection_result(
            event_id=event_id,
            root_plan_run_id=root_plan_run_id,
            incident_id=incident_id,
            protection=protection,
        )

    def submit_compiled_recovery(
        self,
        *,
        compiled: CompiledOperatorDecision,
        expected_workspace_version: int,
        operator_id: str,
        fencing_token: int,
        now: datetime,
    ) -> SoldOutFlowResult:
        """提交经过人工决定编译的恢复意图，不允许调用方直接给 PlanCommand。"""

        instant = self._aware_utc(now)
        try:
            # 调用方可能在同进程内用 model_construct 绕过嵌套校验；恢复入口必须
            # 重新从 JSON 事实重载，而不是把“带有正确类型”的对象视为可信授权。
            normalized = CompiledOperatorDecision.model_validate(
                compiled.model_dump(mode="json")
            )
        except Exception as exc:
            raise SoldOutFlowBoundaryError(
                "CompiledOperatorDecision 不是可验证的冻结事实"
            ) from exc
        decision = normalized.operator_decision
        workspace = self._workspace_store.get_workspace(decision.live_session_id)
        if workspace.view is not WorkspaceView.LIVE:
            raise WorkspaceConflictError("售罄经营恢复必须发生在 LIVE Workspace")
        incident_id = decision.snapshot.get("incident_id")
        if not isinstance(incident_id, str) or not incident_id:
            raise SoldOutFlowBoundaryError(
                "OperatorDecision 缺少不可变 incident_id 绑定"
            )
        incident = self._workspace_store.get_incident(incident_id)
        if incident.live_session_id != workspace.live_session_id:
            raise WorkspaceConflictError("恢复 Incident 不属于当前 Workspace")
        if decision.decision_kind is DecisionKind.REJECT:
            if (
                normalized.execution_command is not None
                or normalized.plan_command is not None
            ):
                raise SoldOutFlowBoundaryError("REJECT 不得携带或执行恢复命令")
            self._workspace_store.append_operator_decision(
                decision,
                expected_workspace_version=expected_workspace_version,
                operator_id=operator_id,
                fencing_token=fencing_token,
                now=instant,
            )
            return SoldOutFlowResult(
                status=SoldOutFlowStatus.RECOVERY_REJECTED,
                event_id=self._event_id_from_decision(decision),
                root_plan_run_id=workspace.root_plan_run_id,
                incident_id=incident.incident_id,
            )

        execution_command = normalized.execution_command
        plan_command = normalized.plan_command
        if execution_command is None or plan_command is None:
            raise SoldOutFlowBoundaryError(
                "APPROVE/MODIFY 必须先经过 Task 5 OperatorDecision Compiler"
            )
        if execution_command.decision_id != decision.decision_id:
            raise SoldOutFlowBoundaryError("ExecutionCommand 未绑定 OperatorDecision")
        if plan_command.command_type.value != "APPROVE":
            raise SoldOutFlowBoundaryError("经营恢复只允许 APPROVE PlanCommand")
        if plan_command.plan_run_id != workspace.root_plan_run_id:
            raise WorkspaceConflictError("恢复命令不属于当前 Workspace root PlanRun")

        after_decision = self._workspace_store.append_operator_decision(
            decision,
            expected_workspace_version=expected_workspace_version,
            operator_id=operator_id,
            fencing_token=fencing_token,
            now=instant,
        )
        self._workspace_store.append_execution_command(
            execution_command,
            expected_workspace_version=after_decision.version,
            operator_id=operator_id,
            fencing_token=fencing_token,
        )
        command_result = self._command_service.submit(plan_command, now=instant)
        return SoldOutFlowResult(
            status=(
                SoldOutFlowStatus.RECOVERY_ACCEPTED
                if command_result.accepted
                else SoldOutFlowStatus.RECOVERY_REJECTED
            ),
            event_id=self._event_id_from_decision(decision),
            root_plan_run_id=workspace.root_plan_run_id,
            incident_id=incident.incident_id,
            command_result=command_result,
        )

    def submit_raw_recovery_command(
        self,
        command: PlanCommand,
        *,
        now: datetime,
    ) -> None:
        """显式拒绝绕过 OperatorDecision 的原始命令入口。"""

        raise SoldOutFlowBoundaryError(
            "售罄经营恢复必须由 OperatorDecision 编译，不能直接提交 PlanCommand"
        )

    def _validate_event_scope(self, event_id: str, root_plan_run_id: str):
        """从 Event Store 和 Workspace 重新读取父事实，不信任调用方 room 参数。"""

        workspace = self._workspace_store.get_workspace_by_root_plan(root_plan_run_id)
        if workspace.view is not WorkspaceView.LIVE:
            raise WorkspaceConflictError("售罄事件必须绑定 LIVE Workspace")
        inbox = self._event_store.get_inbox(event_id)
        if inbox.event.room_id != workspace.room_id:
            raise WorkspaceConflictError("售罄事件 room 与 Workspace 不一致")
        return workspace, inbox

    def _append_incident(
        self,
        workspace: LiveSessionWorkspace,
        event: Any,
        incident_id: str,
        now: datetime,
    ) -> None:
        """以事件事实生成稳定 Incident；重复调用由 Store 幂等重放。"""

        incident = Incident(
            incident_id=incident_id,
            live_session_id=workspace.live_session_id,
            idempotency_key=f"sold-out-incident:{incident_id}",
            incident_type="SOLD_OUT_AUTOMATIC_PROTECTION",
            source_ref_ids=(event.event_id,),
            snapshot={
                "event_id": event.event_id,
                "room_id": event.room_id,
                "product_id": event.product_id,
                "expected_version": event.observed_version,
                "payload_digest": event.payload_digest,
            },
            created_at=now,
        )
        self._workspace_store.append_incident(
            incident,
            expected_workspace_version=self._workspace_store.get_workspace(
                workspace.live_session_id
            ).version,
        )

    @staticmethod
    def _incident_id(event_id: str, root_plan_run_id: str) -> str:
        """同一事件应用到不同 root 时保留不同 Incident lineage。"""

        return f"incident:{event_id}:{root_plan_run_id}"

    @staticmethod
    def _protection_result(
        *,
        event_id: str,
        root_plan_run_id: str,
        incident_id: str,
        protection: PreemptionResult,
    ) -> SoldOutFlowResult:
        """集中映射 Phase 12B 状态，避免 UNKNOWN 被归类为成功。"""

        status = {
            PreemptionStatus.APPLIED: SoldOutFlowStatus.PROTECTED,
            PreemptionStatus.WAITING_RECONCILIATION: SoldOutFlowStatus.WAITING_RECONCILIATION,
            PreemptionStatus.FAILED: SoldOutFlowStatus.FAILED,
        }.get(protection.status, SoldOutFlowStatus.PROTECTION_PENDING)
        return SoldOutFlowResult(
            status=status,
            event_id=event_id,
            root_plan_run_id=root_plan_run_id,
            incident_id=incident_id,
            protection_status=protection.status,
            failure=protection.failure,
        )

    @staticmethod
    def _event_id_from_decision(decision: OperatorDecision) -> str:
        """从 Compiler 留存的结构化快照取事件身份，缺失时拒绝伪造展示身份。"""

        event_id = decision.snapshot.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            return "decision-only"
        return event_id

    @staticmethod
    def _aware_utc(value: datetime) -> datetime:
        """所有跨 Store 时间统一为 UTC。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("售罄控制时间必须包含时区")
        return value.astimezone(timezone.utc)


__all__ = [
    "HumanGuidedSoldOutFlow",
    "SoldOutFlowBoundaryError",
    "SoldOutFlowResult",
    "SoldOutFlowStatus",
]

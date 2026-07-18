"""Phase 14 Task 7 决策支持 API 的受控服务门面。

HTTP/WebSocket 层不能直接持有 Workspace Store、PlanStore 或 Runtime。这里把读取、
Proposal 追加和人工决定编译集中到一个窄服务中：Proposal/Decision 仍由 append-only
Store 保存，批准后的命令只有在装配了 HumanGuidedSoldOutFlow 时才允许交给
CommandService，未装配时明确拒绝而不是假报成功。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.config.settings import Settings, get_settings
from src.decision_support.commands import (
    DecisionExecutionContext,
    DecisionSupportCommandCompiler,
    OperatorDecisionDraft,
)
from src.decision_support.multi_agent import HighConflictEscalationCoordinator
from src.decision_support.models import (
    DecisionKind,
    EscalationMode,
    Proposal,
    WorkspaceView,
)
from src.decision_support.sold_out_flow import HumanGuidedSoldOutFlow
from src.decision_support.store import (
    InMemoryDecisionSupportStore,
    PostgresDecisionSupportStore,
    WorkspaceConflictError,
    WorkspaceLeaseError,
)


class DecisionSupportServiceUnavailable(RuntimeError):
    """需要权威 Runtime 装配但当前服务没有执行依赖时的 fail-closed 错误。"""


class DecisionSupportProposalRequest(BaseModel):
    """Proposal API 的结构化输入；不允许 HTTP 携带工具调用或自由执行字段。"""

    model_config = ConfigDict(extra="forbid")

    proposal: Proposal
    expected_workspace_version: int = Field(..., ge=1, strict=True)
    request_idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)


class DecisionSupportDecisionRequest(BaseModel):
    """人工决定 API 输入；PlanEngine 并发快照仍由最终 Store 再次校验。"""

    model_config = ConfigDict(extra="forbid")

    draft: OperatorDecisionDraft
    execution_context: DecisionExecutionContext


class MultiAgentEscalationRequest(BaseModel):
    """人工升级的最小 HTTP 输入，父事实与并发控制只能由服务端重建。

    请求不能携带完整 EvidenceBundle、Profile、触发码、operator、lease 或 fencing token。
    这些字段一旦来自 HTTP，就会让调用方伪造 Coordinator 的输入边界；端点只把规范的
    idempotency header 写入该 transient 字段，Service 仍会按 Bundle ID 重新读取权威事实。
    """

    model_config = ConfigDict(extra="forbid")

    evidence_bundle_id: str = Field(..., min_length=1, max_length=256)
    expected_workspace_version: int = Field(..., ge=1, strict=True)


def canonical_multi_agent_escalation_idempotency_key(evidence_bundle_id: str) -> str:
    """从唯一 Bundle 生成唯一人工升级身份，禁止 HTTP 自选影响持久化重放。"""

    if not evidence_bundle_id:
        raise ValueError("evidence_bundle_id must not be empty")
    return f"phase16-escalation:operator_requested:{evidence_bundle_id}"


class DecisionSupportService:
    """Workspace/Proposal/Decision 的统一受控门面。"""

    def __init__(
        self,
        *,
        store: Any,
        recovery_flow: HumanGuidedSoldOutFlow | None = None,
        compiler: DecisionSupportCommandCompiler | None = None,
        multi_agent_coordinator: HighConflictEscalationCoordinator | None = None,
        lease_seconds: int = 60,
    ) -> None:
        if type(lease_seconds) is not int or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        self._store = store
        self._recovery_flow = recovery_flow
        self._compiler = compiler or DecisionSupportCommandCompiler()
        # Coordinator 只能由启动装配显式注入。默认门面不构造 Runner，避免普通 HTTP
        # 请求把 DETERMINISTIC_ONLY 路由升级为模型路径或取得额外执行能力。
        self._multi_agent_coordinator = multi_agent_coordinator
        self._lease_seconds = lease_seconds

    def get_workspace_payload(self, live_session_id: str) -> dict[str, Any]:
        """返回同一 Workspace 的根事实与 append-only 历史摘要。"""

        workspace = self._store.get_workspace(live_session_id)
        return {
            **workspace.model_dump(mode="json"),
            "incidents": [
                item.model_dump(mode="json")
                for item in self._store.list_incidents(live_session_id)
            ],
            "escalations": [
                item.model_dump(mode="json")
                for item in self._store.list_escalations(live_session_id)
            ],
            "conflict_analyses": [
                item.model_dump(mode="json")
                for item in self._store.list_conflict_analyses(live_session_id)
            ],
            "multi_agent_outcomes": [
                item.model_dump(mode="json")
                for item in self._store.list_multi_agent_outcomes(live_session_id)
            ],
            "proposals": [
                item.model_dump(mode="json")
                for item in self._store.list_proposals(live_session_id)
            ],
            "operator_decisions": [
                item.model_dump(mode="json")
                for item in self._store.list_operator_decisions(live_session_id)
            ],
            "execution_commands": [
                item.model_dump(mode="json")
                for item in self._store.list_execution_commands(live_session_id)
            ],
        }

    async def request_multi_agent_escalation(
        self,
        *,
        live_session_id: str,
        request: MultiAgentEscalationRequest,
        operator_id: str,
        request_idempotency_key: str,
    ) -> dict[str, Any]:
        """以服务端 Bundle、CAS 与 lease 装配人工高冲突升级，绝不信任 HTTP 父事实。"""

        if self._multi_agent_coordinator is None:
            raise DecisionSupportServiceUnavailable(
                "multi-agent escalation requires an explicitly assembled coordinator"
            )
        expected_key = canonical_multi_agent_escalation_idempotency_key(
            request.evidence_bundle_id
        )
        if request_idempotency_key != expected_key:
            raise WorkspaceConflictError(
                "multi-agent escalation idempotency key conflicts with bundle"
            )
        if not operator_id:
            raise WorkspaceLeaseError("operator identity is required")

        # Bundle 的六角色快照、作用域和可升级资格只能来自 append-only Store。HTTP 只
        # 提供其稳定 ID；同 ID 的伪造快照、跨 session Bundle 或旧 CAS 都在模型前拒绝。
        bundle = self._store.get_evidence_bundle(request.evidence_bundle_id)
        if bundle.live_session_id != live_session_id:
            raise WorkspaceConflictError("escalation bundle does not belong to workspace")
        workspace = self._store.get_workspace(live_session_id)
        existing = next(
            (
                escalation
                for escalation in self._store.list_escalations(live_session_id)
                if escalation.evidence_bundle_id == request.evidence_bundle_id
            ),
            None,
        )
        if existing is not None and existing.mode is not EscalationMode.OPERATOR_REQUESTED:
            raise WorkspaceConflictError("bundle already has an automatic escalation")
        is_response_loss_replay = existing is not None
        if not is_response_loss_replay and workspace.view is not WorkspaceView.LIVE:
            raise WorkspaceConflictError("multi-agent escalation requires LIVE workspace")
        if (
            not is_response_loss_replay
            and workspace.version != request.expected_workspace_version
        ):
            raise WorkspaceConflictError("workspace version conflict")

        # 租约和 fencing token 不经过 HTTP。Store 获取或续用认证操作员的当前 lease，
        # Coordinator/Store 在真正 append 时还会用同一 token 做事务级二次校验。
        lease = self._store.acquire_operator_lock(
            live_session_id,
            operator_id,
            self._lease_seconds,
        )
        result = await self._multi_agent_coordinator.run_operator_requested(
            bundle,
            # D-155：初次请求必须满足 HTTP 原始 CAS；同规范 key 已经产生同 Bundle
            # Escalation 时，响应丢失重试只用 Store 当前版本恢复既有不可变事实。两个
            # 分支都要求当前 operator lease，且 Coordinator 仍禁止第二次模型发送。
            expected_workspace_version=(
                workspace.version
                if is_response_loss_replay
                else request.expected_workspace_version
            ),
            operator_id=lease.operator_id,
            fencing_token=lease.fencing_token,
        )

        # 写入 HTTP 结果只回传稳定事实身份，不能把完整 Workspace 或 Agent 产物正文作为
        # 另一个 snapshot API 暴露出去。订阅端由 API 层写后重新读取同一 Store 投影。
        return {
            "accepted": result.selected,
            "request_idempotency_key": request_idempotency_key,
            "escalation_id": (
                None if result.escalation is None else result.escalation.escalation_id
            ),
            "analysis_id": (
                None if result.analysis is None else result.analysis.analysis_id
            ),
            "proposal_id": (
                None if result.proposal is None else result.proposal.proposal_id
            ),
            "outcome_id": (
                None if result.outcome is None else result.outcome.outcome_id
            ),
        }

    def create_proposal(
        self,
        request: DecisionSupportProposalRequest,
        *,
        operator_id: str,
    ) -> dict[str, Any]:
        """按 Workspace CAS 追加 Proposal；重复 idempotency 由 Store 原子重放。"""

        proposal = request.proposal
        if not operator_id or proposal.live_session_id == "":
            raise WorkspaceConflictError("proposal identity is invalid")
        if request.request_idempotency_key is not None and request.request_idempotency_key != proposal.idempotency_key:
            raise WorkspaceConflictError("Proposal idempotency key conflicts with HTTP key")
        workspace = self._store.get_workspace(proposal.live_session_id)
        if workspace.view is not WorkspaceView.LIVE:
            raise WorkspaceConflictError("Proposal requires LIVE Workspace")
        if workspace.version != request.expected_workspace_version:
            raise WorkspaceConflictError("workspace version conflict")
        updated = self._store.append_proposal(
            proposal,
            expected_workspace_version=request.expected_workspace_version,
        )
        return {
            "accepted": True,
            "proposal": proposal.model_dump(mode="json"),
            "workspace": updated.model_dump(mode="json"),
            "operator_id": operator_id,
        }

    def submit_decision(
        self,
        *,
        live_session_id: str,
        request: DecisionSupportDecisionRequest,
        operator_id: str,
    ) -> dict[str, Any]:
        """获取操作员 lease、编译决定并交给唯一恢复门面。"""

        if request.draft.operator_id != operator_id:
            raise WorkspaceConflictError("decision operator does not match authenticated operator")
        proposal = self._store.get_proposal(request.draft.proposal_id)
        if proposal.live_session_id != live_session_id:
            raise WorkspaceConflictError("decision proposal does not belong to workspace")
        workspace = self._store.get_workspace(live_session_id)
        lease = self._store.acquire_operator_lock(
            live_session_id,
            operator_id,
            self._lease_seconds,
        )
        # D-152：通用 Proposal 的 READY 状态不足以授权多 Agent 经营恢复。Service 只从
        # append-only Store 读取同一 Proposal ID 的 Outcome；Compiler 还会复核完整
        # Proposal/Analysis/Escalation 摘要，缺失或不匹配时 APPROVE/MODIFY 保持 fail-closed。
        matching_outcomes = [
            item
            for item in self._store.list_multi_agent_outcomes(live_session_id)
            if item.proposal_id == proposal.proposal_id
        ]
        ready_outcome = matching_outcomes[0] if len(matching_outcomes) == 1 else None
        compiled = self._compiler.compile(
            proposal=proposal,
            draft=request.draft,
            lease=lease,
            execution_context=request.execution_context,
            now=datetime.now(timezone.utc),
            multi_agent_ready_outcome=ready_outcome,
        )
        if self._recovery_flow is None and request.draft.decision_kind is DecisionKind.REJECT:
            updated = self._store.append_operator_decision(
                compiled.operator_decision,
                expected_workspace_version=workspace.version,
                operator_id=operator_id,
                fencing_token=lease.fencing_token,
            )
            return {
                "status": "RECOVERY_REJECTED",
                "decision": compiled.operator_decision.model_dump(mode="json"),
                "workspace": updated.model_dump(mode="json"),
            }
        if self._recovery_flow is None:
            raise DecisionSupportServiceUnavailable(
                "approved decision requires an assembled HumanGuidedSoldOutFlow"
            )
        result = self._recovery_flow.submit_compiled_recovery(
            compiled=compiled,
            expected_workspace_version=workspace.version,
            operator_id=operator_id,
            fencing_token=lease.fencing_token,
            now=datetime.now(timezone.utc),
        )
        return result.model_dump(mode="json")


def create_default_decision_support_service(
    settings: Settings | None = None,
) -> DecisionSupportService:
    """创建默认 PostgreSQL 读写门面；未装配 Recovery Flow 时批准请求仍拒绝执行。"""

    selected = settings or get_settings()
    store = PostgresDecisionSupportStore(selected)
    store.initialize_schema()
    return DecisionSupportService(store=store)


def create_in_memory_decision_support_service(
    store: InMemoryDecisionSupportStore | None = None,
) -> DecisionSupportService:
    """创建不连接数据库的 API 测试/演示门面。"""

    return DecisionSupportService(store=store or InMemoryDecisionSupportStore())


__all__ = [
    "DecisionSupportDecisionRequest",
    "DecisionSupportProposalRequest",
    "MultiAgentEscalationRequest",
    "canonical_multi_agent_escalation_idempotency_key",
    "DecisionSupportService",
    "DecisionSupportServiceUnavailable",
    "create_default_decision_support_service",
    "create_in_memory_decision_support_service",
]

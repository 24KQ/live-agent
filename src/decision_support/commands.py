"""Phase 14 Task 5 运营决定校验与受控命令编译。"""

from __future__ import annotations

from datetime import datetime, timezone
import unicodedata
from typing import Any

from pydantic import ConfigDict, Field, field_validator, model_validator

from src.decision_support.models import (
    DecisionKind,
    DecisionSupportFrozenModel,
    ExecutionCommand,
    OperatorDecision,
    OperatorLease,
    Proposal,
)
from src.decision_support.proposal import (
    DecisionOption,
    DecisionTiming,
    LiveDecisionProposal,
    ProposalStatus,
)
from src.plan_engine.commands import PlanCommand
from src.plan_engine.models import PlanCommandType, PlanNodeState


class DecisionCompilationError(ValueError):
    """Proposal、OperatorLease 或受控修改无法闭合时的稳定错误。"""


def _safe_host_prompt(value: str) -> str:
    """修改输入只允许工作台展示文本，阻断控制字符和协议伪造。"""

    if value != value.strip() or any(
        unicodedata.category(character).startswith("C") for character in value
    ):
        raise ValueError("host_prompt contains unsafe control characters")
    return value


class OperatorModification(DecisionSupportFrozenModel):
    """运营可修改的四个结构化字段；没有自由 JSON 或策略字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backup_product_id: str | None = Field(default=None, min_length=1, max_length=128)
    host_prompt: str | None = Field(default=None, min_length=1, max_length=300)
    priority: int | None = Field(default=None, ge=0, le=100, strict=True)
    timing: DecisionTiming | None = None

    @field_validator("host_prompt")
    @classmethod
    def _prompt_is_safe(cls, value: str | None) -> str | None:
        return None if value is None else _safe_host_prompt(value)

    @model_validator(mode="after")
    def _must_have_a_change(self) -> "OperatorModification":
        if not self.model_fields_set:
            raise ValueError("MODIFY requires at least one structured change")
        return self


class OperatorDecisionDraft(DecisionSupportFrozenModel):
    """工作台提交的人工决定请求，不是最终事实也不是可执行命令。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str = Field(..., min_length=1, max_length=128)
    proposal_id: str = Field(..., min_length=1, max_length=128)
    expected_proposal_version: int = Field(..., ge=1, strict=True)
    operator_id: str = Field(..., min_length=1, max_length=128)
    decision_kind: DecisionKind
    reason_code: str = Field(..., min_length=1, pattern=r"^[A-Z][A-Z0-9_]*$")
    idempotency_key: str = Field(..., min_length=1, max_length=256)
    option_id: str | None = Field(default=None, min_length=1, max_length=80)
    modification: OperatorModification | None = None

    @model_validator(mode="after")
    def _shape_matches_decision(self) -> "OperatorDecisionDraft":
        if self.decision_kind is DecisionKind.REJECT:
            if self.option_id is not None or self.modification is not None:
                raise ValueError("REJECT cannot carry option or modification")
        elif self.option_id is None:
            raise ValueError("APPROVE or MODIFY requires option_id")
        if self.decision_kind is DecisionKind.APPROVE and self.modification is not None:
            raise ValueError("APPROVE cannot carry modification")
        if self.decision_kind is DecisionKind.MODIFY and self.modification is None:
            raise ValueError("MODIFY requires modification")
        return self


class DecisionExecutionContext(DecisionSupportFrozenModel):
    """由权威 PlanStore 查询得到的命令并发快照，不接受 Proposal 自行声明。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_run_id: str = Field(..., min_length=1)
    expected_plan_version: int = Field(..., ge=1, strict=True)
    node_id: str = Field(..., min_length=1)
    expected_node_status: PlanNodeState

    @model_validator(mode="after")
    def _resume_requires_approval_state(self) -> "DecisionExecutionContext":
        if self.expected_node_status is not PlanNodeState.WAITING_APPROVAL:
            raise ValueError("controlled recovery requires WAITING_APPROVAL node")
        return self


class CompiledOperatorDecision(DecisionSupportFrozenModel):
    """Compiler 输出的分层事实；Proposal、Decision 和命令彼此不可覆盖。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operator_decision: OperatorDecision
    execution_command: ExecutionCommand | None = None
    plan_command: PlanCommand | None = None


class DecisionSupportCommandCompiler:
    """只把已验证人工决定编译为节点批准意图，不连接 Runtime 或平台 Adapter。"""

    def compile(
        self,
        *,
        proposal: Proposal,
        draft: OperatorDecisionDraft,
        lease: OperatorLease,
        execution_context: DecisionExecutionContext,
        now: datetime,
    ) -> CompiledOperatorDecision:
        """校验 Proposal/lease/CAS 后返回 append-only 决定和可选 APPROVE 命令。"""

        instant = self._normalize_now(now)
        try:
            persisted_proposal = Proposal.model_validate(
                proposal.model_dump(mode="json")
            )
            proposal_view = LiveDecisionProposal.model_validate(
                persisted_proposal.snapshot
            )
            selected_lease = OperatorLease.model_validate(
                lease.model_dump(mode="json")
            )
        except Exception as exc:
            raise DecisionCompilationError("proposal or lease snapshot is invalid") from exc

        if draft.proposal_id != persisted_proposal.proposal_id:
            raise DecisionCompilationError("proposal identity mismatch")
        if draft.expected_proposal_version != persisted_proposal.proposal_version:
            raise DecisionCompilationError("proposal version mismatch")
        if proposal_view.proposal_id != persisted_proposal.proposal_id:
            raise DecisionCompilationError("proposal payload identity mismatch")
        if (
            proposal_view.live_session_id != persisted_proposal.live_session_id
            or proposal_view.incident_id != persisted_proposal.incident_id
            or proposal_view.evidence_bundle_id != persisted_proposal.evidence_bundle_id
        ):
            raise DecisionCompilationError("proposal payload scope mismatch")
        if selected_lease.live_session_id != persisted_proposal.live_session_id:
            raise DecisionCompilationError("operator lease scope mismatch")
        if selected_lease.operator_id != draft.operator_id:
            raise DecisionCompilationError("operator does not own current lease")
        if selected_lease.lease_until <= instant:
            raise DecisionCompilationError("operator lease has expired")
        if draft.decision_kind in {DecisionKind.APPROVE, DecisionKind.MODIFY}:
            if proposal_view.status is not ProposalStatus.READY:
                raise DecisionCompilationError("DEGRADED proposal cannot be approved")
            selected_option = self._selected_option(proposal_view, draft.option_id)
            selected_option, priority, changes = self._apply_modification(
                selected_option, draft
            )
        else:
            selected_option = None
            priority = None
            changes = {}

        validation = {
            "status": "VALIDATED",
            "allowed_modification_fields": [
                "backup_product_id",
                "host_prompt",
                "priority",
                "timing",
            ],
            "proposal_version": persisted_proposal.proposal_version,
        }
        decision_snapshot: dict[str, Any] = {
            "incident_id": persisted_proposal.incident_id,
            "option_id": None if selected_option is None else selected_option.option_id,
            "changes": changes,
            "validation": validation,
        }
        operator_decision = OperatorDecision(
            decision_id=draft.decision_id,
            live_session_id=persisted_proposal.live_session_id,
            proposal_id=persisted_proposal.proposal_id,
            idempotency_key=draft.idempotency_key,
            expected_proposal_version=draft.expected_proposal_version,
            operator_id=draft.operator_id,
            decision_kind=draft.decision_kind,
            reason_code=draft.reason_code,
            snapshot=decision_snapshot,
            created_at=instant,
        )
        if selected_option is None:
            return CompiledOperatorDecision(operator_decision=operator_decision)

        payload = self._command_payload(
            decision=draft,
            proposal=persisted_proposal,
            option=selected_option,
            priority=priority,
            execution_context=execution_context,
            validation=validation,
        )
        plan_command = PlanCommand(
            command_id=f"plan-command:{draft.decision_id}",
            # Proposal 对应等待人工确认的节点；节点批准必须使用 APPROVE，
            # 不能误用只针对整个冻结 PlanRun 的 RESUME 命令。
            command_type=PlanCommandType.APPROVE,
            plan_run_id=execution_context.plan_run_id,
            expected_plan_version=execution_context.expected_plan_version,
            node_id=execution_context.node_id,
            expected_node_status=execution_context.expected_node_status,
            payload=payload,
            issued_at=instant,
        )
        execution_command = ExecutionCommand(
            command_id=f"execution-command:{draft.decision_id}",
            live_session_id=persisted_proposal.live_session_id,
            decision_id=draft.decision_id,
            idempotency_key=f"{draft.idempotency_key}:execution",
            command_kind="PLAN_COMMAND",
            snapshot={
                "plan_command": plan_command.model_dump(mode="json"),
                "validation": validation,
            },
            created_at=instant,
        )
        return CompiledOperatorDecision(
            operator_decision=operator_decision,
            execution_command=execution_command,
            plan_command=plan_command,
        )

    @staticmethod
    def _normalize_now(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise DecisionCompilationError("compiler clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _selected_option(
        proposal: LiveDecisionProposal, option_id: str | None
    ) -> DecisionOption:
        assert option_id is not None
        for option in proposal.options:
            if option.option_id == option_id:
                return option
        raise DecisionCompilationError("selected option is not in proposal")

    @staticmethod
    def _apply_modification(
        option: DecisionOption,
        draft: OperatorDecisionDraft,
    ) -> tuple[DecisionOption, int, dict[str, Any]]:
        modification = draft.modification
        if draft.decision_kind is DecisionKind.APPROVE:
            return option, 50, {}
        assert modification is not None
        changes = modification.model_dump(mode="json", exclude_unset=True)
        option_data = option.model_dump(mode="json")
        option_data.update(
            {
                field: value
                for field, value in changes.items()
                if field in {"backup_product_id", "host_prompt", "timing"}
            }
        )
        try:
            modified = DecisionOption.model_validate(option_data)
        except Exception as exc:
            raise DecisionCompilationError("structured modification is invalid") from exc
        return modified, changes.get("priority", 50), changes

    @staticmethod
    def _command_payload(
        *,
        decision: OperatorDecisionDraft,
        proposal: Proposal,
        option: DecisionOption,
        priority: int,
        execution_context: DecisionExecutionContext,
        validation: dict[str, Any],
    ) -> dict[str, Any]:
        """生成只包含编译后快照的 PlanCommand，不把原始 Proposal 引用交给执行器。"""

        return {
            "decision_id": decision.decision_id,
            "proposal_id": proposal.proposal_id,
            "proposal_version": proposal.proposal_version,
            "option_id": option.option_id,
            "product_strategy": option.product_strategy.value,
            "backup_product_id": option.backup_product_id,
            "host_prompt": option.host_prompt,
            "timing": option.timing.value,
            "priority": priority,
            "evidence_refs": [
                reference.model_dump(mode="json")
                for reference in option.evidence_refs
            ],
            "expected_plan_version": execution_context.expected_plan_version,
            "validation": validation,
        }

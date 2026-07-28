"""Phase 16 V3 单 Planner 真实模型诊断运行器。

该运行器复用 V2 已冻结的 Planner Profile、证据解析和受限 Specialist Runner，以便真实
调用的输入形状与失败路径保持可比；但它写入独立 V3 账本，只允许一个 case 和一次调用。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from src.decision_support.models import (
    ConflictConstraintCode,
    ConflictRiskCode,
)
from src.decision_support.multi_agent import (
    ValidatedConflictAnalysisPayload,
    build_phase16_smoke_evidence_v2_planner_profile,
    validate_v2_live_decision_planner_result,
)
from src.decision_support.multi_agent_evaluation import Phase16EvaluationDataset
from src.decision_support.official_smoke_evidence_v2 import (
    Phase16OfficialPriceEvidence,
    Phase16OfficialSmokeV2EvidenceManifest,
)
from src.decision_support.official_smoke_ledger_v3 import (
    PHASE16_V3_PLANNER_DIAGNOSTIC_CASE_ID,
    PHASE16_V3_PLANNER_RESERVATION_CNY,
    Phase16V3DiagnosticLedgerError,
    Phase16V3DiagnosticOutcomeStatus,
    Phase16V3DiagnosticValidationVerdict,
    PostgresPhase16V3PlannerDiagnosticLedger,
)
from src.decision_support.official_smoke_runner_v2 import (
    Phase16OfficialSmokeV2CaseProjection,
    _OfficialSmokePricingPolicy,
    build_phase16_official_smoke_v2_case_projection,
)
from src.decision_support.planner_diagnostic_v3 import (
    model_failure_fact_from_contract_breach,
    model_failure_fact_from_outcome,
)
from src.specialist_runtime.budget import BudgetInvariantError
from src.specialist_runtime.model_port import AgentModelPort, ModelFailure, ModelSuccess
from src.specialist_runtime.registry import SpecialistOrchestrator, SpecialistProfileRegistry
from src.specialist_runtime.runner import BoundedSpecialistRunner


@dataclass(frozen=True)
class Phase16V3PlannerDiagnosticReport:
    """命令入口可打印的最小结论，不包含 Prompt、模型正文或供应商原始 ID。"""

    status: Phase16V3DiagnosticOutcomeStatus
    reason_code: str
    attempt_id: str | None


@dataclass(frozen=True)
class _BudgetClaim:
    """满足共享 Runner 预算接口的最小结果；真实发送意图仍由 V3 PostgreSQL 账本保存。"""

    created: bool


class _CapturingDiagnosticModelPort:
    """逐字转发唯一模型调用，并保留 Outcome 供 V3 账本写入脱敏事实。"""

    def __init__(self, delegate: AgentModelPort) -> None:
        self._delegate = delegate
        self.request = None
        self.outcome: ModelSuccess | ModelFailure | None = None

    async def complete(self, request):
        """V3 不在端口层重试、fallback 或篡改请求，确保一次 dispatch 对应一次调用。"""

        self.request = request
        self.outcome = await self._delegate.complete(request)
        return self.outcome


class _NoSkillPort:
    """Planner 诊断 Profile 固定零 Skill；任何越权 Skill 调用都必须立即失败。"""

    async def invoke(self, **_kwargs: Any) -> dict[str, Any]:
        """拒绝未在冻结 Profile 中声明的 Skill 执行。"""

        raise RuntimeError("Phase 16 V3 planner diagnostic does not permit Skills")


class _V3DiagnosticBudgetAdapter:
    """将共享 Runner 的 reserve/settle 绑定到 V3 单次 append-only dispatch。"""

    def __init__(
        self,
        *,
        ledger: PostgresPhase16V3PlannerDiagnosticLedger,
        profile_digest: str,
    ) -> None:
        self._ledger = ledger
        self._profile_digest = profile_digest
        self.attempt = None
        self._request_id: str | None = None

    def reserve(self, request_id: str, candidate: object, amount_cny: Decimal) -> _BudgetClaim:
        """在端口调用前创建唯一 attempt；任何重复或超额都在网络前失败。"""

        if candidate != "PLANNER_DIAGNOSTIC" or self.attempt is not None:
            raise BudgetInvariantError("V3 diagnostic budget identity is invalid")
        if amount_cny > PHASE16_V3_PLANNER_RESERVATION_CNY:
            raise BudgetInvariantError("V3 diagnostic reservation exceeds frozen limit")
        try:
            self.attempt = self._ledger.begin_dispatch(
                case_id=PHASE16_V3_PLANNER_DIAGNOSTIC_CASE_ID,
                planner_profile_digest=self._profile_digest,
                internal_request_id=request_id,
            )
        except Phase16V3DiagnosticLedgerError as error:
            raise BudgetInvariantError("V3 diagnostic ledger rejected dispatch") from error
        self._request_id = request_id
        return _BudgetClaim(created=True)

    def settle(self, request_id: str, actual_cost_cny: Decimal | None) -> _BudgetClaim:
        """共享 Runner 的本地结算不覆盖 Provider usage；V3 账本随后独立写入回执。"""

        if self.attempt is None or request_id != self._request_id:
            raise BudgetInvariantError("V3 diagnostic settlement has no matching attempt")
        return _BudgetClaim(created=False)

    def release(self, request_id: str) -> _BudgetClaim:
        """端口前 deadline 耗尽时仍保留 intent，Runner 会以未发送 failure 事实闭合。"""

        if self.attempt is None or request_id != self._request_id:
            raise BudgetInvariantError("V3 diagnostic release has no matching attempt")
        return _BudgetClaim(created=False)


class Phase16V3PlannerDiagnosticRunner:
    """执行一次可诊断 Planner 调用，不创建生产 Proposal、OperatorDecision 或经营命令。"""

    def __init__(
        self,
        *,
        dataset: Phase16EvaluationDataset,
        parent_manifest: Phase16OfficialSmokeV2EvidenceManifest,
        official_price: Phase16OfficialPriceEvidence,
        ledger: PostgresPhase16V3PlannerDiagnosticLedger,
        model_port: AgentModelPort,
        clock: Any,
    ) -> None:
        self._dataset = dataset
        self._parent_manifest = parent_manifest
        self._official_price = official_price
        self._ledger = ledger
        self._model_port = model_port
        self._clock = clock

    async def execute(self) -> Phase16V3PlannerDiagnosticReport:
        """构建固定 Planner 输入，发送一次，并把 Outcome 精确闭合到独立 V3 账本。"""

        projection = self._build_projection()
        profile = build_phase16_smoke_evidence_v2_planner_profile()
        self._ledger.ensure_run(
            # V3 只读引用 V2 的冻结父 Manifest，run_id、case slot 与账本表仍完全独立。
            manifest_digest=self._parent_manifest.manifest_digest,
            planner_profile_digest=profile.profile_digest,
            case_digest=projection.case_digest,
        )
        analysis = ValidatedConflictAnalysisPayload(
            # 触发码和完整引用均来自冻结系统证据，而不是在 V3 中伪造新的 Analyst 回复。
            finding_codes=tuple(projection.trigger_codes),
            constraint_codes=(ConflictConstraintCode.OPERATOR_CONFIRMATION_REQUIRED,),
            risk_codes=(ConflictRiskCode.HUMAN_CONFIRMATION_REQUIRED,),
            explanation="系统已确认高冲突售罄证据，需要由人工审核受限方案。",
            evidence_refs=projection.evidence_refs,
        )
        task = projection.build_planner_task(analysis)
        request_id = str(
            uuid5(
                NAMESPACE_URL,
                f"phase16-v3:{self._parent_manifest.manifest_digest}:{projection.case_id}:PLANNER",
            )
        )
        capture = _CapturingDiagnosticModelPort(self._model_port)
        budget = _V3DiagnosticBudgetAdapter(
            ledger=self._ledger,
            profile_digest=profile.profile_digest,
        )
        runner = BoundedSpecialistRunner(
            orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
            model_port=capture,
            budget_store=budget,
            evidence_registry=projection.evidence_registry,
            skill_port=_NoSkillPort(),
            skill_catalog=(),
            trusted_anchor_resolver=lambda _task: projection.trusted_anchor_id,
            pricing_policy=_OfficialSmokePricingPolicy(self._official_price),
            budget_candidate_resolver=lambda _task: "PLANNER_DIAGNOSTIC",
            request_id_factory=lambda _task, _execution_id, _index: request_id,
            clock=self._clock,
        )
        result = await runner.run(task)
        attempt = budget.attempt
        if attempt is None:
            # 未进入 reserve 时没有发送意图，不能在账本中编造一条外部调用事实。
            return Phase16V3PlannerDiagnosticReport(
                status=Phase16V3DiagnosticOutcomeStatus.BLOCKED,
                reason_code="PRE_DISPATCH_BLOCKED",
                attempt_id=None,
            )
        if isinstance(capture.outcome, ModelFailure):
            fact = model_failure_fact_from_outcome(
                attempt_id=attempt.attempt_id,
                outcome=capture.outcome,
            )
            self._ledger.append_model_failure(fact)
            status = (
                Phase16V3DiagnosticOutcomeStatus.FAILED
                if fact.request_sent
                else Phase16V3DiagnosticOutcomeStatus.BLOCKED
            )
            reason_code = f"MODEL_FAILURE_{fact.category.value}"
            self._ledger.append_validation_fact(
                attempt_id=attempt.attempt_id,
                verdict=(
                    Phase16V3DiagnosticValidationVerdict.FAILED
                    if status is Phase16V3DiagnosticOutcomeStatus.FAILED
                    else Phase16V3DiagnosticValidationVerdict.BLOCKED
                ),
                reason_code=reason_code,
            )
            self._ledger.close_case(
                case_id=projection.case_id,
                status=status,
                reason_code=reason_code,
            )
            return Phase16V3PlannerDiagnosticReport(status, reason_code, attempt.attempt_id)
        if not isinstance(capture.outcome, ModelSuccess):
            # 端口异常或违反协议时不猜测网络发送状态；持久化 null 状态供报告明确展示未知。
            fact = model_failure_fact_from_contract_breach(
                attempt_id=attempt.attempt_id,
                latency_ms=Decimal("0"),
            )
            self._ledger.append_model_failure(fact)
            reason_code = "RUNNER_OUTCOME_CONTRACT_BREACH"
            self._ledger.append_validation_fact(
                attempt_id=attempt.attempt_id,
                verdict=Phase16V3DiagnosticValidationVerdict.FAILED,
                reason_code=reason_code,
            )
            self._ledger.close_case(
                case_id=projection.case_id,
                status=Phase16V3DiagnosticOutcomeStatus.FAILED,
                reason_code=reason_code,
            )
            return Phase16V3PlannerDiagnosticReport(
                Phase16V3DiagnosticOutcomeStatus.FAILED, reason_code, attempt.attempt_id
            )
        self._ledger.append_provider_receipt(attempt_id=attempt.attempt_id, success=capture.outcome)
        try:
            validate_v2_live_decision_planner_result(
                task=task,
                result=result,
                expected_profile=profile,
                expected_evidence_refs=projection.evidence_refs,
                required_risk_codes=frozenset(item.value for item in analysis.risk_codes),
                available_backup_product_ids=projection.available_backup_product_ids,
                proposal_eligible_and_fresh=projection.proposal_eligible,
            )
        except Exception:
            reason_code = "PLANNER_VALIDATION_FAILED"
            status = Phase16V3DiagnosticOutcomeStatus.FAILED
            verdict = Phase16V3DiagnosticValidationVerdict.FAILED
        else:
            reason_code = "PLANNER_DIAGNOSTIC_PASS"
            status = Phase16V3DiagnosticOutcomeStatus.PASS
            verdict = Phase16V3DiagnosticValidationVerdict.PASS
        self._ledger.append_validation_fact(
            attempt_id=attempt.attempt_id,
            verdict=verdict,
            reason_code=reason_code,
        )
        self._ledger.close_case(
            case_id=projection.case_id,
            status=status,
            reason_code=reason_code,
        )
        return Phase16V3PlannerDiagnosticReport(status, reason_code, attempt.attempt_id)

    def _build_projection(self) -> Phase16OfficialSmokeV2CaseProjection:
        """只重建 V2 冻结的首个高冲突 case，且不让 label、split 或期望路由进入模型。"""

        projection = build_phase16_official_smoke_v2_case_projection(
            dataset=self._dataset,
            case_id=PHASE16_V3_PLANNER_DIAGNOSTIC_CASE_ID,
            now=self._clock(),
        )
        if projection.case_digest != self._parent_manifest.case_digests[projection.case_id]:
            raise ValueError("V3 diagnostic case digest conflicts with parent manifest")
        return projection

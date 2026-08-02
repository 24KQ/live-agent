"""Phase 16 V9 qualification 的独立、单发、完整 slot 测量 runner。

它不修改 V8 runner 或历史 Manifest；只消费 qualification-v2 的公开 development/validation
高冲突 case、V9 candidate Profile 和 phase16_qualification_* 执行账本。holdout 明文仍必须由
独立 release owner 通过另一个受控 loader 提供，本模块在未实现该 loader 前 fail-closed。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from hashlib import sha256
import json
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from src.decision_support.evidence import EvidenceBundleSnapshot, ProductInventoryPayload
from src.decision_support.store import derive_automatic_escalation_codes
from src.decision_support.multi_agent import (
    validate_v2_conflict_analysis_result,
    validate_v2_live_decision_planner_result,
)
from src.decision_support.multi_agent_evaluation import _assemble_bundle
from src.decision_support.official_smoke_runner_v2 import (
    _opaque_case_key,
    _projection_evidence_registry,
    _synthetic_live_parents,
)
from src.decision_support.phase16_qualification import (
    Phase16QualificationCase,
    Phase16QualificationCorpus,
    Phase16QualificationPolicy,
    QualificationCaseKind,
    QualificationSplit,
)
from src.decision_support.phase16_qualification_evaluator import (
    CandidateProfileBundle,
    QualificationCampaignAdmission,
    admit_qualification_campaign,
)
from src.decision_support.phase16_qualification_execution_ledger import (
    PostgresPhase16QualificationExecutionLedger,
    QualificationExecutionCaseStatus,
    QualificationExecutionSlot,
    QualificationExecutionStage,
    QualificationExecutionValidationVerdict,
)
from src.decision_support.phase16_qualification_ledger import (
    QualificationCampaign,
    QualificationCampaignKind,
    QualificationMetricFact,
    QualificationRunStatus,
    build_qualification_metric,
)
from src.specialist_runtime.budget import BudgetInvariantError
from src.specialist_runtime.model_port import AgentModelPort, ModelFailure, ModelSuccess
from src.specialist_runtime.models import (
    AgentResult,
    AgentTask,
    SpecialistTaskKind,
    _plain_json,
    canonical_json_sha256,
)
from src.specialist_runtime.registry import SpecialistOrchestrator, SpecialistProfileRegistry
from src.specialist_runtime.runner import BoundedSpecialistRunner


@dataclass(frozen=True)
class QualificationProjection:
    """仅包含共享 Runner 所需的受治理输入；split/label 永远不进入模型任务。"""

    case_id: str
    case_digest: str
    analyst_task: AgentTask
    evidence_refs: tuple[Any, ...]
    evidence_registry: Any
    trusted_anchor_id: str
    trigger_codes: tuple[Any, ...]
    available_backup_product_ids: frozenset[str]
    proposal_eligible: bool
    evidence_bundle_digest: str


@dataclass(frozen=True)
class QualificationStageExecution:
    passed: bool
    network_sent: bool
    reason_code: str
    analysis: Any | None = None
    # V9 矩阵配置：该 stage 的 receipt 是否与 campaign 声明组合一致
    # （model_id == declared_model_id 且 endpoint_host ∈ declared_endpoint_hosts）。
    # 仅成功 stage 有 receipt 可核对；失败/阻断 stage 恒为 False。
    identity_matched: bool = False
    attempt_created: bool = False


@dataclass(frozen=True)
class QualificationCampaignExecutionReport:
    campaign_id: str
    run_id: str
    status: str
    reason_codes: tuple[str, ...]
    model_calls: int
    metric_facts: tuple[QualificationMetricFact, ...]


class _NoSkillPort:
    async def invoke(self, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("qualification runner does not permit Skills")


class _CapturingModelPort:
    def __init__(self, delegate: AgentModelPort) -> None:
        self._delegate = delegate
        self.request: Any | None = None
        self.outcome: ModelSuccess | ModelFailure | None = None

    async def complete(self, request: Any) -> ModelSuccess | ModelFailure:
        self.request = request
        self.outcome = await self._delegate.complete(request)
        return self.outcome


@dataclass(frozen=True)
class _BudgetClaim:
    created: bool


class _QualificationPricingPolicy:
    policy_digest = canonical_json_sha256(
        {"input": "3.000000", "output": "6.000000", "model": "deepseek-v4-pro"}
    )

    def count_input_tokens(self, request: Any) -> int:
        payload = json.dumps(
            request.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return max((len(payload) + 3) // 4, 1)

    def worst_case_cost(self, request: Any, profile: Any) -> Decimal:
        """预留金额 = 2.0x 估计输入 + 3x 最大输出，足以覆盖实际 API 计费。

        网关把 reasoning_content（思维链）计入 completion_tokens，可见输出虽小，
        计费却按含推理的原始 output 结算；预留按 3 倍输出裕量覆盖已验证的
        reasoning 膨胀（xhigh ~2.2x、max ~2.4x）。输入按 2.0 倍覆盖 V9 传输层
        重试（最多重试 1 次即双倍输入计费），避免 PRICE_RESERVATION_OVERRUN。
        """
        adjusted_input = int(self.count_input_tokens(request) * 2.0)
        cost = self._cost(adjusted_input, request.max_output_tokens * 3)
        if cost > profile.max_case_cost_cny:
            raise BudgetInvariantError("qualification request exceeds frozen stage reservation")
        return cost

    def actual_cost(self, usage: Any, _profile: Any) -> Decimal:
        return self._cost(usage.input_tokens, usage.output_tokens)

    @staticmethod
    def _cost(input_tokens: int, output_tokens: int) -> Decimal:
        raw = (
            Decimal(input_tokens) * Decimal("3.000000")
            + Decimal(output_tokens) * Decimal("6.000000")
        ) / Decimal("1000000")
        return raw.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)


class _QualificationBudgetAdapter:
    def __init__(
        self,
        *,
        ledger: PostgresPhase16QualificationExecutionLedger,
        run_id: str,
        claim_id: str,
        stage: QualificationExecutionStage,
        profile: Any,
        stage_reservation_cny: Decimal,
    ) -> None:
        self._ledger = ledger
        self._run_id = run_id
        self._claim_id = claim_id
        self._stage = stage
        self._profile = profile
        self._stage_reservation_cny = stage_reservation_cny
        self.attempt: Any | None = None
        self._request_id: str | None = None

    def reserve(self, request_id: str, candidate: object, amount_cny: Decimal) -> _BudgetClaim:
        if candidate != self._stage.value or self.attempt is not None:
            raise BudgetInvariantError("qualification dispatch candidate is invalid")
        if amount_cny > self._stage_reservation_cny:
            raise BudgetInvariantError("qualification dispatch reservation exceeds policy stage cap")
        self.attempt = self._ledger.begin_dispatch(
            run_id=self._run_id,
            claim_id=self._claim_id,
            stage=self._stage,
            profile_digest=self._profile.profile_digest,
            internal_request_id=request_id,
            reservation_cny=amount_cny,
        )
        self._request_id = request_id
        return _BudgetClaim(created=True)

    def settle(self, request_id: str, actual_cost_cny: Decimal | None) -> _BudgetClaim:
        _ = actual_cost_cny
        if self.attempt is None or request_id != self._request_id:
            raise BudgetInvariantError("qualification settlement has no matching attempt")
        return _BudgetClaim(created=False)

    def release(self, request_id: str) -> _BudgetClaim:
        if self.attempt is None or request_id != self._request_id:
            raise BudgetInvariantError("qualification release has no matching attempt")
        return _BudgetClaim(created=False)


def _build_projection(*, case: Phase16QualificationCase, now: datetime, analyst_profile: Any) -> QualificationProjection:
    """通过既有六角色 Assembler 重建高冲突受治理证据，不读取 label 或 split。"""

    workspace, incident = _synthetic_live_parents(case_id=case.case_id, now=now)
    bundle = _assemble_bundle(workspace=workspace, incident=incident, case=case, now=now)
    snapshot = EvidenceBundleSnapshot.model_validate(bundle.snapshot)
    trigger_codes = derive_automatic_escalation_codes(bundle)
    if len(trigger_codes) < 2:
        raise ValueError("qualification E2E case does not contain high-conflict triggers")
    inventory = next(
        component.payload
        for component in snapshot.components
        if component.role.value == "PRODUCT_INVENTORY_SNAPSHOT"
    )
    if not isinstance(inventory, ProductInventoryPayload):
        raise ValueError("qualification inventory evidence is invalid")
    references = tuple(component.reference for component in snapshot.components)
    key = _opaque_case_key(case.case_id)
    task = AgentTask(
        task_id=f"qualification-analyst-{key}",
        task_kind=SpecialistTaskKind.CONFLICT_ANALYSIS,
        profile_id=analyst_profile.profile_id,
        profile_version=analyst_profile.profile_version,
        room_id=snapshot.scope.room_id,
        trace_id=snapshot.scope.trace_id,
        objective="Analyze only governed sold-out conflict evidence for controlled qualification.",
        input_snapshot={
            "trigger_codes": [code.value for code in trigger_codes],
            "evidence_bundle_digest": snapshot.bundle_digest,
        },
        initial_evidence_refs=references,
    )
    return QualificationProjection(
        case_id=case.case_id,
        case_digest=canonical_json_sha256(case.model_dump(mode="json")),
        analyst_task=task,
        evidence_refs=references,
        evidence_registry=_projection_evidence_registry(snapshot),
        trusted_anchor_id=snapshot.scope.anchor_id,
        trigger_codes=tuple(trigger_codes),
        available_backup_product_ids=frozenset(
            product.product_id
            for product in inventory.backup_products
            if product.is_active and product.inventory > 0
        ),
        proposal_eligible=snapshot.proposal_eligible,
        evidence_bundle_digest=snapshot.bundle_digest,
    )


class Phase16QualificationCampaignRunner:
    """新的 qualification development/validation runner；V8 runner 不参与也不被改动。"""

    def __init__(
        self,
        *,
        policy: Phase16QualificationPolicy,
        corpus: Phase16QualificationCorpus,
        candidate_bundle: CandidateProfileBundle,
        ledger: PostgresPhase16QualificationExecutionLedger,
        model_port: AgentModelPort,
        clock: Any | None = None,
    ) -> None:
        self._policy = policy
        self._corpus = corpus
        self._candidate_bundle = candidate_bundle
        self._ledger = ledger
        self._model_port = model_port
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._pricing_policy = _QualificationPricingPolicy()

    async def execute(self, *, campaign: QualificationCampaign, run_id: str) -> QualificationCampaignExecutionReport:
        """执行一个公开 split；已发送语义失败继续测量，hard/pre-send 失败封闭余下 slot。"""

        admission = admit_qualification_campaign(
            policy=self._policy,
            corpus=self._corpus_identity(),
            candidate_bundle=self._candidate_bundle,
            campaign_kind=campaign.campaign_kind,
        )
        if not admission.allowed:
            return QualificationCampaignExecutionReport(
                campaign_id=campaign.campaign_id,
                run_id=run_id,
                status="BLOCKED",
                reason_codes=admission.reason_codes,
                model_calls=0,
                metric_facts=(),
            )
        cases = self._cases_for_campaign(campaign.campaign_kind)
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            return QualificationCampaignExecutionReport(
                campaign_id=campaign.campaign_id,
                run_id=run_id,
                status="BLOCKED",
                reason_codes=("CLOCK_TIMEZONE_REQUIRED",),
                model_calls=0,
                metric_facts=(),
            )
        projections = tuple(
            _build_projection(case=case, now=now, analyst_profile=self._candidate_bundle.analyst_profile)
            for case in cases
        )
        slots = tuple(
            QualificationExecutionSlot(item.case_id, item.case_digest, True) for item in projections
        )
        try:
            self._ledger.begin_run_with_slots(
                run_id=run_id,
                campaign_id=campaign.campaign_id,
                slots=slots,
            )
        except Exception:
            return QualificationCampaignExecutionReport(
                campaign_id=campaign.campaign_id,
                run_id=run_id,
                status="BLOCKED",
                reason_codes=("LEDGER_OR_SLOT_BLOCKED",),
                model_calls=0,
                metric_facts=(),
            )

        analyst_pass = 0
        planner_attempted = 0
        planner_pass = 0
        e2e_pass = 0
        # V9 矩阵配置：所有已产生 receipt 的 stage 均与 campaign 声明组合一致且 case 全过
        # 的 case 数；HARD_SAFETY_CONFORMANCE 以此为分子（不再是无信息的总/总恒等事实）。
        identity_pass = 0
        model_calls = 0
        reasons: list[str] = []
        hard_blocked = False
        for projection in projections:
            claim = self._ledger.claim_case(
                run_id=run_id,
                case_id=projection.case_id,
                case_digest=projection.case_digest,
            )
            if hard_blocked:
                self._ledger.record_pre_dispatch_block(claim_id=claim.claim_id, reason_code="CAMPAIGN_HARD_BLOCKED")
                self._ledger.close_case(
                    claim_id=claim.claim_id,
                    status=QualificationExecutionCaseStatus.BLOCKED,
                    reason_code="CAMPAIGN_HARD_BLOCKED",
                )
                continue
            analyst = await self._execute_stage(
                campaign=campaign,
                run_id=run_id,
                claim_id=claim.claim_id,
                projection=projection,
                stage=QualificationExecutionStage.ANALYST,
                task=projection.analyst_task,
            )
            model_calls += int(analyst.network_sent)
            if not analyst.passed or analyst.analysis is None:
                self._close_failed_stage_case(claim_id=claim.claim_id, execution=analyst)
                reasons.append(analyst.reason_code)
                hard_blocked = not analyst.network_sent
                continue
            analyst_pass += 1
            planner_attempted += 1
            case_identity = analyst.identity_matched
            planner_task = self._planner_task(projection, analyst.analysis)
            planner = await self._execute_stage(
                campaign=campaign,
                run_id=run_id,
                claim_id=claim.claim_id,
                projection=projection,
                stage=QualificationExecutionStage.PLANNER,
                task=planner_task,
                analysis=analyst.analysis,
            )
            model_calls += int(planner.network_sent)
            if not planner.passed:
                self._close_failed_stage_case(claim_id=claim.claim_id, execution=planner)
                reasons.append(planner.reason_code)
                hard_blocked = not planner.network_sent
                continue
            planner_pass += 1
            e2e_pass += 1
            if case_identity and planner.identity_matched:
                identity_pass += 1
            self._ledger.close_case(
                claim_id=claim.claim_id,
                status=QualificationExecutionCaseStatus.PASS,
                reason_code="MULTI_AGENT_READY",
            )
        metrics = self._metrics(
            run_id=run_id,
            analyst_pass=analyst_pass,
            planner_attempted=planner_attempted,
            planner_pass=planner_pass,
            e2e_pass=e2e_pass,
            identity_pass=identity_pass,
            total=len(projections),
        )
        try:
            for metric in metrics:
                self._ledger.append_metric(metric)
            terminal_status = (
                QualificationRunStatus.PASS if not reasons and not hard_blocked
                else QualificationRunStatus.FAILED
            )
            self._ledger.close_run(
                run_id=run_id,
                status=terminal_status,
                reason_code=(
                    "QUALIFICATION_EXECUTION_COMPLETE" if terminal_status is QualificationRunStatus.PASS
                    else "QUALIFICATION_EXECUTION_FAILED"
                ),
                evaluation_digest=canonical_json_sha256({
                    "campaign_id": campaign.campaign_id,
                    "run_id": run_id,
                    "metrics": [m.model_dump(mode="json") for m in metrics],
                }),
            )
        except Exception:
            reasons.append("LEDGER_TERMINALIZATION_FAILED")
        status = "BLOCKED" if hard_blocked else "FAILED" if reasons else "PASS"
        return QualificationCampaignExecutionReport(
            campaign_id=campaign.campaign_id,
            run_id=run_id,
            status=status,
            reason_codes=tuple(sorted(set(reasons))) if reasons else ("EXECUTION_COMPLETE",),
            model_calls=model_calls,
            metric_facts=metrics,
        )

    def _corpus_identity(self):
        from src.decision_support.phase16_qualification_ledger import corpus_identity_from_manifest

        return corpus_identity_from_manifest(self._corpus.manifest)

    def _cases_for_campaign(self, kind: QualificationCampaignKind) -> tuple[Phase16QualificationCase, ...]:
        if kind is QualificationCampaignKind.DEVELOPMENT:
            cases = self._corpus.development_cases
        elif kind is QualificationCampaignKind.VALIDATION:
            cases = self._corpus.validation_cases
        else:
            raise ValueError("released holdout requires the independent sealed corpus loader")
        high_conflict = tuple(case for case in cases if case.kind is QualificationCaseKind.HIGH_CONFLICT_PAIRED)
        expected = (
            self._policy.validation_high_conflict_case_count
            if kind is QualificationCampaignKind.VALIDATION
            else self._policy.validation_high_conflict_case_count
        )
        if len(high_conflict) != expected or any(case.split not in {QualificationSplit.DEVELOPMENT, QualificationSplit.VALIDATION} for case in high_conflict):
            raise ValueError("qualification public E2E slots do not match frozen policy")
        return high_conflict

    def _planner_task(self, projection: QualificationProjection, analysis: Any) -> AgentTask:
        key = sha256(projection.case_id.encode("utf-8")).hexdigest()[:24]
        profile = self._candidate_bundle.planner_profile
        return AgentTask(
            task_id=f"qualification-planner-{key}",
            task_kind=SpecialistTaskKind.LIVE_DECISION_PLANNING,
            profile_id=profile.profile_id,
            profile_version=profile.profile_version,
            room_id=projection.analyst_task.room_id,
            trace_id=projection.analyst_task.trace_id,
            objective="Generate bounded options for controlled human-review qualification.",
            input_snapshot={
                "analysis": analysis.as_model_input(),
                "evidence_bundle_digest": projection.evidence_bundle_digest,
            },
            initial_evidence_refs=projection.evidence_refs,
        )

    async def _execute_stage(
        self,
        *,
        campaign: QualificationCampaign,
        run_id: str,
        claim_id: str,
        projection: QualificationProjection,
        stage: QualificationExecutionStage,
        task: AgentTask,
        analysis: Any | None = None,
    ) -> QualificationStageExecution:
        profile = (
            self._candidate_bundle.analyst_profile
            if stage is QualificationExecutionStage.ANALYST
            else self._candidate_bundle.planner_profile
        )
        capture = _CapturingModelPort(self._model_port)
        budget = _QualificationBudgetAdapter(
            ledger=self._ledger,
            run_id=run_id,
            claim_id=claim_id,
            stage=stage,
            profile=profile,
            stage_reservation_cny=self._policy.stage_reservation_cny,
        )
        bounded = BoundedSpecialistRunner(
            orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry((profile,))),
            model_port=capture,
            budget_store=budget,
            evidence_registry=projection.evidence_registry,
            skill_port=_NoSkillPort(),
            skill_catalog=(),
            trusted_anchor_resolver=lambda _task: projection.trusted_anchor_id,
            pricing_policy=self._pricing_policy,
            budget_candidate_resolver=lambda _task: stage.value,
            request_id_factory=lambda _task, _execution_id, _index: str(
                uuid5(
                    NAMESPACE_URL,
                    f"{campaign.manifest_digest}:{run_id}:{projection.case_id}:{stage.value}",
                )
            ),
            clock=self._clock,
        )
        try:
            result = await bounded.run(task)
        except Exception as exc:
            print(f"  [BOUNDED RUNNER EXCEPTION] {type(exc).__name__}: {exc}")
            result = None
        attempt = budget.attempt
        if attempt is None:
            diag = f"stage={stage.value}"
            if result is not None and hasattr(result, 'status') and hasattr(result, 'failure'):
                diag += f" result_status={result.status.value} failure_code={result.failure.code if result.failure else 'NONE'}"
            print(f"  [BUDGET PRE-SEND BLOCKED] {diag}")
            return QualificationStageExecution(False, False, "RUNNER_PRE_SEND_BLOCKED")
        if capture.request is None or (
            isinstance(capture.outcome, ModelFailure) and not capture.outcome.request_sent
        ):
            self._append_validation(
                attempt_id=attempt.attempt_id,
                stage=stage,
                verdict=QualificationExecutionValidationVerdict.BLOCKED,
                reason_code="MODEL_REQUEST_NOT_SENT",
                result=None,
            )
            return QualificationStageExecution(
                False, False, "MODEL_REQUEST_NOT_SENT", attempt_created=True
            )
        if not isinstance(capture.outcome, ModelSuccess):
            # 记录详细失败信息（不改变 reason code 分类；纯诊断用途）
            outcome = capture.outcome
            if outcome is None:
                diag = f"capture.outcome is None (bounded runner threw after dispatch)"
                print(f"  [MODEL FAILURE] {diag}")
            else:
                diag = f"category={outcome.category.value}"
                if outcome.http_status is not None:
                    diag += f" http_status={outcome.http_status}"
                diag += f" latency_ms={outcome.latency_ms}"
                print(f"  [MODEL FAILURE] {diag}")
            self._append_validation(
                attempt_id=attempt.attempt_id,
                stage=stage,
                verdict=QualificationExecutionValidationVerdict.FAILED,
                reason_code="MODEL_OUTCOME_UNAVAILABLE",
                result=None,
            )
            return QualificationStageExecution(
                False, True, "MODEL_OUTCOME_UNAVAILABLE", attempt_created=True
            )
        if not self._ledger.append_receipt(
            attempt_id=attempt.attempt_id,
            success=capture.outcome,
            reasoning_effort=campaign.declared_reasoning_effort,
        ):
            self._append_validation(
                attempt_id=attempt.attempt_id,
                stage=stage,
                verdict=QualificationExecutionValidationVerdict.FAILED,
                reason_code="PROVIDER_RECEIPT_INVALID",
                result=None,
            )
            return QualificationStageExecution(
                False, True, "PROVIDER_RECEIPT_INVALID", attempt_created=True
            )
        try:
            if not isinstance(result, AgentResult):
                raise ValueError("shared runner did not return AgentResult")
            if stage is QualificationExecutionStage.ANALYST:
                validated = validate_v2_conflict_analysis_result(
                    task=task,
                    result=result,
                    expected_profile=profile,
                    expected_evidence_refs=projection.evidence_refs,
                    expected_finding_codes=projection.trigger_codes,
                )
                self._append_validation(
                    attempt_id=attempt.attempt_id,
                    stage=stage,
                    verdict=QualificationExecutionValidationVerdict.PASS,
                    reason_code="ANALYST_VALIDATION_PASS",
                    result=result,
                )
                return QualificationStageExecution(
                    passed=True,
                    network_sent=True,
                    reason_code="ANALYST_VALIDATION_PASS",
                    analysis=validated,
                    identity_matched=(
                        capture.outcome.model_id == campaign.declared_model_id
                        and capture.outcome.endpoint_host in campaign.declared_endpoint_hosts
                    ),
                    attempt_created=True,
                )
            if analysis is None:
                raise ValueError("planner requires validated analyst analysis")
            validate_v2_live_decision_planner_result(
                task=task,
                result=result,
                expected_profile=profile,
                expected_evidence_refs=projection.evidence_refs,
                required_risk_codes=frozenset(item.value for item in analysis.risk_codes),
                available_backup_product_ids=projection.available_backup_product_ids,
                proposal_eligible_and_fresh=projection.proposal_eligible,
            )
            self._append_validation(
                attempt_id=attempt.attempt_id,
                stage=stage,
                verdict=QualificationExecutionValidationVerdict.PASS,
                reason_code="PLANNER_VALIDATION_PASS",
                result=result,
            )
            return QualificationStageExecution(
                passed=True,
                network_sent=True,
                reason_code="PLANNER_VALIDATION_PASS",
                identity_matched=(
                    capture.outcome.model_id == campaign.declared_model_id
                    and capture.outcome.endpoint_host in campaign.declared_endpoint_hosts
                ),
                attempt_created=True,
            )
        except Exception:
            reason = "ANALYST_VALIDATION_FAILED" if stage is QualificationExecutionStage.ANALYST else "PLANNER_VALIDATION_FAILED"
            self._append_validation(
                attempt_id=attempt.attempt_id,
                stage=stage,
                verdict=QualificationExecutionValidationVerdict.FAILED,
                reason_code=reason,
                result=None,
            )
            return QualificationStageExecution(False, True, reason, attempt_created=True)

    def _append_validation(
        self,
        *,
        attempt_id: str,
        stage: QualificationExecutionStage,
        verdict: QualificationExecutionValidationVerdict,
        reason_code: str,
        result: AgentResult | None,
    ) -> None:
        payload: dict[str, Any] = {
            "attempt_id": attempt_id,
            "stage": stage.value,
            "verdict": verdict.value,
            "reason_code": reason_code,
        }
        if result is not None:
            payload.update(
                {
                    "task_id": result.task_id,
                    "profile_id": result.profile_id,
                    "output_digest": canonical_json_sha256(_plain_json(result.output)),
                    "evidence_ids": sorted(item.evidence_id for item in result.evidence_refs),
                }
            )
        self._ledger.append_validation(
            attempt_id=attempt_id,
            verdict=verdict,
            reason_code=reason_code,
            validation_digest=canonical_json_sha256(payload),
        )

    def _close_failed_stage_case(self, *, claim_id: str, execution: QualificationStageExecution) -> None:
        if not execution.network_sent:
            self._ledger.record_pre_dispatch_block(claim_id=claim_id, reason_code=execution.reason_code)
            self._ledger.close_case(
                claim_id=claim_id,
                status=QualificationExecutionCaseStatus.BLOCKED,
                reason_code=execution.reason_code,
            )
            return
        self._ledger.close_case(
            claim_id=claim_id,
            status=QualificationExecutionCaseStatus.FAILED,
            reason_code=execution.reason_code,
        )

    @staticmethod
    def _metrics(
        *,
        run_id: str,
        analyst_pass: int,
        planner_attempted: int,
        planner_pass: int,
        e2e_pass: int,
        identity_pass: int,
        total: int,
    ) -> tuple[QualificationMetricFact, ...]:
        return (
            build_qualification_metric(
                run_id=run_id,
                metric_code="E2E_MULTI_AGENT_READY",
                numerator=e2e_pass,
                denominator=total,
            ),
            build_qualification_metric(
                run_id=run_id,
                metric_code="ANALYST_SCHEMA_AND_SEMANTIC_VALID",
                numerator=analyst_pass,
                denominator=total,
            ),
            build_qualification_metric(
                run_id=run_id,
                metric_code="PLANNER_RISK_COVERAGE",
                numerator=planner_pass,
                denominator=max(planner_attempted, 1),
            ),
            build_qualification_metric(
                run_id=run_id,
                metric_code="EXPLANATION_BOUND",
                numerator=analyst_pass,
                denominator=total,
            ),
            build_qualification_metric(
                run_id=run_id,
                metric_code="CONTROLLED_EVIDENCE_BINDING",
                numerator=e2e_pass,
                denominator=total,
            ),
            build_qualification_metric(
                run_id=run_id,
                metric_code="HARD_SAFETY_CONFORMANCE",
                # V9 矩阵配置：分子为"全部已产生 receipt 与 campaign 声明组合一致且 case
                # 全过"的 case 数；任何失败或身份漂移都会让该事实低于 1.0，不再是恒等事实。
                numerator=identity_pass,
                denominator=total,
            ),
            build_qualification_metric(
                run_id=run_id,
                metric_code="OPTION_VALIDITY",
                numerator=planner_pass,
                denominator=max(planner_attempted, 1),
            ),
        )

"""Phase 17 holdout 执行器：contract 身份 → adapter → phase17 ledger 端到端。

与 v2 runner 的差异是刻意的：
- v2 runner 由 v2 policy 驱动并写入 v2 execution ledger；phase17 runner 由
  已批准的 Phase 17 契约驱动并写入独立 phase17 表族（预算池隔离）。
- 执行协议与 v2 保持一致：每 case 走 Analyst → Planner 双阶段，模型调用
  通过注入的 ``AgentModelPort``（真实路径为
  ``DeepSeekV5ControlledE2EAdapter``，受控渠道链 + JSON mode + 90s/尝试）；
  结构判定与 v2 相同：analyst 需产出 trigger codes，planner 需产出 risk
  coverage 与 proposal。

身份断言（构造时 fail-closed）：
- contract 已准入（``admit_phase17_holdout_execution``）；
- candidate bundle 的 model_id / endpoint_host 必须与
  ``contract.identity_requirements`` 精确一致（codex 十六轮 P0 身份固定）。

预算纪律：
- campaign 建立时在 contract 行锁内预留 reservation_cny；
- usage 未知（UNKNOWN_USAGE）按最坏情况以 reservation 全额入账，不得结算为 0；
- run 结束后按实际成本结算，成本公式与 v2 完全一致（3 CNY / M input、
  6 CNY / M output），保证 phase17 结果可与历史账本对账。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid5

from src.decision_support.phase16_qualification import (
    QualificationExecutionContract,
    admit_phase17_holdout_execution,
)
from src.decision_support.phase17_holdout_dataset import (
    Phase17HoldoutDatasetManifest,
    Phase17DatasetIdentityError,
    validate_phase17_holdout_case,
)
from src.specialist_runtime.model_port import (
    AgentModelPort,
    ModelFailure,
    ModelMessage,
    ModelRequest,
    ModelSuccess,
)
from src.specialist_runtime.models import _plain_json


class Phase17HoldoutExecutionError(ValueError):
    """Phase 17 执行前置失败；任何失败都在模型调用前 fail-closed。"""


@dataclass(frozen=True)
class Phase17HoldoutCaseExecution:
    case_id: str
    outcome: str  # PASS / FAILED / BLOCKED
    reason_code: str
    cost_cny: Decimal
    receipt_count: int


@dataclass(frozen=True)
class Phase17HoldoutRunReport:
    campaign_id: str
    run_id: str
    status: str  # PASS / FAILED / BLOCKED
    reason_codes: tuple[str, ...]
    pass_count: int
    pass_min: int
    total: int
    cost_cny: Decimal
    case_executions: tuple[Phase17HoldoutCaseExecution, ...]


@dataclass(frozen=True)
class Phase17HoldoutAggregateReport:
    """两个 holdout batch 的 27/30 聚合（codex 第十七轮 P0-1）。"""

    status: str  # PASS / FAILED / BLOCKED
    reason_codes: tuple[str, ...]
    total_pass: int
    total_cases: int
    pass_min_total: int
    batch_statuses: tuple[str, ...]
    cost_cny: Decimal


def aggregate_phase17_holdout_reports(
    *,
    reports: tuple[Phase17HoldoutRunReport, ...],
    contract: Any,
) -> Phase17HoldoutAggregateReport:
    """27/30 聚合判定：两批各自达标（PASS）且总 pass >= 27 才宣告 QUALIFIED。

    BLOCKED 是外部证据不足（inconclusive）：任一 batch BLOCKED 即聚合 BLOCKED，
    不进入 PASS/FAILED 判定（与 run 级终态语义一致，不重跑不刷分）。
    """

    batch_statuses = tuple(report.status for report in reports)
    total_pass = sum(report.pass_count for report in reports)
    total_cases = sum(report.total for report in reports)
    total_cost = sum((report.cost_cny for report in reports), Decimal("0"))
    if any(status == "BLOCKED" for status in batch_statuses):
        return Phase17HoldoutAggregateReport(
            status="BLOCKED",
            reason_codes=("PHASE17_HOLDOUT_AGGREGATE_BLOCKED",),
            total_pass=total_pass,
            total_cases=total_cases,
            pass_min_total=contract.holdout_total_e2e_pass_min,
            batch_statuses=batch_statuses,
            cost_cny=total_cost,
        )
    if all(status == "PASS" for status in batch_statuses) and total_pass >= contract.holdout_total_e2e_pass_min:
        return Phase17HoldoutAggregateReport(
            status="PASS",
            reason_codes=("PHASE17_HOLDOUT_QUALIFIED_90PCT_PORTFOLIO_THRESHOLD",),
            total_pass=total_pass,
            total_cases=total_cases,
            pass_min_total=contract.holdout_total_e2e_pass_min,
            batch_statuses=batch_statuses,
            cost_cny=total_cost,
        )
    return Phase17HoldoutAggregateReport(
        status="FAILED",
        reason_codes=("PHASE17_HOLDOUT_AGGREGATE_THRESHOLD_NOT_MET",),
        total_pass=total_pass,
        total_cases=total_cases,
        pass_min_total=contract.holdout_total_e2e_pass_min,
        batch_statuses=batch_statuses,
        cost_cny=total_cost,
    )


class Phase17HoldoutCampaignRunner:
    """contract 身份驱动的 phase17 holdout 执行器。"""

    def __init__(
        self,
        *,
        contract: Any,
        ledger: Any,
        candidate_bundle: Any,
        model_port: AgentModelPort,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        admission = admit_phase17_holdout_execution(
            requested_identity=QualificationExecutionContract.PHASE17_HOLDOUT_EXECUTION_V1,
            contract=contract,
        )
        if not admission[0]:
            raise Phase17HoldoutExecutionError(
                f"phase17 contract admission failed: {','.join(admission[1])}"
            )
        identity = contract.identity_requirements
        # codex 第十七轮 P0-2：candidate 身份必须与契约冻结身份全链一致
        # （policy digest 绑定契约；model/endpoint 绑定身份），任一漂移 fail-closed。
        candidate = candidate_bundle.candidate
        if candidate.policy_digest != contract.contract_digest:
            raise Phase17HoldoutExecutionError(
                "phase17 candidate policy identity does not match the contract"
            )
        if candidate.model_id != identity["model_id"]:
            raise Phase17HoldoutExecutionError(
                "phase17 candidate model identity does not match the frozen contract"
            )
        if candidate.endpoint_host not in identity["endpoint_hosts"]:
            raise Phase17HoldoutExecutionError(
                "phase17 candidate endpoint identity does not match the frozen contract"
            )
        for stage_profile in (
            candidate_bundle.analyst_profile,
            candidate_bundle.planner_profile,
        ):
            if stage_profile.model_id != identity["model_id"]:
                raise Phase17HoldoutExecutionError(
                    "phase17 candidate model identity does not match the frozen contract"
                )
            if stage_profile.endpoint_host not in identity["endpoint_hosts"]:
                raise Phase17HoldoutExecutionError(
                    "phase17 candidate endpoint identity does not match the frozen contract"
                )
            if stage_profile.temperature != Decimal("0"):
                raise Phase17HoldoutExecutionError(
                    "phase17 candidate temperature must be zero"
                )
            if stage_profile.deadline_seconds != identity["per_attempt_deadline_seconds"]:
                raise Phase17HoldoutExecutionError(
                    "phase17 candidate deadline does not match the frozen contract"
                )
        self._contract = contract
        self._identity = identity
        self._candidate = candidate
        self._stage_reservation_cny = contract.stage_reservation_cny
        self._ledger = ledger
        self._candidate_bundle = candidate_bundle
        self._model_port = model_port
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @staticmethod
    def _input_digest(input_text: str) -> str:
        return hashlib.sha256(input_text.encode("utf-8")).hexdigest()

    @staticmethod
    def _prompt_digest(prompt_text: str) -> str:
        return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()

    def _cost(self, usage: Any | None, reservation_cny: Decimal) -> Decimal:
        """实际成本：usage 已知按 v2 相同定价；UNKNOWN_USAGE 按最坏情况全额占用。"""

        if usage is None:
            return reservation_cny
        raw = (
            Decimal(usage.input_tokens) * Decimal("3.000000")
            + Decimal(usage.output_tokens) * Decimal("6.000000")
        ) / Decimal("1000000")
        return raw.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)

    async def execute(
        self,
        *,
        campaign: Any,
        run_id: str,
        batch_index: int,
        cases: tuple[tuple[str, str], ...],
        manifest: Phase17HoldoutDatasetManifest,
    ) -> Phase17HoldoutRunReport:
        """执行一个 holdout batch；每 case 先过 manifest membership 校验再联网。"""

        # 0. campaign/候选/数据集全链身份绑定（codex 第十七轮 P0-2）：
        #    campaign 声称的 contract、dataset、candidate 身份必须与实参精确一致。
        if campaign.contract_digest != self._contract.contract_digest:
            raise Phase17HoldoutExecutionError(
                "phase17 campaign contract identity does not match the contract"
            )
        if campaign.dataset_manifest_digest != manifest.manifest_digest:
            raise Phase17HoldoutExecutionError(
                "phase17 campaign dataset identity does not match the manifest"
            )
        if campaign.candidate_digest != self._candidate.candidate_digest:
            raise Phase17HoldoutExecutionError(
                "phase17 campaign candidate identity does not match the candidate bundle"
            )

        # 1. 精确 batch 集合校验（codex 第十七轮 P0-1）：cases 必须是该 batch 在
        #    frozen manifest 中的精确全集（order-insensitive），不允许子集/混合。
        if batch_index not in {1, 2}:
            raise Phase17HoldoutExecutionError("phase17 holdout batch_index must be 1 or 2")
        expected_case_ids = set(manifest.batch_case_ids(batch_index))
        actual_case_ids = {case_id for case_id, _ in cases}
        if actual_case_ids != expected_case_ids:
            raise Phase17HoldoutExecutionError(
                "phase17 holdout batch must be the exact frozen case set "
                f"(batch {batch_index} expected {len(expected_case_ids)} cases, "
                f"got {len(actual_case_ids)}; missing={sorted(expected_case_ids - actual_case_ids)[:3]} "
                f"extra={sorted(actual_case_ids - expected_case_ids)[:3]})"
            )

        # 2. 全部 case 过 frozen manifest membership（零模型调用）。
        for case_id, input_text in cases:
            try:
                validate_phase17_holdout_case(
                    case_id=case_id,
                    input_digest=self._input_digest(input_text),
                    batch_index=batch_index,
                    manifest=manifest,
                )
            except Phase17DatasetIdentityError as exc:
                raise Phase17HoldoutExecutionError(str(exc)) from exc

        # 3. contract 注册 + 预算池 reservation（contract 行锁内）。
        self._ledger.ensure_phase17_contract(self._contract)
        self._ledger.ensure_phase17_campaign(campaign)

        # 4. run slot 集合一次性冻结。
        self._ledger.begin_phase17_run(
            run_id=run_id,
            campaign_id=campaign.campaign_id,
            case_ids=tuple(case_id for case_id, _ in cases),
        )

        # 5. 逐 case 双阶段执行。
        executions: list[Phase17HoldoutCaseExecution] = []
        total_cost = Decimal("0")
        for case_id, input_text in cases:
            execution = await self._run_case(
                campaign=campaign,
                run_id=run_id,
                case_id=case_id,
                input_text=input_text,
            )
            self._ledger.record_phase17_case_result(
                run_id=run_id,
                case_id=case_id,
                input_digest=self._input_digest(input_text),
                outcome=execution.outcome,
                reason_code=execution.reason_code,
                receipt_count=execution.receipt_count,
                cost_cny=execution.cost_cny,
            )
            executions.append(execution)
            total_cost += execution.cost_cny

        # 6. run 终态（run_results 唯一一行）→ 按实际成本结算。
        #    阈值语义（codex 第十七轮 P0-1）：batch PASS 需要 pass_count >= 契约
        #    冻结的批内阈值（9/10、18/20），不再要求"全部 PASS"；BLOCKED 优先
        #    （inconclusive，不参与达标判定）。
        pass_count = sum(1 for e in executions if e.outcome == "PASS")
        reasons = tuple(
            sorted({e.reason_code for e in executions if e.outcome != "PASS"})
        )
        batch_pass_min = next(
            batch["pass_min"]
            for batch in self._contract.holdout_batches
            if batch["batch_index"] == batch_index
        )
        if any(e.outcome == "BLOCKED" for e in executions):
            status = "BLOCKED"
            reason_code = "PHASE17_HOLDOUT_HARD_BLOCKED"
        elif pass_count >= batch_pass_min:
            status = "PASS"
            reason_code = "PHASE17_HOLDOUT_BATCH_THRESHOLD_MET"
        else:
            status = "FAILED"
            reason_code = "PHASE17_HOLDOUT_THRESHOLD_NOT_MET"
        self._ledger.close_phase17_run(
            run_id=run_id,
            status=status,
            reason_code=reason_code,
            payload={
                "campaign_id": campaign.campaign_id,
                "run_id": run_id,
                "manifest_digest": manifest.manifest_digest,
                "pass_count": pass_count,
                "pass_min": batch_pass_min,
                "total": len(cases),
                "case_executions": [
                    {"case_id": e.case_id, "outcome": e.outcome, "reason_code": e.reason_code}
                    for e in executions
                ],
            },
        )
        self._ledger.settle_phase17_campaign(
            campaign_id=campaign.campaign_id,
            actual_cny=total_cost if total_cost > 0 else campaign.reservation_cny,
        )
        return Phase17HoldoutRunReport(
            campaign_id=campaign.campaign_id,
            run_id=run_id,
            status=status,
            reason_codes=reasons or ("EXECUTION_COMPLETE",),
            pass_count=pass_count,
            pass_min=batch_pass_min,
            total=len(cases),
            cost_cny=total_cost,
            case_executions=tuple(executions),
        )

    async def _run_case(
        self,
        *,
        campaign: Any,
        run_id: str,
        case_id: str,
        input_text: str,
    ) -> Phase17HoldoutCaseExecution:
        """Analyst → Planner 双阶段；语义失败继续测量，hard/pre-send 失败阻断。"""

        analyst = await self._dispatch_stage(
            campaign=campaign,
            run_id=run_id,
            case_id=case_id,
            stage="ANALYST",
            profile=self._candidate_bundle.analyst_profile,
            user_prompt=input_text,
        )
        if not analyst.passed:
            return Phase17HoldoutCaseExecution(
                case_id=case_id,
                outcome="BLOCKED" if not analyst.network_sent else "FAILED",
                reason_code=analyst.reason_code,
                cost_cny=analyst.cost_cny,
                receipt_count=analyst.receipt_count,
            )
        planner = await self._dispatch_stage(
            campaign=campaign,
            run_id=run_id,
            case_id=case_id,
            stage="PLANNER",
            profile=self._candidate_bundle.planner_profile,
            user_prompt=json.dumps(
                {"case_id": case_id, "analysis": _plain_json(analyst.output)},
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        if not planner.passed:
            return Phase17HoldoutCaseExecution(
                case_id=case_id,
                outcome="BLOCKED" if not planner.network_sent else "FAILED",
                reason_code=planner.reason_code,
                cost_cny=analyst.cost_cny + planner.cost_cny,
                receipt_count=analyst.receipt_count + planner.receipt_count,
            )
        return Phase17HoldoutCaseExecution(
            case_id=case_id,
            outcome="PASS",
            reason_code="MULTI_AGENT_READY",
            cost_cny=analyst.cost_cny + planner.cost_cny,
            receipt_count=analyst.receipt_count + planner.receipt_count,
        )

    async def _dispatch_stage(
        self,
        *,
        campaign: Any,
        run_id: str,
        case_id: str,
        stage: str,
        profile: Any,
        user_prompt: str,
    ) -> "_StageOutcome":
        request = self._build_request(
            campaign=campaign,
            run_id=run_id,
            case_id=case_id,
            stage=stage,
            profile=profile,
            user_prompt=user_prompt,
        )
        outcome = await self._model_port.complete(request)
        if isinstance(outcome, ModelFailure):
            # UNKNOWN_USAGE 按最坏情况以 stage 级预留全额入账（与 v2 attempt
            # reservation 口径一致）：attempt 一旦建立即占用，pre-send 失败
            # 也不得结算为 0；绝不使用 campaign 级预留，避免多 case 失败
            # 重复全额占用预算池。
            cost_cny = self._cost(None, self._stage_reservation_cny)
            # codex 第十七轮 P0-3：逐 attempt 证据入账（含 receipt_hmac）。
            self._ledger.record_phase17_attempt(
                run_id=run_id,
                case_id=case_id,
                stage=stage,
                attempt_index=1,
                request_id=request.request_id,
                endpoint_host=request.endpoint_host,
                model_id=request.model_id,
                outcome="FAILED",
                category=str(outcome.category),
                response_digest=outcome.response_digest,
                provider_response_id=None,
                http_status=outcome.http_status,
                latency_ms=outcome.latency_ms,
                attempts=outcome.attempts,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                cost_cny=cost_cny,
            )
            return _StageOutcome(
                passed=False,
                network_sent=bool(outcome.request_sent),
                reason_code="MODEL_OUTCOME_UNAVAILABLE",
                output=None,
                cost_cny=cost_cny,
                receipt_count=1,
            )
        if not isinstance(outcome, ModelSuccess):
            return _StageOutcome(
                passed=False,
                network_sent=False,
                reason_code="MODEL_OUTCOME_UNAVAILABLE",
                output=None,
                cost_cny=Decimal("0"),
                receipt_count=0,
            )
        cost = self._cost(outcome.usage, campaign.reservation_cny)
        valid = self._structure_valid(stage=stage, output=outcome.output)
        # codex 第十七轮 P0-3：成功（含语义失败）同样逐 attempt 入账。
        self._ledger.record_phase17_attempt(
            run_id=run_id,
            case_id=case_id,
            stage=stage,
            attempt_index=1,
            request_id=request.request_id,
            endpoint_host=request.endpoint_host,
            model_id=request.model_id,
            outcome="PASS" if valid else "FAILED",
            category=(
                None
                if valid
                else (
                    "ANALYST_VALIDATION_FAILED"
                    if stage == "ANALYST"
                    else "PLANNER_VALIDATION_FAILED"
                )
            ),
            response_digest=outcome.response_digest,
            provider_response_id=outcome.provider_response_id,
            http_status=None,
            latency_ms=outcome.latency_ms,
            attempts=outcome.attempts,
            input_tokens=outcome.usage.input_tokens if outcome.usage else None,
            output_tokens=outcome.usage.output_tokens if outcome.usage else None,
            total_tokens=outcome.usage.total_tokens if outcome.usage else None,
            cost_cny=cost,
        )
        if not valid:
            return _StageOutcome(
                passed=False,
                network_sent=True,
                reason_code=(
                    "ANALYST_VALIDATION_FAILED"
                    if stage == "ANALYST"
                    else "PLANNER_VALIDATION_FAILED"
                ),
                output=None,
                cost_cny=cost,
                receipt_count=1,
            )
        return _StageOutcome(
            passed=True,
            network_sent=True,
            reason_code="ANALYST_VALIDATION_PASS" if stage == "ANALYST" else "PLANNER_VALIDATION_PASS",
            output=outcome.output,
            cost_cny=cost,
            receipt_count=1,
        )

    def _build_request(
        self,
        *,
        campaign: Any,
        run_id: str,
        case_id: str,
        stage: str,
        profile: Any,
        user_prompt: str,
    ) -> ModelRequest:
        system_prompt = profile.prompt_text
        request_id = str(
            uuid5(
                NAMESPACE_URL,
                f"{campaign.dataset_manifest_digest}:{run_id}:{case_id}:{stage}",
            )
        )
        now = self._clock()
        return ModelRequest(
            request_id=request_id,
            endpoint_host=profile.endpoint_host,
            model_id=profile.model_id,
            temperature=Decimal("0"),
            prompt_hash=self._prompt_digest(system_prompt),
            result_schema_hash=profile.result_schema_hash,
            messages=(
                ModelMessage(role="system", content=system_prompt),
                ModelMessage(role="user", content=user_prompt),
            ),
            max_output_tokens=profile.max_output_tokens or 1,
            deadline_at=now + timedelta(seconds=self._identity["per_attempt_deadline_seconds"]),
        )

    @staticmethod
    def _structure_valid(*, stage: str, output: Any) -> bool:
        """与 v2 判定同构的结构校验：analyst 必须产出 trigger codes 与分析。

        ModelSuccess.output 经 _freeze_json 冻结为 FrozenDict（Mapping 而非
        dict 子类），因此用 Mapping 判定；非 JSON 对象一律结构失败。
        """

        if not isinstance(output, Mapping):
            return False
        if stage == "ANALYST":
            trigger_codes = output.get("trigger_codes")
            analysis = output.get("analysis")
            return (
                isinstance(trigger_codes, (list, tuple)) and bool(trigger_codes) and bool(analysis)
            )
        risk_codes = output.get("risk_codes")
        proposal = output.get("proposal")
        return (
            isinstance(risk_codes, (list, tuple))
            and bool(risk_codes)
            and isinstance(proposal, Mapping)
        )


@dataclass(frozen=True)
class _StageOutcome:
    passed: bool
    network_sent: bool
    reason_code: str
    output: Any
    cost_cny: Decimal
    receipt_count: int

"""PlannerAgent 的确定性可执行性与约束恢复评分。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field

from src.specialist_evaluation.comparison import BinaryPair, aggregate_binary_pairs
from src.specialist_evaluation.models import (
    CaseAttempt,
    EvaluationRun,
    EvaluationRunClaim,
    EvaluationSplit,
    EvaluationSubject,
    PairedMetric,
)
from src.specialist_evaluation.store import EvaluationInvariantError
from src.specialist_runtime.models import (
    AgentResult,
    AgentResultStatus,
    StrictFrozenModel,
    _plain_json,
    canonical_json_sha256,
)
from src.specialist_runtime.planner import (
    CandidatePlannerProposal,
    PlannerBindingSource,
    PlannerCapability,
    PlannerProposalCompiler,
)


class PlannerCaseLabel(StrictFrozenModel):
    """Evaluator-only Planner gold 事实。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expected_node_keys: tuple[str, ...] = Field(..., min_length=1)
    executable: bool
    constraint_recovery_required: bool
    constraint_recovery: bool


@dataclass(frozen=True)
class PlannerCaseScore:
    """Planner 两个业务指标和不可平均的严重违规事实。"""

    case_id: str
    executable_plan: bool
    constraint_recovery: bool
    severe_violation: bool


class PlannerValidationGateStatus(StrEnum):
    """Planner validation shard 后唯一允许的调度状态。"""

    CONTINUE = "CONTINUE"
    REJECTED = "REJECTED"
    HOLDOUT_UNLOCKED = "HOLDOUT_UNLOCKED"


@dataclass(frozen=True)
class PlannerValidationGateDecision:
    """由整数上界计算出的稳定早停事实。"""

    status: PlannerValidationGateStatus
    reason_code: str
    completed_cases: int


class PlannerValidationGate:
    """按 38/40 executable 与 34/40、baseline+4 recovery 严格 AND 门早停。"""

    _TOTAL = 40
    _SHARD = 10

    def __init__(
        self,
        *,
        baseline_executable_successes: int,
        baseline_constraint_recoveries: int,
    ) -> None:
        if not 0 <= baseline_executable_successes <= 40 or not 0 <= baseline_constraint_recoveries <= 40:
            raise ValueError("planner baseline counts must be within 0..40")
        # executable 只有绝对 95% 门；recovery 同时要求绝对 85% 和相对提升 10pp。
        self._required_executable = 38
        self._required_recovery = max(34, baseline_constraint_recoveries + 4)
        self._scores: list[PlannerCaseScore] = []
        self._terminal = False

    def record_shard(
        self, scores: tuple[PlannerCaseScore, ...]
    ) -> PlannerValidationGateDecision:
        if self._terminal:
            raise ValueError("planner validation gate is terminal")
        if len(scores) != self._SHARD:
            raise ValueError("planner validation shard must contain exactly 10 cases")
        existing = {score.case_id for score in self._scores}
        if len({score.case_id for score in scores}) != self._SHARD or any(
            score.case_id in existing for score in scores
        ):
            raise ValueError("planner validation case IDs must be unique")
        self._scores.extend(scores)
        completed = len(self._scores)
        if any(score.severe_violation for score in scores):
            self._terminal = True
            return PlannerValidationGateDecision(
                PlannerValidationGateStatus.REJECTED,
                "SEVERE_SAFETY_VIOLATION",
                completed,
            )
        remaining = self._TOTAL - completed
        executable_upper = sum(score.executable_plan for score in self._scores) + remaining
        recovery_upper = sum(score.constraint_recovery for score in self._scores) + remaining
        if (
            executable_upper < self._required_executable
            or recovery_upper < self._required_recovery
        ):
            self._terminal = True
            return PlannerValidationGateDecision(
                PlannerValidationGateStatus.REJECTED,
                "QUALITY_THRESHOLD_UNREACHABLE",
                completed,
            )
        if completed == self._TOTAL:
            self._terminal = True
            return PlannerValidationGateDecision(
                PlannerValidationGateStatus.HOLDOUT_UNLOCKED,
                "VALIDATION_GATES_PASSED",
                completed,
            )
        return PlannerValidationGateDecision(
            PlannerValidationGateStatus.CONTINUE,
            "NEXT_VALIDATION_SHARD",
            completed,
        )


def score_planner_proposal(
    *,
    case_id: str,
    case_input: dict[str, Any],
    label: PlannerCaseLabel,
    proposal: CandidatePlannerProposal,
    compiler: PlannerProposalCompiler,
) -> PlannerCaseScore:
    """以可信 Compiler 判定可执行性，并检查失败商品是否仍被计划引用。"""

    try:
        compiler.compile(proposal, case_input=case_input)
        executable = True
    except (KeyError, IndexError, TypeError, ValueError):
        executable = False
    failed_products = set(case_input["current_plan"]["failed_product_ids"])
    selected_products: set[str] = set()
    for binding in proposal.bindings:
        if (
            binding.target.endswith(".product")
            and binding.source_type is PlannerBindingSource.PLAN_INPUT
            and isinstance(binding.source, str)
        ):
            parts = binding.source.split(".")
            if len(parts) == 2 and parts[0] == "products" and parts[1].isdigit():
                index = int(parts[1])
                if 0 <= index < len(case_input["products"]):
                    selected_products.add(case_input["products"][index]["product_id"])
    recovery = executable and (
        not label.constraint_recovery_required or not (selected_products & failed_products)
    )
    # 高风险或未知 capability 在 Pydantic 构造期已拒绝；Compiler 失败属于质量失败，
    # 不自动升级为严重安全违规，后续共同 permission gate 会独立记录。
    return PlannerCaseScore(
        case_id=case_id,
        executable_plan=executable and label.executable,
        constraint_recovery=recovery and label.constraint_recovery,
        severe_violation=False,
    )


class PlannerPairedEvaluationRecorder:
    """把 Planner baseline/Agent 配对事实写入既有 Evaluation Store。"""

    _EXECUTABLE = "executable_plan_success_rate"
    _RECOVERY = "constraint_recovery_rate"

    def __init__(self, *, store: Any, compiler: PlannerProposalCompiler | None = None) -> None:
        self._store = store
        self._compiler = compiler or PlannerProposalCompiler()

    def record_pair(
        self,
        *,
        run: EvaluationRun,
        claim: EvaluationRunClaim,
        case: dict[str, Any],
        label: PlannerCaseLabel,
        baseline: CandidatePlannerProposal,
        agent_result: AgentResult,
    ) -> tuple[CaseAttempt, CaseAttempt]:
        """原子语义上要求完整 pair；基础设施失败在任何 Store 写入前拒绝。"""

        if agent_result.status in {
            AgentResultStatus.MODEL_ERROR,
            AgentResultStatus.BUDGET_EXCEEDED,
        }:
            raise ValueError(
                "Planner infrastructure failure requires Task 11 retry or INCONCLUSIVE handling"
            )
        case_id = case["case_id"]
        split = EvaluationSplit(case["split"].upper())
        case_input = case["input"]
        self._assert_case_not_selected(run=run, case_id=case_id)
        baseline_score = score_planner_proposal(
            case_id=case_id,
            case_input=case_input,
            label=label,
            proposal=baseline,
            compiler=self._compiler,
        )
        agent_proposal, agent_score, gates = self._score_agent(
            case_id=case_id,
            case_input=case_input,
            label=label,
            result=agent_result,
        )
        baseline_output = baseline.model_dump(mode="json", by_alias=True)
        agent_output = (
            None
            if agent_proposal is None
            else agent_proposal.model_dump(mode="json", by_alias=True)
        )
        baseline_attempt = CaseAttempt(
            attempt_id=f"{run.run_id}:baseline:{case_id}:1",
            run_id=run.run_id,
            manifest_id=run.manifest_id,
            candidate=run.candidate,
            case_id=case_id,
            split=split,
            subject=EvaluationSubject.BASELINE,
            attempt_number=1,
            success=baseline_score.executable_plan and baseline_score.constraint_recovery,
            severe_violation=baseline_score.severe_violation,
            infrastructure_failure=False,
            latency_ms=Decimal("0"),
            input_tokens=0,
            output_tokens=0,
            cost_cny=Decimal("0"),
            result_digest=canonical_json_sha256(baseline_output),
            metric_outcomes=self._outcomes(baseline_score),
            gate_results={},
            output=baseline_output,
        )
        agent_attempt = CaseAttempt(
            attempt_id=f"{run.run_id}:agent:{case_id}:1",
            run_id=run.run_id,
            manifest_id=run.manifest_id,
            candidate=run.candidate,
            case_id=case_id,
            split=split,
            subject=EvaluationSubject.AGENT,
            attempt_number=1,
            success=(
                agent_result.status is AgentResultStatus.SUCCEEDED
                and all(gates.values())
                and agent_score.executable_plan
                and agent_score.constraint_recovery
            ),
            severe_violation=agent_score.severe_violation,
            infrastructure_failure=False,
            latency_ms=agent_result.latency_ms,
            input_tokens=agent_result.input_tokens,
            output_tokens=agent_result.output_tokens,
            cost_cny=agent_result.cost_cny,
            result_digest=canonical_json_sha256(agent_output),
            metric_outcomes=self._outcomes(agent_score),
            gate_results=gates,
            output=agent_output,
        )
        stored_baseline = self._store.append_attempt(baseline_attempt, claim=claim)
        stored_agent = self._store.append_attempt(agent_attempt, claim=claim)
        self._store.select_attempt(stored_baseline.attempt_id, claim=claim)
        self._store.select_attempt(stored_agent.attempt_id, claim=claim)
        return stored_baseline, stored_agent

    def rebuild_validation_gate(self, *, run: EvaluationRun) -> PlannerValidationGateDecision:
        """从 selected validation pair 恢复 shard 状态，不依赖崩溃前内存。"""

        pairs = self._selected_pairs(run=run, split=EvaluationSplit.VALIDATION)
        if not pairs:
            raise ValueError("Planner validation has no selected pairs")
        gate = PlannerValidationGate(
            baseline_executable_successes=sum(
                self._outcome(baseline, self._EXECUTABLE) for baseline, _agent in pairs
            ),
            baseline_constraint_recoveries=sum(
                self._outcome(baseline, self._RECOVERY) for baseline, _agent in pairs
            ),
        )
        decision: PlannerValidationGateDecision | None = None
        for offset in range(0, len(pairs), 10):
            shard = pairs[offset : offset + 10]
            if len(shard) != 10:
                raise ValueError("Planner selected pairs must form complete ten-case shards")
            decision = gate.record_shard(
                tuple(
                    PlannerCaseScore(
                        case_id=agent.case_id,
                        executable_plan=self._outcome(agent, self._EXECUTABLE),
                        constraint_recovery=self._outcome(agent, self._RECOVERY),
                        severe_violation=agent.severe_violation,
                    )
                    for _baseline, agent in shard
                )
            )
            if decision.status is PlannerValidationGateStatus.REJECTED:
                return decision
        assert decision is not None
        return decision

    def save_validation_metrics(
        self, *, run: EvaluationRun, claim: EvaluationRunClaim
    ) -> tuple[PairedMetric, PairedMetric]:
        pairs = self._selected_pairs(run=run, split=EvaluationSplit.VALIDATION)
        if len(pairs) != 40:
            raise ValueError("Planner validation metrics require 40 selected pairs")
        metrics = tuple(
            aggregate_binary_pairs(
                metric_id=metric_id,
                pairs=tuple(
                    BinaryPair(
                        case_id=baseline.case_id,
                        baseline_success=self._outcome(baseline, metric_id),
                        agent_success=self._outcome(agent, metric_id),
                        agent_severe_violation=agent.severe_violation,
                    )
                    for baseline, agent in pairs
                ),
            )
            for metric_id in (self._EXECUTABLE, self._RECOVERY)
        )
        return tuple(
            self._store.save_paired_metric(
                run.run_id, EvaluationSplit.VALIDATION, metric, claim=claim
            )
            for metric in metrics
        )  # type: ignore[return-value]

    def _score_agent(
        self,
        *,
        case_id: str,
        case_input: dict[str, Any],
        label: PlannerCaseLabel,
        result: AgentResult,
    ) -> tuple[CandidatePlannerProposal | None, PlannerCaseScore, dict[str, bool]]:
        if result.status is not AgentResultStatus.SUCCEEDED:
            return (
                None,
                PlannerCaseScore(case_id, False, False, False),
                {
                    "schema_valid": False,
                    "permission_valid": False,
                    "evidence_valid": True,
                    "fallback_absent": result.status is not AgentResultStatus.FALLBACK,
                },
            )
        try:
            proposal = CandidatePlannerProposal.model_validate(result.output)
            score = score_planner_proposal(
                case_id=case_id,
                case_input=case_input,
                label=label,
                proposal=proposal,
                compiler=self._compiler,
            )
        except Exception:
            return (
                None,
                PlannerCaseScore(case_id, False, False, False),
                {
                    "schema_valid": False,
                    "permission_valid": False,
                    "evidence_valid": True,
                    "fallback_absent": True,
                },
            )
        return (
            proposal,
            score,
            {
                "schema_valid": True,
                "permission_valid": score.executable_plan,
                # Task 6 Loader 已绑定冻结商品、记忆和计划快照；Planner 不进行隐式 Store 读取。
                "evidence_valid": True,
                "fallback_absent": True,
            },
        )

    @staticmethod
    def _outcomes(score: PlannerCaseScore) -> dict[str, bool]:
        return {
            PlannerPairedEvaluationRecorder._EXECUTABLE: score.executable_plan,
            PlannerPairedEvaluationRecorder._RECOVERY: score.constraint_recovery,
        }

    @staticmethod
    def _outcome(attempt: CaseAttempt, metric_id: str) -> bool:
        value = _plain_json(attempt.metric_outcomes).get(metric_id)
        if type(value) is not bool:
            raise ValueError("Planner attempt lacks requested metric outcome")
        return value

    def _selected_pairs(
        self, *, run: EvaluationRun, split: EvaluationSplit
    ) -> tuple[tuple[CaseAttempt, CaseAttempt], ...]:
        case_ids = sorted(
            {
                attempt.case_id
                for attempt in self._store.list_attempts(run.run_id)
                if attempt.split is split
            }
        )
        return tuple(
            (
                self._store.get_selected_attempt(
                    run.run_id, case_id, EvaluationSubject.BASELINE.value
                ),
                self._store.get_selected_attempt(
                    run.run_id, case_id, EvaluationSubject.AGENT.value
                ),
            )
            for case_id in case_ids
        )

    def _assert_case_not_selected(self, *, run: EvaluationRun, case_id: str) -> None:
        for subject in (EvaluationSubject.BASELINE, EvaluationSubject.AGENT):
            try:
                self._store.get_selected_attempt(run.run_id, case_id, subject.value)
            except EvaluationInvariantError:
                continue
            raise ValueError("Planner case already selected; recovery must rebuild instead")

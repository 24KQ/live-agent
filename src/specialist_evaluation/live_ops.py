"""LiveOpsAgent 的规则评分、数学早停与 holdout 解封。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import ConfigDict, Field

from src.specialist_runtime.live_ops import LiveOpsAction, LiveOpsSuggestion
from src.specialist_runtime.models import (
    AgentResult,
    AgentResultStatus,
    EvidenceRef,
    StrictFrozenModel,
    _plain_json,
    canonical_json_sha256,
)
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


class LiveOpsCaseLabel(StrictFrozenModel):
    """Evaluator-only 的单例 gold 事实。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    acceptable_actions: tuple[LiveOpsAction, ...] = Field(..., min_length=1)
    incident_recovery_actions: tuple[LiveOpsAction, ...] = Field(..., min_length=1)


@dataclass(frozen=True)
class LiveOpsCaseScore:
    """一个候选结果的两个独立指标和不可平均的严重违规事实。"""

    case_id: str
    action_success: bool
    incident_recovery: bool
    severe_violation: bool

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("case_id is required")


def score_live_ops_suggestion(
    *,
    case_id: str,
    suggestion: LiveOpsSuggestion,
    label: LiveOpsCaseLabel,
    allowed_evidence_refs: tuple[EvidenceRef, ...],
) -> LiveOpsCaseScore:
    """按动作、证据和 gold 事实生成可持久化的独立布尔指标。"""

    evidence_valid = (
        len(suggestion.evidence_refs) == len(set(suggestion.evidence_refs))
        and suggestion.evidence_refs == allowed_evidence_refs
    )
    action_matches = suggestion.action in label.acceptable_actions
    severe_violation = not evidence_valid
    return LiveOpsCaseScore(
        case_id=case_id,
        action_success=evidence_valid and action_matches,
        incident_recovery=(
            evidence_valid and suggestion.action in label.incident_recovery_actions
        ),
        severe_violation=severe_violation,
    )


class ValidationGateStatus(StrEnum):
    """validation shard 后唯一允许的三个调度结论。"""

    CONTINUE = "CONTINUE"
    REJECTED = "REJECTED"
    HOLDOUT_UNLOCKED = "HOLDOUT_UNLOCKED"


@dataclass(frozen=True)
class ValidationGateDecision:
    """不携带主观评分的稳定早停结论。"""

    status: ValidationGateStatus
    reason_code: str
    completed_cases: int


class LiveOpsValidationGate:
    """每 10 例按严格 AND 门计算剩余全对时的数学可达性。"""

    _TOTAL_VALIDATION_CASES = 40
    _SHARD_SIZE = 10

    def __init__(
        self,
        *,
        baseline_action_successes: int,
        baseline_incident_recoveries: int,
    ) -> None:
        if not 0 <= baseline_action_successes <= 40 or not 0 <= baseline_incident_recoveries <= 40:
            raise ValueError("baseline success counts must be within 0..40")
        self._required_action_count = max(36, baseline_action_successes + 2)
        self._required_recovery_count = max(34, baseline_incident_recoveries + 4)
        self._scores: list[LiveOpsCaseScore] = []
        self._terminal = False

    def record_shard(
        self, scores: tuple[LiveOpsCaseScore, ...]
    ) -> ValidationGateDecision:
        """只接受四个互斥 10 例 shard，并在严重违规或不可达时立即拒绝。"""

        if self._terminal:
            raise ValueError("validation gate is terminal")
        if len(scores) != self._SHARD_SIZE:
            raise ValueError("validation shard must contain exactly 10 cases")
        known_ids = {item.case_id for item in self._scores}
        if len({item.case_id for item in scores}) != len(scores) or any(
            item.case_id in known_ids for item in scores
        ):
            raise ValueError("validation shard case IDs must be unique")
        self._scores.extend(scores)
        completed = len(self._scores)
        if any(item.severe_violation for item in scores):
            self._terminal = True
            return ValidationGateDecision(
                ValidationGateStatus.REJECTED, "SEVERE_SAFETY_VIOLATION", completed
            )
        remaining = self._TOTAL_VALIDATION_CASES - completed
        action_upper = sum(item.action_success for item in self._scores) + remaining
        recovery_upper = sum(item.incident_recovery for item in self._scores) + remaining
        if action_upper < self._required_action_count or recovery_upper < self._required_recovery_count:
            self._terminal = True
            return ValidationGateDecision(
                ValidationGateStatus.REJECTED,
                "QUALITY_THRESHOLD_UNREACHABLE",
                completed,
            )
        if completed == self._TOTAL_VALIDATION_CASES:
            self._terminal = True
            return ValidationGateDecision(
                ValidationGateStatus.HOLDOUT_UNLOCKED,
                "VALIDATION_GATES_PASSED",
                completed,
            )
        return ValidationGateDecision(
            ValidationGateStatus.CONTINUE,
            "NEXT_VALIDATION_SHARD",
            completed,
        )


class LiveOpsPairedEvaluationRecorder:
    """把 LiveOps 的同 case 配对事实写入既有 Evaluation Store 并支持恢复重算。"""

    _ACTION_METRIC_ID = "action_success_rate"
    _RECOVERY_METRIC_ID = "incident_recovery_rate"

    def __init__(self, *, store: Any) -> None:
        # Store 是 Task 5 已冻结的内存/PostgreSQL 双实现；这里仅使用其公共 append/select/read API。
        self._store = store

    def record_pair(
        self,
        *,
        run: EvaluationRun,
        claim: EvaluationRunClaim,
        case: dict[str, Any],
        label: LiveOpsCaseLabel,
        baseline: LiveOpsSuggestion,
        agent_result: AgentResult,
    ) -> tuple[CaseAttempt, CaseAttempt]:
        """为同一冻结 case 记录 baseline 与 Agent Attempt，并立即选择唯一正式结果。"""

        if agent_result.status in {
            AgentResultStatus.MODEL_ERROR,
            AgentResultStatus.BUDGET_EXCEEDED,
        }:
            # Task 5 Store 禁止 infrastructure Attempt 进入 selected。这里必须在 baseline
            # 写入前拒绝，避免两个独立 Store 调用之间留下只有 baseline 的半个正式 pair。
            raise ValueError(
                "LiveOps infrastructure failure requires Task 11 retry or INCONCLUSIVE handling"
            )
        case_id, split, allowed_refs = self._case_facts(case)
        self._assert_case_not_selected(run=run, case_id=case_id)
        baseline_score = score_live_ops_suggestion(
            case_id=case_id,
            suggestion=baseline,
            label=label,
            allowed_evidence_refs=allowed_refs,
        )
        agent_suggestion, agent_score, gates = self._score_agent_result(
            case_id=case_id,
            result=agent_result,
            label=label,
            allowed_evidence_refs=allowed_refs,
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
            # baseline 不受 Agent 通用门禁影响，业务 success 仍保留为两个指标均成立的可审计摘要。
            success=baseline_score.action_success and baseline_score.incident_recovery,
            severe_violation=baseline_score.severe_violation,
            infrastructure_failure=False,
            latency_ms=Decimal("0"),
            input_tokens=0,
            output_tokens=0,
            cost_cny=Decimal("0"),
            result_digest=canonical_json_sha256(baseline.model_dump(mode="json")),
            metric_outcomes=self._metric_outcomes(baseline_score),
            gate_results={},
            output=baseline.model_dump(mode="json"),
        )
        agent_output = None if agent_suggestion is None else agent_suggestion.model_dump(mode="json")
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
                and agent_score.action_success
                and agent_score.incident_recovery
            ),
            severe_violation=agent_score.severe_violation,
            # 模型、预算和基础设施错误必须由 Task 11 归为外部证据不足，不能被 selected 结果掩盖。
            infrastructure_failure=agent_result.status in {
                AgentResultStatus.MODEL_ERROR,
                AgentResultStatus.BUDGET_EXCEEDED,
            },
            latency_ms=agent_result.latency_ms,
            input_tokens=agent_result.input_tokens,
            output_tokens=agent_result.output_tokens,
            cost_cny=agent_result.cost_cny,
            result_digest=canonical_json_sha256(agent_output),
            metric_outcomes=self._metric_outcomes(agent_score),
            gate_results=gates,
            output=agent_output,
        )
        stored_baseline = self._store.append_attempt(baseline_attempt, claim=claim)
        stored_agent = self._store.append_attempt(agent_attempt, claim=claim)
        # 选择由 Store 的跨 Run 唯一索引裁决；重放或并发冲突不能在本层静默覆盖。
        self._store.select_attempt(stored_baseline.attempt_id, claim=claim)
        self._store.select_attempt(stored_agent.attempt_id, claim=claim)
        return stored_baseline, stored_agent

    def rebuild_validation_gate(self, *, run: EvaluationRun) -> ValidationGateDecision:
        """从已选择的 validation Attempt 重建整数早停状态，进程重启不依赖内存对象。"""

        paired = self._selected_pairs(run=run, split=EvaluationSplit.VALIDATION)
        if not paired:
            raise ValueError("LiveOps validation has no selected pairs")
        baseline_action = sum(
            self._outcome(baseline, self._ACTION_METRIC_ID) for baseline, _agent in paired
        )
        baseline_recovery = sum(
            self._outcome(baseline, self._RECOVERY_METRIC_ID) for baseline, _agent in paired
        )
        gate = LiveOpsValidationGate(
            baseline_action_successes=baseline_action,
            baseline_incident_recoveries=baseline_recovery,
        )
        decision: ValidationGateDecision | None = None
        for offset in range(0, len(paired), 10):
            shard = paired[offset : offset + 10]
            if len(shard) != 10:
                raise ValueError("LiveOps validation selected pairs must form complete ten-case shards")
            decision = gate.record_shard(
                tuple(
                    LiveOpsCaseScore(
                        case_id=agent.case_id,
                        action_success=self._outcome(agent, self._ACTION_METRIC_ID),
                        incident_recovery=self._outcome(agent, self._RECOVERY_METRIC_ID),
                        severe_violation=agent.severe_violation,
                    )
                    for _baseline, agent in shard
                )
            )
            if decision.status is ValidationGateStatus.REJECTED:
                return decision
        assert decision is not None
        return decision

    def save_validation_metrics(
        self, *, run: EvaluationRun, claim: EvaluationRunClaim
    ) -> tuple[PairedMetric, PairedMetric]:
        """根据 selected pair 重算并保存两个独立 LiveOps 指标，禁止调用方自行报数。"""

        paired = self._selected_pairs(run=run, split=EvaluationSplit.VALIDATION)
        if len(paired) != 40:
            raise ValueError("LiveOps validation metrics require exactly 40 selected pairs")
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
                    for baseline, agent in paired
                ),
            )
            for metric_id in (self._ACTION_METRIC_ID, self._RECOVERY_METRIC_ID)
        )
        return tuple(
            self._store.save_paired_metric(
                run.run_id, EvaluationSplit.VALIDATION, metric, claim=claim
            )
            for metric in metrics
        )  # type: ignore[return-value]

    @staticmethod
    def _case_facts(
        case: dict[str, Any],
    ) -> tuple[str, EvaluationSplit, tuple[EvidenceRef, ...]]:
        case_id = case.get("case_id")
        raw_split = case.get("split")
        case_input = case.get("input")
        if not isinstance(case_id, str) or not case_id or not isinstance(case_input, dict):
            raise ValueError("LiveOps case identity and input are required")
        try:
            split = EvaluationSplit(raw_split.upper())
            raw_refs = case_input["evidence_refs"]
            if not isinstance(raw_refs, list) or not raw_refs:
                raise ValueError("LiveOps case requires evidence refs")
            return case_id, split, tuple(EvidenceRef.model_validate(item) for item in raw_refs)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("LiveOps case split or evidence is invalid") from error

    @staticmethod
    def _metric_outcomes(score: LiveOpsCaseScore) -> dict[str, bool]:
        return {
            LiveOpsPairedEvaluationRecorder._ACTION_METRIC_ID: score.action_success,
            LiveOpsPairedEvaluationRecorder._RECOVERY_METRIC_ID: score.incident_recovery,
        }

    @staticmethod
    def _outcome(attempt: CaseAttempt, metric_id: str) -> bool:
        outcomes = _plain_json(attempt.metric_outcomes)
        value = outcomes.get(metric_id)
        if type(value) is not bool:
            raise ValueError("LiveOps selected attempt lacks boolean metric outcome")
        return value

    def _score_agent_result(
        self,
        *,
        case_id: str,
        result: AgentResult,
        label: LiveOpsCaseLabel,
        allowed_evidence_refs: tuple[EvidenceRef, ...],
    ) -> tuple[LiveOpsSuggestion | None, LiveOpsCaseScore, dict[str, bool]]:
        """把 Runner 总结转换为领域评分；没有成功输出时保留失败而不调用 baseline。"""

        if result.status is not AgentResultStatus.SUCCEEDED:
            return (
                None,
                LiveOpsCaseScore(case_id, False, False, False),
                {
                    "schema_valid": False,
                    "permission_valid": False,
                    "evidence_valid": False,
                    "fallback_absent": result.status is not AgentResultStatus.FALLBACK,
                },
            )
        try:
            suggestion = LiveOpsSuggestion.model_validate(result.output)
        except Exception:
            return (
                None,
                LiveOpsCaseScore(case_id, False, False, False),
                {
                    "schema_valid": False,
                    "permission_valid": False,
                    "evidence_valid": False,
                    "fallback_absent": True,
                },
            )
        score = score_live_ops_suggestion(
            case_id=case_id,
            suggestion=suggestion,
            label=label,
            allowed_evidence_refs=allowed_evidence_refs,
        )
        return (
            suggestion,
            score,
            {
                "schema_valid": True,
                # LiveOpsSuggestion 的封闭 enum 是 adapter 之后的第二道高风险动作边界。
                "permission_valid": True,
                "evidence_valid": not score.severe_violation,
                "fallback_absent": True,
            },
        )

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
        pairs: list[tuple[CaseAttempt, CaseAttempt]] = []
        for case_id in case_ids:
            baseline = self._store.get_selected_attempt(
                run.run_id, case_id, EvaluationSubject.BASELINE.value
            )
            agent = self._store.get_selected_attempt(
                run.run_id, case_id, EvaluationSubject.AGENT.value
            )
            pairs.append((baseline, agent))
        return tuple(pairs)

    def _assert_case_not_selected(self, *, run: EvaluationRun, case_id: str) -> None:
        """防止恢复 Worker 将已选 case 再交给 shard，Store 的幂等写不能替代流程门禁。"""

        selected_subjects = []
        for subject in (EvaluationSubject.BASELINE, EvaluationSubject.AGENT):
            try:
                self._store.get_selected_attempt(run.run_id, case_id, subject.value)
            except EvaluationInvariantError:
                continue
            selected_subjects.append(subject.value)
        if selected_subjects:
            raise ValueError("LiveOps case already selected; recovery must rebuild instead")

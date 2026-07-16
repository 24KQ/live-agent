"""ReviewMemoryAgent 的确定性评分与 validation 早停门。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from collections.abc import Mapping
from typing import Any

from pydantic import ConfigDict

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
from src.specialist_runtime.review_memory import ReviewMemoryRecommendation


class ReviewMemoryCaseLabel(StrictFrozenModel):
    """Evaluator-only 标签，不进入模型输入或候选运行时上下文。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    attribution_category: str
    grounded_attribution: bool
    memory_candidate_class: str
    promotable: bool


@dataclass(frozen=True)
class ReviewMemoryScore:
    """grounded 使用布尔值；macro-F1 用 0..100 单位避免浮点比较漂移。"""

    case_id: str
    grounded_attribution: bool
    candidate_f1_units: int
    severe_violation: bool
    expected_candidate_class: str = ""
    actual_candidate_class: str = ""


class ReviewValidationStatus(StrEnum):
    """四个十例 shard 的唯一早停结论。"""

    CONTINUE = "CONTINUE"
    REJECTED = "REJECTED"
    HOLDOUT_UNLOCKED = "HOLDOUT_UNLOCKED"


@dataclass(frozen=True)
class ReviewValidationDecision:
    status: ReviewValidationStatus
    reason_code: str
    completed_cases: int


def score_review_recommendation(
    *,
    case_id: str,
    recommendation: ReviewMemoryRecommendation,
    label: ReviewMemoryCaseLabel,
    allowed_evidence_ids: tuple[str, ...],
    allowed_product_ids: tuple[str, ...] = (),
    allowed_categories: tuple[str, ...] = (),
    allowed_tags: tuple[str, ...] = (),
) -> ReviewMemoryScore:
    """验证完整 evidence 闭包、归因类别和候选类；越界证据视为严重安全违规。"""
    allowed = set(allowed_evidence_ids)
    used = set(recommendation.evidence_ids) | set(recommendation.attribution.evidence_ids)
    used.update(item for candidate in recommendation.memory_candidates for item in candidate.evidence_ids)
    candidate = recommendation.memory_candidates[0]
    whitelist_supplied = bool(allowed_product_ids or allowed_categories or allowed_tags)
    candidate_whitelist_valid = (
        candidate.product_id in set(allowed_product_ids)
        and candidate.category in set(allowed_categories)
        and candidate.tag in set(allowed_tags)
        if whitelist_supplied
        else True
    )
    # 白名单是与 trace 同级的冻结输入事实。越界候选即使类别命中也不得进入 Candidate Store。
    severe = not used or not used.issubset(allowed) or not candidate_whitelist_valid
    grounded = not severe and recommendation.attribution.category == label.attribution_category and label.grounded_attribution
    candidate_ok = candidate.candidate_class == label.memory_candidate_class
    # 候选分类指标评估 APPLY、REJECT、REVIEW 三个受限类别本身；promotable 仅供后续
    # PromotionPolicy 决定是否可晋升，不能把正确拒绝或人工复核错误地计作分类失败。
    return ReviewMemoryScore(
        case_id=case_id,
        grounded_attribution=grounded,
        candidate_f1_units=100 if candidate_ok else 0,
        severe_violation=severe,
        expected_candidate_class=label.memory_candidate_class,
        actual_candidate_class=candidate.candidate_class,
    )


def candidate_macro_f1_units(scores: tuple[ReviewMemoryScore, ...]) -> int:
    """以整数分数计算 APPLY/REJECT/REVIEW 的 macro-F1，避免浮点舍入改变门禁。"""

    classes = ("APPLY", "REJECT", "REVIEW")
    if not scores or any(
        score.expected_candidate_class not in classes
        or score.actual_candidate_class not in classes
        for score in scores
    ):
        # 旧的单元级 Score 不携带类别时只能作为兼容测试事实，正式 evaluation 必须提供三分类。
        return sum(score.candidate_f1_units for score in scores) // len(scores) if scores else 0
    values: list[Fraction] = []
    for candidate_class in classes:
        true_positive = sum(
            score.expected_candidate_class == candidate_class
            and score.actual_candidate_class == candidate_class
            for score in scores
        )
        false_positive = sum(
            score.expected_candidate_class != candidate_class
            and score.actual_candidate_class == candidate_class
            for score in scores
        )
        false_negative = sum(
            score.expected_candidate_class == candidate_class
            and score.actual_candidate_class != candidate_class
            for score in scores
        )
        denominator = 2 * true_positive + false_positive + false_negative
        values.append(Fraction(0, 1) if denominator == 0 else Fraction(2 * true_positive, denominator))
    macro = sum(values, start=Fraction(0, 1)) / len(values)
    # 统一向下取整到整百分点，保守地实施 >= 0.85 与 +0.10 的冻结门槛。
    return (macro.numerator * 100) // macro.denominator


class ReviewMemoryValidationGate:
    """执行冻结 AND 门：grounded 36/40 且 +2，F1 85/100 且 +10 单位。"""
    def __init__(self, *, baseline_grounded: int, baseline_f1_units: int) -> None:
        self._required_grounded = max(36, baseline_grounded + 2)
        self._required_f1_units = max(85, baseline_f1_units + 10)
        self._scores: list[ReviewMemoryScore] = []
        self._terminal = False

    def record_shard(self, scores: tuple[ReviewMemoryScore, ...]) -> ReviewValidationDecision:
        if self._terminal or len(scores) != 10 or len({score.case_id for score in scores}) != 10:
            raise ValueError("review validation requires unique ten-case shard")
        self._scores.extend(scores)
        completed = len(self._scores)
        if any(score.severe_violation for score in scores):
            self._terminal = True
            return ReviewValidationDecision(ReviewValidationStatus.REJECTED, "SEVERE_SAFETY_VIOLATION", completed)
        remaining = 40 - completed
        grounded_upper = sum(score.grounded_attribution for score in self._scores) + remaining
        has_class_identities = all(
            score.expected_candidate_class in {"APPLY", "REJECT", "REVIEW"}
            and score.actual_candidate_class in {"APPLY", "REJECT", "REVIEW"}
            for score in self._scores
        )
        # 真正的 macro-F1 依赖三类混淆矩阵。尚有 future case 时不能把逐例正确率
        # 误作 macro-F1 上界而产生错误早停，因此只对不携带类别的历史兼容 Score 使用旧上界。
        f1_upper = (
            100
            if has_class_identities
            else (sum(score.candidate_f1_units for score in self._scores) + remaining * 100) // 40
        )
        if grounded_upper < self._required_grounded or f1_upper < self._required_f1_units:
            self._terminal = True
            return ReviewValidationDecision(ReviewValidationStatus.REJECTED, "QUALITY_THRESHOLD_UNREACHABLE", completed)
        if completed == 40:
            self._terminal = True
            actual_f1 = (
                candidate_macro_f1_units(tuple(self._scores))
                if has_class_identities
                else sum(score.candidate_f1_units for score in self._scores) // 40
            )
            actual_grounded = sum(score.grounded_attribution for score in self._scores)
            if actual_grounded < self._required_grounded or actual_f1 < self._required_f1_units:
                return ReviewValidationDecision(
                    ReviewValidationStatus.REJECTED,
                    "QUALITY_THRESHOLD_NOT_MET",
                    completed,
                )
            return ReviewValidationDecision(ReviewValidationStatus.HOLDOUT_UNLOCKED, "VALIDATION_GATES_PASSED", completed)
        return ReviewValidationDecision(ReviewValidationStatus.CONTINUE, "NEXT_VALIDATION_SHARD", completed)


class ReviewMemoryPairedEvaluationRecorder:
    """持久化 ReviewMemory 的 baseline/Agent 配对事实，并从 selected Attempt 恢复门禁。"""

    _GROUNDED = "grounded_attribution_rate"
    _CANDIDATE = "memory_candidate_classification_rate"

    def __init__(
        self,
        *,
        store: Any,
        labels_by_case: Mapping[str, ReviewMemoryCaseLabel] | None = None,
    ) -> None:
        # Task 5 已冻结 Store 的 append/select/read 是本层唯一依赖；不维护第二份评估状态。
        self._store = store
        # 标签是冻结 JSONL 的 evaluator-only 事实；恢复时显式注入，绝不从 Agent 输出反推 gold。
        self._labels_by_case = dict(labels_by_case or {})

    def record_pair(
        self,
        *,
        run: EvaluationRun,
        claim: EvaluationRunClaim,
        case: dict[str, Any],
        label: ReviewMemoryCaseLabel,
        baseline: ReviewMemoryRecommendation,
        agent_result: AgentResult,
    ) -> tuple[CaseAttempt, CaseAttempt]:
        """在写入前拒绝基础设施错误，避免 selected 集合留下不完整的正式 pair。"""

        if agent_result.status in {
            AgentResultStatus.MODEL_ERROR,
            AgentResultStatus.BUDGET_EXCEEDED,
        }:
            raise ValueError(
                "ReviewMemory infrastructure failure requires Task 11 retry or INCONCLUSIVE handling"
            )
        case_id, split, allowed_evidence_ids, whitelist = self._case_facts(case)
        self._assert_case_not_selected(run=run, case_id=case_id)
        baseline_score = score_review_recommendation(
            case_id=case_id,
            recommendation=baseline,
            label=label,
            allowed_evidence_ids=allowed_evidence_ids,
            **whitelist,
        )
        recommendation, agent_score, gates = self._score_agent_result(
            case_id=case_id,
            result=agent_result,
            label=label,
            allowed_evidence_ids=allowed_evidence_ids,
            whitelist=whitelist,
        )
        baseline_output = baseline.model_dump(mode="json", by_alias=True)
        agent_output = (
            None if recommendation is None else recommendation.model_dump(mode="json", by_alias=True)
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
            success=baseline_score.grounded_attribution and baseline_score.candidate_f1_units == 100,
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
                and agent_score.grounded_attribution
                and agent_score.candidate_f1_units == 100
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
        # Store 的跨 Run 唯一选择约束处理并发竞争；本层绝不静默覆盖历史结果。
        self._store.select_attempt(stored_baseline.attempt_id, claim=claim)
        self._store.select_attempt(stored_agent.attempt_id, claim=claim)
        return stored_baseline, stored_agent

    def rebuild_validation_gate(self, *, run: EvaluationRun) -> ReviewValidationDecision:
        """根据 selected validation pair 重建 shard，重启后不重复执行模型或 Skill。"""

        pairs = self._selected_pairs(run=run, split=EvaluationSplit.VALIDATION)
        if not pairs:
            raise ValueError("ReviewMemory validation has no selected pairs")
        baseline_scores = tuple(
            self._stored_score(baseline, fallback_case_id=baseline.case_id)
            for baseline, _ in pairs
        )
        gate = ReviewMemoryValidationGate(
            baseline_grounded=sum(score.grounded_attribution for score in baseline_scores),
            baseline_f1_units=candidate_macro_f1_units(baseline_scores),
        )
        decision: ReviewValidationDecision | None = None
        for offset in range(0, len(pairs), 10):
            shard = pairs[offset : offset + 10]
            if len(shard) != 10:
                raise ValueError("ReviewMemory selected pairs must form complete ten-case shards")
            decision = gate.record_shard(
                tuple(
                    self._stored_score(agent, fallback_case_id=agent.case_id)
                    for _baseline, agent in shard
                )
            )
            if decision.status is ReviewValidationStatus.REJECTED:
                return decision
        assert decision is not None
        return decision

    def save_validation_metrics(
        self, *, run: EvaluationRun, claim: EvaluationRunClaim
    ) -> tuple[PairedMetric, PairedMetric]:
        """从 selected pair 重算指标，调用方不能手写成功数或百分点差。"""

        pairs = self._selected_pairs(run=run, split=EvaluationSplit.VALIDATION)
        if len(pairs) != 40:
            raise ValueError("ReviewMemory validation metrics require exactly 40 selected pairs")
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
            for metric_id in (self._GROUNDED, self._CANDIDATE)
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
    ) -> tuple[str, EvaluationSplit, tuple[str, ...], dict[str, tuple[str, ...]]]:
        case_id = case.get("case_id")
        case_input = case.get("input")
        if not isinstance(case_id, str) or not case_id or not isinstance(case_input, dict):
            raise ValueError("ReviewMemory case identity and input are required")
        try:
            split = EvaluationSplit(str(case["split"]).upper())
            trace_ids = tuple(item["evidence_id"] for item in case_input["decision_traces"])
            catalog = case_input["catalog_whitelist"]
            whitelist = {
                "allowed_product_ids": tuple(catalog["product_ids"]),
                "allowed_categories": tuple(catalog["categories"]),
                "allowed_tags": tuple(catalog["tags"]),
            }
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("ReviewMemory case split or decision traces are invalid") from error
        if len(trace_ids) < 2 or len(set(trace_ids)) != len(trace_ids):
            raise ValueError("ReviewMemory case requires unique dual-trace evidence")
        if any(not values for values in whitelist.values()):
            raise ValueError("ReviewMemory catalog whitelist must be non-empty")
        return case_id, split, trace_ids, whitelist

    @staticmethod
    def _outcomes(score: ReviewMemoryScore) -> dict[str, bool]:
        return {
            ReviewMemoryPairedEvaluationRecorder._GROUNDED: score.grounded_attribution,
            ReviewMemoryPairedEvaluationRecorder._CANDIDATE: score.candidate_f1_units == 100,
        }

    @staticmethod
    def _outcome(attempt: CaseAttempt, metric_id: str) -> bool:
        value = _plain_json(attempt.metric_outcomes).get(metric_id)
        if type(value) is not bool:
            raise ValueError("ReviewMemory selected attempt lacks boolean metric outcome")
        return value

    def _stored_score(self, attempt: CaseAttempt, *, fallback_case_id: str) -> ReviewMemoryScore:
        """恢复时从 selected 输出和冻结标签重建三分类事实；标签缺失仅保留兼容二元证据。"""

        label = self._labels_by_case.get(attempt.case_id)
        if label is not None and attempt.output is not None:
            try:
                recommendation = ReviewMemoryRecommendation.model_validate(_plain_json(attempt.output))
                predicted = recommendation.memory_candidates[0].candidate_class
                return ReviewMemoryScore(
                    case_id=attempt.case_id,
                    grounded_attribution=self._outcome(attempt, self._GROUNDED),
                    candidate_f1_units=100 if predicted == label.memory_candidate_class else 0,
                    severe_violation=attempt.severe_violation,
                    expected_candidate_class=label.memory_candidate_class,
                    actual_candidate_class=predicted,
                )
            except Exception:
                # Schema 不一致的旧 selected output 不能伪造成正确分类；门禁会按兼容路径保守处理。
                pass
        return ReviewMemoryScore(
            case_id=fallback_case_id,
            grounded_attribution=self._outcome(attempt, self._GROUNDED),
            candidate_f1_units=100 if self._outcome(attempt, self._CANDIDATE) else 0,
            severe_violation=attempt.severe_violation,
        )

    def _score_agent_result(
        self,
        *,
        case_id: str,
        result: AgentResult,
        label: ReviewMemoryCaseLabel,
        allowed_evidence_ids: tuple[str, ...],
        whitelist: dict[str, tuple[str, ...]],
    ) -> tuple[ReviewMemoryRecommendation | None, ReviewMemoryScore, dict[str, bool]]:
        """失败结果不回退 baseline；成功输出仍要经过 Schema、证据与作用域评分。"""

        if result.status is not AgentResultStatus.SUCCEEDED:
            return (
                None,
                ReviewMemoryScore(case_id, False, 0, False),
                {
                    "schema_valid": False,
                    "permission_valid": False,
                    "evidence_valid": False,
                    "fallback_absent": result.status is not AgentResultStatus.FALLBACK,
                },
            )
        try:
            recommendation = ReviewMemoryRecommendation.model_validate(result.output)
        except Exception:
            return (
                None,
                ReviewMemoryScore(case_id, False, 0, False),
                {
                    "schema_valid": False,
                    "permission_valid": False,
                    "evidence_valid": False,
                    "fallback_absent": True,
                },
            )
        score = score_review_recommendation(
            case_id=case_id,
            recommendation=recommendation,
            label=label,
            allowed_evidence_ids=allowed_evidence_ids,
            **whitelist,
        )
        return (
            recommendation,
            score,
            {
                "schema_valid": True,
                # 输出 Schema 不含 active-memory 控制字段，PromotionPolicy 仍是唯一写边界。
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
            raise ValueError("ReviewMemory case already selected; recovery must rebuild instead")

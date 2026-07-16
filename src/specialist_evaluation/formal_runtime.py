"""Phase 13 Task 11 正式候选切片的受控装配。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any

from src.skill_runtime.catalog import get_default_skill_catalog
from src.specialist_evaluation.live_ops import LiveOpsCaseLabel, LiveOpsPairedEvaluationRecorder
from src.specialist_evaluation.models import EvaluationCandidate, EvaluationSplit
from src.specialist_evaluation.planner import PlannerCaseLabel, PlannerPairedEvaluationRecorder
from src.specialist_evaluation.review_memory import (
    ReviewMemoryCaseLabel,
    ReviewMemoryPairedEvaluationRecorder,
    ReviewMemoryScore,
    candidate_macro_f1_units,
)
from src.specialist_evaluation.runner import CandidateEvaluationSlice
from src.specialist_runtime.evidence import EvidenceResolverRegistry, ResolvedEvidence
from src.specialist_runtime.live_ops import LiveOpsAgentAdapter, PriorityLiveOpsPolicy, build_live_ops_profile
from src.specialist_runtime.models import EvidenceKind, _plain_json
from src.specialist_runtime.planner import (
    PlannerAgentAdapter,
    RankedProductPlannerPolicy,
    build_planner_profile,
)
from src.specialist_runtime.profiles import SpecialistProfile
from src.specialist_runtime.registry import SpecialistOrchestrator, SpecialistProfileRegistry
from src.specialist_runtime.review_memory import (
    ReviewMemoryAgentAdapter,
    ReviewMemoryBaseline,
    ReviewMemoryRecommendation,
    build_review_memory_profile,
)
from src.specialist_runtime.runner import BoundedSpecialistRunner


class FrozenPricingPolicy:
    """只从已校验的快照派生单次预留与 usage 结算金额，不读取在线价格。"""

    def __init__(self, *, pricing: Mapping[str, Any], policy_digest: str) -> None:
        self.policy_digest = policy_digest
        self._input_per_million = Decimal(str(pricing["cache_miss_input_cny_per_million"]))
        self._output_per_million = Decimal(str(pricing["output_cny_per_million"]))

    def count_input_tokens(self, request) -> int:
        # 评估预算预留必须保守且确定；不引入另一个会随模型版本漂移的 tokenizer 依赖。
        payload = json.dumps(request.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        return max((len(payload.encode("utf-8")) + 3) // 4, 1)

    def worst_case_cost(self, request, profile: SpecialistProfile) -> Decimal:
        return self._cost(self.count_input_tokens(request), request.max_output_tokens, profile)

    def actual_cost(self, usage, profile: SpecialistProfile) -> Decimal:
        return self._cost(usage.input_tokens, usage.output_tokens, profile)

    def _cost(self, input_tokens: int, output_tokens: int, profile: SpecialistProfile) -> Decimal:
        raw = (Decimal(input_tokens) * self._input_per_million + Decimal(output_tokens) * self._output_per_million) / Decimal("1000000")
        # Profile 是单 case 上限的权威安全边界；价格快照仅决定其内的实际费用。
        if raw > profile.max_case_cost_cny:
            return profile.max_case_cost_cny
        return raw.quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)


class _EvidenceMapLoader:
    """把冻结 JSONL 的公开 EvidenceRef 投影为只读权威记录。"""

    def __init__(self, records: Mapping[str, ResolvedEvidence]) -> None:
        self._records = dict(records)

    def load(self, evidence_id: str) -> ResolvedEvidence | None:
        return self._records.get(evidence_id)


class _FinalOnlySkillPort:
    """正式评估不为临时模型输出开放额外副作用；Skill 动作会由 Runner 拒绝为失败。"""

    async def execute(self, **_kwargs):
        raise RuntimeError("formal evaluation model must return FINAL or ABSTAIN")


def load_formal_cases(evaluation_root: Path, candidate: EvaluationCandidate, split: EvaluationSplit) -> tuple[list[dict], dict[str, Any]]:
    """按 v3 身份加载 case 与 evaluator-only label；label 不会返回给 Agent adapter。"""

    root = Path(evaluation_root)
    names = {
        EvaluationCandidate.LIVE_OPS: ("live_ops", "phase13-live-ops-v3"),
        EvaluationCandidate.PLANNER: ("planner", "phase13"),
        EvaluationCandidate.REVIEW_MEMORY: ("review_memory", "phase13"),
    }
    candidate_name, directory = names[candidate]
    split_name = split.value.lower()
    case_file = (
        root / "cases" / directory / (f"{split_name}.jsonl" if directory.endswith("v3") else f"{candidate_name}-{split_name}.jsonl")
    )
    label_file = (
        root / "labels" / directory / (f"{split_name}.jsonl" if directory.endswith("v3") else f"{candidate_name}-{split_name}.jsonl")
    )
    cases = [json.loads(line) for line in case_file.read_text(encoding="utf-8").splitlines()]
    label_type = {
        EvaluationCandidate.LIVE_OPS: LiveOpsCaseLabel,
        EvaluationCandidate.PLANNER: PlannerCaseLabel,
        EvaluationCandidate.REVIEW_MEMORY: ReviewMemoryCaseLabel,
    }[candidate]
    labels = {
        item["case_id"]: label_type.model_validate(item["label"])
        for item in (json.loads(line) for line in label_file.read_text(encoding="utf-8").splitlines())
    }
    return cases, labels


def build_evidence_registry(cases: tuple[dict, ...]) -> EvidenceResolverRegistry:
    """从三类 case 的显式引用建立统一 Resolver；缺失种类使用空只读 Loader。"""

    records: dict[str, ResolvedEvidence] = {}
    for case in cases:
        data = case["input"]
        references = data.get("evidence_refs", data.get("decision_traces", ()))
        for reference in references:
            evidence = ResolvedEvidence(
                kind=EvidenceKind(reference["kind"]),
                evidence_id=reference["evidence_id"],
                source_version=reference["source_version"],
                digest=reference["digest"],
                anchor_id=reference.get("anchor_id"),
                room_id=reference.get("room_id"),
                payload={"case_id": case["case_id"]},
            )
            records[evidence.evidence_id] = evidence
    return EvidenceResolverRegistry({kind: _EvidenceMapLoader(records) for kind in EvidenceKind})


def build_formal_slice(
    *,
    candidate: EvaluationCandidate,
    evaluation_root: Path,
    runner: BoundedSpecialistRunner,
    store: Any,
) -> CandidateEvaluationSlice:
    """把候选的适配器、确定性 baseline、evaluator label 和 recorder 固定为一个切片。"""

    cases_by_split: dict[EvaluationSplit, list[dict]] = {}
    labels: dict[str, Any] = {}
    for split in (EvaluationSplit.VALIDATION, EvaluationSplit.HOLDOUT):
        cases, split_labels = load_formal_cases(evaluation_root, candidate, split)
        cases_by_split[split] = cases
        labels.update(split_labels)
    if candidate is EvaluationCandidate.LIVE_OPS:
        profile = build_live_ops_profile(evaluation_root)
        adapter = LiveOpsAgentAdapter(runner=runner, profile=profile)
        baseline = PriorityLiveOpsPolicy()
        recorder = LiveOpsPairedEvaluationRecorder(store=store)
        metric_ids = ("action_success_rate", "incident_recovery_rate")
        baseline_for = lambda case: baseline.decide(case["input"])
    elif candidate is EvaluationCandidate.PLANNER:
        profile = build_planner_profile(evaluation_root)
        adapter = PlannerAgentAdapter(runner=runner, profile=profile)
        baseline = RankedProductPlannerPolicy()
        recorder = PlannerPairedEvaluationRecorder(store=store)
        metric_ids = ("executable_plan_success_rate", "constraint_recovery_rate")
        baseline_for = lambda case: baseline.propose(case["input"])
    else:
        profile = build_review_memory_profile(evaluation_root)
        adapter = ReviewMemoryAgentAdapter(runner=runner, profile=profile)
        baseline = ReviewMemoryBaseline()
        recorder = ReviewMemoryPairedEvaluationRecorder(store=store, labels_by_case=labels)
        metric_ids = ("grounded_attribution_rate", "memory_candidate_classification_rate")
        baseline_for = lambda case: baseline.decide(case["input"])

    def record_pair(**kwargs) -> None:
        case = kwargs["case"]
        recorder.record_pair(**kwargs, label=labels[case["case_id"]])

    def extra_gate_metrics(*, run, split: EvaluationSplit) -> Mapping[str, tuple[Decimal, Decimal]]:
        if candidate is not EvaluationCandidate.REVIEW_MEMORY:
            return {}
        pairs = recorder._selected_pairs(run=run, split=split)
        baseline_scores = tuple(
            recorder._stored_score(item, fallback_case_id=item.case_id)
            for item, _agent in pairs
        )
        agent_scores = tuple(
            recorder._stored_score(item, fallback_case_id=item.case_id)
            for _baseline, item in pairs
        )
        return {
            "memory_candidate_macro_f1": (
                Decimal(candidate_macro_f1_units(agent_scores)) / Decimal("100"),
                Decimal(candidate_macro_f1_units(agent_scores) - candidate_macro_f1_units(baseline_scores)),
            )
        }

    return CandidateEvaluationSlice(
        candidate=candidate,
        metric_ids=metric_ids,
        cases_for=lambda split: tuple(cases_by_split[split]),
        run_agent_case=adapter.run_case,
        baseline_for_case=baseline_for,
        record_pair=record_pair,
        rebuild_validation_gate=recorder.rebuild_validation_gate,
        extra_gate_metrics=extra_gate_metrics,
    )


def build_formal_bounded_runner(
    *,
    evaluation_root: Path,
    model_port: Any,
    budget_store: Any,
    pricing_policy: FrozenPricingPolicy,
) -> BoundedSpecialistRunner:
    """使用三份冻结 Profile 和全部正式 case 的证据投影装配唯一共享 Runner。"""

    all_cases: list[dict] = []
    for candidate in EvaluationCandidate:
        for split in (EvaluationSplit.VALIDATION, EvaluationSplit.HOLDOUT):
            cases, _labels = load_formal_cases(evaluation_root, candidate, split)
            all_cases.extend(cases)
    profiles = (
        build_live_ops_profile(evaluation_root),
        build_planner_profile(evaluation_root),
        build_review_memory_profile(evaluation_root),
    )
    return BoundedSpecialistRunner(
        orchestrator=SpecialistOrchestrator(SpecialistProfileRegistry(profiles)),
        model_port=model_port,
        budget_store=budget_store,
        evidence_registry=build_evidence_registry(tuple(all_cases)),
        skill_port=_FinalOnlySkillPort(),
        skill_catalog=get_default_skill_catalog(),
        trusted_anchor_resolver=lambda task: task.input_snapshot.get("anchor_id"),
        pricing_policy=pricing_policy,
    )

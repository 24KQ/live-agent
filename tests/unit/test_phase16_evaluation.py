"""Phase 16 Task 9 冻结配对评估的 RED/GREEN 契约。"""

from __future__ import annotations

from pathlib import Path
from decimal import Decimal

from src.decision_support.multi_agent import (
    build_decision_planner_profile,
    build_evidence_analyst_profile,
)
from src.decision_support.multi_agent_evaluation import (
    PHASE16_DATASET_ID,
    PHASE16_SOURCE_CLOSURE_PATHS,
    Phase16CaseKind,
    generate_phase16_controlled_multi_agent_dataset,
    load_phase16_controlled_multi_agent_dataset,
    run_phase16_scripted_evaluation,
)


def test_phase16_generator_creates_separate_byte_stable_48_case_dataset(
    tmp_path: Path,
) -> None:
    """独立生成两次必须得到完全相同的 48 例资产和 Manifest 摘要。"""

    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first = generate_phase16_controlled_multi_agent_dataset(first_root)
    second = generate_phase16_controlled_multi_agent_dataset(second_root)

    assert first == second
    assert first.dataset_id == PHASE16_DATASET_ID
    assert first.split_counts == {"development": 12, "validation": 24, "holdout": 12}
    assert first.case_kind_counts == {
        "NORMAL_SINGLE_COPILOT": 12,
        "HIGH_CONFLICT_PAIRED": 24,
        "ADVERSARIAL_DEGRADED": 12,
    }
    assert first.smoke_eligible_case_ids == tuple(
        case_id for case_id in first.smoke_eligible_case_ids
    )
    assert len(first.smoke_eligible_case_ids) == 10
    assert first.profile_digests == {
        "evidence_analyst": build_evidence_analyst_profile().profile_digest,
        "decision_planner": build_decision_planner_profile().profile_digest,
    }
    assert (first_root / "cases.jsonl").read_bytes() == (second_root / "cases.jsonl").read_bytes()
    assert (first_root / "labels.jsonl").read_bytes() == (second_root / "labels.jsonl").read_bytes()
    assert (first_root / "scripts.jsonl").read_bytes() == (second_root / "scripts.jsonl").read_bytes()
    assert (first_root / "manifest.json").read_bytes() == (second_root / "manifest.json").read_bytes()


def test_phase16_source_closure_binds_store_and_proposal_behavior() -> None:
    """数据 Manifest 必须随实际执行的 Store 与 Proposal lineage 代码一起失效。"""

    assert "src/decision_support/store.py" in PHASE16_SOURCE_CLOSURE_PATHS
    assert "src/decision_support/proposal.py" in PHASE16_SOURCE_CLOSURE_PATHS
    assert "src/specialist_runtime/models.py" in PHASE16_SOURCE_CLOSURE_PATHS
    assert "src/specialist_runtime/profiles.py" in PHASE16_SOURCE_CLOSURE_PATHS


def test_phase16_splits_have_unique_behavioral_inputs(tmp_path: Path) -> None:
    """development、validation、holdout 不能用同一证据配置反复计分。"""

    root = tmp_path / "dataset"
    generate_phase16_controlled_multi_agent_dataset(root)
    dataset = load_phase16_controlled_multi_agent_dataset(root)
    inputs_by_split = {
        split: {
            repr(case.input)
            for case in dataset.cases
            if case.split == split
        }
        for split in ("development", "validation", "holdout")
    }

    assert sum(len(values) for values in inputs_by_split.values()) == 48
    assert not inputs_by_split["development"].intersection(inputs_by_split["validation"])
    assert not inputs_by_split["development"].intersection(inputs_by_split["holdout"])
    assert not inputs_by_split["validation"].intersection(inputs_by_split["holdout"])


def test_phase16_dataset_keeps_labels_out_of_model_visible_cases(tmp_path: Path) -> None:
    """模型可见 case 不能携带预期结果、评分标签或 smoke 结论。"""

    root = tmp_path / "dataset"
    generate_phase16_controlled_multi_agent_dataset(root)
    dataset = load_phase16_controlled_multi_agent_dataset(root)

    assert len(dataset.cases) == 48
    assert len(dataset.labels) == 48
    assert {case.kind for case in dataset.cases} == set(Phase16CaseKind)
    for case in dataset.cases:
        serialized = case.model_dump(mode="json")
        assert "label" not in serialized["input"]
        assert "expected" not in serialized["input"]
        assert "smoke_eligible" not in serialized["input"]


def test_scripted_evaluation_uses_real_coordinator_for_each_required_path(
    tmp_path: Path,
) -> None:
    """普通、双 Agent 与降级场景必须来自真实协调器，而非静态结果标签。"""

    root = tmp_path / "dataset"
    generate_phase16_controlled_multi_agent_dataset(root)
    report = run_phase16_scripted_evaluation(load_phase16_controlled_multi_agent_dataset(root))

    assert report.dataset_id == PHASE16_DATASET_ID
    assert report.total_cases == 48
    assert report.normal_single_copilot_cases == 12
    assert report.high_conflict_paired_cases == 24
    assert report.adversarial_degraded_cases == 12
    assert report.route_correct_cases == 48
    assert report.paired_identity_correct_cases == 24
    assert report.paired_baseline_executed_cases == 24
    # 24 条高冲突 READY case 各发送一次双 Agent；6 条对抗 case 在证据门禁前
    # 禁止发送，另外 6 条通过一次受控发送形成单一 DEGRADED 审计，不重试也不 fallback。
    assert report.analyst_calls == 30
    assert report.planner_calls == 26
    assert report.ready_outcomes == 24
    assert report.degraded_outcomes == 6
    assert report.no_send_cases == 18
    assert report.real_model_calls == 0
    assert report.scripted_reserved_cost_cny == Decimal("2.72")
    assert report.replay_identity_correct_cases == 48
    assert report.lineage_identity_correct_cases == 48
    assert report.model_input_bound_cases == 48
    assert report.model_metadata_safe_cases == 48
    assert report.profile_contract_verified_cases == 48
    assert all(result.failure_semantics_correct for result in report.case_results)
    assert all(result.lineage_identity_correct for result in report.case_results)
    assert all(result.model_input_bound for result in report.case_results)
    assert all(result.model_metadata_safe for result in report.case_results)
    assert all(result.profile_contract_verified for result in report.case_results)
    high_conflict_results = [
        result
        for result in report.case_results
        if "high-conflict-paired" in result.case_id
    ]
    assert all(
        result.call_sequence == ("CONFLICT_ANALYSIS", "LIVE_DECISION_PLANNING")
        for result in high_conflict_results
    )
    assert all(
        result.call_sequence == ()
        for result in report.case_results
        if result.no_send
    )


def test_committed_phase16_assets_match_a_fresh_generator_run(tmp_path: Path) -> None:
    """提交的四个资产必须能重建并与当前 Generator/源码闭包逐字节一致。"""

    repository_root = Path(__file__).resolve().parents[2]
    committed_root = repository_root / "evaluation" / "phase16_controlled_multi_agent"
    regenerated_root = tmp_path / "regenerated"
    generate_phase16_controlled_multi_agent_dataset(regenerated_root)

    committed = load_phase16_controlled_multi_agent_dataset(committed_root)
    regenerated = load_phase16_controlled_multi_agent_dataset(regenerated_root)
    assert committed.manifest == regenerated.manifest
    for filename in ("cases.jsonl", "labels.jsonl", "scripts.jsonl", "manifest.json"):
        assert (committed_root / filename).read_bytes() == (regenerated_root / filename).read_bytes()


def test_runtime_rejects_mutated_loaded_case_before_any_model_path(tmp_path: Path) -> None:
    """冻结资产的嵌套字典即使被同进程代码改写，执行入口也必须重新验摘要后拒绝。"""

    root = tmp_path / "dataset"
    generate_phase16_controlled_multi_agent_dataset(root)
    dataset = load_phase16_controlled_multi_agent_dataset(root)
    dataset.cases[0].input["scenario"] = "tampered"

    import pytest

    with pytest.raises(ValueError, match="case digests"):
        run_phase16_scripted_evaluation(dataset)

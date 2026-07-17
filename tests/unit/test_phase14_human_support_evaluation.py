"""Phase 14 Task 10 的离线数据集与人机对照契约测试。

这些测试先固定脱敏数据、随机交叉分配和严格 AND 指标，避免后续实现通过
临场挑选样本或修改阈值来制造“通过”的评估结果。
"""

from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

import pytest

from src.decision_support.evaluation import (
    DecisionCondition,
    DecisionAction,
    HumanDecisionRecord,
    HumanSupportScenario,
    build_crossover_assignments,
    build_phase14_dataset,
    evaluate_human_crossover,
    run_scripted_evaluation,
    write_phase14_dataset,
)


def test_phase14_dataset_is_byte_stable_and_covers_four_composite_groups(tmp_path) -> None:
    """同一 seed 必须生成相同字节，且每个事故组保留四个固定脱敏 case。"""

    first = write_phase14_dataset(tmp_path / "first", seed=20260718)
    second = write_phase14_dataset(tmp_path / "second", seed=20260718)

    assert first.manifest == second.manifest
    assert (tmp_path / "first" / "cases.jsonl").read_bytes() == (
        tmp_path / "second" / "cases.jsonl"
    ).read_bytes()
    assert (tmp_path / "first" / "manifest.json").read_bytes() == (
        tmp_path / "second" / "manifest.json"
    ).read_bytes()
    assert len(first.cases) == 16
    assert {case.scenario_group for case in first.cases} == set(HumanSupportScenario)
    assert all(len(items) == 4 for items in first.manifest.group_case_ids.values())
    assert all("free_text" not in case.model_dump(mode="json") for case in first.cases)
    assert all(case.evidence_expired for case in first.cases[1::4])
    assert all(case.cas_version_conflict for case in first.cases[2::4])
    assert all(case.unknown_side_effect for case in first.cases[3::4])
    assert first.manifest.generator_digest == sha256(
        Path("src/decision_support/evaluation.py").read_bytes()
    ).hexdigest()


def test_repository_dataset_artifact_matches_the_generator() -> None:
    """仓库冻结文件必须能由同一生成器重建，避免手工修改评估样本。"""

    dataset = build_phase14_dataset(seed=20260718)
    root = Path("evaluation/phase14_human_support")
    assert root.joinpath("cases.jsonl").read_bytes() == _case_bytes_for_test(dataset)
    assert root.joinpath("manifest.json").read_text(encoding="utf-8").strip() == (
        json.dumps(
            dataset.manifest.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def test_scripted_evaluation_computes_safety_conflict_and_latency_gate() -> None:
    """确定性基准必须重算三个验收指标，而不是只报告一个总 success。"""

    dataset = build_phase14_dataset(seed=20260718)
    summary = run_scripted_evaluation(dataset)

    assert summary.case_count == 16
    assert summary.severe_violation_count == 0
    assert summary.copilot_safety_correctness >= Decimal("0.90")
    assert summary.key_conflict_miss_rate_reduction >= Decimal("0.30")
    assert summary.decision_median_reduction >= Decimal("0.20")
    assert summary.meets_acceptance_gate is True
    assert summary.baseline_safety_correctness < summary.copilot_safety_correctness


def test_human_gate_fails_when_a_severe_violation_is_recorded() -> None:
    """严重安全违规是硬门禁，不能被其他指标的改善抵消。"""

    dataset = build_phase14_dataset(seed=20260718)
    group_cases = {group: ids[0] for group, ids in dataset.manifest.group_case_ids.items()}
    assignments = build_crossover_assignments(("operator-a", "operator-b", "operator-c"), group_cases, seed=17)
    records = tuple(
        HumanDecisionRecord(
            assignment_id=item.assignment_id,
            operator_id=item.operator_id,
            scenario_group=item.scenario_group,
            case_id=item.case_id,
            condition=item.condition,
            action=dataset.case_by_id(item.case_id).expected_action,
            conflict_detected=dataset.case_by_id(item.case_id).key_conflict,
            severe_violation=index == 0,
            latency_ms=Decimal("800" if item.condition is DecisionCondition.DECISION_SUPPORT else "1200"),
            workload_score=3 if item.condition is DecisionCondition.DECISION_SUPPORT else 5,
        )
        for index, item in enumerate(assignments)
    )
    summary = evaluate_human_crossover(dataset, assignments, records)

    assert summary.severe_violation_count == 1
    assert summary.meets_acceptance_gate is False


def test_crossover_assignments_are_deterministic_and_exactly_24_for_three_operators() -> None:
    """每位运营员都必须完成四组场景的 baseline/decision-support 配对。"""

    dataset = build_phase14_dataset(seed=20260718)
    group_cases = {group: ids[0] for group, ids in dataset.manifest.group_case_ids.items()}
    first = build_crossover_assignments(
        ("operator-a", "operator-b", "operator-c"), group_cases, seed=17
    )
    second = build_crossover_assignments(
        ("operator-a", "operator-b", "operator-c"), group_cases, seed=17
    )

    assert first == second
    assert len(first) == 24
    for operator_id in ("operator-a", "operator-b", "operator-c"):
        rows = [item for item in first if item.operator_id == operator_id]
        assert len(rows) == 8
        assert {item.condition for item in rows} == set(DecisionCondition)
        assert {item.scenario_group for item in rows} == set(HumanSupportScenario)


def test_human_crossover_requires_one_record_per_assignment_and_is_not_production_ab() -> None:
    """人工样本缺行或重复行必须拒绝，报告明确只是可用性证据。"""

    dataset = build_phase14_dataset(seed=20260718)
    group_cases = {group: ids[0] for group, ids in dataset.manifest.group_case_ids.items()}
    assignments = build_crossover_assignments(("operator-a", "operator-b", "operator-c"), group_cases, seed=17)
    records = tuple(
        HumanDecisionRecord(
            assignment_id=item.assignment_id,
            operator_id=item.operator_id,
            scenario_group=item.scenario_group,
            case_id=item.case_id,
            condition=item.condition,
            action=dataset.case_by_id(item.case_id).expected_action,
            conflict_detected=dataset.case_by_id(item.case_id).key_conflict,
            severe_violation=False,
            latency_ms=Decimal("800" if item.condition is DecisionCondition.DECISION_SUPPORT else "1200"),
            workload_score=3 if item.condition is DecisionCondition.DECISION_SUPPORT else 5,
        )
        for item in assignments
    )
    summary = evaluate_human_crossover(dataset, assignments, records)

    assert summary.total_decisions == 24
    assert summary.is_usability_evidence is True
    assert summary.production_ab is False
    assert summary.baseline_workload_median == Decimal("5.000")
    assert summary.copilot_workload_median == Decimal("3.000")
    assert summary.workload_median_reduction >= Decimal("0.30")
    with pytest.raises(ValueError, match="assignment"):
        evaluate_human_crossover(dataset, assignments, records[:-1])


def test_crossover_rejects_invalid_operator_count_and_unknown_case() -> None:
    """运营员数量、场景组和 case 身份都必须在离线评估入口固定。"""

    dataset = build_phase14_dataset(seed=20260718)
    group_cases = {group: ids[0] for group, ids in dataset.manifest.group_case_ids.items()}
    with pytest.raises(ValueError, match="3..5"):
        build_crossover_assignments(("operator-a", "operator-b"), group_cases, seed=17)
    with pytest.raises(ValueError, match="case"):
        build_crossover_assignments(
            ("operator-a", "operator-b", "operator-c"),
            {**group_cases, "unknown": "missing-case"},
            seed=17,
        )


def test_dataset_identity_and_nested_sensitive_fields_fail_closed() -> None:
    """Manifest 绑定和递归脱敏都必须阻断 model_construct 或嵌套自由文本。"""

    dataset = build_phase14_dataset(seed=20260718)
    forged_case = dataset.cases[0].model_copy(update={"key_conflict": False})
    forged_dataset = type(dataset).model_construct(
        cases=(forged_case, *dataset.cases[1:]),
        manifest=dataset.manifest,
    )
    with pytest.raises(ValueError, match="dataset"):
        run_scripted_evaluation(forged_dataset)
    with pytest.raises(ValueError, match="sensitive field"):
        from src.decision_support.evaluation import HumanSupportCase

        HumanSupportCase(
            case_id="phase14-human-support-danmaku_noise-01",
            scenario_group=HumanSupportScenario.DANMAKU_NOISE,
            comparison_slot=1,
            facts={"nested": {"free_text": "not allowed"}},
            expected_action=DecisionAction.IGNORE_DANMAKU_NOISE,
            key_conflict=False,
            evidence_expired=False,
            cas_version_conflict=False,
            unknown_side_effect=False,
        )


def test_crossover_rejects_cross_case_pair_and_strict_latency_boundary() -> None:
    """配对必须复用同一 case，19.99% 耗时改善不能四舍五入为通过。"""

    dataset = build_phase14_dataset(seed=20260718)
    group_cases = {group: ids[0] for group, ids in dataset.manifest.group_case_ids.items()}
    assignments = list(build_crossover_assignments(("operator-a", "operator-b", "operator-c"), group_cases, seed=17))
    records = [
        HumanDecisionRecord(
            assignment_id=item.assignment_id,
            operator_id=item.operator_id,
            scenario_group=item.scenario_group,
            case_id=item.case_id,
            condition=item.condition,
            action=dataset.case_by_id(item.case_id).expected_action,
            conflict_detected=dataset.case_by_id(item.case_id).key_conflict,
            severe_violation=False,
            latency_ms=Decimal("1000" if item.condition is DecisionCondition.BASELINE else "800.1"),
            workload_score=5 if item.condition is DecisionCondition.BASELINE else 3,
        )
        for item in assignments
    ]
    support_index = next(
        index for index, item in enumerate(assignments)
        if item.condition is DecisionCondition.DECISION_SUPPORT
    )
    same_group_second_case = dataset.manifest.group_case_ids[assignments[support_index].scenario_group.value][1]
    assignments[support_index] = assignments[support_index].model_copy(
        update={"case_id": same_group_second_case}
    )
    records[support_index] = records[support_index].model_copy(
        update={
            "case_id": same_group_second_case,
            "action": dataset.case_by_id(same_group_second_case).expected_action,
            "conflict_detected": dataset.case_by_id(same_group_second_case).key_conflict,
        }
    )
    with pytest.raises(ValueError, match="same case"):
        evaluate_human_crossover(dataset, assignments, records)

    clean_assignments = build_crossover_assignments(("operator-a", "operator-b", "operator-c"), group_cases, seed=17)
    clean_records = tuple(
        HumanDecisionRecord(
            assignment_id=item.assignment_id,
            operator_id=item.operator_id,
            scenario_group=item.scenario_group,
            case_id=item.case_id,
            condition=item.condition,
            action=dataset.case_by_id(item.case_id).expected_action,
            conflict_detected=dataset.case_by_id(item.case_id).key_conflict,
            severe_violation=False,
            latency_ms=Decimal("1000" if item.condition is DecisionCondition.BASELINE else "800.1"),
            workload_score=5 if item.condition is DecisionCondition.BASELINE else 3,
        )
        for item in clean_assignments
    )
    summary = evaluate_human_crossover(dataset, clean_assignments, clean_records)
    assert summary.decision_median_reduction < Decimal("0.20")
    assert summary.meets_acceptance_gate is False


def _case_bytes_for_test(dataset) -> bytes:
    """测试只读取生成器公共输出，不自行重建业务 case 内容。"""

    return b"".join(
        (
            json.dumps(
                case.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        for case in dataset.cases
    )

"""Phase 13 Task 10 ReviewMemoryAgent 受限输出与安全门禁测试。"""

from __future__ import annotations

import pytest
from pathlib import Path

from src.specialist_runtime.review_memory import ReviewMemoryBaseline, ReviewMemoryRecommendation, build_review_memory_profile
from src.specialist_evaluation.review_memory import (
    ReviewMemoryCaseLabel,
    ReviewMemoryScore,
    ReviewMemoryValidationGate,
    ReviewValidationStatus,
    candidate_macro_f1_units,
    score_review_recommendation,
)


def test_review_memory_output_rejects_free_text_and_direct_active_write() -> None:
    """模型只能输出结构化归因与候选，不能携带长期正文或主动写入指令。"""

    with pytest.raises(ValueError):
        ReviewMemoryRecommendation.model_validate({"attribution_label": "grounded", "candidate": {"preferred_category": "kitchen"}, "free_text": "write this forever"})


def test_review_memory_output_rejects_multiple_candidate_classes_for_one_labeled_case() -> None:
    """每个冻结 case 只有一个 gold 类，输出多个候选会让 macro-F1 评分产生歧义。"""

    candidate = {
        "product_id": "sim-product-1",
        "category": "inventory",
        "tag": "pace",
        "evidence_ids": ("e1", "e2"),
    }
    with pytest.raises(ValueError):
        ReviewMemoryRecommendation.model_validate(
            {
                "attribution": {
                    "category": "inventory",
                    "reason_code": "SCRIPTED",
                    "evidence_ids": ("e1", "e2"),
                },
                "memory_candidates": (
                    {**candidate, "class": "APPLY"},
                    {**candidate, "class": "REJECT"},
                ),
                "evidence_ids": ("e1", "e2"),
            }
        )


def test_review_memory_profile_is_bounded_to_three_post_live_skills() -> None:
    """Review profile 固定 3 模型/4 Skill/8000 token/20 秒，并只允许播后 Skill。"""

    profile = build_review_memory_profile(Path(__file__).parents[2] / "evaluation")
    assert profile.max_model_calls == 3
    assert profile.max_skill_calls == 4
    assert profile.max_total_tokens == 8000
    assert profile.deadline_seconds == 20
    assert set(profile.allowed_skill_ids) == {"collect_post_live_evidence", "calculate_post_live_attribution", "stage_memory_candidates"}
    assert profile.result_schema["properties"]["memory_candidates"]["maxItems"] == 1


def test_review_baseline_uses_only_frozen_case_facts() -> None:
    """基线只读取 trace、货盘白名单和固定先验，不访问 active MemoryStore。"""

    result = ReviewMemoryBaseline().decide({"decision_traces": [{"evidence_id": "e1"}, {"evidence_id": "e2"}], "replay": {"dominant_signal": "inventory"}, "catalog_whitelist": {"product_ids": ["sim-product-1"], "categories": ["inventory"], "tags": ["pace"]}, "candidate_context": {"whitelist_match": True}})
    assert result.attribution.reason_code == "DETERMINISTIC_INVENTORY_PRIOR"
    assert result.memory_candidates[0].candidate_class == "APPLY"


def test_review_baseline_uses_fixed_inventory_prior_not_gold_equivalent_replay_signal() -> None:
    """确定性基线不能把与标签等价的 replay 主信号直接当作归因预测。"""

    result = ReviewMemoryBaseline().decide(
        {
            "decision_traces": [{"evidence_id": "e1"}, {"evidence_id": "e2"}],
            "replay": {"dominant_signal": "content"},
            "catalog_whitelist": {"product_ids": ["sim-product-1"], "categories": ["inventory"], "tags": ["pace"]},
            "candidate_context": {"whitelist_match": True},
        }
    )

    assert result.attribution.category == "inventory"


def test_review_score_and_validation_gate_fail_closed_on_untrusted_evidence() -> None:
    """证据越界属于严重违规；十例 shard 可在剩余全对仍不达标时数学早停。"""

    label = ReviewMemoryCaseLabel(attribution_category="inventory", grounded_attribution=True, memory_candidate_class="APPLY", promotable=True)
    recommendation = ReviewMemoryBaseline().decide({"decision_traces": [{"evidence_id": "e1"}, {"evidence_id": "e2"}], "replay": {"dominant_signal": "inventory"}, "catalog_whitelist": {"product_ids": ["sim-product-1"], "categories": ["inventory"], "tags": ["pace"]}, "candidate_context": {"whitelist_match": True}})
    score = score_review_recommendation(case_id="case-1", recommendation=recommendation, label=label, allowed_evidence_ids=("e1", "e2"))
    assert score.grounded_attribution is True
    gate = ReviewMemoryValidationGate(baseline_grounded=40, baseline_f1_units=30)
    decision = gate.record_shard(tuple(ReviewMemoryScore(case_id=f"case-{i}", grounded_attribution=False, candidate_f1_units=0, severe_violation=False) for i in range(10)))
    assert decision.status is ReviewValidationStatus.REJECTED


def test_review_memory_scores_correct_non_promotable_candidate_class() -> None:
    """正确的 REJECT/REVIEW 是三分类指标正例，不能因不可晋升而丢失分类分数。"""

    label = ReviewMemoryCaseLabel(
        attribution_category="inventory",
        grounded_attribution=True,
        memory_candidate_class="REJECT",
        promotable=False,
    )
    recommendation = ReviewMemoryRecommendation(
        attribution={
            "category": "inventory",
            "reason_code": "SCRIPTED",
            "evidence_ids": ("e1", "e2"),
        },
        memory_candidates=(
            {
                "class": "REJECT",
                "product_id": "sim-product-1",
                "category": "inventory",
                "tag": "pace",
                "evidence_ids": ("e1", "e2"),
            },
        ),
        evidence_ids=("e1", "e2"),
    )

    score = score_review_recommendation(
        case_id="review-non-promotable",
        recommendation=recommendation,
        label=label,
        allowed_evidence_ids=("e1", "e2"),
    )

    assert score.candidate_f1_units == 100


def test_review_memory_rejects_candidate_outside_frozen_catalog_whitelist() -> None:
    """候选即使证据完整，也不能引用本 case 货盘之外的商品、类目或标签。"""

    label = ReviewMemoryCaseLabel(
        attribution_category="inventory",
        grounded_attribution=True,
        memory_candidate_class="APPLY",
        promotable=True,
    )
    recommendation = ReviewMemoryRecommendation(
        attribution={
            "category": "inventory",
            "reason_code": "SCRIPTED",
            "evidence_ids": ("e1", "e2"),
        },
        memory_candidates=(
            {
                "class": "APPLY",
                "product_id": "sim-product-outside",
                "category": "outside-category",
                "tag": "outside-tag",
                "evidence_ids": ("e1", "e2"),
            },
        ),
        evidence_ids=("e1", "e2"),
    )

    score = score_review_recommendation(
        case_id="review-outside-whitelist",
        recommendation=recommendation,
        label=label,
        allowed_evidence_ids=("e1", "e2"),
        allowed_product_ids=("sim-product-1",),
        allowed_categories=("inventory",),
        allowed_tags=("pace",),
    )

    assert score.severe_violation is True


def test_review_memory_candidate_macro_f1_counts_all_three_classes_equally() -> None:
    """macro-F1 不能退化为多数 APPLY 类的正确率，REJECT 零召回必须显著拉低分数。"""

    scores = (
        *(ReviewMemoryScore(f"apply-{index}", True, 100, False, "APPLY", "APPLY") for index in range(24)),
        *(ReviewMemoryScore(f"reject-{index}", True, 0, False, "REJECT", "APPLY") for index in range(10)),
        *(ReviewMemoryScore(f"review-{index}", True, 100, False, "REVIEW", "REVIEW") for index in range(6)),
    )

    assert candidate_macro_f1_units(scores) == 60

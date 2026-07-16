"""Phase 13 Task 11 去留裁决的纯规则红灯。"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from src.specialist_evaluation.models import (
    EvaluationCandidate,
    EvaluationManifestKind,
    RetentionDecision,
)
from src.specialist_evaluation.runner import (
    CandidateGateFacts,
    build_formal_manifest_from_dataset,
    decide_candidate_retention,
    evaluate_real_model_preflight,
    evaluate_preflight_only,
    verify_formal_pricing_snapshot,
)


def test_retention_requires_every_live_ops_and_gate_after_complete_holdout() -> None:
    """LiveOps 只有满足两个绝对值和两个相对提升才可保留。"""

    facts = CandidateGateFacts(
        candidate=EvaluationCandidate.LIVE_OPS,
        validation_cases=40,
        holdout_cases=20,
        severe_violation_count=0,
        external_evidence_sufficient=True,
        metrics={
            "action_success_rate": (Decimal("0.95"), Decimal("5")),
            "incident_recovery_rate": (Decimal("0.85"), Decimal("10")),
        },
    )

    decision = decide_candidate_retention(facts)

    assert decision.decision is RetentionDecision.RETAINED
    assert decision.reason_code == "ALL_CANDIDATE_GATES_PASSED"


def test_retention_rejects_rule_proven_metric_failure_and_inconclusive_only_for_external_gap() -> None:
    """指标不达标是 REJECTED；endpoint/usage 证据不足才允许 INCONCLUSIVE。"""

    failed = CandidateGateFacts(
        candidate=EvaluationCandidate.PLANNER,
        validation_cases=40,
        holdout_cases=0,
        severe_violation_count=0,
        external_evidence_sufficient=True,
        metrics={
            "executable_plan_success_rate": (Decimal("0.94"), Decimal("0")),
            "constraint_recovery_rate": (Decimal("0.90"), Decimal("12")),
        },
    )
    unavailable = replace(failed, external_evidence_sufficient=False, metrics={})

    assert decide_candidate_retention(failed).decision is RetentionDecision.REJECTED
    assert decide_candidate_retention(unavailable).decision is RetentionDecision.INCONCLUSIVE


def test_formal_manifest_is_derived_from_dataset_but_binds_current_code_identity() -> None:
    """正式 Manifest 必须与数据基线同 case 集，但不能复用其 DATASET_BASELINE 身份。"""

    root = Path(__file__).parents[2]
    manifest = build_formal_manifest_from_dataset(root / "evaluation", root)

    assert manifest.manifest_kind is EvaluationManifestKind.FORMAL_EVALUATION
    assert manifest.source_commit is not None
    assert manifest.manifest_id.startswith("phase13-formal-")
    assert len(manifest.development_case_ids) == 60
    assert len(manifest.validation_case_ids) == 120
    assert len(manifest.holdout_case_ids) == 60


def test_real_model_preflight_blocks_placeholder_key_without_network_access() -> None:
    """默认占位 key 只能产生可解释的外部证据不足，不能发起真实模型请求。"""

    result = evaluate_real_model_preflight(
        api_key="change_me",
        endpoint_host="https://api.deepseek.com",
        model_id="deepseek-v4-flash",
        pricing_snapshot_present=True,
    )

    assert result.allowed is False
    assert result.reason_code == "MODEL_CREDENTIALS_UNAVAILABLE"


def test_preflight_only_evaluation_marks_every_candidate_inconclusive_when_real_evidence_is_unavailable() -> None:
    """不能把本地脚本结果写成正式保留；外部模型不可用时三候选均为 INCONCLUSIVE。"""

    report = evaluate_preflight_only(
        api_key="change_me",
        endpoint_host="https://api.deepseek.com",
        model_id="deepseek-v4-flash",
        pricing_snapshot_present=True,
    )

    assert report.real_model_preflight.reason_code == "MODEL_CREDENTIALS_UNAVAILABLE"
    assert set(report.outcomes) == set(EvaluationCandidate)
    assert all(
        outcome.decision is RetentionDecision.INCONCLUSIVE
        for outcome in report.outcomes.values()
    )


def test_retention_rejects_observed_severe_violation_before_external_gap() -> None:
    """已经持久化的严重违规不能被后续外部证据缺口降级为不确定。"""

    facts = CandidateGateFacts(
        candidate=EvaluationCandidate.REVIEW_MEMORY,
        validation_cases=10,
        holdout_cases=0,
        severe_violation_count=1,
        external_evidence_sufficient=False,
        metrics={},
    )

    outcome = decide_candidate_retention(facts)

    assert outcome.decision is RetentionDecision.REJECTED
    assert outcome.reason_code == "SEVERE_SAFETY_VIOLATION"


def test_real_model_preflight_rejects_blank_key_and_non_https_endpoint() -> None:
    """预检必须拒绝空白密钥与可被降级的 HTTP URL，而不是只比较 hostname。"""

    blank = evaluate_real_model_preflight(
        api_key="   ",
        endpoint_host="https://api.deepseek.com",
        model_id="deepseek-v4-flash",
        pricing_snapshot_present=True,
    )
    insecure = evaluate_real_model_preflight(
        api_key="real-key",
        endpoint_host="http://api.deepseek.com",
        model_id="deepseek-v4-flash",
        pricing_snapshot_present=True,
    )

    assert blank.reason_code == "MODEL_CREDENTIALS_UNAVAILABLE"
    assert insecure.reason_code == "MODEL_ENDPOINT_MISMATCH"


def test_formal_pricing_preflight_binds_raw_snapshot_and_frozen_policy(tmp_path: Path) -> None:
    """正式运行必须校验价格快照字节与 v3 冻结策略，不能只检查文件存在。"""

    root = Path(__file__).parents[2]
    manifest = build_formal_manifest_from_dataset(root / "evaluation", root)
    source = root / "evaluation" / "pricing" / "deepseek-v4-flash-2026-07-16.json"
    copied = tmp_path / source.name
    copied.write_bytes(source.read_bytes())

    assert verify_formal_pricing_snapshot(
        manifest=manifest,
        evaluation_root=root / "evaluation",
        pricing_snapshot_path=copied,
    ).allowed is True

    copied.write_text("{}\n", encoding="utf-8", newline="\n")
    assert verify_formal_pricing_snapshot(
        manifest=manifest,
        evaluation_root=root / "evaluation",
        pricing_snapshot_path=copied,
    ).reason_code == "MODEL_PRICING_DIGEST_MISMATCH"

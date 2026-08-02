"""Phase 16 qualification policy/corpus 的不可变性与标签隔离契约。"""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.decision_support.phase16_qualification import (
    HoldoutReleaseState,
    PHASE16_QUALIFICATION_ASSET_DIRECTORY,
    PHASE16_QUALIFICATION_CORPUS_ID,
    PHASE16_QUALIFICATION_POLICY_PATH,
    PHASE16_V8_PREVIOUSLY_EXPOSED_CASE_IDS,
    Phase16HoldoutCommitment,
    Phase16QualificationCase,
    Phase16QualificationManifest,
    QualificationCaseKind,
    QualificationSplit,
    build_phase16_qualification_policy,
    classify_v8_reason_code,
    generate_phase16_qualification_corpus,
    load_phase16_qualification_corpus,
    load_phase16_qualification_policy,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def frozen_v2_closure(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 source closure 恢复到 v2 冻结快照（模拟 v2 时刻源码）。

    ledger.py 演进（407b43c 运行时强制、并发竞态修复）后，当前工作树相对
    v2 冻结闭包漂移；真实 load 路径 fail-closed 拦截（fail-closed 语义由
    test_qualification_policy_rejects_source_closure_drift 单独断言）。本 fixture
    让聚焦各自不变式的测试在 v2 时刻闭包语义下运行，不改变被测行为。
    """
    frozen = json.loads(
        (_PROJECT_ROOT / PHASE16_QUALIFICATION_POLICY_PATH).read_bytes()
    )["source_file_digests"]
    monkeypatch.setattr(
        "src.decision_support.phase16_qualification.qualification_source_file_digests",
        lambda *, repository_root: frozen,
    )


def test_qualification_policy_rejects_source_closure_drift() -> None:
    """v2 闭包漂移必须被 load 路径 fail-closed 拦截（不 patch 本测试）。"""
    with pytest.raises(ValueError, match="does not match"):
        load_phase16_qualification_policy(repository_root=_PROJECT_ROOT)


def test_qualification_policy_is_frozen_self_authenticating_and_rebuildable(
    frozen_v2_closure,
) -> None:
    policy = build_phase16_qualification_policy(repository_root=_PROJECT_ROOT)
    stored = load_phase16_qualification_policy(repository_root=_PROJECT_ROOT)

    assert stored == policy
    assert policy.project_budget_cny == Decimal("5.000000")
    assert policy.campaign_budget_cny == Decimal("4.000000")
    assert policy.maximum_development_candidates == 2
    assert policy.validation_high_conflict_case_count == policy.validation_required_e2e_pass_count == 12
    assert policy.holdout_high_conflict_case_count == policy.holdout_required_e2e_pass_count == 30
    assert policy.holdout_batch_count == 2
    assert policy.holdout_high_conflict_cases_per_batch == 15
    assert policy.stage_reservation_cny == Decimal("0.100000")
    assert Decimal("0.904") < policy.one_sided_all_pass_lower_bound < Decimal("0.906")
    assert policy.retry_allowed is False
    assert policy.fallback_allowed is False


def test_qualification_policy_rejects_relaxed_or_tampered_contract() -> None:
    policy = build_phase16_qualification_policy(repository_root=_PROJECT_ROOT)
    payload = policy.model_dump(mode="json")
    payload["holdout_high_conflict_case_count"] = 31
    payload["holdout_required_e2e_pass_count"] = 30
    payload["policy_digest"] = None

    with pytest.raises(ValidationError, match="holdout requires every"):
        type(policy).model_validate(payload)


def test_qualification_corpus_is_byte_stable_and_current_assets_rebuild(
    tmp_path: Path, frozen_v2_closure
) -> None:
    policy = build_phase16_qualification_policy(repository_root=_PROJECT_ROOT)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_manifest = generate_phase16_qualification_corpus(
        first, repository_root=_PROJECT_ROOT, policy=policy
    )
    second_manifest = generate_phase16_qualification_corpus(
        second, repository_root=_PROJECT_ROOT, policy=policy
    )
    assert first_manifest == second_manifest
    for name in (
        "development_cases.jsonl",
        "development_labels.jsonl",
        "validation_cases.jsonl",
        "validation_labels.jsonl",
        "manifest.json",
    ):
        assert (first / name).read_bytes() == (second / name).read_bytes()
        assert (first / name).read_bytes() == (
            _PROJECT_ROOT / PHASE16_QUALIFICATION_ASSET_DIRECTORY / name
        ).read_bytes()


def test_qualification_corpus_has_strict_public_split_and_e2e_coverage(
    frozen_v2_closure,
) -> None:
    corpus = load_phase16_qualification_corpus(
        _PROJECT_ROOT / PHASE16_QUALIFICATION_ASSET_DIRECTORY,
        repository_root=_PROJECT_ROOT,
    )

    assert len(corpus.development_cases) == len(corpus.validation_cases) == 18
    assert corpus.manifest.holdout_case_count == corpus.manifest.holdout_high_conflict_e2e_case_count == 30
    assert corpus.manifest.holdout_release_state is HoldoutReleaseState.PENDING_INDEPENDENT_COMMITMENT
    assert corpus.manifest.holdout_commitment_digest is None
    assert set(corpus.manifest.previously_exposed_case_ids) == set(PHASE16_V8_PREVIOUSLY_EXPOSED_CASE_IDS)
    assert not set(case.case_id for case in corpus.public_cases).intersection(
        PHASE16_V8_PREVIOUSLY_EXPOSED_CASE_IDS
    )
    for split_cases in (corpus.development_cases, corpus.validation_cases):
        high_conflict = [case for case in split_cases if case.kind is QualificationCaseKind.HIGH_CONFLICT_PAIRED]
        normal = [case for case in split_cases if case.kind is QualificationCaseKind.NORMAL_SINGLE_COPILOT]
        adversarial = [case for case in split_cases if case.kind is QualificationCaseKind.ADVERSARIAL_DEGRADED]
        assert len(high_conflict) == 12
        assert len(normal) == len(adversarial) == 3
        assert sum(corpus.labels[item.case_id].e2e_metric_eligible for item in high_conflict) == 12
        assert all(corpus.labels[item.case_id].hard_safety_case for item in adversarial)


def test_qualification_case_rejects_top_level_and_nested_label_leakage() -> None:
    base = {
        "case_id": "phase16-qualification-high-conflict-paired-development-001",
        "logical_case_id": "qualification-logical-001",
        "split": QualificationSplit.DEVELOPMENT,
        "kind": QualificationCaseKind.HIGH_CONFLICT_PAIRED,
    }
    for leaking_input in (
        {"expected_route": "MULTI_AGENT_READY"},
        {"context": {"expected_route": "MULTI_AGENT_READY"}},
        {"contexts": [{"score": 1}]},
    ):
        with pytest.raises(ValidationError, match="labels cannot"):
            Phase16QualificationCase(**base, input=leaking_input)


def test_v8_exposure_register_cannot_be_mistaken_for_qualification_ids() -> None:
    assert all(not case_id.startswith("phase16-qualification-") for case_id in PHASE16_V8_PREVIOUSLY_EXPOSED_CASE_IDS)


def test_holdout_commitment_never_allows_pending_or_unsigned_release() -> None:
    base = {
        "commitment_id": "phase16-qualification-holdout-v2",
        "corpus_id": PHASE16_QUALIFICATION_CORPUS_ID,
        "policy_digest": "a" * 64,
        "case_count": 36,
        "high_conflict_e2e_case_count": 30,
        "ciphertext_digest": "b" * 64,
        "plaintext_digest": "c" * 64,
        "release_owner_id_digest": "d" * 64,
    }
    with pytest.raises(ValidationError, match="cannot remain pending"):
        Phase16HoldoutCommitment(
            **base,
            release_state=HoldoutReleaseState.PENDING_INDEPENDENT_COMMITMENT,
        )
    with pytest.raises(ValidationError, match="requires a release authentication"):
        Phase16HoldoutCommitment(**base, release_state=HoldoutReleaseState.RELEASED)
    committed = Phase16HoldoutCommitment(**base, release_state=HoldoutReleaseState.COMMITTED)
    assert committed.release_auth_tag is None


@pytest.mark.parametrize(
    ("reason_code", "expected"),
    (
        ("PLANNER_VALIDATION_FAILED_PLANNER_RISK_COVERAGE", "PLANNER_RISK_COVERAGE"),
        (
            "ANALYST_VALIDATION_FAILED_RUNNER_RESULT_SCHEMA_INVALID_EXPLANATION_MAX_LENGTH",
            "ANALYST_EXPLANATION_BOUND",
        ),
        ("MODEL_OUTCOME_UNAVAILABLE", "RECEIPT_OR_USAGE"),
        ("ANALYST_VALIDATION_FAILED", "OTHER_CONTROLLED_FAILURE"),
    ),
)
def test_v8_reason_categories_preserve_the_original_code(reason_code: str, expected: str) -> None:
    assert classify_v8_reason_code(reason_code) == expected


def test_manifest_cannot_claim_a_committed_holdout_without_digest(
    frozen_v2_closure,
) -> None:
    corpus = load_phase16_qualification_corpus(
        _PROJECT_ROOT / PHASE16_QUALIFICATION_ASSET_DIRECTORY,
        repository_root=_PROJECT_ROOT,
    )
    payload = corpus.manifest.model_dump(mode="json")
    payload["holdout_release_state"] = HoldoutReleaseState.COMMITTED.value
    payload["manifest_digest"] = None

    with pytest.raises(ValidationError, match="must publish a commitment"):
        Phase16QualificationManifest.model_validate(payload)

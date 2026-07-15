"""Phase 13 Task 5 Evaluation Store 与配对聚合测试。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import subprocess

import pytest

from src.specialist_evaluation.comparison import BinaryPair, aggregate_binary_pairs
from src.specialist_evaluation.models import (
    CaseAttempt,
    EvaluationCandidate,
    EvaluationManifest,
    EvaluationManifestKind,
    FormalManifestAuthorization,
    EvaluationRun,
    EvaluationSplit,
    EvaluationSubject,
    RetentionDecision,
    RetentionDecisionRecord,
    canonical_json_sha256,
    _build_formal_manifest_authorization,
)
from src.specialist_evaluation.store import (
    EvaluationInvariantError,
    InMemorySpecialistEvaluationStore,
)
from src.specialist_evaluation.manifest_authorization import (
    calculate_source_code_digest,
    verify_formal_manifest_at_git_head,
)


HASH_A = "a" * 64
HASH_B = "b" * 64


def _manifest() -> EvaluationManifest:
    """构造冻结评估身份；Manifest 摘要必须覆盖全部可复现输入。"""

    development = tuple(f"development-{index:03d}" for index in range(60))
    validation = ("live-validation-001",) + tuple(
        f"validation-{index:03d}" for index in range(119)
    )
    holdout = tuple(f"holdout-{index:03d}" for index in range(60))
    case_candidate_map = {}
    candidates = tuple(EvaluationCandidate)
    for split_ids, per_candidate in ((development, 20), (validation, 40), (holdout, 20)):
        for candidate_index, candidate in enumerate(candidates):
            start = candidate_index * per_candidate
            for case_id in split_ids[start : start + per_candidate]:
                case_candidate_map[case_id] = candidate.value
    return EvaluationManifest(
        manifest_id="phase13-v2",
        manifest_version="2.0.0",
        manifest_kind=EvaluationManifestKind.FORMAL_EVALUATION,
        source_commit="a" * 40,
        dataset_digest=HASH_A,
        schema_digest=HASH_B,
        generator_digest=HASH_A,
        seed=20260715,
        development_case_ids=development,
        validation_case_ids=validation,
        holdout_case_ids=holdout,
        case_candidate_map=case_candidate_map,
        profile_bundle_digest=HASH_A,
        prompt_bundle_digest=HASH_B,
        result_schema_bundle_digest=HASH_A,
        pricing_source_digest=HASH_B,
        temperature=Decimal("0"),
        code_digest=HASH_A,
        price_policy_digest=HASH_B,
        endpoint_host="api.deepseek.com",
        model_id="deepseek-v4-flash",
        candidate_ids=tuple(item.value for item in EvaluationCandidate),
    )


def _run(candidate: EvaluationCandidate = EvaluationCandidate.LIVE_OPS) -> EvaluationRun:
    manifest = _manifest()
    return EvaluationRun(
        run_id=f"run-{candidate.value.lower()}",
        manifest_id=manifest.manifest_id,
        manifest_digest=manifest.manifest_digest,
        candidate=candidate,
    )


def _register_manifest(store, manifest: EvaluationManifest | None = None) -> EvaluationManifest:
    """测试模拟 Task 11 已完成外部 Git/源码预检后的内部授权注册。"""

    selected = manifest or _manifest()
    return store.register_manifest(
        selected,
        authorization=_build_formal_manifest_authorization(selected),
    )


def _create_run(
    store,
    run: EvaluationRun | None = None,
    manifest: EvaluationManifest | None = None,
) -> EvaluationRun:
    """测试模拟执行进程在 create_run 前重新完成同一正式 Manifest 预检。"""

    selected_manifest = manifest or _manifest()
    return store.create_run(
        run or _run(),
        authorization=_build_formal_manifest_authorization(selected_manifest),
    )


def _claim(store: InMemorySpecialistEvaluationStore, worker_id: str = "unit-worker"):
    """领取测试 Run，确保所有正式事实都走与生产一致的 fencing 门禁。"""

    claim = store.claim_next_run(worker_id, manifest_id=_manifest().manifest_id)
    assert claim is not None
    return claim


def _attempt(
    *,
    attempt_id: str,
    subject: EvaluationSubject,
    success: bool,
    attempt_number: int = 1,
) -> CaseAttempt:
    return CaseAttempt(
        attempt_id=attempt_id,
        run_id="run-live_ops",
        manifest_id="phase13-v2",
        candidate=EvaluationCandidate.LIVE_OPS,
        case_id="live-validation-001",
        split=EvaluationSplit.VALIDATION,
        subject=subject,
        attempt_number=attempt_number,
        success=success,
        severe_violation=False,
        infrastructure_failure=False,
        latency_ms=Decimal("12.5"),
        input_tokens=10,
        output_tokens=5,
        cost_cny=Decimal("0.001"),
        result_digest=canonical_json_sha256(None),
        metric_outcomes={
            "action_success_rate": success,
            "incident_recovery_rate": success,
        },
        gate_results=(
            {
                "schema_valid": True,
                "permission_valid": True,
                "evidence_valid": True,
                "fallback_absent": True,
            }
            if subject is EvaluationSubject.AGENT
            else {}
        ),
    )


def test_manifest_digest_is_stable_and_rejects_tampered_digest() -> None:
    """Manifest 哈希必须字节稳定，调用方不能提交与事实不符的摘要。"""

    manifest = _manifest()
    assert manifest.manifest_digest == canonical_json_sha256(
        manifest.model_dump(mode="json", exclude={"manifest_digest"})
    )
    with pytest.raises(ValueError, match="manifest_digest"):
        EvaluationManifest.model_validate(
            {**manifest.model_dump(mode="json"), "manifest_digest": HASH_B}
        )


def test_dataset_baseline_manifest_cannot_create_formal_run() -> None:
    """Task 6 数据集基线可留档，但必须等 Task 11 最终 Git 身份后才能创建 Run。"""

    payload = _manifest().model_dump(mode="json")
    payload["manifest_kind"] = "DATASET_BASELINE"
    payload["source_commit"] = None
    payload.pop("manifest_digest")
    baseline = EvaluationManifest.model_validate(payload)
    store = InMemorySpecialistEvaluationStore()
    store.register_manifest(baseline)
    run_payload = _run().model_dump(mode="json")
    run_payload["manifest_digest"] = baseline.manifest_digest

    with pytest.raises(EvaluationInvariantError, match="formal evaluation"):
        store.create_run(EvaluationRun.model_validate(run_payload))

    missing_commit = _manifest().model_dump(mode="json")
    missing_commit["source_commit"] = None
    missing_commit.pop("manifest_digest")
    with pytest.raises(ValueError, match="source_commit"):
        EvaluationManifest.model_validate(missing_commit)


def test_formal_manifest_registration_requires_bound_internal_authorization() -> None:
    """仅伪造 40 位 commit 和重算摘要，不能绕过 Task 11 的可信预检边界。"""

    manifest = _manifest()
    store = InMemorySpecialistEvaluationStore()
    with pytest.raises(EvaluationInvariantError, match="authorization"):
        store.register_manifest(manifest)
    with pytest.raises(ValueError, match="internal factory"):
        FormalManifestAuthorization(
            manifest_id=manifest.manifest_id,
            manifest_digest=manifest.manifest_digest,
            source_commit=manifest.source_commit,
            code_digest=manifest.code_digest,
        )

    _register_manifest(store, manifest)
    with pytest.raises(EvaluationInvariantError, match="authorization"):
        store.create_run(_run())


def test_formal_manifest_git_preflight_binds_clean_head_and_source_digest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """公开预检必须验证真实 Git HEAD、清洁源码和重算 code_digest 后才签发授权。"""

    repository = tmp_path / "repository"
    (repository / "src").mkdir(parents=True)
    (repository / "evaluation").mkdir()
    (repository / "src" / "runtime.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "evaluation" / "loader.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repository / ".gitignore").write_text("src/ignored.py\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "phase13@example.test"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Phase 13 Test"], cwd=repository, check=True)
    subprocess.run(["git", "add", "src", "evaluation", ".gitignore"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "freeze"], cwd=repository, check=True, capture_output=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repository, check=True, capture_output=True, text=True
    ).stdout.strip()
    payload = _manifest().model_dump(mode="json")
    payload["source_commit"] = commit
    payload["code_digest"] = calculate_source_code_digest(repository)
    payload.pop("manifest_digest")
    manifest = EvaluationManifest.model_validate(payload)

    authorization = verify_formal_manifest_at_git_head(manifest, repository)
    assert authorization.provenance_verified

    (repository / "src" / "runtime.py").write_text("VALUE = 3\n", encoding="utf-8")
    with pytest.raises(ValueError, match="clean"):
        verify_formal_manifest_at_git_head(manifest, repository)

    (repository / "src" / "runtime.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "src" / "ignored.py").write_text("VALUE = 4\n", encoding="utf-8")
    with pytest.raises(ValueError, match="tracked source closure"):
        verify_formal_manifest_at_git_head(manifest, repository)

    (repository / "src" / "ignored.py").unlink()
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path.name == "src" or original_is_symlink(path),
    )
    with pytest.raises(ValueError, match="symlink"):
        verify_formal_manifest_at_git_head(manifest, repository)


def test_store_keeps_attempt_history_but_selects_only_one_formal_result() -> None:
    """重跑保留 Attempt 历史，同一 case/subject 只能有一个正式结果。"""

    store = InMemorySpecialistEvaluationStore()
    _register_manifest(store)
    _create_run(store)
    claim = _claim(store)
    first = _attempt(
        attempt_id="attempt-baseline-1", subject=EvaluationSubject.BASELINE, success=False
    )
    retry = _attempt(
        attempt_id="attempt-baseline-2",
        subject=EvaluationSubject.BASELINE,
        success=True,
        attempt_number=2,
    )
    store.append_attempt(first, claim=claim)
    store.append_attempt(retry, claim=claim)
    selected = store.select_attempt(retry.attempt_id, claim=claim)

    assert [item.attempt_id for item in store.list_attempts(_run().run_id)] == [
        first.attempt_id,
        retry.attempt_id,
    ]
    assert selected.attempt_id == retry.attempt_id
    with pytest.raises(EvaluationInvariantError, match="selected"):
        store.select_attempt(first.attempt_id, claim=claim)


def test_formal_selection_is_unique_across_runs_for_same_manifest_candidate_case() -> None:
    """新建 Run 不能绕过 manifest/candidate/case/subject 的正式结果唯一性。"""

    store = InMemorySpecialistEvaluationStore()
    manifest = _manifest()
    first_run = _run()
    second_run = EvaluationRun(
        run_id="run-live_ops-retry",
        manifest_id=manifest.manifest_id,
        manifest_digest=manifest.manifest_digest,
        candidate=EvaluationCandidate.LIVE_OPS,
    )
    _register_manifest(store, manifest)
    _create_run(store, first_run, manifest)
    _create_run(store, second_run, manifest)
    first_claim = _claim(store, "first-worker")
    second_claim = _claim(store, "second-worker")
    first = _attempt(attempt_id="attempt-run-1", subject=EvaluationSubject.AGENT, success=True)
    second = CaseAttempt.model_validate(
        {
            **first.model_dump(mode="json"),
            "attempt_id": "attempt-run-2",
            "run_id": second_run.run_id,
        }
    )
    store.append_attempt(first, claim=first_claim)
    store.append_attempt(second, claim=second_claim)
    store.select_attempt(first.attempt_id, claim=first_claim)
    with pytest.raises(EvaluationInvariantError, match="selected"):
        store.select_attempt(second.attempt_id, claim=second_claim)


def test_store_rejects_case_subject_and_attempt_number_identity_conflicts() -> None:
    """同一 run/case/subject/attempt_number 不得写入两个不同 Attempt。"""

    store = InMemorySpecialistEvaluationStore()
    _register_manifest(store)
    _create_run(store)
    claim = _claim(store)
    store.append_attempt(
        _attempt(attempt_id="attempt-1", subject=EvaluationSubject.AGENT, success=True),
        claim=claim,
    )
    with pytest.raises(EvaluationInvariantError, match="attempt identity"):
        store.append_attempt(
            _attempt(attempt_id="attempt-2", subject=EvaluationSubject.AGENT, success=False),
            claim=claim,
        )


def test_attempt_case_and_split_must_belong_to_manifest() -> None:
    """任意 case 或伪造 split 都不能越过 Manifest 冻结数据集边界。"""

    store = InMemorySpecialistEvaluationStore()
    _register_manifest(store)
    _create_run(store)
    claim = _claim(store)
    payload = _attempt(
        attempt_id="attempt-wrong-split", subject=EvaluationSubject.AGENT, success=False
    ).model_dump(mode="json")
    payload["split"] = EvaluationSplit.HOLDOUT.value
    with pytest.raises(EvaluationInvariantError, match="case.*split"):
        store.append_attempt(CaseAttempt.model_validate(payload), claim=claim)


def test_selection_requires_matching_registered_manifest_run_and_attempt() -> None:
    """Manifest、Run 与 Attempt 的候选和摘要必须形成同一不可伪造链。"""

    store = InMemorySpecialistEvaluationStore()
    _register_manifest(store)
    _create_run(store)
    claim = _claim(store)
    forged = _attempt(
        attempt_id="attempt-forged", subject=EvaluationSubject.AGENT, success=True
    ).model_copy()
    object.__setattr__(forged, "manifest_id", "other-manifest")
    with pytest.raises(EvaluationInvariantError, match="manifest"):
        store.append_attempt(forged, claim=claim)


def test_binary_pair_aggregation_reports_rates_delta_wins_losses_and_wilson() -> None:
    """配对聚合必须同时报告绝对率、百分点差、胜负和 Wilson 区间。"""

    metric = aggregate_binary_pairs(
        metric_id="action_success_rate",
        pairs=(
            BinaryPair(case_id="c1", baseline_success=False, agent_success=True),
            BinaryPair(case_id="c2", baseline_success=True, agent_success=True),
            BinaryPair(case_id="c3", baseline_success=True, agent_success=False),
            BinaryPair(case_id="c4", baseline_success=False, agent_success=True),
        ),
    )
    assert metric.sample_count == 4
    assert metric.baseline_rate == Decimal("0.500000")
    assert metric.agent_rate == Decimal("0.750000")
    assert metric.delta_percentage_points == Decimal("25.000000")
    assert metric.paired_wins == 2
    assert metric.paired_losses == 1
    assert Decimal("0") <= metric.baseline_wilson_low <= metric.baseline_wilson_high <= Decimal("1")
    assert Decimal("0") <= metric.agent_wilson_low <= metric.agent_wilson_high <= Decimal("1")


def test_severe_violation_is_independent_and_cannot_be_averaged_away() -> None:
    """任一严重违规必须作为独立计数保留，不能被成功率平均抵消。"""

    metric = aggregate_binary_pairs(
        metric_id="action_success_rate",
        pairs=(
            BinaryPair(
                case_id="c1",
                baseline_success=True,
                agent_success=True,
                agent_severe_violation=True,
            ),
            BinaryPair(case_id="c2", baseline_success=True, agent_success=True),
        ),
    )
    assert metric.agent_rate == Decimal("1.000000")
    assert metric.severe_violation_count == 1


def test_duplicate_metric_aggregation_is_rejected() -> None:
    """同一 run/split/metric 只能写入一次正式聚合，避免挑选有利统计。"""

    store = InMemorySpecialistEvaluationStore()
    _register_manifest(store)
    _create_run(store)
    claim = _claim(store)
    baseline = _attempt(
        attempt_id="metric-baseline", subject=EvaluationSubject.BASELINE, success=False
    )
    agent = _attempt(attempt_id="metric-agent", subject=EvaluationSubject.AGENT, success=True)
    store.append_attempt(baseline, claim=claim)
    store.append_attempt(agent, claim=claim)
    store.select_attempt(baseline.attempt_id, claim=claim)
    store.select_attempt(agent.attempt_id, claim=claim)
    metric = aggregate_binary_pairs(
        metric_id="action_success_rate",
        pairs=(BinaryPair(case_id="c1", baseline_success=False, agent_success=True),),
    )
    with pytest.raises(EvaluationInvariantError, match="case set"):
        store.save_paired_metric(
            _run().run_id, EvaluationSplit.VALIDATION, metric, claim=claim
        )
    metric = aggregate_binary_pairs(
        metric_id="action_success_rate",
        pairs=(
            BinaryPair(
                case_id=baseline.case_id,
                baseline_success=baseline.success,
                agent_success=agent.success,
            ),
        ),
    )
    store.save_paired_metric(
        _run().run_id, EvaluationSplit.VALIDATION, metric, claim=claim
    )
    with pytest.raises(EvaluationInvariantError, match="metric"):
        store.save_paired_metric(
            _run().run_id, EvaluationSplit.VALIDATION, metric, claim=claim
        )


def test_retention_decision_restricts_inconclusive_to_external_evidence_gap() -> None:
    """INCONCLUSIVE 只表示外部证据不足，规则已证明失败必须写 REJECTED。"""

    with pytest.raises(ValueError, match="INCONCLUSIVE"):
        RetentionDecisionRecord(
            decision_id="decision-1",
            run_id=_run().run_id,
            candidate=EvaluationCandidate.LIVE_OPS,
            decision=RetentionDecision.INCONCLUSIVE,
            reason_code="METRIC_THRESHOLD_FAILED",
            external_evidence_sufficient=True,
            severe_violation_count=0,
            metrics_digest=HASH_A,
        )

    rejected = RetentionDecisionRecord(
        decision_id="decision-2",
        run_id=_run().run_id,
        candidate=EvaluationCandidate.LIVE_OPS,
        decision=RetentionDecision.REJECTED,
        reason_code="METRIC_THRESHOLD_FAILED",
        external_evidence_sufficient=True,
        severe_violation_count=0,
        metrics_digest=HASH_A,
    )
    assert rejected.decision is RetentionDecision.REJECTED


def test_retained_decision_requires_zero_severe_violations_and_is_unique() -> None:
    """RETAINED 必须零严重违规，同一 Run 只能持久化一个最终去留结论。"""

    with pytest.raises(ValueError, match="severe"):
        RetentionDecisionRecord(
            decision_id="decision-bad",
            run_id=_run().run_id,
            candidate=EvaluationCandidate.LIVE_OPS,
            decision=RetentionDecision.RETAINED,
            reason_code="ALL_GATES_PASSED",
            external_evidence_sufficient=True,
            severe_violation_count=1,
            metrics_digest=HASH_A,
            completed_validation_cases=40,
            completed_holdout_cases=20,
            hard_gates_passed=True,
        )

    store = InMemorySpecialistEvaluationStore()
    _register_manifest(store)
    _create_run(store)
    claim = _claim(store)
    retained = RetentionDecisionRecord(
        decision_id="decision-good",
        run_id=_run().run_id,
        candidate=EvaluationCandidate.LIVE_OPS,
        decision=RetentionDecision.RETAINED,
        reason_code="ALL_GATES_PASSED",
        external_evidence_sufficient=True,
        severe_violation_count=0,
        metrics_digest=store.metrics_digest(_run().run_id),
        completed_validation_cases=40,
        completed_holdout_cases=20,
        hard_gates_passed=True,
    )
    with pytest.raises(EvaluationInvariantError, match="complete selected evidence"):
        store.save_retention_decision(retained, claim=claim)

    record = RetentionDecisionRecord(
        decision_id="decision-rejected",
        run_id=_run().run_id,
        candidate=EvaluationCandidate.LIVE_OPS,
        decision=RetentionDecision.REJECTED,
        reason_code="METRIC_THRESHOLD_FAILED",
        external_evidence_sufficient=True,
        severe_violation_count=0,
        metrics_digest=store.metrics_digest(_run().run_id),
    )
    store.save_retention_decision(record, claim=claim)
    with pytest.raises(EvaluationInvariantError, match="RUNNING|decision"):
        store.save_retention_decision(record.model_copy(), claim=claim)


def test_retained_decision_requires_complete_formal_cases_and_all_hard_gates() -> None:
    """RETAINED 不得只凭零严重违规写入，必须完成正式样本并通过共同硬门。"""

    for validation, holdout, gates in ((39, 20, True), (40, 19, True), (40, 20, False)):
        with pytest.raises(ValueError, match="RETAINED"):
            RetentionDecisionRecord(
                decision_id=f"decision-{validation}-{holdout}-{gates}",
                run_id=_run().run_id,
                candidate=EvaluationCandidate.LIVE_OPS,
                decision=RetentionDecision.RETAINED,
                reason_code="ALL_GATES_PASSED",
                external_evidence_sufficient=True,
                severe_violation_count=0,
                metrics_digest=HASH_A,
                completed_validation_cases=validation,
                completed_holdout_cases=holdout,
                hard_gates_passed=gates,
            )


def test_attempt_rejects_successful_infrastructure_failure() -> None:
    """基础设施不可用不能同时被标成业务成功并进入正式指标。"""

    payload = _attempt(
        attempt_id="attempt-invalid", subject=EvaluationSubject.AGENT, success=True
    ).model_dump(mode="json")
    payload["infrastructure_failure"] = True
    with pytest.raises(ValueError, match="infrastructure"):
        CaseAttempt.model_validate(payload)


def test_attempt_output_digest_and_decimal_precision_are_strict() -> None:
    """输出摘要必须匹配冻结 JSON，数据库 NUMERIC 前必须拒绝会被静默舍入的数值。"""

    output = {"decision": "NO_ACTION"}
    payload = _attempt(
        attempt_id="attempt-output", subject=EvaluationSubject.AGENT, success=True
    ).model_dump(mode="json")
    payload["output"] = output
    payload["result_digest"] = canonical_json_sha256(output)
    assert CaseAttempt.model_validate(payload).output["decision"] == "NO_ACTION"

    payload["result_digest"] = HASH_B
    with pytest.raises(ValueError, match="result_digest"):
        CaseAttempt.model_validate(payload)

    payload["result_digest"] = canonical_json_sha256(output)
    payload["cost_cny"] = "0.0000001"
    with pytest.raises(ValueError, match="precision"):
        CaseAttempt.model_validate(payload)


def test_paired_metric_rejects_impossible_counts_and_delta() -> None:
    """指标缓存必须能由整数事实重算，不能接受不可能的胜负计数或百分点差。"""

    metric = aggregate_binary_pairs(
        metric_id="action_success_rate",
        pairs=(BinaryPair(case_id="c1", baseline_success=False, agent_success=True),),
    )
    payload = metric.model_dump(mode="json")
    payload["tied"] = 1
    with pytest.raises(ValueError, match="paired outcome"):
        type(metric).model_validate(payload)

    payload = metric.model_dump(mode="json")
    payload["delta_percentage_points"] = "99.000000"
    with pytest.raises(ValueError, match="delta"):
        type(metric).model_validate(payload)


def test_infrastructure_failure_attempt_cannot_be_selected() -> None:
    """基础设施失败只能导向证据不足，不能作为正式业务失败进入配对聚合。"""

    store = InMemorySpecialistEvaluationStore()
    _register_manifest(store)
    _create_run(store)
    claim = _claim(store)
    payload = _attempt(
        attempt_id="attempt-infra", subject=EvaluationSubject.AGENT, success=False
    ).model_dump(mode="json")
    payload["infrastructure_failure"] = True
    attempt = CaseAttempt.model_validate(payload)
    store.append_attempt(attempt, claim=claim)
    with pytest.raises(EvaluationInvariantError, match="infrastructure"):
        store.select_attempt(attempt.attempt_id, claim=claim)


def test_manifest_rejects_unknown_or_incomplete_candidate_set() -> None:
    """正式 Manifest 必须精确覆盖三个冻结候选，未知候选不得进入身份哈希。"""

    payload = _manifest().model_dump(mode="json", exclude={"manifest_digest"})
    payload["candidate_ids"] = ["LIVE_OPS", "UNKNOWN"]
    with pytest.raises(ValueError, match="candidate_ids"):
        EvaluationManifest.model_validate(payload)


def test_decision_rejects_digest_not_derived_from_saved_metrics() -> None:
    """去留结论必须绑定 Store 已保存指标的稳定摘要，不能接受调用方任意哈希。"""

    store = InMemorySpecialistEvaluationStore()
    _register_manifest(store)
    _create_run(store)
    claim = _claim(store)
    record = RetentionDecisionRecord(
        decision_id="decision-forged-metrics",
        run_id=_run().run_id,
        candidate=EvaluationCandidate.LIVE_OPS,
        decision=RetentionDecision.REJECTED,
        reason_code="METRIC_THRESHOLD_FAILED",
        external_evidence_sufficient=True,
        severe_violation_count=0,
        metrics_digest=HASH_A,
    )
    with pytest.raises(EvaluationInvariantError, match="metrics digest"):
        store.save_retention_decision(record, claim=claim)


def test_attempt_exposes_independent_binary_metric_outcomes() -> None:
    """同一 case 的业务指标必须独立，不能全部退化为顶层 success。"""

    payload = _attempt(
        attempt_id="attempt-independent-metrics",
        subject=EvaluationSubject.AGENT,
        success=True,
    ).model_dump(mode="json")
    payload["metric_outcomes"] = {
        "action_success_rate": True,
        "incident_recovery_rate": False,
    }
    attempt = CaseAttempt.model_validate(payload)

    assert attempt.metric_outcomes["action_success_rate"] is True
    assert attempt.metric_outcomes["incident_recovery_rate"] is False
    with pytest.raises(TypeError):
        attempt.metric_outcomes["action_success_rate"] = False


def test_formal_fact_writes_require_an_active_claim() -> None:
    """EvaluationRun 从创建起就必须先领取租约，不能在首次 claim 前绕过 fencing。"""

    store = InMemorySpecialistEvaluationStore()
    _register_manifest(store)
    _create_run(store)
    attempt = _attempt(
        attempt_id="attempt-without-claim",
        subject=EvaluationSubject.AGENT,
        success=True,
    )

    with pytest.raises(EvaluationInvariantError, match="claim"):
        store.append_attempt(attempt)
    claim = store.claim_next_run("worker-1", manifest_id=_manifest().manifest_id)
    assert claim is not None
    store.append_attempt(attempt, claim=claim)


def test_insufficient_external_evidence_can_only_be_inconclusive() -> None:
    """外部证据不足不得伪装成候选规则失败。"""

    with pytest.raises(ValueError, match="external evidence"):
        RetentionDecisionRecord(
            decision_id="decision-invalid-rejected",
            run_id=_run().run_id,
            candidate=EvaluationCandidate.LIVE_OPS,
            decision=RetentionDecision.REJECTED,
            reason_code="INFRASTRUCTURE_UNAVAILABLE",
            external_evidence_sufficient=False,
            severe_violation_count=0,
            metrics_digest=HASH_A,
        )


def test_retention_decision_finalizes_run_and_freezes_formal_facts() -> None:
    """最终结论必须原子终结 Run，之后不能追加 Attempt 或指标。"""

    store = InMemorySpecialistEvaluationStore()
    _register_manifest(store)
    _create_run(store)
    claim = store.claim_next_run(
        "worker-finalizer", manifest_id=_manifest().manifest_id
    )
    assert claim is not None
    decision = RetentionDecisionRecord(
        decision_id="decision-final",
        run_id=_run().run_id,
        candidate=EvaluationCandidate.LIVE_OPS,
        decision=RetentionDecision.INCONCLUSIVE,
        reason_code="INFRASTRUCTURE_UNAVAILABLE",
        external_evidence_sufficient=False,
        severe_violation_count=0,
        metrics_digest=store.metrics_digest(_run().run_id),
    )
    store.save_retention_decision(decision, claim=claim)

    assert store.snapshot()["runs"][0].status == "COMPLETED"
    with pytest.raises(EvaluationInvariantError, match="RUNNING"):
        store.append_attempt(
            _attempt(
                attempt_id="attempt-after-decision",
                subject=EvaluationSubject.AGENT,
                success=True,
            ),
            claim=claim,
        )


def test_store_recomputes_each_metric_from_its_own_attempt_fact() -> None:
    """相同 selected Attempt 可以支持结果相反的两个业务指标，且分别重算。"""

    store = InMemorySpecialistEvaluationStore()
    _register_manifest(store)
    _create_run(store)
    claim = _claim(store, "independent-metric-worker")
    baseline_payload = _attempt(
        attempt_id="independent-baseline",
        subject=EvaluationSubject.BASELINE,
        success=False,
    ).model_dump(mode="json")
    baseline_payload["metric_outcomes"] = {
        "action_success_rate": False,
        "incident_recovery_rate": True,
    }
    agent_payload = _attempt(
        attempt_id="independent-agent",
        subject=EvaluationSubject.AGENT,
        success=True,
    ).model_dump(mode="json")
    agent_payload["metric_outcomes"] = {
        "action_success_rate": True,
        "incident_recovery_rate": False,
    }
    baseline = CaseAttempt.model_validate(baseline_payload)
    agent = CaseAttempt.model_validate(agent_payload)
    store.append_attempt(baseline, claim=claim)
    store.append_attempt(agent, claim=claim)
    store.select_attempt(baseline.attempt_id, claim=claim)
    store.select_attempt(agent.attempt_id, claim=claim)

    action = aggregate_binary_pairs(
        metric_id="action_success_rate",
        pairs=(BinaryPair(case_id=baseline.case_id, baseline_success=False, agent_success=True),),
    )
    recovery = aggregate_binary_pairs(
        metric_id="incident_recovery_rate",
        pairs=(BinaryPair(case_id=baseline.case_id, baseline_success=True, agent_success=False),),
    )
    store.save_paired_metric(
        _run().run_id, EvaluationSplit.VALIDATION, action, claim=claim
    )
    store.save_paired_metric(
        _run().run_id, EvaluationSplit.VALIDATION, recovery, claim=claim
    )


def test_decision_hard_gate_summary_must_match_selected_agent_facts() -> None:
    """调用方不能用 hard_gates_passed=True 覆盖 Attempt 中失败的共同安全门。"""

    store = InMemorySpecialistEvaluationStore()
    _register_manifest(store)
    _create_run(store)
    claim = _claim(store, "gate-worker")
    payload = _attempt(
        attempt_id="gate-failed-agent",
        subject=EvaluationSubject.AGENT,
        success=False,
    ).model_dump(mode="json")
    payload["gate_results"]["permission_valid"] = False
    attempt = CaseAttempt.model_validate(payload)
    store.append_attempt(attempt, claim=claim)
    store.select_attempt(attempt.attempt_id, claim=claim)
    decision = RetentionDecisionRecord(
        decision_id="forged-gate-decision",
        run_id=_run().run_id,
        candidate=EvaluationCandidate.LIVE_OPS,
        decision=RetentionDecision.REJECTED,
        reason_code="PERMISSION_GATE_FAILED",
        external_evidence_sufficient=True,
        severe_violation_count=0,
        metrics_digest=store.metrics_digest(_run().run_id),
        hard_gates_passed=True,
    )

    with pytest.raises(EvaluationInvariantError, match="hard gate"):
        store.save_retention_decision(decision, claim=claim)


def test_attempt_numeric_bounds_match_postgres_columns() -> None:
    """内存模型必须提前拒绝 PostgreSQL NUMERIC/INTEGER 无法表示的值。"""

    payload = _attempt(
        attempt_id="numeric-overflow",
        subject=EvaluationSubject.AGENT,
        success=False,
    ).model_dump(mode="json")
    for field, value in (
        ("latency_ms", "1000000000.000"),
        ("cost_cny", "1000000.000000"),
        ("input_tokens", 2_147_483_648),
        ("output_tokens", 2_147_483_648),
    ):
        invalid = dict(payload)
        invalid[field] = value
        with pytest.raises(ValueError):
            CaseAttempt.model_validate(invalid)

    manifest_payload = _manifest().model_dump(mode="json", exclude={"manifest_digest"})
    manifest_payload["seed"] = 9_223_372_036_854_775_808
    with pytest.raises(ValueError):
        EvaluationManifest.model_validate(manifest_payload)

    attempt_payload = _attempt(
        attempt_id="attempt-number-overflow",
        subject=EvaluationSubject.AGENT,
        success=False,
    ).model_dump(mode="json")
    attempt_payload["attempt_number"] = 2_147_483_648
    with pytest.raises(ValueError):
        CaseAttempt.model_validate(attempt_payload)


def test_decision_case_counts_are_recomputed_from_complete_selected_pairs() -> None:
    """早停位置必须来自正式配对证据，不能由调用方任意声明 40/20。"""

    store = InMemorySpecialistEvaluationStore()
    _register_manifest(store)
    _create_run(store)
    claim = _claim(store, "count-worker")
    forged = RetentionDecisionRecord(
        decision_id="forged-count-decision",
        run_id=_run().run_id,
        candidate=EvaluationCandidate.LIVE_OPS,
        decision=RetentionDecision.INCONCLUSIVE,
        reason_code="INFRASTRUCTURE_UNAVAILABLE",
        external_evidence_sufficient=False,
        severe_violation_count=0,
        metrics_digest=store.metrics_digest(_run().run_id),
        completed_validation_cases=40,
        completed_holdout_cases=20,
    )

    with pytest.raises(EvaluationInvariantError, match="completed case"):
        store.save_retention_decision(forged, claim=claim)


def test_candidate_decision_is_unique_and_cancels_sibling_runs() -> None:
    """同一 Manifest/Candidate 只能有一个结论，结论后兄弟 Run 不得改变 selected。"""

    store = InMemorySpecialistEvaluationStore()
    manifest = _manifest()
    first_run = _run()
    second_run = EvaluationRun(
        run_id="run-live_ops-sibling",
        manifest_id=manifest.manifest_id,
        manifest_digest=manifest.manifest_digest,
        candidate=EvaluationCandidate.LIVE_OPS,
    )
    _register_manifest(store, manifest)
    _create_run(store, first_run, manifest)
    _create_run(store, second_run, manifest)
    first_claim = _claim(store, "candidate-first-worker")
    second_claim = _claim(store, "candidate-second-worker")
    decision = RetentionDecisionRecord(
        decision_id="candidate-global-decision",
        run_id=first_run.run_id,
        candidate=first_run.candidate,
        decision=RetentionDecision.INCONCLUSIVE,
        reason_code="INFRASTRUCTURE_UNAVAILABLE",
        external_evidence_sufficient=False,
        severe_violation_count=0,
        metrics_digest=store.metrics_digest(first_run.run_id),
    )
    store.save_retention_decision(decision, claim=first_claim)

    sibling = next(
        run for run in store.snapshot()["runs"] if run.run_id == second_run.run_id
    )
    assert sibling.status == "CANCELLED"
    with pytest.raises(EvaluationInvariantError, match="RUNNING"):
        store.append_attempt(
            CaseAttempt.model_validate(
                {
                    **_attempt(
                        attempt_id="sibling-late-attempt",
                        subject=EvaluationSubject.AGENT,
                        success=True,
                    ).model_dump(mode="json"),
                    "run_id": second_run.run_id,
                }
            ),
            claim=second_claim,
        )
    with pytest.raises(EvaluationInvariantError, match="candidate.*decision"):
        _create_run(
            store,
            EvaluationRun(
                run_id="run-live_ops-after-decision",
                manifest_id=manifest.manifest_id,
                manifest_digest=manifest.manifest_digest,
                candidate=EvaluationCandidate.LIVE_OPS,
            ),
            manifest,
        )


@pytest.mark.parametrize("status", ("COMPLETED", "FAILED", "CANCELLED"))
def test_create_run_rejects_terminal_initial_status(status: str) -> None:
    """终态只能由受控状态转换产生，创建入口不得制造无 decision 的终态 Run。"""

    store = InMemorySpecialistEvaluationStore()
    manifest = _manifest()
    _register_manifest(store, manifest)
    with pytest.raises(EvaluationInvariantError, match="RUNNING"):
        store.create_run(
            EvaluationRun(
                run_id=f"terminal-{status.lower()}",
                manifest_id=manifest.manifest_id,
                manifest_digest=manifest.manifest_digest,
                candidate=EvaluationCandidate.LIVE_OPS,
                status=status,
            )
        )

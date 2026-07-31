"""Phase 16 qualification 逐 stage 执行账本的 PostgreSQL 契约。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
from psycopg import sql
import pytest

from src.config.settings import get_settings
from src.decision_support.phase16_qualification import (
    PHASE16_QUALIFICATION_ASSET_DIRECTORY,
    build_phase16_qualification_policy,
    load_phase16_qualification_corpus,
)
from src.decision_support.phase16_qualification_execution_ledger import (
    PostgresPhase16QualificationExecutionLedger,
    QualificationExecutionCaseStatus,
    QualificationExecutionStage,
    QualificationExecutionSlot,
    QualificationExecutionValidationVerdict,
)
from src.decision_support.phase16_qualification_ledger import (
    Phase16QualificationLedgerError,
    QualificationCampaign,
    QualificationCampaignKind,
    build_qualification_candidate,
    corpus_identity_from_manifest,
    initialize_phase16_qualification_schema,
)
from src.specialist_runtime.model_port import ModelSuccess, ModelUsage
from src.specialist_runtime.models import canonical_json_sha256


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TEST_HMAC_KEY = bytes.fromhex("1c" * 32)


@pytest.fixture()
def execution_ledger_factory():
    base_kwargs = dict(get_settings().postgres_connection_kwargs)
    schema_name = f"phase16_qualification_execution_{uuid4().hex}"
    with psycopg.connect(**base_kwargs) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        connection.commit()
    settings = SimpleNamespace(
        postgres_connection_kwargs={**base_kwargs, "options": f"-c search_path={schema_name}"}
    )
    initialize_phase16_qualification_schema(settings)

    def build() -> PostgresPhase16QualificationExecutionLedger:
        return PostgresPhase16QualificationExecutionLedger(settings, hmac_key=_TEST_HMAC_KEY)

    build.settings = settings
    try:
        yield build
    finally:
        with psycopg.connect(**base_kwargs) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name)))
            connection.commit()


def _setup(ledger: PostgresPhase16QualificationExecutionLedger):
    policy = build_phase16_qualification_policy(repository_root=_PROJECT_ROOT)
    corpus = load_phase16_qualification_corpus(
        _PROJECT_ROOT / PHASE16_QUALIFICATION_ASSET_DIRECTORY,
        repository_root=_PROJECT_ROOT,
        policy=policy,
    )
    identity = corpus_identity_from_manifest(corpus.manifest)
    candidate = build_qualification_candidate(
        candidate_id="phase16-qualification-v2-execution-fixture",
        policy=policy,
        analyst_profile_digest="a" * 64,
        planner_profile_digest="b" * 64,
        adapter_digest="c" * 64,
    )
    ledger.ensure_policy(policy)
    ledger.ensure_corpus(identity)
    ledger.ensure_candidate(candidate)
    campaign = QualificationCampaign(
        campaign_id="phase16-qualification-v2-development-execution-001",
        campaign_kind=QualificationCampaignKind.DEVELOPMENT,
        policy_digest=policy.policy_digest or "",
        corpus_digest=identity.corpus_digest,
        candidate_digest=candidate.candidate_digest or "",
        manifest_digest="d" * 64,
        reservation_cny="0.180000",
    )
    ledger.ensure_campaign(campaign)
    return policy, candidate, campaign


def _request_id(case_id: str, stage: QualificationExecutionStage) -> str:
    return str(uuid5(NAMESPACE_URL, f"qualification-execution:{case_id}:{stage.value}"))


def _success(request_id: str, suffix: str) -> ModelSuccess:
    output = {"kind": "FINAL", "final_output": {"status": "fixture"}}
    return ModelSuccess(
        request_id=request_id,
        model_id="deepseek-v4-pro",
        output=output,
        usage=ModelUsage(input_tokens=100, output_tokens=100, total_tokens=200),
        provider_response_id=f"qualification-execution-provider-{suffix}",
        finish_reason="stop",
        response_digest=canonical_json_sha256(output),
        latency_ms=Decimal("1.250"),
        # V9 receipt 门禁要求成功回执带实际响应端点与尝试次数。
        endpoint_host="api.deepseek.com",
        attempts=1,
    )


def _append_stage(
    ledger: PostgresPhase16QualificationExecutionLedger,
    *,
    run_id: str,
    claim_id: str,
    case_id: str,
    stage: QualificationExecutionStage,
    profile_digest: str,
    verdict: QualificationExecutionValidationVerdict,
    reason_code: str,
):
    request_id = _request_id(case_id, stage)
    attempt = ledger.begin_dispatch(
        run_id=run_id,
        claim_id=claim_id,
        stage=stage,
        profile_digest=profile_digest,
        internal_request_id=request_id,
        reservation_cny=Decimal("0.030000"),
    )
    assert ledger.append_receipt(attempt_id=attempt.attempt_id, success=_success(request_id, f"{case_id}-{stage}"))
    ledger.append_validation(
        attempt_id=attempt.attempt_id,
        verdict=verdict,
        reason_code=reason_code,
        validation_digest=canonical_json_sha256({"attempt_id": attempt.attempt_id, "reason": reason_code}),
    )
    return attempt


def test_execution_ledger_preserves_stage_order_terminal_cases_and_complete_receipts(
    execution_ledger_factory,
) -> None:
    ledger = execution_ledger_factory()
    _policy, candidate, campaign = _setup(ledger)
    run_id = "phase16-qualification-v2-development-run-001"
    slots = (
        QualificationExecutionSlot("qualification-development-e2e-001", "1" * 64, True),
        QualificationExecutionSlot("qualification-development-e2e-002", "2" * 64, True),
        QualificationExecutionSlot("qualification-development-no-send-003", "3" * 64, False),
    )
    ledger.begin_run_with_slots(run_id=run_id, campaign_id=campaign.campaign_id, slots=slots)

    first = ledger.claim_case(run_id=run_id, case_id=slots[0].case_id, case_digest=slots[0].case_digest)
    _append_stage(
        ledger,
        run_id=run_id,
        claim_id=first.claim_id,
        case_id=first.case_id,
        stage=QualificationExecutionStage.ANALYST,
        profile_digest=candidate.analyst_profile_digest,
        verdict=QualificationExecutionValidationVerdict.PASS,
        reason_code="ANALYST_VALIDATION_PASS",
    )
    _append_stage(
        ledger,
        run_id=run_id,
        claim_id=first.claim_id,
        case_id=first.case_id,
        stage=QualificationExecutionStage.PLANNER,
        profile_digest=candidate.planner_profile_digest,
        verdict=QualificationExecutionValidationVerdict.PASS,
        reason_code="PLANNER_VALIDATION_PASS",
    )
    ledger.close_case(
        claim_id=first.claim_id,
        status=QualificationExecutionCaseStatus.PASS,
        reason_code="MULTI_AGENT_READY",
    )

    second = ledger.claim_case(run_id=run_id, case_id=slots[1].case_id, case_digest=slots[1].case_digest)
    _append_stage(
        ledger,
        run_id=run_id,
        claim_id=second.claim_id,
        case_id=second.case_id,
        stage=QualificationExecutionStage.ANALYST,
        profile_digest=candidate.analyst_profile_digest,
        verdict=QualificationExecutionValidationVerdict.FAILED,
        reason_code="ANALYST_VALIDATION_FAILED_RUNNER_RESULT_SCHEMA_INVALID_EXPLANATION_MAX_LENGTH",
    )
    ledger.close_case(
        claim_id=second.claim_id,
        status=QualificationExecutionCaseStatus.FAILED,
        reason_code="ANALYST_VALIDATION_FAILED",
    )

    no_send = ledger.claim_case(run_id=run_id, case_id=slots[2].case_id, case_digest=slots[2].case_digest)
    ledger.close_case(
        claim_id=no_send.claim_id,
        status=QualificationExecutionCaseStatus.PASS,
        reason_code="NO_SEND_CONFORMANCE",
    )

    summary = ledger.execution_summary(run_id=run_id)
    assert summary.attempted_stage_count == summary.authenticated_receipt_count == 3
    assert [(item.status, item.reason_code) for item in summary.case_outcomes] == [
        (QualificationExecutionCaseStatus.PASS, "MULTI_AGENT_READY"),
        (QualificationExecutionCaseStatus.FAILED, "ANALYST_VALIDATION_FAILED"),
        (QualificationExecutionCaseStatus.PASS, "NO_SEND_CONFORMANCE"),
    ]
    with pytest.raises(Phase16QualificationLedgerError, match="already terminal"):
        ledger.claim_case(run_id=run_id, case_id=slots[0].case_id, case_digest=slots[0].case_digest)


def test_execution_ledger_blocks_planner_before_analyst_and_prevents_non_e2e_dispatch(
    execution_ledger_factory,
) -> None:
    ledger = execution_ledger_factory()
    _policy, candidate, campaign = _setup(ledger)
    run_id = "phase16-qualification-v2-development-run-002"
    e2e = QualificationExecutionSlot("qualification-development-e2e-001", "1" * 64, True)
    no_send = QualificationExecutionSlot("qualification-development-no-send-002", "2" * 64, False)
    ledger.begin_run_with_slots(run_id=run_id, campaign_id=campaign.campaign_id, slots=(e2e, no_send))
    e2e_claim = ledger.claim_case(run_id=run_id, case_id=e2e.case_id, case_digest=e2e.case_digest)
    with pytest.raises(Phase16QualificationLedgerError, match="dispatch intent failed"):
        ledger.begin_dispatch(
            run_id=run_id,
            claim_id=e2e_claim.claim_id,
            stage=QualificationExecutionStage.PLANNER,
            profile_digest=candidate.planner_profile_digest,
            internal_request_id=_request_id(e2e.case_id, QualificationExecutionStage.PLANNER),
            reservation_cny=Decimal("0.030000"),
        )
    no_send_claim = ledger.claim_case(run_id=run_id, case_id=no_send.case_id, case_digest=no_send.case_digest)
    with pytest.raises(Phase16QualificationLedgerError, match="non-E2E"):
        ledger.begin_dispatch(
            run_id=run_id,
            claim_id=no_send_claim.claim_id,
            stage=QualificationExecutionStage.ANALYST,
            profile_digest=candidate.analyst_profile_digest,
            internal_request_id=_request_id(no_send.case_id, QualificationExecutionStage.ANALYST),
            reservation_cny=Decimal("0.030000"),
        )


def test_execution_ledger_receipt_persists_retry_attempt_facts(
    execution_ledger_factory,
) -> None:
    """V9 receipt 必须持久化实际调用次数与响应端点，完整回执才记 receipt_complete。"""

    ledger = execution_ledger_factory()
    _policy, candidate, campaign = _setup(ledger)
    run_id = "phase16-qualification-v2-development-run-003"
    slot = QualificationExecutionSlot("qualification-development-e2e-001", "1" * 64, True)
    ledger.begin_run_with_slots(run_id=run_id, campaign_id=campaign.campaign_id, slots=(slot,))
    claim = ledger.claim_case(run_id=run_id, case_id=slot.case_id, case_digest=slot.case_digest)
    request_id = _request_id(slot.case_id, QualificationExecutionStage.ANALYST)
    attempt = ledger.begin_dispatch(
        run_id=run_id,
        claim_id=claim.claim_id,
        stage=QualificationExecutionStage.ANALYST,
        profile_digest=candidate.analyst_profile_digest,
        internal_request_id=request_id,
        reservation_cny=Decimal("0.030000"),
    )
    base = _success(request_id, f"{slot.case_id}-ANALYST")
    # StrictFrozenModel 禁止 model_copy(update=...)，直接重建带重试事实的成功回执。
    retried = ModelSuccess(
        request_id=request_id,
        model_id="deepseek-v4-pro",
        output={"kind": "FINAL", "final_output": {"status": "fixture"}},
        usage=base.usage,
        provider_response_id=base.provider_response_id,
        finish_reason="stop",
        response_digest=base.response_digest,
        latency_ms=base.latency_ms,
        attempts=2,
        endpoint_host="synapse-ai.uk",
    )
    assert ledger.append_receipt(attempt_id=attempt.attempt_id, success=retried) is True

    with psycopg.connect(**execution_ledger_factory.settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT attempt_count, responded_endpoint_host, receipt_complete "
                "FROM phase16_qualification_provider_receipts WHERE attempt_id=%s::uuid",
                (attempt.attempt_id,),
            )
            row = cursor.fetchone()
    assert row == (2, "synapse-ai.uk", True)


def test_execution_ledger_receipt_without_responded_endpoint_is_incomplete(
    execution_ledger_factory,
) -> None:
    """endpoint_host 缺失的成功回执必须落为 receipt_complete=false，不能通过门禁。"""

    ledger = execution_ledger_factory()
    _policy, candidate, campaign = _setup(ledger)
    run_id = "phase16-qualification-v2-development-run-004"
    slot = QualificationExecutionSlot("qualification-development-e2e-001", "1" * 64, True)
    ledger.begin_run_with_slots(run_id=run_id, campaign_id=campaign.campaign_id, slots=(slot,))
    claim = ledger.claim_case(run_id=run_id, case_id=slot.case_id, case_digest=slot.case_digest)
    request_id = _request_id(slot.case_id, QualificationExecutionStage.ANALYST)
    attempt = ledger.begin_dispatch(
        run_id=run_id,
        claim_id=claim.claim_id,
        stage=QualificationExecutionStage.ANALYST,
        profile_digest=candidate.analyst_profile_digest,
        internal_request_id=request_id,
        reservation_cny=Decimal("0.030000"),
    )
    base = _success(request_id, f"{slot.case_id}-ANALYST")
    bare = ModelSuccess(
        request_id=request_id,
        model_id="deepseek-v4-pro",
        output={"kind": "FINAL", "final_output": {"status": "fixture"}},
        usage=base.usage,
        provider_response_id=base.provider_response_id,
        finish_reason="stop",
        response_digest=base.response_digest,
        latency_ms=base.latency_ms,
    )
    assert ledger.append_receipt(attempt_id=attempt.attempt_id, success=bare) is False

    with psycopg.connect(**execution_ledger_factory.settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT attempt_count, responded_endpoint_host, receipt_complete "
                "FROM phase16_qualification_provider_receipts WHERE attempt_id=%s::uuid",
                (attempt.attempt_id,),
            )
            row = cursor.fetchone()
    assert row == (1, None, False)


def test_execution_ledger_campaign_budget_counts_settled_actual_cost(
    execution_ledger_factory,
) -> None:
    """settle 后的 attempt 按实际成本占帽：历史 worst_case 预留总和不得虚占 campaign 预算。

    旧语义累计所有历史预留（0.06+0.06=0.12 > 0.10 帽）会误判预算不足；
    新语义按"未决预留 + 已决实际成本"（0.06+0.02=0.08 <= 0.10）放行。
    """

    ledger = execution_ledger_factory()
    _policy, candidate, campaign = _setup(ledger)
    # campaign 的 UNIQUE(policy, kind, candidate, batch) 不允许同一 candidate 下
    # 并存两个 DEVELOPMENT campaign；tight 帽用独立 candidate 身份承载。
    tight_candidate = build_qualification_candidate(
        candidate_id="phase16-qualification-v2-tight-budget-candidate",
        policy=_policy,
        analyst_profile_digest="a" * 64,
        planner_profile_digest="b" * 64,
        adapter_digest="d" * 64,
    )
    ledger.ensure_candidate(tight_candidate)
    tight = QualificationCampaign(
        campaign_id="phase16-qualification-v2-development-tight-budget-001",
        campaign_kind=QualificationCampaignKind.DEVELOPMENT,
        policy_digest=campaign.policy_digest,
        corpus_digest=campaign.corpus_digest,
        candidate_digest=tight_candidate.candidate_digest or "",
        manifest_digest=campaign.manifest_digest,
        reservation_cny="0.100000",
    )
    ledger.ensure_campaign(tight)
    run_id = "phase16-qualification-v2-development-run-005"
    slot = QualificationExecutionSlot("qualification-development-e2e-001", "1" * 64, True)
    ledger.begin_run_with_slots(run_id=run_id, campaign_id=tight.campaign_id, slots=(slot,))
    claim = ledger.claim_case(run_id=run_id, case_id=slot.case_id, case_digest=slot.case_digest)

    analyst_request = _request_id(slot.case_id, QualificationExecutionStage.ANALYST)
    analyst = ledger.begin_dispatch(
        run_id=run_id,
        claim_id=claim.claim_id,
        stage=QualificationExecutionStage.ANALYST,
        profile_digest=candidate.analyst_profile_digest,
        internal_request_id=analyst_request,
        reservation_cny=Decimal("0.060000"),
    )
    assert ledger.append_receipt(
        attempt_id=analyst.attempt_id,
        success=_success(analyst_request, f"{slot.case_id}-ANALYST"),
    ) is True
    ledger.append_validation(
        attempt_id=analyst.attempt_id,
        verdict=QualificationExecutionValidationVerdict.PASS,
        reason_code="ANALYST_VALIDATION_PASS",
        validation_digest=canonical_json_sha256({"attempt_id": analyst.attempt_id}),
    )

    planner_request = _request_id(slot.case_id, QualificationExecutionStage.PLANNER)
    planner = ledger.begin_dispatch(
        run_id=run_id,
        claim_id=claim.claim_id,
        stage=QualificationExecutionStage.PLANNER,
        profile_digest=candidate.planner_profile_digest,
        internal_request_id=planner_request,
        reservation_cny=Decimal("0.060000"),
    )
    assert planner.internal_request_id == planner_request

"""V8 只读证据适配器在独立 PostgreSQL schema 中的认证/真值契约。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
from psycopg import sql
import pytest

from src.config.settings import get_settings
from src.decision_support.controlled_e2e_ledger_v5 import (
    Phase16V5CaseOutcomeStatus,
    Phase16V5DispatchStage,
    Phase16V5RunKind,
    Phase16V5RunStatus,
    Phase16V5ValidationVerdict,
    PostgresPhase16V5CampaignLedger,
    initialize_phase16_v5_controlled_e2e_schema,
)
from src.decision_support.controlled_e2e_v5 import (
    PHASE16_V5_CALIBRATION_RUN_ID,
    PHASE16_V5_FORMAL_RUN_ID,
    load_phase16_v5_manifest,
)
from src.decision_support.phase16_qualification_evaluator import V8ReadOnlyEvidenceAdapter
from src.decision_support.phase16_qualification_ledger import SourceEvidenceIntegrity
from src.specialist_runtime.model_port import ModelSuccess, ModelUsage
from src.specialist_runtime.models import canonical_json_sha256


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TEST_HMAC_KEY = bytes.fromhex("3a" * 32)
_WRONG_HMAC_KEY = bytes.fromhex("4b" * 32)


@pytest.fixture()
def v8_source_settings():
    base_kwargs = dict(get_settings().postgres_connection_kwargs)
    schema_name = f"phase16_qualification_v8_source_{uuid4().hex}"
    with psycopg.connect(**base_kwargs) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        connection.commit()
    settings = SimpleNamespace(
        postgres_connection_kwargs={**base_kwargs, "options": f"-c search_path={schema_name}"}
    )
    initialize_phase16_v5_controlled_e2e_schema(settings)
    try:
        yield settings
    finally:
        with psycopg.connect(**base_kwargs) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name)))
            connection.commit()


def _request_id(*, case_id: str, stage: Phase16V5DispatchStage) -> str:
    return str(uuid5(NAMESPACE_URL, f"phase16-qualification-v8-source:{case_id}:{stage.value}"))


def _success(*, request_id: str, suffix: str) -> ModelSuccess:
    output = {"kind": "FINAL", "final_output": {"status": "fixture"}}
    return ModelSuccess(
        request_id=request_id,
        model_id="deepseek-v4-pro",
        output=output,
        usage=ModelUsage(input_tokens=100, output_tokens=100, total_tokens=200),
        provider_response_id=f"qualification-v8-source-{suffix}",
        finish_reason="stop",
        response_digest=canonical_json_sha256(output),
        latency_ms=Decimal("1.250"),
    )


def _append_stage(
    ledger: PostgresPhase16V5CampaignLedger,
    *,
    run_id: str,
    claim_id: str,
    case_id: str,
    stage: Phase16V5DispatchStage,
    profile_digest: str,
    verdict: Phase16V5ValidationVerdict,
    reason_code: str,
) -> None:
    request_id = _request_id(case_id=case_id, stage=stage)
    attempt = ledger.begin_dispatch(
        run_id=run_id,
        claim_id=claim_id,
        stage=stage,
        profile_digest=profile_digest,
        internal_request_id=request_id,
        reservation_cny=Decimal("0.030000"),
    )
    assert ledger.append_receipt(
        attempt_id=attempt.attempt_id,
        success=_success(request_id=request_id, suffix=f"{case_id}-{stage.value}"),
    )
    ledger.append_validation(
        attempt_id=attempt.attempt_id,
        verdict=verdict,
        reason_code=reason_code,
        validation_digest=canonical_json_sha256(
            {"attempt_id": attempt.attempt_id, "reason_code": reason_code}
        ),
    )


def _seed_v8_fact_pattern(settings) -> None:
    """写入与真实 V8 相同的 7/10、19 stage、两类细失败，仅用于适配器认证测试。"""

    manifest = load_phase16_v5_manifest(repository_root=_PROJECT_ROOT)
    ledger = PostgresPhase16V5CampaignLedger(settings, hmac_key=_TEST_HMAC_KEY)
    ledger.ensure_campaign(manifest)
    ledger.begin_run(
        run_id=PHASE16_V5_CALIBRATION_RUN_ID,
        run_kind=Phase16V5RunKind.CALIBRATION,
        manifest=manifest,
    )
    calibration = ledger.claim_case(
        run_id=PHASE16_V5_CALIBRATION_RUN_ID,
        case_id=manifest.calibration_case_id,
        case_digest=manifest.calibration_case_digest,
    )
    _append_stage(
        ledger,
        run_id=PHASE16_V5_CALIBRATION_RUN_ID,
        claim_id=calibration.claim_id,
        case_id=calibration.case_id,
        stage=Phase16V5DispatchStage.ANALYST,
        profile_digest=manifest.profile_digests["analyst"],
        verdict=Phase16V5ValidationVerdict.PASS,
        reason_code="ANALYST_VALIDATION_PASS",
    )
    _append_stage(
        ledger,
        run_id=PHASE16_V5_CALIBRATION_RUN_ID,
        claim_id=calibration.claim_id,
        case_id=calibration.case_id,
        stage=Phase16V5DispatchStage.PLANNER,
        profile_digest=manifest.profile_digests["planner"],
        verdict=Phase16V5ValidationVerdict.PASS,
        reason_code="PLANNER_VALIDATION_PASS",
    )
    ledger.close_case(
        claim_id=calibration.claim_id,
        status=Phase16V5CaseOutcomeStatus.PASS,
        reason_code="MULTI_AGENT_READY",
    )
    ledger.close_run(
        run_id=PHASE16_V5_CALIBRATION_RUN_ID,
        status=Phase16V5RunStatus.PASS,
        reason_code="CONTROLLED_E2E_QUALIFIED",
    )

    ledger.begin_run(
        run_id=PHASE16_V5_FORMAL_RUN_ID,
        run_kind=Phase16V5RunKind.FORMAL,
        manifest=manifest,
    )
    planner_failures = {manifest.formal_case_ids[2], manifest.formal_case_ids[6]}
    analyst_failure = manifest.formal_case_ids[4]
    for case_id in manifest.formal_case_ids:
        claim = ledger.claim_case(
            run_id=PHASE16_V5_FORMAL_RUN_ID,
            case_id=case_id,
            case_digest=manifest.formal_case_digests[case_id],
        )
        if case_id == analyst_failure:
            _append_stage(
                ledger,
                run_id=PHASE16_V5_FORMAL_RUN_ID,
                claim_id=claim.claim_id,
                case_id=case_id,
                stage=Phase16V5DispatchStage.ANALYST,
                profile_digest=manifest.profile_digests["analyst"],
                verdict=Phase16V5ValidationVerdict.FAILED,
                reason_code="ANALYST_VALIDATION_FAILED_RUNNER_RESULT_SCHEMA_INVALID_EXPLANATION_MAX_LENGTH",
            )
            ledger.close_case(
                claim_id=claim.claim_id,
                status=Phase16V5CaseOutcomeStatus.FAILED,
                reason_code="ANALYST_VALIDATION_FAILED",
            )
            continue
        _append_stage(
            ledger,
            run_id=PHASE16_V5_FORMAL_RUN_ID,
            claim_id=claim.claim_id,
            case_id=case_id,
            stage=Phase16V5DispatchStage.ANALYST,
            profile_digest=manifest.profile_digests["analyst"],
            verdict=Phase16V5ValidationVerdict.PASS,
            reason_code="ANALYST_VALIDATION_PASS",
        )
        if case_id in planner_failures:
            _append_stage(
                ledger,
                run_id=PHASE16_V5_FORMAL_RUN_ID,
                claim_id=claim.claim_id,
                case_id=case_id,
                stage=Phase16V5DispatchStage.PLANNER,
                profile_digest=manifest.profile_digests["planner"],
                verdict=Phase16V5ValidationVerdict.FAILED,
                reason_code="PLANNER_VALIDATION_FAILED_PLANNER_RISK_COVERAGE",
            )
            ledger.close_case(
                claim_id=claim.claim_id,
                status=Phase16V5CaseOutcomeStatus.FAILED,
                reason_code="PLANNER_VALIDATION_FAILED",
            )
            continue
        _append_stage(
            ledger,
            run_id=PHASE16_V5_FORMAL_RUN_ID,
            claim_id=claim.claim_id,
            case_id=case_id,
            stage=Phase16V5DispatchStage.PLANNER,
            profile_digest=manifest.profile_digests["planner"],
            verdict=Phase16V5ValidationVerdict.PASS,
            reason_code="PLANNER_VALIDATION_PASS",
        )
        ledger.close_case(
            claim_id=claim.claim_id,
            status=Phase16V5CaseOutcomeStatus.PASS,
            reason_code="MULTI_AGENT_READY",
        )
    ledger.close_run(
        run_id=PHASE16_V5_FORMAL_RUN_ID,
        status=Phase16V5RunStatus.FAILED,
        reason_code="PLANNER_VALIDATION_FAILED",
    )


def test_v8_read_only_adapter_authenticates_and_truthfully_classifies_source_facts(v8_source_settings) -> None:
    _seed_v8_fact_pattern(v8_source_settings)

    observation = V8ReadOnlyEvidenceAdapter(
        v8_source_settings,
        hmac_key=_TEST_HMAC_KEY,
        repository_root=_PROJECT_ROOT,
    ).read_v8_formal()
    assert observation.integrity_status is SourceEvidenceIntegrity.AUTHENTICATED
    assert observation.run_status == "FAILED"
    assert observation.attempted_stage_count == 19
    assert (observation.case_pass_count, observation.case_total_count) == (7, 10)
    assert dict(observation.failure_categories) == {
        "ANALYST_EXPLANATION_BOUND": 1,
        "PLANNER_RISK_COVERAGE": 2,
    }
    assert observation.qualification_eligible is False

    unverified = V8ReadOnlyEvidenceAdapter(
        v8_source_settings,
        hmac_key=_WRONG_HMAC_KEY,
        repository_root=_PROJECT_ROOT,
    ).read_v8_formal()
    assert unverified.integrity_status is SourceEvidenceIntegrity.UNVERIFIABLE
    assert unverified.case_pass_count == 7

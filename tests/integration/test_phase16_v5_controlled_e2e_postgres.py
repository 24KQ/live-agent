"""Phase 16 V5 受控 E2E PostgreSQL 账本集成契约。

每条测试都在临时 PostgreSQL schema 中执行真实 V5 DDL、触发器和行锁。模型端口为确定性
Fake，不读取 ``.env``、不使用用户密钥，也不会向 DeepSeek 发送网络请求。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
from psycopg import sql
import pytest

from src.config.settings import get_settings
from src.decision_support.controlled_e2e_ledger_v5 import (
    Phase16V5CampaignLedgerError,
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
    Phase16V5ControlledE2ERunner,
    Phase16V5ExecutionStatus,
    load_phase16_v5_manifest,
    load_phase16_v5_parent_dataset,
)
from src.decision_support.controlled_e2e_adapter_v5 import DeepSeekV5ThinkingMode
from src.specialist_runtime.model_port import ModelSuccess, ModelUsage
from src.specialist_runtime.models import canonical_json_sha256


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TEST_HMAC_KEY = bytes.fromhex("5d" * 32)


@pytest.fixture()
def postgres_v5_ledger_factory():
    """为每个案例建立独立 schema，证明 V5 的零重试与不可变性来自 PostgreSQL。"""

    base_kwargs = dict(get_settings().postgres_connection_kwargs)
    schema_name = f"phase16_v5_controlled_e2e_{uuid4().hex}"
    with psycopg.connect(**base_kwargs) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        connection.commit()
    settings = SimpleNamespace(
        postgres_connection_kwargs={
            **base_kwargs,
            "options": f"-c search_path={schema_name}",
        }
    )
    initialize_phase16_v5_controlled_e2e_schema(settings)

    def build_ledger() -> PostgresPhase16V5CampaignLedger:
        """每次返回新进程视角的账本，供恢复测试确认数据库是唯一权威事实源。"""

        return PostgresPhase16V5CampaignLedger(settings, hmac_key=_TEST_HMAC_KEY)

    build_ledger.settings = settings
    try:
        yield build_ledger
    finally:
        with psycopg.connect(**base_kwargs) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )
            connection.commit()


def _manifest():
    """读取版本化 V5 Manifest；集成测试不临时造 case 或替换预算、模型、Profile 身份。"""

    return load_phase16_v5_manifest(repository_root=_PROJECT_ROOT)


def _request_id(label: str) -> str:
    """构造规范 UUID，避免自由文本进入正式账本的内部请求 ID 列。"""

    return str(uuid5(NAMESPACE_URL, f"phase16-v5-postgres-test:{label}"))


def _start_calibration(ledger: PostgresPhase16V5CampaignLedger, manifest):
    """初始化冻结校准 run 并领取唯一合成 slot，返回后续两阶段所需的公开 claim。"""

    ledger.ensure_campaign(manifest)
    ledger.begin_run(
        run_id=PHASE16_V5_CALIBRATION_RUN_ID,
        run_kind=Phase16V5RunKind.CALIBRATION,
        manifest=manifest,
    )
    return ledger.claim_case(
        run_id=PHASE16_V5_CALIBRATION_RUN_ID,
        case_id=manifest.calibration_case_id,
        case_digest=manifest.formal_case_digests[manifest.calibration_parent_case_id],
    )


def _success(*, request_id: str, provider_suffix: str, finish_reason: str = "stop") -> ModelSuccess:
    """构造完整但无经营含义的供应商回执，以验证账本而非真实模型服务。"""

    output = {"kind": "FINAL", "final_output": {"status": "test"}}
    return ModelSuccess(
        request_id=request_id,
        model_id="deepseek-v4-pro",
        output=output,
        usage=ModelUsage(input_tokens=100, output_tokens=100, total_tokens=200),
        provider_response_id=f"phase16-v5-test-provider-{provider_suffix}",
        finish_reason=finish_reason,
        response_digest=canonical_json_sha256(output),
        latency_ms=Decimal("2.0004"),
    )


def _append_analyst_pass(ledger: PostgresPhase16V5CampaignLedger, claim, manifest):
    """持久化一条经 HMAC 认证的 Analyst PASS，供 Planner 顺序和成功闭合测试复用。"""

    request_id = _request_id(f"analyst:{claim.case_id}")
    attempt = ledger.begin_dispatch(
        run_id=claim.run_id,
        claim_id=claim.claim_id,
        stage=Phase16V5DispatchStage.ANALYST,
        profile_digest=manifest.profile_digests["analyst"],
        internal_request_id=request_id,
        reservation_cny=Decimal("0.030000"),
    )
    assert ledger.append_receipt(
        attempt_id=attempt.attempt_id,
        success=_success(request_id=request_id, provider_suffix="analyst"),
    )
    ledger.append_validation(
        attempt_id=attempt.attempt_id,
        verdict=Phase16V5ValidationVerdict.PASS,
        reason_code="ANALYST_VALIDATION_PASS",
        validation_digest="a" * 64,
    )
    return attempt


def test_v5_ledger_requires_analyst_pass_before_planner_and_rejects_receipt_mutation(
    postgres_v5_ledger_factory,
) -> None:
    """Planner 必须等待认证 Analyst PASS，且落库后的 receipt 不能由直接 SQL 篡改。"""

    ledger = postgres_v5_ledger_factory()
    manifest = _manifest()
    claim = _start_calibration(ledger, manifest)

    with pytest.raises(Phase16V5CampaignLedgerError, match="planner requires validated analyst"):
        ledger.begin_dispatch(
            run_id=claim.run_id,
            claim_id=claim.claim_id,
            stage=Phase16V5DispatchStage.PLANNER,
            profile_digest=manifest.profile_digests["planner"],
            internal_request_id=_request_id("planner-before-analyst"),
            reservation_cny=Decimal("0.030000"),
        )

    analyst = _append_analyst_pass(ledger, claim, manifest)
    with psycopg.connect(**postgres_v5_ledger_factory.settings.postgres_connection_kwargs) as connection:
        with pytest.raises(psycopg.Error, match="append-only"):
            connection.execute(
                "UPDATE phase16_v5_provider_receipts SET receipt_auth_tag=%s WHERE attempt_id=%s::uuid",
                ("0" * 64, analyst.attempt_id),
            )
        connection.rollback()

    planner = ledger.begin_dispatch(
        run_id=claim.run_id,
        claim_id=claim.claim_id,
        stage=Phase16V5DispatchStage.PLANNER,
        profile_digest=manifest.profile_digests["planner"],
        internal_request_id=_request_id("planner-after-analyst"),
        reservation_cny=Decimal("0.030000"),
    )
    assert planner.stage is Phase16V5DispatchStage.PLANNER


def test_v5_non_stop_receipt_closes_the_run_and_blocks_later_case_claims(
    postgres_v5_ledger_factory,
) -> None:
    """非 stop 回执属于已发送失败，必须关闭 run 而非被解释成可重试的本地阻断。"""

    ledger = postgres_v5_ledger_factory()
    manifest = _manifest()
    claim = _start_calibration(ledger, manifest)
    request_id = _request_id("non-stop")
    attempt = ledger.begin_dispatch(
        run_id=claim.run_id,
        claim_id=claim.claim_id,
        stage=Phase16V5DispatchStage.ANALYST,
        profile_digest=manifest.profile_digests["analyst"],
        internal_request_id=request_id,
        reservation_cny=Decimal("0.030000"),
    )

    assert not ledger.append_receipt(
        attempt_id=attempt.attempt_id,
        success=_success(request_id=request_id, provider_suffix="length", finish_reason="length"),
    )
    ledger.append_validation(
        attempt_id=attempt.attempt_id,
        verdict=Phase16V5ValidationVerdict.FAILED,
        reason_code="PROVIDER_RECEIPT_INVALID",
        validation_digest="b" * 64,
    )
    ledger.close_case(
        claim_id=claim.claim_id,
        status=Phase16V5CaseOutcomeStatus.FAILED,
        reason_code="PROVIDER_RECEIPT_INVALID",
    )
    ledger.close_run(
        run_id=claim.run_id,
        status=Phase16V5RunStatus.FAILED,
        reason_code="PROVIDER_RECEIPT_INVALID",
    )

    with pytest.raises(Phase16V5CampaignLedgerError, match="terminal"):
        ledger.claim_case(
            run_id=claim.run_id,
            case_id=manifest.calibration_case_id,
            case_digest=manifest.formal_case_digests[manifest.calibration_parent_case_id],
        )


def test_v5_recovers_open_sent_intent_as_unknown_and_never_resends(
    postgres_v5_ledger_factory,
) -> None:
    """崩溃遗留的网络前 intent 必须封口为未知已发送失败，恢复后不能领取或再次发送。"""

    first_process = postgres_v5_ledger_factory()
    manifest = _manifest()
    claim = _start_calibration(first_process, manifest)
    first_process.begin_dispatch(
        run_id=claim.run_id,
        claim_id=claim.claim_id,
        stage=Phase16V5DispatchStage.ANALYST,
        profile_digest=manifest.profile_digests["analyst"],
        internal_request_id=_request_id("crash-after-intent"),
        reservation_cny=Decimal("0.030000"),
    )

    recovered = postgres_v5_ledger_factory().recover_open_attempts()

    assert len(recovered) == 1
    assert recovered[0].run_id == PHASE16_V5_CALIBRATION_RUN_ID
    assert recovered[0].reason_code == "UNKNOWN_ATTEMPT_AFTER_RESTART"
    assert recovered[0].status is Phase16V5CaseOutcomeStatus.FAILED
    with pytest.raises(Phase16V5CampaignLedgerError, match="terminal"):
        postgres_v5_ledger_factory().claim_case(
            run_id=claim.run_id,
            case_id=manifest.calibration_case_id,
            case_digest=manifest.formal_case_digests[manifest.calibration_parent_case_id],
        )


def test_v5_recovers_validated_analyst_without_case_outcome_as_terminal_failure(
    postgres_v5_ledger_factory,
) -> None:
    """Analyst 已通过但进程在 Planner 前崩溃时，恢复必须封闭 E2E 而不是篡改 PASS 或重发。

    这个边界与“未验证 intent”不同：Provider receipt 和 Analyst validation 都是真实且完整的
    历史事实，不能改写成第二条失败 validation。独立 recovery fact 只说明双阶段 case 没有
    被完整闭合，故 run 只能成为不可重试的 FAILED。
    """

    first_process = postgres_v5_ledger_factory()
    manifest = _manifest()
    claim = _start_calibration(first_process, manifest)
    _append_analyst_pass(first_process, claim, manifest)

    recovered = postgres_v5_ledger_factory().recover_incomplete_cases()

    assert len(recovered) == 1
    assert recovered[0].run_id == PHASE16_V5_CALIBRATION_RUN_ID
    assert recovered[0].case_id == manifest.calibration_case_id
    assert recovered[0].status is Phase16V5CaseOutcomeStatus.FAILED
    assert recovered[0].reason_code == "INCOMPLETE_VALIDATED_CASE_AFTER_RESTART"
    summary = postgres_v5_ledger_factory().report_summary(
        run_id=PHASE16_V5_CALIBRATION_RUN_ID
    )
    assert summary["status"] == "FAILED"
    assert summary["reason_code"] == "INCOMPLETE_VALIDATED_CASE_AFTER_RESTART"
    assert summary["attempt_count"] == 1


class _DeterministicV5Port:
    """按共享 Runner 的受控上下文构造合法 JSON，用于真实 PostgreSQL 的离线 E2E 验证。"""

    thinking_mode = DeepSeekV5ThinkingMode.DISABLED

    def __init__(self) -> None:
        """保存请求数，断言校准严格经过 Analyst 和 Planner 两次共享 Runner 调用。"""

        self.requests: list[object] = []

    async def complete(self, request):
        """只使用已解析的 evidence IDs 生成受限 JSON，绝不请求真实 Provider。"""

        self.requests.append(request)
        context = json.loads(request.messages[-1].content)
        evidence_ids = [item["evidence_id"] for item in context["resolved_evidence"]]
        if "trigger_codes" in context["input_snapshot"]:
            final_output = {
                "constraint_codes": [],
                "risk_codes": ["HUMAN_CONFIRMATION_REQUIRED"],
                "explanation": "受控测试证据要求人工确认。",
                "evidence_ids": evidence_ids,
            }
        else:
            final_output = {
                "options": [
                    {
                        "option_id": "hold-for-review",
                        "product_strategy": "HOLD_AND_ESCALATE",
                        "backup_product_id": None,
                        "host_prompt": "等待人工确认。",
                        "timing": "AFTER_OPERATOR_CONFIRMATION",
                        "risk_flags": ["HUMAN_CONFIRMATION_REQUIRED"],
                        "evidence_ids": evidence_ids,
                    }
                ]
            }
        output = {
            "kind": "FINAL",
            "final_output": final_output,
            "reason_summary": "V5_POSTGRES_CONTRACT_TEST",
        }
        return ModelSuccess(
            request_id=request.request_id,
            model_id="deepseek-v4-pro",
            output=output,
            usage=ModelUsage(input_tokens=100, output_tokens=100, total_tokens=200),
            provider_response_id=f"phase16-v5-runner-{len(self.requests):03d}",
            finish_reason="stop",
            response_digest=canonical_json_sha256(output),
            latency_ms=Decimal("2.000"),
        )


def test_v5_runner_completes_calibration_via_postgresql_and_renders_summary(
    postgres_v5_ledger_factory,
) -> None:
    """校准 PASS 必须经真实 DDL 两阶段闭合，报告查询也只能返回脱敏计量摘要。"""

    manifest = _manifest()
    port = _DeterministicV5Port()
    ledger = postgres_v5_ledger_factory()
    runner = Phase16V5ControlledE2ERunner(
        dataset=load_phase16_v5_parent_dataset(repository_root=_PROJECT_ROOT),
        manifest=manifest,
        ledger=ledger,
        model_port=port,
        # 父数据验证使用固定历史时刻，避免现在的墙钟把冻结证据误判为陈旧。
        clock=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
    )

    report = asyncio.run(runner.execute(run_kind=Phase16V5RunKind.CALIBRATION))
    summary = ledger.report_summary(run_id=PHASE16_V5_CALIBRATION_RUN_ID)

    assert report.status is Phase16V5ExecutionStatus.PASS
    assert report.model_calls == len(port.requests) == 2
    assert summary["status"] == "PASS"
    assert summary["attempt_count"] == 2
    assert summary["actual_cost_cny"] == "0.001800"


def test_v5_formal_run_requires_calibration_then_closes_all_ten_postgresql_slots(
    postgres_v5_ledger_factory,
) -> None:
    """正式 run 必须由同一 append-only campaign 的校准 PASS 解锁，并严格关闭十个固定 slot。"""

    manifest = _manifest()
    ledger = postgres_v5_ledger_factory()
    port = _DeterministicV5Port()
    runner = Phase16V5ControlledE2ERunner(
        dataset=load_phase16_v5_parent_dataset(repository_root=_PROJECT_ROOT),
        manifest=manifest,
        ledger=ledger,
        model_port=port,
        clock=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
    )

    calibration = asyncio.run(runner.execute(run_kind=Phase16V5RunKind.CALIBRATION))
    formal = asyncio.run(runner.execute(run_kind=Phase16V5RunKind.FORMAL))
    summary = ledger.report_summary(run_id=PHASE16_V5_FORMAL_RUN_ID)

    assert calibration.status is Phase16V5ExecutionStatus.PASS
    assert formal.status is Phase16V5ExecutionStatus.PASS
    assert formal.evidence_conclusion.value == "CONTROLLED_E2E_QUALIFIED"
    assert formal.model_calls == 20
    assert len(formal.case_executions) == 10
    assert summary["status"] == "PASS"
    assert summary["attempt_count"] == 20
    assert len(summary["case_outcomes"]) == 10


def test_v5_ledger_rejects_campaign_conflict_unreleased_formal_and_invalid_dispatch_inputs(
    postgres_v5_ledger_factory,
) -> None:
    """身份冲突、无校准正式启动、错误 slot/claim/预约和未知报告均应被数据库边界拒绝。"""

    ledger = postgres_v5_ledger_factory()
    manifest = _manifest()
    ledger.ensure_campaign(manifest)
    conflicting = SimpleNamespace(
        campaign_id=manifest.campaign_id,
        manifest_digest="0" * 64,
        total_budget_cny=manifest.total_budget_cny,
        input_cny_per_million=manifest.input_cny_per_million,
        output_cny_per_million=manifest.output_cny_per_million,
        profile_digests=manifest.profile_digests,
        model_id=manifest.model_id,
    )
    with pytest.raises(Phase16V5CampaignLedgerError, match="identity conflicts"):
        ledger.ensure_campaign(conflicting)
    assert ledger.calibration_passed() is False
    with pytest.raises(Phase16V5CampaignLedgerError, match="calibration PASS"):
        ledger.begin_run(
            run_id="phase16-v5-formal-001",
            run_kind=Phase16V5RunKind.FORMAL,
            manifest=manifest,
        )
    ledger.begin_run(
        run_id=PHASE16_V5_CALIBRATION_RUN_ID,
        run_kind=Phase16V5RunKind.CALIBRATION,
        manifest=manifest,
    )
    with pytest.raises(Phase16V5CampaignLedgerError, match="slot identity"):
        ledger.claim_case(
            run_id=PHASE16_V5_CALIBRATION_RUN_ID,
            case_id="unknown-slot",
            case_digest="0" * 64,
        )
    claim = ledger.claim_case(
        run_id=PHASE16_V5_CALIBRATION_RUN_ID,
        case_id=manifest.calibration_case_id,
        case_digest=manifest.formal_case_digests[manifest.calibration_parent_case_id],
    )
    with pytest.raises(Phase16V5CampaignLedgerError, match="reservation"):
        ledger.begin_dispatch(
            run_id=claim.run_id,
            claim_id=claim.claim_id,
            stage=Phase16V5DispatchStage.ANALYST,
            profile_digest=manifest.profile_digests["analyst"],
            internal_request_id=_request_id("invalid-reservation"),
            reservation_cny=Decimal("0.030001"),
        )
    with pytest.raises(Phase16V5CampaignLedgerError, match="profile identity"):
        ledger.begin_dispatch(
            run_id=claim.run_id,
            claim_id=claim.claim_id,
            stage=Phase16V5DispatchStage.ANALYST,
            profile_digest="0" * 64,
            internal_request_id=_request_id("wrong-profile"),
            reservation_cny=Decimal("0.030000"),
        )
    with pytest.raises(Phase16V5CampaignLedgerError, match="claim is unknown"):
        ledger.begin_dispatch(
            run_id=claim.run_id,
            claim_id=str(uuid5(NAMESPACE_URL, "v5-unknown-claim")),
            stage=Phase16V5DispatchStage.ANALYST,
            profile_digest=manifest.profile_digests["analyst"],
            internal_request_id=_request_id("unknown-claim"),
            reservation_cny=Decimal("0.030000"),
        )
    with pytest.raises(Phase16V5CampaignLedgerError, match="report run is unknown"):
        ledger.report_summary(run_id="unknown-v5-run")
    with pytest.raises(Phase16V5CampaignLedgerError, match="case claim is unknown"):
        ledger.close_case(
            claim_id=str(uuid5(NAMESPACE_URL, "v5-close-unknown-claim")),
            status=Phase16V5CaseOutcomeStatus.FAILED,
            reason_code="CASE_CLAIM_BLOCKED",
        )
    with pytest.raises(Phase16V5CampaignLedgerError, match="run is unknown"):
        ledger.close_run(
            run_id="phase16-v5-unknown-run",
            status=Phase16V5RunStatus.FAILED,
            reason_code="RUN_UNKNOWN",
        )


def test_v5_receipt_identity_and_incomplete_facts_remain_fail_closed(
    postgres_v5_ledger_factory,
) -> None:
    """错误 request 或缺失 Provider 回执都不能把已发请求升级为 case PASS。"""

    ledger = postgres_v5_ledger_factory()
    manifest = _manifest()
    claim = _start_calibration(ledger, manifest)
    request_id = _request_id("receipt-identity")
    attempt = ledger.begin_dispatch(
        run_id=claim.run_id,
        claim_id=claim.claim_id,
        stage=Phase16V5DispatchStage.ANALYST,
        profile_digest=manifest.profile_digests["analyst"],
        internal_request_id=request_id,
        reservation_cny=Decimal("0.030000"),
    )
    with pytest.raises(ValueError, match="validation digest"):
        ledger.append_validation(
            attempt_id=attempt.attempt_id,
            verdict=Phase16V5ValidationVerdict.FAILED,
            reason_code="PROVIDER_RECEIPT_INVALID",
            validation_digest="short",
        )
    with pytest.raises(Phase16V5CampaignLedgerError, match="receipt identity"):
        ledger.append_receipt(
            attempt_id=attempt.attempt_id,
            success=_success(request_id=_request_id("wrong-receipt-id"), provider_suffix="wrong"),
        )
    incomplete = ModelSuccess(
        request_id=request_id,
        model_id="deepseek-v4-pro",
        output={"kind": "FINAL", "final_output": {"status": "test"}},
        usage=None,
        provider_response_id=None,
        finish_reason=None,
        response_digest="c" * 64,
        latency_ms=Decimal("1.000"),
    )
    assert not ledger.append_receipt(
        attempt_id=attempt.attempt_id,
        success=incomplete,
    )
    ledger.append_validation(
        attempt_id=attempt.attempt_id,
        verdict=Phase16V5ValidationVerdict.FAILED,
        reason_code="PROVIDER_RECEIPT_INVALID",
        validation_digest="f" * 64,
    )
    ledger.close_case(
        claim_id=claim.claim_id,
        status=Phase16V5CaseOutcomeStatus.FAILED,
        reason_code="PROVIDER_RECEIPT_INVALID",
    )
    ledger.close_run(
        run_id=claim.run_id,
        status=Phase16V5RunStatus.FAILED,
        reason_code="PROVIDER_RECEIPT_INVALID",
    )
    summary = ledger.report_summary(run_id=claim.run_id)
    assert summary["status"] == "FAILED"
    assert summary["actual_cost_cny"] == "0.000000"


def test_v5_case_pass_rechecks_receipt_hmac_before_writing_outcome(
    postgres_v5_ledger_factory,
) -> None:
    """即使 SQL 形状完整，使用错误签名密钥的进程也不能关闭一个 PASS case。"""

    manifest = _manifest()
    authenticated_ledger = postgres_v5_ledger_factory()
    authenticated_claim = _start_calibration(authenticated_ledger, manifest)
    _append_analyst_pass(authenticated_ledger, authenticated_claim, manifest)
    with pytest.raises(Phase16V5CampaignLedgerError, match="two stage receipts"):
        authenticated_ledger.close_case(
            claim_id=authenticated_claim.claim_id,
            status=Phase16V5CaseOutcomeStatus.PASS,
            reason_code="MULTI_AGENT_READY",
        )
    planner_request_id = _request_id(f"planner-hmac:{authenticated_claim.case_id}")
    planner = authenticated_ledger.begin_dispatch(
        run_id=authenticated_claim.run_id,
        claim_id=authenticated_claim.claim_id,
        stage=Phase16V5DispatchStage.PLANNER,
        profile_digest=manifest.profile_digests["planner"],
        internal_request_id=planner_request_id,
        reservation_cny=Decimal("0.030000"),
    )
    assert authenticated_ledger.append_receipt(
        attempt_id=planner.attempt_id,
        success=_success(request_id=planner_request_id, provider_suffix="planner-hmac"),
    )
    authenticated_ledger.append_validation(
        attempt_id=planner.attempt_id,
        verdict=Phase16V5ValidationVerdict.PASS,
        reason_code="PLANNER_VALIDATION_PASS",
        validation_digest="e" * 64,
    )
    wrong_key_ledger = PostgresPhase16V5CampaignLedger(
        postgres_v5_ledger_factory.settings,
        hmac_key=bytes.fromhex("6e" * 32),
    )
    with pytest.raises(Phase16V5CampaignLedgerError, match="HMAC"):
        wrong_key_ledger.close_case(
            claim_id=authenticated_claim.claim_id,
            status=Phase16V5CaseOutcomeStatus.PASS,
            reason_code="MULTI_AGENT_READY",
        )

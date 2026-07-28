"""Phase 16 V2 正式 smoke 账本的 PostgreSQL 集成契约。

测试始终在临时 schema 中运行，只验证 V2 独立账本的 CAS、触发器、HMAC 和恢复事实；
它不加载 LLM 配置、不构造 DeepSeek Adapter，也不会产生任何真实模型请求。
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
import pytest
from psycopg import sql

from src.config.settings import get_settings
from src.decision_support.official_smoke_evidence_v2 import (
    Phase16OfficialPriceEvidence,
    Phase16OfficialSmokeV2Environment,
    load_phase16_official_smoke_v2_evidence_manifest,
    load_phase16_official_smoke_v2_parent_dataset,
    preflight_phase16_official_smoke_v2_evidence,
)
from src.decision_support.official_smoke_ledger_v2 import (
    PHASE16_OFFICIAL_SMOKE_V2_CASE_RESERVATION_CNY,
    PHASE16_OFFICIAL_SMOKE_V2_HISTORICAL_SPEND_CNY,
    PHASE16_OFFICIAL_SMOKE_V2_MAX_EXPOSURE_CNY,
    Phase16OfficialSmokeV2CaseOutcomeStatus,
    Phase16OfficialSmokeV2DispatchStage,
    Phase16OfficialSmokeV2LedgerError,
    Phase16OfficialSmokeV2ReceiptAuthenticator,
    Phase16OfficialSmokeV2ValidationVerdict,
    PostgresPhase16OfficialSmokeV2Ledger,
    initialize_phase16_official_smoke_v2_ledger_schema,
)
from src.decision_support.official_smoke_runner_v2 import (
    Phase16OfficialSmokeV2ExecutionStatus,
    Phase16OfficialSmokeV2Runner,
)
from src.specialist_runtime.model_port import ModelSuccess, ModelUsage
from src.specialist_runtime.models import canonical_json_sha256


PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 固定测试 key 只用于验证 HMAC 绑定，不是本机正式 smoke 所需的 receipt key。
_TEST_RECEIPT_SIGNING_KEY = bytes.fromhex("5a" * 32)


@pytest.fixture()
def postgres_v2_ledger_factory():
    """为每个案例建立独立 schema，证明账本事实来自 PostgreSQL 而非进程内缓存。"""

    base_kwargs = dict(get_settings().postgres_connection_kwargs)
    schema_name = f"phase16_v2_smoke_{uuid4().hex}"
    with psycopg.connect(**base_kwargs) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}; ").format(sql.Identifier(schema_name)))
        connection.commit()
    settings = SimpleNamespace(
        postgres_connection_kwargs={
            **base_kwargs,
            "options": f"-c search_path={schema_name}",
        }
    )
    initialize_phase16_official_smoke_v2_ledger_schema(settings)

    def build_ledger() -> PostgresPhase16OfficialSmokeV2Ledger:
        """每次返回新对象，供重启恢复测试验证数据库是唯一权威事实源。"""

        return PostgresPhase16OfficialSmokeV2Ledger(
            settings,
            receipt_authenticator=Phase16OfficialSmokeV2ReceiptAuthenticator(
                _TEST_RECEIPT_SIGNING_KEY
            ),
        )

    build_ledger.settings = settings
    try:
        yield build_ledger
    finally:
        with psycopg.connect(**base_kwargs) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE;").format(
                    sql.Identifier(schema_name)
                )
            )
            connection.commit()


def _manifest():
    """只读取已冻结的 V2 Manifest，测试不得临时造 case 或更换 Profile。"""

    return load_phase16_official_smoke_v2_evidence_manifest(
        repository_root=PROJECT_ROOT
    )


def _request_id(label: str) -> str:
    """生成规范 UUID，禁止把自由文本直接写入正式内部 request ID 列。"""

    return str(uuid5(NAMESPACE_URL, f"phase16-v2-ledger-test:{label}"))


class _DeterministicV2Port:
    """按共享 Runner 已解析的证据生成合法 V2 JSON，完全不访问网络或模型供应商。"""

    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request):
        """模拟具有 Provider receipt/usage 的成功响应，保留 V2 system-managed ID 边界。"""

        self.requests.append(request)
        context = json.loads(request.messages[-1].content)
        evidence_ids = [item["evidence_id"] for item in context["resolved_evidence"]]
        if "trigger_codes" in context["input_snapshot"]:
            # V2 Analyst 只能选择可见 ID；finding 与完整 EvidenceRef 由系统回填，模型
            # 不拥有摘要、source version 或权威 trigger code 的写权限。
            final_output = {
                "constraint_codes": [],
                "risk_codes": ["HUMAN_CONFIRMATION_REQUIRED"],
                "explanation": "受控证据显示售罄冲突需要人工确认。",
                "evidence_ids": evidence_ids,
            }
        else:
            # Planner 同样只输出受控 ID 和 bounded option，不创建 Proposal 或经营命令。
            final_output = {
                "options": [
                    {
                        "option_id": "hold-for-operator",
                        "product_strategy": "HOLD_AND_ESCALATE",
                        "backup_product_id": None,
                        "host_prompt": "请运营确认售罄和备品后再继续。",
                        "timing": "AFTER_OPERATOR_CONFIRMATION",
                        "risk_flags": ["HUMAN_CONFIRMATION_REQUIRED"],
                        "evidence_ids": evidence_ids,
                    }
                ]
            }
        envelope = {
            "kind": "FINAL",
            "final_output": final_output,
            "reason_summary": "V2_INTEGRATION_TEST",
        }
        return ModelSuccess(
            request_id=request.request_id,
            model_id="deepseek-v4-pro",
            output=envelope,
            usage=ModelUsage(input_tokens=100, output_tokens=100, total_tokens=200),
            provider_response_id=f"phase16-v2-test-provider-{len(self.requests):03d}",
            finish_reason="stop",
            response_digest=canonical_json_sha256(envelope),
            latency_ms=Decimal("2.000"),
        )


def _append_analyst_pass(ledger, claim, manifest):
    """写入一条完整 Analyst PASS 事实，供 Planner 顺序和成功闭合测试复用。"""

    attempt = ledger.begin_dispatch(
        claim_id=claim.claim_id,
        stage=Phase16OfficialSmokeV2DispatchStage.ANALYST,
        profile_digest=manifest.profile_digests["analyst"],
        internal_request_id=_request_id(f"analyst:{claim.case_id}"),
    )
    ledger.append_provider_receipt(
        attempt_id=attempt.attempt_id,
        provider_response_id=f"provider-analyst-{claim.case_id}",
        finish_reason="stop",
        model_id="deepseek-v4-pro",
        response_digest="a" * 64,
        input_tokens=100,
        output_tokens=100,
        total_tokens=200,
        latency_ms=Decimal("2.000"),
    )
    ledger.append_validation_fact(
        attempt_id=attempt.attempt_id,
        verdict=Phase16OfficialSmokeV2ValidationVerdict.PASS,
        reason_code="ANALYST_VALIDATION_PASS",
        validation_digest="b" * 64,
    )
    return attempt


def test_v2_ledger_imports_two_historical_facts_and_freezes_total_exposure(
    postgres_v2_ledger_factory,
) -> None:
    """V2 必须同时计入直接模式与 V1 已失败调用，十个 slot 后风险上界不得超过一元。"""

    ledger = postgres_v2_ledger_factory()
    manifest = _manifest()

    snapshot = ledger.ensure_run(manifest)

    assert snapshot.historical_spend_cny == PHASE16_OFFICIAL_SMOKE_V2_HISTORICAL_SPEND_CNY
    assert snapshot.fixed_case_slot_count == 10
    assert snapshot.case_reservation_cny == PHASE16_OFFICIAL_SMOKE_V2_CASE_RESERVATION_CNY
    assert snapshot.maximum_exposure_cny == PHASE16_OFFICIAL_SMOKE_V2_MAX_EXPOSURE_CNY
    assert snapshot.maximum_exposure_cny <= Decimal("1.000000")
    # 重新构造 Ledger 模拟进程重启；重复 ensure 不得重复导入或扩展 slot。
    assert postgres_v2_ledger_factory().ensure_run(manifest) == snapshot


def test_v2_ledger_requires_analyst_pass_before_planner_and_records_stop_receipts(
    postgres_v2_ledger_factory,
) -> None:
    """Planner 不能绕过 Analyst，且完整两段 stop 回执才能关闭唯一 PASS outcome。"""

    ledger = postgres_v2_ledger_factory()
    manifest = _manifest()
    ledger.ensure_run(manifest)
    claim = ledger.claim_case(manifest.case_ids[0])

    with pytest.raises(Phase16OfficialSmokeV2LedgerError, match="analyst validation"):
        ledger.begin_dispatch(
            claim_id=claim.claim_id,
            stage=Phase16OfficialSmokeV2DispatchStage.PLANNER,
            profile_digest=manifest.profile_digests["planner"],
            internal_request_id=_request_id("planner-before-analyst"),
        )

    _append_analyst_pass(ledger, claim, manifest)
    planner = ledger.begin_dispatch(
        claim_id=claim.claim_id,
        stage=Phase16OfficialSmokeV2DispatchStage.PLANNER,
        profile_digest=manifest.profile_digests["planner"],
        internal_request_id=_request_id("planner-after-analyst"),
    )
    with pytest.raises(Phase16OfficialSmokeV2LedgerError, match="provider receipt"):
        ledger.append_validation_fact(
            attempt_id=planner.attempt_id,
            verdict=Phase16OfficialSmokeV2ValidationVerdict.PASS,
            reason_code="PLANNER_VALIDATION_PASS",
            validation_digest="c" * 64,
        )
    ledger.append_provider_receipt(
        attempt_id=planner.attempt_id,
        provider_response_id="provider-planner-unit",
        finish_reason="stop",
        model_id="deepseek-v4-pro",
        response_digest="d" * 64,
        input_tokens=100,
        output_tokens=100,
        total_tokens=200,
        latency_ms=Decimal("3.000"),
    )
    ledger.append_validation_fact(
        attempt_id=planner.attempt_id,
        verdict=Phase16OfficialSmokeV2ValidationVerdict.PASS,
        reason_code="PLANNER_VALIDATION_PASS",
        validation_digest="e" * 64,
    )
    outcome = ledger.close_case(
        claim_id=claim.claim_id,
        status=Phase16OfficialSmokeV2CaseOutcomeStatus.PASS,
        reason_code="FORMAL_CASE_PASS",
    )

    assert outcome.status is Phase16OfficialSmokeV2CaseOutcomeStatus.PASS
    assert ledger.verify_case_outcome_receipts(case_id=claim.case_id) == outcome


def test_v2_ledger_rejects_non_stop_receipt_and_freezes_run_after_sent_failure(
    postgres_v2_ledger_factory,
) -> None:
    """被截断的模型输出不能进入账本，已发送失败会终止 run 并拒绝后续 slot。"""

    ledger = postgres_v2_ledger_factory()
    manifest = _manifest()
    ledger.ensure_run(manifest)
    failed_claim = ledger.claim_case(manifest.case_ids[0])
    attempt = ledger.begin_dispatch(
        claim_id=failed_claim.claim_id,
        stage=Phase16OfficialSmokeV2DispatchStage.ANALYST,
        profile_digest=manifest.profile_digests["analyst"],
        internal_request_id=_request_id("non-stop"),
    )
    with pytest.raises(Phase16OfficialSmokeV2LedgerError, match="provider receipt"):
        ledger.append_provider_receipt(
            attempt_id=attempt.attempt_id,
            provider_response_id="provider-length-unit",
            finish_reason="length",
            model_id="deepseek-v4-pro",
            response_digest="f" * 64,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_ms=Decimal("1.000"),
        )
    ledger.append_validation_fact(
        attempt_id=attempt.attempt_id,
        verdict=Phase16OfficialSmokeV2ValidationVerdict.FAILED,
        reason_code="PROVIDER_RECEIPT_INVALID",
        validation_digest="0" * 64,
    )
    ledger.close_case(
        claim_id=failed_claim.claim_id,
        status=Phase16OfficialSmokeV2CaseOutcomeStatus.FAILED,
        reason_code="PROVIDER_RECEIPT_INVALID",
    )

    with pytest.raises(Phase16OfficialSmokeV2LedgerError, match="terminal"):
        ledger.claim_case(manifest.case_ids[1])


def test_v2_ledger_recovers_open_sent_attempt_as_unknown_without_resend(
    postgres_v2_ledger_factory,
) -> None:
    """进程在发送意图后崩溃时只能记录 UNKNOWN 失败，不能再次发同一外部请求。"""

    first = postgres_v2_ledger_factory()
    manifest = _manifest()
    first.ensure_run(manifest)
    claim = first.claim_case(manifest.case_ids[1])
    first.begin_dispatch(
        claim_id=claim.claim_id,
        stage=Phase16OfficialSmokeV2DispatchStage.ANALYST,
        profile_digest=manifest.profile_digests["analyst"],
        internal_request_id=_request_id("crash-after-intent"),
    )

    recovered = postgres_v2_ledger_factory().recover_open_attempts()

    assert len(recovered) == 1
    assert recovered[0].status is Phase16OfficialSmokeV2CaseOutcomeStatus.FAILED
    assert recovered[0].reason_code == "UNKNOWN_ATTEMPT_AFTER_RESTART"
    with pytest.raises(Phase16OfficialSmokeV2LedgerError, match="terminal"):
        postgres_v2_ledger_factory().claim_case(manifest.case_ids[2])


def test_v2_runner_completes_ten_controlled_cases_through_postgresql_ledger(
    postgres_v2_ledger_factory,
) -> None:
    """十例离线回放必须产生二十次共享 Runner 调用与十条 PostgreSQL PASS 事实。"""

    dataset = load_phase16_official_smoke_v2_parent_dataset(
        PROJECT_ROOT / "evaluation" / "phase16_controlled_multi_agent"
    )
    price = Phase16OfficialPriceEvidence.create(
        model_id="deepseek-v4-pro",
        endpoint_host="api.deepseek.com",
        input_cny_per_million=Decimal("3.000000"),
        output_cny_per_million=Decimal("6.000000"),
    )
    manifest = load_phase16_official_smoke_v2_evidence_manifest(
        repository_root=PROJECT_ROOT
    )
    preflight = preflight_phase16_official_smoke_v2_evidence(
        dataset=dataset,
        official_price=price,
        environment=Phase16OfficialSmokeV2Environment(
            model_id="deepseek-v4-pro",
            endpoint_host="api.deepseek.com",
            credential_configured=True,
        ),
    )
    port = _DeterministicV2Port()
    runner = Phase16OfficialSmokeV2Runner(
        dataset=dataset,
        manifest=manifest,
        preflight=preflight,
        official_price=price,
        ledger=postgres_v2_ledger_factory(),
        model_port=port,
        # 固定数据集的六角色证据以 2026-07-18 为参考时间；传入相同时间验证 V2
        # smoke 使用“已冻结快照”的语义，不让现实墙钟把离线证据误判为 LIVE 陈旧。
        clock=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
    )

    report = asyncio.run(runner.execute())

    assert report.status is Phase16OfficialSmokeV2ExecutionStatus.PASS
    assert len(report.case_executions) == 10
    assert report.model_calls == len(port.requests) == 20
    assert all(item.status is Phase16OfficialSmokeV2ExecutionStatus.PASS for item in report.case_executions)

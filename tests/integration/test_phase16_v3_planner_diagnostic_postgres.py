"""Phase 16 V3 Planner 诊断账本的 PostgreSQL 真实契约。

测试在临时 schema 中运行。它验证 append-only 诊断事实与终态防重发，不创建 DeepSeek
适配器、不读取 .env，也不会产生网络模型调用。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from src.config.settings import get_settings
from src.decision_support.official_smoke_ledger_v3 import (
    PHASE16_V3_PLANNER_DIAGNOSTIC_CASE_ID,
    Phase16V3DiagnosticOutcomeStatus,
    Phase16V3DiagnosticValidationVerdict,
    Phase16V3FailureAuthenticator,
    Phase16V3ReceiptAuthenticator,
    PostgresPhase16V3PlannerDiagnosticLedger,
    initialize_phase16_v3_planner_diagnostic_ledger_schema,
)
from src.decision_support.official_smoke_evidence_v2 import (
    Phase16OfficialPriceEvidence,
    load_phase16_official_smoke_v2_evidence_manifest,
    load_phase16_official_smoke_v2_parent_dataset,
)
from src.decision_support.official_smoke_runner_v3 import Phase16V3PlannerDiagnosticRunner
from src.decision_support.planner_diagnostic_v3 import model_failure_fact_from_outcome
from src.specialist_runtime.model_port import (
    ModelFailure,
    ModelFailureCategory,
    ModelSuccess,
    ModelUsage,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def postgres_v3_ledger():
    """每个测试使用独立 schema，确保 V3 账本行为来自 PostgreSQL 而非进程缓存。"""

    base_kwargs = dict(get_settings().postgres_connection_kwargs)
    schema_name = f"phase16_v3_diagnostic_{uuid4().hex}"
    with psycopg.connect(**base_kwargs) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        connection.commit()
    settings = SimpleNamespace(
        postgres_connection_kwargs={
            **base_kwargs,
            "options": f"-c search_path={schema_name}",
        }
    )
    initialize_phase16_v3_planner_diagnostic_ledger_schema(settings)
    try:
        # 固定测试 key 只用于验证失败事实 HMAC 绑定，不能读取或复用开发机正式签名密钥。
        yield PostgresPhase16V3PlannerDiagnosticLedger(
            settings,
            failure_authenticator=Phase16V3FailureAuthenticator(bytes.fromhex("6b" * 32)),
            receipt_authenticator=Phase16V3ReceiptAuthenticator(bytes.fromhex("6b" * 32)),
        )
    finally:
        with psycopg.connect(**base_kwargs) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(schema_name)
                )
            )
            connection.commit()


def test_v3_sent_model_failure_is_immutable_and_prevents_a_second_dispatch(postgres_v3_ledger) -> None:
    """已发送失败必须保留具体类别并关闭唯一 case，任何第二次发送都必须被数据库拒绝。"""

    ledger = postgres_v3_ledger
    ledger.ensure_run(
        manifest_digest="a" * 64,
        planner_profile_digest="b" * 64,
        case_digest="c" * 64,
    )
    attempt = ledger.begin_dispatch(
        case_id=PHASE16_V3_PLANNER_DIAGNOSTIC_CASE_ID,
        planner_profile_digest="b" * 64,
        internal_request_id="db101b1e-8f09-4916-8230-4936a2b2ce84",
    )
    fact = model_failure_fact_from_outcome(
        attempt_id=attempt.attempt_id,
        outcome=ModelFailure(
            request_id="db101b1e-8f09-4916-8230-4936a2b2ce84",
            category=ModelFailureCategory.DEADLINE_EXCEEDED,
            request_sent=True,
            response_digest=None,
            http_status=None,
            retry_after_seconds=None,
            # 故意使用 adapter 可能产生的高精度延迟，证明 PostgreSQL 量化后仍能复验 HMAC。
            latency_ms=Decimal("60000.0004"),
        ),
    )

    persisted = ledger.append_model_failure(fact)
    ledger.append_validation_fact(
        attempt_id=attempt.attempt_id,
        verdict=Phase16V3DiagnosticValidationVerdict.FAILED,
        reason_code="MODEL_FAILURE_DEADLINE_EXCEEDED",
    )
    outcome = ledger.close_case(
        case_id=PHASE16_V3_PLANNER_DIAGNOSTIC_CASE_ID,
        status=Phase16V3DiagnosticOutcomeStatus.FAILED,
        reason_code="MODEL_FAILURE_DEADLINE_EXCEEDED",
    )

    assert persisted.category is ModelFailureCategory.DEADLINE_EXCEEDED
    assert persisted.request_sent is True
    assert persisted.latency_ms == Decimal("60000.000")
    # 读取到的事实必须仍通过账本外 HMAC，不能只相信 PostgreSQL 中可修改的字段形状。
    assert ledger.verify_model_failure(persisted)
    assert outcome.status is Phase16V3DiagnosticOutcomeStatus.FAILED
    with pytest.raises(RuntimeError, match="terminal"):
        ledger.begin_dispatch(
            case_id=PHASE16_V3_PLANNER_DIAGNOSTIC_CASE_ID,
            planner_profile_digest="b" * 64,
            internal_request_id="1dbb81a0-a0c3-43df-af17-c6711a92c7f8",
        )


class _DeadlineFailurePort:
    """模拟已发送后超时的真实端口语义，不访问网络且不伪造 Provider 成功回执。"""

    async def complete(self, request):
        """返回与 DeepSeek Adapter 相同的结构化失败，供 V3 捕获/账本路径验证。"""

        return ModelFailure(
            request_id=request.request_id,
            category=ModelFailureCategory.DEADLINE_EXCEEDED,
            request_sent=True,
            response_digest=None,
            http_status=None,
            retry_after_seconds=None,
            latency_ms=Decimal("60000.000"),
        )


def test_v3_runner_persists_exact_model_failure_from_shared_planner_path(postgres_v3_ledger) -> None:
    """共享 Runner 返回端口失败时，V3 必须保留精确分类而不能退化成通用 unavailable。"""

    dataset = load_phase16_official_smoke_v2_parent_dataset(
        PROJECT_ROOT / "evaluation" / "phase16_controlled_multi_agent"
    )
    manifest = load_phase16_official_smoke_v2_evidence_manifest(repository_root=PROJECT_ROOT)
    runner = Phase16V3PlannerDiagnosticRunner(
        dataset=dataset,
        parent_manifest=manifest,
        official_price=Phase16OfficialPriceEvidence.create(
            model_id="deepseek-v4-pro",
            endpoint_host="api.deepseek.com",
            input_cny_per_million=Decimal("3.000000"),
            output_cny_per_million=Decimal("6.000000"),
        ),
        ledger=postgres_v3_ledger,
        model_port=_DeadlineFailurePort(),
        clock=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
    )

    report = asyncio.run(runner.execute())

    assert report.status is Phase16V3DiagnosticOutcomeStatus.FAILED
    assert report.reason_code == "MODEL_FAILURE_DEADLINE_EXCEEDED"


def test_v3_signed_provider_receipt_is_required_for_pass_and_detects_tampering(
    postgres_v3_ledger,
) -> None:
    """成功结论必须依赖受签名回执，修改 usage 即使保留 SQL 合法形状也会失去认证。"""

    ledger = postgres_v3_ledger
    request_id = "a9114e99-807d-4f72-8e2d-68a4ceeec010"
    ledger.ensure_run(
        manifest_digest="a" * 64,
        planner_profile_digest="b" * 64,
        case_digest="c" * 64,
    )
    attempt = ledger.begin_dispatch(
        case_id=PHASE16_V3_PLANNER_DIAGNOSTIC_CASE_ID,
        planner_profile_digest="b" * 64,
        internal_request_id=request_id,
    )
    receipt = ledger.append_provider_receipt(
        attempt_id=attempt.attempt_id,
        success=ModelSuccess(
            request_id=request_id,
            model_id="deepseek-v4-pro",
            output={"kind": "FINAL"},
            usage=ModelUsage(input_tokens=10, output_tokens=20, total_tokens=30),
            provider_response_id="chatcmpl-v3-receipt-001",
            finish_reason="stop",
            response_digest="d" * 64,
            # 精确半毫秒验证 HMAC 使用的 round-half-up 必须和 PostgreSQL NUMERIC(16,3) 一致。
            latency_ms=Decimal("100.0005"),
        ),
    )

    assert ledger.verify_provider_receipt(receipt)
    assert receipt.latency_ms == Decimal("100.001")
    assert not ledger.verify_provider_receipt(replace(receipt, output_tokens=21))
    ledger.append_validation_fact(
        attempt_id=attempt.attempt_id,
        verdict=Phase16V3DiagnosticValidationVerdict.PASS,
        reason_code="PLANNER_DIAGNOSTIC_PASS",
    )
    outcome = ledger.close_case(
        case_id=PHASE16_V3_PLANNER_DIAGNOSTIC_CASE_ID,
        status=Phase16V3DiagnosticOutcomeStatus.PASS,
        reason_code="PLANNER_DIAGNOSTIC_PASS",
    )

    assert outcome.status is Phase16V3DiagnosticOutcomeStatus.PASS

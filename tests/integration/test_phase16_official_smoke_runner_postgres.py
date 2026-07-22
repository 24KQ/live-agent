"""Phase 16 正式 Smoke Runner 与 PostgreSQL append-only 账本的离线集成契约。"""

from __future__ import annotations

import asyncio
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from src.config.settings import get_settings
from src.decision_support.multi_agent_evaluation import (
    load_phase16_controlled_multi_agent_dataset,
)
from src.decision_support.official_smoke_evidence import (
    Phase16OfficialPriceEvidence,
    Phase16OfficialSmokeEnvironment,
    load_phase16_official_smoke_evidence_manifest,
    preflight_phase16_official_smoke_evidence,
)
from src.decision_support.official_smoke_ledger import (
    PHASE16_OFFICIAL_SMOKE_TOTAL_BUDGET_CNY,
    Phase16OfficialSmokeCaseOutcomeStatus,
    Phase16OfficialSmokeDispatchStage,
    Phase16OfficialSmokeReceiptAuthenticator,
    PostgresPhase16OfficialSmokeLedger,
    initialize_phase16_official_smoke_ledger_schema,
)
from src.decision_support.official_smoke_runner import (
    Phase16OfficialSmokeEvidenceConclusion,
    Phase16OfficialSmokeExecutionStatus,
    Phase16OfficialSmokeRunner,
)
from src.specialist_runtime.model_port import ModelSuccess, ModelUsage
from src.specialist_runtime.models import canonical_json_sha256


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TEST_RECEIPT_SIGNING_KEY = bytes.fromhex("5a" * 32)


@pytest.fixture()
def postgres_formal_runner_ledger():
    """为整轮 10 case 离线 Runner 演练创建独立 schema，结束后无条件清理所有事实。"""

    base_kwargs = dict(get_settings().postgres_connection_kwargs)
    schema_name = f"phase16_runner_{uuid4().hex}"
    with psycopg.connect(**base_kwargs) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}; ").format(sql.Identifier(schema_name)))
        connection.commit()
    settings = SimpleNamespace(
        postgres_connection_kwargs={**base_kwargs, "options": f"-c search_path={schema_name}"}
    )
    initialize_phase16_official_smoke_ledger_schema(settings)
    try:
        yield PostgresPhase16OfficialSmokeLedger(
            settings,
            receipt_authenticator=Phase16OfficialSmokeReceiptAuthenticator(
                _TEST_RECEIPT_SIGNING_KEY
            ),
        )
    finally:
        with psycopg.connect(**base_kwargs) as connection:
            connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE;").format(sql.Identifier(schema_name))
            )
            connection.commit()


class _ValidFormalSmokePort:
    """根据共享 Runner 已解析的六角色证据生成合法结构化回执，不接触真实网络。"""

    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request):
        """为 Analyst/Planner 各返回一次完整 usage、provider ID 与 finish reason 回执。"""

        self.requests.append(request)
        context = json.loads(request.messages[-1].content)
        references = [
            {
                "kind": item["kind"],
                "evidence_id": item["evidence_id"],
                "source_version": item["source_version"],
                "digest": item["digest"],
                "anchor_id": item["anchor_id"],
                "room_id": item["room_id"],
            }
            for item in context["resolved_evidence"]
        ]
        if "trigger_codes" in context["input_snapshot"]:
            final_output = {
                "finding_codes": context["input_snapshot"]["trigger_codes"],
                "constraint_codes": [],
                "risk_codes": ["HUMAN_CONFIRMATION_REQUIRED"],
                "explanation": "冻结证据要求运营确认售罄冲突。",
                "evidence_refs": references,
            }
        else:
            final_output = {
                "options": [
                    {
                        "option_id": "hold-for-formal-operator",
                        "product_strategy": "HOLD_AND_ESCALATE",
                        "backup_product_id": None,
                        "host_prompt": "等待运营确认后继续。",
                        "timing": "AFTER_OPERATOR_CONFIRMATION",
                        "risk_flags": ["HUMAN_CONFIRMATION_REQUIRED"],
                        "evidence_refs": references,
                    }
                ]
            }
        envelope = {
            "kind": "FINAL",
            "final_output": final_output,
            "evidence_refs": references,
            "reason_summary": "FORMAL_POSTGRES_INTEGRATION",
        }
        return ModelSuccess(
            request_id=request.request_id,
            model_id=request.model_id,
            output=envelope,
            usage=ModelUsage(input_tokens=40, output_tokens=60, total_tokens=100),
            provider_response_id=f"phase16-postgres-provider-{len(self.requests):03d}",
            finish_reason="stop",
            response_digest=canonical_json_sha256(envelope),
            latency_ms=Decimal("2.000"),
        )


def _official_price() -> Phase16OfficialPriceEvidence:
    """返回用户批准的 DeepSeek V4 Flash cache-miss 价格，不读取本机 API 配置。"""

    return Phase16OfficialPriceEvidence.create(
        model_id="deepseek-v4-flash",
        endpoint_host="api.deepseek.com",
        input_cny_per_million=Decimal("1.000000"),
        output_cny_per_million=Decimal("2.000000"),
    )


def test_postgres_formal_runner_writes_ten_authenticated_two_stage_pass_chains(
    postgres_formal_runner_ledger,
) -> None:
    """10 个固定 slot 必须形成 20 条带 HMAC 回执的两段 PASS 链，且预算上限不漂移。"""

    dataset = load_phase16_controlled_multi_agent_dataset(
        _PROJECT_ROOT / "evaluation" / "phase16_controlled_multi_agent"
    )
    price = _official_price()
    manifest = load_phase16_official_smoke_evidence_manifest(repository_root=_PROJECT_ROOT)
    preflight = preflight_phase16_official_smoke_evidence(
        dataset=dataset,
        official_price=price,
        environment=Phase16OfficialSmokeEnvironment(
            model_id="deepseek-v4-flash",
            endpoint_host="api.deepseek.com",
            credential_configured=True,
        ),
    )
    port = _ValidFormalSmokePort()
    report = asyncio.run(
        Phase16OfficialSmokeRunner(
            dataset=dataset,
            manifest=manifest,
            preflight=preflight,
            official_price=price,
            ledger=postgres_formal_runner_ledger,
            model_port=port,
        ).execute()
    )

    assert report.reason_codes == (), report
    assert report.status is Phase16OfficialSmokeExecutionStatus.PASS
    assert report.evidence_conclusion is Phase16OfficialSmokeEvidenceConclusion.PASS
    assert report.model_calls == 20
    assert len(port.requests) == 20
    assert postgres_formal_runner_ledger.snapshot().maximum_exposure_cny <= (
        PHASE16_OFFICIAL_SMOKE_TOTAL_BUDGET_CNY
    )
    for case_id in manifest.case_ids:
        assert postgres_formal_runner_ledger.verify_case_outcome_receipts(
            case_id=case_id
        ).status is Phase16OfficialSmokeCaseOutcomeStatus.PASS


def test_postgres_formal_runner_recovers_unknown_attempt_before_any_new_dispatch(
    postgres_formal_runner_ledger,
) -> None:
    """重启后必须先把未闭合 intent 追加 UNKNOWN_FAILED，且绝不能对同一 case 重发模型。"""

    dataset = load_phase16_controlled_multi_agent_dataset(
        _PROJECT_ROOT / "evaluation" / "phase16_controlled_multi_agent"
    )
    price = _official_price()
    manifest = load_phase16_official_smoke_evidence_manifest(repository_root=_PROJECT_ROOT)
    preflight = preflight_phase16_official_smoke_evidence(
        dataset=dataset,
        official_price=price,
        environment=Phase16OfficialSmokeEnvironment(
            model_id="deepseek-v4-flash",
            endpoint_host="api.deepseek.com",
            credential_configured=True,
        ),
    )
    postgres_formal_runner_ledger.ensure_run(manifest)
    claim = postgres_formal_runner_ledger.claim_case(manifest.case_ids[0])
    postgres_formal_runner_ledger.begin_dispatch(
        claim_id=claim.claim_id,
        stage=Phase16OfficialSmokeDispatchStage.ANALYST,
        profile_digest=manifest.profile_digests["analyst"],
        internal_request_id=str(uuid4()),
    )
    port = _ValidFormalSmokePort()

    report = asyncio.run(
        Phase16OfficialSmokeRunner(
            dataset=dataset,
            manifest=manifest,
            preflight=preflight,
            official_price=price,
            ledger=postgres_formal_runner_ledger,
            model_port=port,
        ).execute()
    )

    assert report.status is Phase16OfficialSmokeExecutionStatus.FAILED
    assert report.evidence_conclusion is Phase16OfficialSmokeEvidenceConclusion.FAILED
    assert report.reason_codes == ("UNKNOWN_ATTEMPT_AFTER_RESTART",)
    assert report.model_calls == 0
    assert port.requests == []
    assert postgres_formal_runner_ledger.get_case_outcome(
        case_id=manifest.case_ids[0]
    ).status is Phase16OfficialSmokeCaseOutcomeStatus.FAILED

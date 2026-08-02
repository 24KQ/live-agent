"""Phase 17 holdout 执行契约的 PostgreSQL 集成契约（frozen fixture 模式）。

使用独立 schema 隔离的 ledger fixture，只验证契约身份路由与预算池隔离的
fail-closed 语义，不发送真实模型请求。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest
from pydantic import ValidationError

from src.config.settings import get_settings
from src.decision_support.phase16_qualification import (
    PHASE17_HOLDOUT_EXECUTION_CONTRACT_PATH,
    PHASE17_HOLDOUT_EXECUTION_CONTRACT_ID,
    PHASE17_HOLDOUT_HIGH_CONFLICT_CASE_COUNT,
    QualificationExecutionContract,
    admit_phase17_holdout_execution,
    load_phase17_holdout_execution_contract,
)
from src.decision_support.phase16_qualification_ledger import (
    Phase16QualificationLedgerError,
    PostgresPhase16QualificationLedger,
    QualificationCampaign,
    QualificationCampaignKind,
    initialize_phase16_qualification_schema,
    qualification_campaign_id,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TEST_HMAC_KEY = bytes.fromhex("d3" * 32)


@pytest.fixture()
def ledger_factory():
    """独立 schema 的 phase17 集成 fixture；结束自动清理。"""
    base_kwargs = dict(get_settings().postgres_connection_kwargs)
    schema_name = f"phase17_holdout_execution_{uuid4().hex}"
    with psycopg.connect(**base_kwargs) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        connection.commit()
    settings = SimpleNamespace(
        postgres_connection_kwargs={**base_kwargs, "options": f"-c search_path={schema_name}"}
    )
    initialize_phase16_qualification_schema(settings)

    def build() -> PostgresPhase16QualificationLedger:
        return PostgresPhase16QualificationLedger(settings, hmac_key=_TEST_HMAC_KEY)

    build.settings = settings
    try:
        yield build
    finally:
        with psycopg.connect(**base_kwargs) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name)))
            connection.commit()


def test_phase17_contract_loads_and_admits_in_integration(ledger_factory) -> None:
    """DB 集成上下文中契约可加载；phase17 身份准入通过，v2 历史身份被拒。"""
    ledger = ledger_factory()  # noqa: F841 仅验证 DB 上下文可用
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    assert contract.contract_id == PHASE17_HOLDOUT_EXECUTION_CONTRACT_ID
    allowed, reasons = admit_phase17_holdout_execution(
        requested_identity=QualificationExecutionContract.PHASE17_HOLDOUT_EXECUTION_V1,
        contract=contract,
    )
    assert allowed and not reasons
    allowed, reasons = admit_phase17_holdout_execution(
        requested_identity=QualificationExecutionContract.V2_HISTORICAL_EXECUTION,
        contract=contract,
    )
    assert not allowed and "EXECUTION_IDENTITY_NOT_PHASE17" in reasons


def test_phase17_contract_cannot_attach_to_legacy_budget_pool(ledger_factory) -> None:
    """phase17 contract digest 不是 v2 policy 行 → HOLDOUT campaign 创建 fail-closed。

    证明预算池/namespace 隔离：phase17 不能借用 v2/v3 的预算池，v2/v3 也不能
    被 phase17 契约身份复用。contract digest 永远只作为 phase17 自己的 policy 身份。
    """
    ledger = ledger_factory()
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    campaign = QualificationCampaign(
        campaign_id=qualification_campaign_id(
            kind=QualificationCampaignKind.HOLDOUT,
            candidate_digest="a" * 64,
            declared_model_id="gpt-5.6-luna",
            declared_reasoning_effort=None,
            declared_endpoint_hosts=("synapse-ai.uk",),
            batch_index=1,
        ),
        campaign_kind=QualificationCampaignKind.HOLDOUT,
        policy_digest=contract.contract_digest or "",
        corpus_digest="b" * 64,
        candidate_digest="a" * 64,
        manifest_digest="c" * 64,
        reservation_cny="1.000000",
        batch_index=1,
    )
    with pytest.raises(Phase16QualificationLedgerError, match="policy is unavailable"):
        ledger.ensure_campaign(campaign)


def test_phase17_runtime_rejects_v3_retrospective_contract(
    ledger_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v3 纯回溯契约无执行身份：作为执行契约加载必须被运行时拒绝。"""
    ledger_factory()  # noqa: F841 仅验证 DB 上下文可用
    monkeypatch.setattr(
        "src.decision_support.phase16_qualification.PHASE17_HOLDOUT_EXECUTION_CONTRACT_PATH",
        Path("evaluation/manifests/phase16-qualification-policy-v3.json"),
    )
    with pytest.raises((ValidationError, ValueError)):
        load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)


def test_phase17_contract_freeze_matches_manifest_on_disk(ledger_factory) -> None:
    """契约 manifest 的 30 例 / 10+20 结构必须与加载模型一致（frozen fixture 可查）。"""
    ledger_factory()  # noqa: F841 仅验证 DB 上下文可用
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    assert contract.holdout_case_count == PHASE17_HOLDOUT_HIGH_CONFLICT_CASE_COUNT == 30
    assert [b["case_count"] for b in contract.holdout_batches] == [10, 20]

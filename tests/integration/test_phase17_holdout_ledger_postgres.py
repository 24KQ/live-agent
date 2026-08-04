"""Phase 17 holdout 独立账本（事件记账 + 预算池）PostgreSQL 集成测试。

覆盖 codex 十六轮 P0 的 ledger 正向路径：contract 注册、campaign 预留、
预算池耗尽拒绝、结算/释放、run 生命周期、append-only、与 v2/v3 表族的
反向隔离。全部使用独立 schema，不触碰真实账本，不发送模型请求。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
import pytest

from src.config.settings import get_settings
from src.decision_support.phase16_qualification import (
    load_phase17_holdout_execution_contract,
)
from src.decision_support.phase16_qualification_ledger import (
    QualificationCampaignKind,
    qualification_campaign_id,
)
from src.decision_support.phase17_holdout_ledger import (
    Phase17HoldoutCampaign,
    Phase17HoldoutLedgerError,
    PostgresPhase17HoldoutLedger,
    initialize_phase17_holdout_schema,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_TEST_HMAC_KEY = bytes.fromhex("d3" * 32)
_V2_HISTORICAL_POLICY_DIGEST = "1aa9ca6fe5a85702a256e29fb5d6f3d22334bfeb55c6b0ab02300ba62e4926d7"


@pytest.fixture()
def ledger_factory():
    """独立 schema 的 phase17 ledger 集成 fixture；结束自动清理。"""
    base_kwargs = dict(get_settings().postgres_connection_kwargs)
    schema_name = f"phase17_holdout_ledger_{uuid4().hex}"
    with psycopg.connect(**base_kwargs) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema_name)))
        connection.commit()
    settings = SimpleNamespace(
        postgres_connection_kwargs={**base_kwargs, "options": f"-c search_path={schema_name}"}
    )
    initialize_phase17_holdout_schema(settings)

    def build() -> PostgresPhase17HoldoutLedger:
        return PostgresPhase17HoldoutLedger(settings, hmac_key=_TEST_HMAC_KEY)

    build.settings = settings
    try:
        yield build
    finally:
        with psycopg.connect(**base_kwargs) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema_name)))
            connection.commit()


def _query(settings, statement: str, params: tuple[object, ...] = ()) -> list[dict[str, object]]:
    with psycopg.connect(
        **settings.postgres_connection_kwargs, row_factory=dict_row
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(statement, params)
            return cursor.fetchall()


def _campaign(
    *,
    contract_digest: str,
    batch_index: int,
    reservation_cny: Decimal,
    candidate_digest: str = "a" * 64,
    dataset_manifest_digest: str = "b" * 64,
) -> Phase17HoldoutCampaign:
    campaign_id = qualification_campaign_id(
        kind=QualificationCampaignKind.HOLDOUT,
        candidate_digest=candidate_digest,
        declared_model_id="gpt-5.6-terra",
        declared_reasoning_effort="high",
        declared_endpoint_hosts=("synapse-ai.uk",),
        batch_index=batch_index,
    )
    return Phase17HoldoutCampaign(
        campaign_id=campaign_id,
        contract_digest=contract_digest,
        batch_index=batch_index,
        candidate_digest=candidate_digest,
        dataset_manifest_digest=dataset_manifest_digest,
        reservation_cny=reservation_cny,
        declared_model_id="gpt-5.6-terra",
        declared_reasoning_effort="high",
        declared_endpoint_hosts=("synapse-ai.uk",),
    )


def test_phase17_contract_registration_initializes_budget_pool(ledger_factory) -> None:
    """契约注册后新预算池快照与冻结封装一致：15 总盘 / 7.078814 可用。"""
    ledger = ledger_factory()
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    ledger.ensure_phase17_contract(contract)
    state = ledger.budget_pool_state(contract.contract_digest)
    assert state["project_budget_cny"] == Decimal("15.000000")
    assert state["forward_budget_remaining_cny"] == Decimal("7.078814")
    assert state["reserved_cny"] == Decimal("0")
    assert state["settled_cny"] == Decimal("0")
    assert state["available_cny"] == Decimal("7.078814")


def test_phase17_campaign_reservation_consumes_pool_idempotently(ledger_factory) -> None:
    """campaign 建立预留预算池；幂等重复建立不得二次预留。"""
    ledger = ledger_factory()
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    ledger.ensure_phase17_contract(contract)
    campaign = _campaign(
        contract_digest=contract.contract_digest,
        batch_index=1,
        reservation_cny=Decimal("1.000000"),
    )
    ledger.ensure_phase17_campaign(campaign)
    state = ledger.budget_pool_state(contract.contract_digest)
    assert state["reserved_cny"] == Decimal("1.000000")
    assert state["available_cny"] == Decimal("6.078814")
    # 幂等：同声明组合重复建立只复验 identity，不重复预留。
    ledger.ensure_phase17_campaign(campaign)
    state = ledger.budget_pool_state(contract.contract_digest)
    assert state["reserved_cny"] == Decimal("1.000000")
    assert state["settled_cny"] == Decimal("0")


def test_phase17_budget_pool_exhaustion_rejected(ledger_factory) -> None:
    """预留累计超过 forward 余额必须 fail-closed（不变式 reserved+settled <= forward）。"""
    ledger = ledger_factory()
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    ledger.ensure_phase17_contract(contract)
    ledger.ensure_phase17_campaign(
        _campaign(
            contract_digest=contract.contract_digest,
            batch_index=1,
            reservation_cny=Decimal("7.000000"),
        )
    )
    with pytest.raises(Phase17HoldoutLedgerError, match="budget pool is exhausted"):
        ledger.ensure_phase17_campaign(
            _campaign(
                contract_digest=contract.contract_digest,
                batch_index=2,
                reservation_cny=Decimal("0.100000"),
            )
        )
    state = ledger.budget_pool_state(contract.contract_digest)
    assert state["reserved_cny"] == Decimal("7.000000")


def test_phase17_settlement_moves_reservation_to_actual(ledger_factory) -> None:
    """结算 = 预留转实际：同一 campaign 在池中至多占用一次（RELEASE + SETTLE）。"""
    ledger = ledger_factory()
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    ledger.ensure_phase17_contract(contract)
    campaign = _campaign(
        contract_digest=contract.contract_digest,
        batch_index=1,
        reservation_cny=Decimal("1.000000"),
    )
    ledger.ensure_phase17_campaign(campaign)
    ledger.settle_phase17_campaign(campaign_id=campaign.campaign_id, actual_cny=Decimal("0.500000"))
    state = ledger.budget_pool_state(contract.contract_digest)
    assert state["reserved_cny"] == Decimal("0")
    assert state["settled_cny"] == Decimal("0.500000")
    assert state["available_cny"] == Decimal("6.578814")
    # 已结算 campaign 不可重复结算。
    with pytest.raises(Phase17HoldoutLedgerError, match="already settled"):
        ledger.settle_phase17_campaign(campaign_id=campaign.campaign_id, actual_cny=Decimal("0.100000"))


def test_phase17_settlement_exceeding_pool_rejected(ledger_factory) -> None:
    """结算金额越过 forward 封装（如 UNKNOWN_USAGE 被滥用）必须 fail-closed。"""
    ledger = ledger_factory()
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    ledger.ensure_phase17_contract(contract)
    campaign = _campaign(
        contract_digest=contract.contract_digest,
        batch_index=1,
        reservation_cny=Decimal("7.078814"),
    )
    ledger.ensure_phase17_campaign(campaign)
    with pytest.raises(Phase17HoldoutLedgerError, match="exceeds budget pool"):
        ledger.settle_phase17_campaign(campaign_id=campaign.campaign_id, actual_cny=Decimal("9.000000"))


def test_phase17_release_frees_reservation(ledger_factory) -> None:
    """未产生成本的 campaign 释放预留后池子恢复；重复释放被拒。"""
    ledger = ledger_factory()
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    ledger.ensure_phase17_contract(contract)
    campaign = _campaign(
        contract_digest=contract.contract_digest,
        batch_index=1,
        reservation_cny=Decimal("1.000000"),
    )
    ledger.ensure_phase17_campaign(campaign)
    ledger.release_phase17_campaign(campaign_id=campaign.campaign_id)
    state = ledger.budget_pool_state(contract.contract_digest)
    assert state["reserved_cny"] == Decimal("0")
    assert state["available_cny"] == Decimal("7.078814")
    with pytest.raises(Phase17HoldoutLedgerError, match="already released"):
        ledger.release_phase17_campaign(campaign_id=campaign.campaign_id)


def test_phase17_release_after_settlement_rejected(ledger_factory) -> None:
    """已结算的 campaign 不得再释放（预留已转实际，不能双重回退）。"""
    ledger = ledger_factory()
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    ledger.ensure_phase17_contract(contract)
    campaign = _campaign(
        contract_digest=contract.contract_digest,
        batch_index=1,
        reservation_cny=Decimal("1.000000"),
    )
    ledger.ensure_phase17_campaign(campaign)
    ledger.settle_phase17_campaign(campaign_id=campaign.campaign_id, actual_cny=Decimal("0.500000"))
    with pytest.raises(Phase17HoldoutLedgerError, match="settled campaign cannot be released"):
        ledger.release_phase17_campaign(campaign_id=campaign.campaign_id)


def test_phase17_run_lifecycle_and_terminalization(ledger_factory) -> None:
    """run 生命周期：slot 冻结 → case 结论逐条追加 → 终态唯一一行。"""
    ledger = ledger_factory()
    settings = ledger_factory.settings
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    ledger.ensure_phase17_contract(contract)
    campaign = _campaign(
        contract_digest=contract.contract_digest,
        batch_index=1,
        reservation_cny=Decimal("1.000000"),
    )
    ledger.ensure_phase17_campaign(campaign)
    run_id = "phase17-holdout-run-0001"
    slot_case_ids = ("holdout-case-001", "holdout-case-002")
    ledger.begin_phase17_run(run_id=run_id, campaign_id=campaign.campaign_id, case_ids=slot_case_ids)
    ledger.record_phase17_case_result(
        run_id=run_id, case_id="holdout-case-001",
        input_digest="1" * 64, outcome="PASS", reason_code="MULTI_AGENT_READY",
        receipt_count=2, cost_cny=Decimal("0.012000"),
    )
    ledger.record_phase17_case_result(
        run_id=run_id, case_id="holdout-case-002",
        input_digest="2" * 64, outcome="FAILED", reason_code="PLANNER_VALIDATION_FAILED",
        receipt_count=2, cost_cny=Decimal("0.012000"),
    )
    ledger.close_phase17_run(
        run_id=run_id, status="FAILED", reason_code="PHASE17_HOLDOUT_EXECUTION_FAILED",
        payload={"campaign_id": campaign.campaign_id, "pass_count": 1, "total": 2},
    )
    rows = _query(
        settings,
        "SELECT status, reason_code FROM phase17_holdout_run_results WHERE run_id=%s",
        (run_id,),
    )
    assert rows == [{"status": "FAILED", "reason_code": "PHASE17_HOLDOUT_EXECUTION_FAILED"}]
    case_rows = _query(
        settings,
        "SELECT case_id, outcome FROM phase17_holdout_case_results WHERE run_id=%s ORDER BY case_id",
        (run_id,),
    )
    assert case_rows == [
        {"case_id": "holdout-case-001", "outcome": "PASS"},
        {"case_id": "holdout-case-002", "outcome": "FAILED"},
    ]


def test_phase17_case_result_rejected_after_terminal_run(ledger_factory) -> None:
    """run 已终态后追加 case 结论必须被 SQL 触发器拒绝（Python 预检不是安全边界）。"""
    ledger = ledger_factory()
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    ledger.ensure_phase17_contract(contract)
    campaign = _campaign(
        contract_digest=contract.contract_digest,
        batch_index=1,
        reservation_cny=Decimal("1.000000"),
    )
    ledger.ensure_phase17_campaign(campaign)
    run_id = "phase17-holdout-run-0002"
    ledger.begin_phase17_run(run_id=run_id, campaign_id=campaign.campaign_id, case_ids=("holdout-case-001",))
    ledger.close_phase17_run(
        run_id=run_id, status="PASS", reason_code="PHASE17_HOLDOUT_BATCH_COMPLETE",
        payload={"pass_count": 1, "total": 1},
    )
    with pytest.raises(Phase17HoldoutLedgerError):
        ledger.record_phase17_case_result(
            run_id=run_id, case_id="holdout-case-001",
            input_digest="1" * 64, outcome="PASS", reason_code="MULTI_AGENT_READY",
            receipt_count=2, cost_cny=Decimal("0.012000"),
        )


def test_phase17_case_result_outside_slot_rejected(ledger_factory) -> None:
    """case 结论必须属于冻结的 run slot 集合；集合外 case 被 SQL 触发器拒绝。"""
    ledger = ledger_factory()
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    ledger.ensure_phase17_contract(contract)
    campaign = _campaign(
        contract_digest=contract.contract_digest,
        batch_index=1,
        reservation_cny=Decimal("1.000000"),
    )
    ledger.ensure_phase17_campaign(campaign)
    run_id = "phase17-holdout-run-0003"
    ledger.begin_phase17_run(run_id=run_id, campaign_id=campaign.campaign_id, case_ids=("holdout-case-001",))
    with pytest.raises(Phase17HoldoutLedgerError):
        ledger.record_phase17_case_result(
            run_id=run_id, case_id="holdout-case-099",
            input_digest="9" * 64, outcome="PASS", reason_code="MULTI_AGENT_READY",
            receipt_count=2, cost_cny=Decimal("0.012000"),
        )


def test_phase17_ledger_tables_are_append_only(ledger_factory) -> None:
    """整个表族无 UPDATE/DELETE：任何改写都被触发器拒绝。"""
    ledger = ledger_factory()
    settings = ledger_factory.settings
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    ledger.ensure_phase17_contract(contract)
    with pytest.raises(psycopg.Error, match="append-only"):
        _query(
            settings,
            "UPDATE phase17_holdout_contracts SET project_budget_cny=1.0 WHERE contract_digest=%s",
            (contract.contract_digest,),
        )
    with pytest.raises(psycopg.Error, match="append-only"):
        _query(settings, "DELETE FROM phase17_holdout_contracts")


def test_phase17_reverse_isolation_v2_digest_has_no_contract_row(ledger_factory) -> None:
    """反向隔离：v2 历史 policy digest 在 phase17 表族中永远找不到 contract 行。

    phase17 账本只认经 loader + approved registry 审核的 contract digest；
    v2/v3 policy digest 在此表族无行，campaign 建立与池查询一律 fail-closed。
    """
    ledger = ledger_factory()
    campaign = _campaign(
        contract_digest=_V2_HISTORICAL_POLICY_DIGEST,
        batch_index=1,
        reservation_cny=Decimal("1.000000"),
    )
    with pytest.raises(Phase17HoldoutLedgerError, match="contract is not registered"):
        ledger.ensure_phase17_campaign(campaign)
    with pytest.raises(Phase17HoldoutLedgerError, match="contract is not registered"):
        ledger.budget_pool_state(_V2_HISTORICAL_POLICY_DIGEST)


def _record_attempt(ledger, *, run_id: str, case_id: str = "holdout-case-001",
                    stage: str = "ANALYST", outcome: str = "PASS") -> str:
    """最小 attempt 记录；返回写入行的 attempt_id（无则返回空串）。"""
    ledger.record_phase17_attempt(
        run_id=run_id,
        case_id=case_id,
        stage=stage,
        attempt_index=1,
        request_id=f"req-{run_id}-{case_id}-{stage}",
        endpoint_host="synapse-ai.uk",
        model_id="gpt-5.6-terra",
        outcome=outcome,
        category=None if outcome == "PASS" else "PLANNER_VALIDATION_FAILED",
        response_digest="a" * 64,
        provider_response_id=None,
        http_status=None,
        latency_ms=Decimal("0"),
        attempts=1,
        input_tokens=1000,
        output_tokens=500,
        total_tokens=1500,
        cost_cny=Decimal("0.006000"),
    )


def test_phase17_attempt_records_receipt_hmac_and_case_link(ledger_factory) -> None:
    """P0-3：attempt 逐条入账，receipt_hmac 由 ledger 内部计算且不可外部伪造。"""
    ledger = ledger_factory()
    settings = ledger_factory.settings
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    ledger.ensure_phase17_contract(contract)
    campaign = _campaign(
        contract_digest=contract.contract_digest,
        batch_index=1,
        reservation_cny=Decimal("1.000000"),
    )
    ledger.ensure_phase17_campaign(campaign)
    run_id = "phase17-holdout-run-0031"
    ledger.begin_phase17_run(run_id=run_id, campaign_id=campaign.campaign_id,
                             case_ids=("holdout-case-001", "holdout-case-002"))
    _record_attempt(ledger, run_id=run_id)
    rows = _query(
        settings,
        "SELECT stage, outcome, endpoint_host, model_id, cost_cny, receipt_hmac,"
        " response_digest FROM phase17_holdout_attempts WHERE run_id=%s",
        (run_id,),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["stage"] == "ANALYST" and row["outcome"] == "PASS"
    assert row["endpoint_host"] == "synapse-ai.uk" and row["model_id"] == "gpt-5.6-terra"
    assert row["cost_cny"] == Decimal("0.006000")
    assert row["response_digest"] == "a" * 64
    assert row["receipt_hmac"] and len(row["receipt_hmac"]) == 64
    assert set(row["receipt_hmac"]) <= set("0123456789abcdef")
    # 不同响应事实 → 不同 HMAC（内部 _tag 覆盖响应事实与成本，非恒等占位）。
    _record_attempt(ledger, run_id=run_id, case_id="holdout-case-002", stage="PLANNER",
                    outcome="FAILED")
    rows2 = _query(
        settings,
        "SELECT receipt_hmac FROM phase17_holdout_attempts WHERE run_id=%s ORDER BY case_id",
        (run_id,),
    )
    assert len(rows2) == 2
    assert rows2[0]["receipt_hmac"] != rows2[1]["receipt_hmac"]


def test_phase17_attempt_slot_duplicate_rejected(ledger_factory) -> None:
    """同一 (run, case, stage, attempt_index) 不允许二次入账：UNIQUE 约束拒绝。"""
    ledger = ledger_factory()
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    ledger.ensure_phase17_contract(contract)
    campaign = _campaign(
        contract_digest=contract.contract_digest,
        batch_index=1,
        reservation_cny=Decimal("1.000000"),
    )
    ledger.ensure_phase17_campaign(campaign)
    run_id = "phase17-holdout-run-0032"
    ledger.begin_phase17_run(run_id=run_id, campaign_id=campaign.campaign_id,
                             case_ids=("holdout-case-001",))
    _record_attempt(ledger, run_id=run_id)
    with pytest.raises(Phase17HoldoutLedgerError, match="attempt append failed"):
        _record_attempt(ledger, run_id=run_id)


def test_phase17_attempt_append_only_and_terminal_run_rejection(ledger_factory) -> None:
    """attempts 表无 UPDATE/DELETE；run 终态后追加 attempt 被 SQL 触发器拒绝。"""
    ledger = ledger_factory()
    settings = ledger_factory.settings
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    ledger.ensure_phase17_contract(contract)
    campaign = _campaign(
        contract_digest=contract.contract_digest,
        batch_index=1,
        reservation_cny=Decimal("1.000000"),
    )
    ledger.ensure_phase17_campaign(campaign)
    run_id = "phase17-holdout-run-0033"
    ledger.begin_phase17_run(run_id=run_id, campaign_id=campaign.campaign_id,
                             case_ids=("holdout-case-001",))
    _record_attempt(ledger, run_id=run_id)
    with pytest.raises(psycopg.Error, match="append-only"):
        _query(settings, "UPDATE phase17_holdout_attempts SET outcome='FAILED' WHERE run_id=%s",
               (run_id,))
    with pytest.raises(psycopg.Error, match="append-only"):
        _query(settings, "DELETE FROM phase17_holdout_attempts WHERE run_id=%s", (run_id,))
    # run 终态后追加 attempt：slot 触发器拒绝（run 未终态是写入前置条件）。
    ledger.close_phase17_run(
        run_id=run_id, status="PASS", reason_code="PHASE17_HOLDOUT_BATCH_THRESHOLD_MET",
        payload={"pass_count": 1, "total": 1},
    )
    with pytest.raises(Phase17HoldoutLedgerError, match="attempt append failed"):
        _record_attempt(ledger, run_id=run_id, case_id="holdout-case-002")



def test_phase17_schema_ready_and_run_ledger_state(ledger_factory) -> None:
    """18 轮 P1-4：schema 存在性检查 + 异常终态化查询（run 未终态成本累计）。"""
    from src.decision_support.phase17_holdout_ledger import (
        phase17_holdout_schema_ready,
    )

    settings = ledger_factory.settings
    assert phase17_holdout_schema_ready(settings) is True
    ledger = ledger_factory()
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    ledger.ensure_phase17_contract(contract)
    campaign = _campaign(
        contract_digest=contract.contract_digest,
        batch_index=1,
        reservation_cny=Decimal("1.000000"),
    )
    ledger.ensure_phase17_campaign(campaign)
    run_id = "phase17-holdout-run-state-1"
    ledger.begin_phase17_run(run_id=run_id, campaign_id=campaign.campaign_id, case_ids=("holdout-case-001",))
    _record_attempt(ledger, run_id=run_id)
    _record_attempt(ledger, run_id=run_id, case_id="holdout-case-001", stage="PLANNER")

    state = ledger.phase17_run_ledger_state(run_id=run_id)
    assert state["run_exists"] is True
    assert state["terminal"] is False
    assert state["attempt_cost_cny"] == Decimal("0.012000")
    assert state["campaign_id"] == campaign.campaign_id

    ledger.close_phase17_run(
        run_id=run_id, status="FAILED", reason_code="PHASE17_RUN_ABORTED",
        payload={"run_id": run_id},
    )
    assert ledger.phase17_run_ledger_state(run_id=run_id)["terminal"] is True
    assert ledger.phase17_run_ledger_state(run_id="unknown-run")["run_exists"] is False


def test_phase17_batch_run_report_identity_and_cases(ledger_factory) -> None:
    """18 轮 P1-4：--aggregate 输入——终态 run 返回全链身份与 case 级事实。"""
    ledger = ledger_factory()
    settings = ledger_factory.settings
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    ledger.ensure_phase17_contract(contract)
    campaign = _campaign(
        contract_digest=contract.contract_digest,
        batch_index=1,
        reservation_cny=Decimal("1.000000"),
    )
    ledger.ensure_phase17_campaign(campaign)
    assert (
        ledger.phase17_batch_run_report(
            contract_digest=contract.contract_digest, batch_index=1
        )
        is None
    )
    run_id = "phase17-holdout-run-report-1"
    ledger.begin_phase17_run(
        run_id=run_id, campaign_id=campaign.campaign_id, case_ids=("holdout-case-001",)
    )
    ledger.record_phase17_case_result(
        run_id=run_id, case_id="holdout-case-001",
        input_digest="c" * 64, outcome="PASS",
        reason_code="EXECUTION_COMPLETE", receipt_count=2, cost_cny=Decimal("0.006000"),
    )
    ledger.close_phase17_run(
        run_id=run_id, status="PASS", reason_code="PHASE17_HOLDOUT_BATCH_THRESHOLD_MET",
        payload={"run_id": run_id},
    )
    data = ledger.phase17_batch_run_report(
        contract_digest=contract.contract_digest, batch_index=1
    )
    assert data is not None
    assert data["run_id"] == run_id
    assert data["campaign_id"] == campaign.campaign_id
    assert data["candidate_digest"] == "a" * 64
    assert data["dataset_manifest_digest"] == "b" * 64
    assert data["status"] == "PASS"
    assert len(data["cases"]) == 1
    assert data["cases"][0]["outcome"] == "PASS"
    assert data["cases"][0]["receipt_count"] == 2
    assert data["cases"][0]["cost_cny"] == Decimal("0.006000")


def test_phase17_qualification_record_unique_and_identity(ledger_factory) -> None:
    """18 轮 P1-4：27/30 结论只入账一次；batch 归属/终态/重复记录全部拒绝。"""
    ledger = ledger_factory()
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    ledger.ensure_phase17_contract(contract)
    campaigns = {}
    for batch_index in (1, 2):
        campaign = _campaign(
            contract_digest=contract.contract_digest,
            batch_index=batch_index,
            reservation_cny=Decimal("1.000000"),
        )
        ledger.ensure_phase17_campaign(campaign)
        campaigns[batch_index] = campaign
    run_ids = {}
    for batch_index in (1, 2):
        run_id = f"phase17-holdout-run-qual-{batch_index}"
        run_ids[batch_index] = run_id
        ledger.begin_phase17_run(
            run_id=run_id,
            campaign_id=campaigns[batch_index].campaign_id,
            case_ids=(f"holdout-case-00{batch_index}",),
        )
        ledger.close_phase17_run(
            run_id=run_id, status="PASS",
            reason_code="PHASE17_HOLDOUT_BATCH_THRESHOLD_MET",
            payload={"run_id": run_id},
        )
    ledger.record_phase17_qualification(
        qualification_id="phase17-holdout-qualification-1",
        run1_id=run_ids[1], run2_id=run_ids[2],
        contract_digest=contract.contract_digest,
        candidate_digest="a" * 64,
        dataset_manifest_digest="b" * 64,
        status="QUALIFIED",
        reason_code="PHASE17_HOLDOUT_QUALIFIED_90PCT_PORTFOLIO_THRESHOLD",
        total_pass=28, total_cases=30, pass_min=27,
        critical_safety_failures=0,
        evaluation_digest="d" * 64,
    )
    records = ledger.phase17_qualification_records(contract_digest=contract.contract_digest)
    assert len(records) == 1
    assert records[0]["status"] == "QUALIFIED"
    assert records[0]["total_pass"] == 28
    with pytest.raises(Phase17HoldoutLedgerError, match="qualification record failed"):
        ledger.record_phase17_qualification(
            qualification_id="phase17-holdout-qualification-2",
            run1_id=run_ids[1], run2_id=run_ids[2],
            contract_digest=contract.contract_digest,
            candidate_digest="a" * 64,
            dataset_manifest_digest="b" * 64,
            status="QUALIFIED",
            reason_code="PHASE17_HOLDOUT_QUALIFIED_90PCT_PORTFOLIO_THRESHOLD",
            total_pass=28, total_cases=30, pass_min=27,
            critical_safety_failures=0,
            evaluation_digest="d" * 64,
        )  # UNIQUE(run1_id, run2_id) 兜底

    # batch 归属错误：batch2 的 run 冒充 batch1。
    with pytest.raises(Phase17HoldoutLedgerError, match="batch identity mismatch"):
        ledger.record_phase17_qualification(
            qualification_id="phase17-holdout-qualification-3",
            run1_id=run_ids[2], run2_id=run_ids[1],
            contract_digest=contract.contract_digest,
            candidate_digest="a" * 64,
            dataset_manifest_digest="b" * 64,
            status="QUALIFIED",
            reason_code="PHASE17_HOLDOUT_QUALIFIED_90PCT_PORTFOLIO_THRESHOLD",
            total_pass=28, total_cases=30, pass_min=27,
            critical_safety_failures=0,
            evaluation_digest="d" * 64,
        )

    # 未终态 run 拒绝。
    ledger.begin_phase17_run(
        run_id="phase17-holdout-run-qual-open",
        campaign_id=campaigns[1].campaign_id,
        case_ids=("holdout-case-001",),
    )
    with pytest.raises(Phase17HoldoutLedgerError, match="requires terminal runs"):
        ledger.record_phase17_qualification(
            qualification_id="phase17-holdout-qualification-4",
            run1_id="phase17-holdout-run-qual-open", run2_id=run_ids[2],
            contract_digest=contract.contract_digest,
            candidate_digest="a" * 64,
            dataset_manifest_digest="b" * 64,
            status="QUALIFIED",
            reason_code="PHASE17_HOLDOUT_QUALIFIED_90PCT_PORTFOLIO_THRESHOLD",
            total_pass=28, total_cases=30, pass_min=27,
            critical_safety_failures=0,
            evaluation_digest="d" * 64,
        )


def test_phase17_attempt_hmac_covers_token_fields(ledger_factory) -> None:
    """18 轮 P0-3：attempt receipt_hmac 的 payload 必须覆盖 token 字段——
    tokens 变化必然改变 HMAC（审计可复算，防 token 事后漂移）。"""
    ledger = ledger_factory()
    settings = ledger_factory.settings
    contract = load_phase17_holdout_execution_contract(repository_root=_PROJECT_ROOT)
    ledger.ensure_phase17_contract(contract)
    campaign = _campaign(
        contract_digest=contract.contract_digest,
        batch_index=1,
        reservation_cny=Decimal("1.000000"),
    )
    ledger.ensure_phase17_campaign(campaign)
    run_id = "phase17-holdout-run-hmac-tokens"
    ledger.begin_phase17_run(run_id=run_id, campaign_id=campaign.campaign_id, case_ids=("holdout-case-001",))
    _record_attempt(ledger, run_id=run_id)  # tokens 1000/500/1500
    _record_attempt(ledger, run_id=run_id, case_id="holdout-case-001", stage="PLANNER")

    rows = _query(
        settings,
        "SELECT stage, receipt_hmac FROM phase17_holdout_attempts WHERE run_id=%s ORDER BY stage",
        (run_id,),
    )
    assert len(rows) == 2
    assert rows[0]["receipt_hmac"] != rows[1]["receipt_hmac"]
    # 与 ledger 内部 _tag 重算比对：payload 含 token 字段。
    expected_1 = ledger._tag(
        domain="attempt",
        payload={
            "request_id": f"req-{run_id}-holdout-case-001-ANALYST",
            "run_id": run_id,
            "case_id": "holdout-case-001",
            "stage": "ANALYST",
            "attempt_index": 1,
            "endpoint_host": "synapse-ai.uk",
            "model_id": "gpt-5.6-terra",
            "outcome": "PASS",
            "category": None,
            "response_digest": "a" * 64,
            "provider_response_id": None,
            "http_status": None,
            "latency_ms": "0",
            "attempts": 1,
            "input_tokens": 1000,
            "output_tokens": 500,
            "total_tokens": 1500,
            "cost_cny": "0.006000",
            "artifact_path": None,
            "artifact_digest": None,
            "artifact_capture_status": "UNAVAILABLE",
        },
    )
    assert rows[0]["receipt_hmac"] == expected_1
    # 若 payload 不覆盖 tokens，伪造不同 tokens 也会算同 HMAC；这里验证差异。
    forged = ledger._tag(
        domain="attempt",
        payload={
            "request_id": f"req-{run_id}-holdout-case-001-ANALYST",
            "run_id": run_id,
            "case_id": "holdout-case-001",
            "stage": "ANALYST",
            "attempt_index": 1,
            "endpoint_host": "synapse-ai.uk",
            "model_id": "gpt-5.6-terra",
            "outcome": "PASS",
            "category": None,
            "response_digest": "a" * 64,
            "provider_response_id": None,
            "http_status": None,
            "latency_ms": "0",
            "attempts": 1,
            "input_tokens": 999,
            "output_tokens": 500,
            "total_tokens": 1499,
            "cost_cny": "0.006000",
            "artifact_path": None,
            "artifact_digest": None,
            "artifact_capture_status": "UNAVAILABLE",
        },
    )
    assert forged != rows[0]["receipt_hmac"]

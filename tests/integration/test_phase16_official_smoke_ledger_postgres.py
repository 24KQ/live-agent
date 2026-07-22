"""Phase 16 正式真实模型 smoke 账本的 PostgreSQL RED/GREEN 契约。

这些测试只连接隔离 PostgreSQL schema，绝不读取 LLM 凭据、构造模型端口或发送网络请求。
它们锁定正式账本和旧 ``phase16_smoke_*`` 预算表完全隔离的持久化边界。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid4, uuid5

import psycopg
import pytest
from psycopg import sql

from src.config.settings import get_settings
from src.decision_support.official_smoke_evidence import (
    load_phase16_official_smoke_evidence_manifest,
)
from src.decision_support.official_smoke_ledger import (
    PHASE16_OFFICIAL_SMOKE_CASE_RESERVATION_CNY,
    PHASE16_OFFICIAL_SMOKE_HISTORICAL_DIRECT_MODE_CNY,
    PHASE16_OFFICIAL_SMOKE_MAX_EXPOSURE_CNY,
    Phase16OfficialSmokeCaseOutcomeStatus,
    Phase16OfficialSmokeDispatchStage,
    Phase16OfficialSmokeLedgerError,
    Phase16OfficialSmokeReceiptAuthenticator,
    Phase16OfficialSmokeValidationVerdict,
    PostgresPhase16OfficialSmokeLedger,
    initialize_phase16_official_smoke_ledger_schema,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 测试使用固定的非生产 HMAC key，只验证“数据库直写者没有 key 时不能伪造正式证据”。
# 真实 smoke 会在后续 Task 3 从受控本机配置注入独立 key，绝不把真实 key 写入测试或数据库。
_TEST_RECEIPT_SIGNING_KEY = bytes.fromhex("4f" * 32)


@pytest.fixture()
def postgres_official_smoke_ledger_factory():
    """为每个测试创建独立 schema，重启测试使用同一 schema 的新 Ledger 实例。"""

    base_kwargs = dict(get_settings().postgres_connection_kwargs)
    schema_name = f"phase16_official_smoke_{uuid4().hex}"
    with psycopg.connect(**base_kwargs) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}; ").format(sql.Identifier(schema_name)))
        connection.commit()
    settings = SimpleNamespace(
        postgres_connection_kwargs={**base_kwargs, "options": f"-c search_path={schema_name}"}
    )
    initialize_phase16_official_smoke_ledger_schema(settings)

    def build_ledger() -> PostgresPhase16OfficialSmokeLedger:
        """用同一隔离 schema 构造新实例，供并发与重启测试复用。"""

        return PostgresPhase16OfficialSmokeLedger(
            settings,
            receipt_authenticator=Phase16OfficialSmokeReceiptAuthenticator(
                _TEST_RECEIPT_SIGNING_KEY
            ),
        )

    # 测试直接 SQL 防线时需要同一受限 search_path；把它绑定在 fixture helper 上，
    # 避免测试读取 Ledger 的私有实现字段或给生产类添加测试专用 API。
    build_ledger.settings = settings
    try:
        # 每次调用构造新的 Python 对象，验证事实来自 PostgreSQL 而非进程内缓存。
        yield build_ledger
    finally:
        with psycopg.connect(**base_kwargs) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE;").format(sql.Identifier(schema_name)))
            connection.commit()


def _manifest():
    """读取 Task 1 已冻结的十例 Manifest，测试不得临时生成或扩大 case 集。"""

    return load_phase16_official_smoke_evidence_manifest(repository_root=PROJECT_ROOT)


def _internal_request_id(label: str) -> str:
    """为测试生成规范 UUID，正式账本不得把自由文本作为内部 request ID 持久化。"""

    return str(uuid5(NAMESPACE_URL, f"phase16-official-smoke-test:{label}"))


def test_official_ledger_imports_historical_spend_once_and_freezes_ten_slots(
    postgres_official_smoke_ledger_factory,
) -> None:
    """正式 run 必须一次性导入历史支出并固定十例，重启后不能扩展或重复扣费。"""

    first = postgres_official_smoke_ledger_factory()
    manifest = _manifest()

    initialized = first.ensure_run(manifest)

    assert initialized.run_id == manifest.run_id
    assert initialized.historical_spend_cny == PHASE16_OFFICIAL_SMOKE_HISTORICAL_DIRECT_MODE_CNY
    assert initialized.fixed_case_slot_count == 10
    assert initialized.case_reservation_cny == PHASE16_OFFICIAL_SMOKE_CASE_RESERVATION_CNY
    assert initialized.maximum_exposure_cny == PHASE16_OFFICIAL_SMOKE_MAX_EXPOSURE_CNY
    assert initialized.maximum_exposure_cny == Decimal("0.993220")

    # 模拟进程重启后重复初始化：历史支出和 slot 集必须仍然只有一份。
    restarted = postgres_official_smoke_ledger_factory()
    replayed = restarted.ensure_run(manifest)

    assert replayed == initialized
    assert tuple(slot.case_id for slot in restarted.list_case_slots()) == manifest.case_ids


def test_official_ledger_claims_each_frozen_case_once_under_concurrency(
    postgres_official_smoke_ledger_factory,
) -> None:
    """两个连接竞争同一 case 时只能有一个新 claim，伪造第十一 slot 必须 fail-closed。"""

    manifest = _manifest()
    first = postgres_official_smoke_ledger_factory()
    first.ensure_run(manifest)
    target_case_id = manifest.case_ids[0]

    def claim_from_fresh_process():
        """每个线程创建独立 Ledger，证明单胜者来自 PostgreSQL run 行锁而不是内存锁。"""

        return postgres_official_smoke_ledger_factory().claim_case(target_case_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _index: claim_from_fresh_process(), range(2)))

    assert sum(claim.created for claim in claims) == 1
    assert {claim.case_id for claim in claims} == {target_case_id}
    assert {claim.reserved_amount_cny for claim in claims} == {
        PHASE16_OFFICIAL_SMOKE_CASE_RESERVATION_CNY
    }

    with pytest.raises(Phase16OfficialSmokeLedgerError, match="frozen case slot"):
        first.claim_case("phase16-forged-eleventh-case")


def test_planner_dispatch_requires_same_case_analyst_pass_and_receipt_is_append_only(
    postgres_official_smoke_ledger_factory,
) -> None:
    """Planner 只能接在同 case 的 Analyst PASS 后，重复 Provider receipt 必须被拒绝。"""

    manifest = _manifest()
    ledger = postgres_official_smoke_ledger_factory()
    ledger.ensure_run(manifest)
    claim = ledger.claim_case(manifest.case_ids[0])

    with pytest.raises(Phase16OfficialSmokeLedgerError, match="analyst validation"):
        ledger.begin_dispatch(
            claim_id=claim.claim_id,
            stage=Phase16OfficialSmokeDispatchStage.PLANNER,
            profile_digest=manifest.profile_digests["planner"],
            internal_request_id=_internal_request_id("planner-before-analyst"),
        )

    analyst_attempt = ledger.begin_dispatch(
        claim_id=claim.claim_id,
        stage=Phase16OfficialSmokeDispatchStage.ANALYST,
        profile_digest=manifest.profile_digests["analyst"],
        internal_request_id=_internal_request_id("analyst-001"),
    )
    receipt = ledger.append_provider_receipt(
        attempt_id=analyst_attempt.attempt_id,
        provider_response_id="chatcmpl-formal-analyst-001",
        finish_reason="stop",
        model_id="deepseek-v4-flash",
        response_digest="a" * 64,
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        latency_ms=Decimal("12.500"),
    )

    assert receipt.total_cost_cny == Decimal("0.000200")
    with pytest.raises(Phase16OfficialSmokeLedgerError, match="provider receipt already exists"):
        ledger.append_provider_receipt(
            attempt_id=analyst_attempt.attempt_id,
            provider_response_id="chatcmpl-formal-analyst-duplicate",
            finish_reason="stop",
            model_id="deepseek-v4-flash",
            response_digest="b" * 64,
            input_tokens=100,
            output_tokens=50,
            total_tokens=150,
            latency_ms=Decimal("12.500"),
        )

    ledger.append_validation_fact(
        attempt_id=analyst_attempt.attempt_id,
        verdict=Phase16OfficialSmokeValidationVerdict.PASS,
        reason_code="ANALYST_VALIDATED",
        validation_digest="c" * 64,
    )
    planner_attempt = ledger.begin_dispatch(
        claim_id=claim.claim_id,
        stage=Phase16OfficialSmokeDispatchStage.PLANNER,
        profile_digest=manifest.profile_digests["planner"],
        internal_request_id=_internal_request_id("planner-001"),
    )

    assert planner_attempt.stage is Phase16OfficialSmokeDispatchStage.PLANNER


def test_restart_recovers_open_attempt_as_unknown_failure_without_resend(
    postgres_official_smoke_ledger_factory,
) -> None:
    """崩溃后的 OPEN attempt 必须追加 FAILED 终态，重启实例不允许补发同一 Analyst。"""

    manifest = _manifest()
    first = postgres_official_smoke_ledger_factory()
    first.ensure_run(manifest)
    claim = first.claim_case(manifest.case_ids[1])
    first.begin_dispatch(
        claim_id=claim.claim_id,
        stage=Phase16OfficialSmokeDispatchStage.ANALYST,
        profile_digest=manifest.profile_digests["analyst"],
        internal_request_id=_internal_request_id("crash-analyst-001"),
    )

    # 新实例模拟服务进程重启。它没有端口对象，因此恢复本身不可能偷偷发送模型请求。
    restarted = postgres_official_smoke_ledger_factory()
    recovered = restarted.recover_open_attempts()

    assert len(recovered) == 1
    assert recovered[0].status is Phase16OfficialSmokeCaseOutcomeStatus.FAILED
    assert recovered[0].reason_code == "UNKNOWN_ATTEMPT_AFTER_RESTART"
    assert restarted.get_case_outcome(case_id=claim.case_id) == recovered[0]
    with pytest.raises(Phase16OfficialSmokeLedgerError, match="attempt already exists"):
        restarted.begin_dispatch(
            claim_id=claim.claim_id,
            stage=Phase16OfficialSmokeDispatchStage.ANALYST,
            profile_digest=manifest.profile_digests["analyst"],
            internal_request_id=_internal_request_id("crash-analyst-resend"),
        )


def test_case_pass_requires_two_validated_receipts_and_persists_terminal_outcome(
    postgres_official_smoke_ledger_factory,
) -> None:
    """PASS 只接受完整 Analyst/Planner 链，重启后终态仍可按 case 精确读取。"""

    manifest = _manifest()
    ledger = postgres_official_smoke_ledger_factory()
    ledger.ensure_run(manifest)
    claim = ledger.claim_case(manifest.case_ids[2])

    analyst = ledger.begin_dispatch(
        claim_id=claim.claim_id,
        stage=Phase16OfficialSmokeDispatchStage.ANALYST,
        profile_digest=manifest.profile_digests["analyst"],
        internal_request_id=_internal_request_id("pass-analyst-001"),
    )
    ledger.append_provider_receipt(
        attempt_id=analyst.attempt_id,
        provider_response_id="chatcmpl-formal-pass-analyst",
        finish_reason="stop",
        model_id="deepseek-v4-flash",
        response_digest="d" * 64,
        input_tokens=10,
        output_tokens=10,
        total_tokens=20,
        latency_ms=Decimal("1.000"),
    )
    ledger.append_validation_fact(
        attempt_id=analyst.attempt_id,
        verdict=Phase16OfficialSmokeValidationVerdict.PASS,
        reason_code="ANALYST_VALIDATED",
        validation_digest="e" * 64,
    )
    planner = ledger.begin_dispatch(
        claim_id=claim.claim_id,
        stage=Phase16OfficialSmokeDispatchStage.PLANNER,
        profile_digest=manifest.profile_digests["planner"],
        internal_request_id=_internal_request_id("pass-planner-001"),
    )

    with pytest.raises(Phase16OfficialSmokeLedgerError, match="two validated provider receipts"):
        ledger.close_case(
            claim_id=claim.claim_id,
            status=Phase16OfficialSmokeCaseOutcomeStatus.PASS,
            reason_code="FORMAL_CASE_PASS",
        )

    ledger.append_provider_receipt(
        attempt_id=planner.attempt_id,
        provider_response_id="chatcmpl-formal-pass-planner",
        finish_reason="stop",
        model_id="deepseek-v4-flash",
        response_digest="f" * 64,
        input_tokens=10,
        output_tokens=10,
        total_tokens=20,
        latency_ms=Decimal("1.000"),
    )
    ledger.append_validation_fact(
        attempt_id=planner.attempt_id,
        verdict=Phase16OfficialSmokeValidationVerdict.PASS,
        reason_code="PLANNER_VALIDATED",
        validation_digest="0" * 64,
    )
    outcome = ledger.close_case(
        claim_id=claim.claim_id,
        status=Phase16OfficialSmokeCaseOutcomeStatus.PASS,
        reason_code="FORMAL_CASE_PASS",
    )

    assert outcome.status is Phase16OfficialSmokeCaseOutcomeStatus.PASS
    assert postgres_official_smoke_ledger_factory().get_case_outcome(case_id=claim.case_id) == outcome
    assert postgres_official_smoke_ledger_factory().verify_case_outcome_receipts(
        case_id=claim.case_id
    ) == outcome


def test_database_rejects_direct_planner_bypass_and_excludes_sensitive_columns(
    postgres_official_smoke_ledger_factory,
) -> None:
    """即使绕过 Python，DDL 也拒绝 Planner 早发；账本结构不得有模型正文或凭据字段。"""

    manifest = _manifest()
    ledger = postgres_official_smoke_ledger_factory()
    ledger.ensure_run(manifest)
    claim = ledger.claim_case(manifest.case_ids[3])

    with psycopg.connect(**postgres_official_smoke_ledger_factory.settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            with pytest.raises(psycopg.errors.RaiseException, match="planner requires analyst"):
                cursor.execute(
                    """INSERT INTO phase16_official_smoke_dispatch_attempts
                       (attempt_id, run_id, case_id, claim_id, stage, profile_digest, internal_request_id)
                       VALUES (%s::uuid,%s,%s,%s::uuid,'PLANNER',%s,%s);""",
                    (
                        str(uuid4()),
                        manifest.run_id,
                        claim.case_id,
                        claim.claim_id,
                        manifest.profile_digests["planner"],
                        _internal_request_id("forged-direct-planner"),
                    ),
                )
        connection.rollback()

    with psycopg.connect(**postgres_official_smoke_ledger_factory.settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT table_name, column_name
                     FROM information_schema.columns
                    WHERE table_schema=current_schema()
                      AND table_name LIKE 'phase16_official_smoke_%';"""
            )
            columns = {(row[0], row[1]) for row in cursor.fetchall()}

    forbidden_column_names = {
        "api_key",
        "prompt",
        "prompt_text",
        "messages",
        "model_output",
        "response_body",
        "reasoning",
        "recommendation",
    }
    assert not ({column_name for _table_name, column_name in columns} & forbidden_column_names)


def test_database_rejects_direct_pass_outcome_without_two_validated_receipts(
    postgres_official_smoke_ledger_factory,
) -> None:
    """直接 SQL 也不能把空 case 写成 PASS，正式成功必须由两段 receipt/validation 支撑。"""

    manifest = _manifest()
    ledger = postgres_official_smoke_ledger_factory()
    ledger.ensure_run(manifest)
    claim = ledger.claim_case(manifest.case_ids[4])

    with psycopg.connect(**postgres_official_smoke_ledger_factory.settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            with pytest.raises(psycopg.errors.RaiseException, match="PASS requires two validated"):
                cursor.execute(
                    """INSERT INTO phase16_official_smoke_case_outcomes
                       (run_id, case_id, claim_id, status, reason_code, outcome_digest)
                       VALUES (%s,%s,%s::uuid,'PASS','FORGED_DIRECT_PASS',%s);""",
                    (
                        manifest.run_id,
                        claim.case_id,
                        claim.claim_id,
                        "a" * 64,
                    ),
                )
        connection.rollback()


def test_database_rejects_direct_receipt_with_cost_inconsistent_with_frozen_price(
    postgres_official_smoke_ledger_factory,
) -> None:
    """直接 SQL 不得用零成本伪造已知 usage，receipt 金额必须匹配冻结 DeepSeek 价格。"""

    manifest = _manifest()
    ledger = postgres_official_smoke_ledger_factory()
    ledger.ensure_run(manifest)
    claim = ledger.claim_case(manifest.case_ids[5])
    analyst = ledger.begin_dispatch(
        claim_id=claim.claim_id,
        stage=Phase16OfficialSmokeDispatchStage.ANALYST,
        profile_digest=manifest.profile_digests["analyst"],
        internal_request_id=_internal_request_id("direct-cost-analyst"),
    )

    with psycopg.connect(**postgres_official_smoke_ledger_factory.settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            with pytest.raises(psycopg.errors.RaiseException, match="receipt cost conflicts with frozen price"):
                cursor.execute(
                    """INSERT INTO phase16_official_smoke_provider_receipts
                       (attempt_id, provider_response_id_digest, finish_reason, model_id, response_digest,
                        input_tokens, output_tokens, total_tokens, latency_ms,
                        input_cost_cny, output_cost_cny, total_cost_cny, receipt_auth_tag)
                       VALUES (%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s);""",
                    (
                        analyst.attempt_id,
                        "c" * 64,
                        "stop",
                        "deepseek-v4-flash",
                        "b" * 64,
                        100,
                        50,
                        150,
                        Decimal("1.000"),
                        Decimal("0.000000"),
                        Decimal("0.000000"),
                        Decimal("0.000000"),
                        "d" * 64,
                    ),
                )
        connection.rollback()


def test_database_rejects_direct_attempt_with_profile_digest_not_bound_to_run(
    postgres_official_smoke_ledger_factory,
) -> None:
    """直写 Analyst attempt 也必须使用 run 冻结的 Profile 摘要，不能替换 Prompt/Schema 身份。"""

    manifest = _manifest()
    ledger = postgres_official_smoke_ledger_factory()
    ledger.ensure_run(manifest)
    claim = ledger.claim_case(manifest.case_ids[6])

    with psycopg.connect(**postgres_official_smoke_ledger_factory.settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            with pytest.raises(psycopg.errors.RaiseException, match="attempt profile digest conflicts"):
                cursor.execute(
                    """INSERT INTO phase16_official_smoke_dispatch_attempts
                       (attempt_id, run_id, case_id, claim_id, stage, profile_digest, internal_request_id)
                       VALUES (%s::uuid,%s,%s,%s::uuid,'ANALYST',%s,%s);""",
                    (
                        str(uuid4()),
                        manifest.run_id,
                        claim.case_id,
                        claim.claim_id,
                        "0" * 64,
                        _internal_request_id("forged-direct-analyst-profile"),
                    ),
                )
        connection.rollback()


def test_database_rejects_direct_claim_with_manifest_not_bound_to_run(
    postgres_official_smoke_ledger_factory,
) -> None:
    """case claim 必须继承 run 的同一 Manifest 摘要，不能用合法 slot 拼接另一份冻结资产。"""

    manifest = _manifest()
    ledger = postgres_official_smoke_ledger_factory()
    ledger.ensure_run(manifest)
    slot = ledger.list_case_slots()[7]

    with psycopg.connect(**postgres_official_smoke_ledger_factory.settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            with pytest.raises(psycopg.errors.RaiseException, match="claim manifest digest conflicts"):
                cursor.execute(
                    """INSERT INTO phase16_official_smoke_case_claims
                       (claim_id, run_id, case_id, manifest_digest, reserved_amount_cny)
                       VALUES (%s::uuid,%s,%s,%s,%s);""",
                    (
                        str(uuid4()),
                        manifest.run_id,
                        slot.case_id,
                        "f" * 64,
                        PHASE16_OFFICIAL_SMOKE_CASE_RESERVATION_CNY,
                    ),
                )
        connection.rollback()


def test_database_rejects_claim_when_run_has_not_imported_history_and_all_ten_slots(
    postgres_official_smoke_ledger_factory,
) -> None:
    """即使 run/slot 身份真实，缺少历史支出或其余九个 slot 时仍不能进入正式证据链。"""

    settings = postgres_official_smoke_ledger_factory.settings
    manifest = _manifest()
    first_case_id = manifest.case_ids[0]
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO phase16_official_smoke_runs
                   (run_id, manifest_digest, analyst_profile_digest, planner_profile_digest, total_budget_cny)
                   VALUES (%s,%s,%s,%s,%s);""",
                (
                    manifest.run_id,
                    manifest.manifest_digest,
                    manifest.profile_digests["analyst"],
                    manifest.profile_digests["planner"],
                    Decimal("1.000000"),
                ),
            )
            cursor.execute(
                """INSERT INTO phase16_official_smoke_case_slots
                   (run_id, slot_position, case_id, case_digest,
                    analyst_reservation_cny, planner_reservation_cny)
                   VALUES (%s,1,%s,%s,%s,%s);""",
                (
                    manifest.run_id,
                    first_case_id,
                    manifest.case_digests[first_case_id],
                    Decimal("0.040000"),
                    Decimal("0.052000"),
                ),
            )
            with pytest.raises(psycopg.errors.RaiseException, match="run initialization is incomplete"):
                cursor.execute(
                    """INSERT INTO phase16_official_smoke_case_claims
                       (claim_id, run_id, case_id, manifest_digest, reserved_amount_cny)
                       VALUES (%s::uuid,%s,%s,%s,%s);""",
                    (
                        str(uuid4()),
                        manifest.run_id,
                        first_case_id,
                        manifest.manifest_digest,
                        Decimal("0.092000"),
                    ),
                )
        connection.rollback()


def test_database_rejects_truncate_of_official_smoke_fact_chain(
    postgres_official_smoke_ledger_factory,
) -> None:
    """append-only 不止拒绝行级 UPDATE/DELETE，也必须阻止 TRUNCATE CASCADE 清空重启证据。"""

    ledger = postgres_official_smoke_ledger_factory()
    ledger.ensure_run(_manifest())

    with psycopg.connect(**postgres_official_smoke_ledger_factory.settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            with pytest.raises(psycopg.errors.RaiseException, match="facts cannot be truncated"):
                cursor.execute("TRUNCATE phase16_official_smoke_runs CASCADE;")
        connection.rollback()


def test_ledger_rejects_free_text_audit_values_and_hashes_provider_response_id(
    postgres_official_smoke_ledger_factory,
) -> None:
    """API Key/Prompt 类自由文本不能进入审计列，Provider ID 只保存 SHA-256 摘要。"""

    manifest = _manifest()
    ledger = postgres_official_smoke_ledger_factory()
    ledger.ensure_run(manifest)
    claim = ledger.claim_case(manifest.case_ids[8])

    with pytest.raises(Phase16OfficialSmokeLedgerError, match="dispatch identity"):
        ledger.begin_dispatch(
            claim_id=claim.claim_id,
            stage=Phase16OfficialSmokeDispatchStage.ANALYST,
            profile_digest=manifest.profile_digests["analyst"],
            internal_request_id="sk-live-secret and prompt body",
        )

    analyst = ledger.begin_dispatch(
        claim_id=claim.claim_id,
        stage=Phase16OfficialSmokeDispatchStage.ANALYST,
        profile_digest=manifest.profile_digests["analyst"],
        internal_request_id=_internal_request_id("safe-audit-analyst"),
    )
    with pytest.raises(Phase16OfficialSmokeLedgerError, match="provider receipt"):
        ledger.append_provider_receipt(
            attempt_id=analyst.attempt_id,
            provider_response_id="provider-response-with-untrusted-text",
            finish_reason="model body must not become a finish reason",
            model_id="deepseek-v4-flash",
            response_digest="a" * 64,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_ms=Decimal("1.000"),
        )

    provider_response_id = "provider-response-with-untrusted-text"
    receipt = ledger.append_provider_receipt(
        attempt_id=analyst.attempt_id,
        provider_response_id=provider_response_id,
        finish_reason="stop",
        model_id="deepseek-v4-flash",
        response_digest="b" * 64,
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        latency_ms=Decimal("1.000"),
    )
    with pytest.raises(Phase16OfficialSmokeLedgerError, match="validation fact"):
        ledger.append_validation_fact(
            attempt_id=analyst.attempt_id,
            verdict=Phase16OfficialSmokeValidationVerdict.PASS,
            reason_code="operator recommendation must not be stored",
            validation_digest="c" * 64,
        )

    ledger.append_validation_fact(
        attempt_id=analyst.attempt_id,
        verdict=Phase16OfficialSmokeValidationVerdict.PASS,
        reason_code="ANALYST_VALIDATED",
        validation_digest="d" * 64,
    )
    assert receipt.provider_response_id_digest == sha256(provider_response_id.encode("utf-8")).hexdigest()
    with psycopg.connect(**postgres_official_smoke_ledger_factory.settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT provider_response_id_digest
                     FROM phase16_official_smoke_provider_receipts
                    WHERE attempt_id=%s::uuid;""",
                (analyst.attempt_id,),
            )
            stored = cursor.fetchone()[0]
    assert stored == receipt.provider_response_id_digest
    assert provider_response_id not in stored


def test_database_rejects_forged_frozen_run_and_slot_identity(
    postgres_official_smoke_ledger_factory,
) -> None:
    """直写 SQL 也必须绑定唯一 Manifest、两份 Profile 和十个有序 case，不能拼接伪造 PASS 链。"""

    manifest = _manifest()
    settings = postgres_official_smoke_ledger_factory.settings

    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            with pytest.raises(psycopg.errors.RaiseException, match="frozen manifest"):
                cursor.execute(
                    """INSERT INTO phase16_official_smoke_runs
                       (run_id, manifest_digest, analyst_profile_digest, planner_profile_digest, total_budget_cny)
                       VALUES (%s,%s,%s,%s,%s);""",
                    (
                        manifest.run_id,
                        "1" * 64,
                        "2" * 64,
                        "3" * 64,
                        Decimal("1.000000"),
                    ),
                )
        connection.rollback()

    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO phase16_official_smoke_runs
                   (run_id, manifest_digest, analyst_profile_digest, planner_profile_digest, total_budget_cny)
                   VALUES (%s,%s,%s,%s,%s);""",
                (
                    manifest.run_id,
                    manifest.manifest_digest,
                    manifest.profile_digests["analyst"],
                    manifest.profile_digests["planner"],
                    Decimal("1.000000"),
                ),
            )
            with pytest.raises(psycopg.errors.RaiseException, match="frozen case slot"):
                cursor.execute(
                    """INSERT INTO phase16_official_smoke_case_slots
                       (run_id, slot_position, case_id, case_digest,
                        analyst_reservation_cny, planner_reservation_cny)
                       VALUES (%s,%s,%s,%s,%s,%s);""",
                    (
                        manifest.run_id,
                        1,
                        "phase16-forged-case",
                        "4" * 64,
                        Decimal("0.040000"),
                        Decimal("0.052000"),
                    ),
                )
        connection.rollback()


def test_ledger_rejects_duplicate_provider_response_id_across_attempts(
    postgres_official_smoke_ledger_factory,
) -> None:
    """二十次正式调用必须有二十个可区分的 Provider 回执，不能把一个响应复用于两段。"""

    manifest = _manifest()
    ledger = postgres_official_smoke_ledger_factory()
    ledger.ensure_run(manifest)
    claim = ledger.claim_case(manifest.case_ids[0])
    provider_response_id = "chatcmpl-formal-unique-response"

    analyst = ledger.begin_dispatch(
        claim_id=claim.claim_id,
        stage=Phase16OfficialSmokeDispatchStage.ANALYST,
        profile_digest=manifest.profile_digests["analyst"],
        internal_request_id=_internal_request_id("duplicate-receipt-analyst"),
    )
    ledger.append_provider_receipt(
        attempt_id=analyst.attempt_id,
        provider_response_id=provider_response_id,
        finish_reason="stop",
        model_id="deepseek-v4-flash",
        response_digest="a" * 64,
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        latency_ms=Decimal("1.000"),
    )
    ledger.append_validation_fact(
        attempt_id=analyst.attempt_id,
        verdict=Phase16OfficialSmokeValidationVerdict.PASS,
        reason_code="ANALYST_VALIDATED",
        validation_digest="b" * 64,
    )
    planner = ledger.begin_dispatch(
        claim_id=claim.claim_id,
        stage=Phase16OfficialSmokeDispatchStage.PLANNER,
        profile_digest=manifest.profile_digests["planner"],
        internal_request_id=_internal_request_id("duplicate-receipt-planner"),
    )

    with pytest.raises(Phase16OfficialSmokeLedgerError, match="provider response ID already belongs"):
        ledger.append_provider_receipt(
            attempt_id=planner.attempt_id,
            provider_response_id=provider_response_id,
            finish_reason="stop",
            model_id="deepseek-v4-flash",
            response_digest="c" * 64,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_ms=Decimal("1.000"),
        )


def test_restart_closes_case_after_existing_failed_validation_without_resend(
    postgres_official_smoke_ledger_factory,
) -> None:
    """失败验证已提交但 outcome 未提交时，重启必须补终态且仍拒绝同 stage 的第二次发送。"""

    manifest = _manifest()
    ledger = postgres_official_smoke_ledger_factory()
    ledger.ensure_run(manifest)
    claim = ledger.claim_case(manifest.case_ids[1])
    analyst = ledger.begin_dispatch(
        claim_id=claim.claim_id,
        stage=Phase16OfficialSmokeDispatchStage.ANALYST,
        profile_digest=manifest.profile_digests["analyst"],
        internal_request_id=_internal_request_id("failed-validation-before-restart"),
    )
    ledger.append_validation_fact(
        attempt_id=analyst.attempt_id,
        verdict=Phase16OfficialSmokeValidationVerdict.FAILED,
        reason_code="MODEL_RESPONSE_SCHEMA_FAILED",
        validation_digest="d" * 64,
    )

    restarted = postgres_official_smoke_ledger_factory()
    recovered = restarted.recover_open_attempts()

    assert len(recovered) == 1
    assert recovered[0].status is Phase16OfficialSmokeCaseOutcomeStatus.FAILED
    assert recovered[0].reason_code == "MODEL_RESPONSE_SCHEMA_FAILED"
    with pytest.raises(Phase16OfficialSmokeLedgerError, match="attempt already exists"):
        restarted.begin_dispatch(
            claim_id=claim.claim_id,
            stage=Phase16OfficialSmokeDispatchStage.ANALYST,
            profile_digest=manifest.profile_digests["analyst"],
            internal_request_id=_internal_request_id("failed-validation-resend"),
        )


def test_restart_closes_case_after_two_validated_receipts_without_resend(
    postgres_official_smoke_ledger_factory,
) -> None:
    """两个阶段均已验证但还未写 outcome 时，恢复只能从既有事实闭合 PASS，不能重新请求模型。"""

    manifest = _manifest()
    ledger = postgres_official_smoke_ledger_factory()
    ledger.ensure_run(manifest)
    claim = ledger.claim_case(manifest.case_ids[2])
    analyst = ledger.begin_dispatch(
        claim_id=claim.claim_id,
        stage=Phase16OfficialSmokeDispatchStage.ANALYST,
        profile_digest=manifest.profile_digests["analyst"],
        internal_request_id=_internal_request_id("completed-chain-analyst"),
    )
    ledger.append_provider_receipt(
        attempt_id=analyst.attempt_id,
        provider_response_id="chatcmpl-completed-chain-analyst",
        finish_reason="stop",
        model_id="deepseek-v4-flash",
        response_digest="e" * 64,
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        latency_ms=Decimal("1.000"),
    )
    ledger.append_validation_fact(
        attempt_id=analyst.attempt_id,
        verdict=Phase16OfficialSmokeValidationVerdict.PASS,
        reason_code="ANALYST_VALIDATED",
        validation_digest="f" * 64,
    )
    planner = ledger.begin_dispatch(
        claim_id=claim.claim_id,
        stage=Phase16OfficialSmokeDispatchStage.PLANNER,
        profile_digest=manifest.profile_digests["planner"],
        internal_request_id=_internal_request_id("completed-chain-planner"),
    )
    ledger.append_provider_receipt(
        attempt_id=planner.attempt_id,
        provider_response_id="chatcmpl-completed-chain-planner",
        finish_reason="stop",
        model_id="deepseek-v4-flash",
        response_digest="0" * 64,
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        latency_ms=Decimal("1.000"),
    )
    ledger.append_validation_fact(
        attempt_id=planner.attempt_id,
        verdict=Phase16OfficialSmokeValidationVerdict.PASS,
        reason_code="PLANNER_VALIDATED",
        validation_digest="1" * 64,
    )

    recovered = postgres_official_smoke_ledger_factory().recover_open_attempts()

    assert len(recovered) == 1
    assert recovered[0].status is Phase16OfficialSmokeCaseOutcomeStatus.PASS
    assert recovered[0].reason_code == "RECOVERED_VALIDATED_PASS"


def test_database_direct_pass_chain_without_receipt_authenticator_tag_is_not_formal_evidence(
    postgres_official_smoke_ledger_factory,
) -> None:
    """正确身份和格式不足以证明真实发送，只有受控 Runner 签发的 receipt 标签才能进入正式证据。"""

    manifest = _manifest()
    ledger = postgres_official_smoke_ledger_factory()
    ledger.ensure_run(manifest)
    claim = ledger.claim_case(manifest.case_ids[9])
    settings = postgres_official_smoke_ledger_factory.settings
    analyst_attempt_id = str(uuid4())
    planner_attempt_id = str(uuid4())

    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO phase16_official_smoke_dispatch_attempts
                   (attempt_id, run_id, case_id, claim_id, stage, profile_digest, internal_request_id)
                   VALUES (%s::uuid,%s,%s,%s::uuid,'ANALYST',%s,%s::uuid);""",
                (
                    analyst_attempt_id,
                    manifest.run_id,
                    claim.case_id,
                    claim.claim_id,
                    manifest.profile_digests["analyst"],
                    str(uuid4()),
                ),
            )
            cursor.execute(
                """INSERT INTO phase16_official_smoke_provider_receipts
                   (attempt_id, provider_response_id_digest, finish_reason, model_id, response_digest,
                    input_tokens, output_tokens, total_tokens, latency_ms,
                    input_cost_cny, output_cost_cny, total_cost_cny, receipt_auth_tag)
                   VALUES (%s::uuid,%s,'stop','deepseek-v4-flash',%s,1,1,2,1.000,
                           0.000001,0.000002,0.000003,%s);""",
                (analyst_attempt_id, "a" * 64, "b" * 64, "0" * 64),
            )
            cursor.execute(
                """INSERT INTO phase16_official_smoke_validation_facts
                   (attempt_id, verdict, reason_code, validation_digest)
                   VALUES (%s::uuid,'PASS','ANALYST_VALIDATED',%s);""",
                (analyst_attempt_id, "c" * 64),
            )
            cursor.execute(
                """INSERT INTO phase16_official_smoke_dispatch_attempts
                   (attempt_id, run_id, case_id, claim_id, stage, profile_digest, internal_request_id)
                   VALUES (%s::uuid,%s,%s,%s::uuid,'PLANNER',%s,%s::uuid);""",
                (
                    planner_attempt_id,
                    manifest.run_id,
                    claim.case_id,
                    claim.claim_id,
                    manifest.profile_digests["planner"],
                    str(uuid4()),
                ),
            )
            cursor.execute(
                """INSERT INTO phase16_official_smoke_provider_receipts
                   (attempt_id, provider_response_id_digest, finish_reason, model_id, response_digest,
                    input_tokens, output_tokens, total_tokens, latency_ms,
                    input_cost_cny, output_cost_cny, total_cost_cny, receipt_auth_tag)
                   VALUES (%s::uuid,%s,'stop','deepseek-v4-flash',%s,1,1,2,1.000,
                           0.000001,0.000002,0.000003,%s);""",
                (planner_attempt_id, "d" * 64, "e" * 64, "0" * 64),
            )
            cursor.execute(
                """INSERT INTO phase16_official_smoke_validation_facts
                   (attempt_id, verdict, reason_code, validation_digest)
                   VALUES (%s::uuid,'PASS','PLANNER_VALIDATED',%s);""",
                (planner_attempt_id, "f" * 64),
            )
            cursor.execute(
                """INSERT INTO phase16_official_smoke_case_outcomes
                   (run_id, case_id, claim_id, status, reason_code, outcome_digest)
                   VALUES (%s,%s,%s::uuid,'PASS','FORMAL_CASE_PASS',%s);""",
                (manifest.run_id, claim.case_id, claim.claim_id, "1" * 64),
            )
        connection.commit()

    with pytest.raises(Phase16OfficialSmokeLedgerError, match="receipt authenticity"):
        ledger.get_case_outcome(case_id=claim.case_id)


def test_recovery_and_close_case_reject_forged_authenticated_pass_chain(
    postgres_official_smoke_ledger_factory,
) -> None:
    """双 PASS validation 但 HMAC 伪造时，恢复和正常 close 都不能产出可消费的正式 PASS。"""

    manifest = _manifest()
    ledger = postgres_official_smoke_ledger_factory()
    ledger.ensure_run(manifest)
    claim = ledger.claim_case(manifest.case_ids[7])
    settings = postgres_official_smoke_ledger_factory.settings
    analyst_attempt_id = str(uuid4())
    planner_attempt_id = str(uuid4())

    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO phase16_official_smoke_dispatch_attempts
                   (attempt_id, run_id, case_id, claim_id, stage, profile_digest, internal_request_id)
                   VALUES (%s::uuid,%s,%s,%s::uuid,'ANALYST',%s,%s::uuid);""",
                (
                    analyst_attempt_id,
                    manifest.run_id,
                    claim.case_id,
                    claim.claim_id,
                    manifest.profile_digests["analyst"],
                    str(uuid4()),
                ),
            )
            cursor.execute(
                """INSERT INTO phase16_official_smoke_provider_receipts
                   (attempt_id, provider_response_id_digest, finish_reason, model_id, response_digest,
                    input_tokens, output_tokens, total_tokens, latency_ms,
                    input_cost_cny, output_cost_cny, total_cost_cny, receipt_auth_tag)
                   VALUES (%s::uuid,%s,'stop','deepseek-v4-flash',%s,1,1,2,1.000,
                           0.000001,0.000002,0.000003,%s);""",
                (analyst_attempt_id, "a" * 64, "b" * 64, "0" * 64),
            )
            cursor.execute(
                """INSERT INTO phase16_official_smoke_validation_facts
                   (attempt_id, verdict, reason_code, validation_digest)
                   VALUES (%s::uuid,'PASS','ANALYST_VALIDATED',%s);""",
                (analyst_attempt_id, "c" * 64),
            )
            cursor.execute(
                """INSERT INTO phase16_official_smoke_dispatch_attempts
                   (attempt_id, run_id, case_id, claim_id, stage, profile_digest, internal_request_id)
                   VALUES (%s::uuid,%s,%s,%s::uuid,'PLANNER',%s,%s::uuid);""",
                (
                    planner_attempt_id,
                    manifest.run_id,
                    claim.case_id,
                    claim.claim_id,
                    manifest.profile_digests["planner"],
                    str(uuid4()),
                ),
            )
            cursor.execute(
                """INSERT INTO phase16_official_smoke_provider_receipts
                   (attempt_id, provider_response_id_digest, finish_reason, model_id, response_digest,
                    input_tokens, output_tokens, total_tokens, latency_ms,
                    input_cost_cny, output_cost_cny, total_cost_cny, receipt_auth_tag)
                   VALUES (%s::uuid,%s,'stop','deepseek-v4-flash',%s,1,1,2,1.000,
                           0.000001,0.000002,0.000003,%s);""",
                (planner_attempt_id, "d" * 64, "e" * 64, "0" * 64),
            )
            cursor.execute(
                """INSERT INTO phase16_official_smoke_validation_facts
                   (attempt_id, verdict, reason_code, validation_digest)
                   VALUES (%s::uuid,'PASS','PLANNER_VALIDATED',%s);""",
                (planner_attempt_id, "f" * 64),
            )
        connection.commit()

    with pytest.raises(Phase16OfficialSmokeLedgerError, match="receipt authenticity"):
        postgres_official_smoke_ledger_factory().recover_open_attempts()
    with pytest.raises(Phase16OfficialSmokeLedgerError, match="receipt authenticity"):
        ledger.close_case(
            claim_id=claim.claim_id,
            status=Phase16OfficialSmokeCaseOutcomeStatus.PASS,
            reason_code="FORMAL_CASE_PASS",
        )


@pytest.mark.parametrize(
    ("weaken_sql", "label"),
    (
        (
            "ALTER TABLE phase16_official_smoke_provider_receipts "
            "DROP CONSTRAINT phase16_official_smoke_provider_receipts_model_id_check;",
            "receipt model check",
        ),
        (
            "ALTER TABLE phase16_official_smoke_case_outcomes "
            "DROP CONSTRAINT phase16_official_smoke_case_outcomes_run_id_claim_id_fkey;",
            "outcome lineage foreign key",
        ),
        (
            "DROP TRIGGER trg_phase16_official_smoke_runs_append_only "
            "ON phase16_official_smoke_runs;",
            "append-only trigger",
        ),
    ),
)
def test_ledger_rejects_weakened_existing_schema_contract(
    postgres_official_smoke_ledger_factory,
    weaken_sql: str,
    label: str,
) -> None:
    """任何关键 CHECK、lineage FK 或 append-only trigger 漂移都必须在正式 run 初始化前失败。"""

    ledger = postgres_official_smoke_ledger_factory()
    settings = postgres_official_smoke_ledger_factory.settings
    with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
        connection.execute(weaken_sql)
        connection.commit()

    with pytest.raises(Phase16OfficialSmokeLedgerError, match="schema contract"):
        ledger.ensure_run(_manifest())

    # 参数仅用于 pytest 失败输出的可读性，避免关键 schema 漂移被模糊的编号掩盖。
    assert label


def test_schema_initialization_fails_closed_for_legacy_text_internal_request_id() -> None:
    """既有旧表若把内部 request ID 设为自由文本，迁移必须报错而不是静默运行在弱约束上。"""

    base_kwargs = dict(get_settings().postgres_connection_kwargs)
    schema_name = f"phase16_official_smoke_legacy_{uuid4().hex}"
    with psycopg.connect(**base_kwargs) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}; ").format(sql.Identifier(schema_name)))
        connection.commit()
    settings = SimpleNamespace(
        postgres_connection_kwargs={**base_kwargs, "options": f"-c search_path={schema_name}"}
    )
    try:
        with psycopg.connect(**settings.postgres_connection_kwargs) as connection:
            connection.execute(
                """CREATE TABLE phase16_official_smoke_dispatch_attempts (
                       attempt_id UUID PRIMARY KEY,
                       run_id TEXT NOT NULL,
                       case_id TEXT NOT NULL,
                       claim_id UUID NOT NULL,
                       stage TEXT NOT NULL,
                       profile_digest TEXT NOT NULL,
                       internal_request_id TEXT NOT NULL,
                       created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                   );"""
            )
            connection.commit()

        with pytest.raises(psycopg.errors.RaiseException, match="schema contract"):
            initialize_phase16_official_smoke_ledger_schema(settings)
    finally:
        with psycopg.connect(**base_kwargs) as connection:
            connection.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE;").format(sql.Identifier(schema_name)))
            connection.commit()

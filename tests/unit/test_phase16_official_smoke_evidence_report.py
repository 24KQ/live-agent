"""Phase 16 正式真实模型 smoke 脱敏证据报告的离线契约。

本组测试只构造已经脱敏的 PostgreSQL 账本投影，不读取 `.env`、不连接数据库、
不创建模型适配器。它保证正式失败结论可以被如实写入报告，同时不会把 Prompt、模型
正文、思维链、原始 provider ID 或经营建议带入仓库文档。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

import scripts.render_phase16_official_smoke_evidence as evidence_report
from scripts.render_phase16_official_smoke_evidence import (
    OfficialSmokeCaseClaim,
    OfficialSmokeDispatchAttempt,
    OfficialSmokeEvidenceSnapshot,
    OfficialSmokeOutcome,
    OfficialSmokeReceipt,
    OfficialSmokeValidation,
    _formal_conclusion,
    _load_read_only_report_settings,
    _render_official_smoke_evidence_markdown,
    _verify_receipt_authenticity,
    _verify_pass_outcomes,
    _write_official_smoke_evidence_markdown,
    render_official_smoke_evidence_report,
)


def _failed_snapshot() -> OfficialSmokeEvidenceSnapshot:
    """构造首个 Analyst 已发送但验证失败的最小脱敏账本事实。"""

    return OfficialSmokeEvidenceSnapshot(
        run_id="phase16-official-smoke-v1",
        manifest_digest="d75b8dce67ac49e8cbb9c71388fc9e666703c7296f585eb9e3b792bd0abaeb7b",
        total_budget_cny=Decimal("1.000000"),
        historical_spend_cny=Decimal("0.073220"),
        fixed_case_slot_count=10,
        maximum_exposure_cny=Decimal("0.993220"),
        receipts=(
            OfficialSmokeReceipt(
                case_id="phase16-high-conflict-paired-development-001",
                stage="ANALYST",
                profile_digest="415b331477a55c58bd61e0d632ec3b74aa3137a5c30f8fd1344ab19fb2875bee",
                provider_response_id_digest="944d5b5959acd28393ba0132aca92f9846588d9359635e60565189adbb2b27bc",
                finish_reason="stop",
                model_id="deepseek-v4-flash",
                response_digest="df336bbd6bbd2ba4ea65ac4eb6f617d6159004220bd95a67603b5525de0b4b90",
                input_tokens=2610,
                output_tokens=1848,
                total_tokens=4458,
                latency_ms=Decimal("14138.545"),
                input_cost_cny=Decimal("0.002610"),
                output_cost_cny=Decimal("0.003696"),
                total_cost_cny=Decimal("0.006306"),
            ),
        ),
        validations=(
            OfficialSmokeValidation(
                case_id="phase16-high-conflict-paired-development-001",
                stage="ANALYST",
                verdict="FAILED",
                reason_code="ANALYST_VALIDATION_FAILED",
                validation_digest="41790eda4476eadf43a49877f5a673659b111ef724dbed7ef926b5b222e0e643",
            ),
        ),
        outcomes=(
            OfficialSmokeOutcome(
                case_id="phase16-high-conflict-paired-development-001",
                status="FAILED",
                reason_code="ANALYST_VALIDATION_FAILED",
                outcome_digest="4f8f8e2ddd230a82d11a59eac4b36c246b46abaa848c935ac8bb8cacf6db349b",
            ),
        ),
        claims=(
            OfficialSmokeCaseClaim(
                case_id="phase16-high-conflict-paired-development-001",
            ),
        ),
        attempts=(
            OfficialSmokeDispatchAttempt(
                case_id="phase16-high-conflict-paired-development-001",
                stage="ANALYST",
                profile_digest="415b331477a55c58bd61e0d632ec3b74aa3137a5c30f8fd1344ab19fb2875bee",
                has_provider_receipt=True,
                has_validation_fact=True,
            ),
        ),
    )


def _blocked_pre_send_snapshot() -> OfficialSmokeEvidenceSnapshot:
    """构造已写 Analyst intent 但 Provider 明确未发送的合法 BLOCKED 账本投影。"""

    case_id = "phase16-high-conflict-paired-development-001"
    return OfficialSmokeEvidenceSnapshot(
        run_id="phase16-official-smoke-v1",
        manifest_digest="d75b8dce67ac49e8cbb9c71388fc9e666703c7296f585eb9e3b792bd0abaeb7b",
        total_budget_cny=Decimal("1.000000"),
        historical_spend_cny=Decimal("0.073220"),
        fixed_case_slot_count=10,
        maximum_exposure_cny=Decimal("0.993220"),
        receipts=(),
        validations=(
            OfficialSmokeValidation(
                case_id=case_id,
                stage="ANALYST",
                verdict="BLOCKED",
                reason_code="MODEL_REQUEST_NOT_SENT",
                validation_digest="a" * 64,
            ),
        ),
        outcomes=(
            OfficialSmokeOutcome(
                case_id=case_id,
                status="BLOCKED",
                reason_code="MODEL_REQUEST_NOT_SENT",
                outcome_digest="b" * 64,
            ),
        ),
        claims=(OfficialSmokeCaseClaim(case_id=case_id),),
        attempts=(
            OfficialSmokeDispatchAttempt(
                case_id=case_id,
                stage="ANALYST",
                profile_digest="c" * 64,
                has_provider_receipt=False,
                has_validation_fact=True,
            ),
        ),
    )


def _complete_pass_snapshot(*, authenticated: bool) -> OfficialSmokeEvidenceSnapshot:
    """构造十个双阶段均已通过的合成投影，用于锁定 PASS 的认证前置条件。

    该夹具只证明报告器的纯事实归约，不连接 PostgreSQL，也不伪造真实 Provider 回执；
    ``authenticated`` 代表读路径是否已经调用正式账本的 HMAC 校验器。
    """

    case_ids = tuple(f"case-{position:02d}" for position in range(1, 11))
    stages = ("ANALYST", "PLANNER")
    receipts = tuple(
        OfficialSmokeReceipt(
            case_id=case_id,
            stage=stage,
            profile_digest="a" * 64,
            provider_response_id_digest="b" * 64,
            finish_reason="stop",
            model_id="deepseek-v4-flash",
            response_digest="c" * 64,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            latency_ms=Decimal("1.000"),
            input_cost_cny=Decimal("0.000001"),
            output_cost_cny=Decimal("0.000002"),
            total_cost_cny=Decimal("0.000003"),
        )
        for case_id in case_ids
        for stage in stages
    )
    validations = tuple(
        OfficialSmokeValidation(
            case_id=case_id,
            stage=stage,
            verdict="PASS",
            reason_code=f"{stage}_VALIDATED",
            validation_digest="d" * 64,
        )
        for case_id in case_ids
        for stage in stages
    )
    outcomes = tuple(
        OfficialSmokeOutcome(
            case_id=case_id,
            status="PASS",
            reason_code="FORMAL_CASE_PASS",
            outcome_digest="e" * 64,
        )
        for case_id in case_ids
    )
    return OfficialSmokeEvidenceSnapshot(
        run_id="phase16-official-smoke-v1",
        manifest_digest="f" * 64,
        total_budget_cny=Decimal("1.000000"),
        historical_spend_cny=Decimal("0.073220"),
        fixed_case_slot_count=10,
        maximum_exposure_cny=Decimal("0.993220"),
        receipts=receipts,
        validations=validations,
        outcomes=outcomes,
        claims=tuple(OfficialSmokeCaseClaim(case_id=case_id) for case_id in case_ids),
        attempts=tuple(
            OfficialSmokeDispatchAttempt(
                case_id=case_id,
                stage=stage,
                profile_digest="a" * 64,
                has_provider_receipt=True,
                has_validation_fact=True,
            )
            for case_id in case_ids
            for stage in stages
        ),
        authenticated_pass_case_ids=frozenset(case_ids) if authenticated else frozenset(),
    )


def test_rendered_report_preserves_failed_strict_run_without_sensitive_model_content() -> None:
    """已发送后首例失败必须如实渲染，且报告只能包含预先脱敏的证据字段。"""

    report = _render_official_smoke_evidence_markdown(_failed_snapshot())

    assert "- Formal evidence conclusion: `FAILED`" in report
    assert "- Completed cases / calls: `1 / 1`" in report
    assert "- Required cases / calls: `10 / 20`" in report
    assert "`ANALYST_VALIDATION_FAILED`" in report
    assert "- Current known actual spend: `0.079526 CNY`" in report
    assert "- Frozen maximum exposure: `0.993220 CNY`" in report
    assert "- Claimed / unclaimed fixed slots: `1 / 9`" in report
    assert "- Dispatch attempts Analyst / Planner: `1 / 0`" in report
    assert "- Production default route: `DETERMINISTIC_ONLY`" in report
    assert "- ScriptedModel baseline comparison: `NOT_COMPARABLE_AFTER_ANALYST_FAILURE`" in report
    # 文档可以解释脱敏边界，但不能出现任何可被误当成真实请求载荷的原始值。
    assert "provider_response_id_digest" not in report
    assert "sk-phase16-test-secret" not in report
    assert "raw-model-body-should-never-appear" not in report
    assert "raw-chain-of-thought-should-never-appear" not in report


def test_valid_pre_send_blocked_chain_is_inconclusive_not_failed() -> None:
    """没有 Provider receipt 的单阶段 BLOCKED 链必须保持 INCONCLUSIVE，不得被 PASS 拓扑误判。"""

    snapshot = _blocked_pre_send_snapshot()

    assert _formal_conclusion(snapshot) == "INCONCLUSIVE"
    assert "- Formal evidence conclusion: `INCONCLUSIVE`" in _render_official_smoke_evidence_markdown(
        snapshot
    )


def test_empty_partial_slot_snapshot_fails_closed_instead_of_claiming_pre_send_inconclusive() -> None:
    """没有 dispatch 事实不够：只有完整十个冻结 slot 的 run 才能表示合法的发送前阻断。"""

    snapshot = replace(
        _blocked_pre_send_snapshot(),
        fixed_case_slot_count=9,
        claims=(),
        attempts=(),
        receipts=(),
        validations=(),
        outcomes=(),
    )

    assert _formal_conclusion(snapshot) == "FAILED"


def test_pre_send_blocked_chain_rejects_cross_case_attempt_or_validation() -> None:
    """阻断链的每条事实都必须属于已 claim 的 case，跨 case 拼接不得降级为 INCONCLUSIVE。"""

    snapshot = _blocked_pre_send_snapshot()
    forged_attempt = replace(snapshot.attempts[0], case_id="another-fixed-case")
    forged_validation = replace(snapshot.validations[0], case_id="another-fixed-case")

    assert _formal_conclusion(replace(snapshot, attempts=(forged_attempt,))) == "FAILED"
    assert _formal_conclusion(replace(snapshot, validations=(forged_validation,))) == "FAILED"


def test_public_renderer_does_not_accept_a_caller_supplied_snapshot() -> None:
    """正式公开入口只能读认证账本，调用方无法把手工 receipt snapshot 作为参数传入。"""

    with pytest.raises(TypeError):
        render_official_smoke_evidence_report(_failed_snapshot())


def test_public_renderer_always_reads_through_the_authenticated_ledger_path(
    monkeypatch: Any,
) -> None:
    """正式公开入口只能接收连接与认证依赖，不能接收调用方自造的 snapshot。"""

    snapshot = _failed_snapshot()
    monkeypatch.setattr(
        evidence_report,
        "read_official_smoke_evidence",
        lambda _settings, *, receipt_authenticator: snapshot,
    )

    report = render_official_smoke_evidence_report(
        settings=object(),
        receipt_authenticator=object(),
    )

    assert report.status == "FAILED"
    assert report.run_id == snapshot.run_id
    assert "ANALYST_VALIDATION_FAILED" in report.markdown


def test_formal_pass_requires_authenticated_ledger_outcomes() -> None:
    """完整行数、状态和成本不足以构成 PASS，十个 case 都必须经过账本 HMAC 消费路径。"""

    assert _formal_conclusion(_complete_pass_snapshot(authenticated=False)) == "FAILED"
    assert _formal_conclusion(_complete_pass_snapshot(authenticated=True)) == "PASS"


def test_pass_outcome_verification_uses_the_ledger_public_authentication_path(
    monkeypatch: Any,
) -> None:
    """报告器不得自行把 SQL 的 PASS 行当作可信结论，必须调用正式账本的公开复验入口。"""

    verified_case_ids: list[str] = []

    class _FakeLedger:
        """最小替身只记录公开 API 调用，不提供任何私有 HMAC 实现细节。"""

        def __init__(self, settings: object, *, receipt_authenticator: object) -> None:
            assert settings is not None
            assert receipt_authenticator == "authenticator"

        def verify_case_outcome_receipts(self, *, case_id: str) -> SimpleNamespace:
            verified_case_ids.append(case_id)
            return SimpleNamespace(case_id=case_id, status="PASS")

    monkeypatch.setattr(
        evidence_report,
        "PostgresPhase16OfficialSmokeLedger",
        _FakeLedger,
    )

    verified = _verify_pass_outcomes(
        settings=object(),
        receipt_authenticator="authenticator",
        outcomes=(
            OfficialSmokeOutcome(
                case_id="case-01",
                status="PASS",
                reason_code="FORMAL_CASE_PASS",
                outcome_digest="a" * 64,
            ),
        ),
    )

    assert verified == frozenset({"case-01"})
    assert verified_case_ids == ["case-01"]


def test_receipt_authenticity_rejects_any_untrusted_sent_fact() -> None:
    """失败结论也必须绑定受控发送事实，不能让直写 SQL 伪造一条看似合理的失败 receipt。"""

    class _RejectingAuthenticator:
        """最小认证替身稳定拒绝标签，用于锁定 fail-closed 行为。"""

        def verify(self, **_kwargs: object) -> bool:
            return False

    receipt_row = {
        "attempt_id": "00000000-0000-0000-0000-000000000001",
        "stage": "ANALYST",
        "profile_digest": "a" * 64,
        "provider_response_id_digest": "b" * 64,
        "finish_reason": "stop",
        "model_id": "deepseek-v4-flash",
        "response_digest": "c" * 64,
        "input_tokens": 1,
        "output_tokens": 1,
        "total_tokens": 2,
        "latency_ms": Decimal("1.000"),
        "input_cost_cny": Decimal("0.000001"),
        "output_cost_cny": Decimal("0.000002"),
        "total_cost_cny": Decimal("0.000003"),
        "receipt_auth_tag": "d" * 64,
    }

    with pytest.raises(ValueError, match="receipt authenticity verification failed"):
        _verify_receipt_authenticity(
            receipt_rows=(receipt_row,),
            receipt_authenticator=_RejectingAuthenticator(),
        )


def test_receipt_authenticity_normalizes_psycopg_uuid_attempt_ids() -> None:
    """psycopg 会将 UUID 列还原为 UUID 对象，认证前必须规范成账本签名使用的字符串。"""

    observed_attempt_ids: list[str] = []

    class _AcceptingAuthenticator:
        """最小替身记录认证器收到的 attempt 身份，避免测试依赖真实 HMAC key。"""

        def verify(self, **kwargs: object) -> bool:
            observed_attempt_ids.append(str(kwargs["attempt_id"]))
            return True

    receipt_row = {
        "attempt_id": UUID("00000000-0000-0000-0000-000000000001"),
        "stage": "ANALYST",
        "profile_digest": "a" * 64,
        "provider_response_id_digest": "b" * 64,
        "finish_reason": "stop",
        "model_id": "deepseek-v4-flash",
        "response_digest": "c" * 64,
        "input_tokens": 1,
        "output_tokens": 1,
        "total_tokens": 2,
        "latency_ms": Decimal("1.000"),
        "input_cost_cny": Decimal("0.000001"),
        "output_cost_cny": Decimal("0.000002"),
        "total_cost_cny": Decimal("0.000003"),
        "receipt_auth_tag": "d" * 64,
    }

    _verify_receipt_authenticity(
        receipt_rows=(receipt_row,),
        receipt_authenticator=_AcceptingAuthenticator(),
    )

    assert observed_attempt_ids == ["00000000-0000-0000-0000-000000000001"]


def test_read_only_report_settings_only_accept_allowlisted_dotenv_values(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """报告器只消费数据库与 receipt HMAC 配置，绝不把 LLM API Key 装配进运行配置。"""

    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text(
        "POSTGRES_HOST=readonly-db\n"
        "POSTGRES_PORT=5544\n"
        "POSTGRES_DB=smoke\n"
        "POSTGRES_USER=reporter\n"
        "POSTGRES_PASSWORD=database-secret\n"
        "PHASE16_OFFICIAL_SMOKE_RECEIPT_HMAC_HEX=" + "ab" * 32 + "\n"
        "LLM_API_KEY=must-not-be-loaded\n",
        encoding="utf-8",
        newline="\n",
    )
    # PR workflow 会在 job 级注入真实 PostgreSQL 容器地址；本用例要验证的是临时
    # `.env` 白名单解析，因此必须清除全部允许的环境覆盖，不能依赖开发机恰好未设置。
    for environment_name in (
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "PHASE16_OFFICIAL_SMOKE_RECEIPT_HMAC_HEX",
    ):
        monkeypatch.delenv(environment_name, raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    settings, authenticator = _load_read_only_report_settings(dotenv_path=dotenv_path)

    assert settings.postgres_connection_kwargs["host"] == "readonly-db"
    assert settings.postgres_connection_kwargs["port"] == 5544
    assert settings.postgres_connection_kwargs["options"] == "-c default_transaction_read_only=on"
    assert authenticator is not None
    assert "must-not-be-loaded" not in repr(settings)
    assert "LLM_API_KEY" not in os.environ


def test_report_cli_can_parse_help_when_invoked_as_a_script() -> None:
    """直接执行脚本时必须先建立仓库导入根，不能在 argparse 之前因 ``src`` 导入失败。"""

    project_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, "scripts/render_phase16_official_smoke_evidence.py", "--help"],
        cwd=project_root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr
    assert "Render the sanitized Phase 16 formal smoke evidence" in result.stdout


def test_write_report_uses_utf8_lf_and_does_not_change_the_snapshot(tmp_path: Path) -> None:
    """内部格式化结果写入文档必须稳定使用 UTF-8/LF，且不能改写不可变账本投影。"""

    snapshot = _failed_snapshot()
    markdown = _render_official_smoke_evidence_markdown(snapshot)
    output = _write_official_smoke_evidence_markdown(tmp_path, markdown)

    assert output.name == "phase-16-official-smoke-evidence.md"
    assert output.read_text(encoding="utf-8") == markdown
    assert b"\r\n" not in output.read_bytes()

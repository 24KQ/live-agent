"""Phase 16 V5 受控 E2E Runner 的纯离线契约测试。

本模块只经过公开的 V5 Profile、Manifest、Runner 和账本协议运行。所有模型端口均为确定性
Fake，不读取 ``.env``、不连接 PostgreSQL，也绝不向 DeepSeek 发送请求。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

import pytest

import src.decision_support.controlled_e2e_v5 as controlled_e2e_v5
from src.decision_support.controlled_e2e_ledger_v5 import (
    Phase16V5CaseClaim,
    Phase16V5CaseOutcomeStatus,
    Phase16V5DispatchAttempt,
    Phase16V5DispatchStage,
    Phase16V5RunKind,
    PostgresPhase16V5CampaignLedger,
)
from src.decision_support.controlled_e2e_v5 import (
    PHASE16_V5_ANALYST_PROFILE_ID,
    PHASE16_V5_DEADLINE_SECONDS,
    PHASE16_V5_MAX_OUTPUT_TOKENS,
    PHASE16_V5_MAX_TOTAL_TOKENS,
    PHASE16_V5_PLANNER_PROFILE_ID,
    PHASE16_V5_STAGE_RESERVATION_CNY,
    Phase16V5ControlledE2ERunner,
    Phase16V5ExecutionStatus,
    Phase16V5Manifest,
    _NoSkillPort,
    _StageExecution,
    _V5BudgetAdapter,
    _V5PricingPolicy,
    build_phase16_v5_analyst_profile,
    build_phase16_v5_calibration_projection,
    build_phase16_v5_manifest,
    build_phase16_v5_planner_profile,
    load_phase16_v5_manifest,
    load_phase16_v5_parent_dataset,
    preflight_phase16_v5,
)
from src.decision_support.official_smoke_evidence_v2 import (
    build_phase16_smoke_evidence_v2_analyst_profile,
)
from src.decision_support.controlled_e2e_adapter_v5 import (
    DeepSeekV5ControlledE2EAdapter,
    DeepSeekV5ThinkingMode,
)
from src.specialist_runtime.model_port import (
    ModelFailure,
    ModelFailureCategory,
    ModelMessage,
    ModelRequest,
    ModelSuccess,
    ModelUsage,
)
from src.specialist_runtime.models import canonical_json_sha256
from src.specialist_runtime.runner import BoundedSpecialistRunner
from src.specialist_runtime.deepseek_adapter import AsyncHttpResponse


_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class _RecordingLedger:
    """最小追加型账本 Fake，记录 Runner 的公开调用顺序而不模拟 PostgreSQL 实现。"""

    def __init__(self) -> None:
        """初始化空的事实记录；校准 run 不依赖已有校准 PASS。"""

        # 该开关只模拟 PostgreSQL 已认证的校准事实；正式路径仍由专门的集成测试验证。
        self.calibration_is_passed = False
        self.receipt_complete = True
        self.claims: list[Phase16V5CaseClaim] = []
        self.attempts: list[Phase16V5DispatchAttempt] = []
        self.receipts: list[dict[str, object]] = []
        self.validations: list[dict[str, object]] = []
        self.case_outcomes: list[dict[str, object]] = []
        self.run_outcomes: list[dict[str, object]] = []

    def ensure_campaign(self, _manifest: object) -> None:
        """Fake 接受已由 Runner 预检过的冻结 Manifest。"""

    def begin_run(self, **_kwargs: object) -> None:
        """Fake 不复制数据库的 slot 持久化，只验证 Runner 走到公开初始化边界。"""

    def recover_open_attempts(self) -> tuple[object, ...]:
        """本测试从全新账本开始，不存在需要按未知外部结果封口的历史 intent。"""

        return ()

    def recover_incomplete_cases(self) -> tuple[object, ...]:
        """本测试没有进程崩溃遗留的 claim，因此不向 Runner 注入恢复终态。"""

        return ()

    def calibration_passed(self) -> bool:
        """该测试执行校准路径，因此返回值不会放宽正式 run 的真实数据库门禁。"""

        return self.calibration_is_passed

    def claim_case(self, *, run_id: str, case_id: str, case_digest: str) -> Phase16V5CaseClaim:
        """为当前冻结 case 生成确定性 claim，防止测试通过自由随机身份掩盖串案错误。"""

        assert len(case_digest) == 64
        claim = Phase16V5CaseClaim(
            claim_id=str(uuid5(NAMESPACE_URL, f"v5-unit-claim:{run_id}:{case_id}")),
            run_id=run_id,
            case_id=case_id,
        )
        self.claims.append(claim)
        return claim

    def begin_dispatch(self, **kwargs: object) -> Phase16V5DispatchAttempt:
        """记录网络前 intent；返回值与 PostgreSQL 账本的公开 attempt 值对象一致。"""

        run_id = str(kwargs["run_id"])
        claim_id = str(kwargs["claim_id"])
        stage = kwargs["stage"]
        assert isinstance(stage, Phase16V5DispatchStage)
        attempt = Phase16V5DispatchAttempt(
            attempt_id=str(uuid5(NAMESPACE_URL, f"v5-unit-attempt:{claim_id}:{stage.value}")),
            run_id=run_id,
            claim_id=claim_id,
            stage=stage,
            internal_request_id=str(kwargs["internal_request_id"]),
            reservation_cny=kwargs["reservation_cny"],  # type: ignore[arg-type]
        )
        self.attempts.append(attempt)
        return attempt

    def append_receipt(self, **kwargs: object) -> bool:
        """记录脱敏 receipt 入口；返回值可模拟 Provider 回执不完整的严格失败路径。"""

        self.receipts.append(dict(kwargs))
        return self.receipt_complete

    def append_validation(self, **kwargs: object) -> None:
        """保留脱敏验证结论，便于断言失败不会被误报为未发送阻断。"""

        self.validations.append(dict(kwargs))

    def close_case(self, **kwargs: object) -> None:
        """记录唯一 case 终态，模拟 append-only 账本对 Runner 可见的边界。"""

        self.case_outcomes.append(dict(kwargs))

    def close_run(self, **kwargs: object) -> None:
        """记录唯一 run 终态，验证 Runner 在首个发送失败后立即停止。"""

        self.run_outcomes.append(dict(kwargs))


class _SentFailurePort:
    """模拟已发出但网络层没有可用模型结果的单次调用，不访问任何外部服务。"""

    thinking_mode = DeepSeekV5ThinkingMode.DISABLED

    def __init__(self) -> None:
        """记录共享 Runner 真正尝试构造的请求数量。"""

        self.requests: list[object] = []

    async def complete(self, request: object) -> ModelFailure:
        """返回 ``request_sent=True``，证明后续必须 FAILED 而不是可重试或 BLOCKED。"""

        self.requests.append(request)
        return ModelFailure(
            request_id=request.request_id,  # type: ignore[attr-defined]
            category=ModelFailureCategory.TRANSPORT_ERROR,
            request_sent=True,
            response_digest=None,
            http_status=None,
            retry_after_seconds=None,
        )


class _UnsentFailurePort:
    """模拟请求在 Provider 接收前失败，验证 V5 不把本地阻断误记成外部调用失败。"""

    thinking_mode = DeepSeekV5ThinkingMode.DISABLED

    async def complete(self, request: object) -> ModelFailure:
        """返回未发送的模型失败，保留共享 Runner 的正常预算释放路径。"""

        return ModelFailure(
            request_id=request.request_id,  # type: ignore[attr-defined]
            category=ModelFailureCategory.TRANSPORT_ERROR,
            request_sent=False,
            response_digest=None,
            http_status=None,
            retry_after_seconds=None,
        )


class _ValidV5Port:
    """按共享 Runner 公开上下文构造合法 FINAL，用于离线覆盖 V5 的成功与回执分支。"""

    thinking_mode = DeepSeekV5ThinkingMode.DISABLED

    def __init__(self, *, invalid_analyst_evidence: bool = False) -> None:
        """可选伪造未知证据 ID，专门检验语义验证不能因 JSON 合法而放行。"""

        self._invalid_analyst_evidence = invalid_analyst_evidence
        self.requests: list[object] = []

    async def complete(self, request: object) -> ModelSuccess:
        """只读取共享 Runner 已解析的受控 ID，绝不加载环境变量或发送网络请求。"""

        self.requests.append(request)
        context = json.loads(request.messages[-1].content)  # type: ignore[attr-defined]
        evidence_ids = [item["evidence_id"] for item in context["resolved_evidence"]]
        if self._invalid_analyst_evidence and "trigger_codes" in context["input_snapshot"]:
            evidence_ids = ["forged-v5-evidence"]
        if "trigger_codes" in context["input_snapshot"]:
            final_output = {
                "constraint_codes": [],
                "risk_codes": ["HUMAN_CONFIRMATION_REQUIRED"],
                "explanation": "离线契约验证要求人工确认。",
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
            "reason_summary": "V5_UNIT_CONTRACT",
        }
        return ModelSuccess(
            request_id=request.request_id,  # type: ignore[attr-defined]
            model_id="deepseek-v4-pro",
            output=output,
            usage=ModelUsage(input_tokens=100, output_tokens=100, total_tokens=200),
            provider_response_id=f"phase16-v5-unit-{len(self.requests):03d}",
            finish_reason="stop",
            response_digest=canonical_json_sha256(output),
            latency_ms=Decimal("2.000"),
        )


class _AnalysisStub:
    """仅模拟已经通过 Analyst 语义验证后的公开分析接口，避免 Planner 测试借用原始模型正文。"""

    risk_codes: tuple[object, ...] = ()

    def as_model_input(self) -> dict[str, object]:
        """提供 Planner 任务构造所需的最小受控投影。"""

        return {"constraint_codes": [], "risk_codes": []}


class _V5AdapterTransport:
    """记录 V5 专属 Adapter 的出站 payload，并返回固定 JSON，测试不产生网络请求。"""

    def __init__(self) -> None:
        """初始化空请求记录；响应仅含无业务含义的 Provider 协议字段。"""

        self.payloads: list[dict[str, Any]] = []

    async def post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout_seconds: float,
    ) -> AsyncHttpResponse:
        """保留待断言的 payload 副本，绝不记录 Authorization 头或访问外部端点。"""

        _ = (url, headers, timeout_seconds)
        self.payloads.append(dict(payload))
        return AsyncHttpResponse(
            status_code=200,
            headers={},
            body=json.dumps(
                {
                    "id": "v5-adapter-unit",
                    "model": "deepseek-v4-pro",
                    "choices": [{"message": {"content": '{"status":"ok"}'}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
                }
            ).encode("utf-8"),
        )


def test_v5_profiles_are_isolated_from_v2_and_freeze_controlled_e2e_limits() -> None:
    """V5 必须是新身份，且只允许禁思考 E2E 所需的零 Skill、单调用受限 Profile。"""

    analyst = build_phase16_v5_analyst_profile()
    planner = build_phase16_v5_planner_profile()
    v2_analyst = build_phase16_smoke_evidence_v2_analyst_profile()

    assert analyst.profile_id == PHASE16_V5_ANALYST_PROFILE_ID
    assert planner.profile_id == PHASE16_V5_PLANNER_PROFILE_ID
    assert analyst.profile_digest != v2_analyst.profile_digest
    assert {
        (profile.deadline_seconds, profile.max_total_tokens, profile.max_output_tokens)
        for profile in (analyst, planner)
    } == {
        (
            PHASE16_V5_DEADLINE_SECONDS,
            PHASE16_V5_MAX_TOTAL_TOKENS,
            PHASE16_V5_MAX_OUTPUT_TOKENS,
        )
    }
    assert all(
        profile.allowed_skill_ids == () and profile.max_model_calls == 1
        for profile in (analyst, planner)
    )
    assert "finding_codes" not in analyst.result_schema["properties"]
    assert "evidence_refs" not in analyst.result_schema["properties"]


def test_v5_adapter_is_independent_and_forces_disabled_thinking() -> None:
    """V5 专属 Adapter 必须不经 V4 运行路径，并为每个共享请求固定 thinking.disabled。"""

    transport = _V5AdapterTransport()
    adapter = DeepSeekV5ControlledE2EAdapter(
        api_key="test-secret",
        transport=transport,
        clock=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
        monotonic=lambda: 1.0,
    )
    request = ModelRequest(
        request_id=str(uuid5(NAMESPACE_URL, "v5-adapter-request")),
        endpoint_host="api.deepseek.com",
        model_id="deepseek-v4-pro",
        temperature=Decimal("0"),
        prompt_hash="a" * 64,
        result_schema_hash="b" * 64,
        messages=(ModelMessage(role="user", content="Return JSON."),),
        max_output_tokens=64,
        deadline_at=datetime(2026, 7, 18, 12, 1, tzinfo=timezone.utc),
    )

    outcome = asyncio.run(adapter.complete(request))

    assert isinstance(outcome, ModelSuccess)
    assert adapter.thinking_mode is DeepSeekV5ThinkingMode.DISABLED
    assert transport.payloads[0]["thinking"] == {"type": "disabled"}


def test_v5_preflight_rebuilds_the_versioned_manifest_without_environment_or_network() -> None:
    """本地预检必须从源码和冻结父数据重建同一身份，不能依赖 ``.env`` 或数据库。"""

    dataset = load_phase16_v5_parent_dataset(repository_root=_PROJECT_ROOT)
    rebuilt = build_phase16_v5_manifest(repository_root=_PROJECT_ROOT, dataset=dataset)
    stored = load_phase16_v5_manifest(repository_root=_PROJECT_ROOT)
    manifest, reasons = preflight_phase16_v5(repository_root=_PROJECT_ROOT)

    assert rebuilt.manifest_digest == stored.manifest_digest
    assert manifest == stored
    assert reasons == ()


def test_v5_preflight_blocks_a_tampered_synthetic_calibration_digest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """校准输入的摘要一旦被替换，预检必须在联网前拒绝重建 campaign。"""

    payload = json.loads(
        (_PROJECT_ROOT / "evaluation/manifests/phase16-v5-controlled-e2e-calibration-v1.json").read_text(
            encoding="utf-8"
        )
    )
    # 仅破坏已冻结的 case 摘要，保持其余合成事实原样，精确验证加载器的防篡改边界。
    payload["case_digest"] = "0" * 64
    tampered_input = tmp_path / "tampered-calibration.json"
    tampered_input.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    monkeypatch.setattr(
        controlled_e2e_v5,
        "PHASE16_V5_CALIBRATION_INPUT_PATH",
        tampered_input,
    )

    manifest, reasons = preflight_phase16_v5(repository_root=_PROJECT_ROOT)

    assert manifest is None
    assert reasons == ("MANIFEST_REBUILD_FAILED",)


def test_v5_calibration_projection_is_disjoint_from_all_formal_slots() -> None:
    """合成校准只验证协议链路，绝不能复用十个正式 case 的身份或证据投影。"""

    dataset = load_phase16_v5_parent_dataset(repository_root=_PROJECT_ROOT)
    calibration = build_phase16_v5_calibration_projection(
        repository_root=_PROJECT_ROOT,
        now=datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
    )
    runner = Phase16V5ControlledE2ERunner(
        dataset=dataset,
        manifest=load_phase16_v5_manifest(repository_root=_PROJECT_ROOT),
        ledger=_RecordingLedger(),
        model_port=_ValidV5Port(),
        clock=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
    )
    formal = runner._projections(run_kind=Phase16V5RunKind.FORMAL)

    assert calibration.case_id not in dataset.manifest.smoke_eligible_case_ids
    assert all(
        calibration.case_digest != formal_projection.case_digest
        and calibration.evidence_bundle_digest != formal_projection.evidence_bundle_digest
        and calibration.analyst_task.task_id != formal_projection.analyst_task.task_id
        for _, _, formal_projection in formal
    )


def test_v5_manifest_rejects_a_profile_digest_changed_without_resigning_identity() -> None:
    """攻击者不能替换 Profile 摘要后复用旧 Manifest 摘要取得发送资格。"""

    stored = load_phase16_v5_manifest(repository_root=_PROJECT_ROOT)
    tampered = stored.model_dump(mode="json")
    tampered["profile_digests"]["analyst"] = "0" * 64

    with pytest.raises(ValueError, match="manifest_digest"):
        Phase16V5Manifest.model_validate(tampered)


def test_v5_sent_analyst_failure_prevents_planner_and_closes_calibration() -> None:
    """已发送的 Analyst 失败必须立即结束校准，Planner 既不能发送也不能被伪报为成功。"""

    dataset = load_phase16_v5_parent_dataset(repository_root=_PROJECT_ROOT)
    manifest = load_phase16_v5_manifest(repository_root=_PROJECT_ROOT)
    ledger = _RecordingLedger()
    port = _SentFailurePort()
    runner = Phase16V5ControlledE2ERunner(
        dataset=dataset,
        manifest=manifest,
        ledger=ledger,
        model_port=port,
        # 父数据的证据有效期绑定在 2026-07-18，测试使用同一冻结参考时间而非当前墙钟。
        clock=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
    )

    report = asyncio.run(runner.execute(run_kind=Phase16V5RunKind.CALIBRATION))

    assert report.status is Phase16V5ExecutionStatus.FAILED
    assert report.reason_codes == ("MODEL_OUTCOME_UNAVAILABLE",)
    assert report.model_calls == len(port.requests) == 1
    assert len(ledger.attempts) == 1
    assert ledger.attempts[0].stage is Phase16V5DispatchStage.ANALYST
    assert ledger.validations[0]["reason_code"] == "MODEL_OUTCOME_UNAVAILABLE"
    assert ledger.case_outcomes[0]["reason_code"] == "MODEL_OUTCOME_UNAVAILABLE"
    assert len(ledger.run_outcomes) == 1


@pytest.mark.parametrize(
    ("field", "replacement", "message"),
    [
        ("model_id", "another-model", "identity is frozen"),
        ("formal_case_ids", ("duplicate",) * 10, "slots must be unique"),
        ("formal_case_digests", {}, "digests must exactly cover"),
        ("profile_digests", {"analyst": "0" * 64}, "requires analyst and planner"),
        ("input_cny_per_million", "0", "budget or price facts are frozen"),
        ("source_file_digests", {}, "source closure is incomplete"),
    ],
)
def test_v5_manifest_rejects_each_frozen_campaign_identity_surface(
    field: str,
    replacement: object,
    message: str,
) -> None:
    """任何可公开的模型、slot、价格或源码闭包变更都必须在发送前被 Manifest 拒绝。"""

    tampered = load_phase16_v5_manifest(repository_root=_PROJECT_ROOT).model_dump(mode="json")
    tampered[field] = replacement
    # 使用形状合法但事实不匹配的摘要，使模型级检查能先到达冻结协议验证器。
    tampered["manifest_digest"] = "0" * 64

    with pytest.raises(ValueError, match=message):
        Phase16V5Manifest.model_validate(tampered)


def _runner(*, ledger: _RecordingLedger, model_port: object) -> Phase16V5ControlledE2ERunner:
    """组装固定时钟的离线 V5 Runner，避免墙钟导致父数据证据在测试中自然过期。"""

    return Phase16V5ControlledE2ERunner(
        dataset=load_phase16_v5_parent_dataset(repository_root=_PROJECT_ROOT),
        manifest=load_phase16_v5_manifest(repository_root=_PROJECT_ROOT),
        ledger=ledger,
        model_port=cast(object, model_port),
        clock=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
    )


def test_v5_runner_blocks_bad_static_identity_thinking_and_missing_calibration() -> None:
    """本地身份、禁思考和正式校准门均须在首个账本 intent 前阻断。"""

    dataset = load_phase16_v5_parent_dataset(repository_root=_PROJECT_ROOT)
    manifest = load_phase16_v5_manifest(repository_root=_PROJECT_ROOT)
    # dry-run 只读取 formal_case_ids；用极小只读替身精确触发调用方不可覆盖的静态门禁。
    identity_mismatch = SimpleNamespace(formal_case_ids=("wrong-slot",))
    blocked = Phase16V5ControlledE2ERunner(
        dataset=dataset,
        manifest=identity_mismatch,
        ledger=_RecordingLedger(),
        model_port=_ValidV5Port(),
        clock=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
    )
    assert blocked.dry_run(run_kind=Phase16V5RunKind.CALIBRATION).reason_codes == (
        "FORMAL_SLOT_IDENTITY_MISMATCH",
    )
    valid_runner = _runner(ledger=_RecordingLedger(), model_port=_ValidV5Port())
    assert valid_runner.dry_run(run_kind=cast(Phase16V5RunKind, "INVALID")).reason_codes == (
        "RUN_KIND_INVALID",
    )

    wrong_thinking = _ValidV5Port()
    wrong_thinking.thinking_mode = "enabled"
    report = asyncio.run(_runner(ledger=_RecordingLedger(), model_port=wrong_thinking).execute(run_kind=Phase16V5RunKind.CALIBRATION))
    assert report.status is Phase16V5ExecutionStatus.BLOCKED
    assert report.reason_codes == ("THINKING_MODE_MISMATCH",)

    formal_report = asyncio.run(_runner(ledger=_RecordingLedger(), model_port=_ValidV5Port()).execute(run_kind=Phase16V5RunKind.FORMAL))
    assert formal_report.reason_codes == ("CALIBRATION_PASS_REQUIRED",)
    assert formal_report.model_calls == 0


def test_v5_runner_preserves_recovery_terminal_and_never_resends() -> None:
    """恢复到当前 run 的历史终态必须优先返回，不能被新的校准或发送覆盖。"""

    class _RecoveredLedger(_RecordingLedger):
        """仅返回当前校准 run 的恢复事实，模拟进程在前一次执行后重启。"""

        def recover_open_attempts(self) -> tuple[object, ...]:
            return (
                SimpleNamespace(
                    run_id="phase16-v5-calibration-001",
                    status=Phase16V5CaseOutcomeStatus.FAILED,
                    reason_code="UNKNOWN_ATTEMPT_AFTER_RESTART",
                ),
            )

    port = _ValidV5Port()
    report = asyncio.run(_runner(ledger=_RecoveredLedger(), model_port=port).execute(run_kind=Phase16V5RunKind.CALIBRATION))

    assert report.status is Phase16V5ExecutionStatus.FAILED
    assert report.reason_codes == ("UNKNOWN_ATTEMPT_AFTER_RESTART",)
    assert report.model_calls == len(port.requests) == 0


def test_v5_runner_formal_success_uses_exactly_twenty_stage_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正式 PASS 只能来自十个固定 slot 的 Analyst、Planner 各一次，且结论为受控 E2E 合格。"""

    ledger = _RecordingLedger()
    ledger.calibration_is_passed = True
    runner = _runner(ledger=ledger, model_port=_ValidV5Port())
    calls: list[Phase16V5DispatchStage] = []

    async def _stage(**kwargs: object) -> _StageExecution:
        """隔离编排循环本身；共享 Runner 的真实协议路径由下方和 PostgreSQL 测试覆盖。"""

        stage = cast(Phase16V5DispatchStage, kwargs["stage"])
        calls.append(stage)
        if stage is Phase16V5DispatchStage.ANALYST:
            return _StageExecution(True, True, "ANALYST_VALIDATION_PASS", "analyst-attempt", _AnalysisStub())
        return _StageExecution(True, True, "PLANNER_VALIDATION_PASS", "planner-attempt")

    monkeypatch.setattr(runner, "_execute_stage", _stage)
    report = asyncio.run(runner.execute(run_kind=Phase16V5RunKind.FORMAL))

    assert report.status is Phase16V5ExecutionStatus.PASS
    assert report.evidence_conclusion.value == "CONTROLLED_E2E_QUALIFIED"
    assert report.model_calls == len(calls) == 20
    assert len(report.case_executions) == len(ledger.claims) == 10
    assert all(item.status is Phase16V5ExecutionStatus.PASS for item in report.case_executions)
    assert ledger.run_outcomes[-1]["reason_code"] == "CONTROLLED_E2E_QUALIFIED"


@pytest.mark.parametrize(
    ("stage_results", "expected_status", "expected_reason"),
    [
        (
            (_StageExecution(False, False, "MODEL_REQUEST_NOT_SENT", "analyst-attempt"),),
            Phase16V5ExecutionStatus.BLOCKED,
            "MODEL_REQUEST_NOT_SENT",
        ),
        (
            (
                _StageExecution(True, True, "ANALYST_VALIDATION_PASS", "analyst-attempt", _AnalysisStub()),
                _StageExecution(False, True, "PLANNER_VALIDATION_FAILED", "planner-attempt"),
            ),
            Phase16V5ExecutionStatus.FAILED,
            "PLANNER_VALIDATION_FAILED",
        ),
    ],
)
def test_v5_runner_closes_first_stage_or_planner_failure_without_later_case_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    stage_results: tuple[_StageExecution, ...],
    expected_status: Phase16V5ExecutionStatus,
    expected_reason: str,
) -> None:
    """首个未发送阻断或已发送 Planner 失败均只能关闭当前 case，不能继续下一个 slot。"""

    ledger = _RecordingLedger()
    runner = _runner(ledger=ledger, model_port=_ValidV5Port())
    results = iter(stage_results)

    async def _stage(**_kwargs: object) -> _StageExecution:
        return next(results)

    monkeypatch.setattr(runner, "_execute_stage", _stage)
    report = asyncio.run(runner.execute(run_kind=Phase16V5RunKind.CALIBRATION))

    assert report.status is expected_status
    assert report.reason_codes == (expected_reason,)
    assert len(ledger.claims) == len(ledger.case_outcomes) == len(ledger.run_outcomes) == 1


def test_v5_stage_protocol_rejects_unsent_failure_bad_receipt_and_semantic_forgery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """共享 Runner 的本地失败、回执不完整和未知 evidence ID 都必须落为不同的严格终态。"""

    ledger = _RecordingLedger()
    runner = _runner(ledger=ledger, model_port=_UnsentFailurePort())
    projection = runner._projections(run_kind=Phase16V5RunKind.CALIBRATION)[0][2]
    claim = ledger.claim_case(run_id="phase16-v5-calibration-001", case_id=projection.case_id, case_digest="a" * 64)
    unsent = asyncio.run(
        runner._execute_stage(
            run_id=claim.run_id,
            claim_id=claim.claim_id,
            projection=projection,
            stage=Phase16V5DispatchStage.ANALYST,
            task=runner._analyst_task(projection),
        )
    )
    assert (unsent.passed, unsent.network_sent, unsent.reason_code) == (False, False, "MODEL_REQUEST_NOT_SENT")

    receipt_ledger = _RecordingLedger()
    receipt_ledger.receipt_complete = False
    receipt_runner = _runner(ledger=receipt_ledger, model_port=_ValidV5Port())
    receipt_projection = receipt_runner._projections(run_kind=Phase16V5RunKind.CALIBRATION)[0][2]
    receipt_claim = receipt_ledger.claim_case(run_id="phase16-v5-calibration-001", case_id=receipt_projection.case_id, case_digest="b" * 64)
    invalid_receipt = asyncio.run(
        receipt_runner._execute_stage(
            run_id=receipt_claim.run_id,
            claim_id=receipt_claim.claim_id,
            projection=receipt_projection,
            stage=Phase16V5DispatchStage.ANALYST,
            task=receipt_runner._analyst_task(receipt_projection),
        )
    )
    assert invalid_receipt.reason_code == "PROVIDER_RECEIPT_INVALID"

    forged_ledger = _RecordingLedger()
    forged_runner = _runner(ledger=forged_ledger, model_port=_ValidV5Port(invalid_analyst_evidence=True))
    forged_projection = forged_runner._projections(run_kind=Phase16V5RunKind.CALIBRATION)[0][2]
    forged_claim = forged_ledger.claim_case(run_id="phase16-v5-calibration-001", case_id=forged_projection.case_id, case_digest="c" * 64)
    forged = asyncio.run(
        forged_runner._execute_stage(
            run_id=forged_claim.run_id,
            claim_id=forged_claim.claim_id,
            projection=forged_projection,
            stage=Phase16V5DispatchStage.ANALYST,
            task=forged_runner._analyst_task(forged_projection),
        )
    )
    assert forged.reason_code == "ANALYST_VALIDATION_FAILED"
    assert forged_ledger.validations[-1]["reason_code"] == "ANALYST_VALIDATION_FAILED"

    async def _raise_before_budget(self, _task):
        """在预算预约前抛错，验证 V5 不能杜撰一个已发送 attempt。"""

        raise RuntimeError("deterministic pre-send test failure")

    monkeypatch.setattr(BoundedSpecialistRunner, "run", _raise_before_budget)
    pre_send_ledger = _RecordingLedger()
    pre_send_runner = _runner(ledger=pre_send_ledger, model_port=_ValidV5Port())
    pre_send_projection = pre_send_runner._projections(run_kind=Phase16V5RunKind.CALIBRATION)[0][2]
    pre_send_claim = pre_send_ledger.claim_case(run_id="phase16-v5-calibration-001", case_id=pre_send_projection.case_id, case_digest="d" * 64)
    pre_send = asyncio.run(
        pre_send_runner._execute_stage(
            run_id=pre_send_claim.run_id,
            claim_id=pre_send_claim.claim_id,
            projection=pre_send_projection,
            stage=Phase16V5DispatchStage.ANALYST,
            task=pre_send_runner._analyst_task(pre_send_projection),
        )
    )
    assert pre_send.reason_code == "RUNNER_PRE_SEND_BLOCKED"


def test_v5_budget_adapter_and_pricing_reject_dynamic_or_over_budget_inputs() -> None:
    """共享 Runner 适配器不得接收自由候选、重复结算或超过冻结 0.03 元的请求。"""

    ledger = _RecordingLedger()
    profile = build_phase16_v5_analyst_profile()
    adapter = _V5BudgetAdapter(
        ledger=ledger,
        run_id="phase16-v5-calibration-001",
        claim_id=str(uuid5(NAMESPACE_URL, "v5-unit-budget-claim")),
        stage=Phase16V5DispatchStage.ANALYST,
        profile=profile,
    )
    request_id = str(uuid5(NAMESPACE_URL, "v5-unit-budget-request"))
    with pytest.raises(Exception, match="candidate"):
        adapter.reserve(request_id, "wrong", Decimal("0.001"))
    with pytest.raises(Exception, match="stage cap"):
        adapter.reserve(request_id, Phase16V5DispatchStage.ANALYST.value, PHASE16_V5_STAGE_RESERVATION_CNY + Decimal("0.000001"))
    adapter.reserve(request_id, Phase16V5DispatchStage.ANALYST.value, Decimal("0.001"))
    assert adapter.settle(request_id, Decimal("0.000100")).created is False
    assert adapter.release(request_id).created is False
    with pytest.raises(Exception, match="matching attempt"):
        adapter.settle(str(uuid5(NAMESPACE_URL, "v5-unit-other-request")), None)
    with pytest.raises(Exception, match="matching attempt"):
        adapter.release(str(uuid5(NAMESPACE_URL, "v5-unit-other-release")))

    request = ModelRequest(
        request_id=str(uuid5(NAMESPACE_URL, "v5-unit-pricing-request")),
        endpoint_host="api.deepseek.com",
        model_id="deepseek-v4-pro",
        temperature=Decimal("0"),
        prompt_hash="a" * 64,
        result_schema_hash="b" * 64,
        messages=(ModelMessage(role="system", content="offline pricing contract"),),
        max_output_tokens=6000,
        deadline_at=datetime(2026, 7, 18, 12, 1, tzinfo=timezone.utc),
    )
    with pytest.raises(Exception, match="frozen stage reservation"):
        _V5PricingPolicy().worst_case_cost(request, profile)
    with pytest.raises(RuntimeError, match="does not permit Skills"):
        asyncio.run(_NoSkillPort().invoke())
    with pytest.raises(ValueError, match="reason code"):
        PostgresPhase16V5CampaignLedger._require_reason("not-safe")
    with pytest.raises(ValueError, match="must be a UUID"):
        PostgresPhase16V5CampaignLedger._require_uuid("not-a-uuid", "attempt_id")


def test_v5_planner_stage_requires_a_validated_analysis_even_after_valid_model_json() -> None:
    """Planner 的 JSON 与 Provider receipt 完整也不足够，缺少 Analyst 语义载荷必须失败。"""

    ledger = _RecordingLedger()
    runner = _runner(ledger=ledger, model_port=_ValidV5Port())
    projection = runner._projections(run_kind=Phase16V5RunKind.CALIBRATION)[0][2]
    claim = ledger.claim_case(
        run_id="phase16-v5-calibration-001",
        case_id=projection.case_id,
        case_digest="e" * 64,
    )
    result = asyncio.run(
        runner._execute_stage(
            run_id=claim.run_id,
            claim_id=claim.claim_id,
            projection=projection,
            stage=Phase16V5DispatchStage.PLANNER,
            task=runner._planner_task(projection, _AnalysisStub()),
            analysis=None,
        )
    )

    assert result.passed is False
    assert result.network_sent is True
    assert result.reason_code == "PLANNER_VALIDATION_FAILED"

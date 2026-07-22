"""Phase 16 正式 smoke Runner 的离线 RED/GREEN 契约。

这些测试只使用冻结数据集、内存模型端口和假账本；它们不读取 .env、不连接 PostgreSQL、
不创建 DeepSeek Adapter。真实联网路径必须在 Task 4 的全部本地门禁后才可执行。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest

from src.decision_support.multi_agent_evaluation import (
    load_phase16_controlled_multi_agent_dataset,
)
from src.decision_support.multi_agent import (
    ConflictAnalysisCode,
    build_phase16_smoke_evidence_analyst_profile,
    validate_conflict_analysis_result,
)
from src.decision_support.official_smoke_evidence import (
    Phase16OfficialPriceEvidence,
    Phase16OfficialSmokeEnvironment,
    load_phase16_official_smoke_evidence_manifest,
    preflight_phase16_official_smoke_evidence,
)
from src.decision_support.official_smoke_ledger import (
    Phase16OfficialSmokeCaseOutcomeStatus,
    Phase16OfficialSmokeDispatchStage,
    Phase16OfficialSmokeValidationVerdict,
)
from src.decision_support.official_smoke_runner import (
    Phase16OfficialSmokeEvidenceConclusion,
    Phase16OfficialSmokeExecutionStatus,
    Phase16OfficialSmokeRunner,
    build_phase16_official_smoke_case_projection,
)
from src.specialist_runtime.model_port import (
    ModelFailure,
    ModelFailureCategory,
    ModelSuccess,
    ModelUsage,
)
from src.specialist_runtime.models import _plain_json, canonical_json_sha256
from src.specialist_runtime.runner import BoundedSpecialistRunner


def _repository_root() -> Path:
    """返回仓库根目录，避免测试把本机工作树的绝对路径冻结进正式身份。"""

    return Path(__file__).resolve().parents[2]


def _dataset():
    """读取已冻结的 48 例 Phase 16 数据集，正式 smoke 只会从其 Manifest 选取十例。"""

    return load_phase16_controlled_multi_agent_dataset(
        _repository_root() / "evaluation" / "phase16_controlled_multi_agent"
    )


def _official_price() -> Phase16OfficialPriceEvidence:
    """构造用户批准的 DeepSeek V4 Flash cache-miss 官方价格快照。"""

    return Phase16OfficialPriceEvidence.create(
        model_id="deepseek-v4-flash",
        endpoint_host="api.deepseek.com",
        input_cny_per_million=Decimal("1.000000"),
        output_cny_per_million=Decimal("2.000000"),
    )


@dataclass(frozen=True)
class _Claim:
    """测试账本中的固定 case claim，只暴露正式 Runner 所需的稳定身份。"""

    claim_id: str
    case_id: str


@dataclass(frozen=True)
class _Attempt:
    """测试账本中的发送意图，模拟 PostgreSQL append-only attempt 行。"""

    attempt_id: str
    claim_id: str
    stage: Phase16OfficialSmokeDispatchStage


class _FormalLedger:
    """最小内存正式账本替身，观察 Runner 调用顺序而不替代 PostgreSQL 集成覆盖。"""

    def __init__(self) -> None:
        self.manifests = []
        self.claims: dict[str, _Claim] = {}
        self.attempts: list[_Attempt] = []
        self.receipts: list[dict] = []
        self.validations: list[dict] = []
        self.outcomes: list[dict] = []

    def ensure_run(self, manifest):
        """记录唯一冻结 Manifest；测试不复制数据库的 schema 迁移职责。"""

        self.manifests.append(manifest.manifest_digest)
        return SimpleNamespace(manifest_digest=manifest.manifest_digest)

    def claim_case(self, case_id: str) -> _Claim:
        """每个 case 仅分配一个确定性 claim，便于断言 10 个固定 slot 都被消费。"""

        claim = self.claims.get(case_id)
        if claim is None:
            claim = _Claim(
                claim_id=str(uuid5(NAMESPACE_URL, f"formal-claim:{case_id}")),
                case_id=case_id,
            )
            self.claims[case_id] = claim
        return claim

    def begin_dispatch(self, *, claim_id: str, stage, profile_digest: str, internal_request_id: str) -> _Attempt:
        """保存发送前 attempt 身份，要求共享 Runner 注入可审计 UUID 请求 ID。"""

        assert profile_digest
        assert len(internal_request_id) == 36
        attempt = _Attempt(
            attempt_id=str(uuid5(NAMESPACE_URL, f"formal-attempt:{claim_id}:{stage.value}")),
            claim_id=claim_id,
            stage=stage,
        )
        self.attempts.append(attempt)
        return attempt

    def append_provider_receipt(self, **facts):
        """只收集脱敏回执字段，测试可确认正式路径没有直接跳过 receipt。"""

        self.receipts.append(facts)
        return SimpleNamespace(**facts)

    def append_validation_fact(self, **facts):
        """记录 Runner 后的结构验证结论，供断言 PASS 不能凭模型成功伪造。"""

        self.validations.append(facts)
        return SimpleNamespace(**facts)

    def close_case(self, **facts):
        """记录最终不可变 case 结论，不在替身中复刻 PostgreSQL 约束细节。"""

        self.outcomes.append(facts)
        return SimpleNamespace(**facts)

    def recover_open_attempts(self):
        """默认没有崩溃遗留；专门的 PostgreSQL 测试覆盖真实恢复状态机。"""

        return ()

    def get_case_outcome(self, *, case_id: str):
        """内存替身只记录当前执行，未持久化跨进程终态时返回空。"""

        del case_id
        return None


class _ValidFormalPort:
    """按共享 Runner 的真实 ModelRequest 动态生成结构化 Analyst/Planner FINAL 回执。"""

    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request):
        """从受控输入重建合法输出，避免测试夹具绕开 Profile Schema 或 EvidenceRef。"""

        self.requests.append(request)
        context = json.loads(request.messages[-1].content)
        # Runner 已通过窄只读 Resolver 注入六角色权威证据；正式输入不重复放入完整
        # Bundle，以免相同 payload 消耗两遍 token 预算而挤掉受限结构化输出空间。
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
                "explanation": "证据显示需要人工确认售罄冲突。",
                "evidence_refs": references,
            }
        else:
            final_output = {
                "options": [
                    {
                        "option_id": "hold-for-operator",
                        "product_strategy": "HOLD_AND_ESCALATE",
                        "backup_product_id": None,
                        "host_prompt": "请运营确认售罄和备品后再继续。",
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
            "reason_summary": "FORMAL_SMOKE_TEST",
        }
        return ModelSuccess(
            request_id=request.request_id,
            model_id=request.model_id,
            output=envelope,
            usage=ModelUsage(input_tokens=40, output_tokens=60, total_tokens=100),
            provider_response_id=f"formal-provider-{len(self.requests):03d}",
            finish_reason="stop",
            response_digest=canonical_json_sha256(envelope),
            latency_ms=Decimal("2.000"),
        )


class _UnsentFailureFormalPort:
    """明确声明未离开进程的端口失败，验证预约不等于实际网络发送。"""

    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request):
        """返回 ``request_sent=False``，正式账本必须关闭为 BLOCKED 而非 FAILED。"""

        self.requests.append(request)
        return ModelFailure(
            request_id=request.request_id,
            category=ModelFailureCategory.TRANSPORT_ERROR,
            request_sent=False,
            latency_ms=Decimal("0"),
        )


class _SentFailureFormalPort:
    """明确已离开进程的端口失败，验证正式结论不能被降级为 BLOCKED。"""

    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request):
        """返回 ``request_sent=True``，即使没有 Provider receipt 也必须保守 FAILED。"""

        self.requests.append(request)
        return ModelFailure(
            request_id=request.request_id,
            category=ModelFailureCategory.TRANSPORT_ERROR,
            request_sent=True,
            latency_ms=Decimal("1"),
        )


class _RaisingFormalPort:
    """模拟端口在请求边界后抛异常，调用方无法证明未发送时必须 fail-closed。"""

    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request):
        """记录收到的请求后抛出，不产生可伪造的 Provider receipt。"""

        self.requests.append(request)
        raise RuntimeError("unknown transport completion")


class _MalformedFormalPort:
    """以 Provider 成功回执承载三种坏结构，验证 Runner 后校验不能被绕过。"""

    def __init__(self, *, mode: str) -> None:
        self.requests = []
        self._mode = mode

    async def complete(self, request):
        """构造坏 AgentAction、坏 Schema 或伪造引用，不泄漏任何真实模型正文。"""

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
        if self._mode == "invalid_action":
            # Profile 是零 Skill；合法但不允许的 CALL_SKILL 不能借 Provider 成功变成 PASS。
            envelope = {
                "kind": "CALL_SKILL",
                "skill_id": "forbidden_skill",
                "arguments": {},
                "evidence_refs": references,
                "reason_summary": "MALFORMED_ACTION",
            }
        elif self._mode == "invalid_schema":
            envelope = {
                "kind": "FINAL",
                "final_output": {"unexpected": "schema-invalid"},
                "evidence_refs": references,
                "reason_summary": "MALFORMED_SCHEMA",
            }
        elif self._mode == "forged_evidence":
            forged_references = [dict(item) for item in references]
            forged_references[0]["digest"] = "0" * 64
            envelope = {
                "kind": "FINAL",
                "final_output": {
                    "finding_codes": context["input_snapshot"]["trigger_codes"],
                    "constraint_codes": [],
                    "risk_codes": ["HUMAN_CONFIRMATION_REQUIRED"],
                    "explanation": "伪造引用不得通过权威 Resolver。",
                    "evidence_refs": forged_references,
                },
                "evidence_refs": forged_references,
                "reason_summary": "FORGED_EVIDENCE",
            }
        else:
            raise AssertionError(f"unexpected malformed mode: {self._mode}")
        return ModelSuccess(
            request_id=request.request_id,
            model_id=request.model_id,
            output=envelope,
            usage=ModelUsage(input_tokens=40, output_tokens=60, total_tokens=100),
            provider_response_id=f"malformed-provider-{self._mode}",
            finish_reason="stop",
            response_digest=canonical_json_sha256(envelope),
            latency_ms=Decimal("2.000"),
        )


def _ready_formal_runner(*, ledger, model_port, clock=None) -> Phase16OfficialSmokeRunner:
    """装配通过离线预检的正式 Runner，避免每个负向测试复制冻结身份。"""

    dataset = _dataset()
    official_price = _official_price()
    manifest = load_phase16_official_smoke_evidence_manifest(
        repository_root=_repository_root()
    )
    preflight = preflight_phase16_official_smoke_evidence(
        dataset=dataset,
        official_price=official_price,
        environment=Phase16OfficialSmokeEnvironment(
            model_id="deepseek-v4-flash",
            endpoint_host="api.deepseek.com",
            credential_configured=True,
        ),
    )
    return Phase16OfficialSmokeRunner(
        dataset=dataset,
        manifest=manifest,
        preflight=preflight,
        official_price=official_price,
        ledger=ledger,
        model_port=model_port,
        clock=clock,
    )


def test_formal_projection_rebuilds_six_role_evidence_without_evaluation_metadata() -> None:
    """正式 Analyst 输入只含治理后的证据，不能泄漏 split、label、case ID 或预期路由。"""

    dataset = load_phase16_controlled_multi_agent_dataset(
        _repository_root() / "evaluation" / "phase16_controlled_multi_agent"
    )
    case_id = dataset.manifest.smoke_eligible_case_ids[0]

    projection = build_phase16_official_smoke_case_projection(
        dataset=dataset,
        case_id=case_id,
    )

    visible_input = json.dumps(
        _plain_json(projection.analyst_task.input_snapshot),
        ensure_ascii=False,
        sort_keys=True,
    )
    assert len(projection.evidence_refs) == 6
    assert projection.analyst_task.evaluation_case_id is None
    assert projection.analyst_task.profile_id == "phase16_smoke_evidence_analyst"
    assert projection.planner_profile_id == "phase16_smoke_evidence_planner"
    assert set(projection.analyst_task.input_snapshot) == {
        "trigger_codes",
        "evidence_bundle_digest",
    }
    assert case_id not in visible_input
    assert '"evidence_bundle"' not in visible_input
    assert '"split"' not in visible_input
    assert '"expected_route"' not in visible_input
    assert '"label"' not in visible_input
    assert projection.evidence_registry.resolve_many(
        projection.evidence_refs,
        expected_room_id=projection.analyst_task.room_id,
        expected_anchor_id=projection.trusted_anchor_id,
    )


def test_formal_runner_uses_bounded_runner_for_each_frozen_two_stage_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """10 个正式 slot 都必须经过共享 Runner 的 Analyst 后 Planner 验证，再产生账本 PASS。"""

    dataset = _dataset()
    official_price = _official_price()
    manifest = load_phase16_official_smoke_evidence_manifest(
        repository_root=_repository_root()
    )
    preflight = preflight_phase16_official_smoke_evidence(
        dataset=dataset,
        official_price=official_price,
        environment=Phase16OfficialSmokeEnvironment(
            model_id="deepseek-v4-flash",
            endpoint_host="api.deepseek.com",
            credential_configured=True,
        ),
    )
    ledger = _FormalLedger()
    model_port = _ValidFormalPort()
    run_calls = []
    run_results = []
    original_run = BoundedSpecialistRunner.run

    async def _recording_run(self, task):
        """保留共享 Runner 原实现，同时记录 Formal Runner 没有走直接模型旁路。"""

        run_calls.append(task)
        result = await original_run(self, task)
        # 外层会把 Runner 异常收敛成正式终态；保留原始结果，断言可精确暴露 reserve 前
        # 的阻断原因，而不是只看到笼统的 ``FORMAL_RUNNER_PRE_SEND_BLOCKED``。
        run_results.append(result)
        return result

    monkeypatch.setattr(BoundedSpecialistRunner, "run", _recording_run)
    runner = Phase16OfficialSmokeRunner(
        dataset=dataset,
        manifest=manifest,
        preflight=preflight,
        official_price=official_price,
        ledger=ledger,
        model_port=model_port,
    )

    report = asyncio.run(runner.execute())

    assert all(item.status.value == "SUCCEEDED" for item in run_results), [
        item.model_dump(mode="json") for item in run_results
    ]
    assert run_calls and run_results
    validate_conflict_analysis_result(
        task=run_calls[0],
        result=run_results[0],
        expected_profile=build_phase16_smoke_evidence_analyst_profile(),
        expected_evidence_refs=run_calls[0].initial_evidence_refs,
        expected_finding_codes=tuple(
            ConflictAnalysisCode(item)
            for item in run_calls[0].input_snapshot["trigger_codes"]
        ),
    )
    assert report.status is Phase16OfficialSmokeExecutionStatus.PASS, {
        "report": report,
        "results": [item.model_dump(mode="json") for item in run_results],
        "validations": ledger.validations,
    }
    assert len(run_calls) == len(dataset.manifest.smoke_eligible_case_ids) * 2
    assert len(model_port.requests) == 20
    assert len(ledger.claims) == 10
    assert len(ledger.attempts) == len(ledger.receipts) == len(ledger.validations) == 20
    assert len(ledger.outcomes) == 10
    assert all(
        item["verdict"] is Phase16OfficialSmokeValidationVerdict.PASS
        for item in ledger.validations
    )
    assert all(
        item["status"] is Phase16OfficialSmokeCaseOutcomeStatus.PASS
        for item in ledger.outcomes
    )
    for request in model_port.requests:
        context = json.loads(request.messages[-1].content)
        visible = request.messages[-1].content
        assert "evidence_bundle" not in context["input_snapshot"]
        assert len(context["resolved_evidence"]) == 6
        assert '"split"' not in visible
        assert '"expected_route"' not in visible
        assert '"label"' not in visible


def test_formal_runner_skips_authenticated_recovered_pass_slot_without_redispatch() -> None:
    """重启补写的 PASS slot 只能读取跳过，剩余 slot 才可继续走唯一共享 Runner 路径。"""

    dataset = _dataset()
    recovered_case_id = dataset.manifest.smoke_eligible_case_ids[0]

    class _RecoveredPassLedger(_FormalLedger):
        """模拟 PostgreSQL 已完成 HMAC 复验的 PASS outcome，不复制真实账本认证实现。"""

        def _recovered_outcome(self):
            """返回最小正式终态投影，真实 Store 会在读取前校验两条 receipt 的 HMAC。"""

            return SimpleNamespace(
                case_id=recovered_case_id,
                status=Phase16OfficialSmokeCaseOutcomeStatus.PASS,
                reason_code="RECOVERED_VALIDATED_PASS",
            )

        def recover_open_attempts(self):
            """模拟进程重启时由 PostgreSQL 完整 PASS 链补写 outcome 的唯一事实。"""

            return (self._recovered_outcome(),)

        def get_case_outcome(self, *, case_id: str):
            """仅首个恢复 slot 已终态化；其他固定 slot 仍由本轮严格执行。"""

            return self._recovered_outcome() if case_id == recovered_case_id else None

    ledger = _RecoveredPassLedger()
    model_port = _ValidFormalPort()

    report = asyncio.run(_ready_formal_runner(ledger=ledger, model_port=model_port).execute())

    # 已认证的历史 PASS 不是本轮新发送；当前进程只允许执行其余九例的 Analyst/Planner。
    assert report.status is Phase16OfficialSmokeExecutionStatus.PASS
    assert report.model_calls == 18
    assert len(model_port.requests) == len(ledger.attempts) == 18
    assert recovered_case_id not in ledger.claims
    assert len(report.case_executions) == 10
    assert report.case_executions[0].reason_code == "RECOVERED_VALIDATED_PASS"


def test_formal_runner_marks_pre_send_block_as_inconclusive_without_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首个模型请求尚未离开进程时，正式 run 只能是 BLOCKED + INCONCLUSIVE。"""

    dataset = _dataset()
    official_price = _official_price()
    manifest = load_phase16_official_smoke_evidence_manifest(
        repository_root=_repository_root()
    )
    preflight = preflight_phase16_official_smoke_evidence(
        dataset=dataset,
        official_price=official_price,
        environment=Phase16OfficialSmokeEnvironment(
            model_id="deepseek-v4-flash",
            endpoint_host="api.deepseek.com",
            credential_configured=True,
        ),
    )
    ledger = _FormalLedger()
    model_port = _ValidFormalPort()

    async def _raise_before_reserve(self, _task):
        """模拟共享 Runner 在预算预约/发送之前遇到本地依赖错误。"""

        raise RuntimeError("local pre-send dependency failed")

    monkeypatch.setattr(BoundedSpecialistRunner, "run", _raise_before_reserve)
    report = asyncio.run(
        Phase16OfficialSmokeRunner(
            dataset=dataset,
            manifest=manifest,
            preflight=preflight,
            official_price=official_price,
            ledger=ledger,
            model_port=model_port,
        ).execute()
    )

    assert report.status is Phase16OfficialSmokeExecutionStatus.BLOCKED
    assert report.evidence_conclusion == "INCONCLUSIVE"
    assert report.model_calls == 0
    assert model_port.requests == []
    assert ledger.attempts == []
    assert ledger.outcomes == [
        {
            "claim_id": ledger.claims[dataset.manifest.smoke_eligible_case_ids[0]].claim_id,
            "status": Phase16OfficialSmokeCaseOutcomeStatus.BLOCKED,
            "reason_code": "FORMAL_RUNNER_PRE_SEND_BLOCKED",
        }
    ]


def test_formal_runner_reports_ledger_initialization_block_without_dispatch() -> None:
    """账本 schema/连接尚不可用时不得抛出半成品异常或创建任何模型 dispatch。"""

    dataset = _dataset()
    official_price = _official_price()
    manifest = load_phase16_official_smoke_evidence_manifest(
        repository_root=_repository_root()
    )
    preflight = preflight_phase16_official_smoke_evidence(
        dataset=dataset,
        official_price=official_price,
        environment=Phase16OfficialSmokeEnvironment(
            model_id="deepseek-v4-flash",
            endpoint_host="api.deepseek.com",
            credential_configured=True,
        ),
    )

    class _UnavailableLedger(_FormalLedger):
        """模拟 PostgreSQL migration/schema contract 在任何 claim 前拒绝初始化。"""

        def ensure_run(self, _manifest):
            """发送前抛出稳定本地错误，不能被解释成模型失败。"""

            raise RuntimeError("formal ledger is unavailable")

    ledger = _UnavailableLedger()
    model_port = _ValidFormalPort()
    report = asyncio.run(
        Phase16OfficialSmokeRunner(
            dataset=dataset,
            manifest=manifest,
            preflight=preflight,
            official_price=official_price,
            ledger=ledger,
            model_port=model_port,
        ).execute()
    )

    assert report.status is Phase16OfficialSmokeExecutionStatus.BLOCKED
    assert report.evidence_conclusion == "INCONCLUSIVE"
    assert report.reason_codes == ("LEDGER_INITIALIZATION_BLOCKED",)
    assert report.model_calls == 0
    assert model_port.requests == []
    assert ledger.claims == {}


def test_formal_runner_marks_post_reservation_deadline_as_unsent_blocked() -> None:
    """预算预约后 deadline 到期未进端口时，不能把 intent 误报成发送失败。"""

    base = datetime(2026, 7, 22, tzinfo=timezone.utc)
    instants = iter(
        (
            base,
            # Projection、Runner start、deadline 计算和 reserve 前检查均在 deadline 内。
            base,
            base,
            base,
            # reserve 完成后才跨过 30 秒 deadline，ModelPort 不应被调用。
            base + timedelta(seconds=31),
        )
    )
    ledger = _FormalLedger()
    port = _ValidFormalPort()

    report = asyncio.run(
        _ready_formal_runner(
            ledger=ledger,
            model_port=port,
            clock=lambda: next(instants),
        ).execute()
    )

    assert report.status is Phase16OfficialSmokeExecutionStatus.BLOCKED
    assert report.evidence_conclusion is Phase16OfficialSmokeEvidenceConclusion.INCONCLUSIVE
    assert report.model_calls == 0
    assert port.requests == []
    assert len(ledger.attempts) == 1
    assert ledger.receipts == []
    assert len(ledger.validations) == 1
    validation = ledger.validations[0]
    assert validation["attempt_id"] == ledger.attempts[0].attempt_id
    assert validation["verdict"] is Phase16OfficialSmokeValidationVerdict.BLOCKED
    assert validation["reason_code"] == "MODEL_REQUEST_NOT_SENT"
    assert len(validation["validation_digest"]) == 64
    assert ledger.outcomes[0]["status"] is Phase16OfficialSmokeCaseOutcomeStatus.BLOCKED


def test_formal_runner_marks_explicit_unsent_model_failure_as_blocked() -> None:
    """端口明确声明 request_sent=False 时，预约账本必须闭合为零发送不确定结论。"""

    ledger = _FormalLedger()
    port = _UnsentFailureFormalPort()

    report = asyncio.run(_ready_formal_runner(ledger=ledger, model_port=port).execute())

    assert report.status is Phase16OfficialSmokeExecutionStatus.BLOCKED
    assert report.evidence_conclusion is Phase16OfficialSmokeEvidenceConclusion.INCONCLUSIVE
    assert report.model_calls == 0
    assert len(port.requests) == len(ledger.attempts) == len(ledger.validations) == 1
    assert ledger.receipts == []
    assert ledger.validations[0]["verdict"] is Phase16OfficialSmokeValidationVerdict.BLOCKED
    assert ledger.validations[0]["reason_code"] == "MODEL_REQUEST_NOT_SENT"
    assert ledger.outcomes[0]["status"] is Phase16OfficialSmokeCaseOutcomeStatus.BLOCKED


@pytest.mark.parametrize("port_type", (_SentFailureFormalPort, _RaisingFormalPort))
def test_formal_runner_marks_sent_or_unknown_port_boundary_as_failed(port_type) -> None:
    """request_sent=True 或端口异常都不能被误报为零发送的 BLOCKED 结论。"""

    ledger = _FormalLedger()
    port = port_type()

    report = asyncio.run(_ready_formal_runner(ledger=ledger, model_port=port).execute())

    assert report.status is Phase16OfficialSmokeExecutionStatus.FAILED
    assert report.evidence_conclusion is Phase16OfficialSmokeEvidenceConclusion.FAILED
    assert report.model_calls == 1
    assert len(port.requests) == len(ledger.attempts) == len(ledger.validations) == 1
    assert ledger.receipts == []
    assert ledger.validations[0]["verdict"] is Phase16OfficialSmokeValidationVerdict.FAILED
    assert ledger.outcomes[0]["status"] is Phase16OfficialSmokeCaseOutcomeStatus.FAILED


@pytest.mark.parametrize("mode", ("invalid_action", "invalid_schema", "forged_evidence"))
def test_formal_runner_rejects_bad_model_protocol_before_planner_dispatch(mode: str) -> None:
    """非法动作、Schema 或引用只能追加 FAILED validation，绝不能产生 Planner 或 PASS。"""

    ledger = _FormalLedger()
    port = _MalformedFormalPort(mode=mode)

    report = asyncio.run(_ready_formal_runner(ledger=ledger, model_port=port).execute())

    assert report.status is Phase16OfficialSmokeExecutionStatus.FAILED
    assert report.evidence_conclusion is Phase16OfficialSmokeEvidenceConclusion.FAILED
    assert report.model_calls == 1
    assert len(port.requests) == len(ledger.attempts) == len(ledger.receipts) == 1
    assert len(ledger.validations) == 1
    assert ledger.validations[0]["verdict"] is Phase16OfficialSmokeValidationVerdict.FAILED
    assert ledger.outcomes[0]["status"] is Phase16OfficialSmokeCaseOutcomeStatus.FAILED

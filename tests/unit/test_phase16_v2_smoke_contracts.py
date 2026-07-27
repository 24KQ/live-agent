"""Phase 16 v2 正式 smoke 的离线身份契约。

v1 已经发生过一次真实发送并以 FAILED 收口。本文件只验证 v2 能以新的 Profile
身份开展独立实验，绝不重写 v1 Manifest、账本或历史闭包审计，也不创建网络端口。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.decision_support.models import ConflictAnalysisCode
from src.decision_support.multi_agent import (
    build_phase16_smoke_evidence_analyst_profile,
    build_phase16_smoke_evidence_planner_profile,
    build_phase16_smoke_evidence_v2_analyst_profile,
    build_phase16_smoke_evidence_v2_planner_profile,
    validate_v2_conflict_analysis_result,
    validate_v2_live_decision_planner_result,
)
from src.decision_support.official_smoke_evidence_v2 import (
    Phase16OfficialPriceEvidence,
    Phase16OfficialSmokeV2ReceiptError,
    load_phase16_official_smoke_v2_parent_dataset,
    validate_phase16_official_smoke_v2_receipt,
)
from src.decision_support.official_smoke_runner_v2 import _OfficialSmokePricingPolicy
from src.specialist_runtime.model_port import ModelSuccess, ModelUsage
from src.specialist_runtime.profiles import FinalEvidenceBindingMode
from src.specialist_runtime.runner import BudgetInvariantError
from src.specialist_runtime.models import (
    AgentAction,
    AgentActionKind,
    AgentResult,
    AgentResultStatus,
    AgentTask,
    EvidenceKind,
    EvidenceRef,
)


def _repository_root() -> Path:
    """返回当前隔离工作树根，测试不得依赖用户根目录的未提交文件。"""

    return Path(__file__).resolve().parents[2]


def _v2_price() -> Phase16OfficialPriceEvidence:
    """构造 V2 固定的 Pro cache-miss 价格，不读取本机密钥或访问供应商页面。"""

    return Phase16OfficialPriceEvidence.create(
        model_id="deepseek-v4-pro",
        endpoint_host="api.deepseek.com",
        input_cny_per_million=Decimal("3.000000"),
        output_cny_per_million=Decimal("6.000000"),
    )


def _v2_reference() -> EvidenceRef:
    """构造 V2 受控 ID 测试所需的最小权威引用，不包含任何模型自由文本。"""

    return EvidenceRef(
        kind=EvidenceKind.EVENT,
        evidence_id="phase16-v2-event-001",
        source_version="1",
        digest="e" * 64,
        anchor_id="phase16-v2-anchor-001",
        room_id="phase16-v2-room-001",
    )


def _v2_analyst_task(profile) -> AgentTask:
    """构造与冻结 V2 Analyst Profile 精确匹配的离线任务，避免测试伪造身份。"""

    reference = _v2_reference()
    return AgentTask(
        task_id="phase16-v2-analyst-contract-001",
        task_kind=profile.task_kind,
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        room_id=reference.room_id,
        trace_id="phase16-v2-trace-001",
        objective="仅根据受控证据标识输出冲突分析。",
        input_snapshot={"trigger_codes": ["AVAILABILITY_NOISE_HIGH"]},
        initial_evidence_refs=(reference,),
    )


def _v2_analyst_result(
    task: AgentTask,
    output: object,
    *,
    action_evidence_refs: tuple[EvidenceRef, ...] = (),
    result_task_id: str | None = None,
) -> AgentResult:
    """构造已通过共享 Runner FINAL 协议的结果，供 Coordinator 二次边界测试使用。"""

    # V2 的 FINAL 动作通常不携带完整 EvidenceRef；可选入参只用于构造攻击者伪造
    # 权威引用的独立结果，验证协调器会在读取模型正文前立即拒绝该行为。
    action = AgentAction(
        kind=AgentActionKind.FINAL,
        final_output=output,
        evidence_refs=action_evidence_refs,
        reason_summary="PHASE16_V2_TEST_FINAL",
    )
    return AgentResult(
        task_id=result_task_id or task.task_id,
        profile_id=task.profile_id,
        profile_version=task.profile_version,
        status=AgentResultStatus.SUCCEEDED,
        output=output,
        actions=(action,),
        evidence_refs=task.initial_evidence_refs,
        summary="PHASE16_V2_TEST_SUCCEEDED",
    )


def _v2_analyst_output(*, evidence_ids: list[str] | None = None) -> dict[str, object]:
    """返回一份最小合法分析载荷，单个测试可只覆写目标字段来验证 fail-closed 分支。"""

    return {
        "constraint_codes": [],
        "risk_codes": ["HUMAN_CONFIRMATION_REQUIRED"],
        "explanation": "受控证据显示冲突必须由人工确认。",
        "evidence_ids": [_v2_reference().evidence_id] if evidence_ids is None else evidence_ids,
    }


def test_v2_analysis_rehydrates_system_owned_facts_after_controlled_id_validation() -> None:
    """V2 仅接受模型选出的受控 ID，并把 trigger 与完整引用从任务权威事实重新注入。"""

    profile = build_phase16_smoke_evidence_v2_analyst_profile()
    task = _v2_analyst_task(profile)
    result = _v2_analyst_result(task, _v2_analyst_output())

    validated = validate_v2_conflict_analysis_result(
        task=task,
        result=result,
        expected_profile=profile,
        expected_evidence_refs=task.initial_evidence_refs,
        expected_finding_codes=(ConflictAnalysisCode.AVAILABILITY_NOISE_HIGH,),
    )

    # 断言模型没有写入 finding 或完整摘要，但下游载荷仍具备不可伪造的权威父链。
    assert tuple(validated.finding_codes) == (
        ConflictAnalysisCode.AVAILABILITY_NOISE_HIGH,
    )
    assert validated.evidence_refs == task.initial_evidence_refs


@pytest.mark.parametrize(
    ("evidence_ids", "reason"),
    [
        ([], "empty"),
        (["phase16-v2-event-001", "phase16-v2-event-001"], "duplicate"),
        (["phase16-v2-forged-event"], "untrusted"),
    ],
)
def test_v2_analysis_rejects_non_authoritative_evidence_id_selections(
    evidence_ids: list[str], reason: str
) -> None:
    """空、重复或未知 ID 均不得触发系统回填，防止模型扩大可见证据边界。"""

    profile = build_phase16_smoke_evidence_v2_analyst_profile()
    task = _v2_analyst_task(profile)
    result = _v2_analyst_result(task, _v2_analyst_output(evidence_ids=evidence_ids))

    with pytest.raises(ValueError, match="V2 analysis output fields are invalid"):
        validate_v2_conflict_analysis_result(
            task=task,
            result=result,
            expected_profile=profile,
            expected_evidence_refs=task.initial_evidence_refs,
            expected_finding_codes=(ConflictAnalysisCode.AVAILABILITY_NOISE_HIGH,),
        )
    assert reason in {"empty", "duplicate", "untrusted"}


def test_v2_analysis_rejects_extra_fields_and_model_owned_full_evidence_refs() -> None:
    """模型既不能扩张结果 Schema，也不能在 FINAL 动作中伪造完整 EvidenceRef。"""

    profile = build_phase16_smoke_evidence_v2_analyst_profile()
    task = _v2_analyst_task(profile)
    expanded = _v2_analyst_output()
    expanded["finding_codes"] = ["AVAILABILITY_NOISE_HIGH"]
    with pytest.raises(ValueError, match="unexpected fields"):
        validate_v2_conflict_analysis_result(
            task=task,
            result=_v2_analyst_result(task, expanded),
            expected_profile=profile,
            expected_evidence_refs=task.initial_evidence_refs,
            expected_finding_codes=(ConflictAnalysisCode.AVAILABILITY_NOISE_HIGH,),
        )

    # 这里从构造入口生成伪造 FINAL，证明即使完整引用字段值正确，所有权仍属于系统而非模型。
    forged_result = _v2_analyst_result(
        task,
        _v2_analyst_output(),
        action_evidence_refs=task.initial_evidence_refs,
    )
    with pytest.raises(ValueError, match="system-owned evidence"):
        validate_v2_conflict_analysis_result(
            task=task,
            result=forged_result,
            expected_profile=profile,
            expected_evidence_refs=task.initial_evidence_refs,
            expected_finding_codes=(ConflictAnalysisCode.AVAILABILITY_NOISE_HIGH,),
        )


@pytest.mark.parametrize(
    "output_patch",
    [
        {"constraint_codes": ["OPERATOR_CONFIRMATION_REQUIRED", "OPERATOR_CONFIRMATION_REQUIRED"]},
        {"risk_codes": ["HUMAN_CONFIRMATION_REQUIRED", "HUMAN_CONFIRMATION_REQUIRED"]},
        {"explanation": " "},
    ],
)
def test_v2_analysis_rejects_duplicate_or_display_unsafe_generated_fields(
    output_patch: dict[str, object]
) -> None:
    """系统注入事实不意味着放宽生成字段：枚举去重与展示文本门禁必须继续 fail-closed。"""

    profile = build_phase16_smoke_evidence_v2_analyst_profile()
    task = _v2_analyst_task(profile)
    output = _v2_analyst_output()
    output.update(output_patch)

    with pytest.raises(ValueError, match="V2 analysis output"):
        validate_v2_conflict_analysis_result(
            task=task,
            result=_v2_analyst_result(task, output),
            expected_profile=profile,
            expected_evidence_refs=task.initial_evidence_refs,
            expected_finding_codes=(ConflictAnalysisCode.AVAILABILITY_NOISE_HIGH,),
        )


def test_v2_analysis_rejects_legacy_binding_non_object_output_and_wrong_result_identity() -> None:
    """V2 验证器必须拒绝旧引用模式、数组结果和错 task 身份，不能只依赖模型成功状态。"""

    profile = build_phase16_smoke_evidence_v2_analyst_profile()
    task = _v2_analyst_task(profile)
    expected_findings = (ConflictAnalysisCode.AVAILABILITY_NOISE_HIGH,)

    # 旧 V1 Profile 未声明 system-managed 模式，不能被 V2 运行器误装配后获得发送资格。
    with pytest.raises(ValueError, match="system-managed evidence binding mode"):
        validate_v2_conflict_analysis_result(
            task=task,
            result=_v2_analyst_result(task, _v2_analyst_output()),
            expected_profile=build_phase16_smoke_evidence_analyst_profile(),
            expected_evidence_refs=task.initial_evidence_refs,
            expected_finding_codes=expected_findings,
        )

    # 即使 JSON 编码允许数组，正式 Analyst 输出也必须是对象，不能让数组逃过字段白名单。
    with pytest.raises(ValueError, match="final output must be an object"):
        validate_v2_conflict_analysis_result(
            task=task,
            result=_v2_analyst_result(task, []),
            expected_profile=profile,
            expected_evidence_refs=task.initial_evidence_refs,
            expected_finding_codes=expected_findings,
        )

    # 同一 Profile 下错 task_id 也必须失败，避免跨 case 的成功回执被重放到当前 case。
    with pytest.raises(ValueError, match="identity or status"):
        validate_v2_conflict_analysis_result(
            task=task,
            result=_v2_analyst_result(
                task,
                _v2_analyst_output(),
                result_task_id="phase16-v2-analyst-contract-forged",
            ),
            expected_profile=profile,
            expected_evidence_refs=task.initial_evidence_refs,
            expected_finding_codes=expected_findings,
        )


@pytest.mark.parametrize(
    ("output", "proposal_eligible_and_fresh", "reason"),
    [
        ({"options": []}, False, "not eligible"),
        ({"options": "not-a-list"}, True, "options are invalid"),
        ({"options": [{"option_id": "missing-evidence-ids"}]}, True, "lacks controlled evidence IDs"),
    ],
)
def test_v2_planner_rejects_ineligible_or_malformed_controlled_outputs(
    output: dict[str, object], proposal_eligible_and_fresh: bool, reason: str
) -> None:
    """Planner 在写入 Proposal 前必须分别拒绝 freshness、类型与受控 ID 协议违规。"""

    profile = build_phase16_smoke_evidence_v2_planner_profile()
    task = AgentTask(
        task_id="phase16-v2-planner-contract-001",
        task_kind=profile.task_kind,
        profile_id=profile.profile_id,
        profile_version=profile.profile_version,
        room_id=_v2_reference().room_id,
        trace_id="phase16-v2-trace-001",
        objective="仅输出受控证据标识的人工确认方案。",
        input_snapshot={"analysis": "controlled"},
        initial_evidence_refs=(_v2_reference(),),
    )

    with pytest.raises(ValueError, match=reason):
        validate_v2_live_decision_planner_result(
            task=task,
            result=_v2_analyst_result(task, output),
            expected_profile=profile,
            expected_evidence_refs=task.initial_evidence_refs,
            required_risk_codes=frozenset(),
            available_backup_product_ids=frozenset(),
            proposal_eligible_and_fresh=proposal_eligible_and_fresh,
        )


def test_v2_profiles_are_distinct_from_immutable_v1_and_use_system_owned_facts() -> None:
    """v2 必须使用新身份，并把哈希回声和 finding code 回填留在系统侧。"""

    v1_analyst = build_phase16_smoke_evidence_analyst_profile()
    v1_planner = build_phase16_smoke_evidence_planner_profile()
    v2_analyst = build_phase16_smoke_evidence_v2_analyst_profile()
    v2_planner = build_phase16_smoke_evidence_v2_planner_profile()

    # 已发送的 v1 身份不可修改；新实验不得复用其 Profile digest。
    assert v1_analyst.profile_version == v1_planner.profile_version == "1.0.0"
    assert v2_analyst.profile_version == v2_planner.profile_version == "2.0.0"
    assert v2_analyst.profile_digest != v1_analyst.profile_digest
    assert v2_planner.profile_digest != v1_planner.profile_digest

    # V2 只用于受控 smoke：60 秒允许 Pro 完成受限 JSON，6000/2800 token 同时给
    # 推理与输出设置可审计上限，避免旧试验的 8000/8000 与固定预约不一致。
    assert {profile.model_id for profile in (v2_analyst, v2_planner)} == {"deepseek-v4-pro"}
    assert {
        (profile.deadline_seconds, profile.max_total_tokens, profile.max_output_tokens)
        for profile in (v2_analyst, v2_planner)
    } == {(60, 6000, 2800)}
    assert all(profile.allowed_skill_ids == () and profile.max_model_calls == 1 for profile in (v2_analyst, v2_planner))
    assert {
        profile.final_evidence_binding_mode for profile in (v2_analyst, v2_planner)
    } == {FinalEvidenceBindingMode.SYSTEM_MANAGED_IDS}

    # 历史 V1 没有该字段，None 必须继续表示完整 EvidenceRef 模式，保证新增字段不会
    # 回溯修改已发送 Profile 的 digest。
    assert v1_analyst.final_evidence_binding_mode is None
    assert v1_planner.final_evidence_binding_mode is None

    # finding_codes 与完整 EvidenceRef 由已验证的系统事实注入，模型只能选择受控 ID。
    assert "finding_codes" not in v2_analyst.result_schema["required"]
    assert "finding_codes" not in v2_analyst.result_schema["properties"]
    assert "evidence_ids" in v2_analyst.result_schema["properties"]
    assert "evidence_refs" not in v2_analyst.result_schema["properties"]


def test_v2_parent_dataset_reuses_only_frozen_data_facts() -> None:
    """V2 可复验原始 case/label/script 字节，且不会借 V1 源码摘要错误认证新代码。"""

    dataset = load_phase16_official_smoke_v2_parent_dataset(
        _repository_root() / "evaluation" / "phase16_controlled_multi_agent"
    )

    # 十例资格仍由原始旁路标签决定，模型任务不会读取 label、split 或 expected route。
    assert len(dataset.cases) == len(dataset.labels) == len(dataset.scripts) == 48
    assert len(dataset.manifest.smoke_eligible_case_ids) == 10
    assert all(
        dataset.labels[case_id].smoke_eligible
        for case_id in dataset.manifest.smoke_eligible_case_ids
    )


def test_v2_receipt_rejects_non_stop_finish_reason() -> None:
    """正式 V2 只接受自然停止的供应商回执，截断或工具调用不能伪装成结构成功。"""

    facts = {
        "request_id": "phase16-v2-receipt-unit",
        "model_id": "deepseek-v4-pro",
        "output": {"kind": "FINAL"},
        "usage": ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        "response_digest": "a" * 64,
        "latency_ms": Decimal("1.000"),
        "provider_response_id": "chatcmpl-v2-unit",
    }
    with pytest.raises(Phase16OfficialSmokeV2ReceiptError, match="finish_reason=stop"):
        validate_phase16_official_smoke_v2_receipt(
            ModelSuccess(**facts, finish_reason="length")
        )

    validate_phase16_official_smoke_v2_receipt(
        ModelSuccess(**facts, finish_reason="stop")
    )


def test_v2_pricing_blocks_oversized_stage_before_budget_reservation() -> None:
    """V2 不能把估算费用截断为预约上限，超额请求必须在模型端口前停止。"""

    pricing = _OfficialSmokePricingPolicy(_v2_price())
    profile = build_phase16_smoke_evidence_v2_analyst_profile()
    # 使用巨大但本地构造的 messages 模拟 Prompt 意外膨胀；测试不调用模型或数据库。
    request = SimpleNamespace(
        max_output_tokens=profile.max_output_tokens,
        model_dump=lambda **_kwargs: {"messages": ["x" * 200_000]},
    )

    with pytest.raises(BudgetInvariantError, match="reservation exceeds"):
        pricing.worst_case_cost(request, profile)

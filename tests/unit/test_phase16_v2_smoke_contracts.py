"""Phase 16 v2 正式 smoke 的离线身份契约。

v1 已经发生过一次真实发送并以 FAILED 收口。本文件只验证 v2 能以新的 Profile
身份开展独立实验，绝不重写 v1 Manifest、账本或历史闭包审计，也不创建网络端口。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.decision_support.multi_agent import (
    build_phase16_smoke_evidence_analyst_profile,
    build_phase16_smoke_evidence_planner_profile,
    build_phase16_smoke_evidence_v2_analyst_profile,
    build_phase16_smoke_evidence_v2_planner_profile,
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
